"""The task state machine.

Every task status change goes through :func:`transition` (or :func:`record_insert`
for the initial state). Nothing else in the codebase assigns task statuses.
Each transition is persisted as a `task_events` row so the path a task took is
always reconstructable.

This module also owns status *recomputation* after any transition: dependency
readiness (pending -> runnable/blocked) and the Run/Goal aggregate states.
`dispatch` calls :func:`refresh` after every mutation; it never recomputes
statuses itself.
"""

from __future__ import annotations

from orx import records
from orx.records import (
    ACTIVE_EXECUTION_STATUSES,
    ConflictError,
    GoalStatus,
    RunStatus,
    TaskStatus,
    TransitionError,
)
from orx.state import Goal, Run, Store

# from -> {to}
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.RUNNABLE, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
    ),
    TaskStatus.RUNNABLE: frozenset(
        {TaskStatus.RUNNING, TaskStatus.WAITING_HOST, TaskStatus.WAITING_EXTERNAL,
         TaskStatus.CANCELLED}
    ),
    TaskStatus.RUNNING: frozenset({TaskStatus.VERIFYING, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.WAITING_HOST: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.WAITING_EXTERNAL: frozenset(
        {TaskStatus.VERIFYING, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.VERIFYING: frozenset({TaskStatus.PASSED, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.PASSED: frozenset(),
    TaskStatus.FAILED: frozenset({TaskStatus.RUNNABLE}),
    TaskStatus.BLOCKED: frozenset({TaskStatus.RUNNABLE, TaskStatus.CANCELLED}),
    TaskStatus.CANCELLED: frozenset(),
}

# Named events appear only in the audit log; legality is decided by from/to pairs.


def is_legal(from_status: TaskStatus, to_status: TaskStatus) -> bool:
    return to_status in ALLOWED_TRANSITIONS[from_status]


def record_insert(
    store: Store,
    revision_row_id: int,
    task_id: str,
    status: TaskStatus,
    reason: str | None = None,
) -> None:
    """Record the initial state of a freshly inserted task."""
    if status not in (TaskStatus.PENDING, TaskStatus.RUNNABLE):
        raise TransitionError(
            f"task {task_id}: initial status must be pending or runnable, got {status.value}"
        )
    event = "insert_ready" if status is TaskStatus.RUNNABLE else "insert_pending"
    store.task_event_add(revision_row_id, task_id, None, status.value, event, reason)


def transition(
    store: Store,
    revision_row_id: int,
    task_id: str,
    event: str,
    to_status: TaskStatus,
    reason: str | None = None,
    failure_reason: str | None = None,
) -> None:
    """Validate and persist one status transition inside a single transaction."""
    with store.tx():
        row = store.task_get(revision_row_id, task_id)
        from_status = TaskStatus(row.status)
        if not is_legal(from_status, to_status):
            raise TransitionError(
                f"task {task_id}: illegal transition {from_status.value} -> {to_status.value}"
                f" (event {event})"
            )
        store.task_update_status(
            revision_row_id,
            task_id,
            to_status,
            failure_reason=failure_reason,
            clear_failure=(to_status is TaskStatus.RUNNABLE),
        )
        store.task_event_add(
            revision_row_id, task_id, from_status.value, to_status.value, event, reason
        )


def claim(store: Store, revision_row_id: int, task_id: str) -> None:
    """Host claim: exactly one caller may move waiting_host -> running."""
    with store.tx():
        row = store.task_get(revision_row_id, task_id)
        if row.status != TaskStatus.WAITING_HOST.value:
            raise ConflictError(
                f"task {task_id} is {row.status}, not waiting_host; nothing to claim"
            )
        store.task_update_status(revision_row_id, task_id, TaskStatus.RUNNING)
        store.task_event_add(
            revision_row_id, task_id, TaskStatus.WAITING_HOST.value,
            TaskStatus.RUNNING.value, "claim", "host claim",
        )


# ---------------------------------------------------------------------------
# Recomputation (owned here so statuses have exactly one authority)


def refresh_readiness(store: Store, run: Run) -> None:
    """pending -> runnable (all deps passed) or blocked (a dep failed/cancelled)."""
    revision = store.revision_active(run.id)
    if revision is None:
        return
    statuses = {t.task_id: TaskStatus(t.status) for t in store.tasks_all(revision.id)}
    for task_id, status in statuses.items():
        if status not in (TaskStatus.PENDING, TaskStatus.BLOCKED):
            continue
        deps = store.deps_for(revision.id, task_id)
        if not deps:
            continue
        dep_statuses = [statuses.get(d) for d in deps]
        if any(s in (TaskStatus.FAILED, TaskStatus.CANCELLED) for s in dep_statuses):
            if status is TaskStatus.PENDING:
                transition(
                    store, revision.id, task_id, "dep_failed", TaskStatus.BLOCKED,
                    reason="a dependency failed or was cancelled",
                )
        elif all(s is TaskStatus.PASSED for s in dep_statuses):
            transition(
                store, revision.id, task_id, "deps_satisfied", TaskStatus.RUNNABLE,
                reason="all dependencies passed",
            )


def refresh_run(store: Store, goal: Goal, run: Run) -> None:
    """Done is an invariant, not a judgment call: a Run is done only when every
    active-revision task is passed. Cancelled tasks exist only in superseded
    revisions, so the active revision never contains them."""
    revision = store.revision_active(run.id)
    if revision is None:
        if run.status != RunStatus.PLANNING.value:
            store.run_set_status(run.id, RunStatus.PLANNING)
        return
    statuses = {TaskStatus(t.status) for t in store.tasks_all(revision.id)}
    if statuses and statuses <= {TaskStatus.PASSED}:
        store.run_set_status(run.id, RunStatus.DONE)
        if goal.status == GoalStatus.ACTIVE.value:
            store.goal_set_status(goal.id, GoalStatus.DONE)
    elif statuses & ACTIVE_EXECUTION_STATUSES:
        store.run_set_status(run.id, RunStatus.RUNNING)
    else:
        store.run_set_status(run.id, RunStatus.BLOCKED)


def refresh(store: Store, goal: Goal, run: Run) -> None:
    refresh_readiness(store, run)
    refresh_run(store, goal, run)
