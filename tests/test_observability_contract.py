"""Execute the observability read contract against the observability fixture.

The SQL is taken from docs/observability-contract.md. Nothing here opens
Store or migrates a database.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

import pytest

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
    "replan_correspondence",
    "replan_superseded",
    "replan_artifact_provenance",
    "progress_history",
    "progress_current",
    "goal_lifecycle",
    "goal_lifecycle_events",
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


def test_schema_gate_accepts_only_explicitly_supported_versions(tmp_path: Path):
    """The gate is an explicit allowlist (8 through 15), never a range:
    every other value — older, newer, or malformed — is refused."""
    queries = load_queries()
    db = tmp_path / "state.db"
    build_db(db)
    conn = open_readonly(db)
    try:
        assert run(conn, queries["schema_gate"])[0]["decision"] == "ok"
    finally:
        conn.close()

    for value, expected in (("8", "ok"), ("9", "ok"), ("7", "refuse"),
                            ("10", "ok"), ("11", "ok"), ("12", "ok"),
                            ("13", "ok"), ("14", "ok"), ("15", "ok"),
                            ("16", "refuse"),
                            ("v9", "refuse"), (None, "refuse")):
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
            assert run(reader, queries["schema_gate"])[0]["decision"] == expected, value
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


def test_replan_reads_resolve_identity_not_task_numbers():
    """The G004 reads key on (run, revision, task_id) plus attempt/evidence
    row ids: the short id T001 exists on revisions 1 and 2, and only the
    full identity tells the mapped task, its source, and the two attempts
    of that source apart."""
    queries = load_queries()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SEED.read_text())
    try:
        # No mapping rows for revision 1 at all: first plans and legacy
        # revisions simply have no correspondence rows to read.
        rows = run(conn, queries["replan_correspondence"])
        assert [(r["run_id"], r["revision"], r["task_id"], r["classification"],
                 r["source_revision"], r["source_task_id"], r["part"])
                for r in rows] == [
            ("R001", 2, "T001", "redo", 1, "T001", 0),
        ]
        # The one row carries T001 as BOTH the revision 2 task and the
        # revision 1 source: the same number, two revisions, two tasks —
        # the row only exists because the mapping declared the pair.

        superseded = run(conn, queries["replan_superseded"])
        assert [(r["source_revision"], r["source_task_id"], r["disposition"],
                 r["successors_json"]) for r in superseded] == [
            (1, "T001", "redone", '["T001"]'),
            (1, "T002", "dropped", "[]"),
            (1, "T003", "dropped", "[]"),
            (1, "T004", "dropped", "[]"),
        ]
        assert all(r["note"] for r in superseded if r["disposition"] == "dropped")
        assert {r["superseded_in_revision"] for r in superseded} == {2}

        provenance = run(conn, queries["replan_artifact_provenance"])
        assert [(r["revision"], r["task_id"], r["source_revision"],
                 r["source_task_id"], r["artifact"], r["attempt_id"], r["evidence_id"])
                for r in provenance] == [
            # same source task (1, T001), three provenances: the failed
            # round's attempt 4, the retry's attempt 5, and the retry's
            # completion evidence row 1 — never collapsed by task number.
            (2, "T001", 1, "T001",
             ".orx/runs/R001/check/T001/0001-a4-command.log", 4, None),
            (2, "T001", 1, "T001",
             ".orx/runs/R001/check/T001/0002-a5-command.log", 5, None),
            (2, "T001", 1, "T001",
             ".orx/runs/R001/evidence/T001-5.json", 5, 1),
        ]
        assert len({r["attempt_id"] for r in provenance}) == 2
    finally:
        conn.close()


def test_progress_reads_keep_history_and_current_window_apart():
    """v10 attempt_progress reads (G006). History returns each report bound
    to its attempt in sequence order with full identity; the current window
    is the task's latest attempt only — attempt 5's reports are history, so
    no window on this fixture shows one, and a task whose current attempt
    never reported reads NULL (unknown), never a claim time or an older
    attempt's report."""
    queries = load_queries()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SEED.read_text())
    try:
        history = run(conn, queries["progress_history"])
        assert [
            (r["attempt_id"], r["sequence"], r["phase"], r["message"], r["received_at"])
            for r in history
        ] == [
            (5, 1, "implementing", "store interface first",
             "2026-10-04T01:35:00.000000+00:00"),
            (5, 2, "checking", None, "2026-10-04T01:50:00.000000+00:00"),
            (5, 3, "delivering", "evidence staged",
             "2026-10-04T01:50:00.000000+00:00"),
        ]
        row = history[0]
        assert (row["attempt_role"], row["attempt_driver"]) == ("worker", "host")
        assert (row["revision"], row["task_id"]) == (1, "T001")
        # Two reports share one received_at: sequence is the authority.
        assert history[1]["received_at"] == history[2]["received_at"]
        assert history[1]["sequence"] < history[2]["sequence"]

        current = {
            (r["revision_id"], r["task_id"]): r
            for r in run(conn, queries["progress_current"])
        }
        assert set(current) == {
            (1, "T001"), (1, "T002"), (1, "T003"), (1, "T004"), (2, "T001"),
        }
        # Reports exist in the file, but every task's current attempt (the
        # verifier rounds 6 and 10 closed them out) has none: unknown, with
        # no fallback to attempt 5's or attempt 9's history.
        for row in current.values():
            assert row["sequence"] is None and row["phase"] is None
            assert row["message"] is None and row["received_at"] is None
        assert current[(1, "T001")]["attempt_id"] == 6
        assert current[(2, "T001")]["attempt_id"] == 10
    finally:
        conn.close()


