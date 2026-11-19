"""The corrected readings (docs/metrics.md is the frozen contract).

Differences from the legacy script this package replaces:
- run attribution goes through ``attempts.run_id`` (planner attempts with a
  NULL revision are counted), never ``revision_id IN (...)`` alone;
- first-pass uses the earliest verification row (created_at, then id) plus
  the no-failed-events rule, and excludes tasks that never reached
  verification;
- consumption collapses to the canonical observation per attempt — best
  source first (native_cli > zcode_db > host_report > output_estimate),
  lowest id within a source — before summing, and publishes missing-field
  counts;
- spans and waiting are separate; session trees dedupe by id; unknown
  values stay unknown.

Mirror-table joins always carry ``snapshot_id=?`` on BOTH sides: source
row ids (revisions, tasks) repeat across snapshots.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import correlate
from .store import Store

# contract order (ORX docs/observability-contract.md): native_cli >
# zcode_db > host_report > output_estimate; unknown sources sort last
_RANK = {"native_cli": 0, "zcode_db": 1, "host_report": 2,
         "output_estimate": 3}


def _snapshot(store: Store, project_key: str) -> int:
    snap = correlate.latest_snapshot(store, project_key)
    if snap is None:
        raise SystemExit(f"no ORX snapshot for {project_key}; run collect")
    return snap


def _sec(iso_a: str, iso_b: str) -> int | None:
    if not iso_a or not iso_b:
        return None
    a = datetime.fromisoformat(iso_a)
    b = datetime.fromisoformat(iso_b)
    return int((b - a).total_seconds())


def attempts_for_run(store: Store, snap: int, run_id: str) -> list[dict]:
    """Every attempt of the run: run attribution by attempts.run_id only."""
    return [dict(r) for r in store.conn.execute(
        "SELECT * FROM orx_attempts WHERE snapshot_id=? AND run_id=? "
        "ORDER BY id", (snap, run_id))]


def _canonical_observations(store: Store, snap: int,
                            att_ids: list[int]) -> dict[int, dict]:
    """Best observation per attempt: source rank, then lowest id."""
    qmarks = ",".join("?" * len(att_ids))
    rows = [dict(r) for r in store.conn.execute(
        f"SELECT * FROM orx_usage_obs WHERE snapshot_id=? AND attempt_id "
        f"IN ({qmarks})", [snap, *att_ids])]
    best: dict[int, dict] = {}
    for u in rows:
        cur = best.get(u["attempt_id"])
        key = (_RANK.get(u["source"], 9), u["id"])
        if cur is None or key < (_RANK.get(cur["source"], 9), cur["id"]):
            best[u["attempt_id"]] = u
    return best


def consumption(store: Store, snap: int, run_id: str) -> dict[str, Any]:
    atts = attempts_for_run(store, snap, run_id)
    out: dict[str, Any] = {
        "attempts": len(atts),
        "attempts_with_observation": 0,
        "input_tokens_sum": None, "output_tokens_sum": None,
        "input_missing": 0, "output_missing": 0, "cached_missing": 0,
        "accuracy": {"exact": 0, "estimated": 0, "unknown": 0},
        "per_profile": {},
        "fee": "not computed (no billing basis)",
    }
    for a in atts:
        out["per_profile"].setdefault(a["profile"], {
            "attempts": 0, "observations": 0, "input_tokens": 0,
            "output_tokens": 0})
        out["per_profile"][a["profile"]]["attempts"] += 1
    if not atts:
        out["note"] = "no attempts attributed to this run via attempts.run_id"
        return out
    canon = _canonical_observations(store, snap, [a["id"] for a in atts])
    out["attempts_with_observation"] = len(canon)
    out["canonical_observations"] = len(canon)
    tot_i = tot_o = 0
    for aid, u in canon.items():
        att = next(a for a in atts if a["id"] == aid)
        p = out["per_profile"][att["profile"]]
        p["observations"] += 1
        for fld, miss in (("input_tokens", "input_missing"),
                          ("output_tokens", "output_missing"),
                          ("cached_input", "cached_missing")):
            if u[fld] is None:
                out[miss] += 1
        if u["input_tokens"] is not None:
            tot_i += u["input_tokens"]
            p["input_tokens"] += u["input_tokens"]
        if u["output_tokens"] is not None:
            tot_o += u["output_tokens"]
            p["output_tokens"] += u["output_tokens"]
        if u["accuracy"] in out["accuracy"]:
            out["accuracy"][u["accuracy"]] += 1
    if canon:
        out["input_tokens_sum"] = tot_i
        out["output_tokens_sum"] = tot_o
    else:
        out["note"] = ("no canonical observation: sums stay unknown, "
                       "not zero")
    out["attempts_without_observation"] = len(atts) - len(canon)
    out["partial"] = bool(
        out["input_missing"] or out["output_missing"]
        or out["attempts_without_observation"])
    return out


def first_pass_rate(store: Store, snap: int, run_id: str) -> dict[str, Any]:
    verifs = [dict(r) for r in store.conn.execute(
        "SELECT id, revision_id, task_id, passed, created_at "
        "FROM orx_verifications WHERE snapshot_id=? "
        "ORDER BY created_at, id", (snap,))]
    first: dict[tuple[int, str], dict] = {}
    for v in verifs:
        first.setdefault((v["revision_id"], v["task_id"]), v)
    failed: dict[tuple[int, str], int] = {}
    for r in store.conn.execute(
        "SELECT revision_id, task_id, COUNT(*) c FROM orx_task_events "
        "WHERE snapshot_id=? AND to_status='failed' "
        "GROUP BY revision_id, task_id", (snap,)):
        failed[(r["revision_id"], r["task_id"])] = r["c"]
    judged = numerator = 0
    per_task = []
    for (rev_row, task_id), v in first.items():
        rev = store.conn.execute(
            "SELECT run_id, revision FROM orx_revisions "
            "WHERE snapshot_id=? AND id=?", (snap, rev_row)).fetchone()
        if rev is None or rev["run_id"] != run_id:
            continue
        judged += 1
        no_fail = failed.get((rev_row, task_id), 0) == 0
        fp = 1 if (v["passed"] == 1 and no_fail) else 0
        numerator += fp
        per_task.append({"revision": rev["revision"], "task_id": task_id,
                         "revision_row_id": rev_row,
                         "earliest_verification_id": v["id"],
                         "first_pass": bool(fp)})
    total_tasks = store.conn.execute(
        "SELECT COUNT(*) c FROM orx_tasks t JOIN orx_revisions r "
        "ON r.snapshot_id=? AND r.id=t.revision_id AND r.run_id=? "
        "WHERE t.snapshot_id=?", (snap, run_id, snap)).fetchone()["c"]
    return {
        "numerator": numerator, "denominator": judged,
        "rate": (numerator / judged) if judged else None,
        "per_task": per_task,
        "tasks_never_verified": total_tasks - judged,
        "note": "denominator = tasks with >=1 verification row; tasks that "
                "never reached verification are outside the rate",
    }


def completion_time(store: Store, snap: int, run_id: str) -> dict[str, Any]:
    atts = attempts_for_run(store, snap, run_id)
    spans, incomplete = [], 0
    for a in atts:
        s = _sec(a["started_at"], a["ended_at"]) \
            if a["started_at"] and a["ended_at"] else None
        if s is None:
            incomplete += 1
        else:
            spans.append(s)
    if not atts:
        span_sum = 0
    elif not spans:
        span_sum = None        # every span incomplete: unknown, not zero
    else:
        span_sum = sum(spans)
    run = store.conn.execute(
        "SELECT started_at, completed_at FROM orx_runs WHERE snapshot_id=? "
        "AND id=?", (snap, run_id)).fetchone()
    return {
        "execution_span_sum_sec": span_sum,
        "spans_complete": len(spans),
        "spans_incomplete": incomplete,
        "note": "span sum adds overlapping intervals without merging; "
                "attempts with a missing stamp drop out and are counted "
                "in spans_incomplete",
        "waiting_sec": _sec(run["started_at"], run["completed_at"])
        if run and run["started_at"] and run["completed_at"] else None,
        "waiting_source": "runs.started_at..completed_at lifecycle stamps",
    }


def rework(store: Store, snap: int, run_id: str) -> dict[str, Any]:
    events = [dict(r) for r in store.conn.execute(
        "SELECT e.task_id, e.reason, e.created_at, r.revision "
        "FROM orx_task_events e JOIN orx_revisions r "
        "ON r.snapshot_id=? AND r.id=e.revision_id AND r.run_id=? "
        "WHERE e.snapshot_id=? AND e.to_status='failed' "
        "ORDER BY e.id", (snap, run_id, snap))]
    for e in events:
        reason = e["reason"] or ""
        if reason.startswith("delivery blocked"):
            e["failure_kind"] = "environment_or_tool"
        elif reason.startswith("delivery failed"):
            e["failure_kind"] = "code_failure_reported"
        else:
            e["failure_kind"] = "verification"
    rounds = [dict(r) for r in store.conn.execute(
        "SELECT v.attempt_id, v.command, COUNT(*) rows FROM "
        "orx_verifications v WHERE v.snapshot_id=? AND v.kind='command' "
        "AND v.attempt_id IS NOT NULL GROUP BY v.attempt_id, v.command "
        "HAVING rows > 1", (snap,))]
    retries = [dict(r) for r in store.conn.execute(
        "SELECT a.revision_id, a.task_id, COUNT(*) attempts FROM "
        "orx_attempts a WHERE a.snapshot_id=? AND a.role='worker' "
        "AND a.revision_id IS NOT NULL GROUP BY a.revision_id, a.task_id "
        "HAVING attempts > 1", (snap,))]
    redone = [dict(r) for r in store.conn.execute(
        "SELECT s.source_revision, s.source_task_id, t.task_id, "
        "t.classification, t.redo_reason FROM orx_replan_sources s "
        "JOIN orx_replan_task_maps t ON t.snapshot_id=? "
        "AND t.id=s.task_mapping_id WHERE s.snapshot_id=? "
        "AND t.classification='redo'", (snap, snap))]
    return {"failed_transitions": events,
            "same_attempt_check_loops": rounds,
            "cross_attempt_retries": retries,
            "declared_cross_revision_redo": redone,
            "note": "cross-revision waste claims require a declared "
                    "replan edge; the same task_id on two revisions is "
                    "not one task"}


def coverage(store: Store, snap: int, run_id: str,
             project_key: str) -> dict[str, Any]:
    atts = attempts_for_run(store, snap, run_id)
    by_role: dict[str, dict[str, int]] = {}
    att_with_obs = {r["attempt_id"] for r in store.conn.execute(
        "SELECT DISTINCT attempt_id FROM orx_usage_obs WHERE snapshot_id=?",
        (snap,))}
    for a in atts:
        d = by_role.setdefault(a["role"], {"attempts": 0, "observed": 0,
                                           "missing_recorded": 0,
                                           "unassessed": 0})
        d["attempts"] += 1
        if a["id"] in att_with_obs:
            d["observed"] += 1
        elif a["usage_missing_reason"]:
            d["missing_recorded"] += 1
        else:
            d["unassessed"] += 1
    fields = store.conn.execute(
        "SELECT COUNT(*) n, SUM(input_tokens IS NOT NULL) ip, "
        "SUM(output_tokens IS NOT NULL) op, "
        "SUM(cached_input IS NOT NULL) cp FROM orx_usage_obs "
        "WHERE snapshot_id=?", (snap,)).fetchone()
    links = correlate.links_for_snapshot(store, project_key, snap)
    linkmap = {l.attempt_id: l for l in links}
    session_cov = {"stored": 0, "inferred_high": 0, "ambiguous": 0,
                   "none": 0}
    for a in atts:
        if a["role"] != "worker":
            continue
        l = linkmap.get(a["id"])
        if l is None or l.confidence not in (None, "high", "ambiguous"):
            session_cov["none"] += 1
        elif l.authority == "stored":
            session_cov["stored"] += 1
        elif l.confidence == "high":
            session_cov["inferred_high"] += 1
        else:
            session_cov["ambiguous"] += 1
    return {
        "by_role": by_role,
        "field_coverage": {"observations": fields["n"],
                           "input_present": fields["ip"],
                           "output_present": fields["op"],
                           "cached_present": fields["cp"]},
        "worker_session_link": session_cov,
    }


def session_side(store: Store, snap: int, run_id: str,
                 project_key: str) -> dict[str, Any]:
    """Native-tool totals for the run's linked sessions, separate caliber."""
    att_ids = {a["id"] for a in attempts_for_run(store, snap, run_id)}
    links = [l for l in correlate.links_for_snapshot(store, project_key, snap)
             if l.attempt_id in att_ids]
    windows = correlate.attribute_session_windows(store, project_key, snap)
    out: list[dict[str, Any]] = []
    seen_trees: set[str] = set()
    union_sessions: set[str] = set()
    for l in links:
        att = store.conn.execute(
            "SELECT role, harness, task_id FROM orx_attempts "
            "WHERE snapshot_id=? AND id=?", (snap, l.attempt_id)).fetchone()
        if att is None:
            continue
        entry = {
            "attempt_id": l.attempt_id, "role": att["role"],
            "task_id": att["task_id"], "harness": att["harness"],
            "authority": l.authority, "confidence": l.confidence,
            "basis": l.basis,
        }
        if l.authority == "stored" and att["harness"] == "codex":
            entry.update(correlate.codex_thread_totals(store, l.session_id))
            out.append(entry)
            continue
        if att["harness"] != "zcode":
            continue
        if l.confidence == "ambiguous":
            entry["candidates"] = l.session_id.split(",")
            out.append(entry)
            continue
        entry["session_id"] = l.session_id
        entry["window_split"] = windows.get(l.attempt_id)
        if l.session_id in seen_trees:
            entry["tree_totals"] = ("same session tree as an earlier "
                                    "linked attempt; see that entry")
        else:
            seen_trees.add(l.session_id)
            entry["tree_totals"] = correlate.zcode_tree_totals(
                store, l.session_id)
        # union caliber: every session of every linked tree, once
        union_sessions.update(correlate.zcode_tree(store, l.session_id))
        out.append(entry)
    union = {"sessions": len(union_sessions)}
    if union_sessions:
        q = ",".join("?" * len(union_sessions))
        r = store.conn.execute(
            f"SELECT SUM(input_tokens) i, SUM(output_tokens) o FROM "
            f"zc_turns WHERE session_id IN ({q})", sorted(union_sessions)
        ).fetchone()
        union["input_tokens"] = r["i"]
        union["output_tokens"] = r["o"]
    else:
        union["input_tokens"] = None
        union["output_tokens"] = None
    return {
        "linked": out,
        "union_totals": union,
        "note": ("session trees are the native-tool caliber and are "
                 "reported beside ORX observations, never merged into "
                 "them; unsplit multi-attempt sessions carry their totals "
                 "with an explicit refusal reason; union_totals sums every "
                 "session of every linked tree exactly once"),
    }


