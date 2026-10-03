"""Schema initialization, reopening/recovery, and store invariants."""

from __future__ import annotations

import sqlite3

import pytest

from orx import dispatch, machine
from orx.records import ConflictError, MigrationError, TaskStatus
from orx.state import Store

from conftest import ir_for, task_spec, write_evidence


def test_schema_init_creates_tables_and_meta(tmp_path):
    db = tmp_path / "state.db"
    store = Store.open(db)
    try:
        assert store.schema_version() == 3
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
        }
        assert expected <= names
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
            requested_effort="standard",
        )


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
        requested_effort="deep",
        task_id="T001",
    )
    assert attempt.actual_effort is None and attempt.effort_source is None
    project.store.attempt_update(
        attempt.id, actual_effort="high", effort_source="requested_validated",
        ended_at="2026-10-03T00:00:00+00:00", result="completed",
    )
    stored = project.store.attempt_get(attempt.id)
    assert stored.requested_effort == "deep"
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
    store = Store.open(db)  # code is v2; build a v1 db by hand
    store.close()
    conn = sqlite3.connect(db)
    conn.executescript("""
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
        assert reopened.schema_version() == 3
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
