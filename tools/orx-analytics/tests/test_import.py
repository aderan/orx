"""Importer behavior: idempotency, read-only, incremental cursors."""

from __future__ import annotations

import json
import sqlite3

from orxanalytics.dbutil import file_digest
from orxanalytics.importers import (import_codex, import_cursor,
                                    import_zcode)
from orxanalytics.importers.orxstate import import_orx_project
from orxanalytics import registry


def test_zcode_import_idempotent_and_readonly(world):
    d0 = file_digest(world["zcode_db"])
    sidecars_before = sorted(p.name for p in world["zcode_db"].parent.iterdir())
    r1 = import_zcode(world["store"], world["zcode_db"])
    assert "error" not in r1, r1
    assert r1["counts"]["sessions"] == 4
    assert r1["counts"]["turns"] == 5
    # 8 source rows, 7 distinct request identities: the duplicate record of
    # req_w1 (same session_id + logical_request_id + attempt_index) upserts
    # onto the same natural key and is never counted twice
    assert r1["counts"]["requests"] == 7
    assert r1["counts"]["tools"] == 4
    assert r1["counts"]["first_parts"] == 4
    assert "model_usage_columns_missing" not in r1["counts"]
    r2 = import_zcode(world["store"], world["zcode_db"])
    assert all(v == 0 for k, v in r2["counts"].items()
               if k != "first_parts"), r2["counts"]
    assert r2["counts"]["first_parts"] == 0
    assert file_digest(world["zcode_db"]) == d0
    # read-only opens never leave -wal/-shm sidecars next to the source
    sidecars_after = sorted(p.name for p in world["zcode_db"].parent.iterdir())
    assert sidecars_after == sidecars_before


def test_zcode_model_usage_tokens_mirrored(world):
    import_zcode(world["store"], world["zcode_db"])
    rows = {r["logical_request_id"] + f"#{r['attempt_index']}": r for r in
            world["store"].conn.execute(
                "SELECT * FROM zc_requests WHERE session_id=?",
                (world["zcode"]["w2"],))}
    assert rows["req_w2#0"]["status"] == "error"
    assert rows["req_w2#0"]["input_tokens"] == 0
    assert rows["req_w2#0"]["output_tokens"] == 0
    ok = rows["req_w2#1"]
    assert (ok["input_tokens"], ok["output_tokens"], ok["cache_read"]) == \
        (6_000_000, 35_000, 5_600_000)
    assert ok["computed_total"] == 6_035_000


def test_zcode_new_rows_are_incremental(world):
    import_zcode(world["store"], world["zcode_db"])
    conn = sqlite3.connect(world["zcode_db"])
    conn.execute(
        "INSERT INTO session VALUES ('sess_new',NULL,'/x','p','t',1,2)")
    conn.commit()
    conn.close()
    r = import_zcode(world["store"], world["zcode_db"])
    assert r["counts"]["sessions"] == 1
    assert r["counts"]["turns"] == 0  # untouched tables cost nothing


def test_orx_snapshot_refuses_gate_and_imports(world):
    # wrong schema version is refused honestly
    bad = world["tmp"] / "badproj"
    (bad / ".orx").mkdir(parents=True)
    conn = sqlite3.connect(bad / ".orx" / "state.db")
    conn.executescript("CREATE TABLE meta(key TEXT, value TEXT);"
                       "INSERT INTO meta VALUES ('schema_version','7');")
    conn.commit()
    conn.close()
    r = import_orx_project(world["store"], str(bad))
    assert "error" in r

    registry.add_project(world["store"], world["proj_a"])
    r = import_orx_project(world["store"], world["proj_a"])
    assert "error" not in r, r
    assert r["counts"]["orx_attempts"] == 5
    assert r["counts"]["orx_replan_sources"] == 1
    # second import replaces the retained snapshot, preserving source history
    r2 = import_orx_project(world["store"], world["proj_a"])
    assert r2["snapshot_id"] != r["snapshot_id"]
    rows = world["store"].conn.execute(
        "SELECT COUNT(*) FROM orx_snapshots").fetchone()[0]
    assert rows == 1
    # per-snapshot rows are not duplicated within one snapshot
    n = world["store"].conn.execute(
        "SELECT COUNT(*) FROM orx_attempts WHERE snapshot_id=?",
        (r2["snapshot_id"],)).fetchone()[0]
    assert n == 5


