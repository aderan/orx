"""Host worker session identity (G005).

A host worker is a subagent spawned by the Controller's session; zcode
exports no session id into that subagent's shell. These tests pin:

- parking a task must NOT stamp the Controller's ORX_SESSION_REF onto the
  worker attempt (it is a different session than the executor);
- `task claim --discover-session` performs the deterministic first-prompt
  lookup and stores the id only when the match is unique;
- none/ambiguous stays NULL and reports why;
- `task complete --session` / `task fail --session` late-bind a session to
  the attempt they close;
- the read-only discovery command never writes to the zcode database.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from typer.testing import CliRunner

from orx import dispatch
from orx.cli import app
from conftest import (
    active_task,
    ir_for,
    task_spec,
    write_evidence,
)

runner = CliRunner()

ZCODE_SCHEMA = """
CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT,
    project_id TEXT, title TEXT, time_created INTEGER, time_updated INTEGER);
CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,
    data TEXT, sequence INTEGER);
CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER,
    time_updated INTEGER, data TEXT, sequence INTEGER);
"""


def make_zcode_db(path, project_root: str, markers: list[tuple[str, str | None]]):
    """markers: (session_id, marker_text_or_None) — parented subagents in
    the project directory; a root session for control. Re-writable: an
    existing file is replaced wholesale."""
    if path.exists():
        path.unlink()
    now = int(time.time() * 1000)
    conn = sqlite3.connect(path)
    conn.executescript(ZCODE_SCHEMA)
    rows = [("sess_ctrl", None, project_root, "controller", now, now)]
    parts = []
    for sid, marker in markers:
        rows.append((sid, "sess_ctrl", project_root,
                     f"worker {sid}", now - 60_000, now - 30_000))
        text = (f"You are the ORX worker for assignment T001, {marker}. "
                f"Work in {project_root}") if marker else "no marker here"
        parts.append((f"p_{sid}", f"m_{sid}", sid,
                      json.dumps({"type": "text", "text": text},
                                 separators=(",", ":")), 1))
    conn.executemany("INSERT INTO session VALUES (?,?,?,?,?,?,?)",
                     [(r[0], r[1], r[2], "proj", r[3], r[4], r[5])
                      for r in rows])
    conn.executemany("INSERT INTO part VALUES (?,?,?,?,?)", parts)
    conn.commit()
    conn.close()


def park_host_task(project):
    goal = project.store.goal_active()
    if goal is None:
        goal = dispatch.create_goal(
            project, objective="session identity test",
            acceptance=["marker exists"], constraints=[], context="",
        )[0]
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=["marker exists"],
                  verification=["true"], allowed=["src/"]),
    ]))
    out = dispatch.run_slice(project)
    assert out["host_required"], out
    return out, None


def latest_attempt(project):
    # after completion the Goal may no longer be active: read the latest
    # run directly instead of going through goal_active()
    run = project.store.runs_all()[-1]
    revision = project.store.revision_active(run.id)
    return project.store.attempt_latest_for_task(revision.id, "T001")


def test_park_does_not_stamp_controller_session(project, tmp_path, monkeypatch):
    zdb = tmp_path / "zcode.sqlite"
    make_zcode_db(zdb, str(project.root), [])
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    monkeypatch.setenv("ORX_SESSION_REF", "sess_controller_env_value")
    out, _ = park_host_task(project)
    attempt = latest_attempt(project)
    # the env value belongs to the Controller, not to the worker subagent
    assert attempt.session_ref is None
    # the claim entry teaches discovery instead of the env passthrough
    assert out["host_required"][0]["claim"] == \
        "orx task claim T001 --discover-session"


def test_claim_discover_session_unique(project, tmp_path, monkeypatch):
    zdb = tmp_path / "zcode.sqlite"
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    park_host_task(project)
    # v11: the parked attempt carries a one-time nonce embedded at the top
    # of the dispatch prompt; the subagent's first text part carries it too.
    attempt = latest_attempt(project)
    assert attempt.nonce and attempt.nonce.startswith("orx-assignment:")
    make_zcode_db(zdb, str(project.root), [
        ("sess_sub_a", f"attempt 1\nORX_ASSIGNMENT={attempt.nonce}"),
        ("sess_sub_stale", "attempt 99"),   # different nonce: no match
    ])
    result = dispatch.task_claim(project, "T001", discover_session=True)
    assert result["session_ref"] == "sess_sub_a"
    assert result["session_discovery"]["decision"] == "unique"
    assert attempt.nonce in result["session_discovery"]["basis"]
    assert latest_attempt(project).session_ref == "sess_sub_a"


def test_claim_discover_nonce_ignores_stale_marker_lineage(
        project, tmp_path, monkeypatch):
    """The R006 failure mode, closed: a v11 attempt matches ONLY its nonce.
    A session whose prompt carries a stale 'attempt N' evidence lineage
    (the closed prior attempt's number) must not match — with a nonce
    present, the marker is not even consulted."""
    zdb = tmp_path / "zcode.sqlite"
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    park_host_task(project)
    attempt = latest_attempt(project)
    make_zcode_db(zdb, str(project.root), [
        # carries the live nonce AND a stale lineage reference
        ("sess_sub_a", f"attempt 1\nORX_ASSIGNMENT={attempt.nonce}"),
        # a different session carries only the stale marker text
        ("sess_sub_stale", "source attempt 1, evidence row 58"),
    ])
    result = dispatch.task_claim(project, "T001", discover_session=True)
    assert result["session_ref"] == "sess_sub_a"
    cands = result["session_discovery"]["candidates"]
    assert [c["session_id"] for c in cands] == ["sess_sub_a"]


def test_claim_discover_ambiguous_stays_null(project, tmp_path, monkeypatch):
    zdb = tmp_path / "zcode.sqlite"
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    park_host_task(project)
    attempt = latest_attempt(project)
    # two sessions both carrying the live nonce: ambiguous, no store
    make_zcode_db(zdb, str(project.root), [
        ("sess_sub_a", f"ORX_ASSIGNMENT={attempt.nonce}"),
        ("sess_sub_b", f"ORX_ASSIGNMENT={attempt.nonce}"),
    ])
    result = dispatch.task_claim(project, "T001", discover_session=True)
    assert result["session_ref"] is None
    assert result["session_discovery"]["decision"] == "ambiguous"
    assert len(result["session_discovery"]["candidates"]) == 2
    assert latest_attempt(project).session_ref is None


def test_claim_discover_none_and_missing_db(project, tmp_path, monkeypatch):
    zdb = tmp_path / "zcode.sqlite"
    make_zcode_db(zdb, str(project.root), [("sess_sub_a", None)])
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    park_host_task(project)
    result = dispatch.task_claim(project, "T001", discover_session=True)
    assert result["session_ref"] is None
    assert result["session_discovery"]["decision"] == "none"


def test_claim_discover_missing_db(project, tmp_path, monkeypatch):
    monkeypatch.setenv("ORX_ZCODE_DB", str(tmp_path / "missing.sqlite"))
    park_host_task(project)
    result = dispatch.task_claim(project, "T001", discover_session=True)
    assert result["session_ref"] is None
    assert result["session_discovery"]["decision"] == "unavailable"
    assert "missing.sqlite" in result["session_discovery"]["error"]


def test_explicit_session_wins_over_discovery(project, tmp_path, monkeypatch):
    zdb = tmp_path / "zcode.sqlite"
    make_zcode_db(zdb, str(project.root), [("sess_sub_a", "attempt 1")])
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    park_host_task(project)
    result = dispatch.task_claim(project, "T001", session="explicit-ref",
                                 discover_session=True)
    assert result["session_ref"] == "explicit-ref"
    assert result["session_discovery"] is None   # not even attempted


def test_complete_late_binds_session(project, tmp_path, monkeypatch):
    zdb = tmp_path / "zcode.sqlite"
    make_zcode_db(zdb, str(project.root), [("sess_sub_a", "attempt 1")])
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    monkeypatch.delenv("ORX_SESSION_REF", raising=False)
    park_host_task(project)
    dispatch.task_claim(project, "T001")          # no session anywhere
    dispatch.task_complete(
        project, "T001", str(write_evidence(tmp_path)),
        session="late-bound-session",
    )
    assert latest_attempt(project).session_ref == "late-bound-session"
    run = project.store.runs_all()[-1]
    revision = project.store.revision_active(run.id)
    assert project.store.task_get(revision.id, "T001").status == "passed"


def test_fail_binds_session(project, tmp_path, monkeypatch):
    monkeypatch.delenv("ORX_SESSION_REF", raising=False)
    park_host_task(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_fail(project, "T001", "worker gave up",
                       session="failing-session")
    attempt = latest_attempt(project)
    assert attempt.session_ref == "failing-session"
    assert attempt.result == "failed"


def test_discovery_never_writes_zcode_db(project, tmp_path, monkeypatch):
    import hashlib
    zdb = tmp_path / "zcode.sqlite"
    make_zcode_db(zdb, str(project.root), [("sess_sub_a", "attempt 1")])
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    before = hashlib.sha256(zdb.read_bytes()).hexdigest()
    from orx import zcode_sessions
    res = zcode_sessions.discover_attempt_session(1, project.root)
    assert res["decision"] == "unique"
    assert res["candidates"][0]["session_id"] == "sess_sub_a"
    after = hashlib.sha256(zdb.read_bytes()).hexdigest()
    assert before == after


def test_cli_session_discover_exit_codes(project, tmp_path, monkeypatch):
    zdb = tmp_path / "zcode.sqlite"
    make_zcode_db(zdb, str(project.root), [
        ("sess_sub_a", "attempt 1"), ("sess_sub_b", "attempt 1")])
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    r = runner.invoke(app, ["task", "session-discover", "1"])
    assert r.exit_code == 2      # ambiguous
    make_zcode_db(zdb, str(project.root), [("sess_sub_a", "attempt 1")])
    r = runner.invoke(app, ["task", "session-discover", "1"])
    assert r.exit_code == 0 and "sess_sub_a" in r.output
    r = runner.invoke(app, ["task", "session-discover", "77"])
    assert r.exit_code == 1      # none
    monkeypatch.setenv("ORX_ZCODE_DB", str(tmp_path / "missing.sqlite"))
    r = runner.invoke(app, ["task", "session-discover", "1"])
    assert r.exit_code == 3      # unavailable


# ---------------------------------------------------------------------------
# G010 T001: the orx-analytics G002/R002 breakpoint, reproduced from the
# sanitized real structure (tests/fixtures/g010/provenance.json).
#
# Proven facts behind these tests (read-only on 2026-10-16): the run's 18
# host attempts all ended with session_ref NULL and usage_missing_reason
# host_session_unbound while the zcode database held 1,222 model_usage rows
# (167,384,405 input tokens) across the 16 real subagent sessions. Each
# session's FIRST text part carries its attempt's nonce verbatim; what fails
# is the directory predicate — real subagents inherit the CONTROLLER
# window's directory (/Users/flb/Sources/Products/ORX), not the ORX project
# root of the run being controlled (/Users/flb/Sources/Tools/orx-analytics).

G010_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "g010"
ARCHIVED_WORKER_PROMPT = (G010_FIXTURES / "archived-worker-prompt.md").read_text()
# The nonce ORX really minted for analytics attempt 15 (worker, T002); the
# archived prompt carries it at the top and the fixture keeps both verbatim.
WORKER_NONCE = "orx-assignment:520449c9-32b3-4f60-8cb7-9589bb29e539"
# Synthetic spelling of the controller window's directory: deliberately NOT
# the project root, exactly like the real evidence.
CONTROLLER_WINDOW_DIR = "/users/sample/controller-window"
WORKER_SESSION_ID = "sess_subagent_worker_replay"


def make_g010_discovery_db(path, *, directory=CONTROLLER_WINDOW_DIR,
                           subagents=(), root_first_text=None):
    """The R002 session shape: one controller ROOT session (parent NULL) and
    parented subagents, ALL carrying the controller window's directory. The
    ORX project root differs from `directory` by construction (the caller
    passes the tmp project). `subagents`: [(session_id, first_text)]."""
    if path.exists():
        path.unlink()
    now = int(time.time() * 1000)
    conn = sqlite3.connect(path)
    conn.executescript(ZCODE_SCHEMA)
    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
        ("sess_ctrl_g010", None, directory, "proj", "controller", now, now))
    if root_first_text is not None:
        conn.execute(
            "INSERT INTO part VALUES (?,?,?,?,?)",
            ("p_root", "m_root", "sess_ctrl_g010",
             json.dumps({"type": "text", "text": root_first_text},
                        separators=(",", ":")), 1))
    for sid, text in subagents:
        conn.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
            (sid, "sess_ctrl_g010", directory, "proj",
             f"subagent {sid}", now - 60_000, now - 30_000))
        conn.execute(
            "INSERT INTO part VALUES (?,?,?,?,?)",
            (f"p_{sid}", f"m_{sid}", sid,
             json.dumps({"type": "text", "text": text},
                        separators=(",", ":")), 1))
    conn.commit()
    conn.close()


def test_nonce_discovery_binds_across_controller_window_directory(
        project, tmp_path):
    """THE breakpoint: a subagent session whose first text part carries the
    attempt's exact nonce, but whose directory is the controller window's —
    not this project's root. The nonce is a one-time ORX-minted token: an
    exact first-part match IS the dispatched assignment wherever the
    controller's window lived. Discovery must bind it and RECORD the
    directory fact instead of silently filtering on it."""
    from orx import zcode_sessions
    zdb = tmp_path / "g010.sqlite"
    make_g010_discovery_db(
        zdb, subagents=[(WORKER_SESSION_ID, ARCHIVED_WORKER_PROMPT)])
    res = zcode_sessions.discover_attempt_session(
        15, project.root, db=str(zdb), nonce=WORKER_NONCE)
    assert res["decision"] == "unique", res
    assert res["candidates"][0]["session_id"] == WORKER_SESSION_ID
    assert res["candidates"][0]["directory_matched"] is False
    assert zcode_sessions.first_session_id(res) == WORKER_SESSION_ID


def test_nonce_ambiguity_across_directories_stays_null(project, tmp_path):
    """Directory independence must not become a guess: two sessions carrying
    the live nonce (one in the project directory, one in the controller
    window's) is ambiguous, and ambiguity still means no binding."""
    from orx import zcode_sessions
    zdb = tmp_path / "g010.sqlite"
    make_g010_discovery_db(
        zdb, subagents=[("sess_local_nonce", f"ORX_ASSIGNMENT={WORKER_NONCE}")])
    # a second nonce-carrying session, this time recorded in the project dir
    now = int(time.time() * 1000)
    conn = sqlite3.connect(zdb)
    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
        ("sess_foreign_nonce", "sess_ctrl_g010", str(project.root.resolve()),
         "proj", "second copy", now - 60_000, now - 30_000))
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        ("p_sess_foreign_nonce", "m_f", "sess_foreign_nonce",
         json.dumps({"type": "text",
                     "text": f"prompt copy\nORX_ASSIGNMENT={WORKER_NONCE}"},
                    separators=(",", ":")), 1))
    conn.commit()
    conn.close()
    res = zcode_sessions.discover_attempt_session(
        15, project.root, db=str(zdb), nonce=WORKER_NONCE)
    assert res["decision"] == "ambiguous"
    assert len(res["candidates"]) == 2
    assert zcode_sessions.first_session_id(res) is None


