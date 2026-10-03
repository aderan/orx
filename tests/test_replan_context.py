"""Replanning input contracts (M1.1 Task 2).

`replan --context-file` carries this round's reason, intent, and supporting
material. The planner prompt separates three things: the original Goal, a
deterministic execution-fact snapshot from ORX state, and the Controller's
intent for this round. The exact planner input is archived. Invalid context
files fail before anything is dispatched. Missing facts are marked unknown,
never guessed. The Goal text is never rewritten. No network, no paid model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orx import dispatch
from orx import plan as plan_mod
from orx.records import ORXError

from conftest import HOST_CONFIG_TOML, HOST_PROFILES_TOML, ir_for, make_project, task_spec, write_evidence, active_task


INTENT_TEXT = """\
Reason for this replan: phase 2 assumed the cache helper existed; it does not.

Intent for this round: keep phase 1 as-is (its work passed), re-plan only the
remaining summary work around the missing helper.

Supporting material: docs/cache-notes.md line 12 confirms the helper was
removed in the last refactor.
"""


@pytest.fixture
def intent_file(tmp_path) -> Path:
    path = tmp_path / "replan-intent.md"
    path.write_text(INTENT_TEXT)
    return path


@pytest.fixture
def mixed_run(project, goal, tmp_path):
    """Revision 1 with T001 passed (evidence + verification recorded) and
    T002 failed with a reason — the state a real replan starts from."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance[:1], verification=["true"]),
        task_spec("T002", deps=["T001"], acceptance=goal.acceptance[1:]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T002")
    dispatch.task_fail(project, "T002", "assumption broken: cache helper does not exist")
    return project


# ---------------------------------------------------------------------------
# context file validation happens before any dispatch


def test_missing_context_file_rejected_before_dispatch(mixed_run, tmp_path):
    attempts_before = len(mixed_run.store.attempts_all())
    assignments_before = len(mixed_run.store.assignments_all())
    with pytest.raises(ORXError) as excinfo:
        dispatch.plan_route(mixed_run, context_file=str(tmp_path / "nope.md"))
    assert "cannot read" in str(excinfo.value)
    # Nothing was routed, attempted, or assigned — the model is never called.
    assert len(mixed_run.store.attempts_all()) == attempts_before
    assert len(mixed_run.store.assignments_all()) == assignments_before


def test_empty_context_file_rejected(mixed_run, tmp_path):
    empty = tmp_path / "empty.md"
    empty.write_text("   \n")
    with pytest.raises(ORXError) as excinfo:
        dispatch.plan_route(mixed_run, context_file=str(empty))
    assert "empty" in str(excinfo.value)


def test_oversize_context_file_rejected(mixed_run, tmp_path):
    big = tmp_path / "big.md"
    big.write_text("x" * (64 * 1024 + 1))
    with pytest.raises(ORXError) as excinfo:
        dispatch.plan_route(mixed_run, context_file=str(big))
    assert "exceeds" in str(excinfo.value)


# ---------------------------------------------------------------------------
# the deterministic fact snapshot


def test_snapshot_records_statuses_failures_evidence_and_verifications(mixed_run, goal):
    store = mixed_run.store
    goal_row = store.goal_active() or store.goal_get(goal.id)
    run = store.run_for_goal(goal_row.id)
    snap = dispatch.replan_snapshot(store, goal_row, run)

    assert snap["active_revision"]["revision"] == 1
    assert snap["revisions"] == [{"revision": 1, "status": "active"}]
    by_id = {t["task_id"]: t for t in snap["tasks"]}
    assert by_id["T001"]["status"] == "passed"
    assert by_id["T001"]["verifications"][0]["command"] == "true"
    assert by_id["T001"]["verifications"][0]["passed"] is True
    assert by_id["T001"]["verifications"][0]["output_path"]
    assert by_id["T001"]["evidence"][0]["kind"] == "completion"
    assert by_id["T002"]["status"] == "failed"
    assert "cache helper does not exist" in by_id["T002"]["failure_reason"]


def test_snapshot_is_deterministic(mixed_run, goal):
    store = mixed_run.store
    goal_row = store.goal_active() or store.goal_get(goal.id)
    run = store.run_for_goal(goal_row.id)
    first = dispatch.replan_snapshot(store, goal_row, run)
    second = dispatch.replan_snapshot(store, goal_row, run)
    first.pop("generated_at"), second.pop("generated_at")
    assert first == second


def test_snapshot_marks_missing_information_unknown(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["true"]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_fail(project, "T001", "")  # failed with no recorded reason

    store = project.store
    run = store.run_for_goal(goal.id)
    snap = dispatch.replan_snapshot(store, goal, run)
    facts = plan_mod.render_replan_facts(snap)
    assert "(unknown — no failure reason recorded)" in facts
    assert "evidence: (none recorded)" in facts
    assert "verified: (no verification results recorded)" in facts


def test_snapshot_without_revisions_renders_the_empty_state(project, goal):
    store = project.store
    run = store.run_for_goal(goal.id)
    snap = dispatch.replan_snapshot(store, goal, run)
    facts = plan_mod.render_replan_facts(snap)
    assert "Active plan revision: none." in facts
    assert "(no tasks recorded" in facts


# ---------------------------------------------------------------------------
# the replan assignment separates goal / facts / intent


def test_replan_prompt_separates_goal_facts_and_intent(mixed_run, goal, intent_file):
    result = dispatch.plan_route(mixed_run, context_file=str(intent_file))
    assert result["mode"] == "host_required"
    assert result["replan"] is True
    assert result["context_file"] == str(intent_file)

    prompt = result["assignment"]["prompt"]
    # 1. the original Goal, verbatim and first
    assert goal.objective in prompt
    assert "This is a REPLAN" in prompt
    # 2. deterministic execution facts
    assert "Execution facts — deterministic snapshot of ORX state" in prompt
    assert "T001" in prompt and "PASSED" in prompt
    assert "assumption broken: cache helper does not exist" in prompt
    # 3. this round's intent, verbatim, marked as Controller-supplied
    assert "Controller's intent for THIS replan round" in prompt
    assert INTENT_TEXT.strip() in prompt
    # the boundary rule the M1 dogfood showed planners need
    assert "never auto-passes" in prompt
    # the archived file is exactly what the assignment row carries
    prompt_file = Path(result["assignment"]["prompt_file"])
    assert (mixed_run.root / prompt_file).read_text() == prompt


def test_context_file_never_rewrites_the_goal(mixed_run, goal, intent_file):
    before = project_store_goal(mixed_run)
    dispatch.plan_route(mixed_run, context_file=str(intent_file))
    dispatch.submit_plan(mixed_run, ir_for(goal, [
        task_spec("T101", acceptance=goal.acceptance, verification=["true"]),
    ]))
    assert project_store_goal(mixed_run) == before


def project_store_goal(project):
    row = project.store.conn.execute(
        "SELECT objective, constraints_json, acceptance_json, context FROM goals ORDER BY id"
    ).fetchall()
    return [tuple(r) for r in row]


def test_first_plan_intent_without_facts(project, goal, intent_file):
    result = dispatch.plan_route(project, context_file=str(intent_file))
    assert result["replan"] is False
    prompt = result["assignment"]["prompt"]
    assert "Controller's intent for THIS replan round" in prompt
    assert INTENT_TEXT.strip() in prompt
    assert "Execution facts" not in prompt
    assert "This is a REPLAN" not in prompt


def test_waiting_assignment_prompt_refreshed_with_new_facts_and_intent(mixed_run, goal, intent_file):
    first = dispatch.plan_route(mixed_run)  # creates the waiting assignment
    assignment_id = first["assignment"]["id"]
    assert "Controller's intent" not in first["assignment"]["prompt"]

    second = dispatch.plan_route(mixed_run, context_file=str(intent_file))
    assert second["assignment"]["id"] == assignment_id  # one assignment, refreshed
    refreshed = second["assignment"]["prompt"]
    assert "Controller's intent for THIS replan round" in refreshed
    assert refreshed == mixed_run.store.assignment_get(assignment_id).prompt
    prompt_file = Path(second["assignment"]["prompt_file"])
    assert (mixed_run.root / prompt_file).read_text() == refreshed


# ---------------------------------------------------------------------------
# the CLI planner's exact input is archived


CLI_PLANNER_PROFILES = """

[profiles.cli-planner]
driver = "cli"
harness = "shell"
executable = "/bin/sh"
args = ["-c", "cat > planner-prompt-captured.txt; cat plan.json"]
prompt_transport = "stdin"
model = "fake-model"
class = "economy"
effort = "low"
capabilities = ["coding"]
"""


def test_cli_planner_input_archived_and_contains_goal_facts_intent(
        tmp_path, monkeypatch, intent_file):
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-planner"]', 'profiles = ["cli-planner"]'
    ).replace(
        'profiles = ["host-frontier", "host-planner"]', 'profiles = ["cli-planner"]'
    )
    project = make_project(tmp_path, config_toml=config,
                           profiles_toml=HOST_PROFILES_TOML + CLI_PLANNER_PROFILES)
    try:
        goal = dispatch.create_goal(
            project,
            objective="Ship the login fix",
            acceptance=["marker file exists", "summary is written"],
            constraints=[],
            context="",
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=goal.acceptance[:1], verification=["true"]),
            task_spec("T002", deps=["T001"], acceptance=goal.acceptance[1:]),
        ]))
        dispatch.run_slice(project)
        dispatch.task_claim(project, "T001")
        dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
        dispatch.run_slice(project)
        dispatch.task_claim(project, "T002")
        dispatch.task_fail(project, "T002", "assumption broken: cache helper does not exist")

        # The stub planner echoes plan.json after capturing its stdin prompt.
        plan = {
            "goal": goal.id,
            "exploration": {"summary": "", "relevant_components": ["src/"],
                            "unknowns": [], "assumptions": [], "risks": []},
            "approach": {"summary": "", "decisions": []},
            "tasks": [
                task_spec("T101", acceptance=goal.acceptance[:1], verification=["true"]),
                task_spec("T102", deps=["T101"], acceptance=goal.acceptance[1:],
                          verification=["true"]),
            ],
        }
        (tmp_path / "plan.json").write_text(json.dumps(plan))

        result = dispatch.plan_route(project, context_file=str(intent_file))
        assert result["mode"] == "completed"
        assert result["replan"] is True
        assert result["revision"] == 2

        # The archived input is byte-identical to what the process received.
        archived = (tmp_path / result["prompt_file"]).read_text()
        captured = (tmp_path / "planner-prompt-captured.txt").read_text()
        assert archived == captured
        for marker in ("This is a REPLAN", "Execution facts — deterministic snapshot",
                       "T001", "PASSED", "assumption broken: cache helper does not exist",
                       "Controller's intent for THIS replan round"):
            assert marker in archived
    finally:
        project.close()
