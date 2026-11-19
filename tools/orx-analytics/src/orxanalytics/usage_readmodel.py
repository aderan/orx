"""Task/Run usage attribution read model (design §5) — strictly read-only.

This is the menu-bar app's caliber for "what did this task / this run
cost", beside the frozen six readings in :mod:`orxanalytics.metrics`
(the frozen contract lives in docs/metrics.md; the attribution section
added by G002 T002 also lives there). It only ever SELECTs from the
analytics store, never prorates an unattributable amount, never zeroes
an unknown, and keeps two calibers structurally separate:

- ``orx_recorded`` — sums of canonical ``orx_usage_obs`` rows only
  (one row per attempt by source priority native_cli > zcode_db >
  host_report > output_estimate; sources are alternates describing the
  same work, never addends);
- ``native_session_preview`` — cumulative numbers read from the imported
  native session mirrors. Its token numbers only ever live inside named
  sessions/entries; no accessor returns a number that mixes the two
  calibers, and no combined total exists anywhere.

Books: worker (task consumption, failed and retried attempts included),
verifier (counted toward the task, listed separately), planner (run
planning overhead, attributed via ``attempts.run_id`` so a NULL revision
still lands in the run), controller (separate; session-derived usage
appears only in the preview caliber with attribution evidence), and
unattributed (a named bucket — never spread over tasks or time).
"""

from __future__ import annotations

from typing import Any

from . import correlate
from .metrics import _canonical_observations as _canon
from .store import Store

CALIBER_ORX = "orx_recorded"
CALIBER_NATIVE = "native_session_preview"

# Derived "标准化处理量" (normalized processing volume), a separate and
# VERSIONED metric: raw token sums keep source semantics untouched; this
# derivation only compares work volume. Not billing. Semantics come from
# the ORX observability contract (cache subset vs cache separate).
NORMALIZED_PROCESSING_VERSION = 1
CACHE_SEMANTICS_BY_HARNESS = {
    "zcode": "cache_subset",    # reported input already contains the
    "codex": "cache_subset",    # cache-read prefix: input + output only
    "cursor": "cache_separate", # disjoint buckets: input + cached + output
}

_RULE_TEXT = {
    "task": "task consumption = every attributable attempt of "
            "(run_id, revision_id, task_id); worker attempts include "
            "failed and retried rounds; verifier attempts that are "
            "clearly linked count toward the task and are listed "
            "separately; planner stays run overhead",
    "run": "run attribution via attempts.run_id ONLY (a NULL revision "
           "planner attempt still belongs to the run); the run total "
           "covers every revision, so a replan never drops superseded "
           "consumption; unattributable usage keeps its own book and is "
           "never prorated across tasks or time",
    "unattributed": "usage with no attempt linkage or unknown run "
                    "association stays here; it is never distributed "
                    "over tasks, attempts, or time",
}


def _snap(store: Store, project_key: str,
          snapshot_id: int | None = None) -> int:
    """Resolve the snapshot to read. An explicit ``snapshot_id`` (used by
    the §7 query layer to pin related queries to one successful snapshot)
    is verified to belong to the project; without one the latest snapshot
    is used, as before."""
    if snapshot_id is not None:
        row = store.conn.execute(
            "SELECT project_key FROM orx_snapshots WHERE snapshot_id=?",
            (snapshot_id,)).fetchone()
        if row is None or row["project_key"] != project_key:
            raise ValueError(
                f"snapshot {snapshot_id} does not belong to {project_key}")
        return snapshot_id
    snap = correlate.latest_snapshot(store, project_key)
    if snap is None:
        raise SystemExit(f"no ORX snapshot for {project_key}; run collect")
    return snap


def _as_of(store: Store, snap: int) -> str | None:
    r = store.conn.execute(
        "SELECT taken_at FROM orx_snapshots WHERE snapshot_id=?",
        (snap,)).fetchone()
    return r["taken_at"] if r else None


def _canon_rows(store: Store, snap: int, att_ids: list[int]) -> dict:
    if not att_ids:
        return {}
    return _canon(store, snap, att_ids)