def test_controller_root_session_first_part_nonce_is_never_a_candidate(
        project, tmp_path):
    """不误用 controller 根会话: the controller's OWN session may carry the
    nonce in its first text part (it composed or echoed the assignment), but
    it is a ROOT (parent NULL) — never a subagent, never bound."""
    from orx import zcode_sessions
    zdb = tmp_path / "g010.sqlite"
    make_g010_discovery_db(zdb, subagents=[],
                           root_first_text=ARCHIVED_WORKER_PROMPT)
    res = zcode_sessions.discover_attempt_session(
        15, project.root, db=str(zdb), nonce=WORKER_NONCE)
    assert res["decision"] == "none"
    assert res["candidates"] == []


def test_legacy_marker_still_requires_directory(project, tmp_path):
    """Marker discovery keeps the strict directory predicate: a human-readable
    'attempt N' is weak evidence and the directory is its disambiguator. Only
    the one-time nonce is strong enough to bind across directories."""
    from orx import zcode_sessions
    zdb = tmp_path / "g010.sqlite"
    make_g010_discovery_db(
        zdb, subagents=[("sess_legacy_marker", "You work on attempt 15 now")])
    res = zcode_sessions.discover_attempt_session(
        15, project.root, db=str(zdb))
    assert res["decision"] == "none"
    assert res["directory_filter"] == "required"