def test_progress_current_reports_only_the_current_attempts_report():
    """The reported side of the current window: a report bound to the task's
    latest attempt surfaces; older attempts of the same task stay history.
    The closed-history fixture cannot contain such a row (the write path
    only reports on the latest, still-open host-worker attempt), so this
    test appends the live-case row to a copy before reading."""
    queries = load_queries()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SEED.read_text())
    try:
        conn.execute(
            "INSERT INTO attempt_progress(attempt_id, sequence, phase, message,"
            " received_at) VALUES(10, 1, 'implementing', 'live round',"
            " '2026-10-04T02:45:00.000000+00:00')"
        )
        current = {
            (r["revision_id"], r["task_id"]): r
            for r in run(conn, queries["progress_current"])
        }
        reported = current[(2, "T001")]
        assert reported["attempt_id"] == 10
        assert (reported["sequence"], reported["phase"], reported["message"],
                reported["received_at"]) == (
            1, "implementing", "live round", "2026-10-04T02:45:00.000000+00:00",
        )
        # Other windows are untouched: revision 1 T001 still reads unknown
        # even though attempt 5 reported earlier in that task's history.
        assert current[(1, "T001")]["phase"] is None
        # History now shows both attempts' rows, each in its own sequence
        # space, ordered by attempt then sequence.
        history = run(conn, queries["progress_history"])
        assert [(r["attempt_id"], r["sequence"]) for r in history] == [
            (5, 1), (5, 2), (5, 3), (10, 1),
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# G010 T006 (eleventh amendment): goal lifecycle reads — the new goal states
# (paused / cancelling), the new event families (goal_cancel* and the
# suppression re-verification seam), and their times. The committed fixture
# predates v13 (its goals CHECK has the pre-v13 vocabulary), so this test
# widens the CHECK on its private in-memory copy exactly the way the real
# v13/v14 migrations do (backup-replace, rows preserved verbatim) before
# inserting the lifecycle rows.


def _seed_with_v14_goals() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SEED.read_text())
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("ALTER TABLE goals RENAME TO goals_seed")
    conn.execute(
        "CREATE TABLE goals ("
        " id TEXT PRIMARY KEY,"
        " objective TEXT NOT NULL,"
        " constraints_json TEXT NOT NULL DEFAULT '[]',"
        " acceptance_json TEXT NOT NULL DEFAULT '[]',"
        " context TEXT NOT NULL DEFAULT '',"
        " status TEXT NOT NULL CHECK (status IN"
        "   ('active','paused','cancelling','done','cancelled')),"
        " created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO goals SELECT id, objective, constraints_json,"
        " acceptance_json, context, status, created_at, updated_at"
        " FROM goals_seed"
    )
    conn.execute("DROP TABLE goals_seed")
    conn.commit()
    return conn


