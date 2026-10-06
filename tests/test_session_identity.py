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

from typer.testing import CliRunner

from orx import dispatch
from orx.cli import app
from tests.conftest import (
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
    make_zcode_db(zdb, str(project.root), [
        ("sess_sub_a", "attempt 1"),
        ("sess_sub_stale", "attempt 99"),   # different attempt: no match
    ])
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    park_host_task(project)
    result = dispatch.task_claim(project, "T001", discover_session=True)
    assert result["session_ref"] == "sess_sub_a"
    assert result["session_discovery"]["decision"] == "unique"
    assert "attempt 1" in result["session_discovery"]["basis"]
    assert latest_attempt(project).session_ref == "sess_sub_a"


def test_claim_discover_ambiguous_stays_null(project, tmp_path, monkeypatch):
    zdb = tmp_path / "zcode.sqlite"
    make_zcode_db(zdb, str(project.root), [
        ("sess_sub_a", "attempt 1"),
        ("sess_sub_b", "attempt 1"),
    ])
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    park_host_task(project)
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
