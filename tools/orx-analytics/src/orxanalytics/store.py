"""The analytics tool's own database.

Everything here lives under the data dir (default ``~/.orx-analytics``) and
is ours to write. Source databases are only ever opened through
:mod:`orxanalytics.dbutil` read-only helpers.

Idempotency contract: every imported row lands in a table whose natural
primary key comes from the source (session id, thread id, composer id, ...) or
from an append-only ORX snapshot id. Re-running an import upserts the same
rows and changes nothing else, so repeated collects never double count.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STORE_VERSION = "4"

_RECEIPTS_TABLE = """
CREATE TABLE IF NOT EXISTS orx_receipts(
  snapshot_id INTEGER NOT NULL, id INTEGER NOT NULL, attempt_id INTEGER NOT NULL,
  revision_id INTEGER, task_id TEXT, delivery_status TEXT,
  delivery_digest TEXT, session_ref TEXT, actual_model TEXT,
  created_at TEXT,
  PRIMARY KEY(snapshot_id, id)
);
"""

# store v4: the zcode model_usage mirror (zc_requests) gains the per-request
# token columns the controller preview reads (G012 用户反馈 5). Additive only;
# the natural primary key (session_id, logical_request_id, attempt_index) is
# unchanged, so re-importing the same source rows upserts and never double
# counts.
_REQUEST_TOKEN_COLUMNS = (
    "input_tokens", "output_tokens", "reasoning_tokens",
    "cache_read", "cache_creation", "provider_total", "computed_total",
)


def _migrate_store(conn: sqlite3.Connection, from_version: str) -> None:
    """In-place additive upgrades of the analytics store itself. v1 -> v2:
    orx_attempts gains the ORX v11 `nonce` column (identity anchors).
    v2 -> v3: adds the `orx_receipts` mirror table (ORX v15 completion
    receipts). SCHEMA already creates it idempotently on open; the branch
    keeps the explicit step so a migrated store is provably complete.
    v3 -> v4: zc_requests gains the model_usage token columns; the zcode
    model_usage rowid cursor is reset so the next import re-reads already
    mirrored rows and backfills the new fields (upserts by natural key —
    repeated collection still never double counts)."""
    v = int(from_version)
    if v < 2:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(orx_attempts)")}
        if cols and "nonce" not in cols:
            conn.execute("ALTER TABLE orx_attempts ADD COLUMN nonce TEXT")
    if v < 3:
        conn.executescript(_RECEIPTS_TABLE)
    if v < 4:
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(zc_requests)")}
        for name in _REQUEST_TOKEN_COLUMNS:
            if cols and name not in cols:
                conn.execute(
                    f"ALTER TABLE zc_requests ADD COLUMN {name} INTEGER")
        # Backfill contract: rows already mirrored under v3 were read before
        # the token columns existed. Resetting the incremental cursor makes
        # the next import re-scan them; the natural-key upsert refreshes the
        # new fields in place and counts zero new inserts.
        conn.execute(
            "DELETE FROM import_state WHERE key='model_usage_rowid'")
    conn.execute(
        "UPDATE meta SET value=? WHERE key='store_version'", (STORE_VERSION,))
    conn.commit()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects(
  project_key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  orx_enabled INTEGER NOT NULL DEFAULT 1,
  registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources(
  source_id TEXT PRIMARY KEY, kind TEXT NOT NULL, location TEXT NOT NULL,
  reader_version TEXT NOT NULL, last_probe_json TEXT, last_probe_at TEXT
);
CREATE TABLE IF NOT EXISTS import_state(
  source_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY(source_id, key)
);
CREATE TABLE IF NOT EXISTS import_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL, counts_json TEXT, error TEXT
);
-- zcode mirror --------------------------------------------------------
CREATE TABLE IF NOT EXISTS zc_sessions(
  session_id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT,
  zcode_project_id TEXT, title TEXT,
  created_ms INTEGER, updated_ms INTEGER
);
CREATE TABLE IF NOT EXISTS zc_turns(
  session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
  status TEXT, started_ms INTEGER, completed_ms INTEGER, duration_ms INTEGER,
  model_request_count INTEGER, model_retry_count INTEGER,
  tool_call_count INTEGER, tool_error_count INTEGER,
  input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
  cache_read INTEGER, cache_creation INTEGER,
  PRIMARY KEY(session_id, turn_id)
);
CREATE TABLE IF NOT EXISTS zc_requests(
  session_id TEXT NOT NULL, logical_request_id TEXT NOT NULL,
  attempt_index INTEGER NOT NULL, turn_id TEXT, provider_id TEXT,
  model_id TEXT, status TEXT, started_ms INTEGER, completed_ms INTEGER,
  input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
  cache_read INTEGER, cache_creation INTEGER, provider_total INTEGER,
  computed_total INTEGER,
  PRIMARY KEY(session_id, logical_request_id, attempt_index)
);
CREATE TABLE IF NOT EXISTS zc_tools(
  tool_call_id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT,
  tool_name TEXT, status TEXT, started_ms INTEGER, completed_ms INTEGER,
  duration_ms INTEGER, exit_code INTEGER
);
CREATE TABLE IF NOT EXISTS zc_first_parts(
  session_id TEXT PRIMARY KEY, first_part_text TEXT
);
-- codex mirror --------------------------------------------------------
CREATE TABLE IF NOT EXISTS cx_threads(
  thread_id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, cwd TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS cx_turn_ctx(
  thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
  model TEXT, effort TEXT, cwd TEXT,
  PRIMARY KEY(thread_id, turn_id)
);
CREATE TABLE IF NOT EXISTS cx_usage(
  thread_id TEXT NOT NULL, response_id TEXT NOT NULL,
  turn_id TEXT, recorded_at TEXT,
  input_tokens INTEGER, cached_input INTEGER, cache_write INTEGER,
  output_tokens INTEGER, reasoning_output INTEGER, total_tokens INTEGER,
  PRIMARY KEY(thread_id, response_id)
);
CREATE TABLE IF NOT EXISTS cx_turn_events(
  thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, kind TEXT NOT NULL,
  at_epoch INTEGER, PRIMARY KEY(thread_id, turn_id, kind)
);
-- cursor mirror -------------------------------------------------------
CREATE TABLE IF NOT EXISTS cur_workspaces(
  workspace_id TEXT PRIMARY KEY, folder TEXT
);
CREATE TABLE IF NOT EXISTS cur_composers(
  composer_id TEXT PRIMARY KEY, workspace_id TEXT, name TEXT,
  created_ms INTEGER, updated_ms INTEGER, is_subagent INTEGER,
  subagent_type TEXT, usage_json TEXT, model_json TEXT
);
-- ORX snapshots (append-only per collect) ------------------------------
CREATE TABLE IF NOT EXISTS orx_snapshots(
  snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_key TEXT NOT NULL, taken_at TEXT NOT NULL,
  schema_version TEXT NOT NULL, source_path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orx_runs(
  snapshot_id INTEGER NOT NULL, id TEXT NOT NULL, goal_id TEXT, status TEXT,
  created_at TEXT, updated_at TEXT, started_at TEXT, completed_at TEXT,
  PRIMARY KEY(snapshot_id, id)
);
CREATE TABLE IF NOT EXISTS orx_goals(
  snapshot_id INTEGER NOT NULL, id TEXT NOT NULL, objective TEXT, status TEXT,
  context TEXT, constraints_json TEXT, acceptance_json TEXT,
  created_at TEXT, updated_at TEXT,
  PRIMARY KEY(snapshot_id, id)
);
CREATE TABLE IF NOT EXISTS orx_revisions(
  snapshot_id INTEGER NOT NULL, id INTEGER NOT NULL, run_id TEXT,
  revision INTEGER, depth INTEGER, planner_profile TEXT, status TEXT,
  created_at TEXT,
  PRIMARY KEY(snapshot_id, id)
);
CREATE TABLE IF NOT EXISTS orx_tasks(
  snapshot_id INTEGER NOT NULL, id INTEGER NOT NULL, revision_id INTEGER,
  task_id TEXT, objective TEXT, status TEXT, failure_reason TEXT,
  created_at TEXT, updated_at TEXT,
  PRIMARY KEY(snapshot_id, id)
);
CREATE TABLE IF NOT EXISTS orx_attempts(
  snapshot_id INTEGER NOT NULL, id INTEGER NOT NULL, revision_id INTEGER,
  task_id TEXT, assignment_id TEXT, role TEXT, profile TEXT, driver TEXT,
  harness TEXT, model TEXT, requested_effort TEXT, actual_effort TEXT,
  fallback_used INTEGER, started_at TEXT, ended_at TEXT, result TEXT,
  failure_reason TEXT, session_ref TEXT, run_id TEXT,
  usage_missing_reason TEXT, actual_model TEXT, model_source TEXT,
  nonce TEXT,
  PRIMARY KEY(snapshot_id, id)
);
-- Optional routing facts are additive to store v4. Older readers ignore them.
CREATE TABLE IF NOT EXISTS orx_routing_decisions(
  snapshot_id INTEGER NOT NULL, id INTEGER NOT NULL,
  attempt_id INTEGER, selected TEXT, reason TEXT, created_at TEXT,
  PRIMARY KEY(snapshot_id, id)
);
CREATE INDEX IF NOT EXISTS idx_orx_routing_attempt
  ON orx_routing_decisions(snapshot_id, attempt_id);
CREATE TABLE IF NOT EXISTS orx_verifications(
  snapshot_id INTEGER NOT NULL, id INTEGER NOT NULL, revision_id INTEGER,
  task_id TEXT, attempt_id INTEGER, kind TEXT, command TEXT,
  exit_code INTEGER, passed INTEGER, output_path TEXT, created_at TEXT,
  PRIMARY KEY(snapshot_id, id)
);
CREATE TABLE IF NOT EXISTS orx_usage_obs(
  snapshot_id INTEGER NOT NULL, id INTEGER NOT NULL, attempt_id INTEGER,
  profile TEXT, run_id TEXT, task_id TEXT, input_tokens INTEGER,
  output_tokens INTEGER, cached_input INTEGER, source TEXT, accuracy TEXT,
  created_at TEXT,
  PRIMARY KEY(snapshot_id, id)
);
CREATE TABLE IF NOT EXISTS orx_task_events(
  snapshot_id INTEGER NOT NULL, id INTEGER NOT NULL, revision_id INTEGER,
  task_id TEXT, from_status TEXT, to_status TEXT, event TEXT, reason TEXT,
  created_at TEXT,
  PRIMARY KEY(snapshot_id, id)
);
CREATE TABLE IF NOT EXISTS orx_evidence(
  snapshot_id INTEGER NOT NULL, id INTEGER NOT NULL, attempt_id INTEGER,
  kind TEXT, path TEXT, created_at TEXT,
  PRIMARY KEY(snapshot_id, id)
);
CREATE TABLE IF NOT EXISTS orx_replan_maps(
  snapshot_id INTEGER NOT NULL, id INTEGER NOT NULL, run_id TEXT,
  revision_id INTEGER, prior_revision INTEGER,
  PRIMARY KEY(snapshot_id, id)
);
CREATE TABLE IF NOT EXISTS orx_replan_task_maps(
  snapshot_id INTEGER NOT NULL, id INTEGER NOT NULL, mapping_id INTEGER,
  run_id TEXT, revision_id INTEGER, task_id TEXT, classification TEXT,
  redo_reason TEXT,
  PRIMARY KEY(snapshot_id, id)
);
CREATE TABLE IF NOT EXISTS orx_replan_sources(
  snapshot_id INTEGER NOT NULL, id INTEGER NOT NULL, task_mapping_id INTEGER,
  run_id TEXT, source_revision INTEGER, source_task_id TEXT,
  source_task_row_id INTEGER, part INTEGER,
  PRIMARY KEY(snapshot_id, id)
);
CREATE TABLE IF NOT EXISTS orx_replan_superseded(
  snapshot_id INTEGER NOT NULL, id INTEGER NOT NULL, mapping_id INTEGER,
  run_id TEXT, source_revision INTEGER, source_task_id TEXT,
  disposition TEXT, successors_json TEXT, note TEXT,
  PRIMARY KEY(snapshot_id, id)
);
CREATE TABLE IF NOT EXISTS orx_attempt_progress(
  snapshot_id INTEGER NOT NULL, id INTEGER NOT NULL, attempt_id INTEGER,
  sequence INTEGER, phase TEXT, message TEXT, received_at TEXT,
  PRIMARY KEY(snapshot_id, id)
);
-- ORX v15 completion receipts (G010 T005): replay surfaces keyed by the
-- UNIQUE attempt_id — one receipt per accepted delivery, never a re-run
-- instruction. receipt_json (the verbatim replay payload) is deliberately
-- NOT mirrored: analytics displays receipt facts, it never replays them.
CREATE TABLE IF NOT EXISTS orx_receipts(
  snapshot_id INTEGER NOT NULL, id INTEGER NOT NULL, attempt_id INTEGER NOT NULL,
  revision_id INTEGER, task_id TEXT, delivery_status TEXT,
  delivery_digest TEXT, session_ref TEXT, actual_model TEXT,
  created_at TEXT,
  PRIMARY KEY(snapshot_id, id)
);
-- correlation ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS links(
  project_key TEXT NOT NULL, attempt_id INTEGER NOT NULL,
  harness TEXT NOT NULL, session_id TEXT NOT NULL,
  authority TEXT NOT NULL,        -- stored | inferred
  basis TEXT, evidence_json TEXT, confidence TEXT,
  computed_at TEXT NOT NULL, superseded_by INTEGER,
  PRIMARY KEY(project_key, attempt_id, harness)
);
-- findings (phase 4) ----------------------------------------------------
CREATE TABLE IF NOT EXISTS findings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT NOT NULL UNIQUE,
  rule TEXT NOT NULL, project_key TEXT, title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',   -- open|merged|linked|dismissed
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  payload_json TEXT NOT NULL, merged_into INTEGER
);
CREATE TABLE IF NOT EXISTS finding_refs(
  finding_id INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
  PRIMARY KEY(finding_id, kind, ref)
);
CREATE TABLE IF NOT EXISTS finding_fixes(
  finding_id INTEGER NOT NULL, fix_ref TEXT NOT NULL,
  verified_at TEXT, before_json TEXT, after_json TEXT, comparable INTEGER,
  note TEXT, PRIMARY KEY(finding_id, fix_ref)
);
"""

