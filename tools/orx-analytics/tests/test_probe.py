"""Probe tests against synthetic sources: honest ok AND honest gaps."""

from __future__ import annotations

import sqlite3

from orxanalytics import probe


def _mk_zcode(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT,
            project_id TEXT, title TEXT, time_created INTEGER, time_updated INTEGER);
        CREATE TABLE turn_usage (session_id TEXT, turn_id TEXT, status TEXT,
            started_at INTEGER, completed_at INTEGER, duration_ms INTEGER,
            model_request_count INTEGER, model_retry_count INTEGER,
            tool_call_count INTEGER, tool_error_count INTEGER,
            input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
            cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER);
        CREATE TABLE model_usage (logical_request_id TEXT, attempt_index INTEGER,
            session_id TEXT, turn_id TEXT, provider_id TEXT, model_id TEXT,
            status TEXT, started_at INTEGER, completed_at INTEGER,
            input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
            cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER,
            provider_total_tokens INTEGER, computed_total_tokens INTEGER);
        CREATE TABLE tool_usage (id TEXT, session_id TEXT, turn_id TEXT,
            tool_name TEXT, status TEXT, started_at INTEGER, completed_at INTEGER,
            duration_ms INTEGER, exit_code INTEGER);
        CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,
            data TEXT, sequence INTEGER);
        """
    )
    conn.commit()
    conn.close()


def test_probe_zcode_ok(tmp_path):
    db = tmp_path / "db.sqlite"
    _mk_zcode(db)
    r = probe.probe_zcode(db)
    assert r.ok, r.gaps
    assert r.fields["counts"]["session"] == 0


def test_probe_zcode_missing_column_reported(tmp_path):
    db = tmp_path / "db.sqlite"
    _mk_zcode(db)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE turn_usage DROP COLUMN reasoning_tokens") \
        if sqlite3.sqlite_version_info >= (3, 35) else None
    conn.commit()
    conn.close()
    r = probe.probe_zcode(db)
    if sqlite3.sqlite_version_info >= (3, 35):
        assert not r.ok
        assert any("reasoning_tokens" in g for g in r.gaps)


def test_probe_zcode_absent(tmp_path):
    r = probe.probe_zcode(tmp_path / "nope.sqlite")
    assert not r.ok and r.gaps


def test_probe_orx_gate(tmp_path):
    root = tmp_path / "proj"
    (root / ".orx").mkdir(parents=True)
    r = probe.probe_orx(root)
    assert not r.ok  # no state.db -> honest gap, not a crash
    conn = sqlite3.connect(root / ".orx" / "state.db")
    conn.executescript(
        "CREATE TABLE meta (key TEXT, value TEXT);"
        "CREATE TABLE runs (id TEXT);"
        "INSERT INTO meta VALUES ('schema_version','9');"
        "INSERT INTO runs VALUES ('R001');")
    conn.commit()
    conn.close()
    r = probe.probe_orx(root)
    assert r.ok and r.fields["schema_version"] == "9"
    conn = sqlite3.connect(root / ".orx" / "state.db")
    conn.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    r = probe.probe_orx(root)
    assert not r.ok  # gate refuses 7 exactly like the contract


def test_probe_orx_gate_accepts_v10_through_v15(tmp_path):
    root = tmp_path / "proj"
    (root / ".orx").mkdir(parents=True)
    conn = sqlite3.connect(root / ".orx" / "state.db")
    conn.executescript(
        "CREATE TABLE meta (key TEXT, value TEXT);"
        "CREATE TABLE runs (id TEXT);"
        "INSERT INTO meta VALUES ('schema_version','10');"
        "INSERT INTO runs VALUES ('R001');")
    conn.commit()
    conn.close()
    r = probe.probe_orx(root)
    assert r.ok and r.fields["schema_version"] == "10"
    conn = sqlite3.connect(root / ".orx" / "state.db")
    conn.execute("UPDATE meta SET value='11' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    r = probe.probe_orx(root)
    assert r.ok and r.fields["schema_version"] == "11"
    conn = sqlite3.connect(root / ".orx" / "state.db")
    conn.execute("UPDATE meta SET value='12' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    # v12 (contract sixth amendment, 2026-10-08): additive only — the
    # usage_observations.source CHECK admits 'zcode_db'; no new columns
    r = probe.probe_orx(root)
    assert r.ok and r.fields["schema_version"] == "12"
    # v13/v14/v15 (G010 lifecycle amendments, 2026-10-16): additive only —
    # goals.status CHECK widens (paused/cancelling), v15 adds the
    # completion_receipts table; no pre-existing table changes shape
    for sv in ("13", "14", "15"):
        conn = sqlite3.connect(root / ".orx" / "state.db")
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", (sv,))
        conn.commit()
        conn.close()
        r = probe.probe_orx(root)
        assert r.ok and r.fields["schema_version"] == sv, sv
    conn = sqlite3.connect(root / ".orx" / "state.db")
    conn.execute("UPDATE meta SET value='16' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    # versions beyond the contract are still refused: widen only when the
    # ORX contract amendment lands
    r = probe.probe_orx(root)
    assert not r.ok
    assert any("16" in g for g in r.gaps)
