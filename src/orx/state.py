"""SQLite persistence: schema v1, migrations, and repositories.

SQLite is the authoritative state store. WAL mode, foreign keys ON.
Opening a database whose schema version is newer than the code supports is an
error and never rewrites the file. Migrations beyond v1 migrate a copy and
replace the original only on success.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from orx import records
from orx.records import MigrationError

CODE_SCHEMA_VERSION = 9

INBOX_ITEM_STATUSES = ("pending", "accepted", "rejected", "dismissed")
INBOX_DECIDED_STATUSES = ("accepted", "rejected", "dismissed")


def now() -> str:
    # Sub-second resolution keeps a burst of writes strictly time-ordered
    # on the timeline. Stored values stay ISO-8601 text.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


SCHEMA_V1 = """
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE goals (
  id TEXT PRIMARY KEY,
  objective TEXT NOT NULL,
  constraints_json TEXT NOT NULL DEFAULT '[]',
  acceptance_json TEXT NOT NULL DEFAULT '[]',
  context TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('active','done','cancelled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  goal_id TEXT NOT NULL REFERENCES goals(id),
  status TEXT NOT NULL CHECK (status IN ('planning','running','blocked','done')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT
);

CREATE TABLE plan_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  revision INTEGER NOT NULL,
  depth TEXT NOT NULL,
  planner_profile TEXT NOT NULL,
  ir_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','superseded')),
  created_at TEXT NOT NULL,
  UNIQUE(run_id, revision)
);

CREATE TABLE planning_assignments (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  profile TEXT NOT NULL,
  depth TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('waiting_host','submitted','failed','cancelled')),
  prompt TEXT NOT NULL,
  created_at TEXT NOT NULL,
  submitted_at TEXT
);

CREATE TABLE tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  revision_id INTEGER NOT NULL REFERENCES plan_revisions(id),
  task_id TEXT NOT NULL,
  objective TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  acceptance_json TEXT NOT NULL,
  verification_json TEXT NOT NULL,
  preread_json TEXT NOT NULL DEFAULT '[]',
  routing_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','runnable','running','waiting_host',
                                         'waiting_external','verifying','passed','failed',
                                         'blocked','cancelled')),
  failure_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(revision_id, task_id)
);

CREATE TABLE task_dependencies (
  revision_id INTEGER NOT NULL,
  task_id TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  PRIMARY KEY (revision_id, task_id, depends_on)
);

CREATE TABLE task_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  revision_id INTEGER NOT NULL,
  task_id TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT NOT NULL,
  event TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  revision_id INTEGER REFERENCES plan_revisions(id),
  task_id TEXT,
  assignment_id TEXT,
  role TEXT NOT NULL,
  profile TEXT NOT NULL,
  driver TEXT NOT NULL,
  harness TEXT NOT NULL,
  model TEXT NOT NULL,
  requested_effort TEXT NOT NULL,
  actual_effort TEXT,
  effort_source TEXT,
  fallback_used INTEGER NOT NULL DEFAULT 0,
  routing_reason TEXT,
  started_at TEXT,
  ended_at TEXT,
  result TEXT,
  failure_reason TEXT,
  isolation TEXT,
  session_ref TEXT,
  run_id TEXT,
  usage_missing_reason TEXT,
  verify_entry TEXT,
  actual_model TEXT,
  model_source TEXT
);

CREATE TABLE evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id),
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  revision_id INTEGER NOT NULL,
  task_id TEXT NOT NULL,
  attempt_id INTEGER REFERENCES attempts(id),
  kind TEXT NOT NULL CHECK (kind IN ('command','agent')),
  command TEXT NOT NULL,
  required_capabilities_json TEXT NOT NULL DEFAULT '[]',
  exit_code INTEGER,
  passed INTEGER NOT NULL,
  output_path TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE routing_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER REFERENCES attempts(id),
  role TEXT NOT NULL,
  requested_json TEXT NOT NULL,
  candidates_json TEXT NOT NULL,
  selected TEXT,
  reason TEXT,
  downgrade_blocked INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE resource_status (
  profile TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('abundant','available','constrained',
                                         'exhausted','unavailable','unknown',
                                         'cooldown','auth_required')),
  note TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  last_success_at TEXT,
  last_failure_at TEXT,
  failure_streak INTEGER NOT NULL DEFAULT 0,
  last_error_kind TEXT,
  cooldown_until TEXT,
  quota_reset_at TEXT,
  last_probe_at TEXT,
  override INTEGER NOT NULL DEFAULT 0
);
"""


# v2: recreate resource_status (the v1 CHECK cannot admit cooldown /
# auth_required via ALTER TABLE), carrying old rows forward into the new
# health columns.
SCHEMA_V2_RESOURCE = """
CREATE TABLE resource_status_v2 (
  profile TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('abundant','available','constrained',
                                         'exhausted','unavailable','unknown',
                                         'cooldown','auth_required')),
  note TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  last_success_at TEXT,
  last_failure_at TEXT,
  failure_streak INTEGER NOT NULL DEFAULT 0,
  last_error_kind TEXT,
  cooldown_until TEXT,
  quota_reset_at TEXT,
  last_probe_at TEXT,
  override INTEGER NOT NULL DEFAULT 0
);
INSERT INTO resource_status_v2 (profile, status, note, updated_at)
  SELECT profile, status, note, updated_at FROM resource_status;
DROP TABLE resource_status;
ALTER TABLE resource_status_v2 RENAME TO resource_status;
"""

# v3: usage observations attach to attempts (M1 P5). Unknown is a legal,
# stored outcome — tokens are an observation, never a fabricated total.
SCHEMA_V3_USAGE = """
CREATE TABLE IF NOT EXISTS usage_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id),
  profile TEXT NOT NULL,
  run_id TEXT NOT NULL,
  task_id TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cached_input_tokens INTEGER,
  source TEXT NOT NULL CHECK (source IN ('native_cli', 'output_estimate')),
  accuracy TEXT NOT NULL CHECK (accuracy IN ('exact', 'estimated', 'unknown')),
  created_at TEXT NOT NULL
);
"""


def _migrate_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_V1)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(CODE_SCHEMA_VERSION),),
    )


def _migrate_v2(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_V2_RESOURCE)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(CODE_SCHEMA_VERSION),),
    )


def _migrate_v3(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_V3_USAGE)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(CODE_SCHEMA_VERSION),),
    )


# v4: inbox + external sources. Additive only — existing tables and rows are
# untouched, so the migration is a pure CREATE TABLE pass. (Numbered 3 on the
# p6-inbox branch; renumbered to 4 at the P5+P6 merge so usage stays 3.)
SCHEMA_V4_INBOX = """
CREATE TABLE IF NOT EXISTS external_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  external_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS inbox_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES external_events(id),
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('pending','accepted','rejected','dismissed')),
  goal_id TEXT NULL,
  created_at TEXT NOT NULL,
  decided_at TEXT NULL
);
"""


def _migrate_v4(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_V4_INBOX)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(CODE_SCHEMA_VERSION),),
    )


# v5: attempts declare the isolation each launch actually enforced
# (docs/pbv-mapping.md §4.6) — "read_only"/"workspace_write" for CLI sandbox
# flags, "prompt_only" for host/external prompt discipline, NULL when the
# launch claims nothing. The column ships in SCHEMA_V1's CREATE TABLE, so
# fresh databases (which run every migration) already have it; the ALTER is
# guarded for them and only fires on databases upgraded from v4.
def _migrate_v5(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(attempts)")}
    if "isolation" not in columns:
        conn.execute("ALTER TABLE attempts ADD COLUMN isolation TEXT")
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(CODE_SCHEMA_VERSION),),
    )


# v6: tasks carry `preread` — the bounded read-first file list handed to every
# worker assignment (docs/pbv-mapping.md §4.1). Ships in SCHEMA_V1's CREATE
# TABLE for fresh databases; the ALTER only fires on databases upgraded from
# v5. Existing tasks read as an empty list.
def _migrate_v6(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "preread_json" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN preread_json TEXT NOT NULL DEFAULT '[]'")
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(CODE_SCHEMA_VERSION),),
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if name not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


# v7: observability columns and the widened usage source CHECK.
# Additive only. Legacy NULLs stay NULL — session_ref, usage-missing reasons,
# and run lifecycle timestamps are not invented. run_id is copied only when
# a revision or planning assignment already records the run. updated_at is
# not a completion time. The usage CHECK rebuild follows the v2
# backup-replace pattern inside the copy-then-replace migration.
SCHEMA_V7_USAGE = """
CREATE TABLE usage_observations_v7 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id),
  profile TEXT NOT NULL,
  run_id TEXT NOT NULL,
  task_id TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cached_input_tokens INTEGER,
  source TEXT NOT NULL CHECK (source IN ('native_cli', 'output_estimate', 'host_report')),
  accuracy TEXT NOT NULL CHECK (accuracy IN ('exact', 'estimated', 'unknown')),
  created_at TEXT NOT NULL
);
INSERT INTO usage_observations_v7 (
  id, attempt_id, profile, run_id, task_id, input_tokens, output_tokens,
  cached_input_tokens, source, accuracy, created_at
)
SELECT id, attempt_id, profile, run_id, task_id, input_tokens, output_tokens,
       cached_input_tokens, source, accuracy, created_at