def test_orx_v8_file_imports_without_replan(world):
    v8 = world["tmp"] / "v8proj"
    (v8 / ".orx").mkdir(parents=True)
    # build a v8-shaped file: same core tables, no replan tables, version 8
    from conftest import make_orx_db
    make_orx_db(v8 / ".orx" / "state.db")
    conn = sqlite3.connect(v8 / ".orx" / "state.db")
    conn.execute("UPDATE meta SET value='8'")
    for t in ("replan_mappings", "replan_task_mappings", "replan_sources",
              "replan_superseded"):
        conn.execute(f"DROP TABLE {t}")
    conn.commit()
    conn.close()
    r = import_orx_project(world["store"], str(v8))
    assert "error" not in r, r
    assert r["counts"]["orx_tasks"] == 5
    assert "orx_replan_maps" not in r["counts"]


def test_orx_v10_file_imports_with_replan_and_progress(world):
    # the ==9 trap: v10 keeps every v9 replan table and adds attempt_progress
    v10 = world["tmp"] / "v10proj"
    (v10 / ".orx").mkdir(parents=True)
    from conftest import make_orx_db
    make_orx_db(v10 / ".orx" / "state.db")
    conn = sqlite3.connect(v10 / ".orx" / "state.db")
    conn.execute("UPDATE meta SET value='10'")
    conn.execute(
        "CREATE TABLE attempt_progress (id INTEGER PRIMARY KEY "
        "AUTOINCREMENT, attempt_id INTEGER NOT NULL, sequence INTEGER "
        "NOT NULL, phase TEXT NOT NULL, message TEXT, received_at TEXT "
        "NOT NULL, UNIQUE(attempt_id, sequence))")
    conn.execute(
        "INSERT INTO attempt_progress(attempt_id, sequence, phase, message, "
        "received_at) VALUES (201, 1, 'build', 'running suite', "
        "'2026-10-04T12:00:05+00:00')")
    conn.commit()
    conn.close()
    r = import_orx_project(world["store"], str(v10))
    assert "error" not in r, r
    assert r["counts"]["orx_replan_sources"] == 1   # not silently dropped
    assert r["counts"]["orx_attempt_progress"] == 1
    row = world["store"].conn.execute(
        "SELECT phase, received_at FROM orx_attempt_progress "
        "WHERE snapshot_id=?", (r["snapshot_id"],)).fetchone()
    assert row["phase"] == "build"
    assert row["received_at"] == "2026-10-04T12:00:05+00:00"


def test_orx_v11_file_imports_nonce_column(world):
    # v11 keeps every v9/v10 table and adds attempts.nonce (identity anchor)
    v11 = world["tmp"] / "v11proj"
    (v11 / ".orx").mkdir(parents=True)
    from conftest import make_orx_db
    make_orx_db(v11 / ".orx" / "state.db")
    conn = sqlite3.connect(v11 / ".orx" / "state.db")
    conn.execute("UPDATE meta SET value='11'")
    conn.execute(
        "CREATE TABLE attempt_progress (id INTEGER PRIMARY KEY "
        "AUTOINCREMENT, attempt_id INTEGER NOT NULL, sequence INTEGER "
        "NOT NULL, phase TEXT NOT NULL, message TEXT, received_at TEXT "
        "NOT NULL, UNIQUE(attempt_id, sequence))")
    conn.execute(
        "ALTER TABLE attempts ADD COLUMN nonce TEXT")
    conn.execute(
        "UPDATE attempts SET nonce='orx-assignment:9c1d0a34-1e2f-4a3b-8c5d-"
        "000000000201' WHERE id=201")
    conn.commit()
    conn.close()
    r = import_orx_project(world["store"], str(v11))
    assert "error" not in r, r
    assert r["counts"]["orx_replan_sources"] == 1
    assert r["counts"]["orx_attempt_progress"] == 0   # table present, empty
    rows = {row["id"]: row for row in world["store"].conn.execute(
        "SELECT id, nonce FROM orx_attempts WHERE snapshot_id=?",
        (r["snapshot_id"],))}
    assert rows[201]["nonce"] == "orx-assignment:9c1d0a34-1e2f-4a3b-8c5d-000000000201"
    assert rows[202]["nonce"] is None


