"""Pure-function query layer (design §7) — the app's read model.

Six interfaces over the analytics store, SELECT-only:

==============  =====================================================
``overview``    active runs, attention items, per-source health
``runs.list``   cross-project history with filters, paging, sorting
``runs.get``    one run: revisions, tasks, usage books, native preview
``tasks.get``   one task: attempts, usage books, verifications, retries
``projects.list`` registration, accessibility, snapshot health
``health``      protocol/reader versions, engine status, source health
==============  =====================================================

Every response shares one envelope: ``op``, ``protocol_version``,
``generated_at``, ``snapshots`` (per-project successful-snapshot identity;
cross-project results keep each project's own update time), ``coverage``
and ``warnings``. Lists paginate and always sort with the composite
identity key as the secondary key, so paging is stable. Consumption
sorting carries its coverage and sorts unknown-consumption entries LAST
in both directions — a run with no usage data can never look like the
cheapest one (design §3).

``refresh.request`` is executed by the engine layer (design §7/§8); this
module freezes its request/response contract (``refresh_contract`` /
``validate_refresh_request``).

Reading rules carried over from the frozen contracts: unknown stays
``None`` (only a stored zero is ``0``); identity is composite (short
numbers are never keys); related queries of one project read the SAME
latest successful snapshot (a newer failed import never serves data);
projects whose snapshot schema version is outside the ORX gate
(probe.SUPPORTED_ORX_SCHEMA) are isolated with a warning, never a crash;
data health uses the display contract's precedence and reports 未知 for
unassessed facts instead of guessing 正常.
"""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import READER_VERSION, display, usage_readmodel
from .dbutil import open_ro
from .metrics import _canonical_observations as _canon
from .probe import SUPPORTED_ORX_SCHEMA
from .registry import list_projects as _registry_projects
from .store import STORE_VERSION, Store

PROTOCOL_VERSION = 1
OPS = ("overview", "runs.list", "runs.get", "tasks.get", "projects.list",
       "health")

REFRESH_OP = "refresh.request"
REFRESH_REASONS = ("panel-open", "detail-open", "background", "manual",
                   "history-audit")

#: how old a snapshot may be before the data-health dimension reports
#: 更新延迟. The engine's slowest state tier is ~60s (design §8), so the
#: read layer defaults to that tier plus slack; callers can pass their own.
DEFAULT_STALE_AFTER_SECONDS = 90.0

MAX_PAGE = 500
SORT_KEYS = ("started_at", "completed_at", "observed_tokens")
ORDERS = ("asc", "desc")

_field_sums = usage_readmodel._field_sums
_missing_fields = usage_readmodel._missing_fields
_accuracy = usage_readmodel._accuracy


# ----------------------------------------------------------------- helpers ---