FROM usage_observations;
DROP TABLE usage_observations;
ALTER TABLE usage_observations_v7 RENAME TO usage_observations;
"""


def _migrate_v7(conn: sqlite3.Connection) -> None:
    _add_column(conn, "attempts", "session_ref", "session_ref TEXT")
    _add_column(conn, "attempts", "run_id", "run_id TEXT")
    _add_column(conn, "attempts", "usage_missing_reason", "usage_missing_reason TEXT")
    _add_column(conn, "runs", "started_at", "started_at TEXT")
    _add_column(conn, "runs", "completed_at", "completed_at TEXT")
    # Deterministic attribution from an association that already exists.
    # Rows with neither a revision nor an assignment stay NULL (unknown).
    conn.execute(
        "UPDATE attempts SET run_id = ("
        " SELECT run_id FROM plan_revisions WHERE plan_revisions.id = attempts.revision_id"
        ") WHERE run_id IS NULL AND revision_id IS NOT NULL"
        " AND EXISTS (SELECT 1 FROM plan_revisions WHERE plan_revisions.id = attempts.revision_id)"
    )
    conn.execute(
        "UPDATE attempts SET run_id = ("
        " SELECT run_id FROM planning_assignments"
        " WHERE planning_assignments.id = attempts.assignment_id"
        ") WHERE run_id IS NULL AND assignment_id IS NOT NULL"
        " AND EXISTS (SELECT 1 FROM planning_assignments"
        "             WHERE planning_assignments.id = attempts.assignment_id)"
    )
    has_usage = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_observations' LIMIT 1"
    ).fetchone()
    if has_usage:
        conn.executescript(SCHEMA_V7_USAGE)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(CODE_SCHEMA_VERSION),),
    )


# v8: host subagent execution contract (docs/zcode-subagent-analysis.md §5).
# Additive only. verify_entry binds a host verifier attempt to the exact
# verification entry it was dispatched for, so a verdict submitted later closes
# the attempt whose identity was fixed at dispatch — routing edits in between
# cannot move the attribution. actual_model / model_source record the model the
# executor reported (ZCode dispatch receipt; db model_usage is the authority
# behind it), separate from the profile's requested model. Legacy rows stay
# NULL; neither column is ever back-filled by guesswork.
def _migrate_v8(conn: sqlite3.Connection) -> None:
    _add_column(conn, "attempts", "verify_entry", "verify_entry TEXT")
    _add_column(conn, "attempts", "actual_model", "actual_model TEXT")
    _add_column(conn, "attempts", "model_source", "model_source TEXT")
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(CODE_SCHEMA_VERSION),),
    )


# v9: G004 replan storage (docs/replan-contract.md §8-9, observability read
# contract v9 amendment). Additive only: six new tables for the replan
# correspondence, the preflight report, and traceable artifact provenance.
# Identity everywhere is (run_id, revision, task_id) — never a bare task
# number — plus attempt/evidence row ids on provenance. Nothing is
# backfilled: a database upgraded from v8 has empty replan tables, reads on
# it return unknown (None / []), and no code path derives a correspondence
# or inherits a prior passed status.
SCHEMA_V9_REPLAN = """
CREATE TABLE IF NOT EXISTS replan_mappings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  revision_id INTEGER NOT NULL REFERENCES plan_revisions(id),
  prior_revision INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(revision_id)
);

CREATE TABLE IF NOT EXISTS replan_task_mappings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mapping_id INTEGER NOT NULL REFERENCES replan_mappings(id),
  run_id TEXT NOT NULL REFERENCES runs(id),
  revision_id INTEGER NOT NULL REFERENCES plan_revisions(id),
  task_id TEXT NOT NULL,
  classification TEXT NOT NULL CHECK (classification IN ('new','confirm','redo','continue')),
  redo_reason TEXT,
  confirm_verification_json TEXT NOT NULL DEFAULT '[]',
  artifacts_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE(revision_id, task_id)
);

CREATE TABLE IF NOT EXISTS replan_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_mapping_id INTEGER NOT NULL REFERENCES replan_task_mappings(id),
  run_id TEXT NOT NULL REFERENCES runs(id),
  source_revision INTEGER NOT NULL,
  source_task_id TEXT NOT NULL,
  source_task_row_id INTEGER NOT NULL REFERENCES tasks(id),
  part INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(task_mapping_id, source_revision, source_task_id)
);

CREATE TABLE IF NOT EXISTS replan_superseded (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mapping_id INTEGER NOT NULL REFERENCES replan_mappings(id),
  run_id TEXT NOT NULL REFERENCES runs(id),
  source_revision INTEGER NOT NULL,
  source_task_id TEXT NOT NULL,
  source_task_row_id INTEGER NOT NULL REFERENCES tasks(id),
  disposition TEXT NOT NULL CHECK (disposition IN ('confirmed','continued','redone','split','merged','dropped')),
  successors_json TEXT NOT NULL DEFAULT '[]',
  note TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(mapping_id, source_revision, source_task_id)
);

CREATE TABLE IF NOT EXISTS replan_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  prior_revision INTEGER NOT NULL,
  revision_id INTEGER REFERENCES plan_revisions(id),
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replan_artifact_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  revision_id INTEGER NOT NULL REFERENCES plan_revisions(id),
  task_id TEXT NOT NULL,
  source_revision INTEGER NOT NULL,
  source_task_id TEXT NOT NULL,
  artifact TEXT NOT NULL,
  attempt_id INTEGER REFERENCES attempts(id),
  evidence_id INTEGER REFERENCES evidence(id),
  created_at TEXT NOT NULL
);
"""


def _migrate_v9(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_V9_REPLAN)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(CODE_SCHEMA_VERSION),),
    )


# Migrations keyed by the version they produce.
MIGRATIONS: dict[int, callable] = {
    1: _migrate_v1,
    2: _migrate_v2,
    3: _migrate_v3,
    4: _migrate_v4,
    5: _migrate_v5,
    6: _migrate_v6,
    7: _migrate_v7,
    8: _migrate_v8,
    9: _migrate_v9,
}


# ---------------------------------------------------------------------------
# Row records


@dataclass(frozen=True)
class Goal:
    id: str
    objective: str
    constraints: list[str]
    acceptance: list[str]
    context: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Run:
    id: str
    goal_id: str
    status: str
    created_at: str
    updated_at: str
    # Lifecycle clock. NULL until a real transition stamps them.
    # started_at: first entry to running. completed_at: first entry to done.
    # updated_at is not a substitute for either.
    started_at: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True)
class Revision:
    id: int
    run_id: str
    revision: int
    depth: str
    planner_profile: str
    ir: dict
    status: str
    created_at: str


@dataclass(frozen=True)
class Assignment:
    id: str
    run_id: str
    profile: str
    depth: str
    status: str
    prompt: str
    created_at: str
    submitted_at: str | None


@dataclass(frozen=True)
class TaskRow:
    row_id: int
    revision_id: int
    task_id: str
    objective: str
    scope: dict
    acceptance: list[str]
    verification: list[str]
    routing: dict
    status: str
    failure_reason: str | None
    created_at: str
    updated_at: str
    preread: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TaskEvent:
    id: int
    revision_id: int
    task_id: str
    from_status: str | None
    to_status: str
    event: str
    reason: str | None
    created_at: str


@dataclass(frozen=True)
class Attempt:
    id: int
    revision_id: int
    task_id: str | None
    assignment_id: str | None
    role: str
    profile: str
    driver: str
    harness: str
    model: str
    requested_effort: str
    actual_effort: str | None
    effort_source: str | None
    fallback_used: bool
    routing_reason: str | None
    started_at: str | None
    ended_at: str | None
    result: str | None
    failure_reason: str | None
    # Isolation the launch actually enforced (docs/pbv-mapping.md §4.6):
    # read_only | workspace_write | prompt_only; None = no claim.
    isolation: str | None = None
    # Opaque native session id supplied by the caller. Never synthesized.
    session_ref: str | None = None
    # Run this attempt belongs to, copied from a revision, an assignment,
    # or the caller that already holds the run. NULL when none of those exist.
    run_id: str | None = None
    # Why this attempt has no usage observation. NULL means not assessed
    # (legacy rows, or a path that never looked). Not a fabricated token count.
    usage_missing_reason: str | None = None
    # The exact verification entry raw text a host verifier attempt answers
    # (verifier attempts only; NULL otherwise). Dispatch-time binding: the
    # verdict must close the attempt it was dispatched to, not a re-route.
    verify_entry: str | None = None
    # Model the executor actually reported (e.g. from the ZCode dispatch
    # receipt; db model_usage is the authority behind it). Never synthesized;
    # model_source names where the value came from ('reported' today).
    actual_model: str | None = None
    model_source: str | None = None


@dataclass(frozen=True)
class Verification:
    id: int
    revision_id: int
    task_id: str
    attempt_id: int | None
    kind: str
    command: str
    required_capabilities: list[str]
    exit_code: int | None
    passed: bool
    output_path: str | None
    created_at: str


@dataclass(frozen=True)
class RoutingDecision:
    id: int
    attempt_id: int | None
    role: str
    requested: dict
    candidates: list
    selected: str | None
    reason: str | None
    downgrade_blocked: bool
    created_at: str


@dataclass(frozen=True)
class ReplanSourceRow:
    """One declared source edge (docs/replan-contract.md §5).

    Identity is the (source_revision, source_task_id) pair inside the
    mapping's run; source_task_row_id is the resolved tasks row so the
    correspondence is mechanically anchored even when tasks are renumbered.
    """

    id: int
    task_mapping_id: int
    run_id: str
    source_revision: int
    source_task_id: str
    source_task_row_id: int
    part: bool


@dataclass(frozen=True)
class ReplanTaskMappingRow:
    id: int
    mapping_id: int
    run_id: str
    revision_id: int
    task_id: str
    classification: str
    redo_reason: str | None
    confirm_verification: list[str]
    artifacts: list[str]
    sources: list[ReplanSourceRow] = field(default_factory=list)


@dataclass(frozen=True)
class ReplanSupersededRow:
    id: int
    mapping_id: int
    run_id: str
    source_revision: int
    source_task_id: str
    source_task_row_id: int
    disposition: str
    successors: list[str]
    note: str | None


@dataclass(frozen=True)
class ReplanMappingRow:
    """One replan revision's declared old<->new correspondence."""

    id: int
    run_id: str
    revision_id: int
    prior_revision: int
    created_at: str
    tasks: list[ReplanTaskMappingRow] = field(default_factory=list)
    superseded: list[ReplanSupersededRow] = field(default_factory=list)


@dataclass(frozen=True)
class ReplanReportRow:
    """A replan preflight report (the pre-check diff before a revision lands).

    revision_id is NULL until the proposed revision actually lands and the
    caller binds it; a NULL is unknown, never guessed.
    """

    id: int
    run_id: str
    prior_revision: int
    revision_id: int | None
    payload: dict
    created_at: str


@dataclass(frozen=True)
class ReplanArtifactSourceRow:
    """Traceable artifact provenance on a declared (task <-> source) edge.

    Carries run, revision (the replan revision), the source task's full
    (revision, task_id) identity, the producing attempt and evidence row
    when known, and the artifact reference itself. A same-numbered task in
    another revision never satisfies this identity.
    """

    id: int
    run_id: str
    revision_id: int
    task_id: str
    source_revision: int
    source_task_id: str
    artifact: str
    attempt_id: int | None
    evidence_id: int | None
    created_at: str


@dataclass(frozen=True)
class ReplanSuccessorRow:
    """The new-revision task that took over one old task."""

    revision_id: int
    revision: int
    task_id: str
    classification: str
    part: bool