def test_quasi_real_worker_loop_with_archived_prompt(project, tmp_path,
                                                     monkeypatch):
    """Zero-cost quasi-real closed loop (worker side): the archived ORIGINAL
    prompt from orx-analytics R002 (attempt 15) is the session's first text
    part, the attempt is parked under the same nonce ORX minted for that
    archived assignment, discovery is the REAL discover_attempt_session, and
    the closure is the REAL task_complete autobill. Chain:
    run/revision/task/attempt/nonce/session/usage."""
    from test_host_progress_e2e import make_zcode_usage_db
    zdb = tmp_path / "g010-usage.sqlite"
    # Real R002 numbers for this session (provenance.json): 47 requests,
    # 6,854,455 input / 59,456 output / 6,668,096 cache-read — represented
    # here as two deterministic request rows with one duplicate row dropped.
    make_zcode_usage_db(
        zdb, CONTROLLER_WINDOW_DIR,
        sessions=[(WORKER_SESSION_ID, None, ARCHIVED_WORKER_PROMPT)],
        usages=[
            ("r1", "lr1", 0, WORKER_SESSION_ID, "completed",
             6000000, 50000, 5800000, 6050000),
            ("r2", "lr2", 0, WORKER_SESSION_ID, "completed",
             854455, 9456, 868096, 863911),
            ("r1-dup", "lr1", 0, WORKER_SESSION_ID, "completed",
             6000000, 50000, 5800000, 6050000),
        ],
    )
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    monkeypatch.delenv("ORX_SESSION_REF", raising=False)
    # Deterministic replay of the archived assignment's identity: the park
    # mints "the" nonce the archived prompt carries (the real one from
    # 2026-10-16 07:44), so prompt, attempt row, and session all agree.
    monkeypatch.setattr(dispatch, "mint_nonce", lambda: WORKER_NONCE)
    park_host_task(project)
    attempt = latest_attempt(project)
    assert attempt.nonce == WORKER_NONCE
    assert attempt.session_ref is None          # nothing stamped by the park

    claimed = dispatch.task_claim(project, "T001", discover_session=True)
    assert claimed["session_ref"] == WORKER_SESSION_ID
    assert claimed["session_discovery"]["decision"] == "unique"

    dispatch.task_complete(
        project, "T001", str(write_evidence(tmp_path)), attempt_id=attempt.id)
    closed = latest_attempt(project)
    assert closed.session_ref == WORKER_SESSION_ID
    assert closed.usage_missing_reason is None
    rows = [r for r in project.store.usage_rows()
            if r["attempt_id"] == attempt.id]
    assert len(rows) == 1 and rows[0]["source"] == "zcode_db"
    assert (rows[0]["input_tokens"], rows[0]["output_tokens"],
            rows[0]["cached_input_tokens"]) == (6854455, 59456, 6668096)
    run = project.store.runs_all()[-1]
    revision = project.store.revision_active(run.id)
    assert project.store.task_get(revision.id, "T001").status == "passed"