def _field_sums(rows: list[dict]) -> dict[str, int | None]:
    """Partial sums over non-NULL values. All values NULL (or no rows)
    means completely unknown -> None; a stored zero sums to 0."""
    out: dict[str, int | None] = {}
    for fld in ("input_tokens", "output_tokens", "cached_input"):
        vals = [r[fld] for r in rows if r[fld] is not None]
        out[fld] = sum(vals) if vals else None
    return out


def _missing_fields(rows: list[dict]) -> dict[str, int]:
    return {fld: sum(1 for r in rows if r[fld] is None)
            for fld in ("input_tokens", "output_tokens", "cached_input")}


def _accuracy(rows: list[dict]) -> dict[str, int]:
    out = {"exact": 0, "estimated": 0, "unknown": 0}
    for r in rows:
        label = r["accuracy"]
        out[label if label in ("exact", "estimated") else "unknown"] += 1
    return out


def _normalized(rows: list[dict], harness_of: dict[int, Any]) -> dict:
    """Versioned derived volume metric. Per canonical row:
    cache_subset -> input + output; cache_separate ->
    input + cached + output (a NULL cached contributes an explicit
    partial sum); unknown semantics or a missing operand -> nothing.
    The value is a partial sum whenever rows_missing > 0."""
    counts = {"cache_subset": 0, "cache_separate": 0, "unknown": 0}
    derived: list[int] = []
    missing = 0
    partial_row = False
    for r in rows:
        sem = CACHE_SEMANTICS_BY_HARNESS.get(harness_of.get(r["attempt_id"])
                                             or "")
        counts[sem or "unknown"] += 1
        inp, outp, cached = (r["input_tokens"], r["output_tokens"],
                             r["cached_input"])
        if sem == "cache_subset":
            if inp is None or outp is None:
                missing += 1
                continue
            derived.append(inp + outp)
        elif sem == "cache_separate":
            if inp is None or outp is None:
                missing += 1
                continue
            if cached is None:
                partial_row = True
            derived.append(inp + (cached or 0) + outp)
        else:
            missing += 1
    return {
        "version": NORMALIZED_PROCESSING_VERSION,
        "value": sum(derived) if derived else None,
        "partial": bool(missing or partial_row),
        "rows_derived": len(derived),
        "rows_missing": missing,
        "cache_semantics": counts,
        "note": "separate versioned derived volume for comparing work; "
                "raw token sums keep source semantics and stay "
                "authoritative; not billing",
    }


def _book(atts: list[dict], canon: dict, as_of: str | None,
          harness_of: dict[int, Any] | None = None) -> dict[str, Any]:
    """One ledger: which attempts, coverage (observed/attempts plus the
    contract's miss split), partial token sums, missing-field counts,
    accuracy distribution, as-of stamp."""
    rows = [canon[a["id"]] for a in atts if a["id"] in canon]
    observed = sum(1 for a in atts if a["id"] in canon)
    missing_recorded = sum(1 for a in atts
                           if a["id"] not in canon
                           and a["usage_missing_reason"])
    return {
        "attempts": len(atts),
        "attempt_ids": sorted(a["id"] for a in atts),
        "revision_ids": sorted({a["revision_id"] for a in atts},
                               key=lambda v: (v is None, v)),
        "coverage": {"observed": observed, "attempts": len(atts),
                     "missing_recorded": missing_recorded,
                     "unassessed": len(atts) - observed - missing_recorded},
        "tokens": _field_sums(rows),
        "missing_fields": _missing_fields(rows),
        "accuracy": _accuracy(rows),
        "as_of": as_of,
    }


def _attempts(store: Store, snap: int, where: str,
              args: tuple) -> list[dict]:
    return [dict(r) for r in store.conn.execute(
        f"SELECT * FROM orx_attempts WHERE snapshot_id=? AND {where} "
        f"ORDER BY id", (snap, *args))]


def _observation(u: dict | None) -> dict | None:
    if u is None:
        return None
    return {"source": u["source"], "accuracy": u["accuracy"],
            "tokens": {"input_tokens": u["input_tokens"],
                       "output_tokens": u["output_tokens"],
                       "cached_input": u["cached_input"]}}


