"""Attempt <-> native session correlation.

Two authorities only (docs/correlation.md):
- ``stored``: ORX itself wrote ``attempts.session_ref``. A fact.
- ``inferred``: recomputed from recorded evidence (the attempt's unique
  marker inside a subagent's first prompt part, plus the session's project
  directory). Always carries basis + evidence + confidence, and is
  recomputed (never accumulated) on every correlate pass. High additionally
  requires the session's creation time to fall inside the attempt span ±
  ``WINDOW_SLACK_MS`` — time is only ever a veto against stale markers,
  never join evidence on its own.

Nothing here joins by title similarity, time proximity, or short task ids.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .store import Store, now_iso

_MARKER = re.compile(r"\battempt (\d+)\b")

# ORX v11 dispatch prompts open with "ORX_ASSIGNMENT=orx-assignment:<uuid4>"
# — a one-time token minted per attempt. A token hit is exact and
# unguessable: it cannot be paraphrased away and cannot appear in another
# prompt's evidence lineage, so it needs no window or role cross-check.
_NONCE = re.compile(
    r"ORX_ASSIGNMENT=(orx-assignment:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")

# High-confidence marker matches must also survive a creation-time window:
# the subagent session is created moments BEFORE the worker claims (measured
# 4-11 s on six R006 host workers), never long after the attempt closed. A
# marker hit outside the attempt span ± this slack is a stale copy (e.g. a
# rev-1 evidence lineage pasted into a rev-2 dispatch) and is downgraded to
# ambiguous rather than linked.
WINDOW_SLACK_MS = 300_000

# A dispatch prompt self-declares its role in its opening line ("You are an
# ORX worker/verifier for Goal ..."). When the declaration survives in the
# first part, it vetoes marker matches against attempts of the other role —
# R006 evidence: a verifier session's prompt cites the worker attempt id it
# verifies ("attempt 107" evidence lineage) and its creation time fits the
# worker's span, so only the role text separates them. No declaration means
# no veto (best-effort until ORX embeds per-attempt nonces).
_ROLE_DECLARATIONS = (
    ("ORX verifier", "verifier"),
    ("ORX worker", "worker"),
)


def _role_mismatch(attempt_role: str, first_part: str | None) -> str | None:
    """Reason string when the prompt's role declaration contradicts the
    attempt's role; None when consistent or undeclared."""
    if not first_part:
        return None
    for phrase, role in _ROLE_DECLARATIONS:
        if phrase in first_part and attempt_role != role:
            return (f"prompt declares '{phrase}' but attempt role is "
                    f"'{attempt_role}'")
    return None


@dataclass
class Link:
    project_key: str
    attempt_id: int
    harness: str
    session_id: str
    authority: str                     # stored | inferred
    basis: str
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: str | None = None      # None for stored; high|ambiguous|…


def latest_snapshot(store: Store, project_key: str) -> int | None:
    r = store.conn.execute(
        "SELECT snapshot_id FROM orx_snapshots WHERE project_key=? "
        "ORDER BY snapshot_id DESC LIMIT 1", (project_key,)).fetchone()
    return r["snapshot_id"] if r else None


def zcode_session_ids(store: Store) -> dict[str, dict]:
    rows = store.conn.execute(
        "SELECT s.session_id, s.parent_id, s.directory, "
        "s.created_ms, s.updated_ms, f.first_part_text "
        "FROM zc_sessions s LEFT JOIN zc_first_parts f "
        "ON f.session_id = s.session_id").fetchall()
    return {r["session_id"]: dict(r) for r in rows}


def _nonce_index(store: Store, project_key: str,
                 sessions: dict[str, dict]) -> dict[str, set[str]]:
    """nonce token -> sessions whose first prompt carries it inside this
    project. A token belongs to exactly one attempt, so two sessions on one
    token is a genuine double-dispatch, not a lineage artifact."""
    idx: dict[str, set[str]] = {}
    for sid, s in sessions.items():
        if s["directory"] != project_key or not s["first_part_text"]:
            continue
        m = _NONCE.search(s["first_part_text"])
        if m:
            idx.setdefault(m.group(1), set()).add(sid)
    return idx


