"""Importers: read-only pulls from each source into the analytics store.

Every importer:
- opens the source through dbutil (mode=ro, query_only), or reads files
  directly for JSONL sources;
- upserts by natural primary keys so repeated runs never double count;
- records an import_runs row with counts and leaves cursor state for the
  next incremental pass;
- never writes, migrates, checkpoints, or deletes anything in the source.
"""

from __future__ import annotations

from ..probe import CURSOR_GLOBAL, ZCODE_DB, CODEX_SESSIONS
from ..dbutil import open_ro
from ..store import Store

import json
from pathlib import Path

# zcode model_usage token/metadata columns mirrored into zc_requests (store
# v4). The source schema varies between installs: a column the installed
# database lacks is mirrored as NULL and named in the import result's
# `model_usage_columns_missing` — never guessed, never zero-filled.
_MODEL_USAGE_TOKEN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("reasoning_tokens", "reasoning_tokens"),
    ("cache_read_input_tokens", "cache_read"),
    ("cache_creation_input_tokens", "cache_creation"),
    ("provider_total_tokens", "provider_total"),
    ("computed_total_tokens", "computed_total"),
)


def import_zcode(store: Store, db: Path = ZCODE_DB) -> dict:
    source_id = f"zcode:{db}"
    run = store.begin_import(source_id)
    counts = {"sessions": 0, "turns": 0, "requests": 0, "tools": 0,
              "first_parts": 0}
    try:
        conn = open_ro(db)
        try:
            for r in conn.execute(
                "SELECT id, parent_id, directory, project_id, title, "
                "time_created, time_updated FROM session"
            ):
                a = store.upsert("zc_sessions", {
                    "session_id": r["id"], "parent_id": r["parent_id"],
                    "directory": r["directory"],
                    "zcode_project_id": r["project_id"],
                    "title": r["title"],
                    "created_ms": r["time_created"],
                    "updated_ms": r["time_updated"]},
                    ["session_id"])
                counts["sessions"] += a == "inserted"
            for r in conn.execute(
                "SELECT session_id, turn_id, status, started_at, completed_at, "
                "duration_ms, model_request_count, model_retry_count, "
                "tool_call_count, tool_error_count, input_tokens, "
                "output_tokens, reasoning_tokens, cache_read_input_tokens, "
                "cache_creation_input_tokens FROM turn_usage"
            ):
                a = store.upsert("zc_turns", {
                    "session_id": r["session_id"], "turn_id": r["turn_id"],
                    "status": r["status"],
                    "started_ms": r["started_at"],
                    "completed_ms": r["completed_at"],
                    "duration_ms": r["duration_ms"],
                    "model_request_count": r["model_request_count"],
                    "model_retry_count": r["model_retry_count"],
                    "tool_call_count": r["tool_call_count"],
                    "tool_error_count": r["tool_error_count"],
                    "input_tokens": r["input_tokens"],
                    "output_tokens": r["output_tokens"],
                    "reasoning_tokens": r["reasoning_tokens"],
                    "cache_read": r["cache_read_input_tokens"],
                    "cache_creation": r["cache_creation_input_tokens"]},
                    ["session_id", "turn_id"])
                counts["turns"] += a == "inserted"
            cur_req = int(store.get_state(source_id, "model_usage_rowid") or 0)
            max_req = cur_req
            mu_cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(model_usage)")}
            token_cols = [src for src, _dst in _MODEL_USAGE_TOKEN_COLUMNS
                          if src in mu_cols]
            missing_cols = [src for src, _dst in _MODEL_USAGE_TOKEN_COLUMNS
                            if src not in mu_cols]
            req_sql = (
                "SELECT rowid, logical_request_id, attempt_index, session_id, "
                "turn_id, provider_id, model_id, status, started_at, "
                "completed_at"
                + (", " + ", ".join(token_cols) if token_cols else "")
                + " FROM model_usage WHERE rowid > ? ORDER BY rowid")
            for r in conn.execute(req_sql, (cur_req,)):
                max_req = max(max_req, r["rowid"])
                row = {
                    "session_id": r["session_id"],
                    "logical_request_id": r["logical_request_id"],
                    "attempt_index": r["attempt_index"],
                    "turn_id": r["turn_id"], "provider_id": r["provider_id"],
                    "model_id": r["model_id"], "status": r["status"],
                    "started_ms": r["started_at"],
                    "completed_ms": r["completed_at"]}
                for src, dst in _MODEL_USAGE_TOKEN_COLUMNS:
                    row[dst] = r[src] if src in mu_cols else None
                a = store.upsert("zc_requests", row,
                                 ["session_id", "logical_request_id",
                                  "attempt_index"])
                counts["requests"] += a == "inserted"
            if missing_cols:
                counts["model_usage_columns_missing"] = missing_cols
            store.set_state(source_id, "model_usage_rowid", str(max_req))

            cur_tool = int(store.get_state(source_id, "tool_usage_rowid") or 0)
            max_tool = cur_tool
            for r in conn.execute(
                "SELECT rowid, id, session_id, turn_id, tool_name, status, "
                "started_at, completed_at, duration_ms, exit_code "
                "FROM tool_usage WHERE rowid > ? ORDER BY rowid", (cur_tool,)
            ):
                max_tool = max(max_tool, r["rowid"])
                a = store.upsert("zc_tools", {
                    "tool_call_id": r["id"], "session_id": r["session_id"],
                    "turn_id": r["turn_id"], "tool_name": r["tool_name"],
                    "status": r["status"], "started_ms": r["started_at"],
                    "completed_ms": r["completed_at"],
                    "duration_ms": r["duration_ms"],
                    "exit_code": r["exit_code"]}, ["tool_call_id"])
                counts["tools"] += a == "inserted"
            store.set_state(source_id, "tool_usage_rowid", str(max_tool))

            # First text part per session: the correlation anchor. Only for
            # sessions not anchored yet (bounded work per round).
            have = {r["session_id"] for r in store.conn.execute(
                "SELECT session_id FROM zc_first_parts")}
            todo = [r["session_id"] for r in store.conn.execute(
                "SELECT session_id FROM zc_sessions") if
                r["session_id"] not in have]
            for sid in todo[:2000]:
                r = conn.execute(
                    "SELECT data FROM part WHERE session_id=? AND "
                    "data LIKE '{\"type\":\"text\"%' ORDER BY sequence "
                    "LIMIT 1", (sid,)).fetchone()
                text = None
                if r is not None:
                    try:
                        text = json.loads(r["data"]).get("text", "")[:2000]
                    except json.JSONDecodeError:
                        text = None
                store.conn.execute(
                    "INSERT OR REPLACE INTO zc_first_parts VALUES (?,?)",
                    (sid, text))
                counts["first_parts"] += 1
            store.conn.commit()
        finally:
            conn.close()
        store.finish_import(run, counts)
        return {"source": source_id, "counts": counts}
    except Exception as e:  # noqa: BLE001 - recorded, never swallowed
        store.conn.rollback()   # keep a failed pass atomic
        store.finish_import(run, counts, error=str(e))
        return {"source": source_id, "error": str(e)}


