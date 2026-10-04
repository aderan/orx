"""Verification: deterministic commands, agent verdicts, vision capability."""

from __future__ import annotations

import pytest

from orx import dispatch
from orx.records import ConflictError, NotFoundError, ORXError, RoutingError

from conftest import HOST_CONFIG_TOML, HOST_PROFILES_TOML, ir_for, make_project, task_spec, write_evidence


@pytest.fixture
def planned(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance[:1], verification=["test -f t1.marker"]),
        task_spec("T002", acceptance=goal.acceptance[1:], verification=[]),
        task_spec("T003", acceptance=goal.acceptance[1:], verification=["agent: result reads well"]),
    ]))
    dispatch.run_slice(project)
    return project


def _finish(project, task_id, tmp_path):
    dispatch.task_claim(project, task_id)
    return dispatch.task_complete(project, task_id, str(write_evidence(tmp_path)))


def test_command_verification_pass(planned, tmp_path):
    (planned.root / "t1.marker").write_text("ok")
    result = _finish(planned, "T001", tmp_path)
    assert result["verdict"] == "passed"
    assert result["status"] == "passed"


def test_command_verification_fail(planned, tmp_path):
    # t1.marker deliberately missing
    result = _finish(planned, "T001", tmp_path)
    assert result["verdict"] == "failed"
    assert result["status"] == "failed"
    task = next(t for t in dispatch.task_list(planned) if t["id"] == "T001")
    assert "test -f t1.marker" in (task["failure_reason"] or "")


def test_complete_is_not_passed_until_verification_decides(planned, tmp_path):
    # T003 has an agent verification: completion parks it in verifying.
    result = _finish(planned, "T003", tmp_path)
    assert result["status"] == "verifying"
    task = next(t for t in dispatch.task_list(planned) if t["id"] == "T003")
    assert task["status"] == "verifying"


def test_empty_verification_passes_on_completion(planned, tmp_path):
    result = _finish(planned, "T002", tmp_path)
    assert result["verdict"] == "passed"
    assert result["status"] == "passed"


def test_forbidden_command_fails_without_running(planned, tmp_path, goal):
    dispatch.submit_plan(planned, ir_for(goal, [
        task_spec("T900", acceptance=goal.acceptance, verification=["sudo rm /tmp/x"]),
    ]))
    dispatch.run_slice(planned)
    dispatch.task_claim(planned, "T900")
    result = dispatch.task_complete(planned, "T900", str(write_evidence(tmp_path)))
    assert result["status"] == "failed"


def test_agent_verdict_pass_via_submit(planned, tmp_path):
    _finish(planned, "T003", tmp_path)
    result = dispatch.verify_submit(planned, "T003", "pass", None, None)
    assert result["status"] == "passed"


def test_agent_verdict_fail_via_submit(planned, tmp_path):
    _finish(planned, "T003", tmp_path)
    result = dispatch.verify_submit(planned, "T003", "fail", None, str(write_evidence(tmp_path)))
    assert result["status"] == "failed"


def test_host_verifier_session_does_not_change_verdict(planned, tmp_path, monkeypatch):
    """A host verifier records the caller's session. Pass/fail is unchanged."""
    monkeypatch.setenv("ORX_SESSION_REF", "env-verifier")
    _finish(planned, "T003", tmp_path)
    with pytest.raises(ORXError, match="malformed"):
        dispatch.verify_submit(planned, "T003", "pass", None, None, session="bad ref")
    assert not any(a.role == "verifier" for a in planned.store.attempts_all())
    result = dispatch.verify_submit(
        planned, "T003", "pass", None, None, session="flag-verifier",
    )
    assert result["status"] == "passed"
    assert result["verdict"] == "passed"
    verifier = next(a for a in planned.store.attempts_all() if a.role == "verifier")
    assert verifier.session_ref == "flag-verifier"
    assert verifier.started_at is not None and verifier.ended_at is not None


def test_verify_submit_requires_verifying(planned, tmp_path):
    with pytest.raises(ConflictError):
        dispatch.verify_submit(planned, "T001", "pass", None, None)


def test_verify_submit_without_pending_agent_entry(planned, tmp_path):
    _finish(planned, "T003", tmp_path)
    dispatch.verify_submit(planned, "T003", "pass", None, None)
    with pytest.raises(ConflictError):  # task already moved out of verifying
        dispatch.verify_submit(planned, "T003", "pass", None, None)