def test_goal_lifecycle_reads_states_times_and_open_executions():
    queries = load_queries()
    conn = _seed_with_v14_goals()
    try:
        # The pre-existing seed goal survived the CHECK widening verbatim.
        # (The fixture gives G001 three runs; real databases have one run
        # per goal — the row for R001 is the completed one.)
        seed_rows = run(conn, queries["goal_lifecycle"])
        assert {r["goal_id"] for r in seed_rows} == {"G001"}
        g001 = next(r for r in seed_rows if r["run_id"] == "R001")
        assert g001["goal_status"] == "done"
        assert g001["goal_cancel_events"] == 0
        assert g001["open_attempts"] == 0
        assert g001["run_completed_at"] == "2026-10-04T03:00:00.000000+00:00"

        # A paused goal whose run is still running (the honest combination:
        # pause writes one row — goals.status — and leaves in-flight work
        # alone), and a cancelling goal mid-wrap-up with one unconfirmed
        # execution and recorded goal_cancel transitions.
        conn.executescript("""
        INSERT INTO goals(id, objective, status, created_at, updated_at) VALUES
          ('G002', 'paused round', 'paused',
           '2026-10-16T01:00:00.000000+00:00',
           '2026-10-16T02:00:00.000000+00:00'),
          ('G003', 'cancel wrap-up', 'cancelling',
           '2026-10-16T03:00:00.000000+00:00',
           '2026-10-16T04:00:00.000000+00:00');
        INSERT INTO runs(id, goal_id, status, created_at, updated_at, started_at)
        VALUES
          ('R004', 'G002', 'running',
           '2026-10-16T01:00:00.000000+00:00',
           '2026-10-16T02:00:00.000000+00:00',
           '2026-10-16T01:30:00.000000+00:00'),
          ('R005', 'G003', 'running',
           '2026-10-16T03:00:00.000000+00:00',
           '2026-10-16T04:00:00.000000+00:00',
           '2026-10-16T03:30:00.000000+00:00');
        INSERT INTO plan_revisions(run_id, revision, depth, planner_profile,
                                   ir_json, status, created_at)
        VALUES ('R005', 1, 'light', 'host-planner', '{}', 'active',
                '2026-10-16T03:05:00.000000+00:00');
        INSERT INTO tasks(revision_id, task_id, objective, scope_json,
                          acceptance_json, verification_json, routing_json,
                          status, created_at, updated_at)
        VALUES (3, 'T009', 'swept work', '{}', '[]', '[]', '{}',
                'cancelled', '2026-10-16T03:10:00.000000+00:00',
                '2026-10-16T03:40:00.000000+00:00');
        INSERT INTO attempts(revision_id, task_id, role, profile, driver,
                             harness, model, requested_effort, started_at,
                             run_id)
        VALUES (3, 'T010', 'worker', 'host-worker', 'host', 'zcode',
                'm-worker', 'medium', '2026-10-16T03:20:00.000000+00:00',
                'R005');
        """)
        events = [
            ("T009", "waiting_host", "cancelled", "goal_cancel", None,
             "2026-10-16T03:40:00.000000+00:00"),
            ("T009", "cancelled", "cancelled", "suppression_baseline",
             '{"fingerprint": "v1"}', "2026-10-16T03:41:00.000000+00:00"),
        ]
        for task_id, from_s, to_s, event, reason, created in events:
            conn.execute(
                "INSERT INTO task_events(revision_id, task_id, from_status,"
                " to_status, event, reason, created_at)"
                " VALUES(3, ?, ?, ?, ?, ?, ?)",
                (task_id, from_s, to_s, event, reason, created),
            )
        conn.commit()

        rows = {r["goal_id"]: r for r in run(conn, queries["goal_lifecycle"])}
        # The state is goals.status verbatim, never derived from runs.status:
        # both new states sit beside a 'running' run and stay distinguishable.
        assert rows["G002"]["goal_status"] == "paused"
        assert rows["G002"]["run_status"] == "running"
        assert rows["G002"]["goal_updated_at"] > rows["G002"]["goal_created_at"]
        assert rows["G003"]["goal_status"] == "cancelling"
        assert rows["G003"]["goal_cancel_events"] == 1
        assert rows["G003"]["open_attempts"] == 1  # the unconfirmed execution
        assert rows["G002"]["open_attempts"] == 0

        # The event read: full identity, both families, reason verbatim.
        lifecycle = run(conn, queries["goal_lifecycle_events"])
        assert [(r["run_id"], r["revision"], r["task_id"], r["event"],
                 r["created_at"]) for r in lifecycle] == [
            ("R005", 1, "T009", "goal_cancel",
             "2026-10-16T03:40:00.000000+00:00"),
            ("R005", 1, "T009", "suppression_baseline",
             "2026-10-16T03:41:00.000000+00:00"),
        ]
        assert lifecycle[0]["from_status"] == "waiting_host"
        assert lifecycle[0]["to_status"] == "cancelled"
        assert lifecycle[1]["reason"] == '{"fingerprint": "v1"}'

        # On the plain pre-v13 fixture the reads stay honest: no fabricated
        # lifecycle rows, G001 reads exactly what it always read.
        plain = sqlite3.connect(":memory:")
        plain.row_factory = sqlite3.Row
        plain.executescript(SEED.read_text())
        try:
            assert [r["event"] for r in
                    run(plain, queries["goal_lifecycle_events"])] == []
        finally:
            plain.close()
    finally:
        conn.close()


