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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from orx import records
from orx.records import MigrationError

CODE_SCHEMA_VERSION = 1


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
  updated_at TEXT NOT NULL
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
  failure_reason TEXT
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
                                         'exhausted','unavailable','unknown')),
  note TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
"""


def _migrate_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_V1)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(CODE_SCHEMA_VERSION),),
    )


# Migrations keyed by the version they produce.
MIGRATIONS: dict[int, callable] = {1: _migrate_v1}


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
class ResourceRow:
    profile: str
    status: str
    note: str
    updated_at: str


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


def _resource(r: sqlite3.Row) -> ResourceRow:
    return ResourceRow(
        profile=r["profile"],
        status=r["status"],
        note=r["note"],
        updated_at=r["updated_at"],
    )


# ---------------------------------------------------------------------------


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
            shutil.copy2(path, tmp)
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
            conn.close()
            os.replace(tmp, path)
            # Remove stale WAL/SHM sidecars from the pre-migration database.
            for suffix in ("-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
        finally:
            tmp.unlink(missing_ok=True)
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
            ts = now()
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
                    ts,
                    ts,
                ),
            )
            run_count = self.conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
            run_id = f"R{run_count + 1:03d}"
            self.conn.execute(
                "INSERT INTO runs(id, goal_id, status, created_at, updated_at) VALUES(?,?,?,?,?)",
                (run_id, goal_id, records.RunStatus.PLANNING.value, ts, ts),
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

    def run_set_status(self, run_id: str, status: records.RunStatus) -> None:
        with self.tx():
            self.conn.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, now(), run_id),
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
    ) -> TaskRow:
        ts = now()
        with self.tx():
            self.conn.execute(
                "INSERT INTO tasks(revision_id, task_id, objective, scope_json, acceptance_json,"
                " verification_json, routing_json, status, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    revision_row_id,
                    task_id,
                    objective,
                    json.dumps(scope),
                    json.dumps(acceptance),
                    json.dumps(verification),
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
    ) -> Attempt:
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO attempts(revision_id, task_id, assignment_id, role, profile, driver,"
                " harness, model, requested_effort, actual_effort, effort_source, fallback_used,"
                " routing_reason, started_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    now() if started else None,
                ),
            )
            aid = cur.lastrowid
        return self.attempt_get(aid)

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

    def verifications_for(self, revision_row_id: int, task_id: str) -> list[Verification]:
        rows = self.conn.execute(
            "SELECT * FROM verifications WHERE revision_id = ? AND task_id = ? ORDER BY id",
            (revision_row_id, task_id),
        ).fetchall()
        return [_verification(r) for r in rows]

    def verifications_clear(self, revision_row_id: int, task_id: str) -> None:
        """Retry semantics: verification rows are per-attempt state. A retry
        starts a fresh verification context (history lives in task_events)."""
        with self.tx():
            self.conn.execute(
                "DELETE FROM verifications WHERE revision_id = ? AND task_id = ?",
                (revision_row_id, task_id),
            )

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

    def resource_set(self, profile: str, status: records.ResourceStatus, note: str = "") -> None:
        with self.tx():
            self.conn.execute(
                "INSERT INTO resource_status(profile, status, note, updated_at) VALUES(?,?,?,?)"
                " ON CONFLICT(profile) DO UPDATE SET status = excluded.status,"
                " note = excluded.note, updated_at = excluded.updated_at",
                (profile, status.value, note, now()),
            )

    def resource_rows(self) -> list[ResourceRow]:
        rows = self.conn.execute("SELECT * FROM resource_status ORDER BY profile").fetchall()
        return [_resource(r) for r in rows]

    def seed_resources(self, profile_names: list[str]) -> None:
        """orx init: every profile starts `unknown` so it stays routable."""
        with self.tx():
            for name in profile_names:
                self.conn.execute(
                    "INSERT OR IGNORE INTO resource_status(profile, status, note, updated_at)"
                    " VALUES(?, 'unknown', '', ?)",
                    (name, now()),
                )