def test_orx_v12_file_imports_zcode_db_usage(world):
    # v12 keeps the v11 shape (nonce column present, replan+progress tables)
    # and only widens the usage source CHECK: a zcode_db observation imports,
    # and canonical selection ranks it between native_cli and host_report
    v12 = world["tmp"] / "v12proj"
    (v12 / ".orx").mkdir(parents=True)
    from conftest import make_orx_db
    make_orx_db(v12 / ".orx" / "state.db")
    conn = sqlite3.connect(v12 / ".orx" / "state.db")
    conn.execute("UPDATE meta SET value='12'")
    conn.execute(
        "CREATE TABLE attempt_progress (id INTEGER PRIMARY KEY "
        "AUTOINCREMENT, attempt_id INTEGER NOT NULL, sequence INTEGER "
        "NOT NULL, phase TEXT NOT NULL, message TEXT, received_at TEXT "
        "NOT NULL, UNIQUE(attempt_id, sequence))")
    conn.execute("ALTER TABLE attempts ADD COLUMN nonce TEXT")
    conn.execute(
        "UPDATE attempts SET nonce='orx-assignment:9c1d0a34-1e2f-4a3b-8c5d-"
        "000000000201' WHERE id=201")
    # attempt 203 had no observation; give it host_report (lower id) and
    # zcode_db — zcode_db must win the canonical slot on rank, not id
    conn.executemany(
        "INSERT INTO usage_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(305, 203, "cursor-strong", "R001", "T001", 700, 70, 0,
          "host_report", "exact", "2026-10-04T12:00:30+00:00"),
         (306, 203, "cursor-strong", "R001", "T001", 800, 80, 400,
          "zcode_db", "exact", "2026-10-04T12:00:31+00:00")])
    conn.commit()
    conn.close()
    r = import_orx_project(world["store"], str(v12))
    assert "error" not in r, r
    assert r["counts"]["orx_replan_sources"] == 1
    assert r["counts"]["orx_attempt_progress"] == 0   # table present, empty
    rows = {row["id"]: row for row in world["store"].conn.execute(
        "SELECT id, nonce FROM orx_attempts WHERE snapshot_id=?",
        (r["snapshot_id"],))}
    assert rows[201]["nonce"] == (
        "orx-assignment:9c1d0a34-1e2f-4a3b-8c5d-000000000201")
    assert rows[202]["nonce"] is None
    src = world["store"].conn.execute(
        "SELECT source FROM orx_usage_obs WHERE snapshot_id=? AND id=306",
        (r["snapshot_id"],)).fetchone()["source"]
    assert src == "zcode_db"
    # canonical sums: planner 398699/10755 + 201 native 1000/100
    # + 202 native 5000/500 + 203 zcode_db 800/80 (host_report 700/70 loses)
    from orxanalytics import metrics
    rep = metrics.run_report(world["store"], str(v12), "R001")
    c = rep["consumption"]
    assert c["attempts_with_observation"] == 4
    assert c["input_tokens_sum"] == 398699 + 1000 + 5000 + 800
    assert c["output_tokens_sum"] == 10755 + 100 + 500 + 80


