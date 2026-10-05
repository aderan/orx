"""`orx task check`: same-session command self-check for workers.

The check runner executes the command entries of a task immediately,
appends verification rows bound to the current worker attempt, reports a
structured per-entry result, and never touches the task state machine.
"""

from __future__ import annotations

import pytest

from orx import dispatch, verify
from orx.records import NotFoundError

from conftest import (
    HOST_CONFIG_TOML,
    active_task,
    ir_for,
    make_project,
    task_spec,
    write_evidence,
)


@pytest.fixture
def planned(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance[:1],
                  verification=["test -f t1.marker", "true"]),
        task_spec("T002", acceptance=goal.acceptance[1:], verification=[]),
        task_spec("T003", acceptance=goal.acceptance[1:],
                  verification=["agent: result reads well"]),
    ]))
    dispatch.run_slice(project)
    return project


def _replan(project, goal, tasks):
    """Replace the active revision with a fresh plan (the supersede path
    test_verify.py also uses), park the tasks, and return the new revision."""
    dispatch.submit_plan(project, ir_for(goal, tasks))
    dispatch.run_slice(project)
    run = project.store.run_for_goal(goal.id)
    return project.store.revision_active(run.id)


def _revision(project):
    goal = project.store.goal_active()
    run = project.store.run_for_goal(goal.id)
    return project.store.revision_active(run.id)


def test_check_runs_command_entries_and_reports_structure(planned):
    dispatch.task_claim(planned, "T001")
    (planned.root / "t1.marker").write_text("ok")

    report = dispatch.task_check(planned, "T001")

    assert report["task"] == "T001"
    assert report["status"] == "running"
    assert report["summary"] == {"total": 2, "passed": 2, "failed": 0, "denied": 0}
    first, second = report["results"]
    assert first["command"] == "test -f t1.marker"
    assert first["exit_code"] == 0
    assert first["passed"] is True
    assert first["denied"] is False
    assert first["error_summary"] is None
    assert first["log_path"].startswith(".orx/runs/R001/check/T001/")
    assert (planned.root / first["log_path"]).exists()
    assert second["command"] == "true"
    assert second["passed"] is True

    # Rows recorded on the verifications table, bound to the worker attempt
    # the claimed task runs under.
    revision = _revision(planned)
    attempt = planned.store.attempt_latest_for_task(revision.id, "T001")
    assert attempt.role == "worker"
    assert report["attempt"] == attempt.id
    rows = planned.store.verifications_for(revision.id, "T001")
    assert len(rows) == 2
    assert {r.kind for r in rows} == {"command"}
    assert {r.attempt_id for r in rows} == {attempt.id}
    assert active_task(planned, "T001").status == "running"


def test_check_failure_reports_earliest_failure_line(planned, goal):
    revision = _replan(planned, goal, [
        task_spec("T005", acceptance=goal.acceptance, verification=[
            "echo before; echo 'FAILED tests/test_x.py::test_y assert 1 == 2'; exit 3"
        ]),
    ])
    dispatch.task_claim(planned, "T005")

    report = dispatch.task_check(planned, "T005")

    result = report["results"][0]
    assert result["passed"] is False
    assert result["exit_code"] == 3
    assert result["error_summary"] == "FAILED tests/test_x.py::test_y assert 1 == 2"
    assert report["summary"]["failed"] == 1
    log_text = (planned.root / result["log_path"]).read_text()
    assert "FAILED tests/test_x.py::test_y assert 1 == 2" in log_text
    assert active_task(planned, "T005").status == "running"
    assert len(planned.store.verifications_for(revision.id, "T005")) == 1


def test_check_denied_entry_matches_verify_semantics(planned, goal, tmp_path):
    revision = _replan(planned, goal, [
        task_spec("T900", acceptance=["marker file exists"],
                  verification=["sudo rm /tmp/x"]),
        task_spec("T901", acceptance=["summary is written"],
                  verification=["sudo rm /tmp/y"]),
    ])
    dispatch.task_claim(planned, "T900")

    report = dispatch.task_check(planned, "T900")

    result = report["results"][0]
    assert result["command"] == "sudo rm /tmp/x"
    assert result["denied"] is True
    assert result["denial_reason"] == "sudo"
    assert result["exit_code"] is None
    assert result["passed"] is False
    assert result["error_summary"] == "denied by verification denylist: sudo"
    assert (planned.root / result["log_path"]).read_text().startswith(
        "denied by verification denylist: sudo"
    )
    assert report["summary"] == {"total": 1, "passed": 0, "failed": 0, "denied": 1}
    assert active_task(planned, "T900").status == "running"

    # Same recorded shape as a denylist hit in run_command_verifications: kind
    # command, exit_code NULL, passed false, a denial log written, and the
    # command never executed. (run_command_verifications is called directly:
    # a completion would only be refused — the gate applies the same
    # denylist — and the comparison here is about the recorded row shape.)
    check_row = planned.store.verifications_for(revision.id, "T900")[0]
    dispatch.task_claim(planned, "T901")
    goal = planned.store.goal_active()
    run = planned.store.run_for_goal(goal.id)
    task901 = planned.store.task_get(revision.id, "T901")
    verify.run_command_verifications(
        planned.store, planned.root, run.id, revision.id, task901, timeout=30,
    )
    verify_row = [
        r for r in planned.store.verifications_for(revision.id, "T901")
        if r.kind == "command"
    ][0]
    assert (check_row.kind, check_row.exit_code, check_row.passed) == (
        verify_row.kind, verify_row.exit_code, verify_row.passed
    )
    assert (planned.root / verify_row.output_path).read_text().startswith(
        "denied by verification denylist"
    )


