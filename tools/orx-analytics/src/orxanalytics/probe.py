"""Source capability probes.

A probe answers one question: *what can this tool honestly read from this
source on this machine, right now?* Results are recorded, not assumed —
`docs/source-probes.md` holds the verified baseline. A failing probe is a
reportable gap, never a fake pass.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import READER_VERSION
from .dbutil import open_ro

ZCODE_DB = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"
CODEX_HOME = Path.home() / ".codex"
CODEX_SESSIONS = CODEX_HOME / "sessions"
CURSOR_GLOBAL = (
    Path.home() / "Library" / "Application Support" / "Cursor" / "user"
    / "globalStorage" / "state.vscdb"
)

# The ORX observability contract gate (ORX repo docs/observability-contract.md):
# accept exactly these schema versions, refuse everything else. orxstate.py
# imports this constant so the probe and the importer can never disagree.
# v13 (goals.status +paused), v14 (+cancelling) and v15 (+completion_receipts)
# are additive amendments recorded 2026-10-16 (G010); all three keep the
# v11/v12 shape on every pre-existing table.
SUPPORTED_ORX_SCHEMA = {"8", "9", "10", "11", "12", "13", "14", "15"}

ZCODE_TABLE_FIELDS: dict[str, list[str]] = {
    "session": ["id", "parent_id", "directory", "project_id", "title",
                "time_created", "time_updated"],
    "turn_usage": ["session_id", "turn_id", "status", "started_at", "completed_at",
                   "duration_ms", "model_request_count", "model_retry_count",
                   "tool_call_count", "tool_error_count", "input_tokens",
                   "output_tokens", "reasoning_tokens",
                   "cache_read_input_tokens", "cache_creation_input_tokens"],
    "model_usage": ["logical_request_id", "attempt_index", "session_id", "turn_id",
                    "provider_id", "model_id", "status", "started_at", "completed_at",
                    # G012: per-request token columns mirrored into
                    # zc_requests (store v4); a source lacking them still
                    # imports — the gap is reported here, never guessed.
                    "input_tokens", "output_tokens", "reasoning_tokens",
                    "cache_read_input_tokens", "cache_creation_input_tokens",
                    "provider_total_tokens", "computed_total_tokens"],
    "tool_usage": ["id", "session_id", "turn_id", "tool_name", "status",
                   "started_at", "completed_at", "duration_ms", "exit_code"],
    "part": ["id", "message_id", "session_id", "data", "sequence"],
}

CODEX_EVENT_TYPES = ["session_meta", "turn_context", "token_usage_record",
                     "event_msg"]


@dataclass
class ProbeResult:
    kind: str
    ok: bool
    detail: str
    fields: dict[str, Any] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ok": self.ok,
            "detail": self.detail,
            "fields": self.fields,
            "gaps": self.gaps,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def probe_zcode(db: Path = ZCODE_DB) -> ProbeResult:
    if not db.exists():
        return ProbeResult("zcode", False, f"no database at {db}",
                           gaps=["zcode database absent"])
    try:
        conn = open_ro(db)
    except sqlite3.Error as e:
        return ProbeResult("zcode", False, f"cannot open read-only: {e}",
                           gaps=["unreadable database"])
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in ZCODE_TABLE_FIELDS if t not in tables]
        cols_missing: dict[str, list[str]] = {}
        counts: dict[str, int] = {}
        for t, want in ZCODE_TABLE_FIELDS.items():
            if t not in tables:
                continue
            have = {c[1] for c in conn.execute(f"PRAGMA table_info({t})")}
            cols_missing[t] = [c for c in want if c not in have]
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        ok = not missing and not any(cols_missing.values())
        detail = (f"{len(counts)} tables readable"
                  if ok else f"missing tables={missing} cols={cols_missing}")
        gaps = []
        if missing:
            gaps.append(f"tables absent: {missing}")
        for t, cm in cols_missing.items():
            if cm:
                gaps.append(f"{t} missing columns: {cm}")
        return ProbeResult("zcode", ok, detail, fields={"counts": counts},
                           gaps=gaps)
    finally:
        conn.close()


def probe_codex(sessions: Path = CODEX_SESSIONS) -> ProbeResult:
    if not sessions.is_dir():
        return ProbeResult("codex", False, f"no sessions dir at {sessions}",
                           gaps=["codex sessions directory absent"])
    rollouts = sorted(sessions.rglob("rollout-*.jsonl"))
    if not rollouts:
        return ProbeResult("codex", False, "no rollout files",
                           gaps=["no rollout-*.jsonl under sessions/"])
    seen: set[str] = set()
    checked = 0
    bad = 0
    for f in rollouts[-50:]:
        checked += 1
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        j = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ty = j.get("type")
                    if ty in CODEX_EVENT_TYPES:
                        seen.add(ty)
                    if ty == "event_msg":
                        seen.add(f"event_msg:{(j.get('payload') or {}).get('type')}")
                    if len(seen) >= 8 and checked >= 10:
                        break
        except OSError:
            bad += 1
    want = {"session_meta", "turn_context", "token_usage_record"}
    ok = want.issubset(seen)
    gaps = []
    if not ok:
        gaps.append(f"event types not observed: {sorted(want - seen)}")
    if bad:
        gaps.append(f"{bad} recent files unreadable")
    return ProbeResult(
        "codex", ok,
        f"{len(rollouts)} rollout files, {checked} recent parsed, events={sorted(seen)[:8]}",
        fields={"rollout_files": len(rollouts)}, gaps=gaps)


def probe_cursor(db: Path = CURSOR_GLOBAL) -> ProbeResult:
    if not db.exists():
        return ProbeResult("cursor", False, f"no globalStorage db at {db}",
                           gaps=["cursor globalStorage absent"])
    try:
        conn = open_ro(db)
    except sqlite3.Error as e:
        return ProbeResult("cursor", False, f"cannot open read-only: {e}",
                           gaps=["unreadable database"])
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        gaps: list[str] = []
        fields: dict[str, Any] = {}
        if "composerHeaders" in tables:
            fields["composer_headers"] = conn.execute(
                "SELECT COUNT(*) FROM composerHeaders").fetchone()[0]
        else:
            gaps.append("composerHeaders table absent")
        if "cursorDiskKV" in tables:
            fields["disk_kv_rows"] = conn.execute(
                "SELECT COUNT(*) FROM cursorDiskKV").fetchone()[0]
            for prefix in ("composerData:", "bubbleData:"):
                fields[prefix.rstrip(":")] = conn.execute(
                    "SELECT COUNT(*) FROM cursorDiskKV WHERE key LIKE ?",
                    (prefix + "%",)).fetchone()[0]
            if fields.get("bubbleData") == 0:
                gaps.append("bubbleData empty in globalStorage on this machine; "
                            "per-message detail may live elsewhere or be absent")
        else:
            gaps.append("cursorDiskKV table absent")
        ok = "composerHeaders" in tables and "cursorDiskKV" in tables
        return ProbeResult("cursor", ok,
                           f"tables={sorted(tables)} fields={fields}",
                           fields=fields, gaps=gaps)
    finally:
        conn.close()


def probe_orx(project_root: Path) -> ProbeResult:
    db = project_root / ".orx" / "state.db"
    if not db.exists():
        return ProbeResult("orx", False, f"no .orx/state.db under {project_root}",
                           gaps=["project is not an ORX project (basic session "
                                 "analysis only)"])
    try:
        conn = open_ro(db)
    except sqlite3.Error as e:
        return ProbeResult("orx", False, f"cannot open read-only: {e}",
                           gaps=["unreadable state.db"])
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            return ProbeResult("orx", False, "no schema_version row",
                               gaps=["schema gate: refuse (missing version)"])
        sv = row[0]
        ok = sv in SUPPORTED_ORX_SCHEMA
        gaps = [] if ok else [
            f"schema gate refuses version {sv} (supported: "
            + ", ".join(sorted(SUPPORTED_ORX_SCHEMA, key=int)) + ")"]
        try:
            runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        except sqlite3.Error:
            runs = 0
            gaps.append("runs table unreadable")
        return ProbeResult("orx", ok,
                           f"schema_version={sv}, runs={runs}",
                           fields={"schema_version": sv, "runs": runs},
                           gaps=gaps)
    finally:
        conn.close()


def probe_all(project_roots: list[Path] | None = None) -> dict[str, Any]:
    out = {
        "probed_at": _now(),
        "reader_version": READER_VERSION,
        "results": [
            probe_zcode().as_dict(),
            probe_codex().as_dict(),
            probe_cursor().as_dict(),
        ],
        "orx_projects": [
            probe_orx(Path(p).expanduser()).as_dict() for p in (project_roots or [])
        ],
    }
    return out