def _attempt_details(atts: list[dict], canon: dict, links: list) -> list:
    by_att = {l.attempt_id: l for l in links}
    out = []
    for a in atts:
        d = {"attempt_id": a["id"], "role": a["role"],
             "revision_id": a["revision_id"], "task_id": a["task_id"],
             "result": a["result"], "failure_reason": a["failure_reason"],
             "started_at": a["started_at"], "ended_at": a["ended_at"],
             "harness": a["harness"],
             "model": a["actual_model"] or a["model"],
             "usage_missing_reason": a["usage_missing_reason"],
             "observation": _observation(canon.get(a["id"]))}
        link = by_att.get(a["id"])
        if link is not None:
            d["session_link"] = {
                "session_id": link.session_id,
                "authority": link.authority,
                "confidence": link.confidence}
        out.append(d)
    return out


def task_usage(store: Store, project_key: str, run_id: str,
               revision_id: int, task_id: str,
               snapshot_id: int | None = None) -> dict[str, Any]:
    """ORX-recorded consumption of one task (design §5). Books by role:
    worker (failed and retried attempts included) and verifier (counted,
    listed separately); any other task-linked role is reported under its
    own name and excluded from the task total until a contract names it.
    """
    snap = _snap(store, project_key, snapshot_id)
    as_of = _as_of(store, snap)
    atts = _attempts(store, snap, "run_id=? AND revision_id=? AND task_id=?",
                     (run_id, revision_id, task_id))
    canon = _canon_rows(store, snap, [a["id"] for a in atts])
    harness = {a["id"]: a["harness"] for a in atts}
    worker = [a for a in atts if a["role"] == "worker"]
    verifier = [a for a in atts if a["role"] == "verifier"]
    other = [a for a in atts
             if a["role"] not in ("worker", "verifier", "planner")]
    attributed = worker + verifier
    rows = [canon[a["id"]] for a in attributed if a["id"] in canon]
    links = correlate.links_for_snapshot(store, project_key, snap)
    books = {
        "worker": _book(worker, canon, as_of, harness),
        "verifier": _book(verifier, canon, as_of, harness),
    }
    if other:
        books["other_roles"] = {**_book(other, canon, as_of, harness),
                                "note": "excluded from the task total: no "
                                        "contract attributes this role to "
                                        "a task"}
    observed = sum(1 for a in atts if a["id"] in canon)
    missing_recorded = sum(1 for a in atts
                           if a["id"] not in canon and a["usage_missing_reason"])
    return {
        "caliber": CALIBER_ORX,
        "project": project_key, "run_id": run_id,
        "revision_id": revision_id, "task_id": task_id,
        "snapshot_id": snap, "as_of": as_of,
        "tokens": _field_sums(rows),
        "missing_fields": _missing_fields(rows),
        "accuracy": _accuracy(rows),
        "coverage": {"observed": observed, "attempts": len(atts),
                     "missing_recorded": missing_recorded,
                     "unassessed": len(atts) - observed - missing_recorded},
        "books": books,
        "normalized_processing": _normalized(rows, harness),
        "attempts": _attempt_details(atts, canon, links),
        "attribution": {"rule": _RULE_TEXT["task"]},
    }