def _marker_index(store: Store, project_key: str,
                  sessions: dict[str, dict]) -> dict[int, set[str]]:
    """attempt id -> sessions whose first prompt names that attempt and
    whose directory is exactly this project. A session matches an attempt
    at most once, however often the marker repeats inside the prompt."""
    idx: dict[int, set[str]] = {}
    for sid, s in sessions.items():
        if s["directory"] != project_key or not s["first_part_text"]:
            continue
        for m in _MARKER.finditer(s["first_part_text"]):
            idx.setdefault(int(m.group(1)), set()).add(sid)
    return idx


def _window_check(a: Any, session: dict) -> dict:
    """Creation-time cross-check for a unique marker hit (docs/correlation.md
    high tier). Returns ok + the numbers behind the verdict; a missing
    attempt span or session creation time can never be `ok`."""
    started, ended = a["started_at"], a["ended_at"]
    created = session["created_ms"]
    if not started or not ended:
        return {"ok": False,
                "reason": "attempt span missing; creation time cannot be "
                          "cross-checked",
                "attempt_span": [started, ended],
                "session_created_ms": created}
    if created is None:
        return {"ok": False,
                "reason": "session creation time unknown",
                "attempt_span": [started, ended],
                "session_created_ms": None}
    lo = _epoch_ms(started) - WINDOW_SLACK_MS
    hi = _epoch_ms(ended) + WINDOW_SLACK_MS
    ok = lo <= created <= hi
    return {"ok": ok,
            "reason": None if ok else
            "session created outside the attempt span ± slack "
            f"({WINDOW_SLACK_MS // 1000}s); stale marker or wrong lineage",
            "attempt_span": [started, ended],
            "session_created_ms": created,
            "window_ms": [lo, hi]}


