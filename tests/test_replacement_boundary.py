"""Plan replacement boundary (M1.1 Task 3).

Preparing or generating a new plan never cancels old tasks. A new revision
takes effect only after validation and commit; every failure mode leaves the
active plan and its task statuses untouched. Passed tasks reach the planner
as facts; nothing in a new revision is auto-passed. Every revision must cover
the Goal acceptance criteria verbatim. No network, no paid model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orx import dispatch
from orx.records import ORXError, PlanValidationError, ReplanRejectedError

from conftest import (
    HOST_CONFIG_TOML,
    HOST_PROFILES_TOML,
    active_task,
    ir_for,
    make_project,
    replan_task_entry,
    superseded_entry,
    task_spec,
    with_replan,
    write_evidence,
)


@pytest.fixture
def planned(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance[:1], verification=["true"]),
        task_spec("T002", deps=["T001"], acceptance=goal.acceptance[1:]),
    ]))
    return project


def _state_vector(project):
    """The full replaceable state: revision rows + every task status."""
    rows = project.store.conn.execute(
        "SELECT r.run_id, r.revision, r.status,"
        " (SELECT GROUP_CONCAT(t.task_id || '=' || t.status)"
        "    FROM tasks t WHERE t.revision_id = r.id ORDER BY t.id) AS tasks"
        " FROM plan_revisions r ORDER BY r.revision"
    ).fetchall()
    return [tuple(row) for row in rows]


# ---------------------------------------------------------------------------
# 1. planner failure (the launch itself fails or emits garbage)


FAILING_PLANNER_PROFILES = """

