"""Host failure facts, trusted observation, and the safe recovery entry
(G009 T006, the a130 incident 2026-10-07).

The incident, replayed deterministically: a host worker claimed its task,
worked for about an hour (114 model requests, 35 Edits, 3 Writes), hit the
GLM 1308 quota failure at 02:05 — the same provider account the CONTROLLER
was failing on — and ORX only closed the attempt at 08:30, after the user
returned, recording a failure reason that was flatly WRONG ("died at spawn /
never claimed / no workspace changes") although claim events and real edits
existed. The wall-clock span of a host attempt is not model runtime.

These tests pin the four seams that close that gap, all deterministic
(frozen ORX clock seams, a fake zcode session database, no paid models):

1. ``task observe`` — the independently callable, read-only diagnostic:
   terminal facts (session created / last activity / archived, edit and
   write counts, model steps) bound to session/attempt/nonce with a source
   and a timestamp on every fact, claim facts preserved, and the three
   clocks (real failure time, observation time, closure time) kept distinct.
   It renders NO verdict: heartbeat silence stays an observation, and
   session activity newer than the last report is a fact, not a judgement.
2. ``task recover`` — the explicit operator-invoked recovery. It requires a
   confirmed termination fact, closes the terminated attempt (single
   writer), and records the machine-read facts next to the operator's fact
   so the recorded reason can never again claim "never claimed / no
   changes" when claim and changes exist. Old-attempt deliveries stay stale
   after it; the fresh attempt carries a new nonce.
3. ``task note`` — the append-only operator channel: the shared-failure
   parking fact (controller and worker down on one quota domain; state
   stays explainably parked, nothing auto-continues) and corrections of
   misreported history, which APPEND a record quoting the original and
   never edit it.
4. The controller skill teaches the confirmed-termination-fact discipline.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orx import dispatch
from orx.cli import app
from orx.records import ConflictError, NotFoundError, ORXError

from conftest import (
    HOST_CONFIG_TOML,
    active_task,
    ir_for,
    make_project,
    task_spec,
    write_evidence,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()

PROGRESS_TIMEOUT_ONE_MIN = HOST_CONFIG_TOML.replace(
    '[worker]\nprofiles = ["host-worker", "host-external"]\n',
    '[worker]\nprofiles = ["host-worker", "host-external"]\n'
    "progress_timeout_min = 1\n",
)

# The incident's clock, frozen: park 01:00, claim 01:01, last report 01:30,
# provider failure 02:05, operator returns 08:30.
T0 = datetime(2026, 10, 7, 1, 0, 0, tzinfo=timezone.utc)
T_FAIL = T0 + timedelta(hours=1, minutes=5)      # 02:05 the GLM 1308 moment
T_RETURN = T0 + timedelta(hours=7, minutes=30)   # 08:30 the user returns


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _freeze_orx_clock(monkeypatch, start=T0):
    holder = {"now": start}

    def now():
        return holder["now"].isoformat(timespec="microseconds")

    monkeypatch.setattr("orx.state.now", now)
    monkeypatch.setattr("orx.dispatch.db_now", now)
    return holder


ZCODE_SCHEMA = """
CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT, title TEXT,
    time_created INTEGER, time_updated INTEGER, time_archived INTEGER);
CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, data TEXT,
    sequence INTEGER, time_created INTEGER);