def correlate(store: Store, project_key: str,
              allow_medium: bool = False) -> dict:
    """(Re)compute links for the project's latest ORX snapshot."""
    snap = latest_snapshot(store, project_key)
    if snap is None:
        return {"project": project_key, "error": "no ORX snapshot"}
    sessions = zcode_session_ids(store)
    markers = _marker_index(store, project_key, sessions)
    nonces = _nonce_index(store, project_key, sessions)
    attempts = store.conn.execute(
        "SELECT id, role, harness, session_ref, driver, started_at, ended_at, "
        "nonce FROM orx_attempts WHERE snapshot_id=?", (snap,)).fetchall()

    # stored links first; inferred rows are recomputed from scratch
    store.conn.execute(
        "DELETE FROM links WHERE project_key=? AND authority='inferred'",
        (project_key,))
    counts = {"stored": 0, "inferred_high": 0, "ambiguous": 0,
              "none": 0, "superseded": 0}
    for a in attempts:
        # existing stored rows survive; a stored ref may appear later and
        # must supersede an older inferred row for the same attempt
        prev = store.conn.execute(
            "SELECT rowid, authority, session_id FROM links "
            "WHERE project_key=? AND attempt_id=? AND harness=?",
            (project_key, a["id"], a["harness"])).fetchone()
        if a["session_ref"]:
            if prev and prev["authority"] == "inferred":
                store.conn.execute(
                    "UPDATE links SET superseded_by=NULL, basis=?, "
                    "authority='stored', confidence=NULL, evidence=?, "
                    "computed_at=? WHERE rowid=?",
                    ("ORX attempts.session_ref (written by ORX itself)",
                     json.dumps({"prior_inferred": prev["session_id"]}),
                     now_iso(), prev["rowid"]))
                counts["superseded"] += 1
            elif not prev:
                store.conn.execute(
                    "INSERT INTO links(project_key, attempt_id, harness, "
                    "session_id, authority, basis, evidence_json, "
                    "confidence, computed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (project_key, a["id"], a["harness"], a["session_ref"],
                     "stored",
                     "ORX attempts.session_ref (written by ORX itself)",
                     None, None, now_iso()))
            counts["stored"] += 1
            continue
        if a["harness"] != "zcode" or a["driver"] != "host":
            counts["none"] += 1
            continue
        if a["nonce"]:
            # v11 identity anchor: the token is one-time and per-attempt, so
            # a hit needs no window/role cross-check (it cannot be stale
            # lineage — a uuid exists in exactly one dispatch prompt).
            cands = {c for c in nonces.get(a["nonce"], set())
                     if sessions[c]["parent_id"]}
            if len(cands) == 1:
                sid = next(iter(cands))
                snippet = _NONCE.search(sessions[sid]["first_part_text"])
                store.conn.execute(
                    "INSERT INTO links(project_key, attempt_id, harness, "
                    "session_id, authority, basis, evidence_json, "
                    "confidence, computed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (project_key, a["id"], "zcode", sid, "inferred",
                     "subagent first prompt carries this attempt's one-time "
                     "ORX_ASSIGNMENT token (ORX v11 identity anchor; minted "
                     "at dispatch, unique to this attempt); session.directory "
                     "equals the project root; session has a parent "
                     "(subagent structure)",
                     json.dumps({
                         "matched_text": snippet.group(0) if snippet else None,
                         "nonce": a["nonce"],
                         "session_directory": sessions[sid]["directory"],
                         "session_created_ms": sessions[sid]["created_ms"]}),
                     "high", now_iso()))
                counts["inferred_high"] += 1
            elif len(cands) > 1:
                store.conn.execute(
                    "INSERT INTO links(project_key, attempt_id, harness, "
                    "session_id, authority, basis, evidence_json, "
                    "confidence, computed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (project_key, a["id"], "zcode", ",".join(sorted(cands)),
                     "inferred",
                     "the attempt's one-time ORX_ASSIGNMENT token appears in "
                     "more than one subagent session (genuine double "
                     "dispatch of the same prompt)",
                     json.dumps({"nonce": a["nonce"],
                                 "candidates": sorted(cands)}),
                     "ambiguous", now_iso()))
                counts["ambiguous"] += 1
            else:
                counts["none"] += 1
            continue
        cands = markers.get(a["id"], set())
        # subagent structure required: a marker in a root session's prompt
        # is not evidence about a worker attempt
        cands = {c for c in cands if sessions[c]["parent_id"]}
        if len(cands) == 1:
            sid = next(iter(cands))
            snippet = _MARKER.search(sessions[sid]["first_part_text"])
            w = _window_check(a, sessions[sid])
            role_reason = _role_mismatch(
                a["role"], sessions[sid]["first_part_text"])
            evidence = {
                "matched_text": snippet.group(0) if snippet else None,
                "session_directory": sessions[sid]["directory"],
                "session_created_ms": sessions[sid]["created_ms"],
                "attempt_span": w["attempt_span"],
                "window_ms": w.get("window_ms"),
                "slack_ms": WINDOW_SLACK_MS}
            if w["ok"] and role_reason is None:
                store.conn.execute(
                    "INSERT INTO links(project_key, attempt_id, harness, "
                    "session_id, authority, basis, evidence_json, "
                    "confidence, computed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (project_key, a["id"], "zcode", sid, "inferred",
                     "subagent first prompt contains the attempt's unique "
                     "marker 'attempt <id>'; session.directory equals the "
                     "project root; session has a parent (subagent "
                     "structure); session created within the attempt span "
                     f"± {WINDOW_SLACK_MS // 1000}s slack; prompt role "
                     "declaration consistent",
                     json.dumps(evidence), "high", now_iso()))
                counts["inferred_high"] += 1
            else:
                reason = role_reason or w["reason"]
                store.conn.execute(
                    "INSERT INTO links(project_key, attempt_id, harness, "
                    "session_id, authority, basis, evidence_json, "
                    "confidence, computed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (project_key, a["id"], "zcode", sid, "inferred",
                     "marker matched exactly one subagent session, but "
                     + reason + "; refusing high",
                     json.dumps({**evidence,
                                 "window_reason": reason}),
                     "ambiguous", now_iso()))
                counts["ambiguous"] += 1
        elif len(cands) > 1:
            store.conn.execute(
                "INSERT INTO links(project_key, attempt_id, harness, "
                "session_id, authority, basis, evidence_json, confidence, "
                "computed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (project_key, a["id"], "zcode", ",".join(sorted(cands)),
                 "inferred",
                 "marker matched more than one subagent session",
                 json.dumps({"candidates": sorted(cands)}),
                 "ambiguous", now_iso()))
            counts["ambiguous"] += 1
        else:
            counts["none"] += 1
    store.conn.commit()
    return {"project": project_key, "snapshot_id": snap, **counts}