def delivery_quality(store: Store, snap: int, run_id: str) -> dict[str, Any]:
    rows = [dict(r) for r in store.conn.execute(
        "SELECT r.revision, t.task_id, t.id task_row_id, t.status, "
        "t.failure_reason FROM orx_tasks t JOIN orx_revisions r "
        "ON r.snapshot_id=? AND r.id=t.revision_id AND r.run_id=? "
        "WHERE t.snapshot_id=? ORDER BY r.revision, t.id",
        (snap, run_id, snap))]
    passed = sum(1 for r in rows if r["status"] == "passed")
    terminal = sum(1 for r in rows
                   if r["status"] in ("passed", "failed", "cancelled"))
    return {"tasks": rows, "passed": passed, "terminal": terminal,
            "ratio": (passed / terminal) if terminal else None,
            "post_pass_defects": None,
            "note": "post-pass defects come from recorded findings, "
                    "not from state alone"}


def run_report(store: Store, project_key: str,
               run_id: str | None = None) -> dict[str, Any]:
    snap = _snapshot(store, project_key)
    runs = [dict(r) for r in store.conn.execute(
        "SELECT id FROM orx_runs WHERE snapshot_id=? ORDER BY id", (snap,))]
    if not runs:
        raise SystemExit(f"no runs in snapshot {snap}")
    if run_id is None:
        run_id = runs[-1]["id"]
    if run_id not in {r["id"] for r in runs}:
        raise SystemExit(f"run {run_id} not in snapshot {snap}")
    return {
        "project": project_key,
        "run": run_id,
        "snapshot_id": snap,
        "delivery_quality": delivery_quality(store, snap, run_id),
        "consumption": consumption(store, snap, run_id),
        "completion_time": completion_time(store, snap, run_id),
        "first_pass_rate": first_pass_rate(store, snap, run_id),
        "rework": rework(store, snap, run_id),
        "coverage": coverage(store, snap, run_id, project_key),
        "session_side": session_side(store, snap, run_id, project_key),
    }


