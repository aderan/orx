"""Plan revision semantics: supersession, replan guard, one active graph."""

from __future__ import annotations

import pytest

from orx import dispatch
from orx.records import NotFoundError, ReplanRejectedError

from conftest import ir_for, task_spec, write_evidence


@pytest.fixture
def planned(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance[:1], verification=["true"]),
        task_spec("T002", deps=["T001"], acceptance=goal.acceptance[1:]),
    ]))
    return project


def test_new_revision_supersedes_and_cancels_unfinished(planned, goal, tmp_path):
    project = planned
    # pass T001 so we can verify terminal states survive supersession
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))

    result = dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T101", acceptance=goal.acceptance),
    ]))
    assert result["revision"] == 2
    assert result["superseded_revision"] == 1
    assert result["cancelled_tasks"] == ["T002"]  # pending -> cancelled

    statuses = {t["id"]: t["status"] for t in dispatch.task_list(project)}
    assert set(statuses) == {"T101"}  # only the active revision is listed
    assert statuses["T101"] == "runnable"

    # T001 stays passed (terminal states are not rewritten), T002 cancelled.
    goal_row = project.store.goal_active()
    run = project.store.run_for_goal(goal_row.id)
    old = [r for r in [project.store.revision_active(run.id)]]
    superseded = project.store.conn.execute(
        "SELECT * FROM plan_revisions WHERE run_id = ? ORDER BY revision", (run.id,)
    ).fetchall()
    assert [s["status"] for s in superseded] == ["superseded", "active"]
    old_tasks = {
        r["task_id"]: r["status"]
        for r in project.store.conn.execute(
            "SELECT task_id, status FROM tasks WHERE revision_id = ?", (superseded[0]["id"],)
        )
    }
    assert old_tasks == {"T001": "passed", "T002": "cancelled"}


def test_replan_rejected_while_running(planned):
    project = planned
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    with pytest.raises(ReplanRejectedError) as excinfo:
        dispatch.plan_route(project)
    assert "T001" in str(excinfo.value)
    with pytest.raises(ReplanRejectedError):
        dispatch.submit_plan(project, ir_for(project.store.goal_active(), [
            task_spec("T101", acceptance=["marker file exists"]),
        ]))


def test_replan_rejected_while_verifying(planned, goal):
    project = planned
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["agent: looks fine"]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(project.root)))
    with pytest.raises(ReplanRejectedError):
        dispatch.plan_route(project)


def test_replan_allowed_when_only_waiting(planned):
    project = planned
    dispatch.run_slice(project)  # T001 -> waiting_host (not running)
    result = dispatch.plan_route(project)  # must not raise
    assert result["mode"] == "host_required"


def test_old_revision_tasks_do_not_participate(planned, goal, tmp_path):
    project = planned
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T201", acceptance=goal.acceptance[:1], verification=["true"]),
        task_spec("T202", acceptance=goal.acceptance[1:], verification=["true"]),
    ]))
    # claim/complete the NEW tasks only; old ids are gone from the active set
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T201")
    dispatch.task_complete(project, "T201", str(write_evidence(tmp_path)))
    dispatch.task_claim(project, "T202")
    dispatch.task_complete(project, "T202", str(write_evidence(tmp_path)))

    data = dispatch.status_data(project)
    assert data["run"]["status"] == "done"
    assert data["goal"]["status"] == "done"

    # old ids no longer resolve against the active revision
    with pytest.raises(NotFoundError):
        dispatch.task_claim(project, "T001")


def test_assignment_marked_submitted(planned, goal):
    project = planned
    result = dispatch.plan_route(project)
    assignment_id = result["assignment"]["id"]
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ]))
    assignment = planned.store.assignment_get(assignment_id)
    assert assignment.status == "submitted"
    assert assignment.submitted_at is not None


def test_only_one_active_revision_per_run(planned):
    goal_row = planned.store.goal_active()
    run = planned.store.run_for_goal(goal_row.id)
    rows = planned.store.conn.execute(
        "SELECT revision, status FROM plan_revisions WHERE run_id = ? AND status = 'active'",
        (run.id,),
    ).fetchall()
    assert len(rows) == 1


def test_replan_after_done_reopens_goal(planned, goal, tmp_path):
    project = planned
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T002")
    dispatch.task_complete(project, "T002", str(write_evidence(tmp_path)))
    assert dispatch.status_data(project)["run"]["status"] == "done"

    result = dispatch.plan_route(project)  # replan a finished run
    assert result["mode"] == "host_required"
    assert dispatch.status_data(project)["run"]["status"] == "planning"