def run_usage(store: Store, project_key: str, run_id: str,
              snapshot_id: int | None = None) -> dict[str, Any]:
    """ORX-recorded consumption of one run over ALL revisions (design
    §5). Attributed total = task books (worker + verifier) + planner
    (+ ORX-recorded controller attempts, should they ever exist).
    Unattributable-but-in-run attempts keep their own book, outside the
    total, never prorated.
    """
    snap = _snap(store, project_key, snapshot_id)
    as_of = _as_of(store, snap)
    atts = _attempts(store, snap, "run_id=?", (run_id,))
    canon = _canon_rows(store, snap, [a["id"] for a in atts])
    harness = {a["id"]: a["harness"] for a in atts}

    planner = [a for a in atts if a["role"] == "planner"]
    controller = [a for a in atts if a["role"] == "controller"]
    named = {a["id"] for a in planner + controller}
    task_atts = [a for a in atts
                 if a["id"] not in named
                 and a["revision_id"] is not None
                 and a["task_id"] is not None]
    unatt = [a for a in atts
             if a["id"] not in named and a["id"] not in
             {t["id"] for t in task_atts}]
    attributed = planner + controller + task_atts
    rows = [canon[a["id"]] for a in attributed if a["id"] in canon]

    rev_numbers = {r["id"]: r["revision"] for r in store.conn.execute(
        "SELECT id, revision FROM orx_revisions WHERE snapshot_id=?",
        (snap,))}
    per_task: dict[tuple[int, str], list[dict]] = {}
    for a in task_atts:
        per_task.setdefault((a["revision_id"], a["task_id"]), []).append(a)
    task_entries = []
    for (rev_id, t_id), group in sorted(per_task.items()):
        entry = _book(group, canon, as_of, harness)
        entry["revision_id"] = rev_id
        entry["revision"] = rev_numbers.get(rev_id)
        entry["task_id"] = t_id
        task_entries.append(entry)
    per_revision: dict[int, list[dict]] = {}
    for a in task_atts:
        per_revision.setdefault(a["revision_id"], []).append(a)
    revision_entries = []
    for rev_id, group in sorted(per_revision.items()):
        entry = _book(group, canon, as_of, harness)
        entry["revision_id"] = rev_id
        entry["revision"] = rev_numbers.get(rev_id)
        revision_entries.append(entry)

    observed = sum(1 for a in atts if a["id"] in canon)
    missing_recorded = sum(1 for a in atts
                           if a["id"] not in canon and a["usage_missing_reason"])
    return {
        "caliber": CALIBER_ORX,
        "project": project_key, "run_id": run_id,
        "revision_scope": "all revisions via attempts.run_id",
        "snapshot_id": snap, "as_of": as_of,
        "tokens": _field_sums(rows),
        "missing_fields": _missing_fields(rows),
        "accuracy": _accuracy(rows),
        "coverage": {"observed": observed, "attempts": len(atts),
                     "missing_recorded": missing_recorded,
                     "unassessed": len(atts) - observed - missing_recorded},
        "books": {
            "tasks": _book(task_atts, canon, as_of, harness),
            "planner": _book(planner, canon, as_of, harness),
            "controller": {**_book(controller, canon, as_of, harness),
                           "note": "ORX-recorded controller attempts only; "
                                   "session-derived controller usage lives "
                                   "in the native preview caliber"},
            "unattributed_within_run": {
                **_book(unatt, canon, as_of, harness),
                "note": "run attributed but not attributable to any task "
                        "or named role; never prorated"},
        },
        "per_task": task_entries,
        "per_revision": revision_entries,
        "normalized_processing": _normalized(rows, harness),
        "attribution": {"rule": _RULE_TEXT["run"]},
    }


def project_unattributed(store: Store, project_key: str,
                         snapshot_id: int | None = None) -> dict[str, Any]:
    """Usage that cannot be attributed to any run or attempt: attempts
    with unknown run association, and observations with no attempt
    linkage. A named ledger — never distributed over tasks or time."""
    snap = _snap(store, project_key, snapshot_id)
    as_of = _as_of(store, snap)
    atts = _attempts(store, snap, "run_id IS NULL", ())
    canon = _canon_rows(store, snap, [a["id"] for a in atts])
    harness = {a["id"]: a["harness"] for a in atts}
    orphans = [dict(r) for r in store.conn.execute(
        "SELECT * FROM orx_usage_obs WHERE snapshot_id=? "
        "AND attempt_id IS NULL ORDER BY id", (snap,))]
    rows = list(canon.values()) + orphans
    return {
        "caliber": CALIBER_ORX,
        "project": project_key, "snapshot_id": snap, "as_of": as_of,
        "tokens": _field_sums(rows),
        "missing_fields": _missing_fields(rows),
        "accuracy": _accuracy(rows),
        "books": {
            "attempts_unknown_run": _book(atts, canon, as_of, harness),
            "observations_without_attempt": {
                "rows": len(orphans),
                "row_ids": [o["id"] for o in orphans],
                "tokens": _field_sums(orphans),
                "missing_fields": _missing_fields(orphans),
                "accuracy": _accuracy(orphans),
                "as_of": as_of},
        },
        "note": "never prorated across tasks, attempts, or time; a short "
                "task id is not an identity and never attaches these rows",
        "attribution": {"rule": _RULE_TEXT["unattributed"]},
    }


