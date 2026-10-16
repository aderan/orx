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
    GoalCancellingError,
    GoalPausedError,
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


# ---------------------------------------------------------------------------
# Goal lifecycle (G010 T002)
#
# The Goal state machine is a transition table like the task one, with one
# deliberate difference: pause/resume are IDEMPOTENT operations. Repeating
# the current state (pause on paused, resume on active) is a legal, explicit
# no-op with a clear result, because the operator cannot lose by asking
# twice — the state they asked for already holds. Every other pair follows
# the plain legality rule.

GOAL_ALLOWED_TRANSITIONS: dict[GoalStatus, frozenset[GoalStatus]] = {
    GoalStatus.ACTIVE: frozenset(
        {GoalStatus.PAUSED, GoalStatus.DONE, GoalStatus.CANCELLING}
    ),
    # G010 T003: cancel is legal from a paused Goal too — pausing stops new
    # work but never resurrects it; the operator may still decide to cancel.
    GoalStatus.PAUSED: frozenset({GoalStatus.ACTIVE, GoalStatus.CANCELLING}),
    # The persisted cancel REQUEST converges one way only: cancelling ->
    # cancelled, and only when no unconfirmed execution remains (see
    # dispatch._converge_cancelled). cancelling is NOT resumable — a cancel
    # cannot be un-asked, only concluded by real facts.
    GoalStatus.CANCELLING: frozenset({GoalStatus.CANCELLED}),
    GoalStatus.DONE: frozenset(),
    GoalStatus.CANCELLED: frozenset(),
}

# Terminal goal statuses: pause/resume refuse with a transition error, they
# never resurrect a finished or cancelled Goal.
GOAL_TERMINAL_STATUSES = frozenset({GoalStatus.DONE, GoalStatus.CANCELLED})


def goal_is_legal(from_status: GoalStatus, to_status: GoalStatus) -> bool:
    return to_status in GOAL_ALLOWED_TRANSITIONS[from_status]


def goal_pause(store: Store, goal_id: str) -> dict:
    """active -> paused. Idempotent: pausing an already paused Goal is a
    no-op that reports `changed: False`. A paused Goal blocks every new
    executor start (see dispatch's startup gate); running executors are
    untouched. Terminal statuses refuse."""
    with store.tx():
        status = store.goal_status(goal_id)
        current = GoalStatus(status)
        if current is GoalStatus.PAUSED:
            return {"goal": goal_id, "status": status, "changed": False}
        if not goal_is_legal(current, GoalStatus.PAUSED):
            raise TransitionError(
                f"goal {goal_id}: cannot pause from {status}"
                f" (only an active Goal can pause)"
            )
        store.goal_set_status(goal_id, GoalStatus.PAUSED)
        return {"goal": goal_id, "status": GoalStatus.PAUSED.value, "changed": True}


def goal_resume(store: Store, goal_id: str) -> dict:
    """paused -> active. Idempotent: resuming an already active Goal is a
    no-op that reports `changed: False`. Resume starts NOTHING itself — the
    caller re-reads in-flight ownership and the next `orx run` dispatches.
    Terminal statuses refuse; a cancelling Goal refuses too (it converges to
    cancelled, it cannot be resumed)."""
    with store.tx():
        status = store.goal_status(goal_id)
        current = GoalStatus(status)
        if current is GoalStatus.ACTIVE:
            return {"goal": goal_id, "status": status, "changed": False}
        if not goal_is_legal(current, GoalStatus.ACTIVE):
            raise TransitionError(
                f"goal {goal_id}: cannot resume from {status}"
                f" (only a paused Goal can resume"
                + ("; a cancelling Goal converges to cancelled, it cannot be resumed)"
                   if current is GoalStatus.CANCELLING else ")")
            )
        store.goal_set_status(goal_id, GoalStatus.ACTIVE)
        return {"goal": goal_id, "status": GoalStatus.ACTIVE.value, "changed": True}


