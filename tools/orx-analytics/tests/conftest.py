"""Synthetic source fixtures shaped exactly like the real stores.

These builders encode the field semantics verified against the live
databases (2026-10-06, docs/source-probes.md). Tests for importers,
correlation, metrics, and bundles all build on them, so behavior is
asserted without touching real data.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


# ---------------------------------------------------------------- zcode ---

ZCODE_SCHEMA = """
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
CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER,
    time_updated INTEGER, data TEXT, sequence INTEGER);
"""

MS = 1000


def ts(s: int) -> int:
    """Epoch ms for a fixed synthetic timeline starting 2026-10-04T12:00Z."""
    return 1791115200000 + s * MS


def make_zcode_db(path: Path, project_a: str, project_b: str) -> dict:
    """A controller + 2 worker subagents in project A, 1 session in B."""
    conn = sqlite3.connect(path)
    conn.executescript(ZCODE_SCHEMA)
    ctrl = "sess_ctrl-0001"
    w1 = "sess_subagent_agent_w1"
    w2 = "sess_subagent_agent_w2"
    other = "sess_other-project-0001"
    sessions = [
        (ctrl, None, project_a, "proj_a", "controller", ts(0), ts(600)),
        # subagent sessions are created moments BEFORE the worker claims
        # (measured 4-11 s on six R006 host workers): w1 precedes attempt
        # 201's span T(10)..T(19), w2 precedes attempt 202's T(20)..T(28)
        (w1, ctrl, project_a, "proj_a",
         "You are the ORX worker for assignment T001, attempt 201. W…",
         ts(9), ts(200)),
        (w2, ctrl, project_a, "proj_a",
         "You are the ORX worker for assignment T002 (revision 3), "
         "attempt 202. W…", ts(19), ts(220)),
        (other, None, project_b, "proj_b", "unrelated work", ts(50), ts(90)),
    ]
    conn.executemany("INSERT INTO session VALUES (?,?,?,?,?,?,?)", sessions)
    turns = [
        # controller: 2 turns; w1/w2: 1 turn each; other: 1 turn
        (ctrl, "turn_c1", "completed", ts(10), ts(50), 40000, 14, 0, 8, 0,
         1000000, 20000, 0, 900000, 0),
        (ctrl, "turn_c2", "completed", ts(100), ts(590), 490000, 44, 0, 30, 1,
         3000000, 40000, 0, 2800000, 0),
        (w1, "turn_w1", "completed", ts(65), ts(195), 130000, 43, 0, 25, 0,
         5000000, 30000, 0, 4700000, 0),
        (w2, "turn_w2", "completed", ts(75), ts(215), 140000, 49, 1, 27, 2,
         6000000, 35000, 0, 5600000, 0),
        (other, "turn_o", "completed", ts(55), ts(85), 30000, 5, 0, 3, 0,
         700000, 8000, 0, 600000, 0),
    ]
    conn.executemany("INSERT INTO turn_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     turns)
    # request-level usage mirrors the turn numbers where both exist; a row
    # repeats the SAME request identity (logical_request_id + attempt_index
    # + session) once to prove the mirror dedupes by natural key, an error
    # retry carries explicit zero placeholders, and a running row is still
    # settling (never terminal). Statuses use zcode's real vocabulary
    # (completed / error / cancelled / running).
    requests = [
        ("req_c1", 0, ctrl, "turn_c1", "p1", "model-x", "completed",
         ts(11), ts(12), 1000000, 20000, 0, 900000, 0, 1020000, 1020000),
        ("req_c2", 0, ctrl, "turn_c2", "p1", "model-x", "completed",
         ts(101), ts(102), 3000000, 40000, 0, 2800000, 0, 3040000, 3040000),
        # w2 shows one logical request retried: attempt 0 errored with the
        # provider's explicit zero placeholder (flagged, never "free"), 1 ok
        ("req_w2", 0, w2, "turn_w2", "p1", "model-x", "error",
         ts(76), ts(77), 0, 0, 0, 0, 0, 0, 0),
        ("req_w2", 1, w2, "turn_w2", "p1", "model-x", "completed",
         ts(78), ts(79), 6000000, 35000, 0, 5600000, 0, 6035000, 6035000),
        ("req_w1", 0, w1, "turn_w1", "p1", "model-x", "completed",
         ts(66), ts(67), 5000000, 30000, 0, 4700000, 0, 5030000, 5030000),
        # duplicate record of the SAME request (identical natural key and
        # values): the mirror counts it once
        ("req_w1", 0, w1, "turn_w1", "p1", "model-x", "completed",
         ts(66), ts(67), 5000000, 30000, 0, 4700000, 0, 5030000, 5030000),
        ("req_o", 0, other, "turn_o", "p1", "model-y", "completed",
         ts(56), ts(57), 700000, 8000, 0, 600000, 0, 708000, 708000),
        # still streaming: non-terminal status, numbers provisional
        ("req_o2", 0, other, "turn_o", "p1", "model-y", "running",
         ts(58), None, 500, 0, 0, 0, 0, 500, 500),
    ]
    conn.executemany(
        "INSERT INTO model_usage VALUES ("
        + ",".join("?" for _ in range(16)) + ")", requests)
    tools = [
        ("t1", w1, "turn_w1", "Bash", "completed", ts(66), ts(68), 2000, 0),
        ("t2", w1, "turn_w1", "Read", "completed", ts(70), ts(71), 1000, None),
        ("t3", w2, "turn_w2", "Bash", "error", ts(80), ts(82), 2000, 1),
        ("t4", ctrl, "turn_c1", "Bash", "completed", ts(12), ts(14), 2000, 0),
    ]
    conn.executemany("INSERT INTO tool_usage VALUES (?,?,?,?,?,?,?,?,?)", tools)
    parts = [
        # first text parts carry the attempt markers used for correlation
        ("p_ctrl_1", "m1", ctrl,
         json.dumps({"type": "text", "text": "run the goal please"}, separators=(",", ":")), 1),
        ("p_w1_1", "m2", w1,
         json.dumps({"type": "text", "text": "You are the ORX worker for assignment T001, attempt 201. Work in " + project_a}, separators=(",", ":")), 1),
        ("p_w2_1", "m3", w2,
         json.dumps({"type": "text", "text": "You are the ORX worker for assignment T002 (revision 3), attempt 202. Work in " + project_a}, separators=(",", ":")),
         1),
        ("p_o_1", "m4", other,
         json.dumps({"type": "text", "text": "unrelated"}, separators=(",", ":")), 1),
    ]
    conn.executemany("INSERT INTO part VALUES (?,?,?,?,?)", parts)
    # step-finish parts mirroring turn_usage (cross-check caliber)
    for sid, turn, i, o, cr in ((ctrl, "turn_c1", 1000000, 20000, 900000),
                                (w1, "turn_w1", 5000000, 30000, 4700000)):
        conn.execute(
            "INSERT INTO part VALUES (?,?,?,?,?)",
            (f"p_{turn}_sf", "m_x", sid,
             json.dumps({"type": "step-finish", "reason": "tool-calls",
                         "tokens": {"total": i + o, "input": i, "output": o,
                                    "reasoning": 0,
                                    "cache": {"read": cr, "write": 0}}}), 5))
    conn.commit()
    conn.close()
    return {"controller": ctrl, "w1": w1, "w2": w2, "other": other}


# ------------------------------------------------------------------ orx ---

def make_orx_db(path: Path) -> dict:
    """A v9-shaped ORX state.db exercising the tricky reads.

    - planner attempt with NULL revision but run_id set (must be counted)
    - T001 on two revisions (two different tasks, same short id)
    - attempt 201: two usage rows (native_cli x2 -> canonical lowest id)
    - attempt 202: native_cli + host_report (same work seen twice)
    - attempt 203: no observation, usage_missing_reason recorded
    - verifications: earliest-pass + later fail (round failed after green
      gate), retry attempt green again
    - a task never verified (outside first-pass denominator)
    - replan tables: rev2 T003 declared redo of rev1 T003 (the ONLY edge)
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version','9');
        CREATE TABLE goals(id TEXT PRIMARY KEY, objective TEXT,
            constraints_json TEXT, acceptance_json TEXT, context TEXT,
            status TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE runs(id TEXT PRIMARY KEY, goal_id TEXT, status TEXT,
            created_at TEXT, updated_at TEXT, started_at TEXT,
            completed_at TEXT);
        CREATE TABLE plan_revisions(id INTEGER PRIMARY KEY, run_id TEXT,
            revision INTEGER, depth INTEGER, planner_profile TEXT,
            status TEXT, created_at TEXT);
        CREATE TABLE tasks(id INTEGER PRIMARY KEY, revision_id INTEGER,
            task_id TEXT, objective TEXT, scope_json TEXT,
            acceptance_json TEXT, verification_json TEXT,
            preread_json TEXT, routing_json TEXT, status TEXT,
            failure_reason TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE task_events(id INTEGER PRIMARY KEY, revision_id INTEGER,
            task_id TEXT, from_status TEXT, to_status TEXT, event TEXT,
            reason TEXT, created_at TEXT);
        CREATE TABLE attempts(id INTEGER PRIMARY KEY, revision_id INTEGER,
            task_id TEXT, assignment_id TEXT, role TEXT, profile TEXT,
            driver TEXT, harness TEXT, model TEXT, requested_effort TEXT,
            actual_effort TEXT, effort_source TEXT, fallback_used INTEGER,
            routing_reason TEXT, started_at TEXT, ended_at TEXT,
            result TEXT, failure_reason TEXT, isolation TEXT,
            session_ref TEXT, run_id TEXT, usage_missing_reason TEXT,
            actual_model TEXT, model_source TEXT);
        CREATE TABLE usage_observations(id INTEGER PRIMARY KEY,
            attempt_id INTEGER, profile TEXT, run_id TEXT, task_id TEXT,
            input_tokens INTEGER, output_tokens INTEGER,
            cached_input_tokens INTEGER, source TEXT, accuracy TEXT,
            created_at TEXT);
        CREATE TABLE verifications(id INTEGER PRIMARY KEY,
            revision_id INTEGER, task_id TEXT, attempt_id INTEGER,
            kind TEXT, command TEXT, required_capabilities_json TEXT,
            exit_code INTEGER, passed INTEGER, output_path TEXT,
            created_at TEXT);
        CREATE TABLE evidence(id INTEGER PRIMARY KEY, attempt_id INTEGER,
            kind TEXT, path TEXT, created_at TEXT);
        CREATE TABLE planning_assignments(id TEXT PRIMARY KEY, run_id TEXT,
            profile TEXT, depth TEXT, status TEXT, prompt TEXT,
            created_at TEXT, submitted_at TEXT);
        CREATE TABLE replan_mappings(id INTEGER PRIMARY KEY, run_id TEXT,
            revision_id INTEGER UNIQUE, prior_revision INTEGER,
            created_at TEXT);
        CREATE TABLE replan_task_mappings(id INTEGER PRIMARY KEY,
            mapping_id INTEGER, run_id TEXT, revision_id INTEGER,
            task_id TEXT, classification TEXT, redo_reason TEXT,
            confirm_verification_json TEXT, artifacts_json TEXT,
            created_at TEXT);
        CREATE TABLE replan_sources(id INTEGER PRIMARY KEY,
            task_mapping_id INTEGER, run_id TEXT, source_revision INTEGER,
            source_task_id TEXT, source_task_row_id INTEGER, part INTEGER,
            created_at TEXT);
        CREATE TABLE replan_superseded(id INTEGER PRIMARY KEY,
            mapping_id INTEGER, run_id TEXT, source_revision INTEGER,
            source_task_id TEXT, source_task_row_id INTEGER,
            disposition TEXT, successors_json TEXT, note TEXT,
            created_at TEXT);
        """
    )
    from datetime import datetime, timedelta, timezone as _tz
    _T0 = datetime(2026, 10, 4, 12, 0, 0, tzinfo=_tz.utc)

    def T(sec: int) -> str:
        return (_T0 + timedelta(seconds=sec)).isoformat()
    conn.execute("INSERT INTO goals VALUES ('G001','goal text','[]','[]','','"
                 "active',?,?)", (T(0), T(0)))
    conn.execute("INSERT INTO runs VALUES ('R001','G001','done',?,?,?,?)",
                 (T(0), T(90), T(1), T(80)))
    # rev1: T001(passed after retry), T002(never verified -> outside rate)
    # rev2: T001(new task, passed), T003(redo of rev1 T003? no—rev1 has no
    #       T003; use rev1 T001? The redo edge must point at a real prior
    #       task; make rev1 T003 too for the redo case.)
    conn.executemany("INSERT INTO plan_revisions VALUES (?,?,?,?,?,?,?)", [
        (11, "R001", 1, 2, "planner-a", "superseded", T(2)),
        (12, "R001", 2, 2, "planner-a", "active", T(30)),
    ])
    tasks = [
        (101, 11, "T001", "rev1 gate work", "[]", "[]", "[]", "[]", "{}",
         "failed", "verification failed: red", T(3), T(20)),
        (102, 11, "T002", "rev1 never verified", "[]", "[]", "[]", "[]", "{}",
         "cancelled", None, T(3), T(25)),
        (103, 11, "T003", "rev1 old thing", "[]", "[]", "[]", "[]", "{}",
         "passed", None, T(3), T(26)),
        (111, 12, "T001", "rev2 fresh task same short id", "[]", "[]", "[]",
         "[]", "{}", "passed", None, T(31), T(60)),
        (113, 12, "T003", "rev2 redo of rev1 T003", "[]", "[]", "[]", "[]",
         "{}", "passed", None, T(31), T(70)),
    ]
    conn.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     tasks)
    attempts = [
        # 200: planner, NULL revision, run attribution ONLY via run_id
        (200, None, None, "P001", "planner", "planner-a", "cli", "codex",
         "m1", "high", None, None, 0, "primary", T(1), T(9),
         "completed", None, None, "01aa-planner-thread", "R001", None,
         "codex-frontier", "reported"),
        # 201: rev1 T001 worker, failed round; two native_cli rows
        (201, 11, "T001", None, "worker", "zcode-worker", "host", "zcode",
         "m2", "max", None, None, 0, "primary", T(10), T(19),
         "failed", "verification failed: red", "prompt_only", None, "R001",
         None, None, None),
        # 202: rev1 T001 retry, passed; native_cli + host_report dup
        (202, 11, "T001", None, "worker", "zcode-worker", "host", "zcode",
         "m2", "max", None, None, 0, "primary", T(20), T(28),
         "completed", None, "prompt_only", None, "R001", None, None, None),
        # 203: rev2 T001 worker, no observation, reason recorded
        (203, 12, "T001", None, "worker", "cursor-strong", "cli", "cursor",
         "m3", "high", None, None, 0, "fallback", T(31), T(59),
         "completed", None, None, None, "R001", "harness_omitted", None,
         None),
        # 204: rev2 T003 worker, with STORED session ref (authoritative)
        (204, 12, "T003", None, "worker", "zcode-worker", "host", "zcode",
         "m2", "max", None, None, 0, "primary", T(32), T(69),
         "completed", None, "prompt_only", "sess_subagent_agent_w1", "R001",
         None, None, None),
    ]
    conn.executemany("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?"
                     ",?,?,?,?,?,?,?,?,?,?)", attempts)
    usage = [
        (300, 200, "planner-a", "R001", None, 398699, 10755, 322176,
         "native_cli", "exact", T(9)),
        (301, 201, "zcode-worker", "R001", "T001", 1000, 100, 500,
         "native_cli", "exact", T(19)),
        (302, 201, "zcode-worker", "R001", "T001", 1100, 110, 550,
         "native_cli", "exact", T(19)),
        (303, 202, "zcode-worker", "R001", "T001", 5000, 500, 4000,
         "native_cli", "exact", T(28)),
        (304, 202, "zcode-worker", "R001", "T001", 5000, 500, 4000,
         "host_report", "estimated", T(29)),
    ]
    conn.executemany("INSERT INTO usage_observations VALUES (?,?,?,?,?,?,?,?,?,"
                     "?,?)", usage)
    verifs = [
        # rev1 T001 attempt 201: green gate then agent fail (round failed)
        (400, 11, "T001", 201, "command", "pytest -q", 0, 1, None,
         T(18)),
        (401, 11, "T001", 201, "agent", "verifier verdict", None, 0, None,
         T(19)),
        # retry attempt 202: green
        (402, 11, "T001", 202, "command", "pytest -q", 0, 1, None,
         T(27)),
        (403, 11, "T001", 202, "agent", "verifier verdict", None, 1, None,
         T(28)),
        # rev2 T001 (a DIFFERENT task): green first
        (404, 12, "T001", 203, "command", "pytest -q", 0, 1, None,
         T(58)),
        # rev2 T003: green first
        (405, 12, "T003", 204, "command", "pytest -q", 0, 1, None,
         T(68)),
        # rev1 T003: green (so rev1 T003 was passing already — the redo
        # edge below is about scope change, not waste)
        (406, 11, "T003", None, "command", "pytest -q", 0, 1, None,
         T(26)),
    ]
    conn.executemany(
        "INSERT INTO verifications(id, revision_id, task_id, attempt_id, "
        "kind, command, exit_code, passed, output_path, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)", verifs)
    events = [
        (500, 11, "T001", "verifying", "failed", "verify_fail",
         "verification failed: agent verdict red", T(19)),
        (501, 11, "T001", "failed", "runnable", "retry", "retry round 2",
         T(20)),
        (502, 11, "T002", "runnable", "cancelled", "cancel", None,
         T(25)),
    ]
    conn.executemany("INSERT INTO task_events VALUES (?,?,?,?,?,?,?,?)", events)
    conn.execute("INSERT INTO evidence VALUES (900,202,'completion',"
                 "'evidence.json',?)", (T(28),))
    # replan: rev2 declared T003 redo of rev1 T003 (task row 103)
    conn.execute("INSERT INTO replan_mappings VALUES (1,'R001',12,1,?)",
                 (T(30),))
    conn.execute("INSERT INTO replan_task_mappings VALUES (1,1,'R001',12,"
                 "'T003','redo','scope widened',NULL,'[]',?)",
                 (T(30),))
    conn.execute("INSERT INTO replan_sources VALUES (1,1,'R001',1,'T003',103,"
                 "1,?)", (T(30),))
    conn.execute("INSERT INTO replan_superseded VALUES (1,1,'R001',1,'T003',"
                 "103,'redone','[\"T003\"]',NULL,?)", (T(30),))
    conn.commit()
    conn.close()
    return {"attempts": [a[0] for a in attempts]}


# ---------------------------------------------------------------- codex ---

def make_codex_rollout(path: Path, cwd: str, thread: str = "01aa-cx-1") -> None:
    lines = [
        {"timestamp": "2026-10-04T12:00:00.000Z", "ordinal": 0,
         "type": "session_meta",
         "payload": {"session_id": thread, "id": thread, "cwd": cwd}},
        {"timestamp": "2026-10-04T12:00:01.000Z", "ordinal": 1,
         "type": "turn_context",
         "payload": {"turn_id": "turn1", "cwd": cwd, "model": "gpt-x",
                     "effort": "high"}},
        {"timestamp": "2026-10-04T12:00:02.000Z", "ordinal": 2,
         "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn1",
                     "started_at": 1791115201}},
        {"timestamp": "2026-10-04T12:00:03.000Z", "ordinal": 3,
         "type": "token_usage_record",
         "payload": {"thread_id": thread, "turn_id": "turn1",
                     "response_id": "resp_1",
                     "usage": {"input_tokens": 1000,
                               "cached_input_tokens": 800,
                               "cache_write_input_tokens": 0,
                               "output_tokens": 100,
                               "reasoning_output_tokens": 0,
                               "total_tokens": 1100}}},
        {"timestamp": "2026-10-04T12:00:04.000Z", "ordinal": 4,
         "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn1",
                     "completed_at": 1791115204}},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for j in lines:
            fh.write(json.dumps(j) + "\n")


# ---------------------------------------------------------------- cursor ---

def make_cursor_store(root: Path, folder_uri: str,
                      workspace_id: str = "ws0001") -> None:
    """globalStorage/state.vscdb + workspaceStorage/<id>/workspace.json."""
    gs = root / "user" / "globalStorage"
    gs.mkdir(parents=True, exist_ok=True)
    ws = root / "user" / "workspaceStorage" / workspace_id
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "workspace.json").write_text(json.dumps({"folder": folder_uri}))
    conn = sqlite3.connect(gs / "state.vscdb")
    conn.executescript(
        """
        CREATE TABLE ItemTable (key TEXT, value BLOB);
        CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB);
        CREATE TABLE composerHeaders (composerId TEXT, workspaceId TEXT,
            createdAt TEXT, lastUpdatedAt TEXT, isArchived TEXT,
            isSubagent TEXT, recency TEXT, checkpointAt TEXT, value BLOB,
            subagentTypeName TEXT);
        """
    )
    usage = {"total": {"input": 2000, "output": 200, "cacheRead": 1500}}
    conn.execute("INSERT INTO composerHeaders VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("comp-1", workspace_id, "1791115200000",
                  "1791115300000", "0", "0", "1791115300000", "None",
                  json.dumps({"type": "head", "composerId": "comp-1"}), ""))
    conn.execute("INSERT INTO cursorDiskKV VALUES (?,?)",
                 ("composerData:comp-1",
                  json.dumps({"composerId": "comp-1", "name": "cursor task",
                              "createdAt": 1791115200000,
                              "usageData": usage,
                              "modelConfig": {"model": "opus"}})))
    conn.commit()
    conn.close()


@pytest.fixture()
def world(tmp_path):
    """A full synthetic multi-source world with its own analytics store."""
    from orxanalytics.store import Store

    proj_a = tmp_path / "ProjA"
    proj_a.mkdir()
    proj_b = tmp_path / "ProjB"
    proj_b.mkdir()
    zcode_db = tmp_path / "zcode-db.sqlite"
    zc = make_zcode_db(zcode_db, str(proj_a), str(proj_b))
    orx_db = proj_a / ".orx"
    orx_db.mkdir()
    make_orx_db(orx_db / "state.db")
    codex_dir = tmp_path / "codex-sessions" / "2026" / "10" / "04"
    codex_dir.mkdir(parents=True)
    make_codex_rollout(codex_dir / "rollout-test.jsonl", str(proj_a))
    cursor_root = tmp_path / "cursor-home"
    make_cursor_store(cursor_root, "file://" + str(proj_a))
    store = Store(tmp_path / "analytics-home")
    return {
        "tmp": tmp_path, "proj_a": str(proj_a), "proj_b": str(proj_b),
        "zcode_db": zcode_db, "zcode": zc,
        "codex_dir": tmp_path / "codex-sessions",
        "cursor_db": cursor_root / "user" / "globalStorage" / "state.vscdb",
        "store": store,
    }


@pytest.fixture(autouse=True)
def _notifications_off_by_default(monkeypatch):
    """Tests must never pop real mac notifications; the notify path is
    exercised with subprocess mocked and the env re-enabled explicitly."""
    monkeypatch.setenv("ORX_ANALYTICS_NOTIFY", "0")


@pytest.fixture(autouse=True)
def _isolate_orx_index(monkeypatch, tmp_path):
    """Discovery's default index location must never read the developer's
    real ORX projects-index.json; resolve it into this test's tmp dir."""
    monkeypatch.setenv("ORX_DATA_DIR", str(tmp_path / "orx-data"))


@pytest.fixture(autouse=True)
def _isolate_default_collection(monkeypatch, tmp_path):
    """A watch round must not import the developer's real session history.

    Direct importer tests supply their synthetic sources explicitly. The
    watch loop's default collect path sees absent sources in this test's
    temporary directory, exercising honest error reporting on every host.
    """
    from functools import partial
    from orxanalytics import collect, importers

    monkeypatch.setattr(collect, "import_zcode", partial(
        importers.import_zcode, db=tmp_path / "absent-zcode.db"))
    monkeypatch.setattr(collect, "import_codex", partial(
        importers.import_codex, sessions_dir=tmp_path / "absent-codex"))
    monkeypatch.setattr(collect, "import_cursor", partial(
        importers.import_cursor, db=tmp_path / "absent-cursor.db"))
