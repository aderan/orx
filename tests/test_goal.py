"""Goal model: creation, single-active invariant, persistence."""

from __future__ import annotations

import pytest

from orx import dispatch
from orx.records import ORXError

from conftest import ir_for, task_spec, write_evidence


def test_goal_create_makes_goal_and_run(project):
    goal, run = dispatch.create_goal(
        project, "fix the bug", ["bug is fixed"], ["no new deps"], "context here"
    )
    assert goal.id == "G001"
    assert goal.status == "active"
    assert goal.acceptance == ["bug is fixed"]
    assert goal.constraints == ["no new deps"]
    assert run.id == "R001"
    assert run.status == "planning"
    assert run.goal_id == goal.id


def test_second_active_goal_refused(project):
    dispatch.create_goal(project, "first", ["a1"])
    with pytest.raises(ORXError) as excinfo:
        dispatch.create_goal(project, "second", ["a2"])
    assert "already active" in str(excinfo.value)


def test_goal_requires_objective_and_acceptance(project):
    with pytest.raises(ORXError):
        dispatch.create_goal(project, "  ", ["a"])
    with pytest.raises(ORXError):
        dispatch.create_goal(project, "objective", [])


def test_goal_persists_across_reopen(project, tmp_path):
    goal, run = dispatch.create_goal(project, "persist me", ["p1"])
    project.close()

    reopened = dispatch.open_project()
    try:
        shown = dispatch.goal_show(reopened)
        assert shown["goal"]["id"] == goal.id
        assert shown["goal"]["acceptance"] == ["p1"]
        assert shown["run"]["id"] == run.id
    finally:
        reopened.close()


def test_goal_done_at_run_completion(project, goal, tmp_path):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    assert project.store.goal_active() is None  # no longer active: it is done
    stored = project.store.goal_get(goal.id)
    assert stored.status == "done"
