"""ORX per-project state.db importer.

Follows docs/observability-contract.md of the ORX repo: schema gate accepts
exactly {8..15} (single source: probe.SUPPORTED_ORX_SCHEMA); named columns
only; read-only in place. Each collect appends one snapshot per project;
successful snapshots are retained according to the store retention policy.
"""

from __future__ import annotations

from pathlib import Path

from ..dbutil import open_ro
from ..probe import SUPPORTED_ORX_SCHEMA, probe_orx
from ..store import Store, now_iso

# kept as an alias: probe and importer share one gate definition
SUPPORTED = SUPPORTED_ORX_SCHEMA

_TABLES: list[tuple[str, str, list[str]]] = [
    # (mirror table, source table, columns) — named columns only, never *
    ("orx_runs", "runs",
     ["id", "goal_id", "status", "created_at", "updated_at",
      "started_at", "completed_at"]),
    ("orx_goals", "goals",
     ["id", "objective", "status", "context", "constraints_json",
      "acceptance_json", "created_at", "updated_at"]),
    ("orx_revisions", "plan_revisions",
     ["id", "run_id", "revision", "depth", "planner_profile", "status",
      "created_at"]),
    ("orx_tasks", "tasks",
     ["id", "revision_id", "task_id", "objective", "status",
      "failure_reason", "created_at", "updated_at"]),
    ("orx_attempts", "attempts",
     ["id", "revision_id", "task_id", "assignment_id", "role", "profile",
      "driver", "harness", "model", "requested_effort", "actual_effort",
      "fallback_used", "started_at", "ended_at", "result", "failure_reason",
      "session_ref", "run_id", "usage_missing_reason", "actual_model",
      "model_source"]),
    ("orx_verifications", "verifications",
     ["id", "revision_id", "task_id", "attempt_id", "kind", "command",
      "exit_code", "passed", "output_path", "created_at"]),
    ("orx_usage_obs", "usage_observations",
     ["id", "attempt_id", "profile", "run_id", "task_id", "input_tokens",
      "output_tokens", "cached_input_tokens", "source", "accuracy",
      "created_at"]),
    ("orx_task_events", "task_events",
     ["id", "revision_id", "task_id", "from_status", "to_status", "event",
      "reason", "created_at"]),
    ("orx_evidence", "evidence",
     ["id", "attempt_id", "kind", "path", "created_at"]),
]

_V9_TABLES: list[tuple[str, str, list[str]]] = [
    # replan correspondence exists on v9+ files (v10 keeps every v9 table)
    ("orx_replan_maps", "replan_mappings",
     ["id", "run_id", "revision_id", "prior_revision"]),
    ("orx_replan_task_maps", "replan_task_mappings",
     ["id", "mapping_id", "run_id", "revision_id", "task_id",
      "classification", "redo_reason"]),
    ("orx_replan_sources", "replan_sources",
     ["id", "task_mapping_id", "run_id", "source_revision", "source_task_id",
      "source_task_row_id", "part"]),
    ("orx_replan_superseded", "replan_superseded",
     ["id", "mapping_id", "run_id", "source_revision", "source_task_id",
      "disposition", "successors_json", "note"]),
]

_V10_TABLES: list[tuple[str, str, list[str]]] = [
    # append-only host heartbeat progress, added by the v10 amendment
    ("orx_attempt_progress", "attempt_progress",
     ["id", "attempt_id", "sequence", "phase", "message", "received_at"]),
]

# v11 adds the one-time identity nonce column on attempts; v12 only widens
# the usage_observations.source CHECK (admitting 'zcode_db') and keeps the
# v11 shape otherwise — no new columns on either. v13 (goals.status admits
# 'paused') and v14 (admits 'cancelling') widen CHECKs only — same shape as
# v11/v12. v15 adds the completion_receipts table on top. The core column
# list stays version-free, so the importer rebinds the attempts entry with
# the extra column for v11+ files only (v8-v10 files have no such column).
_ATTEMPT_NONCE_EXTRA = ["nonce"]

