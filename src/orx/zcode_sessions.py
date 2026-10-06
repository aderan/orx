"""Read-only zcode session discovery for host worker identity.

Why this exists: a host worker is a subagent the Controller's session
spawns. ORX never sees the spawn, and zcode exports no session id into the
subagent's shell environment — so ``attempts.session_ref`` stayed NULL and
the recovery contract ("check the original subagent, handle =
session_ref") had no handle to use.

Two anchors, strongest first:

- **Nonce (schema v11)**: ORX mints ``orx-assignment:<uuid4>`` per attempt
  and embeds it as the first lines of the dispatch prompt. A subagent
  session whose first text part carries the token IS the dispatched
  assignment — exact, unguessable, and immune to the controller-side prompt
  paraphrasing that erased every "attempt N" marker in R006 (5 of 7 worker
  prompts lost the marker; one carried a closed attempt's number from
  stale evidence lineage). When an attempt has a nonce, the nonce is the
  only authority: a marker hit is not consulted, because stale lineage
  references are exactly the failure mode the nonce exists to prevent.
- **Marker (legacy)**: attempts from before v11 have no nonce; their
  discovery still matches a human-readable "attempt N" in the first text
  part plus directory and subagent structure.

The zcode database is opened read-only (mode=ro + query_only), never
migrated or checkpointed. Default location can be overridden with
ORX_ZCODE_DB for tests and alternate installs.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ZCODE_DB = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"

_MARKER = re.compile(r"\battempt (\d+)\b")
# ORX_ASSIGNMENT=orx-assignment:<uuid4> — the identity line dispatch prompts
# carry at their very top (first lines survive the 2000-char first-part
# truncation any reader applies).
_NONCE = re.compile(
    r"ORX_ASSIGNMENT=(orx-assignment:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)


class ZcodeDbUnavailable(Exception):
    """The zcode database is absent or unreadable: discovery cannot run."""


@dataclass
class Candidate:
    session_id: str
    parent_id: str
    title: str
    created_ms: int
    matched_text: str | None = None
    first_part_preview: str = field(default="", repr=False)

    def payload(self) -> dict:
        return {
            "session_id": self.session_id,
            "parent_id": self.parent_id,
            "title": self.title,
            "created_ms": self.created_ms,
            "matched_text": self.matched_text,
            "first_part_preview": self.first_part_preview[:160],
        }


def zcode_db_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("ORX_ZCODE_DB", "").strip()
    return Path(env).expanduser() if env else DEFAULT_ZCODE_DB


def _open_ro(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise ZcodeDbUnavailable(f"no zcode database at {db}")
    conn = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro",
                           uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def discover_attempt_session(attempt_id: int, project_root: Path,
                             db: str | None = None,
                             created_within_ms: int | None = None,
                             now_ms: int | None = None,
                             nonce: str | None = None) -> dict:
    """Find the zcode subagent session that executed this attempt, inside
    this project's directory.

    With ``nonce`` (a v11 attempt), the session is the one whose first text
    part carries ``ORX_ASSIGNMENT=<nonce>`` — the token ORX embedded in the
    dispatch prompt. Without one (legacy attempts), the marker fallback
    matches a first part naming "attempt <id>".

    Returns {"candidates": [...], "decision": "unique"|"none"|"ambiguous",
             "basis": str} — the caller decides what to store; this
    function never writes anything anywhere.
    """
    path = zcode_db_path(db)
    root = str(project_root.resolve())
    try:
        conn = _open_ro(path)
    except sqlite3.Error as e:  # pragma: no cover - environment dependent
        raise ZcodeDbUnavailable(f"cannot open {path} read-only: {e}")
    try:
        rows = conn.execute(
            "SELECT id, parent_id, title, time_created, directory "
            "FROM session WHERE directory=? AND parent_id IS NOT NULL "
            "ORDER BY time_created", (root,)).fetchall()
        if created_within_ms is not None and now_ms is not None:
            rows = [r for r in rows
                    if now_ms - r["time_created"] <= created_within_ms]
        cands: list[Candidate] = []
        for r in rows:
            part = conn.execute(
                "SELECT data FROM part WHERE session_id=? AND "
                "data LIKE '{\"type\":\"text\"%' ORDER BY sequence LIMIT 1",
                (r["id"],)).fetchone()
            if part is None:
                continue
            try:
                text = json.loads(part["data"]).get("text", "") or ""
            except (json.JSONDecodeError, AttributeError):
                continue
            if nonce is not None:
                hit = _NONCE.search(text)
                if hit and hit.group(1) == nonce:
                    cands.append(Candidate(
                        session_id=r["id"], parent_id=r["parent_id"],
                        title=r["title"], created_ms=r["time_created"],
                        matched_text=hit.group(0),
                        first_part_preview=text))
            else:
                hit = _MARKER.search(text)
                if hit and int(hit.group(1)) == attempt_id:
                    cands.append(Candidate(
                        session_id=r["id"], parent_id=r["parent_id"],
                        title=r["title"], created_ms=r["time_created"],
                        matched_text=hit.group(0),
                        first_part_preview=text))
        n = len(cands)
        decision = "unique" if n == 1 else ("none" if n == 0 else "ambiguous")
        if nonce is not None:
            basis = (
                "zcode subagent session whose first text part carries the "
                "attempt's one-time identity token "
                f"ORX_ASSIGNMENT={nonce} with session.directory equal to "
                "the project root and a non-null parent (subagent "
                "structure); read-only lookup, no writes"
            )
        else:
            basis = (
                "zcode subagent session whose first text part names "
                f"'attempt {attempt_id}' with session.directory equal to the "
                "project root and a non-null parent (subagent structure); "
                "read-only lookup, no writes"
            )
        return {
            "attempt_id": attempt_id,
            "project_root": root,
            "zcode_db": str(path),
            "decision": decision,
            "candidates": [c.payload() for c in cands],
            "basis": basis,
        }
    finally:
        conn.close()


def first_session_id(result: dict) -> str | None:
    """The single candidate's id, or None for none/ambiguous."""
    if result.get("decision") == "unique":
        return result["candidates"][0]["session_id"]
    return None