def links_for_snapshot(store: Store, project_key: str,
                       snapshot_id: int) -> list[Link]:
    out = []
    for r in store.conn.execute(
        "SELECT l.attempt_id, a.harness, l.session_id, l.authority, "
        "l.basis, l.evidence_json, l.confidence FROM links l "
        "JOIN orx_attempts a ON a.snapshot_id=? AND a.id=l.attempt_id "
        "WHERE l.project_key=?", (snapshot_id, project_key)):
        out.append(Link(
            project_key=project_key, attempt_id=r["attempt_id"],
            harness=r["harness"], session_id=r["session_id"],
            authority=r["authority"], basis=r["basis"],
            evidence=json.loads(r["evidence_json"]) if r["evidence_json"]
            else {},
            confidence=r["confidence"]))
    return out


def zcode_tree(store: Store, session_id: str) -> list[str]:
    """The session and all descendants, each exactly once."""
    out: list[str] = []
    seen: set[str] = set()
    frontier = [session_id]
    while frontier:
        sid = frontier.pop()
        if sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
        rows = store.conn.execute(
            "SELECT session_id FROM zc_sessions WHERE parent_id=? "
            "AND session_id != ?", (sid, sid)).fetchall()
        frontier.extend(r["session_id"] for r in rows)
    return out


def zcode_tree_totals(store: Store, session_id: str) -> dict[str, Any]:
    """Token/duration totals over the session tree, deduped by id."""
    ids = zcode_tree(store, session_id)
    qmarks = ",".join("?" * len(ids))
    r = store.conn.execute(
        f"SELECT COUNT(*) turns, SUM(input_tokens) i, SUM(output_tokens) o, "
        f"SUM(reasoning_tokens) r, SUM(cache_read) cr, "
        f"SUM(cache_creation) cc, SUM(model_request_count) reqs, "
        f"SUM(model_retry_count) retries, SUM(tool_call_count) tools, "
        f"SUM(tool_error_count) tool_err FROM zc_turns "
        f"WHERE session_id IN ({qmarks})", ids).fetchone()
    per_session = {}
    for row in store.conn.execute(
        f"SELECT session_id, COUNT(*) turns, SUM(input_tokens) i, "
        f"SUM(output_tokens) o FROM zc_turns WHERE session_id IN ({qmarks}) "
        f"GROUP BY session_id", ids):
        per_session[row["session_id"]] = {
            "turns": row["turns"], "input_tokens": row["i"],
            "output_tokens": row["o"]}
    return {
        "root": session_id, "sessions_in_tree": len(ids),
        "session_ids": ids,
        "turns": r["turns"],
        "input_tokens": r["i"], "output_tokens": r["o"],
        "reasoning_tokens": r["r"],
        "cache_read": r["cr"], "cache_creation": r["cc"],
        "model_requests": r["reqs"], "model_retries": r["retries"],
        "tool_calls": r["tools"], "tool_errors": r["tool_err"],
        "per_session": per_session,
    }