# ------------------------------------------------------------- sessions ---

def session_report(store: Store, session_id: str) -> dict[str, Any]:
    """Native zcode session analysis; works without any ORX project."""
    s = store.conn.execute(
        "SELECT * FROM zc_sessions WHERE session_id=?",
        (session_id,)).fetchone()
    if s is None:
        raise SystemExit(f"unknown session {session_id}")
    totals = correlate.zcode_tree_totals(store, session_id)
    turns = [dict(r) for r in store.conn.execute(
        "SELECT turn_id, status, started_ms, completed_ms, "
        "model_request_count, model_retry_count, tool_call_count, "
        "tool_error_count, input_tokens, output_tokens, cache_read "
        "FROM zc_turns WHERE session_id=? ORDER BY started_ms",
        (session_id,))]
    reqs = [dict(r) for r in store.conn.execute(
        "SELECT model_id, status, COUNT(*) n FROM zc_requests "
        "WHERE session_id=? GROUP BY model_id, status", (session_id,))]
    logical = store.conn.execute(
        "SELECT COUNT(DISTINCT logical_request_id) n FROM zc_requests "
        "WHERE session_id=?", (session_id,)).fetchone()["n"]
    retry_rows = store.conn.execute(
        "SELECT COUNT(*) n FROM zc_requests WHERE session_id=? "
        "AND attempt_index > 0", (session_id,)).fetchone()["n"]
    tools = [dict(r) for r in store.conn.execute(
        "SELECT tool_name, COUNT(*) n, SUM(status='error') errors, "
        "SUM(duration_ms) total_ms FROM zc_tools WHERE session_id=? "
        "GROUP BY tool_name ORDER BY n DESC", (session_id,))]
    stamps = sorted(
        [x for t in turns
         for x in (t["started_ms"], t["completed_ms"]) if x is not None])
    gaps = [(b - a) / 1000 for a, b in zip(stamps, stamps[1:])]
    return {
        "session_id": session_id,
        "directory": s["directory"],
        "title": s["title"],
        "parent_id": s["parent_id"],
        "tree": totals,
        "turns": turns,
        "requests_by_model": reqs,
        "logical_requests": logical,
        "request_retry_rows": retry_rows,
        "tools": tools,
        "longest_no_progress_sec": max(gaps) if gaps else None,
    }


def project_session_index(store: Store, project_key: str) -> dict:
    """All native sessions/codex threads/cursor composers for a project."""
    zc = [dict(r) for r in store.conn.execute(
        "SELECT session_id, title, created_ms, updated_ms, parent_id "
        "FROM zc_sessions WHERE directory=? ORDER BY created_ms",
        (project_key,))]
    cx = [dict(r) for r in store.conn.execute(
        "SELECT thread_id, rollout_path, created_at FROM cx_threads "
        "WHERE cwd=?", (project_key,))]
    cur = [dict(r) for r in store.conn.execute(
        "SELECT c.composer_id, c.name, c.created_ms, c.is_subagent "
        "FROM cur_composers c JOIN cur_workspaces w "
        "ON w.workspace_id=c.workspace_id WHERE w.folder IN (?, ?)",
        ("file://" + project_key, project_key))]
    return {"zcode_sessions": zc, "codex_threads": cx,
            "cursor_composers": cur}