DEFAULT_DATA_DIR = Path.home() / ".orx-analytics"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# store versions this code can upgrade in place; anything else refuses
_MIGRATABLE_FROM = {"1", "2", "3"}


class Store:
    def __init__(self, data_dir: Path | None = None):
        import os
        if data_dir is None:
            env = os.environ.get("ORX_ANALYTICS_HOME")
            data_dir = Path(env) if env else DEFAULT_DATA_DIR
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "analytics.db"
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        meta = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        row = (self.conn.execute(
            "SELECT value FROM meta WHERE key='store_version'").fetchone() if meta else None)
        version = row["value"] if row else None
        if row and version != STORE_VERSION and version not in _MIGRATABLE_FROM:
            self.conn.close()
            raise RuntimeError(
                f"analytics.db store_version {version} != {STORE_VERSION}; "
                "this reader cannot open it")
        self.conn.executescript(SCHEMA)
        if row is None:
            self.conn.execute(
                "INSERT INTO meta VALUES ('store_version', ?)", (STORE_VERSION,))
            self.conn.commit()
        elif version in _MIGRATABLE_FROM:
            _migrate_store(self.conn, version)

    # -- generic helpers -------------------------------------------------
    def upsert(self, table: str, row: dict[str, Any],
               pk: list[str]) -> str:
        """Idempotent insert-or-replace by natural key. Returns the action."""
        cols = list(row.keys())
        exists = self.conn.execute(
            f"SELECT 1 FROM {table} WHERE "
            + " AND ".join(f"{k}=?" for k in pk),
            [row[k] for k in pk]).fetchone()
        ph = ",".join("?" for _ in cols)
        self.conn.execute(
            f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES ({ph})",
            [row[c] for c in cols])
        return "unchanged" if exists else "inserted"

    def get_state(self, source_id: str, key: str) -> str | None:
        r = self.conn.execute(
            "SELECT value FROM import_state WHERE source_id=? AND key=?",
            (source_id, key)).fetchone()
        return r["value"] if r else None

    def set_state(self, source_id: str, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO import_state VALUES (?,?,?)",
            (source_id, key, value))

    def begin_import(self, source_id: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO import_runs(source_id, started_at, status) "
            "VALUES (?,?,'running')", (source_id, now_iso()))
        self.conn.commit()
        return cur.lastrowid

    def snapshot_keep(self) -> int:
        raw = os.environ.get("ORX_ANALYTICS_SNAPSHOT_KEEP", "1")
        try:
            keep = int(raw)
        except ValueError as exc:
            raise ValueError("ORX_ANALYTICS_SNAPSHOT_KEEP must be a positive integer") from exc
        if keep < 1:
            raise ValueError("ORX_ANALYTICS_SNAPSHOT_KEEP must be a positive integer")
        return keep

    def prune_snapshots(self, project_key: str, *,
                        current_snapshot: int | None = None) -> None:
        """Prune every snapshot-keyed mirror inside the caller's transaction.

        A new snapshot is eligible only after all source tables were read.
        Existing snapshots must have a successful import receipt. Failed
        partial imports are removed only after a complete replacement exists.
        """
        keep = self.snapshot_keep()
        existing = {r[0] for r in self.conn.execute(
            "SELECT snapshot_id FROM orx_snapshots WHERE project_key=?",
            (project_key,))}
        retained = set()
        if current_snapshot is not None:
            if current_snapshot not in existing:
                raise ValueError("current snapshot does not belong to project")
            retained.add(current_snapshot)
        rows = self.conn.execute(
            "SELECT counts_json FROM import_runs WHERE source_id=? "
            "AND status='done' ORDER BY id DESC", ("orx:" + project_key,))
        for row in rows:
            if len(retained) >= keep:
                break
            sid = json.loads(row[0] or "{}").get("snapshot_id")
            if sid in existing:
                retained.add(sid)
        if not retained:
            return  # never remove the only available data without a success
        obsolete = existing - retained
        tables = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name GLOB 'orx_*'")]
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            if "snapshot_id" not in {r[1] for r in self.conn.execute(
                    f"PRAGMA table_info({quoted})")}:
                continue
            self.conn.executemany(
                f"DELETE FROM {quoted} WHERE snapshot_id=?",
                ((sid,) for sid in obsolete))

    def prune_import_logs(self, source_id: str) -> None:
        """Keep bounded diagnostics plus the last success and live imports."""
        keep = max(100, self.snapshot_keep())
        self.conn.execute(
            "DELETE FROM import_runs WHERE source_id=? AND status!='running' "
            "AND id NOT IN (SELECT id FROM import_runs WHERE source_id=? "
            "AND status!='running' ORDER BY id DESC LIMIT ?) "
            "AND id NOT IN (SELECT id FROM import_runs WHERE source_id=? "
            "AND status='done' ORDER BY id DESC LIMIT ?)",
            (source_id, source_id, keep, source_id, self.snapshot_keep()))

    def finish_import(self, run_id: int, counts: dict[str, Any],
                      error: str | None = None) -> None:
        status = "error" if error else "done"
        self.conn.execute(
            "UPDATE import_runs SET finished_at=?, status=?, counts_json=?, "
            "error=? WHERE id=?",
            (now_iso(), status, json.dumps(counts), error, run_id))
        source = self.conn.execute(
            "SELECT source_id FROM import_runs WHERE id=?", (run_id,)).fetchone()
        if source:
            self.prune_import_logs(source[0])
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