[profiles.cli-planner-fail]
driver = "cli"
harness = "shell"
executable = "/bin/sh"
args = ["-c", "cat > /dev/null; echo 'I explored and decided not to plan today' >&2; exit 1"]
prompt_transport = "stdin"
model = "fake-model"
class = "economy"
effort = "low"
capabilities = ["coding"]
"""


def test_planner_failure_preserves_active_plan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-planner"]', 'profiles = ["cli-planner-fail"]'
    ).replace(
        'profiles = ["host-frontier", "host-planner"]', 'profiles = ["cli-planner-fail"]'
    )
    project = make_project(tmp_path, config_toml=config,
                           profiles_toml=HOST_PROFILES_TOML + FAILING_PLANNER_PROFILES)
    try:
        goal = dispatch.create_goal(
            project, objective="Ship the login fix",
            acceptance=["marker file exists", "summary is written"], constraints=[],
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=goal.acceptance[:1]),
            task_spec("T002", deps=["T001"], acceptance=goal.acceptance[1:]),
        ]))
        dispatch.run_slice(project)  # T001 waiting_host
        before = _state_vector(project)

        with pytest.raises(ORXError):
            dispatch.plan_route(project)
        assert _state_vector(project) == before
    finally:
        project.close()


# ---------------------------------------------------------------------------
# 2. invalid plans are rejected; the old plan survives byte-for-byte


def test_invalid_plan_rejected_state_untouched(planned, goal):
    dispatch.run_slice(planned)  # park T001 so statuses are non-trivial
    before = _state_vector(planned)
    assignments_before = [a.prompt for a in planned.store.assignments_all()]

    # dependency names a missing task
    bad_deps = ir_for(goal, [
        task_spec("T101", deps=["T999"], acceptance=goal.acceptance),
    ])
    with pytest.raises(PlanValidationError) as excinfo:
        dispatch.submit_plan(planned, bad_deps)
    assert any("T999" in e for e in excinfo.value.errors)
    assert _state_vector(planned) == before
    assert [a.prompt for a in planned.store.assignments_all()] == assignments_before


def test_new_revision_must_still_cover_goal_acceptance_verbatim(planned, goal):
    before = _state_vector(planned)
    missing = ir_for(goal, [
        # paraphrases the second criterion — rejected, old plan survives
        task_spec("T101", acceptance=["marker file exists", "a summary got written"]),
    ])
    with pytest.raises(PlanValidationError) as excinfo:
        dispatch.submit_plan(planned, missing)
    assert any("not present verbatim" in e for e in excinfo.value.errors)
    assert _state_vector(planned) == before


# ---------------------------------------------------------------------------
# 3. busy tasks reject the replan at every entry point


def test_busy_tasks_reject_replan_including_with_context_file(planned, goal, tmp_path):
    dispatch.run_slice(planned)
    dispatch.task_claim(planned, "T001")  # running
    intent = tmp_path / "intent.md"
    intent.write_text("reason: dependency graph is wrong\nintent: re-shape phase 2\n")

    with pytest.raises(ReplanRejectedError):
        dispatch.plan_route(planned)
    with pytest.raises(ReplanRejectedError):
        dispatch.plan_route(planned, context_file=str(intent))
    with pytest.raises(ReplanRejectedError):
        dispatch.submit_plan(planned, ir_for(goal, [
            task_spec("T101", acceptance=goal.acceptance),
        ]))
    assert active_task(planned, "T001").status == "running"


def test_verifying_task_rejects_replan(planned, goal, tmp_path):
    # G004 (out-of-scope mechanical fixture fix): the replacement declares
    # its correspondence explicitly.
    dispatch.submit_plan(planned, with_replan(ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["agent: look at it"]),
    ]), 1, [
        replan_task_entry("T001", "continue", sources=[(1, "T001")]),
    ], [
        superseded_entry(1, "T001", "continued", successors=["T001"]),
        superseded_entry(1, "T002", "dropped", note="folded into the new T001"),
    ]))
    dispatch.run_slice(planned)
    dispatch.task_claim(planned, "T001")
    dispatch.task_complete(planned, "T001", str(write_evidence(tmp_path)))
    assert active_task(planned, "T001").status == "verifying"
    with pytest.raises(ReplanRejectedError):
        dispatch.plan_route(planned)


# ---------------------------------------------------------------------------
# 4. successful replacement: exact commit semantics


def test_preparing_new_plan_does_not_cancel_old_tasks(planned, goal):
    dispatch.run_slice(planned)  # T001 -> waiting_host
    result = dispatch.plan_route(planned)  # planning assignment appears
    assert result["mode"] == "host_required"
    statuses = {t["id"]: t["status"] for t in dispatch.task_list(planned)}
    assert statuses == {"T001": "waiting_host", "T002": "pending"}


def test_successful_replacement_commit_semantics(planned, goal, tmp_path):
    # T001 passes (becomes a fact); T002 stays unfinished (gets cancelled).
    dispatch.run_slice(planned)
    dispatch.task_claim(planned, "T001")
    dispatch.task_complete(planned, "T001", str(write_evidence(tmp_path)))
    dispatch.run_slice(planned)  # T002 -> waiting_host

    # The Controller routes the replan; the waiting planner assignment carries
    # the passed task as an execution fact.
    routed = dispatch.plan_route(planned)
    prompt = routed["assignment"]["prompt"]
    assert "T001" in prompt and "PASSED" in prompt
    assert "never auto-passes" in prompt

    result = dispatch.submit_plan(planned, with_replan(ir_for(goal, [
        task_spec("T101", acceptance=goal.acceptance[:1], verification=["true"]),
        task_spec("T102", deps=["T101"], acceptance=goal.acceptance[1:],
                  verification=["true"]),
    ]), 1, [
        replan_task_entry("T101", "confirm", sources=[(1, "T001")],
                          confirm_verification=["true"]),
        replan_task_entry("T102", "redo", sources=[(1, "T002")],
                          redo_reason="the unfinished summary work is planned anew"),
    ], [
        superseded_entry(1, "T001", "confirmed", successors=["T101"]),
        superseded_entry(1, "T002", "redone", successors=["T102"]),
    ]))
    assert result["revision"] == 2
    assert result["superseded_revision"] == 1
    assert result["cancelled_tasks"] == ["T002"]

    # old revision: passed fact survives, unfinished work cancelled
    old = project_revision_tasks(planned, 1)
    assert old == {"T001": "passed", "T002": "cancelled"}
    # new revision: nothing auto-passed — only pending/runnable are legal inserts
    new = project_revision_tasks(planned, 2)
    assert set(new.values()) <= {"pending", "runnable"}
    assert new == {"T101": "runnable", "T102": "pending"}


def test_replacement_then_completion_lands_done(planned, goal, tmp_path):
    dispatch.run_slice(planned)
    dispatch.task_claim(planned, "T001")
    dispatch.task_complete(planned, "T001", str(write_evidence(tmp_path)))
    dispatch.run_slice(planned)
    dispatch.submit_plan(planned, with_replan(ir_for(goal, [
        task_spec("T101", acceptance=goal.acceptance[:1], verification=["true"]),
        task_spec("T102", deps=["T101"], acceptance=goal.acceptance[1:],
                  verification=["true"]),
    ]), 1, [
        replan_task_entry("T101", "confirm", sources=[(1, "T001")],
                          confirm_verification=["true"]),
        replan_task_entry("T102", "redo", sources=[(1, "T002")],
                          redo_reason="the unfinished summary work is planned anew"),
    ], [
        superseded_entry(1, "T001", "confirmed", successors=["T101"]),
        superseded_entry(1, "T002", "redone", successors=["T102"]),
    ]))
    dispatch.run_slice(planned)
    dispatch.task_claim(planned, "T101")
    dispatch.task_complete(planned, "T101", str(write_evidence(tmp_path, "e1.json")))
    dispatch.run_slice(planned)
    dispatch.task_claim(planned, "T102")
    dispatch.task_complete(planned, "T102", str(write_evidence(tmp_path, "e2.json")))
    data = dispatch.status_data(planned)
    assert data["run"]["status"] == "done"
    assert data["goal"]["status"] == "done"


def project_revision_tasks(project, revision_number):
    rows = project.store.conn.execute(
        "SELECT t.task_id, t.status FROM tasks t JOIN plan_revisions r ON t.revision_id = r.id"
        " WHERE r.revision = ? ORDER BY t.id",
        (revision_number,),
    ).fetchall()
    return {row["task_id"]: row["status"] for row in rows}