def test_verify_dispatch_reports_agent_assignments(planned, tmp_path):
    _finish(planned, "T003", tmp_path)
    result = dispatch.verify_dispatch(planned)
    entry = next(e for e in result["agent_required"] if e["task"] == "T003")
    assert entry["profile"] == "host-verifier"
    assert entry["required_capabilities"] == ["coding"]
    assert entry["driver"] == "host"


def test_multiple_agent_entries_submitted_individually(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    goal = dispatch.create_goal(project, "two checks", ["both checked"])[0]
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance,
                  verification=["agent: first", "agent[vision]: second"]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))

    status = next(t for t in dispatch.task_list(project) if t["id"] == "T001")
    assert status["status"] == "verifying"

    first = dispatch.verify_submit(project, "T001", "pass", "agent: first", None)
    assert first["status"] == "verifying"
    second = dispatch.verify_submit(project, "T001", "pass", "agent[vision]: second", None)
    assert second["status"] == "passed"


def test_vision_entry_requires_vision_capable_verifier(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-verifier", "host-vision"]', 'profiles = ["host-verifier"]'
    )
    project = make_project(tmp_path, config_toml=config)
    goal = dispatch.create_goal(project, "vision check", ["ui looks right"])[0]
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance,
                  verification=["agent[vision]: the screenshot matches"]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))

    with pytest.raises(RoutingError) as excinfo:
        dispatch.verify_submit(project, "T001", "pass", None, None)
    assert "vision" in str(excinfo.value)
    project.close()


def test_vision_entry_routes_vision_profile_when_available(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    goal = dispatch.create_goal(project, "vision check", ["ui looks right"])[0]
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance,
                  verification=["agent[vision]: the screenshot matches"]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))

    result = dispatch.verify_dispatch(project)
    entry = next(e for e in result["agent_required"] if e["task"] == "T001")
    assert entry["profile"] == "host-vision"

    submitted = dispatch.verify_submit(project, "T001", "pass", None, None)
    assert submitted["status"] == "passed"
    # The verifier attempt is attributed to the vision-capable profile.
    decisions = project.store.routing_decisions_all()
    assert decisions[-1].selected == "host-vision"
    assert decisions[-1].role == "verifier"
    project.close()


def test_verification_capabilities_persisted_decomposed(tmp_path, monkeypatch):
    # The agent[vision]: bracket is IR surface syntax; persisted state keeps
    # role/capability as independent fields.
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    goal = dispatch.create_goal(project, "vision check", ["ui looks right"])[0]
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance,
                  verification=["agent[vision]: the screenshot matches"]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    dispatch.verify_submit(project, "T001", "pass", None, None)

    revision = project.store.revision_active(
        project.store.run_for_goal(goal.id).id)
    [row] = project.store.verifications_for(revision.id, "T001")
    assert row.kind == "agent"
    assert row.required_capabilities == ["vision"]
    attempt = project.store.attempt_get(row.attempt_id)
    assert attempt.role == "verifier"
    assert attempt.effort_source is None  # host attempt: no child process
    project.close()


def test_retry_after_verification_failure_can_pass(planned, tmp_path):
    # First completion fails verification (marker missing)...
    result = _finish(planned, "T001", tmp_path)
    assert result["status"] == "failed"
    # ...the operator fixes the work, retries, and the same verification passes.
    (planned.root / "t1.marker").write_text("ok")
    dispatch.task_retry(planned, "T001")
    dispatch.run_slice(planned)
    dispatch.task_claim(planned, "T001")
    result = dispatch.task_complete(planned, "T001", str(write_evidence(tmp_path, "e1b.json")))
    assert result["status"] == "passed"


def test_verification_output_and_evidence_recorded(planned, tmp_path):
    (planned.root / "t1.marker").write_text("ok")
    _finish(planned, "T001", tmp_path)
    goal = planned.store.goal_active()
    run = planned.store.run_for_goal(goal.id)
    revision = planned.store.revision_active(run.id)
    rows = planned.store.verifications_for(revision.id, "T001")
    assert len(rows) == 1
    assert rows[0].passed and rows[0].kind == "command"
    assert rows[0].output_path and (planned.root / rows[0].output_path).exists()
    attempt = planned.store.attempt_latest_for_task(revision.id, "T001")
    assert attempt.result == "completed"