def codex_thread_totals(store: Store, thread_id: str) -> dict[str, Any]:
    r = store.conn.execute(
        "SELECT COUNT(*) rows, COUNT(DISTINCT turn_id) turns, "
        "SUM(input_tokens) i, SUM(cached_input) ci, "
        "SUM(cache_write) cw, SUM(output_tokens) o, "
        "SUM(reasoning_output) ro FROM cx_usage WHERE thread_id=?",
        (thread_id,)).fetchone()
    return {"thread_id": thread_id, "usage_rows": r["rows"],
            "turns_with_usage": r["turns"], "input_tokens": r["i"],
            "cached_input": r["ci"], "cache_write": r["cw"],
            "output_tokens": r["o"], "reasoning_output": r["ro"]}


def attribute_session_windows(store: Store, project_key: str,
                              snapshot_id: int) -> dict[int, dict]:
    """Split one session's usage across the attempts it carried.

    Only when a session is linked to multiple attempts do we try: a turn
    belongs to an attempt when the turn's [started, completed] window falls
    inside the attempt's [started_at, ended_at] span. Overlapping attempt
    spans or turns outside every span leave the session unattributed —
    reported as such, never split by guesswork.
    """
    rows = store.conn.execute(
        "SELECT l.attempt_id, l.session_id, l.authority, "
        "a.started_at, a.ended_at FROM links l JOIN orx_attempts a "
        "ON a.snapshot_id=? AND a.id=l.attempt_id "
        "WHERE l.project_key=? AND l.authority IN ('stored','inferred') "
        "AND (l.confidence IS NULL OR l.confidence != 'ambiguous') "
        "AND a.harness='zcode'",
        (snapshot_id, project_key)).fetchall()
    by_session: dict[str, list] = {}
    for r in rows:
        by_session.setdefault(r["session_id"], []).append(r)
    out: dict[int, dict] = {}
    for sid, attrs in by_session.items():
        if len(attrs) < 2:
            continue
        spans = []
        ok = True
        for a in attrs:
            if not a["started_at"] or not a["ended_at"]:
                ok = False
                break
            spans.append((_epoch_ms(a["started_at"]),
                          _epoch_ms(a["ended_at"]), a["attempt_id"]))
        if not ok:
            for a in attrs:
                out[a["attempt_id"]] = {
                    "attributable": False,
                    "reason": "attempt span missing; session totals only"}
            continue
        spans.sort()
        for (s0, e0, i0), (s1, e1, i1) in zip(spans, spans[1:]):
            if s1 < e0:
                for a in attrs:
                    out[a["attempt_id"]] = {
                        "attributable": False,
                        "reason": "attempt spans overlap; refusing to split"}
                ok = False
                break
        if not ok:
            continue
        turns = store.conn.execute(
            "SELECT turn_id, started_ms, completed_ms, input_tokens, "
            "output_tokens FROM zc_turns WHERE session_id=?", (sid,)).fetchall()
        session_in = session_out = 0
        matched_in = matched_out = 0
        got: dict[int, list[int]] = {a["attempt_id"]: [0, 0] for a in attrs}
        for t in turns:
            session_in += t["input_tokens"] or 0
            session_out += t["output_tokens"] or 0
            if t["started_ms"] is None:
                continue
            for s, e, aid in spans:
                if s <= t["started_ms"] and (
                        t["completed_ms"] is None or t["completed_ms"] <= e):
                    got[aid][0] += t["input_tokens"] or 0
                    got[aid][1] += t["output_tokens"] or 0
                    matched_in += t["input_tokens"] or 0
                    matched_out += t["output_tokens"] or 0
                    break
        for aid, (i, o) in got.items():
            out[aid] = {
                "attributable": True, "session_id": sid,
                "input_tokens": i, "output_tokens": o,
                "session_totals": {"input": session_in,
                                   "output": session_out},
                "unattributed_within_session": {
                    "input": session_in - matched_in,
                    "output": session_out - matched_out},
            }
    return out


def _epoch_ms(iso: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(iso).timestamp() * 1000)