def import_codex(store: Store, sessions_dir: Path = CODEX_SESSIONS) -> dict:
    source_id = f"codex:{sessions_dir}"
    run = store.begin_import(source_id)
    counts = {"threads": 0, "turn_ctx": 0, "usage": 0, "events": 0,
              "bytes_read": 0}
    try:
        files = sorted(sessions_dir.rglob("rollout-*.jsonl"))
        for f in files:
            key = f"offset:{f}"
            offset = int(store.get_state(source_id, key) or 0)
            size = f.stat().st_size
            if offset > size:      # file was truncated/rotated: re-read whole
                offset = 0
            if offset == size:
                continue
            # One thread may appear in several rollout files (a resumed
            # session). The file->thread mapping lives in import_state; the
            # thread row itself is keyed only by thread id.
            mapped = store.get_state(source_id, f"thread:{f}")
            thread_id = mapped
            if thread_id is None:
                row = store.conn.execute(
                    "SELECT thread_id FROM cx_threads WHERE rollout_path=?",
                    (str(f),)).fetchone()
                thread_id = row["thread_id"] if row else None
            with open(f, "rb") as fh:
                fh.seek(offset)
                buf = fh.read()
            if not buf:
                store.set_state(source_id, key, str(size))
                continue
            consumed = 0
            pos = 0
            while True:
                nl = buf.find(b"\n", pos)
                if nl == -1:
                    break        # torn tail: wait for the rest, do not advance
                line = buf[pos:nl].rstrip(b"\r")
                pos = nl + 1
                consumed = pos
                counts["bytes_read"] += len(line) + 1
                try:
                    j = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue    # a complete but unusable line: skip it
                ty = j.get("type")
                p = j.get("payload") or {}
                ts = j.get("timestamp")
                if ty == "session_meta":
                    thread_id = p.get("id") or p.get("session_id")
                    if thread_id:
                        cwd = p.get("cwd")
                        cur = store.conn.execute(
                            "INSERT OR IGNORE INTO cx_threads(thread_id, "
                            "rollout_path, cwd, created_at, updated_at) "
                            "VALUES (?,?,?,?,?)",
                            (thread_id, str(f), cwd, ts, ts))
                        counts["threads"] += cur.rowcount == 1
                        store.conn.execute(
                            "UPDATE cx_threads SET updated_at=? "
                            "WHERE thread_id=?", (ts, thread_id))
                        store.set_state(source_id, f"thread:{f}", thread_id)
                elif thread_id is None:
                    continue
                elif ty == "turn_context":
                    tid = p.get("turn_id")
                    if tid:
                        a = store.upsert("cx_turn_ctx", {
                            "thread_id": thread_id, "turn_id": tid,
                            "model": p.get("model"),
                            "effort": p.get("effort"),
                            "cwd": p.get("cwd")},
                            ["thread_id", "turn_id"])
                        counts["turn_ctx"] += a == "inserted"
                elif ty == "token_usage_record":
                    u = p.get("usage") or {}
                    rid = p.get("response_id") or (
                        f"{p.get('turn_id')}:{j.get('ordinal')}")
                    a = store.upsert("cx_usage", {
                        "thread_id": thread_id, "response_id": rid,
                        "turn_id": p.get("turn_id"), "recorded_at": ts,
                        "input_tokens": u.get("input_tokens"),
                        "cached_input": u.get("cached_input_tokens"),
                        "cache_write": u.get("cache_write_input_tokens"),
                        "output_tokens": u.get("output_tokens"),
                        "reasoning_output":
                            u.get("reasoning_output_tokens"),
                        "total_tokens": u.get("total_tokens")},
                        ["thread_id", "response_id"])
                    counts["usage"] += a == "inserted"
                elif ty == "event_msg":
                    et = p.get("type")
                    if et in ("task_started", "task_complete"):
                        tid = p.get("turn_id") or p.get("root_turn_id")
                        at = p.get("started_at") or p.get("completed_at")
                        if tid and at:
                            a = store.upsert("cx_turn_events", {
                                "thread_id": thread_id, "turn_id": tid,
                                "kind": et, "at_epoch": at},
                                ["thread_id", "turn_id", "kind"])
                            counts["events"] += a == "inserted"
            store.set_state(source_id, key, str(offset + consumed))
        store.conn.commit()
        store.finish_import(run, counts)
        return {"source": source_id, "counts": counts}
    except Exception as e:  # noqa: BLE001
        store.conn.rollback()   # keep a failed pass atomic
        store.finish_import(run, counts, error=str(e))
        return {"source": source_id, "error": str(e)}