_V15_TABLES: list[tuple[str, str, list[str]]] = [
    # ORX v15 (G010 T005): one write-once receipt per accepted delivery.
    # receipt_json is intentionally not mirrored — receipts are replay
    # surfaces for the completing caller, analytics only reads facts.
    ("orx_receipts", "completion_receipts",
     ["id", "attempt_id", "revision_id", "task_id", "delivery_status",
      "delivery_digest", "session_ref", "actual_model", "created_at"]),
]

_SOURCE_TO_MIRROR_COL = {
    "cached_input_tokens": "cached_input",
}

_OPTIONAL_SOURCE_COLUMNS = {
    "attempts": ("effort_source",),
    "routing_decisions": ("requested_json", "candidates_json"),
}


def import_orx_project(store: Store, project_key: str) -> dict:
    """Append one snapshot of the project's ORX state. Refuses unsupported
    schema versions via the shared gate in probe.SUPPORTED_ORX_SCHEMA."""
    db = Path(project_key) / ".orx" / "state.db"
    pr = probe_orx(Path(project_key))
    if not pr.ok:
        return {"project": project_key, "error": pr.detail,
                "gaps": pr.gaps}
    store.snapshot_keep()  # reject invalid retention before writing anything
    counts: dict[str, int] = {}
    source_id = f"orx:{project_key}"
    run = store.begin_import(source_id)
    try:
        conn = open_ro(db)
        try:
            conn.execute("BEGIN")  # one consistent source view across tables
            cur = store.conn.execute(
                "INSERT INTO orx_snapshots(project_key, taken_at, "
                "schema_version, source_path) VALUES (?,?,?,?)",
                (project_key, now_iso(), pr.fields["schema_version"], str(db)))
            snapshot_id = cur.lastrowid
            tables = list(_TABLES)
            sv = pr.fields["schema_version"]
            if sv in ("9", "10"):
                tables += _V9_TABLES
            if sv == "10":
                tables += _V10_TABLES
            if sv in ("11", "12", "13", "14", "15"):
                tables += _V9_TABLES + _V10_TABLES
                tables = [
                    (mirror, src, cols + _ATTEMPT_NONCE_EXTRA)
                    if src == "attempts" else (mirror, src, cols)
                    for mirror, src, cols in tables
                ]
            if sv == "15":
                tables += _V15_TABLES
            # Older or minimal fixtures can lack the optional route journal.
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='routing_decisions'"
            ).fetchone():
                tables.append(("orx_routing_decisions", "routing_decisions",
                               ["id", "attempt_id", "selected", "reason", "created_at"]))
            for mirror, src, cols in tables:
                if optional := _OPTIONAL_SOURCE_COLUMNS.get(src):
                    present = {row[1] for row in conn.execute(f"PRAGMA table_info({src})")}
                    cols = [*cols, *(column for column in optional if column in present)]
                select_cols = ", ".join(cols)
                n = 0
                for r in conn.execute(f"SELECT {select_cols} FROM {src}"):
                    row = {
                        "snapshot_id": snapshot_id,
                        **{_SOURCE_TO_MIRROR_COL.get(c, c): r[c]
                           for c in cols}}
                    store.conn.execute(
                        f"INSERT OR REPLACE INTO {mirror}("
                        + ",".join(row.keys())
                        + ") VALUES ("
                        + ",".join("?" for _ in row) + ")",
                        list(row.values()))
                    n += 1
                counts[mirror] = n
        finally:
            conn.close()
        store.prune_snapshots(project_key, current_snapshot=snapshot_id)
        store.finish_import(run, {"snapshot_id": snapshot_id, **counts})
        return {"project": project_key, "snapshot_id": snapshot_id,
                "counts": counts}
    except Exception as e:  # noqa: BLE001
        store.conn.rollback()
        store.finish_import(run, counts, error=str(e))
        return {"project": project_key, "error": str(e)}