# ------------------------------------------------- native session preview ---

# G012 (用户反馈 5): statuses of a zcode model_usage request row that carry
# FINAL numbers — the same vocabulary ORX's own read-only reader uses
# (src/orx/zcode_sessions.py, read from the real database 2026-10-16). A row
# with any other status (a request still streaming) is counted in
# ``settling_request_rows``; its numbers make the preview's cumulative sums
# provisional, which is stated, never hidden.
TERMINAL_REQUEST_STATUSES = frozenset({"completed", "error", "cancelled"})

_REQUEST_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens",
                   "cache_read", "cache_creation", "provider_total",
                   "computed_total")


def request_totals(store: Store, session_ids: list[str]) -> dict[str, Any]:
    """model_usage request caliber over the ``zc_requests`` mirror.

    One request is ``(session_id, logical_request_id, attempt_index)`` —
    the mirror's natural primary key, so every request is counted exactly
    once however often it was imported (repeated collection never double
    counts). zcode cache semantics: ``cache_read`` is a SUBSET of input, so
    the derived ``processed_tokens`` is input + output and never adds cache
    on top; ``turn_usage`` is a different granularity and is never summed
    into this caliber. An error-status row whose token fields are explicit
    zeros is a placeholder, not a free request: counted as a request and
    flagged in ``error_rows_zero_tokens``. NULL fields stay NULL — partial
    sums with per-field missing counts, never zero-filled.
    """
    out: dict[str, Any] = {
        "caliber": "model_usage_requests",
        "source": "zcode model_usage mirror (zc_requests; read-only import)",
        "session_ids": sorted(set(session_ids)),
        "requests": 0,
        "status_counts": {},
        "settling_request_rows": 0,
        "error_rows_zero_tokens": 0,
        "processed_rule": "processed = input + output; cache_read is a "
                          "subset of input and is never added on top; "
                          "turn_usage is a different caliber and is never "
                          "stacked",
        "last_activity_ms": None,
    }
    for f in _REQUEST_FIELDS:
        out[f] = None
    out["processed_tokens"] = None
    out["missing_fields"] = {f: 0 for f in _REQUEST_FIELDS}
    out["rows_missing_processed"] = 0
    ids = sorted(set(session_ids))
    if not ids:
        return out
    qmarks = ",".join("?" * len(ids))
    rows = store.conn.execute(
        f"SELECT status, started_ms, completed_ms, "
        f"{', '.join(_REQUEST_FIELDS)} FROM zc_requests "
        f"WHERE session_id IN ({qmarks})", ids).fetchall()
    sums: dict[str, int] = {f: 0 for f in _REQUEST_FIELDS}
    seen_any: dict[str, bool] = {f: False for f in _REQUEST_FIELDS}
    processed: list[int] = []
    last_ms: list[int] = []
    for r in rows:
        out["requests"] += 1
        sc = r["status"] or "unknown"
        out["status_counts"][sc] = out["status_counts"].get(sc, 0) + 1
        if sc not in TERMINAL_REQUEST_STATUSES:
            out["settling_request_rows"] += 1
        if sc == "error" and r["input_tokens"] == 0 \
                and r["output_tokens"] == 0:
            out["error_rows_zero_tokens"] += 1
        for f in _REQUEST_FIELDS:
            v = r[f]
            if v is None:
                out["missing_fields"][f] += 1
            else:
                sums[f] += v
                seen_any[f] = True
        if r["input_tokens"] is not None and r["output_tokens"] is not None:
            processed.append(r["input_tokens"] + r["output_tokens"])
        else:
            out["rows_missing_processed"] += 1
        for t in (r["completed_ms"], r["started_ms"]):
            if t is not None:
                last_ms.append(t)
    for f in _REQUEST_FIELDS:
        if seen_any[f]:
            out[f] = sums[f]
    if processed:
        out["processed_tokens"] = sum(processed)
    if last_ms:
        out["last_activity_ms"] = max(last_ms)
    return out


