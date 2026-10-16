"""Task state machine: legal/illegal transitions, readiness, retry, audit log."""

from __future__ import annotations

import pytest

from orx import dispatch, machine
from orx.records import ConflictError, TaskStatus, TransitionError
from orx.state import Store

from conftest import ir_for, task_spec, write_evidence


@pytest.fixture
def seeded(project, goal):
    """A submitted plan with T001 (no deps) and T002 (depends on T001)."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance[:1], verification=["true"]),
        task_spec("T002", deps=["T001"], acceptance=goal.acceptance[1:]),
    ]))
    return project


def _rev(project):
    goal = project.store.goal_active()
    run = project.store.run_for_goal(goal.id)
    return project.store.revision_active(run.id).id


def _set_status(project, task_id, status: TaskStatus):
    # Tests seed states directly through the storage primitive; production
    # code only ever goes through machine.transition.
    project.store.task_update_status(_rev(project), task_id, status)


LEGAL_PAIRS = [
    ("pending", "runnable"), ("pending", "blocked"), ("pending", "cancelled"),
    ("runnable", "running"), ("runnable", "waiting_host"), ("runnable", "waiting_external"),
    ("runnable", "cancelled"),
    ("running", "verifying"), ("running", "failed"), ("running", "cancelled"),
    ("waiting_host", "running"), ("waiting_host", "cancelled"),
    ("waiting_external", "verifying"), ("waiting_external", "failed"),
    ("waiting_external", "cancelled"),
    ("verifying", "passed"), ("verifying", "failed"), ("verifying", "cancelled"),
    ("failed", "runnable"),
    ("blocked", "runnable"), ("blocked", "cancelled"),
]

ILLEGAL_PAIRS = [
    ("pending", "running"), ("pending", "passed"),
    ("runnable", "verifying"), ("runnable", "passed"),
    ("running", "passed"), ("running", "runnable"),
    ("waiting_host", "verifying"), ("waiting_host", "failed"),
    ("verifying", "running"), ("verifying", "runnable"),
    ("failed", "pending"), ("failed", "running"),
    ("blocked", "running"),
    ("passed", "runnable"), ("passed", "failed"), ("passed", "cancelled"),
    ("cancelled", "runnable"), ("cancelled", "pending"),
]


@pytest.mark.parametrize("frm,to", LEGAL_PAIRS)
def test_legal_transitions_allowed(seeded, frm, to):
    project = seeded
    _set_status(project, "T001", TaskStatus(frm))
    machine.transition(project.store, _rev(project), "T001", "test", TaskStatus(to))


@pytest.mark.parametrize("frm,to", ILLEGAL_PAIRS)
def test_illegal_transitions_rejected(seeded, frm, to):
    project = seeded
    _set_status(project, "T001", TaskStatus(frm))
    with pytest.raises(TransitionError) as excinfo:
        machine.transition(project.store, _rev(project), "T001", "test", TaskStatus(to))
    assert frm in str(excinfo.value) and to in str(excinfo.value)


def test_transition_leaves_status_unchanged_on_rejection(seeded):
    project = seeded
    _set_status(project, "T001", TaskStatus.RUNNING)
    with pytest.raises(TransitionError):
        machine.transition(project.store, _rev(project), "T001", "test", TaskStatus.PASSED)
    assert project.store.task_get(_rev(project), "T001").status == "running"


def test_insert_states_depend_on_deps(seeded):
    project = seeded
    assert project.store.task_get(_rev(project), "T001").status == "runnable"
    assert project.store.task_get(_rev(project), "T002").status == "pending"


def test_dependency_unlock_on_pass(seeded, tmp_path):
    project = seeded
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    assert project.store.task_get(_rev(project), "T001").status == "passed"
    assert project.store.task_get(_rev(project), "T002").status == "runnable"


def test_dependency_blocks_on_failure(seeded, tmp_path):
    project = seeded
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_fail(project, "T001", "host gave up")
    assert project.store.task_get(_rev(project), "T001").status == "failed"
    assert project.store.task_get(_rev(project), "T002").status == "blocked"


def test_blocked_task_unblocks_when_dependency_retried_to_pass(seeded, tmp_path):
    project = seeded
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_fail(project, "T001", "transient")
    assert project.store.task_get(_rev(project), "T002").status == "blocked"

    dispatch.task_retry(project, "T001")
    assert project.store.task_get(_rev(project), "T001").status == "runnable"
    # dependency is runnable (not passed): T002 stays blocked
    assert project.store.task_get(_rev(project), "T002").status == "blocked"

    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    assert project.store.task_get(_rev(project), "T002").status == "runnable"


def test_retry_moves_failed_back_to_runnable(seeded, tmp_path):
    project = seeded
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_fail(project, "T001", "boom")
    result = dispatch.task_retry(project, "T001")
    assert result["status"] == "runnable"
    assert project.store.task_get(_rev(project), "T001").failure_reason is None


def test_retry_requires_failed(seeded):
    with pytest.raises(ConflictError):
        dispatch.task_retry(seeded, "T001")  # runnable, not failed


def test_complete_requires_running_or_waiting_external(seeded):
    dispatch.run_slice(seeded)
    with pytest.raises(ConflictError):
        dispatch.task_complete(seeded, "T001", "/tmp/nope.json")  # waiting_host


def test_events_persist_transition_history(seeded):
    project = seeded
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(seeded.root)))

    events = project.store.task_events(_rev(project), "T001")
    chain = [(e.from_status, e.to_status, e.event) for e in events]
    assert chain == [
        (None, "runnable", "insert_ready"),
        ("runnable", "waiting_host", "route_host"),
        ("waiting_host", "running", "claim"),
        ("running", "verifying", "complete"),
        ("verifying", "passed", "verify_pass"),
    ]
    assert all(e.reason is None or isinstance(e.reason, str) for e in events)


# -- Goal lifecycle state machine (G010 T002) ---------------------------------


GOAL_LEGAL_PAIRS = [
    ("active", "paused"), ("active", "done"), ("active", "cancelled"),
    ("paused", "active"),
]

GOAL_ILLEGAL_PAIRS = [
    ("paused", "done"), ("paused", "cancelled"), ("paused", "paused"),
    ("done", "paused"), ("done", "active"),
    ("cancelled", "paused"), ("cancelled", "active"),
]


@pytest.mark.parametrize("frm,to", GOAL_LEGAL_PAIRS)
def test_goal_legal_transitions_allowed(frm, to):
    assert machine.goal_is_legal(
        machine.GoalStatus(frm), machine.GoalStatus(to)) is True


@pytest.mark.parametrize("frm,to", GOAL_ILLEGAL_PAIRS)
def test_goal_illegal_transitions_rejected(frm, to):
    assert machine.goal_is_legal(
        machine.GoalStatus(frm), machine.GoalStatus(to)) is False


def test_goal_pause_and_resume_transition_with_idempotent_repeats(project, goal):
    store = project.store
    first = machine.goal_pause(store, goal.id)
    assert first == {"goal": goal.id, "status": "paused", "changed": True}
    assert store.goal_get(goal.id).status == "paused"

    # Repeating the current state is a legal, explicit no-op.
    again = machine.goal_pause(store, goal.id)
    assert again == {"goal": goal.id, "status": "paused", "changed": False}
    assert store.goal_get(goal.id).status == "paused"

    resumed = machine.goal_resume(store, goal.id)
    assert resumed == {"goal": goal.id, "status": "active", "changed": True}
    repeated = machine.goal_resume(store, goal.id)
    assert repeated == {"goal": goal.id, "status": "active", "changed": False}
    assert store.goal_get(goal.id).status == "active"


def test_goal_pause_resume_refuse_terminal_statuses(project, goal):
    store = project.store
    store.goal_set_status(goal.id, machine.GoalStatus.DONE)
    with pytest.raises(TransitionError, match="cannot pause"):
        machine.goal_pause(store, goal.id)
    with pytest.raises(TransitionError, match="cannot resume"):
        machine.goal_resume(store, goal.id)
    assert store.goal_get(goal.id).status == "done"


def test_refresh_never_lifts_a_paused_goal(seeded):
    """The completion invariant fires only on an ACTIVE goal row: with every
    task passed while paused, the Run records done but the Goal stays
    paused; `goal resume` replays the same invariant and converges to done.
    A stale Goal object (read before the pause committed) cannot lift the
    pause either — legality is decided by the row, not the argument."""
    project = seeded
    store = project.store
    goal = store.goal_get(_goal_id(project))
    run = store.run_for_goal(goal.id)
    rev = _rev(project)

    # The stale snapshot: read while the row is still active.
    stale_active = store.goal_get(goal.id)
    machine.goal_pause(store, goal.id)
    assert stale_active.status == "active"  # the object predates the pause
    assert store.goal_get(goal.id).status == "paused"

    # Every task reaches passed while the Goal is paused (fact submissions
    # from executors that were already running).
    for task_id in ("T001", "T002"):
        store.task_update_status(rev, task_id, TaskStatus.RUNNING)
        store.task_update_status(rev, task_id, TaskStatus.VERIFYING)
        store.task_update_status(rev, task_id, TaskStatus.PASSED)

    # refresh with the stale active-looking object: the row decides.
    machine.refresh(store, stale_active, run)
    assert store.run_get(run.id).status == "done"
    assert store.goal_get(goal.id).status == "paused"  # never implicitly lifted

    machine.goal_resume(store, goal.id)
    machine.refresh(store, store.goal_get(goal.id), run)
    assert store.run_get(run.id).status == "done"
    assert store.goal_get(goal.id).status == "done"  # converged at resume


def _goal_id(project):
    goal = project.store.goal_active()
    if goal is None:
        row = project.store.conn.execute(
            "SELECT id FROM goals WHERE status != 'cancelled'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        goal = project.store.goal_get(row["id"])
    return goal.id
