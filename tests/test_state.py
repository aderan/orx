"""Schema initialization, reopening/recovery, and store invariants."""

from __future__ import annotations

import sqlite3

import pytest

from orx import dispatch, machine, plan
from orx.records import ConflictError, MigrationError, NotFoundError, ORXError, TaskStatus
from orx.state import Store
import orx.state as state_mod

from conftest import ir_for, task_spec, write_evidence


def _strip_v7_columns(conn) -> None:
    """Drop v7/v8 columns so a reopen has to add them, as a real v4/v5/v6
    file would."""
    for table, column in (
        ("attempts", "session_ref"),
        ("attempts", "run_id"),
        ("attempts", "usage_missing_reason"),
        ("attempts", "verify_entry"),
        ("attempts", "actual_model"),
        ("attempts", "model_source"),
        ("runs", "started_at"),
        ("runs", "completed_at"),
    ):
        names = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in names:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def _narrow_usage_check(conn) -> None:
    """Restore the pre-v7 usage source CHECK (no host_report)."""
    conn.executescript("""
CREATE TABLE usage_observations_old (
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
INSERT INTO usage_observations_old
  SELECT id, attempt_id, profile, run_id, task_id, input_tokens, output_tokens,
         cached_input_tokens, source, accuracy, created_at
  FROM usage_observations;
DROP TABLE usage_observations;
ALTER TABLE usage_observations_old RENAME TO usage_observations;
""")


REPLAN_V9_TABLES = (
    "replan_mappings",
    "replan_task_mappings",
    "replan_sources",
    "replan_superseded",
    "replan_reports",
    "replan_artifact_sources",
)


def _drop_v9_tables(conn) -> None:
    """Drop the v9 replan tables so a reopen has to recreate them, as a real
    v8 file would look."""
    for table in reversed(REPLAN_V9_TABLES):
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def _mk_revision(store: Store, run_id: str, task_ids: list[str]) -> int:
    """A revision row with real task rows, for replan storage tests."""
    rev = store.revision_create(run_id, "standard", "host-planner", {"tasks": task_ids})
    for tid in task_ids:
        store.task_insert(rev.id, tid, f"objective {tid}", {}, [], [], {}, "pending")
    return rev.id


def test_schema_init_creates_tables_and_meta(tmp_path):
    db = tmp_path / "state.db"
    store = Store.open(db)
    try:
        assert store.schema_version() == 11
        names = {
            r["name"]
            for r in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        expected = {
            "meta", "goals", "runs", "plan_revisions", "planning_assignments",
            "tasks", "task_dependencies", "task_events", "attempts", "evidence",
            "verifications", "routing_decisions", "resource_status",
            "external_events", "inbox_items",
            # v9 (G004): replan correspondence, preflight reports, and
            # traceable artifact provenance, additive on top of v8.
            *REPLAN_V9_TABLES,
            # v10 (G006): append-only host worker progress reports.
            "attempt_progress",
        }
        assert expected <= names
        # v5: fresh databases create attempts with the isolation column in
        # place (docs/pbv-mapping.md §4.6); the guarded ALTER is a no-op here.
        columns = {
            r["name"] for r in store.conn.execute("PRAGMA table_info(attempts)")
        }
        assert "isolation" in columns
        assert {"session_ref", "run_id", "usage_missing_reason"} <= columns
        run_columns = {
            r["name"] for r in store.conn.execute("PRAGMA table_info(runs)")
        }
        assert {"started_at", "completed_at"} <= run_columns
        usage_sql = store.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'usage_observations'"
        ).fetchone()["sql"]
        assert "host_report" in usage_sql
        assert "unknown" in usage_sql
    finally:
        store.close()


def test_seed_resources_marks_every_profile_unknown(tmp_path):
    proj = dispatch.init_project(tmp_path)
    store = Store.open(tmp_path / ".orx" / "state.db")
    try:
        rows = store.resource_rows()
        assert [r.profile for r in rows] == ["orx-host"]
        assert rows[0].status == "unknown"
    finally:
        store.close()


def test_reopen_preserves_in_progress_state(project, goal, tmp_path):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance[:1], verification=["true"]),
        task_spec("T002", deps=["T001"], acceptance=goal.acceptance[1:]),
    ]))
    dispatch.run_slice(project)          # T001 -> waiting_host, T002 pending
    dispatch.task_claim(project, "T001")  # -> running
    project.close()

    reopened = dispatch.open_project()
    try:
        task1 = next(t for t in dispatch.task_list(reopened) if t["id"] == "T001")
        task2 = next(t for t in dispatch.task_list(reopened) if t["id"] == "T002")
        assert task1["status"] == "running"
        assert task2["status"] == "pending"
        assert reopened.store.goal_active().id == goal.id
        # The reopened project still accepts completions (claim stays honored).
        evidence = write_evidence(tmp_path)
        result = dispatch.task_complete(reopened, "T001", str(evidence))
        assert result["status"] == "passed"
    finally:
        reopened.close()


def test_newer_schema_version_refused_without_modifying_file(tmp_path):
    store = Store.open(tmp_path / "state.db")
    store.conn.execute("UPDATE meta SET value = '42' WHERE key = 'schema_version'")
    store.close()
    raw = (tmp_path / "state.db").read_bytes()

    with pytest.raises(MigrationError) as excinfo:
        Store.open(tmp_path / "state.db")
    assert "42" in str(excinfo.value)
    assert (tmp_path / "state.db").read_bytes() == raw