def test_check_fix_loop_not_poisoned_at_complete(planned, tmp_path):
    """check -> fail -> fix -> check -> complete must end passed: the stale
    failed row is superseded by the fresh one, exactly what a same-session
    check/fix loop needs."""
    dispatch.task_claim(planned, "T001")

    first = dispatch.task_check(planned, "T001")
    assert first["summary"]["passed"] == 1  # only `true` passes; marker missing
    assert first["summary"]["failed"] == 1

    (planned.root / "t1.marker").write_text("ok")
    second = dispatch.task_check(planned, "T001")
    assert second["summary"] == {"total": 2, "passed": 2, "failed": 0, "denied": 0}

    revision = _revision(planned)
    rows = planned.store.verifications_for(revision.id, "T001")
    assert len(rows) == 4  # two appended rows per entry, in check order
    assert verify.evaluate(
        planned.store, revision.id, active_task(planned, "T001")
    ) == "passed"
    assert verify.first_failure(
        planned.store, revision.id, active_task(planned, "T001")
    ) is None

    result = dispatch.task_complete(planned, "T001", str(write_evidence(tmp_path)))
    assert result["verdict"] == "passed"
    assert result["status"] == "passed"


def test_check_latest_failed_row_still_fails_evaluation(planned, goal, tmp_path):
    """Latest-row semantics cut both ways: a fresh failure after an older
    pass still fails the task, and names the failing command."""
    revision = _replan(planned, goal, [
        task_spec("T004", acceptance=goal.acceptance,
                  verification=["test -f gone.marker"]),
    ])
    dispatch.task_claim(planned, "T004")
    (planned.root / "gone.marker").write_text("ok")
    dispatch.task_check(planned, "T004")
    (planned.root / "gone.marker").unlink()
    report = dispatch.task_check(planned, "T004")
    assert report["summary"]["failed"] == 1

    task = active_task(planned, "T004")
    assert verify.evaluate(planned.store, revision.id, task) == "failed"
    assert verify.first_failure(planned.store, revision.id, task) == "test -f gone.marker"


def test_check_does_not_change_status_or_events(planned):
    dispatch.task_claim(planned, "T001")
    revision = _revision(planned)
    events_before = len(planned.store.task_events(revision.id, "T001"))
    attempt_before = planned.store.attempt_latest_for_task(revision.id, "T001")

    report = dispatch.task_check(planned, "T001")  # fails: no marker

    assert report["status"] == "running"
    assert active_task(planned, "T001").status == "running"
    assert len(planned.store.task_events(revision.id, "T001")) == events_before
    attempt_after = planned.store.attempt_latest_for_task(revision.id, "T001")
    assert attempt_after.id == attempt_before.id
    assert attempt_after.ended_at is None  # the worker attempt stays open


def test_check_unknown_task_is_a_clear_error(planned):
    with pytest.raises(NotFoundError, match="T999"):
        dispatch.task_check(planned, "T999")


def test_check_without_command_entries(planned):
    dispatch.task_claim(planned, "T002")

    report = dispatch.task_check(planned, "T002")

    assert report["results"] == []
    assert report["summary"] == {"total": 0, "passed": 0, "failed": 0, "denied": 0}
    assert "no executable command verification entries" in report["note"]
    assert report["status"] == "running"
    assert planned.store.verifications_for(_revision(planned).id, "T002") == []


def test_check_skips_agent_entries(planned):
    dispatch.task_claim(planned, "T003")

    report = dispatch.task_check(planned, "T003")

    assert report["results"] == []
    assert report["agent_entries_not_run"] == 1
    assert report["note"]
    assert planned.store.verifications_for(_revision(planned).id, "T003") == []


def test_check_timeout_semantics_match_verify(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        "command_timeout_sec = 30", "command_timeout_sec = 1"
    )
    project = make_project(tmp_path, config_toml=config)
    try:
        goal = dispatch.create_goal(
            project, objective="timeout check", acceptance=["a1"],
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["a1"], verification=["sleep 3"]),
        ]))
        dispatch.run_slice(project)
        dispatch.task_claim(project, "T001")

        report = dispatch.task_check(project, "T001")

        result = report["results"][0]
        assert result["timed_out"] is True
        assert result["exit_code"] is None
        assert result["passed"] is False
        assert report["summary"]["failed"] == 1
        log_text = (project.root / result["log_path"]).read_text()
        assert "[exit timeout" in log_text
    finally:
        project.close()