def test_orx_v13_file_imports_lifecycle_goal_states(world):
    # v13 (G010 T002): goals.status CHECK widens with 'paused' — the table
    # shape is v11/v12 otherwise. The raw goal state mirrors verbatim; it is
    # read from goals.status, never derived from run aggregates.
    v13 = world["tmp"] / "v13proj"
    (v13 / ".orx").mkdir(parents=True)
    from conftest import make_orx_db
    make_orx_db(v13 / ".orx" / "state.db")
    conn = sqlite3.connect(v13 / ".orx" / "state.db")
    conn.execute("UPDATE meta SET value='13'")
    conn.execute(
        "CREATE TABLE attempt_progress (id INTEGER PRIMARY KEY "
        "AUTOINCREMENT, attempt_id INTEGER NOT NULL, sequence INTEGER "
        "NOT NULL, phase TEXT NOT NULL, message TEXT, received_at TEXT "
        "NOT NULL, UNIQUE(attempt_id, sequence))")
    conn.execute("ALTER TABLE attempts ADD COLUMN nonce TEXT")
    conn.execute("UPDATE goals SET status='paused'")
    conn.commit()
    conn.close()
    r = import_orx_project(world["store"], str(v13))
    assert "error" not in r, r
    assert r["counts"]["orx_replan_sources"] == 1      # v9 tables still kept
    assert r["counts"]["orx_attempt_progress"] == 0    # v10 table still kept
    assert "orx_receipts" not in r["counts"]           # receipts are v15-only
    row = world["store"].conn.execute(
        "SELECT status FROM orx_goals WHERE snapshot_id=?",
        (r["snapshot_id"],)).fetchone()
    assert row["status"] == "paused"                   # verbatim, not derived


def test_orx_v15_file_imports_receipts(world):
    # v15 (G010 T005): adds completion_receipts — one write-once receipt per
    # accepted delivery, keyed by the UNIQUE attempt_id. Only receipt FACTS
    # are mirrored; receipt_json (the verbatim replay payload) never is —
    # analytics reads receipts, it never replays them.
    v15 = world["tmp"] / "v15proj"
    (v15 / ".orx").mkdir(parents=True)
    from conftest import make_orx_db
    make_orx_db(v15 / ".orx" / "state.db")
    conn = sqlite3.connect(v15 / ".orx" / "state.db")
    conn.execute("UPDATE meta SET value='15'")
    conn.execute(
        "CREATE TABLE attempt_progress (id INTEGER PRIMARY KEY "
        "AUTOINCREMENT, attempt_id INTEGER NOT NULL, sequence INTEGER "
        "NOT NULL, phase TEXT NOT NULL, message TEXT, received_at TEXT "
        "NOT NULL, UNIQUE(attempt_id, sequence))")
    conn.execute("ALTER TABLE attempts ADD COLUMN nonce TEXT")
    conn.execute(
        "UPDATE attempts SET nonce='orx-assignment:9c1d0a34-1e2f-4a3b-8c5d-"
        "000000000201' WHERE id=201")
    conn.execute("UPDATE goals SET status='cancelling'")
    conn.execute(
        "CREATE TABLE completion_receipts (id INTEGER PRIMARY KEY "
        "AUTOINCREMENT, attempt_id INTEGER NOT NULL UNIQUE, revision_id "
        "INTEGER, task_id TEXT, delivery_status TEXT, delivery_digest "
        "TEXT, session_ref TEXT, actual_model TEXT, receipt_json TEXT, "
        "created_at TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO completion_receipts(id, attempt_id, revision_id, "
        "task_id, delivery_status, delivery_digest, session_ref, "
        "actual_model, receipt_json, created_at) VALUES (7, 201, 10, "
        "'T001', 'passed', "
        "'d94c5e548240ab9f3d6ad5584a48ff9581be22f79417f6ce08a15ecc6b1a0d2e', "
        "'sess_subagent_worker', 'glm-5.3', "
        "'{\"status\":\"passed\",\"idempotent\":true}', "
        "'2026-10-16T18:00:00+00:00')")
    conn.commit()
    conn.close()
    r = import_orx_project(world["store"], str(v15))
    assert "error" not in r, r
    assert r["counts"]["orx_receipts"] == 1
    assert r["counts"]["orx_replan_sources"] == 1
    row = world["store"].conn.execute(
        "SELECT attempt_id, task_id, delivery_status, delivery_digest, "
        "session_ref, actual_model, created_at FROM orx_receipts "
        "WHERE snapshot_id=?", (r["snapshot_id"],)).fetchone()
    assert row["attempt_id"] == 201
    assert row["task_id"] == "T001"
    assert row["delivery_status"] == "passed"
    assert row["delivery_digest"].startswith("d94c5e54")
    assert row["created_at"] == "2026-10-16T18:00:00+00:00"
    # the replay payload is deliberately absent from the mirror surface
    cols = {c["name"] for c in world["store"].conn.execute(
        "PRAGMA table_info(orx_receipts)")}
    assert "receipt_json" not in cols
    nonce = world["store"].conn.execute(
        "SELECT nonce FROM orx_attempts WHERE snapshot_id=? AND id=201",
        (r["snapshot_id"],)).fetchone()
    assert nonce["nonce"] == "orx-assignment:9c1d0a34-1e2f-4a3b-8c5d-000000000201"
    goal = world["store"].conn.execute(
        "SELECT status FROM orx_goals WHERE snapshot_id=?",
        (r["snapshot_id"],)).fetchone()
    assert goal["status"] == "cancelling"              # second new state, verbatim