"""


def make_incident_db(path, project_root: str, nonce: str, *,
                     edits: int = 35, writes: int = 3, steps: int = 114,
                     created_ms: int, last_activity_ms: int,
                     archived_ms: int | None = None,
                     session_id: str = "sess_worker_a130"):
    """A fake zcode database holding ONE subagent session that executed the
    attempt whose nonce ORX minted: first text part carries the identity
    anchor, then `steps` model requests, `edits` Edit and `writes` Write tool
    calls, all between created_ms and last_activity_ms. Rewritable."""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(ZCODE_SCHEMA)
    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
        ("sess_ctrl", None, project_root, "controller",
         created_ms - 3_600_000, last_activity_ms, None),
    )
    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
        (session_id, "sess_ctrl", project_root, "worker a130",
         created_ms, last_activity_ms, archived_ms),
    )
    span = max(last_activity_ms - created_ms, 1)
    parts: list[tuple] = []

    def add(seq: int, payload: dict) -> None:
        parts.append((
            f"p{seq}", f"m{seq}", session_id,
            json.dumps(payload, separators=(",", ":")), seq,
            created_ms + (seq * span) // 100,
        ))

    add(1, {"type": "text", "text": (
        "ASSIGNMENT IDENTITY (machine anchor — do not remove or alter this"
        f" line):\nORX_ASSIGNMENT={nonce}\n\nYou are an ORX worker executing"
        " ONE assignment.")})
    for i in range(steps):
        add(2 + i, {"type": "step-finish", "reason": "tool-calls"})
    for i in range(edits):
        add(2 + steps + i,
            {"type": "tool", "tool": "Edit", "callID": f"e{i}",
             "state": {"status": "completed"}})
    for i in range(writes):
        add(2 + steps + edits + i,
            {"type": "tool", "tool": "Write", "callID": f"w{i}",
             "state": {"status": "completed"}})
    conn.executemany("INSERT INTO part VALUES (?,?,?,?,?,?)", parts)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def incident(tmp_path, monkeypatch):
    """The a130 shape under a frozen clock: a parked host task claimed by a
    session that then worked, reported once, and failed at 02:05. Yields
    (project, clock, zdb, attempt_id, nonce); the ORX clock sits at 01:30
    (the last report) so each test moves it to the moment it observes."""
    monkeypatch.chdir(tmp_path)
    clock = _freeze_orx_clock(monkeypatch)
    project = make_project(tmp_path, config_toml=PROGRESS_TIMEOUT_ONE_MIN)
    goal = dispatch.create_goal(
        project, objective="recovery facts acceptance",
        acceptance=["the incident facts stay distinguishable"],
        constraints=[], context="",
    )[0]
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["true"]),
        task_spec("T002", acceptance=goal.acceptance, verification=["true"]),
    ]))
    out = dispatch.run_slice(project)
    [parked] = [e for e in out["host_required"] if e["task"] == "T001"]
    nonce = parked["execution"]["nonce"]
    zdb = make_incident_db(
        tmp_path / "zcode.sqlite", str(project.root), nonce,
        created_ms=ms(T0 + timedelta(minutes=1)),
        last_activity_ms=ms(T_FAIL),
    )
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    clock["now"] = T0 + timedelta(minutes=1)
    claimed = dispatch.task_claim(project, "T001", discover_session=True)
    attempt_id = claimed["attempt"]
    assert claimed["session_ref"] == "sess_worker_a130"
    clock["now"] = T0 + timedelta(minutes=30)
    dispatch.task_heartbeat(
        project, "T001", attempt_id, "implementing",
        "37k-char test file drafted; edits on disk",
    )
    yield project, clock, zdb, attempt_id, nonce
    project.close()


def _facts(payload: dict, name: str):
    return payload["session"]["facts"][name]


def _state_snapshot(project):
    return (
        len(project.store.attempts_all()),
        len(project.store.task_events_all()),
        active_task(project, "T001").status,
    )


def _open_worker_attempts(project, task_id="T001"):
    """Open worker attempts for ONE task — the single-writer invariant is
    per task: another task's parked attempt is that task's own single
    writer, never a second writer here."""
    return [a for a in project.store.attempts_all()
            if a.role == "worker" and a.ended_at is None
            and a.task_id == task_id]


# ---------------------------------------------------------------------------
# 1. task observe: the read-only, independently callable diagnostic


def test_observe_distinguishes_failure_observation_and_closure_times(incident):
    project, clock, zdb, attempt_id, nonce = incident
    before_bytes = zdb.read_bytes()
    before = _state_snapshot(project)

    # The operator returns at 08:30 — 6h25m after the provider failure, 7h
    # after the last report. The observation is read at that moment.
    clock["now"] = T_RETURN
    payload = dispatch.task_observe(project, "T001")

    # Identity is bound to session/attempt/nonce, never guessed.
    assert payload["task"] == "T001"
    assert payload["status"] == "running"
    assert payload["read_at"] == clock["now"].isoformat(timespec="microseconds")
    assert payload["attempt"]["id"] == attempt_id
    assert payload["attempt"]["nonce"] == nonce
    assert payload["attempt"]["open"] is True

    # Claim facts survive exactly as recorded (the a130 misreport claimed
    # "never claimed"; the record says otherwise, with its source).
    assert payload["claim"]["claimed_at"] == (
        T0 + timedelta(minutes=1)).isoformat(timespec="microseconds")
    assert payload["claim"]["session_ref"] == "sess_worker_a130"
    assert "started_at" in payload["claim"]["source"]

    # Session terminal facts: every fact carries a value AND a source.
    facts = payload["session"]["facts"]
    assert facts["created_ms"]["value"] == ms(T0 + timedelta(minutes=1))
    assert facts["created_ms"]["source"] == "zcode.session.time_created"
    # 02:05 is the SESSION's last activity — the real failure time, kept in
    # its original epoch form (timezone-neutral, never re-zoned).
    assert facts["last_activity_ms"]["value"] == ms(T_FAIL)
    assert facts["archived_ms"]["value"] is None  # readable and NULL: a fact
    assert facts["edit_calls"]["value"] == 35
    assert facts["write_calls"]["value"] == 3
    assert facts["model_steps"]["value"] == 114
    assert payload["session"]["session_id"] == "sess_worker_a130"
    assert payload["session"]["lookup"] == "session_ref"
    # The success path carries the same availability key as the honest
    # degradations (the human CLI output reads it on every branch).
    assert payload["session"]["available"] is True

    # The three clocks stay distinct: failure (session) 02:05, observation
    # (last report) 01:30, closure (attempt) still open at 08:30.
    assert payload["progress"]["state"] == "overdue"
    assert payload["progress"]["received_at"] == (
        T0 + timedelta(minutes=30)).isoformat(timespec="microseconds")
    assert payload["closure"]["ended_at"] is None
    assert payload["closure"]["result"] is None
    separation = payload["separation"]
    for phrase in ("real failure", "observation", "closure"):
        assert phrase in separation
    assert "silence is not death" in separation

    # The worker kept working AFTER its last report (01:30 -> 02:05): an
    # observation fact, never converted into a liveness claim or a verdict.
    assert payload["activity_after_last_report"] is True
    assert payload["verdict"] is None
    assert payload["session"]["verdict"] is None

    # Read-only, proven: no ORX state moved and the zcode db is byte-identical.
    assert _state_snapshot(project) == before
    assert zdb.read_bytes() == before_bytes


def test_observe_is_honest_without_a_session_or_database(incident, tmp_path,
                                                         monkeypatch):
    project, clock, _, attempt_id, _ = incident
    # No zcode database at all: the session block says unavailable, the ORX
    # facts (claim, progress, closure) still render.
    monkeypatch.setenv("ORX_ZCODE_DB", str(tmp_path / "missing.sqlite"))
    clock["now"] = T_RETURN
    payload = dispatch.task_observe(project, "T001")
    assert payload["session"]["available"] is False
    assert payload["session"]["facts"] == {}
    assert payload["claim"]["claimed_at"] is not None
    assert payload["progress"]["state"] == "overdue"
    assert payload["activity_after_last_report"] is None
    assert payload["verdict"] is None


def test_observe_finds_the_session_by_nonce_without_writing_it(incident):
    """An attempt whose session_ref is still NULL (claim could not bind it)
    still gets session facts: the nonce lookup is read-only observation and
    never stores the discovered id."""
    project, clock, zdb, attempt_id, nonce = incident
    # Wipe the binding claim-time discovery left, keep the clock honest.
    # attempt_update treats None as "leave unchanged", so the fixture wipes
    # the column directly in its own throwaway database.
    with project.store.tx():
        project.store.conn.execute(
            "UPDATE attempts SET session_ref = NULL WHERE id = ?",
            (attempt_id,),
        )
    before_bytes = zdb.read_bytes()
    clock["now"] = T_RETURN
    payload = dispatch.task_observe(project, "T001")
    assert payload["session"]["lookup"] == "nonce"
    assert payload["session"]["session_id"] == "sess_worker_a130"
    assert _facts(payload, "edit_calls")["value"] == 35
    # The lookup wrote nothing: session_ref is still NULL, db byte-identical.
    assert project.store.attempt_get(attempt_id).session_ref is None
    assert zdb.read_bytes() == before_bytes


def test_observe_renders_never_claimed_only_when_it_is_true(incident):
    """T002 was parked in the same slice and never claimed: claimed_at is
    honestly NULL — 'never claimed' is a fact when it is true, and only
    then."""
    project, clock, _, _, _ = incident
    clock["now"] = T_RETURN
    payload = dispatch.task_observe(project, "T002")
    assert payload["status"] == "waiting_host"
    assert payload["claim"]["claimed_at"] is None
    assert payload["claim"]["session_ref"] is None
    assert payload["attempt"]["open"] is True
    assert payload["progress"]["state"] == "unknown"
    assert payload["closure"]["ended_at"] is None


# ---------------------------------------------------------------------------
# 2. task recover: explicit, operator-invoked, fact-anchored


def test_recover_requires_a_confirmed_termination_fact(incident):
    project, clock, _, attempt_id, _ = incident
    clock["now"] = T_RETURN
    before = _state_snapshot(project)
    # Blank fact is a usage error (input class), not a state refusal.
    with pytest.raises(ORXError) as excinfo:
        dispatch.task_recover(project, "T001", "   ")
    assert excinfo.value.reason == "termination_fact_invalid"
    # A too-long fact is bounded like every free-text input.
    with pytest.raises(ORXError) as excinfo:
        dispatch.task_recover(project, "T001", "x" * 513)
    assert excinfo.value.reason == "termination_fact_invalid"
    # Nothing was written by the refusals.
    assert _state_snapshot(project) == before
    assert active_task(project, "T001").status == "running"
    assert _open_worker_attempts(project) and _open_worker_attempts(project)[0].id == attempt_id


def test_recover_records_machine_facts_next_to_the_operator_fact(incident):
    project, clock, _, attempt_id, _ = incident
    clock["now"] = T_RETURN
    fact = ("provider GLM 1308 at 02:05 confirmed by the operator; session"
            " sess_worker_a130 has no further activity")
    result = dispatch.task_recover(project, "T001", fact)

    # The task is reopened for dispatch; the terminated attempt is closed.
    assert result["status"] == "runnable"
    assert result["closed_attempt"] == attempt_id
    assert active_task(project, "T001").status == "runnable"
    closed = project.store.attempt_get(attempt_id)
    assert closed.ended_at == clock["now"].isoformat(timespec="microseconds")
    assert closed.result == "failed"

    # THE misreport guard: the recorded reason carries the operator's fact
    # AND the machine-read facts — claim time, session, edit/write counts —
    # so it can never again say "never claimed / no workspace changes".
    reason = closed.failure_reason
    assert fact in reason
    assert "claimed_at=" + (T0 + timedelta(minutes=1)).isoformat(
        timespec="microseconds") in reason
    assert "sess_worker_a130" in reason
    assert "edits=35" in reason
    assert "writes=3" in reason
    assert result["failure_reason"] == reason
    # One audit event brands the recovery.
    [event] = [e for e in project.store.task_events_all()
               if e.event == "task_recovered"]
    assert fact in event.reason
    # Closure (08:30) lands 6h25m after the 02:05 failure the reason also
    # records: the two clocks never collapse into one "death time".
    assert _parse(closed.ended_at) - T_FAIL == timedelta(hours=6, minutes=25)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def test_recover_keeps_single_writer_and_rotates_the_nonce(incident, tmp_path):
    project, clock, _, attempt_id, old_nonce = incident
    clock["now"] = T_RETURN
    dispatch.task_recover(project, "T001", "termination confirmed by operator")

    # Single writer invariant: the closed attempt holds nothing open.
    assert _open_worker_attempts(project) == []

    # The old attempt's late delivery is refused as a stale submission even
    # though the task is runnable again (recovery reopened dispatch); the
    # late report is refused by the frozen gate order — status gate first,
    # exactly like the delivered-attempt precedent (test_host_progress_e2e).
    with pytest.raises(ConflictError, match="stale"):
        dispatch.task_complete(
            project, "T001", str(write_evidence(tmp_path, "late.json")),
            attempt_id=attempt_id,
        )
    with pytest.raises(ConflictError) as excinfo:
        dispatch.task_heartbeat(project, "T001", attempt_id, "checking", "late")
    assert excinfo.value.reason == "task_not_running"

    # A fresh attempt routes with a NEW nonce; the old nonce never comes back.
    out = dispatch.run_slice(project)
    [parked] = [e for e in out["host_required"] if e["task"] == "T001"]
    new_nonce = parked["execution"]["nonce"]
    assert new_nonce != old_nonce
    clock["now"] = clock["now"] + timedelta(minutes=1)
    claimed = dispatch.task_claim(project, "T001", session="sess_worker_new")
    new_attempt = claimed["attempt"]
    assert new_attempt != attempt_id
    assert len(_open_worker_attempts(project)) == 1
    assert _open_worker_attempts(project)[0].id == new_attempt

    # With the task running under the fresh attempt, the OLD attempt's
    # report is refused as a closed attempt (same reason vocabulary as the
    # fail/retry flow): the recovered attempt is never revived by a report.
    with pytest.raises(ConflictError) as excinfo:
        dispatch.task_heartbeat(project, "T001", attempt_id, "checking", "late")
    assert excinfo.value.reason == "attempt_closed"

    # A delivery quoting the OLD attempt can never close the new one.
    with pytest.raises(ConflictError, match="stale"):
        dispatch.task_complete(
            project, "T001", str(write_evidence(tmp_path, "oldnonce.json")),
            attempt_id=attempt_id,
        )
    # The new attempt delivers normally: exactly one writer, one completion.
    result = dispatch.task_complete(
        project, "T001", str(write_evidence(tmp_path, "new.json")),
        attempt_id=new_attempt,
    )
    assert result["status"] == "passed"
    # The run still finishes cleanly once the sibling task delivers too.
    dispatch.task_claim(project, "T002")
    dispatch.task_complete(
        project, "T002", str(write_evidence(tmp_path, "t2.json")),
    )
    assert dispatch.status_data(project)["run"]["status"] == "done"


def test_recover_refuses_states_that_are_not_the_parked_writer(incident):
    project, clock, _, _, _ = incident
    clock["now"] = T_RETURN
    # Already failed: the recovery entry is retry, not recover.
    dispatch.task_fail(project, "T001", "operator closed it first")
    with pytest.raises(ConflictError, match="requires running"):
        dispatch.task_recover(project, "T001", "fact")
    # Unknown task is a plain NotFound.
    with pytest.raises(NotFoundError):
        dispatch.task_recover(project, "T404", "fact")


# ---------------------------------------------------------------------------
# 3. Shared-failure parking and appended corrections (task note)


def test_shared_failure_records_the_domain_fact_and_parks_explainably(incident,
                                                                      tmp_path):
    project, clock, _, attempt_id, _ = incident
    clock["now"] = T_FAIL  # the controller hits the same failure at 02:05
    note = dispatch.task_note(
        project, "T001", "shared_failure",
        "GLM 1308 on the shared provider account stopped the controller and"
        " the worker at 02:05",
    )

    # Explainably parked: the note is recorded, NOTHING moves.
    assert note["kind"] == "shared_failure"
    assert note["parked"] is True
    assert note["status"] == "running"
    [event] = [e for e in project.store.task_events_all()
               if e.event == "shared_failure_parked"]
    assert "GLM 1308" in event.reason
    assert "orx task recover" in event.reason  # the documented recovery entry
    assert active_task(project, "T001").status == "running"
    assert len(_open_worker_attempts(project)) == 1  # still the same writer

    # No auto-continue and no owner change: the worker still reports and
    # still owns delivery after the note.
    clock["now"] = T_FAIL + timedelta(minutes=1)
    report = dispatch.task_heartbeat(
        project, "T001", attempt_id, "blocked", "quota failure mid-loop",
    )
    assert report["sequence"] == 2
    clock["now"] = T_RETURN
    result = dispatch.task_complete(
        project, "T001", str(write_evidence(tmp_path, "late.json")),
        attempt_id=attempt_id,
    )
    assert result["status"] == "passed"

    # observe surfaces the parked note next to the (now closed) facts.
    payload = dispatch.task_observe(project, "T001")
    kinds = [n["event"] for n in payload["notes"]]
    assert "shared_failure_parked" in kinds


def test_note_input_validation(incident):
    project, _, _, _, _ = incident
    with pytest.raises(ORXError) as excinfo:
        dispatch.task_note(project, "T001", "gossip", "text")
    assert excinfo.value.reason == "note_kind_invalid"
    with pytest.raises(ORXError) as excinfo:
        dispatch.task_note(project, "T001", "shared_failure", "   ")
    assert excinfo.value.reason == "note_detail_invalid"


def test_correction_appends_referencing_the_original_never_editing_it(incident):
    project, clock, zdb, attempt_id, _ = incident
    clock["now"] = T_RETURN
    zcode_before = zdb.read_bytes()
    # The WRONG closure, exactly as the incident recorded it.
    misreport = ("died at spawn; never claimed; no workspace changes")
    dispatch.task_fail(project, "T001", misreport)
    wrong = project.store.attempt_get(attempt_id)
    events_before = len(project.store.task_events_all())

    corrected = dispatch.task_note(
        project, "T001", "correction",
        "claim event and 35 Edits / 3 Writes exist in zcode session"
        " sess_worker_a130; failure was GLM 1308 at 02:05, closed 6h25m"
        " later",
        attempt_id=attempt_id,
    )
    assert corrected["kind"] == "correction"

    # APPEND-only: the original attempt row and its events are untouched.
    still_wrong = project.store.attempt_get(attempt_id)
    assert still_wrong.failure_reason == misreport
    assert still_wrong.ended_at == wrong.ended_at
    assert len(project.store.task_events_all()) == events_before + 1
    # The correction references the original record: attempt id + the
    # original text quoted verbatim.
    [event] = [e for e in project.store.task_events_all()
               if e.event == "record_correction"]
    assert f"attempt {attempt_id}" in event.reason
    assert misreport in event.reason
    assert "35 Edits" in event.reason
    # observe shows the unchanged original closure AND the correction.
    payload = dispatch.task_observe(project, "T001")
    assert payload["closure"]["failure_reason"] == misreport
    assert any(n["event"] == "record_correction" for n in payload["notes"])
    # History was appended, never edited — including the zcode database.
    assert zdb.read_bytes() == zcode_before


def test_correction_requires_the_attempt_it_corrects(incident):
    project, _, _, attempt_id, _ = incident
    with pytest.raises(ORXError) as excinfo:
        dispatch.task_note(project, "T001", "correction", "text")
    assert excinfo.value.reason == "note_attempt_required"
    other = project.store.attempt_create(
        revision_row_id=project.store.revision_active(
            project.store.run_for_goal(project.store.goal_active().id).id).id,
        role="worker", profile="host-worker", driver="host", harness="zcode",
        model_id="m", requested_effort="medium", task_id="T888",
        started=False,
    )
    with pytest.raises(ConflictError, match="T888"):
        dispatch.task_note(
            project, "T001", "correction", "text", attempt_id=other.id,
        )
    assert attempt_id


# ---------------------------------------------------------------------------
# 4. Lost contact but still writing stays observation-only


def test_lost_contact_but_still_writing_never_becomes_a_verdict(incident):
    project, clock, zdb, attempt_id, _ = incident
    # The report is hours old (overdue), but the session shows the worker
    # STILL WRITING one minute before the read.
    still_writing = T_RETURN - timedelta(minutes=1)
    make_incident_db(
        zdb, str(project.root), project.store.attempt_get(attempt_id).nonce,
        created_ms=ms(T0 + timedelta(minutes=1)),
        last_activity_ms=ms(still_writing),
    )
    clock["now"] = T_RETURN
    before = _state_snapshot(project)

    payload = dispatch.task_observe(project, "T001")
    # Both facts coexist: the report window is overdue AND the session was
    # active a minute ago. Neither is converted into death or life.
    assert payload["progress"]["state"] == "overdue"
    assert payload["activity_after_last_report"] is True
    assert _facts(payload, "last_activity_ms")["value"] == ms(still_writing)
    assert payload["verdict"] is None
    # Observation only: nothing moved anywhere.
    assert _state_snapshot(project) == before
    # No fact ever becomes an automatic fail/retry: the recovery entry still
    # demands the operator's confirmed termination fact.
    with pytest.raises(ORXError) as excinfo:
        dispatch.task_recover(project, "T001", "  ")
    assert excinfo.value.reason == "termination_fact_invalid"
    # And with a fact the decision stays the operator's — recorded next to
    # the observed recent activity, contradiction visible to any auditor.
    result = dispatch.task_recover(
        project, "T001", "operator confirmed the session is finished"
    )
    assert result["status"] == "runnable"
    assert "last_activity=" in project.store.attempt_get(attempt_id).failure_reason


# ---------------------------------------------------------------------------
# 5. CLI surface: exit codes, envelopes, and the recovery face


def test_cli_observe_recover_note_surfaces(incident, tmp_path, monkeypatch):
    project, clock, _, attempt_id, _ = incident
    clock["now"] = T_RETURN

    observed = runner.invoke(app, ["task", "observe", "T001", "--json"])
    assert observed.exit_code == 0
    envelope = json.loads(observed.stdout)
    assert envelope["ok"] is True
    assert envelope["attempt"]["id"] == attempt_id
    assert envelope["session"]["facts"]["edit_calls"]["value"] == 35

    # The human rendering reads every branch of the session block (a live
    # smoke test caught a missing availability key only this path reads).
    human = runner.invoke(app, ["task", "observe", "T001"])
    assert human.exit_code == 0
    assert "edit_calls: 35" in human.stdout
    assert "verdict: none (observation only)" in human.stdout

    # recover without a fact is a usage error: exit 2 with the machine reason.
    missing = runner.invoke(app, ["task", "recover", "T001", "--json"])
    assert missing.exit_code == 2
    blank = runner.invoke(
        app, ["task", "recover", "T001", "--termination-fact", "  ", "--json"])
    assert blank.exit_code == 2
    assert json.loads(blank.stdout)["error"]["reason"] == "termination_fact_invalid"

    recovered = runner.invoke(
        app, ["task", "recover", "T001",
              "--termination-fact", "operator confirmed termination",
              "--json"])
    assert recovered.exit_code == 0
    body = json.loads(recovered.stdout)
    assert body["status"] == "runnable"
    assert body["closed_attempt"] == attempt_id

    bad_note = runner.invoke(
        app, ["task", "note", "T001", "--kind", "gossip",
              "--detail", "x", "--json"])
    assert bad_note.exit_code == 2
    assert json.loads(bad_note.stdout)["error"]["reason"] == "note_kind_invalid"

    note = runner.invoke(
        app, ["task", "note", "T001", "--kind", "shared-failure",
              "--detail", "controller and worker on one quota domain", "--json"])
    assert note.exit_code == 0
    body = json.loads(note.stdout)
    assert body["parked"] is True
    assert body["kind"] == "shared_failure"  # hyphen hint, identifier record
    assert any(
        e.event == "shared_failure_parked"
        for e in project.store.task_events_all()
    )


def test_run_recovery_face_points_at_the_new_entries(incident):
    project, clock, _, attempt_id, _ = incident
    clock["now"] = T_RETURN
    [entry] = [e for e in dispatch.run_slice(project)["recovery"]
               if e["task"] == "T001"]
    # The frozen fail/retry contract strings stay (G006 contract §10)…
    assert f"orx task complete T001 --attempt {attempt_id}" in entry["contract"]
    assert "orx task fail T001" in entry["contract"]
    assert "orx task retry T001" in entry["contract"]
    # …and the new entries: the read-only diagnostic and the explicit,
    # fact-gated recovery step.
    assert entry["observe"] == "orx task observe T001"
    assert "orx task recover T001" in entry["recover"]
    assert "--termination-fact" in entry["recover"]
    assert "shared-failure" in entry["contract"]


# ---------------------------------------------------------------------------
# 6. The controller skill teaches the confirmed-termination-fact discipline


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_controller_skill_teaches_observe_recover_and_parking():
    text = _flat((REPO_ROOT / "skills" / "orx-controller" / "SKILL.md").read_text())
    # The diagnostic entry and what it is (and is not).
    assert "orx task observe" in text
    assert "never a verdict" in text or "not a verdict" in text
    # The recovery step requires a CONFIRMED TERMINATION FACT — the death
    # decision is the operator's, and the fact is recorded with the
    # machine-read evidence.
    assert "orx task recover" in text
    assert "--termination-fact" in text
    assert "termination fact" in text
    # The shared-failure parking: record the fact, stay parked, no
    # auto-continue.
    assert "shared-failure" in text
    assert "parked" in text
    # Corrections append, they never rewrite history.
    assert "--kind correction" in text
    assert "append" in text
    # The bounded-wait discipline of G006 stays intact.
    assert "check the original worker session before any fail/retry" in text
    assert "confirmed dead" in text
    assert "no automatic heartbeat" in text
    assert "not a liveness proof" in text


def test_no_recovery_doc_line_claims_automation_or_liveness():
    """The recovery additions stay inside the contract §11 red lines: no
    line mentioning recover/observe/note may promise an automatic action,
    and nothing anywhere claims observe decides liveness."""
    path = REPO_ROOT / "skills" / "orx-controller" / "SKILL.md"
    for line in path.read_text().splitlines():
        lower = line.lower()
        if "observe" in lower or "recover" in lower:
            assert "automatically" not in lower or "not" in lower, line
            assert "decides" not in lower, line
