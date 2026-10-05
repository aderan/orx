"""Span and run-lifecycle behavior for the v7 observability foundation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orx import dispatch
from orx.adapters.codex import CodexAdapter
from orx.adapters.cursor import CursorAdapter
from orx.records import ConflictError, PlanValidationError

from conftest import ir_for, task_spec, write_evidence

FIXTURES = Path(__file__).parent / "fixtures"


def test_blank_session_ref_is_not_stored(project, goal, monkeypatch):
    monkeypatch.setenv("ORX_SESSION_REF", "   ")
    dispatch.plan_route(project)
    planner = next(a for a in project.store.attempts_all() if a.role == "planner")
    assert planner.session_ref is None


def test_host_planner_reuse_and_spans(project, goal, tmp_path, monkeypatch):
    """A waiting host assignment keeps one open planner attempt. Successful
    submit closes that attempt. A rejected submit does not. Completed host
    worker and verifier attempts both carry started_at and ended_at."""
    monkeypatch.setenv("ORX_SESSION_REF", "host-sess-7")
    run = project.store.run_for_goal(goal.id)
    assert run.started_at is None and run.completed_at is None

    planned = dispatch.plan_route(project)
    dispatch.plan_route(project)
    planners = [a for a in project.store.attempts_all() if a.role == "planner"]
    assert len(planners) == 1
    assert planners[0].ended_at is None
    assert planners[0].started_at is not None
    assert planners[0].result is None
    assert planners[0].session_ref == "host-sess-7"
    assert planners[0].run_id == run.id
    assert planners[0].assignment_id == planned["assignment"]["id"]

    bad = ir_for(goal, [
        task_spec("T101", deps=["T999"], acceptance=goal.acceptance),
    ])
    with pytest.raises(PlanValidationError):
        dispatch.submit_plan(project, bad)
    assert project.store.attempt_get(planners[0].id).ended_at is None
    assert project.store.attempt_get(planners[0].id).result is None
    assert len([a for a in project.store.attempts_all() if a.role == "planner"]) == 1

    dispatch.submit_plan(project, ir_for(goal, [
        task_spec(
            "T001",
            acceptance=goal.acceptance,
            verification=["true", "agent: the summary is honest"],
        ),
    ]))
    closed = project.store.attempt_get(planners[0].id)
    assert closed.started_at is not None
    assert closed.ended_at is not None
    assert closed.result == "completed"
    assert len([a for a in project.store.attempts_all() if a.role == "planner"]) == 1

    running = project.store.run_get(run.id)
    assert running.status == "running"
    assert running.started_at is not None
    assert running.completed_at is None
    started = running.started_at
    dispatch.refresh(project.store, project.store.goal_get(goal.id), running)
    refreshed = project.store.run_get(run.id)
    assert refreshed.started_at == started
    assert refreshed.completed_at is None

    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    result = dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    assert result["status"] == "verifying"
    worker = next(a for a in project.store.attempts_all() if a.role == "worker")
    assert worker.started_at is not None
    assert worker.ended_at is not None
    assert worker.result == "completed"
    assert worker.run_id == run.id
    assert worker.session_ref == "host-sess-7"

    verdict = dispatch.verify_submit(project, "T001", "pass", None, None)
    assert verdict["status"] == "passed"
    verifier = next(a for a in project.store.attempts_all() if a.role == "verifier")
    assert verifier.started_at is not None
    assert verifier.ended_at is not None
    assert verifier.run_id == run.id
    assert verifier.session_ref == "host-sess-7"

    done = project.store.run_get(run.id)
    assert done.status == "done"
    assert done.completed_at is not None
    completed = done.completed_at
    dispatch.refresh(project.store, project.store.goal_get(goal.id), done)
    again = project.store.run_get(run.id)
    assert again.completed_at == completed
    assert again.started_at == started
    assert again.updated_at != completed

    dispatch.plan_route(project)
    reopened = project.store.run_get(run.id)
    # G004 T003: routing a replan does NOT reopen a completed run anymore —
    # the recorded completion survives until a new revision actually lands.
    assert reopened.status == "done"
    assert reopened.completed_at == completed
    assert reopened.started_at == started


def test_host_report_clears_usage_missing_reason(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ]))
    run = project.store.run_for_goal(goal.id)
    revision = project.store.revision_active(run.id)
    attempt = project.store.attempt_create(
        revision_row_id=revision.id,
        role="worker",
        profile="orx-host",
        driver="host",
        harness="zcode",
        model_id="m",
        requested_effort="medium",
        task_id="T001",
    )
    project.store.attempt_mark_usage_missing(attempt.id, "harness_omitted")
    assert project.store.attempt_get(attempt.id).usage_missing_reason == "harness_omitted"
    project.store.usage_add(
        attempt.id, "orx-host", run.id, "T001", 1, 1, 0, "host_report", "unknown",
    )
    assert project.store.attempt_get(attempt.id).usage_missing_reason is None
    row = project.store.usage_rows()[0]
    assert row["source"] == "host_report"
    assert row["accuracy"] == "unknown"
    assert attempt.run_id == run.id


def test_claim_session_flag_precedes_env(project, goal, monkeypatch):
    monkeypatch.setenv("ORX_SESSION_REF", "from-env")
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ]))
    dispatch.run_slice(project)
    worker = next(a for a in project.store.attempts_all() if a.role == "worker")
    assert worker.session_ref == "from-env"
    dispatch.task_claim(project, "T001", session="from-flag")
    assert project.store.attempt_get(worker.id).session_ref == "from-flag"


def test_host_report_is_idempotent_and_rejects_conflicts(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ]))
    run = project.store.run_for_goal(goal.id)
    revision = project.store.revision_active(run.id)
    attempt = project.store.attempt_create(
        revision.id, "worker", "orx-host", "host", "zcode", "m", "medium", task_id="T001",
    )
    first = project.store.usage_record_host(attempt.id, 10, 2, 80, "estimated")
    assert first["idempotent"] is False
    assert first["profile"] == "orx-host"
    assert first["run_id"] == run.id
    assert first["task_id"] == "T001"
    assert first["cached_input_tokens"] == 80
    assert first["cached_input_tokens"] > first["input_tokens"]
    second = project.store.usage_record_host(attempt.id, 10, 2, 80, "estimated")
    assert second["idempotent"] is True
    assert len(project.store.usage_rows()) == 1
    with pytest.raises(ConflictError, match="refusing to replace"):
        project.store.usage_record_host(attempt.id, 11, 2, 80, "estimated")
    assert len(project.store.usage_rows()) == 1
    assert project.store.usage_rows()[0]["input_tokens"] == 10

    report = dispatch.usage(project)
    observed = next(row for row in report["observations"] if row["source"] == "host_report")
    assert observed["accuracy"] == "estimated"
    assert observed["cached_input_tokens"] == 80
    coverage = next(row for row in report["coverage"] if row["profile"] == "orx-host")
    assert coverage["measurement_accuracy"] == "estimated"
    profile = next(row for row in report["profiles"] if row["profile"] == "orx-host")
    assert "fee" not in profile and "cost" not in observed


class _Stream:
    def __init__(self, stdout: str, exit_code: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.exit_code = exit_code
        self.timed_out = False


def test_sanitized_fixtures_extract_session_or_null():
    """Codex thread_id and cursor session_id come from the sanitized fixtures.
    request_id and a numeric thread_id are not sessions. The last codex
    turn.completed is kept whole; it is not added to the earlier turn.
    Cursor cached may exceed input."""
    codex = CodexAdapter()
    cursor = CursorAdapter()
    launch = None

    both = codex.interpret_capture(launch, _Stream((FIXTURES / "codex-thread-usage.jsonl").read_text()))
    assert both.session_ref == "11111111-1111-4111-8111-111111111111"
    assert both.session_ref != "do-not-use-as-session"
    assert both.usage == {
        "input_tokens": 3, "output_tokens": 2, "cached_input_tokens": 4,
        "source": "native_cli", "accuracy": "exact",
    }
    assert both.usage["input_tokens"] != 13

    numeric = codex.interpret_capture(
        launch, _Stream((FIXTURES / "codex-numeric-thread.jsonl").read_text()))
    assert numeric.session_ref is None
    assert numeric.usage["accuracy"] == "exact"
    assert numeric.usage["input_tokens"] == 1

    captured = (FIXTURES / "codex-0.160-worker-exec.jsonl").read_text()
    historical = codex.interpret_capture(launch, _Stream(captured))
    assert historical.session_ref == "00000000-0000-0000-0000-000000000000"
    assert historical.usage["input_tokens"] == 73844
    assert historical.usage["cached_input_tokens"] == 63232
    assert historical.usage["output_tokens"] == 308

    envelope = cursor.interpret_capture(
        launch, _Stream((FIXTURES / "cursor-session-usage.json").read_text()))
    assert envelope.session_ref == "22222222-2222-4222-8222-222222222222"
    body = json.loads((FIXTURES / "cursor-session-usage.json").read_text())
    assert envelope.session_ref != body["request_id"]
    assert envelope.usage["input_tokens"] == 10
    assert envelope.usage["cached_input_tokens"] == 500
    assert envelope.usage["cached_input_tokens"] > envelope.usage["input_tokens"]
    assert envelope.usage["accuracy"] == "exact"

    request_only = cursor.interpret_capture(
        launch, _Stream((FIXTURES / "cursor-request-id-only.json").read_text()))
    assert request_only.session_ref is None
    assert request_only.usage["accuracy"] == "exact"


def test_partial_and_malformed_usage_never_become_exact_or_zero():
    cursor = CursorAdapter()
    partial = cursor.interpret_capture(None, _Stream(json.dumps({
        "type": "result", "session_id": "sess-1",
        "usage": {"inputTokens": 8, "outputTokens": 2},
    })))
    assert partial.usage["cached_input_tokens"] is None
    assert partial.usage["accuracy"] == "unknown"
    assert partial.usage["input_tokens"] == 8
    assert partial.session_ref == "sess-1"

    malformed = cursor.interpret_capture(None, _Stream(json.dumps({
        "type": "result", "session_id": "sess-1",
        "usage": {"inputTokens": 8, "outputTokens": True, "cacheReadTokens": 1},
    })))
    assert malformed.usage is None
    assert malformed.miss_reason == "malformed_output"
    assert malformed.session_ref == "sess-1"

    spaced = cursor.interpret_capture(None, _Stream(json.dumps({
        "type": "result", "session_id": "not a session",
        "usage": {"inputTokens": 1, "outputTokens": 1, "cacheReadTokens": 1},
    })))
    assert spaced.session_ref is None
    assert spaced.usage["accuracy"] == "exact"


def test_doctor_required_sets_match_adapter_required_sets():
    from orx.adapters.codex import REQUIRED_FLAGS as codex_flags
    from orx.adapters.cursor import REQUIRED_FLAGS as cursor_flags
    from orx.probes import HARNESSES
    assert HARNESSES["codex"].required_flags == codex_flags
    assert HARNESSES["cursor"].required_flags == cursor_flags
