"""Findings: investigation state, dedupe, issue drafts, fix links.

A finding is a signal worth a human's (or another agent's) attention. It
is never an execution authorization: drafts stay local, nothing is
published, no fix is started. Identity is the fingerprint — the same
(rule, project, component, detail) seen again updates `last_seen` and
merges refs instead of creating a near-duplicate per session.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .store import Store, now_iso


def fingerprint(rule: str, project_key: str | None, component: str,
                detail_key: str) -> str:
    h = hashlib.sha256()
    h.update(json.dumps([rule, project_key, component, detail_key],
                        ensure_ascii=False).encode())
    return h.hexdigest()[:32]


def upsert_finding(store: Store, *, rule: str, project_key: str | None,
                   component: str, detail_key: str, title: str,
                   payload: dict[str, Any],
                   refs: list[tuple[str, str]] | None = None) -> dict:
    """Insert or refresh a finding. Returns the row with `merged` flag."""
    fp = fingerprint(rule, project_key, component, detail_key)
    existing = store.conn.execute(
        "SELECT id, first_seen, status FROM findings WHERE fingerprint=?",
        (fp,)).fetchone()
    ts = now_iso()
    if existing is None:
        cur = store.conn.execute(
            "INSERT INTO findings(fingerprint, rule, project_key, title, "
            "status, first_seen, last_seen, payload_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (fp, rule, project_key, title, "open", ts, ts,
             json.dumps(payload, ensure_ascii=False)))
        finding_id = cur.lastrowid
        merged = False
    else:
        finding_id = existing["id"]
        store.conn.execute(
            "UPDATE findings SET last_seen=?, payload_json=?, title=? "
            "WHERE id=?",
            (ts, json.dumps(payload, ensure_ascii=False), title, finding_id))
        # seen before this call = merged, full stop. Comparing timestamps
        # (first_seen != ts) silently mislabelled same-second re-upserts
        # as new — now_iso() truncates to seconds.
        merged = True
    for kind, ref in refs or []:
        store.conn.execute(
            "INSERT OR IGNORE INTO finding_refs VALUES (?,?,?)",
            (finding_id, kind, ref))
    store.conn.commit()
    row = store.conn.execute(
        "SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
    out = dict(row)
    out["merged"] = merged
    return out


def dismiss_finding(store: Store, finding_id: int, note: str) -> None:
    store.conn.execute(
        "UPDATE findings SET status='dismissed', payload_json=json_set("
        "payload_json, '$.dismiss_note', ?) WHERE id=?", (note, finding_id))
    store.conn.commit()


def link_fix(store: Store, finding_id: int, fix_ref: str,
             before: dict | None, after: dict | None,
             note: str = "") -> dict:
    """Associate a fix and record a same-caliber before/after comparison.

    `comparable` is False when either side lacks a sample: the improvement
    is then presented as unproven, never as demonstrated.
    """
    comparable = bool(before and after)
    ts = now_iso()
    store.conn.execute(
        "INSERT OR REPLACE INTO finding_fixes VALUES (?,?,?,?,?,?,?)",
        (finding_id, fix_ref, ts,
         json.dumps(before, ensure_ascii=False) if before else None,
         json.dumps(after, ensure_ascii=False) if after else None,
         1 if comparable else 0, note))
    store.conn.commit()
    return {"finding": finding_id, "fix": fix_ref, "comparable": comparable,
            "verified_at": ts if comparable else None}


def draft_issue(store: Store, finding_id: int) -> dict:
    """The issue draft a receiving agent or human can act on. Structured,
    evidence-bound, no auto-publication."""
    row = store.conn.execute(
        "SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
    if row is None:
        raise SystemExit(f"no finding {finding_id}")
    p = json.loads(row["payload_json"])
    refs = [dict(r) for r in store.conn.execute(
        "SELECT kind, ref FROM finding_refs WHERE finding_id=?",
        (finding_id,))]
    fixes = [dict(r) for r in store.conn.execute(
        "SELECT fix_ref, comparable, verified_at, note FROM finding_fixes "
        "WHERE finding_id=?", (finding_id,))]
    return {
        "title": row["title"],
        "rule": row["rule"],
        "project": row["project_key"],
        "status": row["status"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "actual_behavior": p.get("actual_behavior"),
        "expected_behavior": p.get("expected_behavior"),
        "impact": p.get("impact"),
        "evidence": p.get("evidence"),
        "confirmed_causes": p.get("confirmed_causes", []),
        "hypotheses_to_verify": p.get("hypotheses_to_verify", []),
        "minimal_fix_suggestion": p.get("minimal_fix_suggestion"),
        "acceptance_criteria": p.get("acceptance_criteria"),
        "regression_verification": p.get("regression_verification"),
        "baseline": p.get("baseline"),
        "confidence": p.get("confidence"),
        "missing_evidence": p.get("missing_evidence", []),
        "linked_refs": refs,
        "linked_fixes": fixes,
        "note": "draft only: not published anywhere; fix verification is "
                "unproven until comparable=true",
    }


def list_findings(store: Store, project_key: str | None = None) -> list[dict]:
    q = "SELECT id, fingerprint, rule, project_key, title, status, " \
        "first_seen, last_seen FROM findings"
    args: tuple = ()
    if project_key:
        q += " WHERE project_key=?"
        args = (project_key,)
    return [dict(r) for r in store.conn.execute(q + " ORDER BY last_seen")]