def test_tables_without_meta_refused(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE goals (id TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(MigrationError):
        Store.open(db)


def test_claim_is_single_winner(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    with pytest.raises(ConflictError):
        dispatch.task_claim(project, "T001")


def test_foreign_keys_enforced(project, goal):
    with pytest.raises(sqlite3.IntegrityError):
        project.store.attempt_create(
            revision_row_id=999999,
            role="worker",
            profile="host-worker",
            driver="host",
            harness="zcode",
            model_id="m-worker",
            requested_effort="medium",
        )


def test_verifications_current_window_and_per_attempt_history(project, goal):
    """Store-level contract for per-attempt history: rows are append-only,
    `verifications_for_attempt` returns each round's rows, and
    `verifications_current` keeps only the latest worker attempt's window —
    its own rows, later verifier attempts' rows, and unbound rows; never an
    older attempt's rows."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["true"]),
    ]))
    dispatch.run_slice(project)
    store = project.store
    goal_row = store.goal_active()
    revision = store.revision_active(store.run_for_goal(goal_row.id).id)

    def worker():
        return store.attempt_create(
            revision_row_id=revision.id, role="worker", profile="host-worker",
            driver="host", harness="zcode", model_id="m-worker",
            requested_effort="medium", task_id="T001",
        )

    def verifier():
        return store.attempt_create(
            revision_row_id=revision.id, role="verifier", profile="host-verifier",
            driver="host", harness="zcode", model_id="m-verifier",
            requested_effort="medium", task_id="T001",
        )

    # Round 1: an unbound row (no attempt existed yet), the worker's red
    # row, and a verifier row on top of it.
    store.verification_add(revision.id, "T001", "command", "true", passed=False)
    first = worker()
    store.verification_add(revision.id, "T001", "command", "true",
                           passed=False, attempt_id=first.id)
    first_verifier = verifier()
    store.verification_add(revision.id, "T001", "agent", "agent: looks fine",
                           passed=False, attempt_id=first_verifier.id)
    # Everything is current while round 1 is the latest round.
    assert [v.attempt_id for v in store.verifications_current(revision.id, "T001")] == [
        None, first.id, first_verifier.id,
    ]

    # Round 2 (the retry routes a fresh worker attempt): history stays...
    second = worker()
    store.verification_add(revision.id, "T001", "command", "true",
                           passed=True, attempt_id=second.id)
    assert len(store.verifications_for(revision.id, "T001")) == 4
    assert store.verifications_for_attempt(first.id) == [
        v for v in store.verifications_for(revision.id, "T001")
        if v.attempt_id == first.id
    ]
    assert store.verifications_for_attempt(first_verifier.id)[0].kind == "agent"
    # ...but the current view is the new window only: both bound rows of
    # round 1 are history. The unbound row has no attempt to be superseded
    # by, so it stays in the window — and always loses to the fresh row of
    # the same entry under latest-row-per-entry judgment.
    assert [v.attempt_id for v in store.verifications_current(revision.id, "T001")] == [
        None, second.id,
    ]
    assert all(
        v.attempt_id not in (first.id, first_verifier.id)
        for v in store.verifications_current(revision.id, "T001")
    )
    assert store.attempt_current_worker_for_task(revision.id, "T001").id == second.id


def test_wal_mode_enabled(project):
    mode = project.store.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_attempt_effort_observability_roundtrip(project, goal):
    from conftest import ir_for, task_spec
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ]))
    revision = project.store.revision_active(
        project.store.run_for_goal(goal.id).id)
    attempt = project.store.attempt_create(
        revision_row_id=revision.id,
        role="worker",
        profile="cli-fake",
        driver="cli",
        harness="shell",
        model_id="fake-model",
        requested_effort="high",
        task_id="T001",
    )
    assert attempt.actual_effort is None and attempt.effort_source is None
    project.store.attempt_update(
        attempt.id, actual_effort="high", effort_source="requested_validated",
        ended_at="2026-10-03T00:00:00+00:00", result="completed",
    )
    stored = project.store.attempt_get(attempt.id)
    assert stored.requested_effort == "high"
    assert stored.actual_effort == "high"
    assert stored.effort_source == "requested_validated"


def test_v1_to_v2_migration_preserves_resource_rows(tmp_path):
    """A v1 database upgrades in place (backup-replace-restore) and keeps
    every resource row; new health columns default; the new CHECK admits
    cooldown/auth_required."""
    import sqlite3
    from orx import state as state_mod
    from orx.records import ResourceStatus
    from orx.state import Store

    db = tmp_path / "v1.db"
    store = Store.open(db)  # code is v5; build a v1 db by hand
    store.close()
    conn = sqlite3.connect(db)
    conn.executescript("""
DROP TABLE inbox_items;
DROP TABLE external_events;
DROP TABLE resource_status;
CREATE TABLE resource_status (
  profile TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('abundant','available','constrained',
                                         'exhausted','unavailable','unknown')),
  note TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
UPDATE meta SET value = '1' WHERE key = 'schema_version';
INSERT INTO resource_status(profile, status, note, updated_at)
  VALUES ('legacy', 'exhausted', 'weekly quota', '2026-01-01T00:00:00+00:00');
""")
    conn.commit()
    conn.close()

    reopened = Store.open(db)
    try:
        assert reopened.schema_version() == 11
        row = reopened.resource_row("legacy")
        assert (row.status, row.note) == ("exhausted", "weekly quota")
        assert row.override == 0 and row.failure_streak == 0
        # new states are writable under the recreated CHECK
        reopened.resource_learn("legacy", status="cooldown", cooldown_until="2099-01-01T00:00:00+00:00")
        assert reopened.resource_row("legacy").status == "cooldown"
        reopened.resource_learn("legacy", status="auth_required")
        assert reopened.resource_row("legacy").status == "auth_required"
    finally:
        reopened.close()
    assert not list(tmp_path.glob("v1.db.migrate-*")), "stale migration backups"


def test_v4_to_v5_migration_adds_attempts_isolation(tmp_path):
    """A v4 database (attempts without isolation) upgrades on reopen: the
    column is added by migration v5, the schema version bumps, and existing
    rows survive with NULL isolation (docs/pbv-mapping.md §4.6)."""
    db = tmp_path / "v4.db"
    store = Store.open(db)  # code is v7; build a v4 db by hand
    store.conn.execute("ALTER TABLE attempts DROP COLUMN isolation")
    _strip_v7_columns(store.conn)
    store.conn.execute(
        "INSERT INTO attempts(role, profile, driver, harness, model,"
        " requested_effort) VALUES('worker', 'legacy', 'cli', 'shell', 'm', 'high')"
    )
    store.conn.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
    store.close()
    check = sqlite3.connect(db)
    columns_before = {r[1] for r in check.execute("PRAGMA table_info(attempts)")}
    check.close()
    assert "isolation" not in columns_before

    reopened = Store.open(db)
    try:
        assert reopened.schema_version() == 11
        columns = {
            r["name"] for r in reopened.conn.execute("PRAGMA table_info(attempts)")
        }
        assert "isolation" in columns
        assert {"session_ref", "run_id", "usage_missing_reason"} <= columns
        legacy = reopened.attempts_all()[0]
        assert legacy.isolation is None  # pre-v5 rows claim nothing
        assert legacy.session_ref is None
        assert legacy.run_id is None  # no revision and no assignment to copy
        assert legacy.usage_missing_reason is None
        assert legacy.started_at is None
    finally:
        reopened.close()
    assert not list(tmp_path.glob("v4.db.migrate-*")), "stale migration backups"


def test_v5_to_v6_migration_adds_tasks_preread(tmp_path):
    """A v5 database (tasks without preread) upgrades on reopen: migration v6
    adds preread_json, existing tasks read as an empty list (docs/pbv-mapping.md §4.1)."""
    db = tmp_path / "v5.db"
    store = Store.open(db)  # code is v7; build a v5 db by hand
    store.conn.execute("ALTER TABLE tasks DROP COLUMN preread_json")
    _strip_v7_columns(store.conn)
    store.conn.execute("PRAGMA foreign_keys=OFF")
    store.conn.execute(
        "INSERT INTO tasks(revision_id, task_id, objective, scope_json, acceptance_json,"
        " verification_json, routing_json, status, created_at, updated_at)"
        " VALUES(1, 'T001', 'legacy', '{}', '[]', '[]', '{}', 'passed', '0', '0')"
    )
    store.conn.execute("PRAGMA foreign_keys=ON")
    store.conn.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
    store.close()
    check = sqlite3.connect(db)
    columns_before = {r[1] for r in check.execute("PRAGMA table_info(tasks)")}
    check.close()
    assert "preread_json" not in columns_before

    reopened = Store.open(db)
    try:
        assert reopened.schema_version() == 11
        columns = {r["name"] for r in reopened.conn.execute("PRAGMA table_info(tasks)")}
        assert "preread_json" in columns
        attempt_columns = {
            r["name"] for r in reopened.conn.execute("PRAGMA table_info(attempts)")
        }
        assert {"session_ref", "run_id", "usage_missing_reason"} <= attempt_columns
        run_columns = {r["name"] for r in reopened.conn.execute("PRAGMA table_info(runs)")}
        assert {"started_at", "completed_at"} <= run_columns
        legacy = reopened.tasks_all(1)[0]
        assert legacy.preread == []  # pre-v6 tasks claim no read-first list
    finally:
        reopened.close()
    assert not list(tmp_path.glob("v5.db.migrate-*")), "stale migration backups"


def test_attempt_isolation_roundtrip(project, goal):
    """attempt_create(..., isolation=...) persists on the attempt row and
    round-trips through the reader; the default stays None (backward
    compatible — dispatch call sites wire it in a later slice)."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ]))
    revision = project.store.revision_active(
        project.store.run_for_goal(goal.id).id)
    attempt = project.store.attempt_create(
        revision_row_id=revision.id,
        role="planner",
        profile="cli-fake",
        driver="cli",
        harness="codex",
        model_id="fake-codex-model",
        requested_effort="high",
        task_id="T001",
        isolation="read_only",
    )
    assert attempt.isolation == "read_only"
    latest = project.store.attempt_latest_for_task(revision.id, "T001")
    assert latest is not None
    assert latest.isolation == "read_only"

    plain = project.store.attempt_create(
        revision_row_id=revision.id,
        role="worker",
        profile="host-worker",
        driver="host",
        harness="zcode",
        model_id="m-worker",
        requested_effort="medium",
        task_id="T001",
    )
    assert plain.isolation is None  # omitted -> no isolation claim
    # attempts_all sees both rows with their isolation intact
    stored = {a.id: a.isolation for a in project.store.attempts_all()}
    assert stored == {attempt.id: "read_only", plain.id: None}
    # revision_id is enough to attribute the attempt; no explicit run_id needed
    run = project.store.run_for_goal(goal.id)
    assert attempt.run_id == run.id
    assert plain.run_id == run.id


def test_v6_upgrade_adds_observability_without_inventing_legacy_facts(tmp_path):
    """A v6 database gains session_ref, run attribution, usage-missing reasons,
    run lifecycle columns, and host_report. Existing rows survive. Timestamps
    and missing associations stay NULL — updated_at is not a completion time.
    run_id is copied only from a revision that already exists."""
    db = tmp_path / "v6.db"
    store = Store.open(db)
    goal, run = store.goal_create("legacy objective", ["keep this"], [], "")
    revision = store.revision_create(run.id, "light", "legacy-planner", {"tasks": []})
    linked = store.attempt_create(
        revision_row_id=revision.id,
        role="worker",
        profile="legacy",
        driver="cli",
        harness="shell",
        model_id="m",
        requested_effort="high",
        task_id=None,
    )
    loose = store.attempt_create(
        revision_row_id=None,
        role="planner",
        profile="legacy",
        driver="host",
        harness="zcode",
        model_id="m",
        requested_effort="low",
    )
    store.usage_add(
        linked.id, "legacy", run.id, None, 3, 1, None, "native_cli", "unknown",
    )
    store.conn.execute(
        "UPDATE runs SET status = 'done', updated_at = '2099-01-01T00:00:00+00:00'"
        " WHERE id = ?",
        (run.id,),
    )
    _narrow_usage_check(store.conn)
    _strip_v7_columns(store.conn)
    store.conn.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
    store.close()

    reopened = Store.open(db)
    try:
        assert reopened.schema_version() == 11
        assert reopened.goal_get(goal.id).objective == "legacy objective"
        upgraded = reopened.run_get(run.id)
        assert upgraded.status == "done"
        assert upgraded.updated_at == "2099-01-01T00:00:00+00:00"
        assert upgraded.started_at is None
        assert upgraded.completed_at is None
        rows = {a.id: a for a in reopened.attempts_all()}
        assert rows[linked.id].run_id == run.id
        assert rows[linked.id].session_ref is None
        assert rows[linked.id].usage_missing_reason is None
        assert rows[linked.id].profile == "legacy"
        assert rows[loose.id].run_id is None
        assert rows[loose.id].session_ref is None
        assert rows[loose.id].usage_missing_reason is None
        usage = reopened.usage_rows()
        assert len(usage) == 1
        assert usage[0]["source"] == "native_cli"
        assert usage[0]["accuracy"] == "unknown"
        assert usage[0]["input_tokens"] == 3
        assert usage[0]["attempt_id"] == linked.id
        reopened.usage_add(
            linked.id, "legacy", run.id, None, None, None, None, "host_report", "unknown",
        )
        sources = [row["source"] for row in reopened.usage_rows()]
        assert sources == ["native_cli", "host_report"]
        with pytest.raises(sqlite3.IntegrityError):
            reopened.conn.execute(
                "INSERT INTO usage_observations(attempt_id, profile, run_id, source,"
                " accuracy, created_at) VALUES(999999, 'x', 'R001', 'host_report',"
                " 'unknown', '0')"
            )
    finally:
        reopened.close()
    assert not list(tmp_path.glob("v6.db.migrate-*")), "stale migration backups"


def test_migration_failure_preserves_original_database(tmp_path, monkeypatch):
    db = tmp_path / "keep.db"
    store = Store.open(db)
    goal, _run = store.goal_create("do not lose", ["a"], [], "")
    store.conn.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
    store.close()

    def boom(_conn):
        raise RuntimeError("v7 failed")

    monkeypatch.setitem(state_mod.MIGRATIONS, 7, boom)
    with pytest.raises(RuntimeError, match="v7 failed"):
        Store.open(db)

    conn = sqlite3.connect(db)
    try:
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        objective = conn.execute(
            "SELECT objective FROM goals WHERE id = ?", (goal.id,)
        ).fetchone()[0]
        assert version == "6"
        assert objective == "do not lose"
    finally:
        conn.close()
    assert not list(tmp_path.glob("keep.db.migrate-*"))


def test_migration_keeps_wal_committed_rows(tmp_path):
    """A commit that still lives in the WAL must survive replacement, and the
    temporary migration files must be gone afterwards."""
    db = tmp_path / "wal.db"
    store = Store.open(db)
    store.conn.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
    store.close()

    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(
        "INSERT INTO goals(id, objective, constraints_json, acceptance_json, context,"
        " status, created_at, updated_at) VALUES('G777', 'wal kept', '[]', '[]', '',"
        " 'done', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    writer.commit()
    # Leave the writer open so the commit is not folded by connection close.
    try:
        reopened = Store.open(db)
        try:
            assert reopened.schema_version() == 11
            assert reopened.goal_get("G777").objective == "wal kept"
        finally:
            reopened.close()
    finally:
        writer.close()
    assert not list(tmp_path.glob("wal.db.migrate-*"))


# ---------------------------------------------------------------------------
# G004 replan storage (schema v9): correspondence, reports, provenance


def _worker_attempt(store: Store, revision_row_id: int, task_id: str):
    return store.attempt_create(
        revision_row_id=revision_row_id,
        role="worker",
        profile="host-worker",
        driver="host",
        harness="zcode",
        model_id="m-worker",
        requested_effort="high",
        task_id=task_id,
    )


def test_replan_mapping_persists_renumbered_correspondence(project, goal):
    """A declared mapping round-trips as queryable rows keyed by
    (revision, task_id) on both ends, so renumbered tasks stay traceable in
    both directions; a second save for one revision is a conflict."""
    store = project.store
    run = store.run_for_goal(goal.id)
    rev1 = _mk_revision(store, run.id, ["T001", "T002", "T003"])
    # Real terminal history on the old task: attempts, evidence, and a
    # verification row must all survive the replan storage untouched.
    attempt = _worker_attempt(store, rev1, "T001")
    store.attempt_update(attempt.id, ended_at="2026-10-05T00:00:00+00:00", result="completed")
    store.evidence_add(attempt.id, "completion", ".orx/runs/R001/evidence/T001-1.json")
    store.verification_add(rev1, "T001", "command", "true", True, attempt_id=attempt.id)
    store.task_update_status(rev1, "T001", TaskStatus.PASSED)
    store.revision_mark_superseded(rev1)

    rev2 = _mk_revision(store, run.id, ["T101", "T102", "T103"])
    mapping = plan.ReplanMapping(
        prior_revision=1,
        tasks=[
            plan.ReplanTaskMapping(
                task="T101", classification="confirm",
                sources=[plan.ReplanSource(revision=1, task_id="T001")],
                confirm_verification=["true"],
            ),
            plan.ReplanTaskMapping(
                task="T102", classification="redo",
                sources=[plan.ReplanSource(revision=1, task_id="T002")],
                redo_reason="the interface changed under us",
            ),
            plan.ReplanTaskMapping(task="T103", classification="new"),
        ],
        superseded=[
            plan.ReplanSuperseded(revision=1, task_id="T001", disposition="confirmed",
                                  successors=["T101"]),
            plan.ReplanSuperseded(revision=1, task_id="T002", disposition="redone",
                                  successors=["T102"]),
            plan.ReplanSuperseded(revision=1, task_id="T003", disposition="dropped",
                                  successors=[], note="out of scope now"),
        ],
    )
    saved = store.replan_mapping_save(rev2, mapping)
    assert saved.run_id == run.id
    assert saved.revision_id == rev2
    assert saved.prior_revision == 1
    by_task = {t.task_id: t for t in saved.tasks}
    assert set(by_task) == {"T101", "T102", "T103"}
    assert by_task["T101"].classification == "confirm"
    assert by_task["T101"].confirm_verification == ["true"]
    assert [(s.source_revision, s.source_task_id, s.part) for s in by_task["T101"].sources] == [
        (1, "T001", False)
    ]
    assert by_task["T102"].redo_reason == "the interface changed under us"
    assert by_task["T102"].classification == "redo"
    assert by_task["T103"].sources == []  # new work claims no prior task
    by_source = {s.source_task_id: s for s in saved.superseded}
    assert by_source["T001"].disposition == "confirmed"
    assert by_source["T001"].successors == ["T101"]
    assert by_source["T003"].disposition == "dropped"
    assert by_source["T003"].note == "out of scope now"

    # Trace forward across the renumbering: T001's successor is T101, found
    # by (run, revision, task_id) — never by task number.
    successors = store.replan_successors_for_source(run.id, 1, "T001")
    assert [(s.revision, s.task_id, s.classification) for s in successors] == [
        (2, "T101", "confirm")
    ]
    # Trace backward: the new task's sources read back the same way.
    sources = store.replan_sources_for_task(rev2, "T102")
    assert [(s.source_revision, s.source_task_id, s.part) for s in sources] == [
        (1, "T002", False)
    ]
    # A source that does not resolve inside the run is refused, not guessed
    # (on a fresh revision, so the append-only conflict cannot mask it).
    rev3 = _mk_revision(store, run.id, ["T201"])
    with pytest.raises(NotFoundError, match="T999"):
        store.replan_mapping_save(rev3, plan.ReplanMapping(
            prior_revision=1,
            tasks=[plan.ReplanTaskMapping(
                task="T201", classification="confirm",
                sources=[plan.ReplanSource(revision=1, task_id="T999")],
            )],
        ))
    assert store.replan_mapping_for(rev3) is None  # the failed save left nothing
    # Declared mappings are append-only per revision.
    with pytest.raises(ConflictError):
        store.replan_mapping_save(rev2, mapping)

    # Reopen: the mapping and the untouched legacy rows stay queryable.
    db_path = store.path
    project.close()
    reopened = Store.open(db_path)
    try:
        assert reopened.schema_version() == 11
        again = reopened.replan_mapping_for(rev2)
        assert again is not None
        assert {t.task_id: t.classification for t in again.tasks} == {
            "T101": "confirm", "T102": "redo", "T103": "new",
        }
        # Revision 1 has no mapping: unknown, never backfilled.
        assert reopened.replan_mapping_for(rev1) is None
        # The old task's attempts, evidence, and verification rows are intact.
        assert [a.id for a in reopened.attempts_all()] == [attempt.id]
        assert reopened.evidence_for_task(rev1, "T001") == [
            ("completion", ".orx/runs/R001/evidence/T001-1.json")
        ]
        verifications = reopened.verifications_for(rev1, "T001")
        assert len(verifications) == 1 and verifications[0].passed
        assert verifications[0].attempt_id == attempt.id
        assert reopened.task_get(rev1, "T001").status == "passed"  # recorded fact, not inherited
    finally:
        reopened.close()


def test_replan_trace_chain_spans_rounds_split_and_merge(project, goal):
    """Multi-round tracing: a split (one old task into two new), then a merge
    (two old tasks into one), each round renumbering — the chain stays
    queryable across all of it."""
    store = project.store
    run = store.run_for_goal(goal.id)
    rev1 = _mk_revision(store, run.id, ["T001"])
    store.revision_mark_superseded(rev1)
    rev2 = _mk_revision(store, run.id, ["T101", "T102"])
    store.replan_mapping_save(rev2, plan.ReplanMapping(
        prior_revision=1,
        tasks=[
            plan.ReplanTaskMapping(
                task="T101", classification="redo",
                sources=[plan.ReplanSource(revision=1, task_id="T001", part=True)],
                redo_reason="half the approach was wrong",
            ),
            plan.ReplanTaskMapping(
                task="T102", classification="continue",
                sources=[plan.ReplanSource(revision=1, task_id="T001", part=True)],
            ),
        ],
        superseded=[
            plan.ReplanSuperseded(revision=1, task_id="T001", disposition="split",
                                  successors=["T101", "T102"]),
        ],
    ))
    store.revision_mark_superseded(rev2)
    rev3 = _mk_revision(store, run.id, ["T201"])
    store.replan_mapping_save(rev3, plan.ReplanMapping(
        prior_revision=2,
        tasks=[
            plan.ReplanTaskMapping(
                task="T201", classification="confirm",
                sources=[plan.ReplanSource(revision=2, task_id="T101"),
                         plan.ReplanSource(revision=2, task_id="T102")],
                confirm_verification=["true"],
            ),
        ],
        superseded=[
            plan.ReplanSuperseded(revision=2, task_id="T101", disposition="merged",
                                  successors=["T201"]),
            plan.ReplanSuperseded(revision=2, task_id="T102", disposition="merged",
                                  successors=["T201"]),
        ],
    ))

    assert [m.revision_id for m in store.replan_mappings_for_run(run.id)] == [rev2, rev3]
    chain = store.replan_trace_chain(run.id, 1, "T001")
    steps = [
        (s.from_revision, s.from_task_id, s.to_revision, s.to_task_id, s.classification, s.part)
        for s in chain
    ]
    assert steps == [
        (1, "T001", 2, "T101", "redo", True),      # split, part 1
        (1, "T001", 2, "T102", "continue", True),  # split, part 2
        (2, "T101", 3, "T201", "confirm", False),  # merge, part 1
        (2, "T102", 3, "T201", "confirm", False),  # merge, part 2
    ]
    # A mid-chain start traces only the remaining rounds.
    tail = store.replan_trace_chain(run.id, 2, "T102")
    assert [(s.from_revision, s.to_revision, s.to_task_id) for s in tail] == [
        (2, 3, "T201")
    ]
    # A task with no successors ends the chain.
    assert store.replan_trace_chain(run.id, 3, "T201") == []

    # Still queryable after a reopen.
    db_path = store.path
    project.close()
    reopened = Store.open(db_path)
    try:
        assert len(reopened.replan_trace_chain(run.id, 1, "T001")) == 4
        assert reopened.replan_successors_for_source(run.id, 1, "T001")[0].task_id == "T101"
    finally:
        reopened.close()


def test_replan_artifact_sources_bind_full_identity(project, goal):
    """Artifact provenance resolves by full identity: the same task number in
    a different revision never matches, and two attempts of the same source
    task stay distinct rows."""
    store = project.store
    run = store.run_for_goal(goal.id)
    rev1 = _mk_revision(store, run.id, ["T001", "T002"])
    store.revision_mark_superseded(rev1)
    # Same task number on a new revision: different work by definition.
    rev2 = _mk_revision(store, run.id, ["T001"])
    store.replan_mapping_save(rev2, plan.ReplanMapping(
        prior_revision=1,
        tasks=[plan.ReplanTaskMapping(
            task="T001", classification="redo",
            sources=[plan.ReplanSource(revision=1, task_id="T001")],
            redo_reason="the old result did not hold",
        )],
        superseded=[plan.ReplanSuperseded(revision=1, task_id="T001", disposition="redone",
                                          successors=["T001"])],
    ))
    first_round = _worker_attempt(store, rev1, "T001")
    retry_round = _worker_attempt(store, rev1, "T001")  # second attempt, same task
    store.evidence_add(retry_round.id, "completion", ".orx/runs/R001/evidence/T001-retry.json")
    new_round = _worker_attempt(store, rev2, "T001")  # same NUMBER, revision 2

    # An attempt of the wrong revision (same task number) must be refused.
    with pytest.raises(ORXError, match="belongs to"):
        store.replan_artifact_source_add(
            run.id, rev2, "T001", 1, "T001", "not-this-one.json", attempt_id=new_round.id,
        )
    # An artifact edge must be a declared correspondence, not a bare number.
    with pytest.raises(ORXError, match="no declared correspondence edge"):
        store.replan_artifact_source_add(
            run.id, rev2, "T001", 1, "T002", "undeclared-edge.json",
        )

    row_a = store.replan_artifact_source_add(
        run.id, rev2, "T001", 1, "T001",
        ".orx/runs/R001/check/T001/0001-a4-command.log", attempt_id=first_round.id,
    )
    row_b = store.replan_artifact_source_add(
        run.id, rev2, "T001", 1, "T001",
        ".orx/runs/R001/check/T001/0002-a5-command.log", attempt_id=retry_round.id,
    )
    # Same source task, different attempts: distinct provenance rows.
    assert row_a.attempt_id == first_round.id
    assert row_b.attempt_id == retry_round.id
    assert row_a.id != row_b.id

    # Evidence carries its own recorded attempt binding.
    evidence_id = store.conn.execute(
        "SELECT id FROM evidence WHERE attempt_id = ?", (retry_round.id,)
    ).fetchone()["id"]
    row_c = store.replan_artifact_source_add(
        run.id, rev2, "T001", 1, "T001",
        ".orx/runs/R001/evidence/T001-retry.json", evidence_id=evidence_id,
    )
    assert row_c.evidence_id == evidence_id
    assert row_c.attempt_id == retry_round.id  # the evidence row's own binding
    # Evidence and attempt that disagree are refused.
    with pytest.raises(ORXError, match="belongs to attempt"):
        store.replan_artifact_source_add(
            run.id, rev2, "T001", 1, "T001", "mismatch.json",
            attempt_id=first_round.id, evidence_id=evidence_id,
        )
    # An identical reference is idempotent.
    again = store.replan_artifact_source_add(
        run.id, rev2, "T001", 1, "T001",
        ".orx/runs/R001/check/T001/0001-a4-command.log", attempt_id=first_round.id,
    )
    assert again.id == row_a.id

    # Reads: by new task, and back by source task — full identity everywhere.
    by_task = store.replan_artifact_sources_for_task(rev2, "T001")
    assert [r.attempt_id for r in by_task] == [
        first_round.id, retry_round.id, retry_round.id,
    ]
    by_source = store.replan_artifact_sources_for_source(run.id, 1, "T001")
    assert [(r.artifact, r.attempt_id, r.evidence_id) for r in by_source] == [
        (".orx/runs/R001/check/T001/0001-a4-command.log", first_round.id, None),
        (".orx/runs/R001/check/T001/0002-a5-command.log", retry_round.id, None),
        (".orx/runs/R001/evidence/T001-retry.json", retry_round.id, evidence_id),
    ]
    # The revision-2 task of the same number has no provenance of its own.
    assert store.replan_artifact_sources_for_task(rev2, "T001") != (
        store.replan_artifact_sources_for_task(rev1, "T001")
    )
    assert store.replan_artifact_sources_for_task(rev1, "T001") == []

    # Reopen keeps the provenance queryable.
    db_path = store.path
    project.close()
    reopened = Store.open(db_path)
    try:
        rows = reopened.replan_artifact_sources_for_task(rev2, "T001")
        assert [r.attempt_id for r in rows] == [
            first_round.id, retry_round.id, retry_round.id,
        ]
    finally:
        reopened.close()


def test_replan_report_pending_then_bound(project, goal):
    """A preflight report is stored verbatim with its revision unbound
    (unknown) until the revision lands and the caller binds it."""
    store = project.store
    run = store.run_for_goal(goal.id)
    _mk_revision(store, run.id, ["T001"])

    report = store.replan_report_add(run.id, 1, {
        "correspondence": [{"from": "1:T001", "to": "2:T101", "classification": "confirm"}],
        "classification_counts": {"new": 0, "confirm": 1, "redo": 0, "continue": 0},
    })
    assert report.revision_id is None  # unknown until the revision lands
    assert report.payload["classification_counts"]["confirm"] == 1

    rev2 = _mk_revision(store, run.id, ["T101"])
    store.replan_report_bind(report.id, rev2)
    got = store.replan_reports_for_run(run.id)
    assert [r.id for r in got] == [report.id]
    assert got[0].revision_id == rev2
    assert got[0].prior_revision == 1
    # Binding is one-shot; a different revision is a conflict.
    rev3 = _mk_revision(store, run.id, ["T201"])
    with pytest.raises(ConflictError):
        store.replan_report_bind(report.id, rev3)


def test_v8_to_v9_migration_additive_without_backfill(tmp_path):
    """A v8 database gains the replan tables on reopen; every legacy task,
    attempt, evidence, and verification row survives; nothing is backfilled
    into the new tables — unknown stays unknown. (The file now also crosses
    the additive v10 migration on the same reopen: attempt_progress arrives
    empty too.)"""
    db = tmp_path / "v8.db"
    store = Store.open(db)  # code is v10; build a v8 db by hand
    goal, run = store.goal_create("legacy objective", ["keep this"], [], "")
    rev1 = store.revision_create(run.id, "standard", "legacy-planner", {"tasks": ["T001"]}).id
    store.task_insert(rev1, "T001", "legacy task", {}, ["keep this"], ["true"], {}, "passed")
    attempt = store.attempt_create(
        revision_row_id=rev1, role="worker", profile="legacy", driver="host",
        harness="zcode", model_id="m", requested_effort="high", task_id="T001",
    )
    store.evidence_add(attempt.id, "completion", ".orx/runs/R001/evidence/legacy.json")
    store.verification_add(rev1, "T001", "command", "true", True, attempt_id=attempt.id)
    _drop_v9_tables(store.conn)
    _drop_v10_progress(store.conn)
    store.conn.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")
    store.close()
    check = sqlite3.connect(db)
    tables_before = {
        r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    check.close()
    assert not set(REPLAN_V9_TABLES) <= tables_before
    assert "attempt_progress" not in tables_before

    reopened = Store.open(db)
    try:
        assert reopened.schema_version() == 11
        tables = {
            r["name"]
            for r in reopened.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert set(REPLAN_V9_TABLES) <= tables
        assert "attempt_progress" in tables
        # Legacy rows are intact, byte for byte in meaning.
        assert reopened.goal_get(goal.id).objective == "legacy objective"
        task = reopened.tasks_all(rev1)[0]
        assert (task.task_id, task.status, task.acceptance) == ("T001", "passed", ["keep this"])
        stored_attempt = reopened.attempts_all()[0]
        assert stored_attempt.task_id == "T001" and stored_attempt.id == attempt.id
        assert reopened.evidence_for_task(rev1, "T001") == [
            ("completion", ".orx/runs/R001/evidence/legacy.json")
        ]
        verifications = reopened.verifications_for(rev1, "T001")
        assert len(verifications) == 1 and verifications[0].passed
        assert verifications[0].attempt_id == attempt.id
        # The new tables are empty: no guessed correspondence, no invented
        # report, no provenance, and no mapping derived from the passed task.
        assert reopened.replan_mapping_for(rev1) is None
        assert reopened.replan_mappings_for_run(run.id) == []
        assert reopened.replan_reports_for_run(run.id) == []
        assert reopened.replan_artifact_sources_for_task(rev1, "T001") == []
        assert reopened.replan_successors_for_source(run.id, 1, "T001") == []
        assert reopened.replan_trace_chain(run.id, 1, "T001") == []
        # No progress report was backfilled for the legacy attempt: unknown
        # stays unknown — no claim time, session, or usage is turned into one.
        assert reopened.attempt_progress_latest(attempt.id) is None
        assert reopened.attempt_progress_all(attempt.id) == []
        # The upgraded table is writable: the store appends for real attempts.
        row_id = reopened.attempt_progress_add(
            attempt.id, "checking", "post-upgrade report",
            "2026-10-06T02:00:00.000000+00:00",
        )
        latest = reopened.attempt_progress_latest(attempt.id)
        assert latest is not None and latest.id == row_id
        assert (latest.sequence, latest.phase, latest.message) == (
            1, "checking", "post-upgrade report",
        )
    finally:
        reopened.close()
    assert not list(tmp_path.glob("v8.db.migrate-*")), "stale migration backups"


def test_v9_migration_failure_preserves_original_database(tmp_path, monkeypatch):
    """A failing v9 migration leaves the v8 file untouched and usable; a
    later clean open migrates it normally."""
    db = tmp_path / "keep9.db"
    store = Store.open(db)
    goal, _run = store.goal_create("do not lose", ["a"], [], "")
    _drop_v9_tables(store.conn)
    _drop_v10_progress(store.conn)
    store.conn.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")
    store.close()

    def boom(_conn):
        raise RuntimeError("v9 failed")

    monkeypatch.setitem(state_mod.MIGRATIONS, 9, boom)
    with pytest.raises(RuntimeError, match="v9 failed"):
        Store.open(db)

    conn = sqlite3.connect(db)
    try:
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        objective = conn.execute(
            "SELECT objective FROM goals WHERE id = ?", (goal.id,)
        ).fetchone()[0]
        assert version == "8"
        assert objective == "do not lose"
    finally:
        conn.close()
    assert not list(tmp_path.glob("keep9.db.migrate-*"))

    # The v8 original is still usable: reopening without the sabotage
    # migrates it and keeps the row.
    monkeypatch.undo()
    reopened = Store.open(db)
    try:
        assert reopened.schema_version() == 11
        assert reopened.goal_get(goal.id).objective == "do not lose"
    finally:
        reopened.close()
    assert not list(tmp_path.glob("keep9.db.migrate-*"))


def test_v8_to_v9_migration_keeps_wal_committed_rows(tmp_path):
    """A commit that still lives in the WAL must survive the v8 -> v9
    replacement, and the temporary migration files must be gone."""
    db = tmp_path / "wal9.db"
    store = Store.open(db)
    _drop_v9_tables(store.conn)
    _drop_v10_progress(store.conn)
    store.conn.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")
    store.close()

    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(
        "INSERT INTO goals(id, objective, constraints_json, acceptance_json, context,"
        " status, created_at, updated_at) VALUES('G888', 'wal kept 9', '[]', '[]', '',"
        " 'done', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    writer.commit()
    try:
        reopened = Store.open(db)
        try:
            assert reopened.schema_version() == 11
            assert reopened.goal_get("G888").objective == "wal kept 9"
        finally:
            reopened.close()
    finally:
        writer.close()
    assert not list(tmp_path.glob("wal9.db.migrate-*"))


# ---------------------------------------------------------------------------
# G006 host worker progress storage (schema v10): append, read, isolation


def _drop_v10_progress(conn) -> None:
    """Drop the v10 progress table so a reopen has to recreate it, as a real
    v9 file would look."""
    conn.execute("DROP TABLE IF EXISTS attempt_progress")


def _drop_v11_nonce(conn) -> None:
    """Drop the v11 nonce column so a reopen has to re-add it, as a real
    v10 file would look."""
    conn.execute("ALTER TABLE attempts DROP COLUMN nonce")


def test_attempt_progress_append_latest_all_consistent_across_reopen(project, goal):
    """add -> reopen: reports are append-only rows bound to the real attempt;
    latest/all read the same before and after the database is reopened.
    Two reports may share one received_at — sequence is the authority."""
    store = project.store
    run = store.run_for_goal(goal.id)
    rev = _mk_revision(store, run.id, ["T001"])
    attempt = _worker_attempt(store, rev, "T001")

    same_instant = "2026-10-06T02:00:00.000000+00:00"
    first = store.attempt_progress_add(
        attempt.id, "exploring", "reading the contract", same_instant
    )
    second = store.attempt_progress_add(attempt.id, "implementing", None, same_instant)
    third = store.attempt_progress_add(
        attempt.id, "checking", "round 1/3 red, fixing",
        "2026-10-06T02:05:00.000000+00:00",
    )
    assert first < second < third  # row ids follow the append order

    all_rows = store.attempt_progress_all(attempt.id)
    assert [(r.sequence, r.phase, r.message, r.received_at) for r in all_rows] == [
        (1, "exploring", "reading the contract", same_instant),
        (2, "implementing", None, same_instant),  # omitted message stays NULL
        (3, "checking", "round 1/3 red, fixing", "2026-10-06T02:05:00.000000+00:00"),
    ]
    latest = store.attempt_progress_latest(attempt.id)
    assert latest is not None
    assert (latest.id, latest.sequence, latest.phase) == (third, 3, "checking")

    # Reopen: latest and history read identically from the persisted file.
    db_path = store.path
    project.close()
    reopened = Store.open(db_path)
    try:
        assert reopened.schema_version() == 11
        again_all = reopened.attempt_progress_all(attempt.id)
        assert [(r.id, r.sequence, r.phase, r.message, r.received_at) for r in again_all] == [
            (r.id, r.sequence, r.phase, r.message, r.received_at) for r in all_rows
        ]
        again_latest = reopened.attempt_progress_latest(attempt.id)
        assert again_latest is not None
        assert again_latest.id == latest.id and again_latest.received_at == latest.received_at
    finally:
        reopened.close()


def test_attempt_progress_sequence_strictly_increasing_across_connections(tmp_path):
    """sequence is allocated inside the write transaction (max + 1), so
    interleaved writers on separate connections still produce a strictly
    increasing, duplicate-free sequence for one attempt."""
    db = tmp_path / "concurrent.db"
    store = Store.open(db)
    try:
        goal, run = store.goal_create("concurrent", ["a"], [], "")
        rev = store.revision_create(run.id, "standard", "p", {"tasks": ["T001"]})
        store.task_insert(rev.id, "T001", "o", {}, [], [], {}, "running")
        attempt = store.attempt_create(
            revision_row_id=rev.id, role="worker", profile="host-worker",
            driver="host", harness="zcode", model_id="m", requested_effort="high",
            task_id="T001",
        )
        path = db
        original_id = attempt.id
    finally:
        store.close()

    other = Store.open(path)
    try:
        base = Store.open(path)
        try:
            ids = []
            # A and B alternate on their own connections; each allocation
            # must see the other's committed rows.
            stamps = ["2026-10-06T03:00:0%d.000000+00:00" % i for i in range(4)]
            ids.append(base.attempt_progress_add(original_id, "a1", None, stamps[0]))
            ids.append(other.attempt_progress_add(original_id, "a2", None, stamps[1]))
            ids.append(base.attempt_progress_add(original_id, "a3", None, stamps[2]))
            ids.append(other.attempt_progress_add(original_id, "a4", None, stamps[3]))
            rows = base.attempt_progress_all(original_id)
            sequences = [r.sequence for r in rows]
            assert sequences == [1, 2, 3, 4]
            assert len(set(sequences)) == 4  # UNIQUE backstop never tripped
            assert ids == sorted(ids)
            assert base.attempt_progress_latest(original_id).phase == "a4"
        finally:
            base.close()
    finally:
        other.close()


def test_attempt_progress_isolated_by_attempt_identity(project, goal):
    """Reports bind to the attempt row, never the task number: a retry with
    no reports reads unknown, and the same task number on another revision
    has its own isolated history."""
    store = project.store
    run = store.run_for_goal(goal.id)
    rev1 = _mk_revision(store, run.id, ["T001"])
    store.revision_mark_superseded(rev1)
    rev2 = _mk_revision(store, run.id, ["T001"])  # same NUMBER, new revision

    first_round = _worker_attempt(store, rev1, "T001")
    retry_round = _worker_attempt(store, rev1, "T001")  # the retry's attempt
    new_round = _worker_attempt(store, rev2, "T001")

    store.attempt_progress_add(
        first_round.id, "implementing", "first try", "2026-10-06T04:00:00.000000+00:00"
    )
    store.attempt_progress_add(
        first_round.id, "blocked", "gate red", "2026-10-06T04:10:00.000000+00:00"
    )
    store.attempt_progress_add(
        new_round.id, "exploring", None, "2026-10-06T05:00:00.000000+00:00"
    )

    # The retry attempt has no reports: unknown, never the prior round's.
    assert store.attempt_progress_latest(retry_round.id) is None
    assert store.attempt_progress_all(retry_round.id) == []
    # The first round keeps its own append history.
    first_latest = store.attempt_progress_latest(first_round.id)
    assert first_latest is not None
    assert (first_latest.sequence, first_latest.phase) == (2, "blocked")
    assert len(store.attempt_progress_all(first_round.id)) == 2
    # Same task number on revision 2: its own sequence space, nothing leaked
    # from revision 1's attempts.
    new_latest = store.attempt_progress_latest(new_round.id)
    assert new_latest is not None
    assert (new_latest.sequence, new_latest.phase, new_latest.message) == (
        1, "exploring", None
    )
    assert len(store.attempt_progress_all(new_round.id)) == 1
    # The report row is bound to a real attempt row: an unknown attempt id is
    # refused by the foreign key, not silently recorded.
    with pytest.raises(sqlite3.IntegrityError):
        store.attempt_progress_add(
            999999, "exploring", None, "2026-10-06T06:00:00.000000+00:00"
        )


def test_v9_to_v10_migration_additive_without_backfill(tmp_path):
    """A v9 database gains attempt_progress on reopen; every legacy row
    survives; no report is backfilled — an attempt that never reported reads
    unknown, not a claim time or usage derived stand-in."""
    db = tmp_path / "v9.db"
    store = Store.open(db)  # code is v10; build a v9 db by hand
    goal, run = store.goal_create("legacy objective", ["keep this"], [], "")
    rev1 = store.revision_create(run.id, "standard", "legacy-planner", {"tasks": ["T001"]}).id
    store.task_insert(rev1, "T001", "legacy task", {}, ["keep this"], ["true"], {}, "running")
    attempt = store.attempt_create(
        revision_row_id=rev1, role="worker", profile="legacy", driver="host",
        harness="zcode", model_id="m", requested_effort="high", task_id="T001",
    )
    _drop_v10_progress(store.conn)
    store.conn.execute("UPDATE meta SET value = '9' WHERE key = 'schema_version'")
    store.close()
    check = sqlite3.connect(db)
    tables_before = {
        r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    check.close()
    assert "attempt_progress" not in tables_before

    reopened = Store.open(db)
    try:
        assert reopened.schema_version() == 11
        tables = {
            r["name"]
            for r in reopened.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "attempt_progress" in tables
        assert set(REPLAN_V9_TABLES) <= tables  # v9 storage untouched
        # Legacy rows survive byte for byte in meaning.
        assert reopened.goal_get(goal.id).objective == "legacy objective"
        stored = reopened.attempt_get(attempt.id)
        assert (stored.task_id, stored.driver, stored.session_ref) == ("T001", "host", None)
        # The new table is empty and nothing was derived: unknown stays unknown.
        assert reopened.attempt_progress_latest(attempt.id) is None
        assert reopened.attempt_progress_all(attempt.id) == []
        count = reopened.conn.execute(
            "SELECT COUNT(*) AS c FROM attempt_progress"
        ).fetchone()["c"]
        assert count == 0
    finally:
        reopened.close()
    assert not list(tmp_path.glob("v9.db.migrate-*")), "stale migration backups"


def test_v10_migration_failure_preserves_original_database(tmp_path, monkeypatch):
    """A failing v10 migration leaves the v9 file untouched and usable; a
    later clean open migrates it normally."""
    db = tmp_path / "keep10.db"
    store = Store.open(db)
    goal, _run = store.goal_create("do not lose", ["a"], [], "")
    _drop_v10_progress(store.conn)
    store.conn.execute("UPDATE meta SET value = '9' WHERE key = 'schema_version'")
    store.close()

    def boom(_conn):
        raise RuntimeError("v10 failed")

    monkeypatch.setitem(state_mod.MIGRATIONS, 10, boom)
    with pytest.raises(RuntimeError, match="v10 failed"):
        Store.open(db)

    conn = sqlite3.connect(db)
    try:
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        objective = conn.execute(
            "SELECT objective FROM goals WHERE id = ?", (goal.id,)
        ).fetchone()[0]
        assert version == "9"
        assert objective == "do not lose"
    finally:
        conn.close()
    assert not list(tmp_path.glob("keep10.db.migrate-*"))

    # The v9 original is still usable: reopening without the sabotage
    # migrates it and keeps the row.
    monkeypatch.undo()
    reopened = Store.open(db)
    try:
        assert reopened.schema_version() == 11
        assert reopened.goal_get(goal.id).objective == "do not lose"
    finally:
        reopened.close()
    assert not list(tmp_path.glob("keep10.db.migrate-*"))


def test_v10_migration_keeps_wal_committed_rows(tmp_path):
    """A commit that still lives in the WAL must survive the v9 -> v10
    replacement, and the temporary migration files must be gone."""
    db = tmp_path / "wal10.db"
    store = Store.open(db)
    _drop_v10_progress(store.conn)
    store.conn.execute("UPDATE meta SET value = '9' WHERE key = 'schema_version'")
    store.close()

    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(
        "INSERT INTO goals(id, objective, constraints_json, acceptance_json, context,"
        " status, created_at, updated_at) VALUES('G999', 'wal kept 10', '[]', '[]', '',"
        " 'done', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    writer.commit()
    try:
        reopened = Store.open(db)
        try:
            assert reopened.schema_version() == 11
            assert reopened.goal_get("G999").objective == "wal kept 10"
        finally:
            reopened.close()
    finally:
        writer.close()
    assert not list(tmp_path.glob("wal10.db.migrate-*"))


def test_v10_to_v11_migration_additive_without_backfill(tmp_path):
    """A v10 database gains attempts.nonce on reopen; legacy rows keep NULL
    — a nonce is a dispatch fact of the moment, never back-filled — while
    attempts created afterwards mint unique nonces."""
    db = tmp_path / "v10.db"
    store = Store.open(db)
    goal, run = store.goal_create("legacy objective", ["keep this"], [], "")
    rev1 = store.revision_create(run.id, "standard", "legacy-planner", {"tasks": ["T001"]}).id
    store.task_insert(rev1, "T001", "legacy task", {}, ["keep this"], ["true"], {}, "running")
    legacy = store.attempt_create(
        revision_row_id=rev1, role="worker", profile="legacy", driver="host",
        harness="zcode", model_id="m", requested_effort="high", task_id="T001",
    )
    assert legacy.nonce and legacy.nonce.startswith("orx-assignment:")
    _drop_v10_progress(store.conn)  # keep this a genuine v10 file
    _drop_v11_nonce(store.conn)
    store.conn.execute("UPDATE meta SET value = '10' WHERE key = 'schema_version'")
    store.close()
    conn = sqlite3.connect(db)
    cols_before = {r[1] for r in conn.execute("PRAGMA table_info(attempts)")}
    conn.close()
    assert "nonce" not in cols_before

    reopened = Store.open(db)
    try:
        assert reopened.schema_version() == 11
        cols = {r["name"] for r in reopened.conn.execute("PRAGMA table_info(attempts)")}
        assert "nonce" in cols
        # The legacy row is not rewritten: its nonce stays NULL and marker
        # fallback discovery keeps applying to it.
        assert reopened.attempt_get(legacy.id).nonce is None
        # New attempts mint unique tokens; an explicit nonce is honored
        # verbatim (dispatch passes the token it already embedded).
        fresh = reopened.attempt_create(
            revision_row_id=rev1, role="worker", profile="p", driver="host",
            harness="zcode", model_id="m", requested_effort="high",
            task_id="T001",
        )
        explicit = reopened.attempt_create(
            revision_row_id=rev1, role="worker", profile="p", driver="host",
            harness="zcode", model_id="m", requested_effort="high",
            task_id="T001", nonce="orx-assignment:00000000-0000-4000-8000-000000000000",
        )
        assert fresh.nonce != explicit.nonce != legacy.nonce
        assert explicit.nonce == "orx-assignment:00000000-0000-4000-8000-000000000000"
        import uuid
        parsed = uuid.UUID(fresh.nonce.removeprefix("orx-assignment:"))
        assert parsed.version == 4
    finally:
        reopened.close()
    assert not list(tmp_path.glob("v10.db.migrate-*")), "stale migration backups"


def test_v11_migration_failure_preserves_original_database(tmp_path, monkeypatch):
    """A failing v11 migration leaves the v10 file untouched and usable."""
    db = tmp_path / "keep11.db"
    store = Store.open(db)
    goal, _run = store.goal_create("do not lose", ["a"], [], "")
    _drop_v11_nonce(store.conn)
    store.conn.execute("UPDATE meta SET value = '10' WHERE key = 'schema_version'")
    store.close()

    def boom(_conn):
        raise RuntimeError("v11 failed")

    monkeypatch.setitem(state_mod.MIGRATIONS, 11, boom)
    with pytest.raises(RuntimeError, match="v11 failed"):
        Store.open(db)

    conn = sqlite3.connect(db)
    try:
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        objective = conn.execute(
            "SELECT objective FROM goals WHERE id = ?", (goal.id,)
        ).fetchone()[0]
        cols = {r[1] for r in conn.execute("PRAGMA table_info(attempts)")}
        assert version == "10"
        assert objective == "do not lose"
        assert "nonce" not in cols
    finally:
        conn.close()
    assert not list(tmp_path.glob("keep11.db.migrate-*"))

    monkeypatch.undo()
    reopened = Store.open(db)
    try:
        assert reopened.schema_version() == 11
        assert reopened.goal_get(goal.id).objective == "do not lose"
    finally:
        reopened.close()
    assert not list(tmp_path.glob("keep11.db.migrate-*"))


def test_attempts_close_for_tasks_stamps_disposition_not_span(tmp_path):
    """Replan supersession closes open attempts with a recorded disposition;
    started_at is never synthesized — an unclaimed attempt keeps NULL."""
    db = tmp_path / "close.db"
    store = Store.open(db)
    try:
        goal, run = store.goal_create("g", ["a"], [], "")
        rev1 = store.revision_create(run.id, "standard", "p", {"tasks": []}).id
        store.task_insert(rev1, "T001", "parked never claimed", {}, ["a"], ["true"], {},
                          "waiting_host")
        store.task_insert(rev1, "T002", "claimed then running", {}, ["a"], ["true"], {},
                          "running")
        parked = store.attempt_create(
            revision_row_id=rev1, role="worker", profile="p", driver="host",
            harness="zcode", model_id="m", requested_effort="high",
            task_id="T001", started=False,
        )
        running = store.attempt_create(
            revision_row_id=rev1, role="worker", profile="p", driver="host",
            harness="zcode", model_id="m", requested_effort="high",
            task_id="T002", started=True,
        )
        closed = store.attempts_close_for_tasks(
            rev1, ["T001", "T002"],
            ended_at="2026-10-06T00:00:00+00:00", result="superseded",
            failure_reason="revision 1 superseded",
        )
        assert closed == [parked.id, running.id]
        for row_id, started in ((parked.id, None), (running.id, running.started_at)):
            row = store.attempt_get(row_id)
            assert row.result == "superseded"
            assert row.ended_at == "2026-10-06T00:00:00+00:00"
            assert row.failure_reason == "revision 1 superseded"
            assert row.started_at == started  # unclaimed keeps NULL
        # idempotent against already-closed attempts
        again = store.attempts_close_for_tasks(
            rev1, ["T001"], ended_at="2026-10-06T01:00:00+00:00",
            result="superseded",
        )
        assert again == []
        assert store.attempt_get(parked.id).ended_at == "2026-10-06T00:00:00+00:00"
    finally:
        store.close()