def test_store_v2_upgrades_to_v3_adding_receipts(tmp_path):
    """An existing analytics.db (store v2, before orx_receipts existed)
    opens, upgrades in place, keeps its rows; a foreign future version
    still refuses."""
    import pytest
    from orxanalytics.store import Store
    old = Store(tmp_path / "home1")
    old.conn.execute(
        "INSERT INTO orx_snapshots(project_key, taken_at, schema_version, "
        "source_path) VALUES ('p','2026-10-12T00:00:00+00:00','12','x')")
    old.conn.commit()
    old.close()
    # degrade it to a v2 store: drop the receipts mirror, reset the version
    conn = sqlite3.connect(tmp_path / "home1" / "analytics.db")
    conn.execute("DROP TABLE orx_receipts")
    conn.execute("UPDATE meta SET value='2' WHERE key='store_version'")
    conn.commit()
    conn.close()

    store = Store(tmp_path / "home1")
    try:
        cols = {r["name"] for r in store.conn.execute(
            "PRAGMA table_info(orx_receipts)")}
        assert {"attempt_id", "delivery_status", "delivery_digest"} <= cols
        version = store.conn.execute(
            "SELECT value FROM meta WHERE key='store_version'").fetchone()[0]
        assert version == "4"   # chain lands on the current store version
        kept = store.conn.execute(
            "SELECT COUNT(*) FROM orx_snapshots").fetchone()[0]
        assert kept == 1
    finally:
        store.close()
    # a version this code cannot upgrade refuses
    conn = sqlite3.connect(tmp_path / "home1" / "analytics.db")
    conn.execute("UPDATE meta SET value='9' WHERE key='store_version'")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="store_version 9"):
        Store(tmp_path / "home1")