@dataclass(frozen=True)
class ReplanTraceStep:
    """One hop of a forward trace across replan rounds."""

    from_revision: int
    from_task_id: str
    to_revision: int
    to_task_id: str
    classification: str
    part: bool


@dataclass(frozen=True)
class ResourceRow:
    profile: str
    status: str
    note: str
    updated_at: str
    last_success_at: str | None = None
    last_failure_at: str | None = None
    failure_streak: int = 0
    last_error_kind: str | None = None
    cooldown_until: str | None = None
    quota_reset_at: str | None = None
    last_probe_at: str | None = None
    override: int = 0


def _j(value: str | None, default):
    if value is None:
        return default
    return json.loads(value)


def _goal(r: sqlite3.Row) -> Goal:
    return Goal(
        id=r["id"],
        objective=r["objective"],
        constraints=_j(r["constraints_json"], []),
        acceptance=_j(r["acceptance_json"], []),
        context=r["context"],
        status=r["status"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def _run(r: sqlite3.Row) -> Run:
    return Run(
        id=r["id"],
        goal_id=r["goal_id"],
        status=r["status"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        started_at=r["started_at"],
        completed_at=r["completed_at"],
    )


def _revision(r: sqlite3.Row) -> Revision:
    return Revision(
        id=r["id"],
        run_id=r["run_id"],
        revision=r["revision"],
        depth=r["depth"],
        planner_profile=r["planner_profile"],
        ir=_j(r["ir_json"], {}),
        status=r["status"],
        created_at=r["created_at"],
    )


def _assignment(r: sqlite3.Row) -> Assignment:
    return Assignment(
        id=r["id"],
        run_id=r["run_id"],
        profile=r["profile"],
        depth=r["depth"],
        status=r["status"],
        prompt=r["prompt"],
        created_at=r["created_at"],
        submitted_at=r["submitted_at"],
    )


def _task(r: sqlite3.Row) -> TaskRow:
    return TaskRow(
        row_id=r["id"],
        revision_id=r["revision_id"],
        task_id=r["task_id"],
        objective=r["objective"],
        scope=_j(r["scope_json"], {}),
        acceptance=_j(r["acceptance_json"], []),
        verification=_j(r["verification_json"], []),
        preread=_j(r["preread_json"], []),
        routing=_j(r["routing_json"], {}),
        status=r["status"],
        failure_reason=r["failure_reason"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def _event(r: sqlite3.Row) -> TaskEvent:
    return TaskEvent(
        id=r["id"],
        revision_id=r["revision_id"],
        task_id=r["task_id"],
        from_status=r["from_status"],
        to_status=r["to_status"],
        event=r["event"],
        reason=r["reason"],
        created_at=r["created_at"],
    )


def _attempt(r: sqlite3.Row) -> Attempt:
    return Attempt(
        id=r["id"],
        revision_id=r["revision_id"],
        task_id=r["task_id"],
        assignment_id=r["assignment_id"],
        role=r["role"],
        profile=r["profile"],
        driver=r["driver"],
        harness=r["harness"],
        model=r["model"],
        requested_effort=r["requested_effort"],
        actual_effort=r["actual_effort"],
        effort_source=r["effort_source"],
        fallback_used=bool(r["fallback_used"]),
        routing_reason=r["routing_reason"],
        started_at=r["started_at"],
        ended_at=r["ended_at"],
        result=r["result"],
        failure_reason=r["failure_reason"],
        isolation=r["isolation"],
        session_ref=r["session_ref"],
        run_id=r["run_id"],
        usage_missing_reason=r["usage_missing_reason"],
        verify_entry=r["verify_entry"],
        actual_model=r["actual_model"],
        model_source=r["model_source"],
    )


def _verification(r: sqlite3.Row) -> Verification:
    return Verification(
        id=r["id"],
        revision_id=r["revision_id"],
        task_id=r["task_id"],
        attempt_id=r["attempt_id"],
        kind=r["kind"],
        command=r["command"],
        required_capabilities=_j(r["required_capabilities_json"], []),
        exit_code=r["exit_code"],
        passed=bool(r["passed"]),
        output_path=r["output_path"],
        created_at=r["created_at"],
    )


def _decision(r: sqlite3.Row) -> RoutingDecision:
    return RoutingDecision(
        id=r["id"],
        attempt_id=r["attempt_id"],
        role=r["role"],
        requested=_j(r["requested_json"], {}),
        candidates=_j(r["candidates_json"], []),
        selected=r["selected"],
        reason=r["reason"],
        downgrade_blocked=bool(r["downgrade_blocked"]),
        created_at=r["created_at"],
    )


def _replan_source(r: sqlite3.Row) -> ReplanSourceRow:
    return ReplanSourceRow(
        id=r["id"],
        task_mapping_id=r["task_mapping_id"],
        run_id=r["run_id"],
        source_revision=r["source_revision"],
        source_task_id=r["source_task_id"],
        source_task_row_id=r["source_task_row_id"],
        part=bool(r["part"]),
    )


def _replan_task_mapping(r: sqlite3.Row, sources: list[ReplanSourceRow]) -> ReplanTaskMappingRow:
    return ReplanTaskMappingRow(
        id=r["id"],
        mapping_id=r["mapping_id"],
        run_id=r["run_id"],
        revision_id=r["revision_id"],
        task_id=r["task_id"],
        classification=r["classification"],
        redo_reason=r["redo_reason"],
        confirm_verification=_j(r["confirm_verification_json"], []),
        artifacts=_j(r["artifacts_json"], []),
        sources=list(sources),
    )


def _replan_superseded(r: sqlite3.Row) -> ReplanSupersededRow:
    return ReplanSupersededRow(
        id=r["id"],
        mapping_id=r["mapping_id"],
        run_id=r["run_id"],
        source_revision=r["source_revision"],
        source_task_id=r["source_task_id"],
        source_task_row_id=r["source_task_row_id"],
        disposition=r["disposition"],
        successors=_j(r["successors_json"], []),
        note=r["note"],
    )


def _replan_report(r: sqlite3.Row) -> ReplanReportRow:
    return ReplanReportRow(
        id=r["id"],
        run_id=r["run_id"],
        prior_revision=r["prior_revision"],
        revision_id=r["revision_id"],
        payload=_j(r["payload_json"], {}),
        created_at=r["created_at"],
    )


def _replan_artifact_source(r: sqlite3.Row) -> ReplanArtifactSourceRow:
    return ReplanArtifactSourceRow(
        id=r["id"],
        run_id=r["run_id"],
        revision_id=r["revision_id"],
        task_id=r["task_id"],
        source_revision=r["source_revision"],
        source_task_id=r["source_task_id"],
        artifact=r["artifact"],
        attempt_id=r["attempt_id"],
        evidence_id=r["evidence_id"],
        created_at=r["created_at"],
    )


def _resource(r: sqlite3.Row) -> ResourceRow:
    def opt(key):
        try:
            return r[key]
        except (IndexError, KeyError):
            return None

    return ResourceRow(
        profile=r["profile"],
        status=r["status"],
        note=r["note"],
        updated_at=r["updated_at"],
        last_success_at=opt("last_success_at"),
        last_failure_at=opt("last_failure_at"),
        failure_streak=r["failure_streak"] if "failure_streak" in r.keys() else 0,
        last_error_kind=opt("last_error_kind"),
        cooldown_until=opt("cooldown_until"),
        quota_reset_at=opt("quota_reset_at"),
        last_probe_at=opt("last_probe_at"),
        override=r["override"] if "override" in r.keys() else 0,
    )


# ---------------------------------------------------------------------------


def _nonnegative_count(name: str, value, *, allow_none: bool = False):
    """A token count the caller actually reported. Never coerced from None to 0."""
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise records.ORXError(f"{name} must be a nonnegative integer")
    return value


def _host_report_key(row: sqlite3.Row) -> tuple:
    return (
        row["input_tokens"],
        row["output_tokens"],
        row["cached_input_tokens"],
        row["accuracy"],
    )


def _host_report_payload(attempt: Attempt, row: sqlite3.Row, *, idempotent: bool) -> dict:
    return {
        "attempt": attempt.id,
        "profile": attempt.profile,
        "run_id": attempt.run_id,
        "task_id": attempt.task_id,
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cached_input_tokens": row["cached_input_tokens"],
        "source": "host_report",
        "accuracy": row["accuracy"],
        "idempotent": idempotent,
    }


class Store:
    """Repository over one SQLite database. All writes go through here."""

    def __init__(self, conn: sqlite3.Connection, path: Path):
        self.conn = conn
        self.path = path
        self._tx_depth = 0

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, path: Path) -> "Store":
        path = Path(path)
        fresh = not path.exists() or path.stat().st_size == 0
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        store = cls(conn, path)
        if fresh:
            with store.tx():
                for version in range(1, CODE_SCHEMA_VERSION + 1):
                    MIGRATIONS[version](conn)
            return store

        version = store._read_version(conn)
        if version is None:
            has_tables = bool(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='goals' LIMIT 1"
                ).fetchone()
            )
            conn.close()
            if has_tables:
                raise MigrationError(
                    f"{path.name}: database has tables but no schema_version; refusing to touch it"
                )
            # Non-empty file without ORX tables: treat as a fresh database.
            return cls._open_fresh(path)
        if version > CODE_SCHEMA_VERSION:
            conn.close()
            raise MigrationError(
                f"{path.name}: schema version {version} is newer than supported "
                f"version {CODE_SCHEMA_VERSION}; upgrade orx"
            )
        if version < CODE_SCHEMA_VERSION:
            conn.close()
            return cls._migrate_copy(path, version)
        return store

    @classmethod
    def _open_fresh(cls, path: Path) -> "Store":
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        store = cls(conn, path)
        with store.tx():
            for version in range(1, CODE_SCHEMA_VERSION + 1):
                MIGRATIONS[version](conn)
        return store

    @classmethod
    def _migrate_copy(cls, path: Path, from_version: int) -> "Store":
        """Copy, migrate the copy, replace the original only on success."""
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.migrate-")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            # Committed rows may still live in the WAL. Fold them into the
            # main file before copying, and copy any leftover sidecars with
            # the main file so a busy checkpoint cannot drop them.
            src = sqlite3.connect(path)
            try:
                src.execute("PRAGMA busy_timeout=5000")
                src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                src.close()
            shutil.copy2(path, tmp)
            # Pair the main file with its WAL so committed frames survive even
            # when checkpoint could not truncate under another connection.
            # The shm index is rebuilt; copying it would copy live locks.
            wal_sidecar = Path(str(path) + "-wal")
            if wal_sidecar.exists():
                shutil.copy2(wal_sidecar, Path(str(tmp) + "-wal"))
            conn = sqlite3.connect(tmp, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                for version in range(from_version + 1, CODE_SCHEMA_VERSION + 1):
                    MIGRATIONS[version](conn)
                conn.commit()
            except Exception:
                conn.close()
                tmp.unlink(missing_ok=True)
                raise
            # Fold the WAL back into the temp file so os.replace carries a
            # complete database and no tmp -wal/-shm sidecars survive.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            os.replace(tmp, path)
            # Remove stale WAL/SHM sidecars from the pre-migration database.
            for suffix in ("-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
        finally:
            tmp.unlink(missing_ok=True)
            # The migrated temp's own sidecars never outlive the swap.
            for suffix in ("-wal", "-shm"):
                Path(str(tmp) + suffix).unlink(missing_ok=True)
        return cls.open(path)

    @staticmethod
    def _read_version(conn: sqlite3.Connection) -> int | None:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta' LIMIT 1"
        ).fetchone()
        if not row:
            return None
        got = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not got:
            return None
        try:
            return int(got["value"])
        except ValueError:
            raise MigrationError(f"schema_version {got['value']!r} is not an integer")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        """Write transaction. Nested calls become savepoints."""
        if self._tx_depth > 0:
            sp = f"orx_sp_{self._tx_depth}"
            self.conn.execute(f"SAVEPOINT {sp}")
            self._tx_depth += 1
            try:
                yield
            except Exception:
                self.conn.execute(f"ROLLBACK TO {sp}")
                self.conn.execute(f"RELEASE {sp}")
                self._tx_depth -= 1
                raise
            else:
                self.conn.execute(f"RELEASE {sp}")
                self._tx_depth -= 1
            return

        self.conn.execute("BEGIN IMMEDIATE")
        self._tx_depth = 1
        try:
            yield
        except Exception:
            self.conn.rollback()
            self._tx_depth = 0
            raise
        else:
            self.conn.commit()
            self._tx_depth = 0

    def schema_version(self) -> int:
        got = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(got["value"])

    # -- goals / runs -------------------------------------------------------

    def goal_create(self, objective: str, acceptance: list[str], constraints: list[str], context: str) -> tuple[Goal, Run]:
        with self.tx():
            count = self.conn.execute("SELECT COUNT(*) AS c FROM goals").fetchone()["c"]
            goal_id = f"G{count + 1:03d}"
            goal_ts = now()
            self.conn.execute(
                "INSERT INTO goals(id, objective, constraints_json, acceptance_json, context,"
                " status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    goal_id,
                    objective,
                    json.dumps(constraints),
                    json.dumps(acceptance),
                    context,
                    records.GoalStatus.ACTIVE.value,
                    goal_ts,
                    goal_ts,
                ),
            )
            run_count = self.conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
            run_id = f"R{run_count + 1:03d}"
            run_ts = now()
            self.conn.execute(
                "INSERT INTO runs(id, goal_id, status, created_at, updated_at) VALUES(?,?,?,?,?)",
                (run_id, goal_id, records.RunStatus.PLANNING.value, run_ts, run_ts),
            )
        return self.goal_get(goal_id), self.run_get(run_id)

    def goal_get(self, goal_id: str) -> Goal:
        r = self.conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if not r:
            raise records.NotFoundError(f"goal {goal_id} not found")
        return _goal(r)

    def goal_active(self) -> Goal | None:
        r = self.conn.execute("SELECT * FROM goals WHERE status = 'active' ORDER BY id LIMIT 1").fetchone()
        return _goal(r) if r else None

    def goal_set_status(self, goal_id: str, status: records.GoalStatus) -> None:
        with self.tx():
            self.conn.execute(
                "UPDATE goals SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, now(), goal_id),
            )

    def run_get(self, run_id: str) -> Run:
        r = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not r:
            raise records.NotFoundError(f"run {run_id} not found")
        return _run(r)

    def run_for_goal(self, goal_id: str) -> Run | None:
        r = self.conn.execute(
            "SELECT * FROM runs WHERE goal_id = ? ORDER BY id DESC LIMIT 1", (goal_id,)
        ).fetchone()
        return _run(r) if r else None

    def goals_all(self) -> list[Goal]:
        rows = self.conn.execute("SELECT * FROM goals ORDER BY created_at, id").fetchall()
        return [_goal(r) for r in rows]

    def runs_all(self) -> list[Run]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY created_at, id").fetchall()
        return [_run(r) for r in rows]

    def run_set_status(self, run_id: str, status: records.RunStatus) -> None:
        """Move a run and stamp lifecycle columns from the transition itself.

        started_at is the first transition into running. completed_at is the
        first transition into done. Leaving done clears completed_at and
        keeps started_at. Repeating the current status does not rewrite
        either stamp. updated_at still moves; it is not a completion time.
        """
        with self.tx():
            row = self.conn.execute(
                "SELECT status, started_at, completed_at FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise records.NotFoundError(f"run {run_id} not found")
            ts = now()
            new_status = status.value
            started_at = row["started_at"]
            completed_at = row["completed_at"]
            if row["status"] == records.RunStatus.DONE.value and new_status != records.RunStatus.DONE.value:
                completed_at = None
            if new_status == records.RunStatus.RUNNING.value and started_at is None:
                started_at = ts
            if new_status == records.RunStatus.DONE.value and completed_at is None:
                completed_at = ts
            self.conn.execute(
                "UPDATE runs SET status = ?, updated_at = ?, started_at = ?, completed_at = ?"
                " WHERE id = ?",
                (new_status, ts, started_at, completed_at, run_id),
            )

    # -- planning assignments -------------------------------------------------

    def assignment_create(self, run_id: str, profile: str, depth: str, prompt: str) -> Assignment:
        with self.tx():
            count = self.conn.execute(
                "SELECT COUNT(*) AS c FROM planning_assignments"
            ).fetchone()["c"]
            aid = f"P{count + 1:03d}"
            self.conn.execute(
                "INSERT INTO planning_assignments(id, run_id, profile, depth, status, prompt,"
                " created_at) VALUES(?,?,?,?,?,?,?)",
                (aid, run_id, profile, depth, records.AssignmentStatus.WAITING_HOST.value, prompt, now()),
            )
        return self.assignment_get(aid)

    def assignment_get(self, assignment_id: str) -> Assignment:
        r = self.conn.execute(
            "SELECT * FROM planning_assignments WHERE id = ?", (assignment_id,)
        ).fetchone()
        if not r:
            raise records.NotFoundError(f"planning assignment {assignment_id} not found")
        return _assignment(r)

    def assignments_all(self) -> list[Assignment]:
        rows = self.conn.execute(
            "SELECT * FROM planning_assignments ORDER BY created_at, id"
        ).fetchall()
        return [_assignment(r) for r in rows]

    def assignment_waiting(self, run_id: str) -> Assignment | None:
        r = self.conn.execute(
            "SELECT * FROM planning_assignments WHERE run_id = ? AND status = 'waiting_host'"
            " ORDER BY id LIMIT 1",
            (run_id,),
        ).fetchone()
        return _assignment(r) if r else None

    def assignment_set_status(
        self, assignment_id: str, status: records.AssignmentStatus, mark_submitted: bool = False
    ) -> None:
        with self.tx():
            if mark_submitted:
                self.conn.execute(
                    "UPDATE planning_assignments SET status = ?, submitted_at = ? WHERE id = ?",
                    (status.value, now(), assignment_id),
                )
            else:
                self.conn.execute(
                    "UPDATE planning_assignments SET status = ? WHERE id = ?",
                    (status.value, assignment_id),
                )

    def assignment_update_prompt(self, assignment_id: str, prompt: str) -> None:
        """Refresh a still-waiting assignment's prompt (a replan re-routed with
        newer facts/intent). The row and the on-disk file stay in sync because
        the caller rewrites the file in the same flow."""
        with self.tx():
            self.conn.execute(
                "UPDATE planning_assignments SET prompt = ? WHERE id = ?",
                (prompt, assignment_id),
            )

    # -- plan revisions ---------------------------------------------------------

    def revision_create(self, run_id: str, depth: str, planner_profile: str, ir: dict) -> Revision:
        with self.tx():
            got = self.conn.execute(
                "SELECT COALESCE(MAX(revision), 0) AS m FROM plan_revisions WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            revision = got["m"] + 1
            cur = self.conn.execute(
                "INSERT INTO plan_revisions(run_id, revision, depth, planner_profile, ir_json,"
                " status, created_at) VALUES(?,?,?,?,?,?,?)",
                (run_id, revision, depth, planner_profile, json.dumps(ir),
                 records.RevisionStatus.ACTIVE.value, now()),
            )
            rid = cur.lastrowid
        return self.revision_get(rid)

    def revision_get(self, revision_row_id: int) -> Revision:
        r = self.conn.execute("SELECT * FROM plan_revisions WHERE id = ?", (revision_row_id,)).fetchone()
        if not r:
            raise records.NotFoundError(f"plan revision row {revision_row_id} not found")
        return _revision(r)

    def revisions_all(self) -> list[Revision]:
        rows = self.conn.execute("SELECT * FROM plan_revisions ORDER BY id").fetchall()
        return [_revision(r) for r in rows]

    def revision_active(self, run_id: str) -> Revision | None:
        r = self.conn.execute(
            "SELECT * FROM plan_revisions WHERE run_id = ? AND status = 'active' ORDER BY revision DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return _revision(r) if r else None

    def revision_mark_superseded(self, revision_row_id: int) -> None:
        with self.tx():
            self.conn.execute(
                "UPDATE plan_revisions SET status = 'superseded' WHERE id = ?", (revision_row_id,)
            )

    # -- tasks -------------------------------------------------------------------

    def task_insert(
        self,
        revision_row_id: int,
        task_id: str,
        objective: str,
        scope: dict,
        acceptance: list[str],
        verification: list[str],
        routing: dict,
        status: str,
        preread: list[str] | None = None,
    ) -> TaskRow:
        ts = now()
        with self.tx():
            self.conn.execute(
                "INSERT INTO tasks(revision_id, task_id, objective, scope_json, acceptance_json,"
                " verification_json, preread_json, routing_json, status, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    revision_row_id,
                    task_id,
                    objective,
                    json.dumps(scope),
                    json.dumps(acceptance),
                    json.dumps(verification),
                    json.dumps(preread or []),
                    json.dumps(routing),
                    status,
                    ts,
                    ts,
                ),
            )
        return self.task_get(revision_row_id, task_id)

    def task_get(self, revision_row_id: int, task_id: str) -> TaskRow:
        r = self.conn.execute(
            "SELECT * FROM tasks WHERE revision_id = ? AND task_id = ?",
            (revision_row_id, task_id),
        ).fetchone()
        if not r:
            raise records.NotFoundError(f"task {task_id} not found in revision row {revision_row_id}")
        return _task(r)

    def tasks_every(self) -> list[TaskRow]:
        rows = self.conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        return [_task(r) for r in rows]

    def tasks_all(self, revision_row_id: int) -> list[TaskRow]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE revision_id = ? ORDER BY id", (revision_row_id,)
        ).fetchall()
        return [_task(r) for r in rows]

    def task_update_status(
        self,
        revision_row_id: int,
        task_id: str,
        status: records.TaskStatus,
        failure_reason: str | None = None,
        clear_failure: bool = False,
    ) -> None:
        with self.tx():
            if clear_failure:
                self.conn.execute(
                    "UPDATE tasks SET status = ?, failure_reason = NULL, updated_at = ?"
                    " WHERE revision_id = ? AND task_id = ?",
                    (status.value, now(), revision_row_id, task_id),
                )
            elif failure_reason is not None:
                self.conn.execute(
                    "UPDATE tasks SET status = ?, failure_reason = ?, updated_at = ?"
                    " WHERE revision_id = ? AND task_id = ?",
                    (status.value, failure_reason, now(), revision_row_id, task_id),
                )
            else:
                self.conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE revision_id = ? AND task_id = ?",
                    (status.value, now(), revision_row_id, task_id),
                )

    def task_deps_insert(self, revision_row_id: int, task_id: str, depends_on: list[str]) -> None:
        with self.tx():
            for dep in depends_on:
                self.conn.execute(
                    "INSERT OR IGNORE INTO task_dependencies(revision_id, task_id, depends_on)"
                    " VALUES(?,?,?)",
                    (revision_row_id, task_id, dep),
                )

    def deps_for(self, revision_row_id: int, task_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT depends_on FROM task_dependencies WHERE revision_id = ? AND task_id = ?"
            " ORDER BY depends_on",
            (revision_row_id, task_id),
        ).fetchall()
        return [r["depends_on"] for r in rows]

    def task_event_add(
        self,
        revision_row_id: int,
        task_id: str,
        from_status: str | None,
        to_status: str,
        event: str,
        reason: str | None = None,
    ) -> None:
        with self.tx():
            self.conn.execute(
                "INSERT INTO task_events(revision_id, task_id, from_status, to_status, event,"
                " reason, created_at) VALUES(?,?,?,?,?,?,?)",
                (revision_row_id, task_id, from_status, to_status, event, reason, now()),
            )

    def task_events_all(self) -> list[TaskEvent]:
        rows = self.conn.execute(
            "SELECT * FROM task_events ORDER BY created_at, id"
        ).fetchall()
        return [_event(r) for r in rows]

    def task_events(self, revision_row_id: int, task_id: str) -> list[TaskEvent]:
        rows = self.conn.execute(
            "SELECT * FROM task_events WHERE revision_id = ? AND task_id = ? ORDER BY id",
            (revision_row_id, task_id),
        ).fetchall()
        return [_event(r) for r in rows]

    # -- attempts / evidence ------------------------------------------------------

    def attempt_create(
        self,
        revision_row_id: int | None,
        role: str,
        profile: str,
        driver: str,
        harness: str,
        model_id: str,
        requested_effort: str,
        routing_reason: str | None = None,
        fallback_used: bool = False,
        task_id: str | None = None,
        assignment_id: str | None = None,
        started: bool = True,
        effort_source: str | None = None,
        isolation: str | None = None,
        session_ref: str | None = None,
        run_id: str | None = None,
        verify_entry: str | None = None,
    ) -> Attempt:
        with self.tx():
            resolved_run = run_id
            if resolved_run is None and revision_row_id is not None:
                parent = self.conn.execute(
                    "SELECT run_id FROM plan_revisions WHERE id = ?",
                    (revision_row_id,),
                ).fetchone()
                if parent is not None:
                    resolved_run = parent["run_id"]
            if resolved_run is None and assignment_id is not None:
                parent = self.conn.execute(
                    "SELECT run_id FROM planning_assignments WHERE id = ?",
                    (assignment_id,),
                ).fetchone()
                if parent is not None:
                    resolved_run = parent["run_id"]
            cur = self.conn.execute(
                "INSERT INTO attempts(revision_id, task_id, assignment_id, role, profile, driver,"
                " harness, model, requested_effort, actual_effort, effort_source, fallback_used,"
                " routing_reason, isolation, started_at, session_ref, run_id, verify_entry)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    revision_row_id,
                    task_id,
                    assignment_id,
                    role,
                    profile,
                    driver,
                    harness,
                    model_id,
                    requested_effort,
                    None,
                    effort_source,
                    1 if fallback_used else 0,
                    routing_reason,
                    isolation,
                    now() if started else None,
                    session_ref,
                    resolved_run,
                    verify_entry,
                ),
            )
            aid = cur.lastrowid
        return self.attempt_get(aid)

    def attempt_open_for_assignment(self, assignment_id: str) -> Attempt | None:
        """The planner attempt still open for a waiting assignment, if any."""
        r = self.conn.execute(
            "SELECT * FROM attempts WHERE assignment_id = ? AND role = 'planner'"
            " AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            (assignment_id,),
        ).fetchone()
        return _attempt(r) if r else None

    def attempt_open_verifier_for_entry(
        self, revision_row_id: int, task_id: str, entry: str
    ) -> Attempt | None:
        """The host verifier attempt still open for one verification entry.
        Dispatch creates it; a verdict submitted later must close THIS attempt
        (identity fixed at dispatch), never a re-route."""
        r = self.conn.execute(
            "SELECT * FROM attempts WHERE revision_id = ? AND task_id = ?"
            " AND role = 'verifier' AND verify_entry = ? AND ended_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (revision_row_id, task_id, entry),
        ).fetchone()
        return _attempt(r) if r else None

    def attempt_mark_usage_missing(self, attempt_id: int, reason: str) -> None:
        """Record why no usage observation was stored. Does not invent tokens."""
        if not reason or not reason.strip():
            return
        with self.tx():
            self.conn.execute(
                "UPDATE attempts SET usage_missing_reason = ? WHERE id = ?",
                (reason, attempt_id),
            )

    def attempts_all(self) -> list[Attempt]:
        rows = self.conn.execute("SELECT * FROM attempts ORDER BY id").fetchall()
        return [_attempt(r) for r in rows]

    def attempt_get(self, attempt_id: int) -> Attempt:
        r = self.conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if not r:
            raise records.NotFoundError(f"attempt {attempt_id} not found")
        return _attempt(r)

    def attempt_latest_for_task(self, revision_row_id: int, task_id: str) -> Attempt | None:
        r = self.conn.execute(
            "SELECT * FROM attempts WHERE revision_id = ? AND task_id = ? ORDER BY id DESC LIMIT 1",
            (revision_row_id, task_id),
        ).fetchone()
        return _attempt(r) if r else None

    def attempt_update(
        self,
        attempt_id: int,
        started_at: str | None = None,
        ended_at: str | None = None,
        result: str | None = None,
        failure_reason: str | None = None,
        actual_effort: str | None = None,
        effort_source: str | None = None,
        session_ref: str | None = None,
        actual_model: str | None = None,
        model_source: str | None = None,
    ) -> None:
        sets: list[str] = []
        args: list = []
        if started_at is not None:
            sets.append("started_at = ?")
            args.append(started_at)
        if ended_at is not None:
            sets.append("ended_at = ?")
            args.append(ended_at)
        if result is not None:
            sets.append("result = ?")
            args.append(result)
        if failure_reason is not None:
            sets.append("failure_reason = ?")
            args.append(failure_reason)
        if actual_effort is not None:
            sets.append("actual_effort = ?")
            args.append(actual_effort)
        if effort_source is not None:
            sets.append("effort_source = ?")
            args.append(effort_source)
        if session_ref is not None:
            sets.append("session_ref = ?")
            args.append(session_ref)
        if actual_model is not None:
            sets.append("actual_model = ?")
            args.append(actual_model)
        if model_source is not None:
            sets.append("model_source = ?")
            args.append(model_source)
        if not sets:
            return
        args.append(attempt_id)
        with self.tx():
            self.conn.execute(f"UPDATE attempts SET {', '.join(sets)} WHERE id = ?", args)

    def evidence_add(self, attempt_id: int, kind: str, path: str) -> None:
        with self.tx():
            self.conn.execute(
                "INSERT INTO evidence(attempt_id, kind, path, created_at) VALUES(?,?,?,?)",
                (attempt_id, kind, path, now()),
            )

    def evidence_for_task(self, revision_row_id: int, task_id: str) -> list[tuple[str, str]]:
        """(kind, path) pairs for every evidence row attached to a task's
        attempts, oldest first — the verifier context handoff."""
        rows = self.conn.execute(
            "SELECT e.kind AS kind, e.path AS path FROM evidence e"
            " JOIN attempts a ON e.attempt_id = a.id"
            " WHERE a.revision_id = ? AND a.task_id = ? ORDER BY e.id",
            (revision_row_id, task_id),
        ).fetchall()
        return [(r["kind"], r["path"]) for r in rows]

    # -- verifications ----------------------------------------------------------

    def verification_add(
        self,
        revision_row_id: int,
        task_id: str,
        kind: str,
        command: str,
        passed: bool,
        attempt_id: int | None = None,
        exit_code: int | None = None,
        output_path: str | None = None,
        required_capabilities: list[str] | None = None,
    ) -> Verification:
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO verifications(revision_id, task_id, attempt_id, kind, command,"
                " required_capabilities_json, exit_code, passed, output_path, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (revision_row_id, task_id, attempt_id, kind, command,
                 json.dumps(required_capabilities or []), exit_code,
                 1 if passed else 0, output_path, now()),
            )
            vid = cur.lastrowid
        r = self.conn.execute("SELECT * FROM verifications WHERE id = ?", (vid,)).fetchone()
        return _verification(r)

    def verifications_all(self) -> list[Verification]:
        rows = self.conn.execute(
            "SELECT * FROM verifications ORDER BY created_at, id"
        ).fetchall()
        return [_verification(r) for r in rows]

    def verifications_for(self, revision_row_id: int, task_id: str) -> list[Verification]:
        rows = self.conn.execute(
            "SELECT * FROM verifications WHERE revision_id = ? AND task_id = ? ORDER BY id",
            (revision_row_id, task_id),
        ).fetchall()
        return [_verification(r) for r in rows]

    def attempt_current_worker_for_task(
        self, revision_row_id: int, task_id: str
    ) -> Attempt | None:
        """The latest worker attempt for a task — the boundary between the
        task's current verification window and its per-attempt history.

        A retry routes a fresh worker attempt, so that attempt plus everything
        created after it (the verifier attempts dispatched on top of it) is
        the current round; earlier attempts are history."""
        r = self.conn.execute(
            "SELECT * FROM attempts WHERE revision_id = ? AND task_id = ?"
            " AND role = 'worker' ORDER BY id DESC LIMIT 1",
            (revision_row_id, task_id),
        ).fetchone()
        return _attempt(r) if r else None

    def verifications_for_attempt(self, attempt_id: int) -> list[Verification]:
        """Every verification row recorded for one attempt — the per-attempt
        history read. Rows with no attempt binding (attempt_id NULL) belong
        to no attempt's list and are only reachable through the task reads."""
        rows = self.conn.execute(
            "SELECT * FROM verifications WHERE attempt_id = ? ORDER BY id",
            (attempt_id,),
        ).fetchall()
        return [_verification(r) for r in rows]

    def verifications_current(
        self, revision_row_id: int, task_id: str
    ) -> list[Verification]:
        """The task's CURRENT verification rows: everything recorded in the
        current attempt window.

        Verification history is append-only per attempt — a retry deletes
        nothing — so "current" is a window over the rows, not the whole
        table: rows bound to the latest worker attempt, rows bound to
        attempts created after it (the verifier attempts dispatched for that
        round), and unbound rows (attempt_id NULL). Rows bound to older
        attempts are history: still queryable (`verifications_for_attempt`,
        `verifications_for`), never read as the task's current result. With
        no worker attempt at all, nothing was superseded and every row is
        current."""
        current = self.attempt_current_worker_for_task(revision_row_id, task_id)
        boundary = current.id if current is not None else 0
        rows = self.conn.execute(
            "SELECT * FROM verifications"
            " WHERE revision_id = ? AND task_id = ?"
            " AND (attempt_id IS NULL OR attempt_id >= ?)"
            " ORDER BY id",
            (revision_row_id, task_id, boundary),
        ).fetchall()
        return [_verification(r) for r in rows]

    # -- replan correspondence / reports / artifact provenance (G004) ------

    def _replan_task_row_id(self, run_id: str, revision: int, task_id: str) -> int:
        """Resolve a (revision number, task_id) pair inside one run to its
        tasks row. Identity is the pair, never the bare task number: the same
        task_id on another revision of the same run is different work and
        never satisfies this lookup."""
        row = self.conn.execute(
            "SELECT t.id AS task_row_id FROM tasks t"
            " JOIN plan_revisions pr ON pr.id = t.revision_id"
            " WHERE pr.run_id = ? AND pr.revision = ? AND t.task_id = ?",
            (run_id, revision, task_id),
        ).fetchone()
        if not row:
            raise records.NotFoundError(
                f"replan task ({run_id}, revision {revision}, {task_id}) not found"
            )
        return row["task_row_id"]

    def _replan_mapping_assemble(self, header: sqlite3.Row) -> ReplanMappingRow:
        mid = header["id"]
        tasks: list[ReplanTaskMappingRow] = []
        for t in self.conn.execute(
            "SELECT * FROM replan_task_mappings WHERE mapping_id = ? ORDER BY id", (mid,)
        ).fetchall():
            sources = [
                _replan_source(s)
                for s in self.conn.execute(
                    "SELECT * FROM replan_sources WHERE task_mapping_id = ? ORDER BY id",
                    (t["id"],),
                ).fetchall()
            ]
            tasks.append(_replan_task_mapping(t, sources))
        superseded = [
            _replan_superseded(s)
            for s in self.conn.execute(
                "SELECT * FROM replan_superseded WHERE mapping_id = ?"
                " ORDER BY source_revision, source_task_id, id",
                (mid,),
            ).fetchall()
        ]
        return ReplanMappingRow(
            id=mid,
            run_id=header["run_id"],
            revision_id=header["revision_id"],
            prior_revision=header["prior_revision"],
            created_at=header["created_at"],
            tasks=tasks,
            superseded=superseded,
        )

    def replan_mapping_save(self, revision_row_id: int, mapping) -> ReplanMappingRow:
        """Persist one declared replan correspondence (docs/replan-contract.md §3).

        ``mapping`` is a ``plan.ReplanMapping`` (duck-typed): ``prior_revision``,
        ``tasks`` (task / classification / sources / redo_reason /
        confirm_verification / artifacts) and ``superseded`` declarations.
        Every source and superseded entry resolves by (revision, task_id)
        inside the revision's run — an unresolvable pair is an error, not a
        guess — and each new task must exist in the revision. Mappings are
        append-only per revision: saving a second mapping for one revision is
        a ConflictError. Nothing here derives a correspondence from task
        numbers or inherits a prior passed status.
        """
        with self.tx():
            rev = self.conn.execute(
                "SELECT id, run_id FROM plan_revisions WHERE id = ?", (revision_row_id,)
            ).fetchone()
            if not rev:
                raise records.NotFoundError(f"plan revision row {revision_row_id} not found")
            existing = self.conn.execute(
                "SELECT id FROM replan_mappings WHERE revision_id = ?", (revision_row_id,)
            ).fetchone()
            if existing:
                raise records.ConflictError(
                    f"revision row {revision_row_id} already has replan mapping {existing['id']};"
                    " declared mappings are append-only"
                )
            ts = now()
            revision_number = self.conn.execute(
                "SELECT revision FROM plan_revisions WHERE id = ?", (revision_row_id,)
            ).fetchone()["revision"]
            cur = self.conn.execute(
                "INSERT INTO replan_mappings(run_id, revision_id, prior_revision, created_at)"
                " VALUES(?,?,?,?)",
                (rev["run_id"], revision_row_id, int(mapping.prior_revision), ts),
            )
            mid = cur.lastrowid
            for t in mapping.tasks:
                try:
                    classification = records.ReplanClassification(t.classification).value
                except ValueError:
                    raise records.ORXError(
                        f"invalid replan classification {t.classification!r} for task {t.task}"
                    ) from None
                # The mapped task must be a task of this revision.
                self._replan_task_row_id(rev["run_id"], revision_number, t.task)
                tcur = self.conn.execute(
                    "INSERT INTO replan_task_mappings(mapping_id, run_id, revision_id, task_id,"
                    " classification, redo_reason, confirm_verification_json, artifacts_json,"
                    " created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (mid, rev["run_id"], revision_row_id, t.task, classification,
                     t.redo_reason or None, json.dumps(t.confirm_verification),
                     json.dumps(t.artifacts), ts),
                )
                for s in t.sources:
                    source_row_id = self._replan_task_row_id(rev["run_id"], s.revision, s.task_id)
                    self.conn.execute(
                        "INSERT INTO replan_sources(task_mapping_id, run_id, source_revision,"
                        " source_task_id, source_task_row_id, part, created_at)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (tcur.lastrowid, rev["run_id"], s.revision, s.task_id,
                         source_row_id, 1 if s.part else 0, ts),
                    )
            for s in mapping.superseded:
                try:
                    disposition = records.SupersededDisposition(s.disposition).value
                except ValueError:
                    raise records.ORXError(
                        f"invalid superseded disposition {s.disposition!r}"
                        f" for ({s.revision}, {s.task_id})"
                    ) from None
                source_row_id = self._replan_task_row_id(rev["run_id"], s.revision, s.task_id)
                self.conn.execute(
                    "INSERT INTO replan_superseded(mapping_id, run_id, source_revision,"
                    " source_task_id, source_task_row_id, disposition, successors_json,"
                    " note, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (mid, rev["run_id"], s.revision, s.task_id, source_row_id,
                     disposition, json.dumps(s.successors), s.note or None, ts),
                )
        return self.replan_mapping_for(revision_row_id)  # type: ignore[return-value]

    def replan_mapping_for(self, revision_row_id: int) -> ReplanMappingRow | None:
        """The declared mapping of one revision, or None when no mapping was
        recorded (a first plan, a pre-G004 revision, or an upgraded legacy
        database). None means unknown; it is never filled by inference."""
        header = self.conn.execute(
            "SELECT * FROM replan_mappings WHERE revision_id = ?", (revision_row_id,)
        ).fetchone()
        if not header:
            return None
        return self._replan_mapping_assemble(header)

    def replan_mappings_for_run(self, run_id: str) -> list[ReplanMappingRow]:
        """Every mapping declared in a run, oldest revision first — the
        multi-round trace input."""
        headers = self.conn.execute(
            "SELECT m.* FROM replan_mappings m"
            " JOIN plan_revisions pr ON pr.id = m.revision_id"
            " WHERE m.run_id = ? ORDER BY pr.revision, m.id",
            (run_id,),
        ).fetchall()
        return [self._replan_mapping_assemble(h) for h in headers]

    def replan_sources_for_task(self, revision_row_id: int, task_id: str) -> list[ReplanSourceRow]:
        """The declared sources of one replan task (trace backward)."""
        rows = self.conn.execute(
            "SELECT s.* FROM replan_sources s"
            " JOIN replan_task_mappings tm ON tm.id = s.task_mapping_id"
            " WHERE tm.revision_id = ? AND tm.task_id = ? ORDER BY s.id",
            (revision_row_id, task_id),
        ).fetchall()
        return [_replan_source(r) for r in rows]

    def replan_successors_for_source(
        self, run_id: str, source_revision: int, source_task_id: str
    ) -> list[ReplanSuccessorRow]:
        """The new-revision tasks that took over one old task (trace forward
        across renumbering): identity is (run, source_revision, task_id)."""
        rows = self.conn.execute(
            "SELECT pr.id AS revision_id, pr.revision AS revision, tm.task_id AS task_id,"
            " tm.classification AS classification, s.part AS part"
            " FROM replan_sources s"
            " JOIN replan_task_mappings tm ON tm.id = s.task_mapping_id"
            " JOIN replan_mappings m ON m.id = tm.mapping_id"
            " JOIN plan_revisions pr ON pr.id = m.revision_id"
            " WHERE s.run_id = ? AND s.source_revision = ? AND s.source_task_id = ?"
            " ORDER BY pr.revision, tm.task_id",
            (run_id, source_revision, source_task_id),
        ).fetchall()
        return [
            ReplanSuccessorRow(
                revision_id=r["revision_id"],
                revision=r["revision"],
                task_id=r["task_id"],
                classification=r["classification"],
                part=bool(r["part"]),
            )
            for r in rows
        ]

    def replan_trace_chain(
        self, run_id: str, revision: int, task_id: str
    ) -> list[ReplanTraceStep]:
        """Forward trace across replan rounds: every hop that carries this
        task's work forward, through renumbering, splits (one task to many
        successors), merges (many sources into one task), and sources that
        reach back past prior_revision. Hop order is by revision, then task.
        A cycle in malformed data cannot loop forever: each (from, to) hop
        is emitted once."""
        steps: list[ReplanTraceStep] = []
        emitted: set[tuple[int, str, int, str]] = set()
        frontier = {(revision, task_id)}
        mappings = self.conn.execute(
            "SELECT m.id AS mapping_id, pr.revision AS revision FROM replan_mappings m"
            " JOIN plan_revisions pr ON pr.id = m.revision_id"
            " WHERE m.run_id = ? ORDER BY pr.revision, m.id",
            (run_id,),
        ).fetchall()
        for m in mappings:
            edges = self.conn.execute(
                "SELECT s.source_revision AS source_revision, s.source_task_id AS source_task_id,"
                " tm.task_id AS task_id, tm.classification AS classification, s.part AS part"
                " FROM replan_sources s"
                " JOIN replan_task_mappings tm ON tm.id = s.task_mapping_id"
                " WHERE tm.mapping_id = ? ORDER BY tm.task_id, s.source_revision, s.source_task_id",
                (m["mapping_id"],),
            ).fetchall()
            for e in edges:
                key = (e["source_revision"], e["source_task_id"])
                if key not in frontier:
                    continue
                hop = (key[0], key[1], m["revision"], e["task_id"])
                if hop in emitted:
                    continue
                emitted.add(hop)
                steps.append(
                    ReplanTraceStep(
                        from_revision=key[0],
                        from_task_id=key[1],
                        to_revision=m["revision"],
                        to_task_id=e["task_id"],
                        classification=e["classification"],
                        part=bool(e["part"]),
                    )
                )
                frontier.add((m["revision"], e["task_id"]))
        return steps

    def replan_report_add(self, run_id: str, prior_revision: int, payload: dict) -> ReplanReportRow:
        """Store one replan preflight report (the pre-check diff produced
        before a new revision takes effect). The report is a document; ORX
        stores it verbatim and never edits it afterwards."""
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO replan_reports(run_id, prior_revision, revision_id,"
                " payload_json, created_at) VALUES(?,?,NULL,?,?)",
                (run_id, int(prior_revision), json.dumps(payload), now()),
            )
            rid = cur.lastrowid
        r = self.conn.execute("SELECT * FROM replan_reports WHERE id = ?", (rid,)).fetchone()
        return _replan_report(r)

    def replan_report_bind(self, report_id: int, revision_row_id: int) -> None:
        """Bind a preflight report to the revision that actually landed.
        Binding is one-shot: re-binding the same revision is a no-op and a
        different revision is a ConflictError."""
        with self.tx():
            row = self.conn.execute(
                "SELECT revision_id FROM replan_reports WHERE id = ?", (report_id,)
            ).fetchone()
            if not row:
                raise records.NotFoundError(f"replan report {report_id} not found")
            rev = self.conn.execute(
                "SELECT id FROM plan_revisions WHERE id = ?", (revision_row_id,)
            ).fetchone()
            if not rev:
                raise records.NotFoundError(f"plan revision row {revision_row_id} not found")
            if row["revision_id"] is not None and row["revision_id"] != revision_row_id:
                raise records.ConflictError(
                    f"replan report {report_id} is already bound to revision row"
                    f" {row['revision_id']}"
                )
            self.conn.execute(
                "UPDATE replan_reports SET revision_id = ? WHERE id = ?",
                (revision_row_id, report_id),
            )

    def replan_reports_for_run(self, run_id: str) -> list[ReplanReportRow]:
        rows = self.conn.execute(
            "SELECT * FROM replan_reports WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [_replan_report(r) for r in rows]

    def replan_artifact_source_add(
        self,
        run_id: str,
        revision_row_id: int,
        task_id: str,
        source_revision: int,
        source_task_id: str,
        artifact: str,
        attempt_id: int | None = None,
        evidence_id: int | None = None,
    ) -> ReplanArtifactSourceRow:
        """Record one traceable artifact provenance row on a declared
        (task <-> source) edge.

        Artifacts are referenced through the correspondence, never by a bare
        task number: the edge must exist in this revision's declared mapping.
        ``attempt_id`` must be an attempt of the source task itself — the
        same task_id in another revision does not match. ``evidence_id``
        carries its own recorded attempt binding; when only evidence is
        given, that binding is the provenance attempt (a stored fact, not a
        guess). An identical row already stored is returned unchanged.
        """
        with self.tx():
            edge = self.conn.execute(
                "SELECT tm.id FROM replan_task_mappings tm"
                " JOIN replan_sources s ON s.task_mapping_id = tm.id"
                " WHERE tm.revision_id = ? AND tm.task_id = ?"
                " AND s.source_revision = ? AND s.source_task_id = ?",
                (revision_row_id, task_id, source_revision, source_task_id),
            ).fetchone()
            if not edge:
                raise records.ORXError(
                    f"no declared correspondence edge for (revision row {revision_row_id},"
                    f" {task_id}) <-> ({source_revision}, {source_task_id});"
                    " artifacts are referenced through the mapping, not by task number"
                )
            if evidence_id is not None:
                ev = self.conn.execute(
                    "SELECT id, attempt_id FROM evidence WHERE id = ?", (evidence_id,)
                ).fetchone()
                if not ev:
                    raise records.NotFoundError(f"evidence {evidence_id} not found")
                if attempt_id is None:
                    # the evidence row's own recorded binding is the fact
                    attempt_id = ev["attempt_id"]
                elif ev["attempt_id"] != attempt_id:
                    raise records.ORXError(
                        f"evidence {evidence_id} belongs to attempt {ev['attempt_id']},"
                        f" not attempt {attempt_id}"
                    )
            if attempt_id is not None:
                a = self.conn.execute(
                    "SELECT a.id AS id, a.task_id AS task_id, pr.revision AS revision,"
                    " pr.run_id AS run_id FROM attempts a"
                    " LEFT JOIN plan_revisions pr ON pr.id = a.revision_id"
                    " WHERE a.id = ?",
                    (attempt_id,),
                ).fetchone()
                if not a:
                    raise records.NotFoundError(f"attempt {attempt_id} not found")
                if (
                    a["run_id"] != run_id
                    or a["revision"] != source_revision
                    or a["task_id"] != source_task_id
                ):
                    raise records.ORXError(
                        f"attempt {attempt_id} belongs to ({a['run_id']}, revision"
                        f" {a['revision']}, task {a['task_id']}); the declared source is"
                        f" ({run_id}, revision {source_revision}, task {source_task_id})"
                        " — provenance must name the source task's own attempt,"
                        " never a same-numbered task in another revision"
                    )
            existing = self.conn.execute(
                "SELECT * FROM replan_artifact_sources WHERE revision_id = ? AND task_id = ?"
                " AND source_revision = ? AND source_task_id = ? AND artifact = ?"
                " AND attempt_id IS ? AND evidence_id IS ?",
                (revision_row_id, task_id, source_revision, source_task_id, artifact,
                 attempt_id, evidence_id),
            ).fetchone()
            if existing:
                return _replan_artifact_source(existing)
            self.conn.execute(
                "INSERT INTO replan_artifact_sources(run_id, revision_id, task_id,"
                " source_revision, source_task_id, artifact, attempt_id, evidence_id,"
                " created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, revision_row_id, task_id, source_revision, source_task_id,
                 artifact, attempt_id, evidence_id, now()),
            )
        r = self.conn.execute(
            "SELECT * FROM replan_artifact_sources WHERE run_id = ? AND revision_id = ?"
            " AND task_id = ? AND source_revision = ? AND source_task_id = ? AND artifact = ?"
            " AND attempt_id IS ? AND evidence_id IS ? ORDER BY id DESC LIMIT 1",
            (run_id, revision_row_id, task_id, source_revision, source_task_id, artifact,
             attempt_id, evidence_id),
        ).fetchone()
        return _replan_artifact_source(r)

    def replan_artifact_sources_for_task(
        self, revision_row_id: int, task_id: str
    ) -> list[ReplanArtifactSourceRow]:
        """Every provenance row hanging on one replan task, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM replan_artifact_sources WHERE revision_id = ? AND task_id = ?"
            " ORDER BY id",
            (revision_row_id, task_id),
        ).fetchall()
        return [_replan_artifact_source(r) for r in rows]

    def replan_artifact_sources_for_source(
        self, run_id: str, source_revision: int, source_task_id: str
    ) -> list[ReplanArtifactSourceRow]:
        """Every provenance row citing one old task as its source."""
        rows = self.conn.execute(
            "SELECT * FROM replan_artifact_sources WHERE run_id = ? AND source_revision = ?"
            " AND source_task_id = ? ORDER BY id",
            (run_id, source_revision, source_task_id),
        ).fetchall()
        return [_replan_artifact_source(r) for r in rows]

    # -- routing decisions ---------------------------------------------------------

    def routing_decision_add(
        self,
        role: str,
        requested: dict,
        candidates: list,
        selected: str | None,
        reason: str | None,
        downgrade_blocked: bool = False,
        attempt_id: int | None = None,
    ) -> RoutingDecision:
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO routing_decisions(attempt_id, role, requested_json, candidates_json,"
                " selected, reason, downgrade_blocked, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (attempt_id, role, json.dumps(requested), json.dumps(candidates), selected,
                 reason, 1 if downgrade_blocked else 0, now()),
            )
            did = cur.lastrowid
        r = self.conn.execute("SELECT * FROM routing_decisions WHERE id = ?", (did,)).fetchone()
        return _decision(r)

    def routing_decisions_all(self) -> list[RoutingDecision]:
        rows = self.conn.execute("SELECT * FROM routing_decisions ORDER BY id").fetchall()
        return [_decision(r) for r in rows]

    # -- resource status -----------------------------------------------------------

    def resource_get(self, profile: str) -> records.ResourceStatus:
        """A missing row means `unknown`. Never reads or writes TOML."""
        r = self.conn.execute(
            "SELECT status FROM resource_status WHERE profile = ?", (profile,)
        ).fetchone()
        if not r:
            return records.ResourceStatus.UNKNOWN
        return records.ResourceStatus(r["status"])

    def resource_set(self, profile: str, status: records.ResourceStatus, note: str = "",
                      override: bool = True) -> None:
        """Manual status change: marks the row override=1 so health
        auto-learning never clobbers an explicit operator decision."""
        with self.tx():
            self.conn.execute(
                "INSERT INTO resource_status(profile, status, note, updated_at, override)"
                " VALUES(?,?,?,?,1)"
                " ON CONFLICT(profile) DO UPDATE SET status = excluded.status,"
                " note = excluded.note, updated_at = excluded.updated_at, override = 1",
                (profile, status.value, note, now()),
            )

    def resource_clear(self, profile: str) -> None:
        """Re-enable health auto-learning for a profile (override -> 0)."""
        with self.tx():
            self.conn.execute(
                "UPDATE resource_status SET override = 0, updated_at = ? WHERE profile = ?",
                (now(), profile),
            )

    def resource_learn(self, profile: str, **fields) -> None:
        """Auto-learning write. Never touches rows with override = 1; a
        profile without a row is inserted (learning must not silently drop
        just because init never seeded it)."""
        with self.tx():
            row = self.conn.execute(
                "SELECT override FROM resource_status WHERE profile = ?", (profile,)
            ).fetchone()
            if row is not None and row["override"]:
                return
            if row is None:
                self.conn.execute(
                    "INSERT OR IGNORE INTO resource_status(profile, status, note, updated_at)"
                    " VALUES(?, 'unknown', '', ?)",
                    (profile, now()),
                )
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.conn.execute(
                f"UPDATE resource_status SET {sets}, updated_at = ? WHERE profile = ?",
                (*fields.values(), now(), profile),
            )

    def resource_row(self, profile: str) -> ResourceRow | None:
        r = self.conn.execute(
            "SELECT * FROM resource_status WHERE profile = ?", (profile,)
        ).fetchone()
        return _resource(r) if r else None

    def resource_rows(self) -> list[ResourceRow]:
        rows = self.conn.execute("SELECT * FROM resource_status ORDER BY profile").fetchall()
        return [_resource(r) for r in rows]

    # -- usage observations (M1 P5) --------------------------------------

    def usage_add(self, attempt_id: int, profile: str, run_id: str, task_id: str | None,
                  input_tokens: int | None, output_tokens: int | None,
                  cached_input_tokens: int | None, source: str, accuracy: str) -> None:
        with self.tx():
            self.conn.execute(
                "INSERT INTO usage_observations(attempt_id, profile, run_id, task_id,"
                " input_tokens, output_tokens, cached_input_tokens, source, accuracy, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, profile, run_id, task_id, input_tokens, output_tokens,
                 cached_input_tokens, source, accuracy, now()),
            )
            # A stored observation supersedes an earlier "missing" reason.
            self.conn.execute(
                "UPDATE attempts SET usage_missing_reason = NULL WHERE id = ?",
                (attempt_id,),
            )

    def usage_record_host(
        self,
        attempt_id: int,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int | None,
        accuracy: str,
    ) -> dict:
        """Persist one host_report for an attempt.

        Profile, run, and task come from the attempt and its stored run
        association. Callers cannot supply those. An identical report is
        a no-op. A different report for the same attempt is rejected so a
        second insert cannot inflate token totals. Cached may exceed input.
        Missing cached stays NULL. No runner normalization and no fee.
        """
        input_tokens = _nonnegative_count("input", input_tokens)
        output_tokens = _nonnegative_count("output", output_tokens)
        cached_input_tokens = _nonnegative_count(
            "cached", cached_input_tokens, allow_none=True
        )
        if accuracy not in ("exact", "estimated"):
            raise records.ORXError(
                "--accuracy must be 'exact' or 'estimated'"
            )
        attempt = self.attempt_get(attempt_id)
        if not attempt.run_id:
            raise records.ORXError(
                f"attempt {attempt_id} has no run association; "
                "usage metadata is taken from the attempt and is not guessed"
            )
        incoming = (input_tokens, output_tokens, cached_input_tokens, accuracy)
        with self.tx():
            existing = self.conn.execute(
                "SELECT * FROM usage_observations WHERE attempt_id = ? AND source = ?"
                " ORDER BY id",
                (attempt_id, "host_report"),
            ).fetchall()
            if existing:
                mismatched = [
                    row for row in existing if _host_report_key(row) != incoming
                ]
                if mismatched:
                    row = mismatched[0]
                    raise records.ConflictError(
                        f"attempt {attempt_id} already has a host_report observation "
                        f"(input={row['input_tokens']}, output={row['output_tokens']}, "
                        f"cached={row['cached_input_tokens']}, accuracy={row['accuracy']}); "
                        f"refusing to replace it with input={input_tokens}, "
                        f"output={output_tokens}, cached={cached_input_tokens}, "
                        f"accuracy={accuracy}"
                    )
                return _host_report_payload(attempt, existing[0], idempotent=True)
            self.usage_add(
                attempt.id,
                attempt.profile,
                attempt.run_id,
                attempt.task_id,
                input_tokens,
                output_tokens,
                cached_input_tokens,
                "host_report",
                accuracy,
            )
            stored = self.conn.execute(
                "SELECT * FROM usage_observations WHERE attempt_id = ? AND source = ?"
                " ORDER BY id DESC LIMIT 1",
                (attempt_id, "host_report"),
            ).fetchone()
        return _host_report_payload(attempt, stored, idempotent=False)

    def usage_rows(self, profile: str | None = None) -> list[sqlite3.Row]:
        if profile is None:
            return self.conn.execute(
                "SELECT * FROM usage_observations ORDER BY id").fetchall()
        return self.conn.execute(
            "SELECT * FROM usage_observations WHERE profile = ? ORDER BY id",
            (profile,)).fetchall()

    def seed_resources(self, profile_names: list[str]) -> None:
        """orx init: every profile starts `unknown` so it stays routable."""
        with self.tx():
            for name in profile_names:
                self.conn.execute(
                    "INSERT OR IGNORE INTO resource_status(profile, status, note, updated_at)"
                    " VALUES(?, 'unknown', '', ?)",
                    (name, now()),
                )

    # -- inbox / external events -------------------------------------------------

    def external_event_find(self, source: str, external_id: str) -> int | None:
        """Row id of (source, external_id), or None when never fetched."""
        row = self.conn.execute(
            "SELECT id FROM external_events WHERE source = ? AND external_id = ?",
            (source, external_id),
        ).fetchone()
        return row["id"] if row else None

    def external_event_add(self, source: str, external_id: str, kind: str, payload: dict) -> int:
        """INSERT OR IGNORE-style dedupe: when (source, external_id) already
        exists the existing row id is returned and the row is never rewritten
        (the first fetch's payload and fetched_at stay authoritative)."""
        with self.tx():
            self.conn.execute(
                "INSERT OR IGNORE INTO external_events(source, external_id, kind,"
                " payload_json, fetched_at) VALUES(?,?,?,?,?)",
                (source, external_id, kind, json.dumps(payload), now()),
            )
            row = self.conn.execute(
                "SELECT id FROM external_events WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
        return row["id"]

    def _inbox_row(self, r: sqlite3.Row) -> dict:
        event = self.conn.execute(
            "SELECT source, external_id, kind FROM external_events WHERE id = ?",
            (r["event_id"],),
        ).fetchone()
        return {
            "id": r["id"],
            "event_id": r["event_id"],
            "source": event["source"] if event else None,
            "external_id": event["external_id"] if event else None,
            "kind": event["kind"] if event else None,
            "title": r["title"],
            "body": r["body"],
            "url": r["url"],
            "status": r["status"],
            "goal_id": r["goal_id"],
            "created_at": r["created_at"],
            "decided_at": r["decided_at"],
        }

    def inbox_add(self, event_id: int, title: str, body: str = "", url: str = "") -> int:
        """Create the pending inbox item for an event. At most one item exists
        per event: when one already exists its id is returned unchanged."""
        with self.tx():
            row = self.conn.execute(
                "SELECT id FROM inbox_items WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is not None:
                return row["id"]
            cur = self.conn.execute(
                "INSERT INTO inbox_items(event_id, title, body, url, status, created_at)"
                " VALUES(?,?,?,?,'pending',?)",
                (event_id, title, body, url, now()),
            )
            return cur.lastrowid

    def inbox_items(self, status: str | None = None) -> list[dict]:
        """Inbox rows joined with their event source, oldest first.
        status filters to one of pending|accepted|rejected|dismissed."""
        if status is not None and status not in INBOX_ITEM_STATUSES:
            raise records.ORXError(
                f"invalid inbox status {status!r}"
                f" (expected {' | '.join(INBOX_ITEM_STATUSES)})"
            )
        if status is None:
            rows = self.conn.execute("SELECT * FROM inbox_items ORDER BY id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM inbox_items WHERE status = ? ORDER BY id", (status,)
            ).fetchall()
        return [self._inbox_row(r) for r in rows]

    def inbox_item_get(self, item_id: int) -> dict:
        r = self.conn.execute(
            "SELECT * FROM inbox_items WHERE id = ?", (item_id,)
        ).fetchone()
        if not r:
            raise records.NotFoundError(f"inbox item {item_id} not found")
        return self._inbox_row(r)

    def inbox_decide(self, item_id: int, status: str, goal_id: str | None = None) -> dict:
        """Move a pending item to accepted/rejected/dismissed, optionally
        linking the Goal an acceptance created."""
        if status not in INBOX_DECIDED_STATUSES:
            raise records.ORXError(
                f"invalid inbox decision {status!r}"
                f" (expected {' | '.join(INBOX_DECIDED_STATUSES)})"
            )
        with self.tx():
            cur = self.conn.execute(
                "UPDATE inbox_items SET status = ?, goal_id = ?, decided_at = ?"
                " WHERE id = ?",
                (status, goal_id, now(), item_id),
            )
            if cur.rowcount == 0:
                raise records.NotFoundError(f"inbox item {item_id} not found")
        return self.inbox_item_get(item_id)
