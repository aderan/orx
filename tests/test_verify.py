"""Verification: deterministic commands, agent verdicts, vision capability."""

from __future__ import annotations

import json

import pytest

from orx import dispatch, verify
from orx.records import ConflictError, NotFoundError, ORXError, RoutingError

from conftest import (
    HOST_CONFIG_TOML,
    HOST_PROFILES_TOML,
    active_task,
    ir_for,
    make_project,
    task_spec,
    write_evidence,
)


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


def test_command_verification_red_refuses_completion(planned, tmp_path):
    """Red-complete semantics (host): a red command gate REFUSES the
    completion. Nothing is accepted — the task keeps its prior status, the
    attempt stays open, no completion event or evidence is recorded — and
    the refusal carries the per-check detail. Fixing the workspace and
    completing again on the same attempt is the intended loop."""
    # t1.marker deliberately missing
    with pytest.raises(verify.DeliveryRejected) as excinfo:
        _finish(planned, "T001", tmp_path)
    assert excinfo.value.kind == "gate"
    report = excinfo.value.report
    assert report["task"] == "T001"
    assert report["task_status"] == "running"  # the prior status, kept
    [failure] = report["failures"]
    assert failure["command"] == "test -f t1.marker"
    assert failure["exit_code"] == 1
    assert failure["log_path"]
    assert (planned.root / failure["log_path"]).exists()

    task = next(t for t in dispatch.task_list(planned) if t["id"] == "T001")
    assert task["status"] == "running"
    revision_id = active_task(planned, "T001").revision_id
    attempt = planned.store.attempt_latest_for_task(revision_id, "T001")
    assert attempt.ended_at is None  # the attempt stays open
    assert not [
        e for e in planned.store.task_events(revision_id, "T001")
        if e.event == "complete"
    ]
    assert not [
        kind for kind, _ in planned.store.evidence_for_task(revision_id, "T001")
        if kind == "completion"
    ]

    # The refusal still recorded its red rows (history in the making): fix
    # the workspace and complete green on the SAME attempt.
    (planned.root / "t1.marker").write_text("ok")
    result = dispatch.task_complete(planned, "T001", str(write_evidence(tmp_path, "e1b.json")))
    assert result["verdict"] == "passed"
    assert result["status"] == "passed"
    assert result["attempt"] == attempt.id


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


def test_forbidden_command_refuses_completion_without_running(planned, tmp_path, goal):
    """A denylisted command entry can never pass: the gate refuses the
    completion with the denial detail (the command never executed), records
    the row with exit NULL and passed false, and the task stays running."""
    dispatch.submit_plan(planned, ir_for(goal, [
        task_spec("T900", acceptance=goal.acceptance, verification=["sudo rm /tmp/x"]),
    ]))
    dispatch.run_slice(planned)
    dispatch.task_claim(planned, "T900")
    with pytest.raises(verify.DeliveryRejected) as excinfo:
        dispatch.task_complete(planned, "T900", str(write_evidence(tmp_path)))
    assert excinfo.value.kind == "gate"
    [failure] = excinfo.value.report["failures"]
    assert failure["command"] == "sudo rm /tmp/x"
    assert failure["denied"] is True
    assert failure["denial_reason"] == "sudo"
    assert failure["exit_code"] is None
    assert "denied by verification denylist: sudo" in failure["error_summary"]

    task = active_task(planned, "T900")
    assert task.status == "running"
    [row] = planned.store.verifications_for(task.revision_id, "T900")
    assert row.kind == "command" and row.exit_code is None and not row.passed
    assert (planned.root / row.output_path).read_text().startswith(
        "denied by verification denylist: sudo"
    )


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
    """Per-attempt history: a failed round's verification rows survive the
    retry and stay queryable by attempt; the retried task's verdict reads
    only the new attempt's window, so a task with failure history that
    passes on retry is 'passed', never misread from the old rows."""
    # Round 1: the red gate refuses the completion; the worker then reports
    # the failure as its delivery result, which fails the task.
    with pytest.raises(verify.DeliveryRejected):
        _finish(planned, "T001", tmp_path)
    failed = write_evidence(
        tmp_path, "e1-failed.json", status="failed", summary="marker not written",
        checks=[{"command": "test -f t1.marker", "exit_code": 1, "log": None}],
    )
    result = dispatch.task_complete(planned, "T001", str(failed))
    assert result["status"] == "failed"

    revision_id = active_task(planned, "T001").revision_id
    round_one = planned.store.attempt_latest_for_task(revision_id, "T001")
    refused_rows = planned.store.verifications_for(revision_id, "T001")
    assert refused_rows and all(not v.passed for v in refused_rows)
    assert all(v.attempt_id == round_one.id for v in refused_rows)

    # Retry deletes nothing: every old row is still there, per attempt too.
    (planned.root / "t1.marker").write_text("ok")
    dispatch.task_retry(planned, "T001")
    assert planned.store.verifications_for(revision_id, "T001") == refused_rows
    assert planned.store.verifications_for_attempt(round_one.id) == refused_rows

    # Round 2: fresh attempt, green gate — the verdict ignores round 1.
    dispatch.run_slice(planned)
    dispatch.task_claim(planned, "T001")
    round_two = planned.store.attempt_latest_for_task(revision_id, "T001")
    assert round_two.id != round_one.id
    result = dispatch.task_complete(planned, "T001", str(write_evidence(tmp_path, "e1b.json")))
    assert result["verdict"] == "passed"
    assert result["status"] == "passed"

    # Full history coexists with the current view: both rounds' rows are in
    # the table; only the latest attempt's window is the current result.
    all_rows = planned.store.verifications_for(revision_id, "T001")
    assert [v.attempt_id for v in all_rows] == [round_one.id, round_two.id]
    assert [v.passed for v in all_rows] == [False, True]  # history kept
    current = planned.store.verifications_current(revision_id, "T001")
    assert [v.attempt_id for v in current] == [round_two.id]
    task_row = active_task(planned, "T001")
    assert verify.evaluate(planned.store, revision_id, task_row) == "passed"