def _now_dt(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _epoch_ms(value: str | None) -> int | None:
    dt = _parse_iso(value)
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def _warn(warnings: list[dict], code: str, project: str | None,
          detail: str) -> None:
    warnings.append({"code": code, "project": project, "detail": detail})


def _warn_once(warnings: list[dict], code: str, project: str | None,
               detail: str) -> None:
    entry = {"code": code, "project": project, "detail": detail}
    if entry not in warnings:
        warnings.append(entry)


def _validate_paging(limit: int, offset: int) -> None:
    if not isinstance(limit, int) or not isinstance(offset, int):
        raise ValueError("limit and offset must be integers")
    if limit < 0 or offset < 0:
        raise ValueError("limit and offset must be >= 0")
    if limit > MAX_PAGE:
        raise ValueError(f"limit must be <= {MAX_PAGE}")


# ------------------------------------------------------- snapshot pinning ---

_SNAP_COLS = ("snapshot_id, project_key, taken_at, schema_version, "
              "source_path")


def _successful_snapshot_ids(store: Store) -> dict[str, int]:
    """project_key -> snapshot_id of its latest COMPLETED import (the
    importer records snapshot_id in counts_json only on success)."""
    out: dict[str, int] = {}
    for r in store.conn.execute(
            "SELECT source_id, counts_json FROM import_runs "
            "WHERE status='done' AND source_id LIKE 'orx:%' ORDER BY id"):
        key = r["source_id"][4:]
        try:
            sid = json.loads(r["counts_json"] or "{}").get("snapshot_id")
        except (TypeError, ValueError):
            continue
        if sid is not None:
            out[key] = sid          # later rows overwrite: latest wins
    return out


def _snapshot_row(store: Store, snapshot_id: int) -> dict | None:
    r = store.conn.execute(
        f"SELECT {_SNAP_COLS} FROM orx_snapshots WHERE snapshot_id=?",
        (snapshot_id,)).fetchone()
    return dict(r) if r else None


def _read_snapshot_map(store: Store, keys: list[str],
                       warnings: list[dict]) -> dict[str, dict | None]:
    """Latest successful snapshot per project. Cross-project results keep
    each project's own row (own update time). A project without a
    completed import falls back to its latest snapshot row with a warning
    — a newer failed import never serves data."""
    successful = _successful_snapshot_ids(store)
    out: dict[str, dict | None] = {}
    for key in keys:
        if key in successful:
            out[key] = _snapshot_row(store, successful[key])
            continue
        r = store.conn.execute(
            f"SELECT {_SNAP_COLS} FROM orx_snapshots WHERE project_key=? "
            f"ORDER BY snapshot_id DESC LIMIT 1", (key,)).fetchone()
        if r is not None:
            out[key] = dict(r)
            _warn_once(warnings, "snapshot-success-unknown", key,
                       "no completed import recorded; using the latest "
                       "snapshot row")
        else:
            out[key] = None
    return out


def _snapshots_view(snap_map: dict[str, dict | None]) -> dict:
    out = {}
    for key in sorted(snap_map):
        row = snap_map[key]
        if row is None:
            out[key] = {"snapshot_id": None, "taken_at": None,
                        "schema_version": None, "supported": None}
        else:
            out[key] = {
                "snapshot_id": row["snapshot_id"],
                "taken_at": row["taken_at"],
                "schema_version": row["schema_version"],
                "supported": row["schema_version"] in SUPPORTED_ORX_SCHEMA,
                "source_path": row["source_path"]}
    return out


# --------------------------------------------------------- project context ---

def _project_keys(store: Store, project: str | None,
                  warnings: list[dict]) -> list[str]:
    registered = sorted(p["project_key"] for p in _registry_projects(store))
    if project is None:
        return registered
    key = display.project_key(project)
    if key not in registered:
        _warn(warnings, "unknown-project-filter", key,
              f"project filter {project!r} matches no registered project")
        return []
    return [key]


def _project_context(store: Store, key: str,
                     snap_row: dict) -> dict[str, Any]:
    """Bulk read of one project's pinned snapshot (SELECT-only)."""
    sid = snap_row["snapshot_id"]
    runs = [dict(r) for r in store.conn.execute(
        "SELECT * FROM orx_runs WHERE snapshot_id=? ORDER BY id", (sid,))]
    goals = {r["id"]: dict(r) for r in store.conn.execute(
        "SELECT * FROM orx_goals WHERE snapshot_id=?", (sid,))}
    revisions = [dict(r) for r in store.conn.execute(
        "SELECT * FROM orx_revisions WHERE snapshot_id=? "
        "ORDER BY revision, id", (sid,))]
    tasks = [dict(r) for r in store.conn.execute(
        "SELECT * FROM orx_tasks WHERE snapshot_id=? ORDER BY id", (sid,))]
    attempts = [dict(r) for r in store.conn.execute(
        "SELECT * FROM orx_attempts WHERE snapshot_id=? ORDER BY id",
        (sid,))]
    canon = (_canon(store, sid, [a["id"] for a in attempts])
             if attempts else {})
    tasks_by_rev: dict[int, list[dict]] = {}
    for t in tasks:
        tasks_by_rev.setdefault(t["revision_id"], []).append(t)
    revs_by_run: dict[str, list[dict]] = {}
    for r in revisions:
        revs_by_run.setdefault(r["run_id"], []).append(r)
    atts_by_run: dict[str | None, list[dict]] = {}
    for a in attempts:
        atts_by_run.setdefault(a["run_id"], []).append(a)
    atts_by_task: dict[tuple[int, str], list[dict]] = {}
    for a in attempts:
        if a["revision_id"] is not None and a["task_id"] is not None:
            atts_by_task.setdefault(
                (a["revision_id"], a["task_id"]), []).append(a)
    return {"snapshot_id": sid, "runs": runs, "goals": goals,
            "revisions": revisions, "tasks": tasks, "attempts": attempts,
            "canon": canon, "tasks_by_rev": tasks_by_rev,
            "revs_by_run": revs_by_run, "atts_by_run": atts_by_run,
            "atts_by_task": atts_by_task}


def _coverage_block(atts: list[dict], canon: dict) -> dict:
    observed = sum(1 for a in atts if a["id"] in canon)
    missing_recorded = sum(1 for a in atts
                           if a["id"] not in canon
                           and a["usage_missing_reason"])
    return {"observed": observed, "attempts": len(atts),
            "missing_recorded": missing_recorded,
            "unassessed": len(atts) - observed - missing_recorded}


def _run_consumption(atts: list[dict], canon: dict) -> dict:
    """Attributed consumption of one run — the SAME classification as
    usage_readmodel.run_usage (planner + controller + every task-linked
    attempt; unattributable attempts excluded from the total), computed
    in bulk so lists stay one query per project."""
    planner = [a for a in atts if a["role"] == "planner"]
    controller = [a for a in atts if a["role"] == "controller"]
    named = {a["id"] for a in planner + controller}
    task_atts = [a for a in atts
                 if a["id"] not in named
                 and a["revision_id"] is not None
                 and a["task_id"] is not None]
    attributed = planner + controller + task_atts
    rows = [canon[a["id"]] for a in attributed if a["id"] in canon]
    tokens = _field_sums(rows)
    observed_total = (
        None if tokens["input_tokens"] is None
        and tokens["output_tokens"] is None
        else (tokens["input_tokens"] or 0) + (tokens["output_tokens"] or 0))
    return {"caliber": usage_readmodel.CALIBER_ORX,
            "tokens": tokens,
            "observed_total": observed_total,
            "missing_fields": _missing_fields(rows),
            "accuracy": _accuracy(rows),
            "coverage": _coverage_block(atts, canon)}


def _task_consumption(atts: list[dict], canon: dict) -> dict:
    """Task-level books — the same scope as usage_readmodel.task_usage
    totals: worker + verifier attempts (other task-linked roles stay
    listed but outside the total until a contract names them)."""
    worker = [a for a in atts if a["role"] == "worker"]
    verifier = [a for a in atts if a["role"] == "verifier"]
    rows = [canon[a["id"]] for a in worker + verifier
            if a["id"] in canon]
    tokens = _field_sums(rows)
    observed_total = (
        None if tokens["input_tokens"] is None
        and tokens["output_tokens"] is None
        else (tokens["input_tokens"] or 0) + (tokens["output_tokens"] or 0))
    return {"caliber": usage_readmodel.CALIBER_ORX,
            "tokens": tokens,
            "observed_total": observed_total,
            "missing_fields": _missing_fields(rows),
            "accuracy": _accuracy(rows),
            "coverage": _coverage_block(atts, canon)}


def _time_basis(run: dict) -> tuple[str | None, str | None]:
    for field in ("started_at", "created_at", "updated_at"):
        if run.get(field):
            return field, run[field]
    return None, None


def _run_item(key: str, run: dict, ctx: dict, snap_row: dict) -> dict:
    goal = ctx["goals"].get(run["goal_id"]) if run["goal_id"] else None
    revs = ctx["revs_by_run"].get(run["id"], [])
    cur = revs[-1] if revs else None     # ordered by revision: last = current
    cur_tasks = ctx["tasks_by_rev"].get(cur["id"], []) if cur else []
    atts = ctx["atts_by_run"].get(run["id"], [])
    basis_field, basis_value = _time_basis(run)
    return {
        "project": key, "run_id": run["id"],
        "identity": {"project": key, "run_id": run["id"]},
        "goal": {
            "id": run["goal_id"],
            "objective": goal["objective"] if goal else None,
            "status": display.goal_status(goal["status"]) if goal else None},
        "status": display.run_status(run["status"]),
        "started_at": run["started_at"],
        "completed_at": run["completed_at"],
        "updated_at": run["updated_at"],
        "created_at": run["created_at"],
        "time_basis": {"field": basis_field, "value": basis_value},
        "progress": {
            "completed": sum(1 for t in cur_tasks if t["status"] == "passed"),
            "total": len(cur_tasks),
            "revision_id": cur["id"] if cur else None,
            "revision": cur["revision"] if cur else None,
            "basis": "tasks of the current (highest) revision only"},
        "consumption": _run_consumption(atts, ctx["canon"]),
        "data_as_of": snap_row["taken_at"],
        "schema_version": snap_row["schema_version"],
    }


def _recovery(atts: list[dict]) -> tuple[bool, list[int], list[int]]:
    """(recovered, failing_attempt_ids, successor_attempt_ids). A failed
    task is CURRENT — worth attention — only when no later attempt of the
    same (revision, task) has taken over: completed, or still running.
    History that a successor already replaced is not an anomaly (§4).
    Timestamps compare as epoch ms so mixed ISO shapes stay honest."""
    failed = [a for a in atts if a["result"] == "failed"]
    failing = sorted(a["id"] for a in failed)
    last_end_ms = max((ms for ms in (_epoch_ms(a["ended_at"])
                                     for a in failed) if ms is not None),
                      default=None)
    successors = []
    for a in atts:
        started = _epoch_ms(a["started_at"])
        after = (started is not None
                 and (last_end_ms is None or started > last_end_ms))
        if after and (a["result"] == "completed" or a["ended_at"] is None):
            successors.append(a["id"])
    successors.sort()
    return bool(failed) and bool(successors), failing, successors


def _attention_for_run(key: str, run: dict, ctx: dict) -> list[dict]:
    out: list[dict] = []
    if run["status"] == "blocked":
        out.append({"kind": "blocked_run", "project": key,
                    "run_id": run["id"], "raw_status": run["status"],
                    "detail": "run blocked (受阻)"})
    revs = ctx["revs_by_run"].get(run["id"], [])
    if not revs:
        return out
    cur = revs[-1]
    for t in ctx["tasks_by_rev"].get(cur["id"], []):
        if t["status"] != "failed":
            continue
        atts = ctx["atts_by_task"].get((cur["id"], t["task_id"]), [])
        recovered, failing, successors = _recovery(atts)
        if recovered:
            continue
        out.append({
            "kind": "failed_task", "project": key, "run_id": run["id"],
            "revision_id": cur["id"], "task_id": t["task_id"],
            "detail": t["failure_reason"] or "task failed",
            "evidence": {"failing_attempt_ids": failing,
                         "successor_attempt_ids": successors,
                         "recovered": False}})
    return out


# --------------------------------------------------------- source health ---

def _live_source_facts(project_key: str) -> dict:
    """Read-only facts about the project's live state.db (observability
    contract: read-only open, named columns, version gate)."""
    db = Path(project_key) / ".orx" / "state.db"
    facts = {"state_db_present": db.exists()}
    if not db.exists():
        return {**facts, "readable": False, "schema_version": None}
    try:
        conn = open_ro(db)
    except sqlite3.Error:
        return {**facts, "readable": False, "schema_version": None}
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error:
        return {**facts, "readable": False, "schema_version": None}
    finally:
        conn.close()
    return {**facts, "readable": True,
            "schema_version": row[0] if row else None}


def _project_health(project_key: str, snap_row: dict | None,
                    now: datetime, stale_after: float | None,
                    warnings: list[dict]) -> dict:
    """Data health of one project: display.data_health over live source
    facts + snapshot staleness. Unassessed facts stay None — health then
    reports 未知, never a guessed 正常."""
    live = _live_source_facts(project_key)
    if not live["state_db_present"]:
        if not Path(project_key).exists():
            _warn_once(warnings, "project-directory-missing", project_key,
                       "registered project directory no longer exists; "
                       "historical data is kept")
        else:
            _warn_once(warnings, "no-orx-state-db", project_key,
                       "no .orx/state.db: registered for basic session "
                       "analysis only")
    if live["readable"] and live["schema_version"] is not None:
        schema_supported = live["schema_version"] in SUPPORTED_ORX_SCHEMA
    elif snap_row is not None:
        schema_supported = snap_row["schema_version"] in SUPPORTED_ORX_SCHEMA
    else:
        schema_supported = None
    stale: bool | None = None
    if snap_row is not None and stale_after is not None:
        taken = _parse_iso(snap_row["taken_at"])
        if taken is not None:
            stale = (now - taken).total_seconds() > stale_after
    return display.data_health(
        source_readable=live["readable"] if live["state_db_present"]
        else False,
        schema_supported=schema_supported, stale=stale)


# ------------------------------------------------------------ sort & page ---

def _sort_stable(items: list[dict], value_of: Callable[[dict], Any],
                 order: str) -> list[dict]:
    """Sort with the composite identity as the secondary key. Unknown
    (None) primary values sort LAST in both directions — missing data
    must never look smallest."""
    ident = lambda it: (it["project"], it["run_id"])  # noqa: E731
    decorated = []
    for it in items:
        v = value_of(it)
        unknown = v is None
        second = 0 if unknown else (v if order == "asc" else -v)
        decorated.append(((1 if unknown else 0, second), ident(it), it))
    decorated.sort(key=lambda d: (d[0], d[1]))
    return [d[2] for d in decorated]


def _page_meta(items: list[dict], page: list[dict], limit: int,
               offset: int) -> dict:
    return {"limit": limit, "offset": offset, "total_matched": len(items),
            "returned": len(page)}


# ------------------------------------------------------------- envelope ---

def _envelope(op: str, payload: dict, snap_map: dict[str, dict | None],
              coverage: dict, warnings: list[dict],
              now: datetime | None) -> dict:
    resp: dict[str, Any] = {
        "op": op,
        "protocol_version": PROTOCOL_VERSION,
        "generated_at": _iso(_now_dt(now))}
    resp.update(payload)
    resp["snapshots"] = _snapshots_view(snap_map)
    resp["coverage"] = coverage
    resp["warnings"] = warnings
    return resp


# =========================================================== runs.list ===

def runs_list(store: Store, *, project: str | None = None,
              text: str | None = None, run_id: str | None = None,
              task_id: str | None = None, status: str | None = None,
              model: str | None = None, since: str | None = None,
              until: str | None = None, sort: str = "started_at",
              order: str = "desc", limit: int = 50, offset: int = 0,
              now: datetime | None = None) -> dict:
    """Cross-project run history (design §3/§7). Filters: project, goal
    or task title text, run/task number, status, model, time range.
    Sorting: time or observed consumption (always with coverage); the
    composite identity is the secondary key so paging is stable."""
    _validate_paging(limit, offset)
    if sort not in SORT_KEYS:
        raise ValueError(f"sort must be one of {SORT_KEYS}")
    if order not in ORDERS:
        raise ValueError(f"order must be one of {ORDERS}")
    warnings: list[dict] = []
    keys = _project_keys(store, project, warnings)
    snap_map = _read_snapshot_map(store, keys, warnings)
    items: list[dict] = []
    excluded: list[dict] = []
    for key in sorted(keys):
        row = snap_map[key]
        if row is None:
            excluded.append({"project": key, "reason": "no snapshot"})
            _warn_once(warnings, "no-snapshot", key,
                       "no ORX snapshot imported yet")
            continue
        if row["schema_version"] not in SUPPORTED_ORX_SCHEMA:
            excluded.append({"project": key, "reason":
                             f"unsupported snapshot schema "
                             f"{row['schema_version']}"})
            _warn_once(warnings, "unsupported-snapshot-schema", key,
                       f"snapshot schema version "
                       f"{row['schema_version']} is outside the gate "
                       f"{sorted(SUPPORTED_ORX_SCHEMA, key=int)}; project "
                       f"isolated")
            continue
        ctx = _project_context(store, key, row)
        for run in ctx["runs"]:
            if not _run_matches(run, ctx, text=text, run_id=run_id,
                                task_id=task_id, status=status, model=model,
                                since=since, until=until):
                continue
            items.append(_run_item(key, run, ctx, row))
    if sort == "observed_tokens":
        value_of = lambda it: it["consumption"]["observed_total"]  # noqa: E731
    elif sort == "completed_at":
        value_of = lambda it: _epoch_ms(it["completed_at"])  # noqa: E731
    else:
        value_of = lambda it: _epoch_ms(it["time_basis"]["value"])  # noqa: E731
    items = _sort_stable(items, value_of, order)
    page = items[offset:offset + limit]
    for it in page:
        if not it["status"]["known"]:
            _warn_once(warnings, "unknown-run-status", it["project"],
                       f"run {it['run_id']} raw status "
                       f"{it['status']['raw']!r} is not recognized")
    coverage = {"projects": {"considered": len(keys),
                             "queried": len(keys) - len(excluded),
                             "excluded": excluded},
                "runs": {"matched": len(items)}}
    query = {
        "filters": {k: v for k, v in
                    (("project", project), ("text", text),
                     ("run_id", run_id), ("task_id", task_id),
                     ("status", status), ("model", model),
                     ("since", since), ("until", until)) if v is not None},
        "sort": {"key": sort, "order": order,
                 "secondary": "identity(project, run_id)"}}
    return _envelope("runs.list", {"query": query, "runs": page,
                                   "page": _page_meta(items, page, limit,
                                                      offset)},
                     snap_map, coverage, warnings, now)


def _run_matches(run: dict, ctx: dict, *, text: str | None,
                 run_id: str | None, task_id: str | None,
                 status: str | None, model: str | None,
                 since: str | None, until: str | None) -> bool:
    if run_id is not None and run["id"] != run_id:
        return False
    if status is not None and run["status"] != status:
        return False
    _, time_value = _time_basis(run)
    if since is not None or until is not None:
        if time_value is None:
            return False
        if since is not None and time_value < since:
            return False
        if until is not None and time_value > until:
            return False
    if text is not None:
        needle = text.casefold()
        goal = ctx["goals"].get(run["goal_id"]) if run["goal_id"] else None
        in_goal = goal is not None and needle in (goal["objective"]
                                                  or "").casefold()
        revs = ctx["revs_by_run"].get(run["id"], [])
        in_task = any(
            needle in (t["objective"] or "").casefold()
            for rev in revs
            for t in ctx["tasks_by_rev"].get(rev["id"], []))
        if not (in_goal or in_task):
            return False
    if task_id is not None:
        revs = ctx["revs_by_run"].get(run["id"], [])
        in_tasks = any(
            t["task_id"] == task_id
            for rev in revs
            for t in ctx["tasks_by_rev"].get(rev["id"], []))
        in_atts = any(a["task_id"] == task_id
                      for a in ctx["atts_by_run"].get(run["id"], []))
        if not (in_tasks or in_atts):
            return False
    if model is not None:
        atts = ctx["atts_by_run"].get(run["id"], [])
        needle = model.casefold()
        if not any(needle in ((a["model"] or "").casefold(),
                              (a["actual_model"] or "").casefold())
                   for a in atts):
            return False
    return True


# ============================================================ runs.get ===

def runs_get(store: Store, project: str, run_id: str,
             now: datetime | None = None) -> dict:
    """One run's detail. Every sub-query (run facts, task list, usage
    books, native preview) reads the SAME latest successful snapshot."""
    warnings: list[dict] = []
    key = display.project_key(project)
    snap_map = _read_snapshot_map(store, [key], warnings)
    row = snap_map[key]
    payload: dict[str, Any] = {"project": key, "run_id": run_id}
    if row is None:
        _warn(warnings, "no-snapshot", key, "no ORX snapshot imported yet")
        payload.update({"found": False, "reason": "no snapshot"})
        return _envelope("runs.get", payload, snap_map,
                         {"project": key, "found": False}, warnings, now)
    sid = row["snapshot_id"]
    if row["schema_version"] not in SUPPORTED_ORX_SCHEMA:
        _warn(warnings, "unsupported-snapshot-schema", key,
              f"snapshot schema version {row['schema_version']} is outside "
              f"the gate {sorted(SUPPORTED_ORX_SCHEMA, key=int)}")
        payload.update({"found": False,
                        "reason": "unsupported snapshot schema"})
        return _envelope("runs.get", payload, snap_map,
                         {"project": key, "found": False, "snapshot_id": sid},
                         warnings, now)
    ctx = _project_context(store, key, row)
    run = next((r for r in ctx["runs"] if r["id"] == run_id), None)
    if run is None:
        _warn(warnings, "run-not-found", key,
              f"run {run_id!r} not in snapshot {sid}")
        payload.update({"found": False, "reason": "run not found"})
        return _envelope("runs.get", payload, snap_map,
                         {"project": key, "found": False, "snapshot_id": sid},
                         warnings, now)
    revs = ctx["revs_by_run"].get(run_id, [])
    revisions = [{"revision_id": r["id"], "revision": r["revision"],
                  "status": r["status"], "created_at": r["created_at"],
                  "task_count": len(ctx["tasks_by_rev"].get(r["id"], []))}
                 for r in revs]
    tasks = []
    for rev in revs:
        for t in ctx["tasks_by_rev"].get(rev["id"], []):
            tatts = ctx["atts_by_task"].get((rev["id"], t["task_id"]), [])
            tasks.append({
                "project": key, "run_id": run_id,
                "identity": {"project": key, "run_id": run_id,
                             "revision_id": rev["id"],
                             "task_id": t["task_id"]},
                "revision_id": rev["id"], "revision": rev["revision"],
                "task_id": t["task_id"], "objective": t["objective"],
                "status": display.task_status(t["status"]),
                "failure_reason": t["failure_reason"],
                "attempt_count": len(tatts),
                "consumption": _task_consumption(tatts, ctx["canon"])})
    if not run["status"] or run["status"] not in display.RUN_DISPLAY:
        _warn_once(warnings, "unknown-run-status", key,
                   f"run {run_id} raw status {run['status']!r} is not "
                   f"recognized")
    payload.update({
        "found": True,
        "run": _run_item(key, run, ctx, row),
        "revisions": revisions,
        "tasks": tasks,
        "usage": usage_readmodel.run_usage(store, key, run_id,
                                           snapshot_id=sid),
        "native_preview": usage_readmodel.native_session_preview(
            store, key, run_id, snapshot_id=sid),
    })
    return _envelope("runs.get", payload, snap_map,
                     {"project": key, "found": True, "snapshot_id": sid},
                     warnings, now)


# =========================================================== tasks.get ===

def tasks_get(store: Store, project: str, run_id: str, revision_id: int,
              task_id: str, now: datetime | None = None) -> dict:
    """One task's detail: identity, facts, usage books with attempts and
    session links (T002 read model), verification rows, retry summary."""
    warnings: list[dict] = []
    key = display.project_key(project)
    snap_map = _read_snapshot_map(store, [key], warnings)
    row = snap_map[key]
    payload: dict[str, Any] = {"project": key, "run_id": run_id,
                               "revision_id": revision_id,
                               "task_id": task_id}
    if row is None:
        _warn(warnings, "no-snapshot", key, "no ORX snapshot imported yet")
        payload.update({"found": False, "reason": "no snapshot"})
        return _envelope("tasks.get", payload, snap_map,
                         {"project": key, "found": False}, warnings, now)
    sid = row["snapshot_id"]
    if row["schema_version"] not in SUPPORTED_ORX_SCHEMA:
        _warn(warnings, "unsupported-snapshot-schema", key,
              f"snapshot schema version {row['schema_version']} is outside "
              f"the gate {sorted(SUPPORTED_ORX_SCHEMA, key=int)}")
        payload.update({"found": False,
                        "reason": "unsupported snapshot schema"})
        return _envelope("tasks.get", payload, snap_map,
                         {"project": key, "found": False, "snapshot_id": sid},
                         warnings, now)
    task_row = store.conn.execute(
        "SELECT * FROM orx_tasks WHERE snapshot_id=? AND revision_id=? AND "
        "task_id=?", (sid, revision_id, task_id)).fetchone()
    usage = usage_readmodel.task_usage(store, key, run_id, revision_id,
                                       task_id, snapshot_id=sid)
    if task_row is None and not usage["attempts"]:
        _warn(warnings, "task-not-found", key,
              f"task {task_id!r} of run {run_id!r} revision {revision_id} "
              f"not in snapshot {sid}")
        payload.update({"found": False, "reason": "task not found"})
        return _envelope("tasks.get", payload, snap_map,
                         {"project": key, "found": False, "snapshot_id": sid},
                         warnings, now)
    recovered, failing, successors = _recovery(
        [dict(a) for a in store.conn.execute(
            "SELECT id, result, started_at, ended_at FROM orx_attempts "
            "WHERE snapshot_id=? AND run_id=? AND revision_id=? AND "
            "task_id=? ORDER BY id", (sid, run_id, revision_id, task_id))])
    verifications = [dict(v) for v in store.conn.execute(
        "SELECT id, attempt_id, kind, command, exit_code, passed, "
        "created_at FROM orx_verifications WHERE snapshot_id=? AND "
        "revision_id=? AND task_id=? ORDER BY created_at, id",
        (sid, revision_id, task_id))]
    payload.update({
        "found": True,
        "task": {
            "identity": {"project": key, "run_id": run_id,
                         "revision_id": revision_id, "task_id": task_id},
            "objective": task_row["objective"] if task_row else None,
            "status": display.task_status(
                task_row["status"] if task_row else None),
            "failure_reason": task_row["failure_reason"] if task_row
            else None,
            "created_at": task_row["created_at"] if task_row else None,
            "updated_at": task_row["updated_at"] if task_row else None},
        "usage": usage,
        "verifications": verifications,
        "retry": {"recovered": recovered,
                  "failing_attempt_ids": failing,
                  "successor_attempt_ids": successors},
    })
    return _envelope("tasks.get", payload, snap_map,
                     {"project": key, "found": True, "snapshot_id": sid},
                     warnings, now)


# ============================================================= overview ===

def overview(store: Store, *, now: datetime | None = None,
             stale_after: float | None = DEFAULT_STALE_AFTER_SECONDS
             ) -> dict:
    """Menu-bar quick panel data: active runs, attention items, recent
    completions, per-source health. One Run appears once per project;
    attention is current-facts only (a failed round that a successor
    already replaced is history, §4). Grouping follows the G011 lifecycle
    matrix (display.lifecycle_view): the Goal control layer and the Run
    execution facts are combined — a cancelled Goal is history-only (never
    active, never attention, never notified), a paused Goal keeps its
    in-flight runs current with an 已暂停 badge, a cancelling Goal keeps
    its not-yet-closed executions visible, and a run finishing during a
    pause stays recent WITHOUT masquerading as a Goal completion."""
    now = _now_dt(now)
    warnings: list[dict] = []
    keys = sorted(p["project_key"] for p in _registry_projects(store))
    snap_map = _read_snapshot_map(store, keys, warnings)
    active: list[dict] = []
    attention: list[dict] = []
    recent: list[dict] = []
    health_rows: list[dict] = []
    excluded: list[dict] = []
    for key in keys:
        row = snap_map[key]
        health = _project_health(key, row, now, stale_after, warnings)
        health_rows.append({
            "project": key, "data_health": health,
            "snapshot_id": row["snapshot_id"] if row else None,
            "taken_at": row["taken_at"] if row else None})
        if row is None:
            excluded.append({"project": key, "reason": "no snapshot"})
            _warn_once(warnings, "no-snapshot", key,
                       "no ORX snapshot imported yet")
            continue
        if row["schema_version"] not in SUPPORTED_ORX_SCHEMA:
            excluded.append({"project": key, "reason":
                             f"unsupported snapshot schema "
                             f"{row['schema_version']}"})
            _warn_once(warnings, "unsupported-snapshot-schema", key,
                       f"snapshot schema version "
                       f"{row['schema_version']} is outside the gate "
                       f"{sorted(SUPPORTED_ORX_SCHEMA, key=int)}; project "
                       f"isolated")
            continue
        ctx = _project_context(store, key, row)
        for run in ctx["runs"]:
            st = display.run_status(run["status"])
            if not st["known"]:
                _warn_once(warnings, "unknown-run-status", key,
                           f"run {run['id']} raw status "
                           f"{run['status']!r} is not recognized")
            goal = ctx["goals"].get(run["goal_id"]) if run["goal_id"] else None
            goal_st = display.goal_status(goal["status"]) if goal else None
            if goal is not None and not goal_st["known"]:
                _warn_once(warnings, "unknown-goal-status", key,
                           f"goal {run['goal_id']} raw status "
                           f"{goal['status']!r} is not recognized")
            view = display.lifecycle_view(goal_st, st)
            item = _run_item(key, run, ctx, row)
            if view["bucket"] == "active":
                active.append(item)
            elif view["bucket"] == "recent_completed":
                recent.append(item)
            if view["attention"]:
                attention.extend(_attention_for_run(key, run, ctx))
    active = _sort_stable(
        active, lambda it: _epoch_ms(it["time_basis"]["value"]), "desc")
    recent = _sort_stable(recent, lambda it: _epoch_ms(it["completed_at"]),
                          "desc")[:5]
    attention.sort(key=lambda e: (e["project"], e["run_id"],
                                  e.get("task_id") or ""))
    payload = {
        "summary": {"active_runs": len(active), "attention": len(attention),
                    "recent_completed": len(recent),
                    "projects": {"considered": len(keys),
                                 "queried": len(keys) - len(excluded),
                                 "excluded": excluded}},
        "active": active,
        "attention": attention,
        "recent_completed": recent,
        "project_health": health_rows,
    }
    coverage = {"projects": {"considered": len(keys),
                             "queried": len(keys) - len(excluded),
                             "excluded": excluded},
                "active_runs": len(active), "attention": len(attention)}
    return _envelope("overview", payload, snap_map, coverage, warnings, now)


# ======================================================== projects.list ===

def projects_list(store: Store, *, now: datetime | None = None,
                  stale_after: float | None = DEFAULT_STALE_AFTER_SECONDS,
                  limit: int = 100, offset: int = 0) -> dict:
    """Registered projects with accessibility and snapshot health. A
    deleted or unreadable project keeps its place with 来源不可读 —
    historical data is never dropped silently."""
    _validate_paging(limit, offset)
    now = _now_dt(now)
    warnings: list[dict] = []
    registered = _registry_projects(store)
    keys = [p["project_key"] for p in registered]
    snap_map = _read_snapshot_map(store, keys, warnings)
    items = []
    for p in registered:
        key = p["project_key"]
        row = snap_map[key]
        health = _project_health(key, row, now, stale_after, warnings)
        runs_known = 0
        if row is not None:
            runs_known = store.conn.execute(
                "SELECT COUNT(*) c FROM orx_runs WHERE snapshot_id=?",
                (row["snapshot_id"],)).fetchone()["c"]
        items.append({
            "project": key, "name": p["name"],
            "registered_at": p["registered_at"],
            "orx_enabled": p["orx_enabled"],
            "state_db_present": p["state_db_present"],
            "runs_known": runs_known,
            "last_snapshot": (None if row is None else {
                "snapshot_id": row["snapshot_id"],
                "taken_at": row["taken_at"],
                "schema_version": row["schema_version"],
                "supported": row["schema_version"]
                in SUPPORTED_ORX_SCHEMA}),
            "data_health": health})
    page = items[offset:offset + limit]
    return _envelope(
        "projects.list",
        {"projects": page, "page": _page_meta(items, page, limit, offset)},
        snap_map, {"registered": len(items), "returned": len(page)},
        warnings, now)


# ================================================================ health ===

def health(store: Store, *, now: datetime | None = None,
           stale_after: float | None = DEFAULT_STALE_AFTER_SECONDS
           ) -> dict:
    """Engine/source/protocol health. The scheduling engine itself is a
    later layer (design §7/§8); this interface reports honestly that it
    is not implemented here, beside reader/store/protocol versions and
    every source's data health."""
    now = _now_dt(now)
    warnings: list[dict] = []
    keys = sorted(p["project_key"] for p in _registry_projects(store))
    snap_map = _read_snapshot_map(store, keys, warnings)
    rows = []
    for key in keys:
        row = snap_map[key]
        rows.append({
            "project": key,
            "data_health": _project_health(key, row, now, stale_after,
                                           warnings),
            "snapshot_id": row["snapshot_id"] if row else None,
            "taken_at": row["taken_at"] if row else None})
    global_sources = [dict(r) for r in store.conn.execute(
        "SELECT source_id, kind, location, reader_version, last_probe_at "
        "FROM sources ORDER BY source_id")]
    payload = {
        "engine": {"implemented": False,
                   "note": "scheduling, refresh execution and the Unix "
                           "socket server live in the engine layer "
                           "(design §7); this pure layer freezes the "
                           "protocol contract and reads"},
        "interfaces": list(OPS),
        "reader_version": READER_VERSION,
        "store_version": STORE_VERSION,
        "projects": rows,
        "global_sources": global_sources,
    }
    return _envelope("health", payload, snap_map,
                     {"registered": len(keys), "sources_listed":
                      len(global_sources)}, warnings, now)


# ===================================================== refresh contract ===

_REFRESH_REQUEST_SPEC = {
    "op": REFRESH_OP,
    "protocol_version": PROTOCOL_VERSION,
    "fields": {
        "projects": {"type": "array<string> | null", "required": False,
                     "semantics": "null means every registered project"},
        "reason": {"type": "string", "required": False,
                   "enum": list(REFRESH_REASONS)},
    },
}

_REFRESH_RESPONSE_SPEC = {
    "op": "refresh.accepted",
    "protocol_version": PROTOCOL_VERSION,
    "fields": {
        "job_id": "string — identity of the coalesced refresh job; "
                  "requests merged into a pending job return that id",
        "merged_with": "job_id | null — the pending job this request "
                       "merged into",
        "projects": "accepted project keys",
        "rejected": [{"project": "…",
                      "reason": "no snapshot source | unsupported schema "
                                "| unreadable"}],
    },
    "errors": {
        "unsupported_protocol": "protocol_version mismatch: the engine "
                                "reports the version problem and never "
                                "force-takes-over",
        "unknown_op": f"op not {REFRESH_OP!r}",
    },
}


def refresh_contract() -> dict:
    """The frozen refresh.request contract (design §7). The engine layer
    executes it; this read layer only defines and validates it."""
    return {"request": deepcopy(_REFRESH_REQUEST_SPEC),
            "response": deepcopy(_REFRESH_RESPONSE_SPEC)}


def validate_refresh_request(payload: Any) -> dict:
    """Validate and normalize a refresh.request payload. Raises
    ValueError on an unknown op, an unsupported protocol version, or a
    malformed projects list — the engine reports these instead of
    guessing."""
    if not isinstance(payload, dict):
        raise ValueError("refresh request must be an object")
    if payload.get("op") != REFRESH_OP:
        raise ValueError(f"unknown op {payload.get('op')!r}; expected "
                         f"{REFRESH_OP!r}")
    version = payload.get("protocol_version", PROTOCOL_VERSION)
    if version != PROTOCOL_VERSION:
        raise ValueError(f"unsupported_protocol: request version {version}"
                         f" != {PROTOCOL_VERSION}")
    projects = payload.get("projects")
    if projects is not None and (
            not isinstance(projects, list)
            or not all(isinstance(p, str) and p for p in projects)):
        raise ValueError("projects must be null or an array of non-empty "
                         "project keys")
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be a string")
    return {"op": REFRESH_OP, "protocol_version": PROTOCOL_VERSION,
            "projects": projects, "reason": reason}
