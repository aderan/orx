"""Read-only, bounded audit of the reported ORX/ZCode incident.

Prints derived counters and selected non-secret evidence, not transcripts,
credentials, raw provider metadata, or arbitrary user configuration.
"""

from __future__ import annotations

import collections
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SID = "sess_7c667fa0-9206-4ffc-bd55-8d6b0f19b4ee"


def readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def usage(rows: list[dict]) -> dict:
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "computed_total_tokens")
    return {
        "requests": len(rows),
        "status_counts": dict(collections.Counter(row["status"] for row in rows)),
        **{key: sum(row[key] for row in rows) for key in keys},
        "uncached_input_tokens": sum(row["input_tokens"] - row["cache_read_input_tokens"] for row in rows),
        "max_request_input_tokens": max((row["input_tokens"] for row in rows), default=0),
        "mean_request_input_tokens": sum(row["input_tokens"] for row in rows) / max(len(rows), 1),
    }


def audit() -> dict:
    zcode = readonly(Path.home() / ".zcode/cli/db/db.sqlite")
    orx = readonly(ROOT / ".orx/state.db")
    try:
        sessions = [SID] + [row[0] for row in zcode.execute(
            "WITH RECURSIVE tree(id) AS ("
            " SELECT id FROM session WHERE parent_id=?"
            " UNION ALL SELECT s.id FROM session s JOIN tree t ON s.parent_id=t.id)"
            " SELECT id FROM tree", (SID,),
        )]
        rows_by_session = {
            session: [dict(row) for row in zcode.execute(
                "SELECT * FROM model_usage WHERE session_id=?", (session,),
            )] for session in sessions
        }
        attempts = [dict(row) for row in orx.execute(
            "SELECT * FROM attempts WHERE run_id IN ('R007','R008') ORDER BY id"
        )]
        observed = [dict(row) for row in orx.execute(
            "SELECT * FROM usage_observations WHERE run_id IN ('R007','R008') ORDER BY id"
        )]
        incident = []
        for attempt in attempts:
            if not 130 <= attempt["id"] <= 135:
                continue
            checks = [dict(row) for row in orx.execute(
                "SELECT id,command,exit_code,passed,output_path,created_at FROM verifications"
                " WHERE attempt_id=? ORDER BY id", (attempt["id"],),
            )]
            item = {key: attempt[key] for key in (
                "id", "profile", "session_ref", "started_at", "ended_at", "result", "failure_reason",
            )}
            item["checks"] = checks
            item["zcode_usage"] = usage(rows_by_session.get(attempt["session_ref"], []))
            if attempt["harness"] == "cursor":
                log = (ROOT / f'.orx/runs/R008/exec/T003-{attempt["id"]}.log').read_text()
                envelope = json.loads(next(
                    line for line in reversed(log.splitlines())
                    if line.startswith('{"type":"result"')
                ))
                item["cli_result"] = {key: envelope[key] for key in (
                    "type", "subtype", "is_error", "result", "usage",
                )}
                session = attempt["session_ref"]
                transcript = Path.home() / ".cursor/projects/Users-flb-Sources-Products-ORX/agent-transcripts" / session / f"{session}.jsonl"
                item["cursor_transcript_exists"] = transcript.is_file()
            incident.append(item)
        role_usage = {}
        for run in ("R007", "R008"):
            for role in ("worker", "verifier"):
                matching = [attempt for attempt in attempts if attempt["run_id"] == run
                            and attempt["role"] == role and attempt["harness"] == "zcode"]
                role_usage[f"{run}:{role}"] = usage([
                    row for attempt in matching for row in rows_by_session.get(attempt["session_ref"], [])
                ])
        return {
            "session_id": SID,
            "zcode_session_count": len(sessions),
            "orx_attempt_count": len(attempts),
            "orx_observed_attempt_count": len({row["attempt_id"] for row in observed}),
            "root": usage(rows_by_session[SID]),
            "children": usage([row for session in sessions[1:] for row in rows_by_session[session]]),
            "zcode_role_usage": role_usage,
            "native_cli_observations": observed,
            "incident": incident,
        }
    finally:
        zcode.close()
        orx.close()


if __name__ == "__main__":
    print(json.dumps(audit(), indent=2, ensure_ascii=False))
