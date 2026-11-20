"""Per-row verification validity over one ORX snapshot (0.5.0 closeout).

``tasks.get`` keeps every historical verification row with its original
``passed`` value and identity. This module projects the CURRENT window over
those rows using the same rules as ORX ``Store.verifications_current``
(``src/orx/state.py`` of the ORX repo):

- the boundary is the task's latest WORKER attempt in the same snapshot,
  revision and task: rows bound to older worker attempts are history;
- the latest ``verification_context`` task event carries a
  ``verification_after`` row-id watermark: rows at or below it are history;
- a modern delivery (a context bound to the current worker) cannot inherit
  unbound legacy rows (``attempt_id`` NULL).

Analytics reads a snapshot, not the live ORX database, so a missing,
corrupt, worker-mismatched or non-reusable basis is reported as UNKNOWN
(``current: null``) — never confirmed, and never silently falling back to an
earlier good context (the latest event wins, exactly as in ORX, where an
invalid context is an error rather than a rewind). The projection is
SELECT-only and never edits the mirror tables or the source database.
"""

from __future__ import annotations

import json
from typing import Any

from .store import Store

CONTEXT_EVENT = "verification_context"

#: reasons attached to rows outside (or unable to enter) the current window
REASON_OLDER_WORKER = "history: bound to an older worker attempt"
REASON_WATERMARK = "history: at or below the verification-context watermark"
REASON_UNBOUND = "history: unbound legacy row a modern delivery cannot inherit"
REASON_MISSING_CONTEXT = "unknown: verification context missing"
REASON_INVALID_CONTEXT = "unknown: latest verification context invalid"
REASON_WORKER_MISMATCH = "unknown: context does not match the current worker"
REASON_NOT_REUSABLE = "unknown: context inputs are not reusable"

#: context_state -> per-row reason when the basis cannot confirm anything
_UNKNOWN_REASONS = {
    "missing": REASON_MISSING_CONTEXT,
    "invalid": REASON_INVALID_CONTEXT,
    "worker_mismatch": REASON_WORKER_MISMATCH,
    "not_reusable": REASON_NOT_REUSABLE,
}


def _current_worker(store: Store, snapshot_id: int, revision_id: int,
                    task_id: str) -> int | None:
    row = store.conn.execute(
        "SELECT id FROM orx_attempts WHERE snapshot_id=? AND revision_id=? "
        "AND task_id=? AND role='worker' ORDER BY id DESC LIMIT 1",
        (snapshot_id, revision_id, task_id)).fetchone()
    return row["id"] if row else None


def _latest_context(store: Store, snapshot_id: int, revision_id: int,
                    task_id: str) -> tuple[int | None, dict | None, bool]:
    """(event id, parsed payload, valid). The LATEST event wins; an invalid
    latest event never falls back to an earlier good one."""
    row = store.conn.execute(
        "SELECT id, reason FROM orx_task_events WHERE snapshot_id=? AND "
        "revision_id=? AND task_id=? AND event=? ORDER BY id DESC LIMIT 1",
        (snapshot_id, revision_id, task_id, CONTEXT_EVENT)).fetchone()
    if row is None:
        return None, None, False
    try:
        context = json.loads(row["reason"])
        if (context["version"] != 1
                or type(context["verification_after"]) is not int
                or context["verification_after"] < 0
                or not isinstance(context["manifest"], dict)
                or not isinstance(context["digest"], str)
                or type(context["reusable"]) is not bool
                or not (context["worker_attempt"] is None
                        or type(context["worker_attempt"]) is int)):
            raise ValueError("invalid context fields")
    except (ValueError, TypeError, KeyError):
        return row["id"], None, False
    return row["id"], context, True


def _classify_row(row: dict, worker: int | None, state: str, floor: int,
                  allow_unbound: bool) -> dict:
    attempt_id = row["attempt_id"]
    if (attempt_id is not None and worker is not None
            and attempt_id < worker):
        return {"current": False, "validity_reason": REASON_OLDER_WORKER}
    if state != "valid":
        return {"current": None,
                "validity_reason": _UNKNOWN_REASONS[state]}
    if row["id"] <= floor:
        return {"current": False,
                "validity_reason": f"{REASON_WATERMARK} "
                                   f"(verification_after={floor})"}
    if attempt_id is None and not allow_unbound:
        return {"current": False, "validity_reason": REASON_UNBOUND}
    return {"current": True, "validity_reason": None}


def classify(store: Store, snapshot_id: int, revision_id: int,
             task_id: str) -> dict:
    """Project the current verification window of one task over its snapshot
    rows. Returns ``{"window": {…summary…}, "rows": {verification id:
    {"current", "validity_reason"}}}`` — pure, SELECT-only, snapshot-scoped
    (another project, run or revision never mixes in)."""
    worker = _current_worker(store, snapshot_id, revision_id, task_id)
    event_id, context, valid = _latest_context(store, snapshot_id,
                                               revision_id, task_id)
    if not valid:
        state = "missing" if event_id is None else "invalid"
        floor = 0
        allow_unbound = True
    elif context["worker_attempt"] != worker:
        state = "worker_mismatch"
        floor = 0
        allow_unbound = True
    elif not context["reusable"]:
        state = "not_reusable"
        floor = 0
        allow_unbound = True
    else:
        state = "valid"
        floor = context["verification_after"]
        allow_unbound = worker is None
    verifications = [dict(r) for r in store.conn.execute(
        "SELECT id, attempt_id, passed FROM orx_verifications WHERE "
        "snapshot_id=? AND revision_id=? AND task_id=? ORDER BY id",
        (snapshot_id, revision_id, task_id))]
    rows = {r["id"]: _classify_row(r, worker, state, floor, allow_unbound)
            for r in verifications}
    current_rows = [r for r in verifications if rows[r["id"]]["current"]]
    taken = store.conn.execute(
        "SELECT taken_at FROM orx_snapshots WHERE snapshot_id=?",
        (snapshot_id,)).fetchone()
    window: dict[str, Any] = {
        "basis": "snapshot",
        "snapshot_id": snapshot_id,
        "taken_at": taken["taken_at"] if taken else None,
        "note": "projected from the recorded ORX snapshot at import time; "
                "not a guarantee that files are unchanged since collection",
        "context_state": state,
        "context_event_id": event_id,
        "worker_attempt_id": worker,
        "verification_after": floor if state == "valid" else None,
        "counts": {
            "current": len(current_rows),
            "current_passed": sum(1 for r in current_rows if r["passed"] == 1),
            "historical": sum(1 for v in rows.values()
                              if v["current"] is False),
            "unknown": sum(1 for v in rows.values()
                           if v["current"] is None)},
    }
    return {"window": window, "rows": rows}