def test_store_v3_upgrades_to_v4_backfilling_request_tokens(tmp_path, world):
    """An existing v3 store (zc_requests without token columns, cursor
    already past every row) upgrades in place; the cursor reset makes the
    next import re-read the mirrored rows and backfill the new fields —
    without double counting — and a foreign future version still refuses."""
    import pytest
    from orxanalytics.store import Store
    old = Store(tmp_path / "home1")
    old.close()
    # degrade to a v3 store: drop the token columns, reset the version, and
    # pretend a v3-era import already mirrored every model_usage row (cursor
    # at max) WITHOUT token fields
    import_zcode(world["store"], world["zcode_db"])
    src = world["store"].conn.execute(
        "SELECT session_id, logical_request_id, attempt_index, turn_id, "
        "provider_id, model_id, status, started_ms, completed_ms "
        "FROM zc_requests").fetchall()
    conn = sqlite3.connect(tmp_path / "home1" / "analytics.db")
    for name in ("input_tokens", "output_tokens", "reasoning_tokens",
                 "cache_read", "cache_creation", "provider_total",
                 "computed_total"):
        conn.execute(f"ALTER TABLE zc_requests DROP COLUMN {name}")
    conn.executemany(
        "INSERT INTO zc_requests(session_id, logical_request_id, "
        "attempt_index, turn_id, provider_id, model_id, status, started_ms, "
        "completed_ms) VALUES (?,?,?,?,?,?,?,?,?)",
        [tuple(r) for r in src])
    conn.execute(
        "INSERT INTO import_state VALUES ('zcode:" + str(world["zcode_db"])
        + "','model_usage_rowid','999999')")
    conn.execute("UPDATE meta SET value='3' WHERE key='store_version'")
    conn.commit()
    conn.close()

    store = Store(tmp_path / "home1")
    try:
        cols = {r["name"] for r in store.conn.execute(
            "PRAGMA table_info(zc_requests)")}
        assert {"input_tokens", "output_tokens", "cache_read",
                "computed_total"} <= cols
        version = store.conn.execute(
            "SELECT value FROM meta WHERE key='store_version'").fetchone()[0]
        assert version == "4"
        # the upgrade reset the cursor: rows survive, fields still empty
        n_before = store.conn.execute(
            "SELECT COUNT(*) FROM zc_requests").fetchone()[0]
        assert n_before == len(src)
        assert store.conn.execute(
            "SELECT COUNT(*) FROM zc_requests WHERE input_tokens IS NULL "
            "AND output_tokens IS NULL").fetchone()[0] == len(src)
    finally:
        store.close()

    # the next import re-reads everything (cursor reset) and backfills the
    # token fields; upserts mean ZERO new rows — no double counting
    from orxanalytics.store import Store as _Store
    back = _Store(tmp_path / "home1")
    try:
        r = import_zcode(back, world["zcode_db"])
        assert r["counts"]["requests"] == 0, r["counts"]
        n_after = back.conn.execute(
            "SELECT COUNT(*) FROM zc_requests").fetchone()[0]
        assert n_after == len(src)
        row = back.conn.execute(
            "SELECT input_tokens, output_tokens FROM zc_requests WHERE "
            "session_id=? AND logical_request_id='req_c1'",
            (world["zcode"]["controller"],)).fetchone()
        assert (row["input_tokens"], row["output_tokens"]) == (1_000_000, 20_000)
        # and a repeated collect still changes nothing
        r2 = import_zcode(back, world["zcode_db"])
        assert r2["counts"]["requests"] == 0
        assert back.conn.execute(
            "SELECT COUNT(*) FROM zc_requests").fetchone()[0] == len(src)
    finally:
        back.close()
    # a version this code cannot upgrade refuses
    conn = sqlite3.connect(tmp_path / "home1" / "analytics.db")
    conn.execute("UPDATE meta SET value='9' WHERE key='store_version'")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="store_version 9"):
        Store(tmp_path / "home1")


