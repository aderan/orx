"""Anomaly rules: small, explainable, configurable.

Every rule returns finding-shaped dicts with trigger evidence, the
baseline it compared against, a confidence label, and the evidence still
missing. Rules emit signals; they never assert cause. Absolute-token
rankings across task types or tools are deliberately absent.

Thresholds live in a plain dict (config file or CLI), so a rule can be
tuned without code. `run_all` applies the enabled rules over the current
store and upserts findings (dedupe by fingerprint).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..store import Store

DEFAULTS: dict[str, Any] = {
    "usage_outlier": {"enabled": True, "min_samples": 5, "factor": 3.0},
    "repeat_failures": {"enabled": True, "min_failed_events": 2},
    "replan_burst": {"enabled": True, "min_replans": 2, "window_hours": 24},
    "no_progress": {"enabled": True, "min_gap_sec": 3600},
    "fast_fail_stall": {"enabled": True},
    "coverage_gap": {"enabled": True, "min_unassessed_frac": 0.5,
                     "min_unlinked_frac": 0.5, "min_attempts": 3},
    "check_loop_overflow": {"enabled": True, "budget": 3},
    "duplicate_delivery": {"enabled": True},
}


@dataclass
class RuleResult:
    rule: str
    project_key: str | None
    component: str
    detail_key: str
    title: str
    payload: dict[str, Any]
    refs: list[tuple[str, str]]

    def upsert_args(self) -> dict:
        return dict(rule=self.rule, project_key=self.project_key,
                    component=self.component, detail_key=self.detail_key,
                    title=self.title, payload=self.payload,
                    refs=self.refs)


# ------------------------------------------------------------------ rules ---

def usage_outlier(store: Store, project_key: str,
                  cfg: dict) -> list[RuleResult]:
    """Attempt consumption far above the same (role, profile) baseline.

    Baseline needs min_samples canonical observations; below that the rule
    reports nothing and says why (insufficient baseline is a stated
    condition, not a silent pass).
    """
    out: list[RuleResult] = []
    snap = _latest_snapshot(store, project_key)
    if snap is None:
        return out
    rank = {"native_cli": 0, "zcode_db": 1, "host_report": 2,
            "output_estimate": 3}
    best: dict[int, dict] = {}
    for u in store.conn.execute(
        "SELECT u.id, u.attempt_id, u.input_tokens, u.output_tokens, "
        "u.source FROM orx_usage_obs u WHERE u.snapshot_id=?", (snap,)):
        cur = best.get(u["attempt_id"])
        key = (rank.get(u["source"], 9), u["id"])
        if cur is None or key < (rank.get(cur["source"], 9), cur["id"]):
            best[u["attempt_id"]] = dict(u)
    attempts = {a["id"]: dict(a) for a in store.conn.execute(
        "SELECT id, role, profile, task_id FROM orx_attempts "
        "WHERE snapshot_id=?", (snap,))}
    groups: dict[tuple[str, str], list[tuple[int, str | None, int]]] = {}
    for aid, u in best.items():
        a = attempts.get(aid)
        if a is None:
            continue
        total = (u["input_tokens"] or 0) + (u["output_tokens"] or 0)
        groups.setdefault((a["role"], a["profile"]), []).append(
            (aid, a["task_id"], total))
    factor = float(cfg.get("factor", 3.0))
    min_samples = int(cfg.get("min_samples", 5))
    for (role, profile), items in groups.items():
        vals = sorted(v for _, _, v in items if v > 0)
        if len(vals) < min_samples:
            continue        # insufficient baseline: stated, not flagged
        med = vals[len(vals) // 2]
        for aid, task, v in items:
            if v > factor * med and med > 0:
                out.append(RuleResult(
                    "usage_outlier", project_key, f"{role}/{profile}",
                    f"attempt-{aid}",
                    f"attempt {aid} ({task or role}) used {v} tokens, "
                    f">{factor}x the {role}/{profile} median {med}",
                    {"actual_behavior": f"canonical input+output {v}",
                     "expected_behavior": "within historical band",
                     "baseline": {"group": f"{role}/{profile}",
                                  "median": med, "samples": len(vals),
                                  "factor": factor},
                     "confidence": "signal",
                     "missing_evidence": [
                         "task scope may legitimately differ — compare "
                         "objectives before calling it waste"],
                     "evidence": {"attempt_id": aid, "tokens": v}},
                    [("attempt", str(aid))]))
    return out


def repeat_failures(store: Store, project_key: str,
                    cfg: dict) -> list[RuleResult]:
    min_n = int(cfg.get("min_failed_events", 2))
    out: list[RuleResult] = []
    snap = _latest_snapshot(store, project_key)
    if snap is None:
        return out
    rows = store.conn.execute(
        "SELECT e.revision_id, e.task_id, COUNT(*) n, "
        "GROUP_CONCAT(e.reason, ' || ') reasons FROM orx_task_events e "
        "WHERE e.snapshot_id=? AND e.to_status='failed' "
        "GROUP BY e.revision_id, e.task_id HAVING n >= ?",
        (snap, min_n)).fetchall()
    for r in rows:
        out.append(RuleResult(
            "repeat_failures", project_key, f"task/{r['task_id']}",
            f"rev{r['revision_id']}-{r['task_id']}",
            f"task {r['task_id']} failed {r['n']} times",
            {"actual_behavior": f"{r['n']} failed transitions",
             "expected_behavior": "failures are exceptional, not routine",
             "baseline": {"min_failed_events": min_n},
             "confidence": "signal",
             "missing_evidence": [
                 "whether failures share one root cause or differ"],
             "evidence": {"reasons": r["reasons"]}},
            [("task", f"rev{r['revision_id']}/{r['task_id']}")]))
    return out


def replan_burst(store: Store, project_key: str,
                 cfg: dict) -> list[RuleResult]:
    min_n = int(cfg.get("min_replans", 2))
    window_h = float(cfg.get("window_hours", 24))
    out: list[RuleResult] = []
    snap = _latest_snapshot(store, project_key)
    if snap is None:
        return out
    from datetime import datetime
    rows = store.conn.execute(
        "SELECT m.run_id, r.created_at FROM orx_replan_maps m "
        "JOIN orx_revisions r ON r.snapshot_id=? AND r.id=m.revision_id "
        "WHERE m.snapshot_id=?", (snap, snap)).fetchall()
    by_run: dict[str, list[datetime]] = {}
    for r in rows:
        by_run.setdefault(r["run_id"], []).append(
            datetime.fromisoformat(r["created_at"]))
    for run_id, times in by_run.items():
        times.sort()
        window = window_h * 3600
        for i in range(len(times)):
            cluster = [t for t in times
                       if 0 <= (t - times[i]).total_seconds() <= window]
            if len(cluster) >= min_n:
                out.append(RuleResult(
                    "replan_burst", project_key, f"run/{run_id}",
                    f"burst-{run_id}",
                    f"run {run_id} replanned {len(cluster)} times within "
                    f"{window_h}h",
                    {"actual_behavior":
                        f"{len(cluster)} replans in {window_h}h",
                     "expected_behavior": "plans survive contact with work",
                     "baseline": {"window_hours": window_h,
                                  "min_replans": min_n},
                     "confidence": "signal",
                     "missing_evidence": [
                         "whether each replan was justified by new "
                         "information or by plan quality"],
                     "evidence": {"times": [t.isoformat()
                                            for t in cluster]}},
                    [("run", run_id)]))
                break
    return out


def no_progress(store: Store, cfg: dict) -> list[RuleResult]:
    min_gap = int(cfg.get("min_gap_sec", 3600))
    out: list[RuleResult] = []
    sessions = store.conn.execute(
        "SELECT DISTINCT session_id FROM zc_turns").fetchall()
    for s in sessions:
        stamps = [r["started_ms"] for r in store.conn.execute(
            "SELECT started_ms FROM zc_turns WHERE session_id=? AND "
            "started_ms IS NOT NULL ORDER BY started_ms",
            (s["session_id"],))]
        gaps = [(b - a) / 1000 for a, b in zip(stamps, stamps[1:])]
        if gaps and max(gaps) >= min_gap:
            out.append(RuleResult(
                "no_progress", None, f"session/{s['session_id'][:20]}",
                f"gap-{s['session_id']}",
                f"session had a {max(gaps):.0f}s gap with no activity",
                {"actual_behavior": f"longest in-session gap "
                                    f"{max(gaps):.0f}s",
                 "expected_behavior": "work proceeds in smaller gaps",
                 "baseline": {"min_gap_sec": min_gap},
                 "confidence": "low",
                 "missing_evidence": [
                     "gap may be user think-time or an idle prompt; check "
                     "the messages around it"],
                 "evidence": {"longest_gap_sec": round(max(gaps))}},
                [("session", s["session_id"])]))
    return out


def fast_fail_stall(store: Store, cfg: dict) -> list[RuleResult]:
    """A session whose latest turn is a zero-token error turn.

    The silent-stall signature measured on real controller stalls (R006
    2.48h; To2050 2026-10-06 2h47m): one model request, no retry, no
    tokens, no tools — the session dies instantly and nothing surfaces
    until a human happens to return. Errored-after-work turns (nonzero
    tokens) are a different, visible failure mode and stay out of scope.
    no_progress needs a 3600s gap before it can even look, so it reports
    an hour late; this rule fires on the next watch round.
    """
    out: list[RuleResult] = []
    rows = store.conn.execute(
        "SELECT t.session_id, t.turn_id, t.started_ms, "
        "t.model_request_count, t.model_retry_count FROM zc_turns t "
        "JOIN (SELECT session_id, MAX(started_ms) mx FROM zc_turns WHERE "
        "started_ms IS NOT NULL GROUP BY session_id) m "
        "ON m.session_id=t.session_id AND m.mx=t.started_ms "
        "WHERE t.status='error' AND COALESCE(t.input_tokens,0)=0 "
        "AND COALESCE(t.output_tokens,0)=0").fetchall()
    now_ms = time.time() * 1000
    seen: set[str] = set()
    for r in rows:
        if r["session_id"] in seen:
            continue    # two turns sharing the max started_ms
        seen.add(r["session_id"])
        minutes = (round((now_ms - r["started_ms"]) / 60000)
                   if r["started_ms"] else None)
        out.append(RuleResult(
            "fast_fail_stall", None, f"session/{r['session_id'][:20]}",
            f"stall-{r['session_id']}",
            (f"session's latest turn is a zero-token error turn "
             f"({minutes}m ago)" if minutes is not None else
             "session's latest turn is a zero-token error turn"),
            {"actual_behavior":
                "turn ended status=error with 0 tokens, 1 request, "
                "no retry; the session has not progressed since",
             "expected_behavior":
                "a fast-failed session surfaces on the next watch "
                "round, not when a human happens to return",
             "baseline": {
                 "signature": "status=error AND input+output tokens=0",
                 "measured_stalls": [
                     "R006 controller 2.48h (GLM quota fast-fail)",
                     "To2050 controller 2026-10-06 2h47m"]},
             "confidence": "signal",
             "missing_evidence": [
                 "the provider error text (turn_usage records no reason)",
                 "whether the operator intended to stop the session here"],
             "evidence": {"session_id": r["session_id"],
                          "turn_id": r["turn_id"],
                          "minutes_since": minutes,
                          "model_request_count": r["model_request_count"],
                          "model_retry_count": r["model_retry_count"]}},
            [("session", r["session_id"])]))
    return out


def coverage_gap(store: Store, project_key: str,
                 cfg: dict) -> list[RuleResult]:
    out: list[RuleResult] = []
    snap = _latest_snapshot(store, project_key)
    if snap is None:
        return out
    min_att = int(cfg.get("min_attempts", 3))
    rows = store.conn.execute(
        "SELECT role, COUNT(*) n FROM orx_attempts WHERE snapshot_id=? "
        "GROUP BY role", (snap,)).fetchall()
    counts = {r["role"]: r["n"] for r in rows}
    workers = counts.get("worker", 0)
    if workers < min_att:
        return out
    att_with_obs = {r["attempt_id"] for r in store.conn.execute(
        "SELECT DISTINCT attempt_id FROM orx_usage_obs WHERE snapshot_id=?",
        (snap,))}
    unassessed = store.conn.execute(
        "SELECT COUNT(*) c FROM orx_attempts WHERE snapshot_id=? AND "
        "role='worker' AND id NOT IN (SELECT attempt_id FROM orx_usage_obs "
        "WHERE snapshot_id=?) AND usage_missing_reason IS NULL",
        (snap, snap)).fetchone()["c"]
    unlinked = store.conn.execute(
        "SELECT COUNT(*) c FROM orx_attempts a WHERE a.snapshot_id=? AND "
        "a.role='worker' AND a.driver='host' AND a.session_ref IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM links l WHERE l.attempt_id=a.id "
        "AND l.confidence='high')", (snap,)).fetchone()["c"]
    host_workers = store.conn.execute(
        "SELECT COUNT(*) c FROM orx_attempts WHERE snapshot_id=? AND "
        "role='worker' AND driver='host'", (snap,)).fetchone()["c"]
    frac_unassessed = unassessed / workers if workers else 0
    frac_unlinked = unlinked / host_workers if host_workers else 0
    if frac_unassessed >= float(cfg.get("min_unassessed_frac", 0.5)):
        out.append(RuleResult(
            "coverage_gap", project_key, "usage-coverage",
            "unassessed",
            f"{unassessed}/{workers} worker attempts have no usage "
            f"observation and no recorded reason",
            {"actual_behavior": f"{unassessed} unassessed of {workers}",
             "expected_behavior": "every attempt observed or reasoned",
             "baseline": {"min_unassessed_frac":
                          cfg.get("min_unassessed_frac", 0.5)},
             "confidence": "signal",
             "missing_evidence": ["pre-G005 eras legitimately lack these"],
             "evidence": {"workers": workers, "unassessed": unassessed}},
            []))
    if frac_unlinked >= float(cfg.get("min_unlinked_frac", 0.5)):
        out.append(RuleResult(
            "coverage_gap", project_key, "session-coverage",
            "unlinked-host-workers",
            f"{unlinked}/{host_workers} host worker attempts carry no "
            f"session identity (stored or inferred-high)",
            {"actual_behavior": f"{unlinked} unlinked of {host_workers}",
             "expected_behavior": "claim-time binding leaves few NULLs",
             "baseline": {"min_unlinked_frac":
                          cfg.get("min_unlinked_frac", 0.5)},
             "confidence": "signal",
             "missing_evidence": ["old runs predate --discover-session"],
             "evidence": {"host_workers": host_workers,
                          "unlinked": unlinked}},
            []))
    return out


def check_loop_overflow(store: Store, project_key: str,
                        cfg: dict) -> list[RuleResult]:
    budget = int(cfg.get("budget", 3))
    out: list[RuleResult] = []
    snap = _latest_snapshot(store, project_key)
    if snap is None:
        return out
    rows = store.conn.execute(
        "SELECT v.attempt_id, v.command, COUNT(*) n FROM orx_verifications "
        "v WHERE v.snapshot_id=? AND v.kind='command' AND v.attempt_id IS "
        "NOT NULL GROUP BY v.attempt_id, v.command HAVING n > ?",
        (snap, budget)).fetchall()
    for r in rows:
        out.append(RuleResult(
            "check_loop_overflow", project_key, f"attempt/{r['attempt_id']}",
            f"rounds-{r['attempt_id']}-{r['command'][:40]}",
            f"attempt {r['attempt_id']} ran '{r['command'][:40]}' "
            f"{r['n']} times (budget {budget})",
            {"actual_behavior": f"{r['n']} rounds for one command",
             "expected_behavior": f"at most {budget} rounds per attempt",
             "baseline": {"budget": budget},
             "confidence": "signal",
             "missing_evidence": [
                 "red-then-green TDD is legal; check the pass sequence"],
             "evidence": {"rows": r["n"], "command": r["command"]}},
            [("attempt", str(r["attempt_id"]))]))
    return out


def duplicate_delivery(store: Store, project_key: str,
                       cfg: dict) -> list[RuleResult]:
    """Declared redo of an already-passed prior task: worth verifying the
    scope actually changed — only fires on declared replan edges."""
    out: list[RuleResult] = []
    snap = _latest_snapshot(store, project_key)
    if snap is None:
        return out
    rows = store.conn.execute(
        "SELECT s.source_revision, s.source_task_id, t.task_id, "
        "t.redo_reason, pt.status prior_status FROM orx_replan_sources s "
        "JOIN orx_replan_task_maps t ON t.snapshot_id=? AND "
        "t.id=s.task_mapping_id AND t.classification='redo' "
        "LEFT JOIN orx_tasks pt ON pt.snapshot_id=? AND "
        "pt.id=s.source_task_row_id WHERE s.snapshot_id=?",
        (snap, snap, snap)).fetchall()
    for r in rows:
        out.append(RuleResult(
            "duplicate_delivery", project_key,
            f"redo/{r['source_revision']}-{r['source_task_id']}",
            f"redo-{r['source_revision']}-{r['source_task_id']}",
            f"{r['task_id']} declared redo of "
            f"rev{r['source_revision']}/{r['source_task_id']} "
            f"(prior status {r['prior_status']})",
            {"actual_behavior": "a new task re-does a prior task",
             "expected_behavior": "redo only when scope genuinely moved",
             "baseline": None,
             "confidence": "signal",
             "missing_evidence": [
                 "prior task passed and redo_reason must be checked "
                 "against the actual diff; same task_id number alone "
                 "proves nothing"],
             "evidence": {"redo_reason": r["redo_reason"],
                          "prior_status": r["prior_status"]}},
            [("task", f"rev{r['source_revision']}/"
                      f"{r['source_task_id']}")]))
    return out


RULES = {
    "usage_outlier": usage_outlier,
    "repeat_failures": repeat_failures,
    "replan_burst": replan_burst,
    "no_progress": no_progress,
    "fast_fail_stall": fast_fail_stall,
    "coverage_gap": coverage_gap,
    "check_loop_overflow": check_loop_overflow,
    "duplicate_delivery": duplicate_delivery,
}


def run_all(store: Store, project_keys: list[str],
            config: dict[str, Any] | None = None) -> list[RuleResult]:
    cfg = dict(DEFAULTS)
    for k, v in (config or {}).items():
        cfg.setdefault(k, {}).update(v)
    results: list[RuleResult] = []
    for name, fn in RULES.items():
        rule_cfg = cfg.get(name) or {}
        if not rule_cfg.get("enabled", True):
            continue
        if name in ("no_progress", "fast_fail_stall"):
            results += fn(store, rule_cfg)
        else:
            for pk in project_keys:
                results += fn(store, pk, rule_cfg)
    return results


# ---------------------------------------------------------------- helpers ---

def _latest_snapshot(store: Store, project_key: str) -> int | None:
    r = store.conn.execute(
        "SELECT snapshot_id FROM orx_snapshots WHERE project_key=? "
        "ORDER BY snapshot_id DESC LIMIT 1", (project_key,)).fetchone()
    return r["snapshot_id"] if r else None