def goal_cancel_request(store: Store, goal_id: str) -> dict:
    """active|paused -> cancelling — the persisted cancel REQUEST (G010 T003).

    Idempotent: cancelling an already cancelling (or already cancelled) Goal
    reports `changed: False` and re-runs nothing — the wrap-up itself is in
    dispatch.goal_cancel / _converge_cancelled, which are idempotent sweeps,
    so a repeated request never produces a second wrap-up. A done Goal
    refuses: completion that already converged is a legal wrap-up result and
    a cancel arriving late cannot replace it (exactly one legal wrap-up
    result)."""
    with store.tx():
        status = store.goal_status(goal_id)
        current = GoalStatus(status)
        if current in (GoalStatus.CANCELLING, GoalStatus.CANCELLED):
            return {"goal": goal_id, "status": status, "changed": False}
        if not goal_is_legal(current, GoalStatus.CANCELLING):
            raise TransitionError(
                f"goal {goal_id}: cannot cancel from {status}"
                f" (only an unfinished Goal — active or paused — can cancel;"
                " a done Goal keeps its completion)"
            )
        store.goal_set_status(goal_id, GoalStatus.CANCELLING)
        return {"goal": goal_id, "status": GoalStatus.CANCELLING.value, "changed": True}


def goal_converge_cancelled(store: Store, goal_id: str) -> bool:
    """cancelling -> cancelled, compare-and-set. Fires only when the row still
    holds `cancelling`; the caller decides WHEN (no unconfirmed execution
    remains). Returns whether it fired — a converged Goal stays converged."""
    return store.goal_transition(goal_id, GoalStatus.CANCELLING, GoalStatus.CANCELLED)


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
    """Host claim: exactly one caller may move waiting_host -> running.

    The unified startup gate (G010 T002/T003) reads the goal status INSIDE
    this transaction: while the Goal is paused or cancelling (or converged
    to cancelled) a parked assignment cannot be claimed — the gate decision
    and the claim write serialize against a concurrent `goal pause` /
    `goal cancel` in the same BEGIN IMMEDIATE, so there is no window where
    either lands between the check and the claim."""
    with store.tx():
        row = store.task_get(revision_row_id, task_id)
        if row.status != TaskStatus.WAITING_HOST.value:
            raise ConflictError(
                f"task {task_id} is {row.status}, not waiting_host; nothing to claim"
            )
        goal_status = store.goal_status_for_revision(revision_row_id)
        if goal_status == GoalStatus.PAUSED.value:
            raise GoalPausedError(
                f"goal is paused; task {task_id} stays parked:"
                " run `orx goal resume` before claiming"
            )
        # G010 T003: the same gate refuses while a cancel is pending (or has
        # converged) — a parked assignment is unstarted work the cancel owns.
        if goal_status in (GoalStatus.CANCELLING.value, GoalStatus.CANCELLED.value):
            raise GoalCancellingError(
                f"goal is {goal_status}; task {task_id} stays parked:"
                " a cancel was requested and no new executor starts"
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
    revisions, so the active revision never contains them.

    G010 T002: the goal-side completion write is a compare-and-set on
    active -> done. A PAUSED goal is never lifted here — the caller's Goal
    object may be a stale snapshot (a pause can commit between the caller's
    read and this refresh), so legality is decided by the row, not by the
    argument. When every task passed during a pause the run records done
    and the goal converges to done at `goal resume` (the same invariant,
    replayed after the transition)."""
    revision = store.revision_active(run.id)
    if revision is None:
        if run.status != RunStatus.PLANNING.value:
            store.run_set_status(run.id, RunStatus.PLANNING)
        return
    statuses = {TaskStatus(t.status) for t in store.tasks_all(revision.id)}
    if statuses and statuses <= {TaskStatus.PASSED}:
        store.run_set_status(run.id, RunStatus.DONE)
        store.goal_complete_if_active(goal.id)
    elif statuses & ACTIVE_EXECUTION_STATUSES:
        store.run_set_status(run.id, RunStatus.RUNNING)
    else:
        store.run_set_status(run.id, RunStatus.BLOCKED)


def refresh(store: Store, goal: Goal, run: Run) -> None:
    refresh_readiness(store, run)
    refresh_run(store, goal, run)