def test_verify_logs_coexist_per_attempt(planned):
    """The verify/ log tree is attempt-addressed: the same command entry run
    for two attempts of one task leaves two log files side by side (the path
    carries the attempt number), and the later attempt never overwrites the
    earlier attempt's output."""
    goal = planned.store.goal_active()
    run = planned.store.run_for_goal(goal.id)
    revision = planned.store.revision_active(run.id)
    task = planned.store.task_get(revision.id, "T001")
    first_attempt = planned.store.attempt_latest_for_task(revision.id, "T001")
    second_attempt = planned.store.attempt_create(
        revision_row_id=revision.id, role="worker", profile="host-worker",
        driver="host", harness="zcode", model_id="m-worker",
        requested_effort="medium", task_id="T001",
    )

    # Attempt 1 runs the entry red (marker missing)...
    first = verify.run_command_verifications(
        planned.store, planned.root, run.id, revision.id, task,
        timeout=30, attempt_id=first_attempt.id,
    )
    assert len(first) == 1 and not first[0].passed
    # ...the retry routes attempt 2, which runs it green: the history row
    # from attempt 1 does not suppress the re-run.
    (planned.root / "t1.marker").write_text("ok")
    second = verify.run_command_verifications(
        planned.store, planned.root, run.id, revision.id, task,
        timeout=30, attempt_id=second_attempt.id,
    )
    assert len(second) == 1 and second[0].passed

    logs = [row.output_path for row in planned.store.verifications_for(revision.id, "T001")]
    assert len(logs) == 2
    assert all("/verify/T001/" in path for path in logs)
    assert f"a{first_attempt.id:03d}" in logs[0]
    assert f"a{second_attempt.id:03d}" in logs[1]
    texts = {(planned.root / path).read_text() for path in logs}
    assert any("[exit 1" in text for text in texts)  # attempt 1's output survived
    assert any("[exit 0" in text for text in texts)


def test_stale_attempt_with_legacy_evidence_reports_evidence_rejection(planned, tmp_path):
    """Ordering contract: the evidence document is validated before attempt
    staleness. A stale submission carrying a legacy-shape file is refused as
    an EVIDENCE problem (naming the missing fields), not as a stale
    submission — the worker learns to write a delivery result first."""
    dispatch.task_claim(planned, "T001")
    revision_id = active_task(planned, "T001").revision_id
    old_attempt = planned.store.attempt_latest_for_task(revision_id, "T001")

    dispatch.task_fail(planned, "T001", "worker died")
    dispatch.task_retry(planned, "T001")
    dispatch.run_slice(planned)
    dispatch.task_claim(planned, "T001")

    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"summary": "done", "commands": [], "artifacts": []}))
    with pytest.raises(verify.DeliveryRejected) as excinfo:
        dispatch.task_complete(planned, "T001", str(legacy), attempt_id=old_attempt.id)
    assert excinfo.value.kind == "evidence"
    joined = "; ".join(excinfo.value.fields)
    assert "'status'" in joined and "'checks'" in joined
    assert active_task(planned, "T001").status == "running"  # nothing was recorded


def test_cli_red_gate_follows_failed_flow_with_detail(tmp_path, monkeypatch):
    """Red-complete semantics (CLI worker): the launched worker finishing is
    not a green delivery — the gate re-ran its checks, and a red gate there
    follows the current FAILED flow (complete -> verify_fail) with the full
    per-check detail recorded. The refuse-and-keep-running flow is the
    host/external contract; CLI execution has no caller to refuse."""
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]', 'profiles = ["cli-fake"]'
    )
    project = make_project(tmp_path, config_toml=config)
    try:
        goal = dispatch.create_goal(project, "cli gate", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["a1"], verification=["false"]),
        ]))
        outcome = dispatch.run_slice(project)
        [failed] = outcome["failed"]
        assert failed["task"] == "T001"
        assert failed["status"] == "failed"
        assert failed["verdict"] == "failed"
        reason = failed["reason"] or ""
        assert "verification failed" in reason and "delivery gate" in reason
        assert "false" in reason  # the failing command is named
        [gate_failure] = verify.gate_failures(failed["gate"])
        assert gate_failure["command"] == "false"
        assert gate_failure["exit_code"] == 1
        assert gate_failure["log_path"].startswith(".orx/runs/R001/check/T001/")
        assert (project.root / gate_failure["log_path"]).exists()

        task = active_task(project, "T001")
        assert task.status == "failed"
        assert task.failure_reason == reason
        events = [e.event for e in project.store.task_events(task.revision_id, "T001")]
        assert events[-2:] == ["complete", "verify_fail"]
    finally:
        project.close()


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