def _directory_matches(directory: str | None, project_key: str) -> bool:
    """Exact or symlink-resolved equality — both spellings of the same
    directory match; a different directory never does (ORX G009 semantics,
    shared with src/orx/zcode_sessions.py)."""
    if directory is None:
        return False
    if directory == project_key:
        return True
    import os
    try:
        return os.path.realpath(directory) == os.path.realpath(project_key)
    except OSError:                                  # pragma: no cover
        return False


def _root_ancestor(sessions: dict[str, dict], sid: str) -> str | None:
    """Follow session.parent_id up to the root session. Cycle-safe; a
    missing row anywhere on the chain means no root can be proven."""
    seen: set[str] = set()
    cur: str | None = sid
    while cur is not None and cur not in seen and cur in sessions:
        seen.add(cur)
        nxt = sessions[cur]["parent_id"]
        if nxt is None:
            return cur
        cur = nxt
    return None


def _turn_totals(store: Store, session_ids: list[str]) -> dict[str, Any]:
    if not session_ids:
        return {"turns": 0, "input_tokens": None, "output_tokens": None,
                "reasoning_tokens": None, "cache_read": None,
                "cache_creation": None}
    qmarks = ",".join("?" * len(session_ids))
    r = store.conn.execute(
        f"SELECT COUNT(*) turns, SUM(input_tokens) i, SUM(output_tokens) o, "
        f"SUM(reasoning_tokens) rr, SUM(cache_read) cr, "
        f"SUM(cache_creation) cc FROM zc_turns WHERE session_id IN ({qmarks})",
        session_ids).fetchone()
    return {"turns": r["turns"], "input_tokens": r["i"],
            "output_tokens": r["o"], "reasoning_tokens": r["rr"],
            "cache_read": r["cr"], "cache_creation": r["cc"]}