_COMPOSER_UPSERT = """
INSERT INTO cur_composers(composer_id, workspace_id, name, created_ms,
                          updated_ms, is_subagent, subagent_type,
                          usage_json, model_json)
VALUES (:composer_id, :workspace_id, NULL, :created_ms, :updated_ms,
        :is_subagent, :subagent_type, NULL, NULL)
ON CONFLICT(composer_id) DO UPDATE SET
  workspace_id=excluded.workspace_id,
  created_ms=excluded.created_ms,
  updated_ms=excluded.updated_ms,
  is_subagent=excluded.is_subagent,
  subagent_type=excluded.subagent_type
"""


def import_cursor(store: Store, db: Path = CURSOR_GLOBAL) -> dict:
    source_id = f"cursor:{db}"
    run = store.begin_import(source_id)
    counts = {"workspaces": 0, "headers": 0, "composers_inserted": 0,
              "composers_refreshed": 0}
    try:
        conn = open_ro(db)
        try:
            headers = conn.execute(
                "SELECT composerId, workspaceId, createdAt, lastUpdatedAt, "
                "isSubagent, subagentTypeName FROM composerHeaders"
            ).fetchall()
            # Decide which payloads need a refresh BEFORE the header upsert
            # moves updated_ms: new ids, or ids whose lastUpdatedAt moved.
            stored = {r["composer_id"]: r["updated_ms"] for r in
                      store.conn.execute(
                          "SELECT composer_id, updated_ms FROM cur_composers")}
            refresh: list[tuple[str, int | None]] = []
            for h in headers:
                cid = h["composerId"]
                ums = int(h["lastUpdatedAt"]) if h["lastUpdatedAt"] else None
                if cid not in stored or stored[cid] != ums:
                    refresh.append((cid, ums))
            for h in headers:
                was_new = h["composerId"] not in stored
                store.conn.execute(_COMPOSER_UPSERT, {
                    "composer_id": h["composerId"],
                    "workspace_id": h["workspaceId"],
                    "created_ms": int(h["createdAt"]) if h["createdAt"]
                    else None,
                    "updated_ms": int(h["lastUpdatedAt"])
                    if h["lastUpdatedAt"] else None,
                    "is_subagent": 1 if h["isSubagent"] else 0,
                    "subagent_type": h["subagentTypeName"] or None})
                counts["headers"] += 1
                counts["composers_inserted"] += was_new
            # composerData values are large blobs in a 1.36 GB database:
            # fetch one key at a time and only for the refresh set.
            for cid, _ums in refresh:
                r = conn.execute(
                    "SELECT value FROM cursorDiskKV WHERE key=?",
                    (f"composerData:{cid}",)).fetchone()
                if r is None:
                    continue
                raw = r["value"]
                try:
                    v = json.loads(raw) if isinstance(raw, str) else \
                        json.loads(raw.decode("utf-8", "replace"))
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                    continue
                usage = v.get("usageData")
                model = v.get("modelConfig")
                store.conn.execute(
                    "UPDATE cur_composers SET name=?, usage_json=?, "
                    "model_json=? WHERE composer_id=?",
                    (v.get("name"),
                     json.dumps(usage, ensure_ascii=False) if usage else None,
                     json.dumps(model, ensure_ascii=False) if model else None,
                     cid))
                counts["composers_refreshed"] += 1
        finally:
            conn.close()

        ws_root = db.parent.parent / "workspaceStorage"
        if ws_root.is_dir():
            for d in ws_root.iterdir():
                wj = d / "workspace.json"
                if not wj.is_file():
                    continue
                try:
                    folder = json.loads(wj.read_text()).get("folder", "")
                except (json.JSONDecodeError, OSError):
                    continue
                a = store.upsert("cur_workspaces", {
                    "workspace_id": d.name, "folder": folder},
                    ["workspace_id"])
                counts["workspaces"] += a == "inserted"
        store.conn.commit()
        store.finish_import(run, counts)
        return {"source": source_id, "counts": counts}
    except Exception as e:  # noqa: BLE001
        store.conn.rollback()   # keep a failed pass atomic
        store.finish_import(run, counts, error=str(e))
        return {"source": source_id, "error": str(e)}