def test_store_v1_upgrades_through_v2_to_v3(tmp_path):
    """An existing analytics.db (store v1, orx_attempts without nonce, before
    orx_receipts existed) opens, upgrades in place through every additive
    step, and keeps its rows; a foreign future version still refuses."""
    import pytest
    from orxanalytics.store import Store
    old = Store(tmp_path / "home1")
    old.conn.execute(
        "INSERT INTO orx_snapshots(project_key, taken_at, schema_version, "
        "source_path) VALUES ('p','2026-10-06T00:00:00+00:00','11','x')")
    old.conn.commit()
    old.close()
    # degrade it to a v1 store: drop the nonce column and the receipts
    # mirror, reset the version
    conn = sqlite3.connect(tmp_path / "home1" / "analytics.db")
    conn.execute("ALTER TABLE orx_attempts DROP COLUMN nonce")
    conn.execute("DROP TABLE orx_receipts")
    conn.execute("UPDATE meta SET value='1' WHERE key='store_version'")
    conn.commit()
    conn.close()

    store = Store(tmp_path / "home1")
    try:
        cols = {r["name"] for r in store.conn.execute(
            "PRAGMA table_info(orx_attempts)")}
        assert "nonce" in cols
        receipts = {r["name"] for r in store.conn.execute(
            "PRAGMA table_info(orx_receipts)")}
        assert "delivery_digest" in receipts
        version = store.conn.execute(
            "SELECT value FROM meta WHERE key='store_version'").fetchone()[0]
        assert version == "4"   # chain lands on the current store version
        kept = store.conn.execute(
            "SELECT COUNT(*) FROM orx_snapshots").fetchone()[0]
        assert kept == 1
    finally:
        store.close()
    # a version this code cannot upgrade refuses
    conn = sqlite3.connect(tmp_path / "home1" / "analytics.db")
    conn.execute("UPDATE meta SET value='9' WHERE key='store_version'")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="store_version 9"):
        Store(tmp_path / "home1")


def test_codex_import_incremental_and_torn_tail(world):
    r1 = import_codex(world["store"], world["codex_dir"])
    assert r1["counts"]["threads"] == 1
    assert r1["counts"]["usage"] == 1
    # append a new record plus a torn (partial) line
    f = world["codex_dir"] / "2026" / "10" / "04" / "rollout-test.jsonl"
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "timestamp": "2026-10-04T12:01:00.000Z", "ordinal": 5,
            "type": "token_usage_record",
            "payload": {"thread_id": "01aa-cx-1", "turn_id": "turn1",
                        "response_id": "resp_2",
                        "usage": {"input_tokens": 50, "output_tokens": 5,
                                  "total_tokens": 55}}}) + "\n")
        fh.write('{"timestamp": "2026-10-04T12:0')   # torn tail
    r2 = import_codex(world["store"], world["codex_dir"])
    assert r2["counts"]["usage"] == 1
    assert r2["counts"]["threads"] == 0
    # completing the torn line next round parses it (no double count)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write('1:00.500Z","ordinal":6,"type":"event_msg","payload":'
                 '{"type":"task_complete","turn_id":"turn2",'
                 '"completed_at":1791115261}}\n')
    r3 = import_codex(world["store"], world["codex_dir"])
    assert r3["counts"]["events"] == 1
    n = world["store"].conn.execute(
        "SELECT COUNT(*) FROM cx_usage").fetchone()[0]
    assert n == 2


def test_cursor_import_preserves_usage_across_rounds(world):
    r1 = import_cursor(world["store"], world["cursor_db"])
    assert r1["counts"]["composers_inserted"] == 1
    row = world["store"].conn.execute(
        "SELECT name, usage_json FROM cur_composers").fetchone()
    assert row["name"] == "cursor task"
    assert json.loads(row["usage_json"])["total"]["input"] == 2000
    # second round with no change: no refresh, usage preserved
    r2 = import_cursor(world["store"], world["cursor_db"])
    assert r2["counts"]["composers_inserted"] == 0
    assert r2["counts"]["composers_refreshed"] == 0
    row = world["store"].conn.execute(
        "SELECT usage_json FROM cur_composers").fetchone()
    assert row["usage_json"] is not None
    assert r1["counts"]["workspaces"] == 1
    assert r2["counts"]["workspaces"] == 0
    ws = world["store"].conn.execute(
        "SELECT folder FROM cur_workspaces").fetchone()
    assert ws["folder"] == "file://" + world["proj_a"]



def test_unknown_store_refusal_does_not_apply_schema_ddl(tmp_path):
    import pytest
    from orxanalytics.store import Store

    db = tmp_path / "analytics.db"
    connection = sqlite3.connect(db)
    with connection:
        connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO meta VALUES ('store_version', '99')")
    connection.close()
    before = db.read_bytes()
    with pytest.raises(RuntimeError, match="store_version 99"):
        Store(tmp_path)
    assert db.read_bytes() == before