def native_session_preview(store: Store, project_key: str,
                           run_id: str,
                           snapshot_id: int | None = None) -> dict[str, Any]:
    """The native caliber beside ORX-recorded usage: cumulative numbers
    of the sessions linked to this run's attempts (design §5). Parent and
    child sessions never double count (trees dedupe by id; the union
    counts every session exactly once); overlapping subtrees linked to
    different attempts are flagged as conflicts instead of summed
    blindly. Structurally separate from the ORX caliber: this result
    carries NO top-level token numbers, only per-session entries, and no
    accessor merges the two calibers.
    """
    snap = _snap(store, project_key, snapshot_id)
    atts = _attempts(store, snap, "run_id=?", (run_id,))
    att_by_id = {a["id"]: a for a in atts}
    links = [l for l in correlate.links_for_snapshot(store, project_key,
                                                     snap)
             if l.attempt_id in att_by_id]
    sessions = {r["session_id"]: dict(r) for r in store.conn.execute(
        "SELECT session_id, parent_id, directory, created_ms, updated_ms "
        "FROM zc_sessions")}

    entries = []
    trees: dict[int, frozenset[str]] = {}
    union: set[str] = set()
    for l in links:
        a = att_by_id[l.attempt_id]
        entry: dict[str, Any] = {
            "attempt_id": l.attempt_id, "role": a["role"],
            "revision_id": a["revision_id"], "task_id": a["task_id"],
            "harness": a["harness"], "authority": l.authority,
            "confidence": l.confidence}
        if l.confidence == "ambiguous":
            entry["candidates"] = l.session_id.split(",")
            entries.append(entry)
            continue
        if a["harness"] == "codex":
            entry["thread_id"] = l.session_id
            entry["thread_totals"] = correlate.codex_thread_totals(
                store, l.session_id)
        elif a["harness"] == "zcode":
            entry["session_id"] = l.session_id
            entry["tree_totals"] = correlate.zcode_tree_totals(
                store, l.session_id)
            trees[l.attempt_id] = frozenset(
                correlate.zcode_tree(store, l.session_id))
            union.update(trees[l.attempt_id])
        else:
            entry["session_id"] = l.session_id
            entry["note"] = ("no preview reader for this harness; "
                             "link reported only")
        entries.append(entry)

    # One session tree carrying several attempts is the documented
    # multi-attempt session case: not an error, but per-attempt tree
    # totals must never be summed over it (the union counts it once;
    # window splitting may attribute inside it). DISTINCT trees that
    # intersect (an ancestor linked to one attempt, its descendant to
    # another) are genuine attribution conflicts.
    by_tree: dict[frozenset[str], list[int]] = {}
    for aid, tree in trees.items():
        by_tree.setdefault(tree, []).append(aid)
    shared = [{"session_ids": sorted(tree), "attempt_ids": sorted(aids)}
              for tree, aids in sorted(
                  by_tree.items(), key=lambda kv: sorted(kv[1]))
              if len(aids) > 1]
    distinct = sorted(by_tree.items(), key=lambda kv: sorted(kv[1]))
    conflicts = []
    for i, (tree_a, aids_a) in enumerate(distinct):
        for tree_b, aids_b in distinct[i + 1:]:
            inter = sorted(tree_a & tree_b)
            if inter:
                conflicts.append({
                    "session_ids": inter,
                    "attempt_ids": sorted(set(aids_a) | set(aids_b))})

    # controller sessions (G012 用户反馈 5): the ROOT ancestor (parent_id
    # chain, however deep) of sessions linked to attempts — not linked to
    # any attempt itself. OWN usage only, in the model_usage request
    # caliber: worker/verifier descendants belong to their attempts' books
    # and must never be stacked on top. The anchors that tie children to
    # attempts (stored session_ref / ORX v11 ORX_ASSIGNMENT nonce via
    # correlate) are the ONLY attribution evidence; the session's directory
    # is recorded as a fact (alias-resolved), never used to guess, and a
    # controller whose children span several runs stays unattributed with
    # the reason stated.
    all_atts = {r["id"]: dict(r) for r in store.conn.execute(
        "SELECT id, run_id, role, session_ref, nonce FROM orx_attempts "
        "WHERE snapshot_id=?", (snap,))}
    linked_ids = {l.session_id for l in links if l.confidence != "ambiguous"}
    all_links = [l for l in correlate.links_for_snapshot(store,
                                                         project_key, snap)
                 if l.confidence != "ambiguous"]
    roots: dict[str, list] = {}
    for l in all_links:
        root = _root_ancestor(sessions, l.session_id)
        if root is None or root == l.session_id:
            continue        # the linked session IS a root: an attempt book
        if root in linked_ids:
            continue        # root itself is linked: nested attempt session
        roots.setdefault(root, []).append(l)
    controllers = []
    for root_sid, children in sorted(roots.items()):
        ps = sessions.get(root_sid)
        if ps is None:
            continue
        runs: set = set()
        evidence = []
        for l in children:
            a = all_atts.get(l.attempt_id, {})
            runs.add(a.get("run_id"))
            evidence.append({
                "session_id": l.session_id, "attempt_id": l.attempt_id,
                "role": a.get("role"), "authority": l.authority,
                "confidence": l.confidence,
                "nonce": l.evidence.get("nonce") or a.get("nonce")})
        known = {r for r in runs if r is not None}
        attributed = len(known) == 1 and len(runs) == len(known)
        if attributed:
            reason = None
        elif len(known) > 1:
            reason = (f"child sessions link to attempts of {len(known)} "
                      f"different runs {sorted(known)}; controller usage "
                      f"cannot be attributed to one run and is not split "
                      f"by guesswork")
        else:
            reason = ("a child attempt has no run_id (run association "
                      "unknown); controller usage stays unattributed")
        controllers.append({
            "session_id": root_sid,
            "directory": ps["directory"],
            "directory_matches_project": _directory_matches(
                ps["directory"], project_key),
            "created_ms": ps["created_ms"], "updated_ms": ps["updated_ms"],
            "own_usage": request_totals(store, [root_sid]),
            "run_attribution": {
                "attributed": attributed,
                "runs": sorted(known),
                "reason": reason,
                "basis": "root ancestor (session.parent_id chain, any "
                         "depth) of subagent sessions whose first prompts "
                         "carry this project's attempt anchors (stored "
                         "attempts.session_ref or the ORX v11 "
                         "ORX_ASSIGNMENT nonce); own usage only — worker/"
                         "verifier descendants belong to their attempts' "
                         "books; directory recorded as a fact (alias-"
                         "resolved), never used to guess",
                "child_evidence": evidence}})

    # per-run verdict for the detail view: which controller session(s) this
    # run can claim, or the honest reason it cannot. No token numbers live
    # at this level (the preview top level carries none by contract).
    run_zcode_atts = [a for a in atts if a["harness"] == "zcode"]
    linked_run_atts = {l.attempt_id for l in links
                       if l.confidence != "ambiguous"}
    attributed_here = [c for c in controllers
                       if c["run_attribution"]["attributed"]
                       and c["run_attribution"]["runs"] == [run_id]]
    mentions_here = [c for c in controllers
                     if run_id in c["run_attribution"]["runs"]]
    if attributed_here:
        controller_for_run = {
            "run_id": run_id, "status": "attributed",
            "session_ids": [c["session_id"] for c in attributed_here],
            "reason": None,
            "note": "controller own usage (model_usage request caliber) "
                    "for this run; see controller_sessions entries"}
    elif mentions_here:
        controller_for_run = {
            "run_id": run_id, "status": "unattributed",
            "session_ids": [c["session_id"] for c in mentions_here],
            "reason": mentions_here[0]["run_attribution"]["reason"],
            "note": "controller session evidence exists but is not unique "
                    "to this run; usage stays unattributed, never prorated"}
    elif not run_zcode_atts:
        controller_for_run = {
            "run_id": run_id, "status": "no_zcode_attempts",
            "session_ids": [],
            "reason": "this run has no zcode attempts; the controller "
                      "session caliber only exists for zcode-driven runs",
            "note": None}
    elif not any(a["id"] in linked_run_atts for a in run_zcode_atts):
        controller_for_run = {
            "run_id": run_id, "status": "host_session_unbound",
            "session_ids": [],
            "reason": "none of this run's zcode attempts is bound to a "
                      "session (ORX records usage_missing_reason "
                      "'host_session_unbound' for such attempts); the "
                      "controller session cannot be identified",
            "note": None}
    else:
        missing_mirror = sorted({l.session_id for l in links
                                 if l.confidence != "ambiguous"
                                 and l.session_id not in sessions})
        if missing_mirror:
            controller_for_run = {
                "run_id": run_id, "status": "session_not_imported",
                "session_ids": [],
                "reason": "this run has linked session id(s) "
                          f"{missing_mirror} that the zcode session mirror "
                          f"does not carry (source not imported or session "
                          f"unknown); controller usage stays unattributed",
                "note": None}
        else:
            controller_for_run = {
                "run_id": run_id, "status": "no_controller_session",
                "session_ids": [],
                "reason": "linked sessions exist but their root ancestor "
                          "is itself linked to an attempt; no separate "
                          "controller session was found",
                "note": None}

    updated = [s["updated_ms"] for s in sessions.values()
               if s["updated_ms"] is not None]
    return {
        "caliber": CALIBER_NATIVE,
        "project": project_key, "run_id": run_id, "snapshot_id": snap,
        "as_of": _as_of(store, snap),
        "as_of_sessions_ms": max(updated) if updated else None,
        "linked_attempts": entries,
        "union": {"sessions": sorted(union),
                  "tokens": _turn_totals(store, sorted(union))},
        "shared_session_trees": shared,
        "subtree_conflicts": conflicts,
        "controller_sessions": controllers,
        "controller_for_run": controller_for_run,
        "never_sum_with": CALIBER_ORX,
        "note": "cumulative native-session numbers, a separate caliber "
                "from orx_recorded; never added to it and no combined "
                "total exists; unsplit multi-attempt sessions keep their "
                "totals with an explicit refusal reason; controller own "
                "usage is the model_usage request caliber (deduped by "
                "session_id + logical_request_id + attempt_index)",
    }