def test_goal_lifecycle_reads_reject_writes_like_every_contract_query(tmp_path):
    """The lifecycle queries ride the same read-only discipline: on a
    query_only connection every documented query runs and a write is
    rejected (the global no-modify test covers the file digest; this pins
    the lifecycle pair specifically)."""
    queries = load_queries()
    db = tmp_path / "state.db"
    build_db(db)
    conn = open_readonly(db)
    try:
        rows = run(conn, queries["goal_lifecycle"])
        assert rows and rows[0]["goal_id"] == "G001"
        assert run(conn, queries["goal_lifecycle_events"]) == []
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "UPDATE goals SET status = 'cancelling' WHERE id = 'G001'")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO task_events(revision_id, task_id, to_status,"
                " event, created_at) VALUES(1, 'T001', 'cancelled',"
                " 'goal_cancel', '2026-10-16T00:00:00.000000+00:00')")
    finally:
        conn.close()


def test_contract_records_analytics_compatibility_without_granting_rights():
    """The eleventh amendment records the independent Analytics status
    (allowlist 8-12 observed, adaptations needed for 13/14/15) and keeps
    the authority boundary explicit: no scheduling, no migration."""
    flat = " ".join(CONTRACT.read_text().split())
    assert 'SUPPORTED_ORX_SCHEMA = {"8", "9", "10", "11", "12"}' in flat
    for version in ("13", "14", "15"):
        assert version in flat
    assert "no scheduling authority" in flat
    assert "no migration authority" in flat
    assert "UNFINISHED goal states" in flat
    assert "never calls `Store.open`" in flat
