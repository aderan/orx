"""Execute the observability read contract against the observability fixture.

The SQL is taken from docs/observability-contract.md. Nothing here opens
Store or migrates a database.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parents[1]
CONTRACT = ROOT / "docs" / "observability-contract.md"
SEED = ROOT / "tests" / "fixtures" / "observability" / "seed.sql"

QUERIES = (
    "schema_gate",
    "linkage",
    "delivery_failure_kind",
    "verification_history",
    "verification_current",
    "manual_ledger_dedupe",
    "delivery_quality",
    "consumption",
    "elapsed",
    "elapsed_spans",
    "first_acceptance",
    "rework",
    "coverage",
    "coverage_fields",
)

_FENCE = re.compile(r"```sql\n(.*?)```", re.S)
_NAME = re.compile(r"-- query: (\w+)\n")
# replace(...) is the scalar function used to normalize timestamps.
_WRITE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|VACUUM|ATTACH|REINDEX)\b|\bREPLACE\s+INTO\b",
    re.I,
)


def load_queries() -> dict[str, str]:
    text = CONTRACT.read_text()
    found: dict[str, str] = {}
    for block in _FENCE.findall(text):
        match = _NAME.match(block)
        assert match, block[:80]
        name = match.group(1)
        assert name not in found
        assert _WRITE.search(block) is None, name
        found[name] = block
    assert tuple(found) == QUERIES
    return found


def build_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SEED.read_text())
        conn.commit()
    finally:
        conn.close()


def open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def run(conn: sqlite3.Connection, sql: str, project_id: str = "proj-alpha"):
    params = {"project_id": project_id} if ":project_id" in sql else {}
    return conn.execute(sql, params).fetchall()


def test_documented_queries_do_not_modify_the_database(tmp_path: Path):
    db = tmp_path / "state.db"
    build_db(db)
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    queries = load_queries()
    conn = open_readonly(db)
    try:
        for sql in queries.values():
            run(conn, sql)
        assert _write_was_rejected(conn)
    finally:
        conn.close()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == digest
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()

    snap = tmp_path / "snap.db"
    src = open_readonly(db)
    try:
        dst = sqlite3.connect(snap)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == digest
    assert not Path(str(db) + "-wal").exists()


def _write_was_rejected(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("INSERT INTO meta(key, value) VALUES('x', 'y')")
    except sqlite3.OperationalError:
        return True
    return False


def test_schema_gate_refuses_anything_but_v8(tmp_path: Path):
    queries = load_queries()
    db = tmp_path / "state.db"
    build_db(db)
    conn = open_readonly(db)
    try:
        assert run(conn, queries["schema_gate"])[0]["decision"] == "ok"
    finally:
        conn.close()

    for value in ("7", "9", "v8", None):
        sample = tmp_path / f"state-{value}.db"
        build_db(sample)
        writer = sqlite3.connect(sample)
        try:
            if value is None:
                writer.execute("DELETE FROM meta WHERE key = 'schema_version'")
            else:
                writer.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                    (value,),
                )
            writer.commit()
        finally:
            writer.close()
        reader = open_readonly(sample)
        try:
            assert run(reader, queries["schema_gate"])[0]["decision"] == "refuse"
        finally:
            reader.close()


def test_linkage_keeps_repeated_ids_and_unknown_runs_apart():
    queries = load_queries()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SEED.read_text())
    try:
        alpha = run(conn, queries["linkage"], "proj-alpha")
        beta = run(conn, queries["linkage"], "proj-beta")
        assert [row["attempt_id"] for row in alpha] == [row["attempt_id"] for row in beta]
        assert {row["project_id"] for row in alpha} == {"proj-alpha"}
        assert {row["project_id"] for row in beta} == {"proj-beta"}

        by_id = {row["attempt_id"]: row for row in alpha}
        failed_planner = by_id[1]
        assert failed_planner["revision_row_id"] is None
        assert failed_planner["revision"] is None
        assert failed_planner["run_id"] == "R001"
        assert failed_planner["assignment_id"] == "P002"
        assert failed_planner["run_association"] == "known"

        legacy = by_id[2]
        assert legacy["run_id"] is None
        assert legacy["assignment_run_id"] is None
        assert legacy["revision_row_id"] is None
        assert legacy["run_association"] == "unknown"
        assert legacy["session_ref"] is None

        t001 = [row for row in alpha if row["task_id"] == "T001"]
        assert {(row["revision"], row["task_row_id"]) for row in t001} == {(1, 10), (2, 20)}
        assert len({row["task_row_id"] for row in t001}) == 2
        assert by_id[3]["session_ref"] == "11111111-1111-4111-8111-111111111111"
        assert by_id[5]["session_ref"] == "host-sess-7"
    finally:
        conn.close()


def test_six_readings_match_fixture_semantics():
    queries = load_queries()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SEED.read_text())
    try:
        quality = run(conn, queries["delivery_quality"])
        assert [
            (row["revision"], row["task_id"], row["task_row_id"], row["status"])
            for row in quality
        ] == [
            (1, "T001", 10, "failed"),
            (1, "T002", 11, "passed"),
            (1, "T003", 12, "passed"),
            (1, "T004", 13, "passed"),
            (2, "T001", 20, "passed"),
        ]
        rev1 = [row for row in quality if row["revision"] == 1]
        rev2 = [row for row in quality if row["revision"] == 2]
        assert {row["passed_in_revision"] for row in rev1} == {3}
        assert {row["terminal_in_revision"] for row in rev1} == {4}
        assert {row["passed_in_revision"] for row in rev2} == {1}
        assert rev1[0]["task_row_id"] != rev2[0]["task_row_id"]

        spent = run(conn, queries["consumption"])
        assert len(spent) == 1
        row = spent[0]
        assert row["canonical_observations"] == 5
        assert row["input_tokens_sum"] == 128
        assert row["output_tokens_sum"] == 18
        assert row["cached_input_tokens_sum"] == 909
        assert row["cached_input_tokens_sum"] > row["input_tokens_sum"]
        assert row["cached_missing"] == 1
        assert row["cached_present"] == 4
        assert row["input_missing"] == 0
        assert row["accuracy_exact"] == 3
        assert row["accuracy_estimated"] == 1
        assert row["accuracy_unknown"] == 1
        assert "fee" not in row.keys()

        elapsed = {row["run_id"]: row for row in run(conn, queries["elapsed"])}
        assert elapsed["R001"]["span_sum_sec"] == 12900
        assert elapsed["R001"]["spans_complete"] == 14
        assert elapsed["R001"]["spans_incomplete"] == 0
        assert elapsed["R001"]["waiting_sec"] == 7200
        assert elapsed["R001"]["span_sum_sec"] != elapsed["R001"]["waiting_sec"]
        assert elapsed["R002"]["span_sum_sec"] == 0
        assert elapsed["R002"]["waiting_sec"] is None
        assert elapsed["R003"]["waiting_sec"] is None
        assert elapsed["R003"]["span_sum_sec"] == 0
        assert elapsed[None]["span_sum_sec"] is None
        assert elapsed[None]["spans_incomplete"] == 1
        assert elapsed[None]["waiting_sec"] is None

        spans = {row["attempt_id"]: row for row in run(conn, queries["elapsed_spans"])}
        assert spans[4]["span_sec"] == 1800
        assert spans[5]["span_sec"] == 1800
        assert spans[4]["started_at"] < spans[5]["started_at"] < spans[4]["ended_at"]
        assert spans[2]["span_sec"] is None
        assert sum(row["span_sec"] or 0 for row in spans.values()) == 12900

        first = run(conn, queries["first_acceptance"])
        assert [(row["revision"], row["task_id"], row["first_pass"], row["final_status"])
                for row in first] == [
            (1, "T001", 0, "failed"),
            (1, "T002", 1, "passed"),
            (1, "T003", 0, "passed"),
            (1, "T004", 0, "passed"),
            (2, "T001", 1, "passed"),
        ]
        assert {row["first_pass_numerator"] for row in first} == {2}
        assert {row["first_pass_denominator"] for row in first} == {5}
        assert len({(row["revision"], row["task_row_id"]) for row in first if row["task_id"] == "T001"}) == 2

        reasons = [
            (row["revision"], row["task_id"], row["task_row_id"], row["reason"])
            for row in run(conn, queries["rework"])
        ]
        assert reasons == [
            (1, "T001", 10, "tests failed: missing contract"),
            (1, "T001", 10, "agent verifier: contract section absent"),
            (1, "T003", 12, "agent verifier: fixture gap"),
            (1, "T004", 13, "agent verifier: stale result cleared on retry"),
        ]

        coverage = {
            (row["dimension"], row["dimension_value"]): row
            for row in run(conn, queries["coverage"])
        }
        expect = {
            ("role", "planner"): (3, 1, 1, 1),
            ("role", "worker"): (6, 3, 1, 2),
            ("role", "verifier"): (6, 1, 2, 3),
            ("profile", "codex-strong"): (1, 1, 0, 0),
            ("profile", "cursor-economy"): (12, 3, 3, 6),
            ("profile", "orx-host"): (1, 1, 0, 0),
            ("profile", "shell-local"): (1, 0, 1, 0),
            ("harness", "codex"): (1, 1, 0, 0),
            ("harness", "cursor"): (12, 3, 3, 6),
            ("harness", "shell"): (1, 0, 1, 0),
            ("harness", "zcode"): (1, 1, 0, 0),
        }
        assert set(coverage) == set(expect)
        for key, counts in expect.items():
            got = coverage[key]
            assert (
                got["attempts"], got["observed"], got["missing_recorded"], got["unassessed"]
            ) == counts
            assert got["observed"] + got["missing_recorded"] + got["unassessed"] == got["attempts"]

        fields = run(conn, queries["coverage_fields"])[0]
        assert fields["dimension"] == "token_field"
        assert fields["observations"] == 7
        assert fields["input_present"] == 7
        assert fields["output_present"] == 7
        assert fields["cached_present"] == 6
        assert fields["cached_missing"] == 1
        assert fields["input_missing"] == 0

        ledger = run(conn, queries["manual_ledger_dedupe"])
        assert [
            (row["attempt_id"], row["task_id"], row["ledger_disposition"])
            for row in ledger
        ] == [
            (5, "T001", "excluded_duplicate"),
            (11, "T003", "eligible"),
            (None, "T001", "unknown"),
        ]
    finally:
        conn.close()


def test_contract_points_readers_away_from_store_and_an_in_repo_analyzer():
    text = CONTRACT.read_text()
    assert "Do not call Store.open" in text
    assert "from orx" not in text
    assert "orx-analytics" in text
    assert "tests/fixtures/observability/seed.sql" in text


def test_verification_history_and_current_window_split_rounds():
    """Per-attempt history vs the current view: retry keeps every round's
    rows (grouped by attempt), and the current read drops rows bound to
    attempts older than the task's latest worker attempt."""
    queries = load_queries()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SEED.read_text())
    try:
        history = run(conn, queries["verification_history"])
        rev1_t001 = [
            (row["attempt_id"], row["attempt_role"],
             row["verification_rows"], row["passed_rows"])
            for row in history
            if row["revision_id"] == 1 and row["task_id"] == "T001"
        ]
        assert rev1_t001 == [
            (4, "worker", 1, 0),    # the failed round's red gate row
            (5, "worker", 1, 1),    # the retried round ran the same command green
            (6, "verifier", 1, 0),  # the agent verdict that failed the round
        ]
        # one group per attempt; nothing was deleted anywhere
        assert sum(row["verification_rows"] for row in history) == 10

        current = run(conn, queries["verification_current"])
        rev1_t001_now = [
            (row["verification_row_id"], row["attempt_id"], row["passed"])
            for row in current
            if row["revision_id"] == 1 and row["task_id"] == "T001"
        ]
        assert rev1_t001_now == [(1, 6, 0), (10, 5, 1)]  # row 9 is history
        assert {(row["revision_id"], row["task_id"]) for row in current} == {
            (1, "T001"), (1, "T002"), (1, "T003"), (1, "T004"), (2, "T001"),
        }
        assert all(row["verification_row_id"] != 9 for row in current)
        # single-round tasks keep all of their rows in the current view
        rev1_t003 = [
            row["verification_row_id"]
            for row in current
            if row["revision_id"] == 1 and row["task_id"] == "T003"
        ]
        assert rev1_t003 == [4, 5]
    finally:
        conn.close()
