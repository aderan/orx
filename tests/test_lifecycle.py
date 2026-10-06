"""Full fake lifecycle (no AI provider), restart/recovery, run completion."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from orx import dispatch, verify
from orx.verify import DeliveryRejected

from conftest import active_task, ir_for, task_spec


def write_evidence(tmp_path, name: str = "evidence.json", *, status: str = "passed",
                   checks=None, artifacts=(), summary: str = "work finished"):
    """A structured delivery result (the `task complete` evidence contract):
    status/checks/artifacts/summary. The delivery gate re-runs the command
    entries itself, so an empty checks list is a legal success claim."""
    path = tmp_path / name
    path.write_text(json.dumps({
        "status": status,
        "summary": summary,
        "checks": list(checks or []),
        "artifacts": list(artifacts),
    }))
    return path


def _drive_goal_to_plan(project, goal):
    planned = dispatch.plan_route(project)
    assert planned["mode"] == "host_required"
    submit = dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance[:1], verification=["test -f t1.marker"]),
        task_spec("T002", deps=["T001"], acceptance=goal.acceptance[1:], verification=[]),
        task_spec("T003", deps=["T001"], acceptance=goal.acceptance[1:],
                  verification=["agent: the summary is honest"]),
    ]))
    assert submit["revision"] == 1
    return project


def test_full_fake_lifecycle_to_done(project, goal, tmp_path):
    _drive_goal_to_plan(project, goal)

    data = dispatch.status_data(project)
    assert data["run"]["status"] == "running"
    assert {t["id"]: t["status"] for t in data["tasks"]} == {
        "T001": "runnable", "T002": "pending", "T003": "pending",
    }

    # Route: host work is parked at waiting_host; dependents stay pending.
    slice_out = dispatch.run_slice(project)
    assert [h["task"] for h in slice_out["host_required"]] == ["T001"]
    statuses = {t["id"]: t["status"] for t in dispatch.task_list(project)}
    assert statuses["T001"] == "waiting_host"
    assert statuses["T002"] == "pending"

    # Host does T001: claim, produce the marker + evidence, complete.
    dispatch.task_claim(project, "T001")
    assert next(t for t in dispatch.task_list(project) if t["id"] == "T001")["status"] == "running"
    (project.root / "t1.marker").write_text("done")
    result = dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    assert result["status"] == "passed"

    # T001 passed -> dependents became runnable.
    dispatch.run_slice(project)
    statuses = {t["id"]: t["status"] for t in dispatch.task_list(project)}
    assert statuses["T002"] == "waiting_host" and statuses["T003"] == "waiting_host"

    # T002: empty verification list -> passes on completion.
    dispatch.task_claim(project, "T002")
    result = dispatch.task_complete(project, "T002", str(write_evidence(tmp_path, "e2.json")))
    assert result["status"] == "passed"

    # T003: agent verification -> verifying until the host submits a verdict.
    dispatch.task_claim(project, "T003")
    result = dispatch.task_complete(project, "T003", str(write_evidence(tmp_path, "e3.json")))
    assert result["status"] == "verifying"
    assert dispatch.status_data(project)["run"]["status"] == "running"

    verify_out = dispatch.verify_dispatch(project)
    assert any(e["task"] == "T003" for e in verify_out["agent_required"])
    verdict = dispatch.verify_submit(project, "T003", "pass", None, str(write_evidence(tmp_path, "v3.json")))
    assert verdict["status"] == "passed"

    data = dispatch.status_data(project)
    assert data["run"]["status"] == "done"
    assert data["goal"]["status"] == "done"
    assert all(t["status"] == "passed" for t in data["tasks"])


def test_run_blocked_when_task_fails_and_nothing_runnable(project, goal, tmp_path):
    _drive_goal_to_plan(project, goal)
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_fail(project, "T001", "cannot proceed")

    data = dispatch.status_data(project)
    assert data["run"]["status"] == "blocked"
    statuses = {t["id"]: t["status"] for t in data["tasks"]}
    assert statuses["T001"] == "failed"
    assert statuses["T002"] == "blocked"
    assert statuses["T003"] == "blocked"

    # Fix and retry: blocked dependents unblock as the dependency passes.
    dispatch.task_retry(project, "T001")
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    (project.root / "t1.marker").write_text("done")
    assert dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))["status"] == "passed"
    statuses = {t["id"]: t["status"] for t in dispatch.task_list(project)}
    assert statuses["T002"] == "runnable"
    assert dispatch.status_data(project)["run"]["status"] == "running"


def test_restart_recovery_mid_run(project, goal, tmp_path):
    _drive_goal_to_plan(project, goal)
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    (project.root / "t1.marker").write_text("done")
    before = {
        t["id"]: t["status"] for t in dispatch.task_list(project)
    }
    assert before == {"T001": "running", "T002": "pending", "T003": "pending"}
    project.close()  # "crash": a new process reopens from SQLite

    reopened = dispatch.open_project()
    try:
        assert {t["id"]: t["status"] for t in dispatch.task_list(reopened)} == before
        assert reopened.store.schema_version() == 10
        result = dispatch.task_complete(reopened, "T001", str(write_evidence(tmp_path)))
        assert result["status"] == "passed"
        dispatch.run_slice(reopened)
        dispatch.task_claim(reopened, "T002")
        dispatch.task_complete(reopened, "T002", str(write_evidence(tmp_path, "e2.json")))
        dispatch.task_claim(reopened, "T003")
        dispatch.task_complete(reopened, "T003", str(write_evidence(tmp_path, "e3.json")))
        dispatch.verify_submit(reopened, "T003", "pass", None, None)
        data = dispatch.status_data(reopened)
        assert data["run"]["status"] == "done"
    finally:
        reopened.close()


def test_status_in_a_separate_process_sees_same_state(project, goal, tmp_path):
    _drive_goal_to_plan(project, goal)
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")

    env = os.environ.copy()
    env["ORX_PROJECT"] = str(project.root)
    proc = subprocess.run(
        [sys.executable, "-m", "orx", "status", "--json"],
        capture_output=True, text=True, cwd=str(project.root), env=env,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["run"]["status"] == "running"
    statuses = {t["id"]: t["status"] for t in payload["tasks"]}
    assert statuses == {"T001": "running", "T002": "pending", "T003": "pending"}


def test_worker_contract_and_preflight_are_static(project, goal):
    """The delivery contract rides the parked assignment, and the preflight
    is static: nothing is executed, no verification row appears, and the
    state machine is untouched until the host acts. A blocked preflight row
    is visible in the prompt instead of being discovered after a wasted
    attempt."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance,
                  verification=["test -f t1.marker",
                                "orx-lifecycle-no-such-tool-3 --version"]),
    ]))
    out = dispatch.run_slice(project)
    entry = out["host_required"][0]

    text = (project.root / entry["prompt_file"]).read_text()
    assert entry["prompt"] == text
    assert "orx task check T001" in text
    assert "[preflight:ok] command 'test -f t1.marker'" in text
    assert "[preflight:blocked] command 'orx-lifecycle-no-such-tool-3 --version'" in text
    assert "BLOCKED EXIT" in text and "DELIVERY GATE" in text

    row = active_task(project, "T001")
    assert row.status == "waiting_host"
    assert project.store.verifications_for(row.revision_id, "T001") == []


def test_delivery_contract_leaves_completion_semantics_unchanged(project, goal, tmp_path):
    """The contract is prompt discipline only: completion still runs the
    prescribed checks independently, and a workspace that satisfies them
    passes exactly as before the contract existed."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["test -f t1.marker"]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    row = active_task(project, "T001")  # captured before the goal goes done
    (project.root / "t1.marker").write_text("done")
    result = dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    assert result["status"] == "passed"

    recorded = project.store.verifications_for(row.revision_id, "T001")
    assert [v.passed for v in recorded] == [True]  # complete-time check ran
    assert dispatch.status_data(project)["run"]["status"] == "done"


# -- delivery gate + structured delivery result (R002 follow-up) ------------


def _claimed_marker_task(project, goal, verification):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=verification),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    return active_task(project, "T001")


def test_complete_rejects_legacy_evidence_naming_missing_fields(project, goal, tmp_path):
    """A plain {summary, commands, artifacts} file is not a delivery result:
    the completion is refused, every missing field is named, and nothing
    changes — task, attempt, and events all stay as they were."""
    row = _claimed_marker_task(project, goal, ["true"])
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"summary": "done", "commands": [], "artifacts": []}))

    with pytest.raises(DeliveryRejected) as excinfo:
        dispatch.task_complete(project, "T001", str(legacy))
    assert excinfo.value.kind == "evidence"
    joined = "; ".join(excinfo.value.fields)
    assert "'status'" in joined and "'checks'" in joined
    assert excinfo.value.fields  # one entry per missing field

    assert active_task(project, "T001").status == "running"
    attempt = project.store.attempt_latest_for_task(row.revision_id, "T001")
    assert attempt.ended_at is None  # the attempt was not closed
    assert not [
        e for e in project.store.evidence_for_task(row.revision_id, "T001")
        if e[0] == "completion"
    ]


def test_complete_rejects_malformed_evidence_fields(project, goal, tmp_path):
    """Present-but-wrong fields are named too: a bad status value and check
    items missing exit_code/log are rejected before any state is written."""
    _claimed_marker_task(project, goal, ["true"])
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "status": "green", "summary": "  ",
        "checks": [{"command": "pytest"}], "artifacts": "none",
    }))
    with pytest.raises(DeliveryRejected) as excinfo:
        dispatch.task_complete(project, "T001", str(bad))
    fields = "; ".join(excinfo.value.fields)
    for fragment in ("'status'", "'summary'", "checks[0].exit_code", "'artifacts'"):
        assert fragment in fields

    not_json = tmp_path / "not.json"
    not_json.write_text("the work is done, trust me")
    with pytest.raises(DeliveryRejected, match="not valid JSON"):
        dispatch.task_complete(project, "T001", str(not_json))


def test_gate_rejects_red_delivery_then_recovery_completes_same_attempt(project, goal, tmp_path):
    """The R002 core failure made impossible: a red command check cannot be
    completed past. The rejection keeps the task in its original state with
    the attempt open (no new attempt, no completion record), carries the
    per-failure command/exit code/error summary/log path, and a fixed
    workspace completes green through the normal verify path."""
    row = _claimed_marker_task(project, goal, ["ls t1.marker", "true"])

    with pytest.raises(DeliveryRejected) as excinfo:
        dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    assert excinfo.value.kind == "gate"
    report = excinfo.value.report
    assert report["task"] == "T001"
    assert report["task_status"] == "running"  # original state kept
    assert report["summary"]["total"] == 2
    failure = report["failures"][0]
    assert failure["command"] == "ls t1.marker"
    assert failure["exit_code"] != 0
    assert failure["error_summary"]  # the earliest failing output line
    assert failure["log_path"].startswith(".orx/runs/R001/check/T001/")
    assert (project.root / failure["log_path"]).exists()

    # Attempt stays open; no completion event was recorded.
    attempt = project.store.attempt_latest_for_task(row.revision_id, "T001")
    assert attempt.ended_at is None and attempt.result is None
    assert not [
        e for e in project.store.task_events(row.revision_id, "T001")
        if e.event == "complete"
    ]

    # Fix in the same session and complete again: all green, same attempt.
    (project.root / "t1.marker").write_text("done")
    result = dispatch.task_complete(project, "T001", str(write_evidence(tmp_path, "e2.json")))
    assert result["status"] == "passed"
    assert result["attempt"] == attempt.id  # no new attempt was opened
    assert result["delivery"] == {"status": "passed", "checks_run": 2}
    assert dispatch.status_data(project)["run"]["status"] == "done"


def test_gate_reruns_checks_even_after_green_self_check(project, goal, tmp_path):
    """A green `task check` earlier in the session is not an exemption: the
    gate re-runs the command entries at completion, and a workspace that
    regressed between self-check and delivery is rejected."""
    _claimed_marker_task(project, goal, ["test -f t1.marker"])
    (project.root / "t1.marker").write_text("done")
    check = dispatch.task_check(project, "T001")
    assert check["summary"]["passed"] == 1

    (project.root / "t1.marker").unlink()  # regressed after the self-check
    with pytest.raises(DeliveryRejected, match="delivery gate rejected"):
        dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))


def test_blocked_delivery_is_an_independent_recorded_result(project, goal, tmp_path):
    """status=blocked fails the task as an environment/tool block — a
    distinct recorded outcome, distinguishable in the failure reason and on
    the attempt from a worker-reported code failure."""
    row = _claimed_marker_task(project, goal, ["true"])

    blocked = write_evidence(
        tmp_path, status="blocked", summary="sandbox denies all shell execution",
        checks=[{"command": "true", "exit_code": None, "log": None}],
    )
    result = dispatch.task_complete(project, "T001", str(blocked))
    assert result["status"] == "failed"
    assert result["delivery"]["status"] == "blocked"
    reason = result["delivery"]["reason"]
    assert reason.startswith("delivery blocked (environment/tool blocked):")
    assert "true (not run)" in reason  # the worker-reported not-run check
    task_row = active_task(project, "T001")
    assert task_row.status == "failed" and task_row.failure_reason == reason
    attempt = project.store.attempt_latest_for_task(row.revision_id, "T001")
    assert attempt.result == "blocked"  # independent delivery result
    assert attempt.failure_reason == reason
    fail_events = [
        e for e in project.store.task_events(row.revision_id, "T001")
        if e.event == "fail"
    ]
    assert fail_events and fail_events[-1].reason == reason

    # Contrast: a worker-reported code failure is a different, named kind.
    dispatch.task_retry(project, "T001")
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    failed = write_evidence(
        tmp_path, "e-failed.json", status="failed", summary="pytest still red",
        checks=[{"command": "pytest -q", "exit_code": 1,
                 "log": ".orx/runs/R001/check/T001/01-0000-command.log"}],
    )
    result = dispatch.task_complete(project, "T001", str(failed))
    assert result["delivery"]["status"] == "failed"
    code_reason = result["delivery"]["reason"]
    assert code_reason.startswith("delivery failed (worker-reported failure):")
    assert "environment/tool" not in code_reason
    assert active_task(project, "T001").failure_reason == code_reason


def test_gate_never_judges_agent_entries(project, goal, tmp_path):
    """The gate covers command entries only: an agent-only task completes
    without anything being executed for it, and a task with a green command
    entry plus a pending agent entry still lands in verifying — the
    independent verifier decides, a green gate never replaces it."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance,
                  verification=["test -f t1.marker", "agent: the work is honest"]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    row = active_task(project, "T001")
    (project.root / "t1.marker").write_text("done")

    result = dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    assert result["status"] == "verifying"  # agent verdict outstanding
    kinds = [v.kind for v in project.store.verifications_for(row.revision_id, "T001")]
    assert kinds == ["command"]  # the gate ran the command entry only

    verdict = dispatch.verify_submit(project, "T001", "pass", None, None)
    assert verdict["status"] == "passed"


# -- retry feedback from per-attempt verification history (R002 follow-up) ---


def test_retry_prompt_quotes_retained_failure_evidence(project, goal, tmp_path):
    """A retried worker goes straight at the recorded failure: the new
    assignment quotes the failed attempt's surviving verification rows —
    command, exit code, error summary, log path (the delivery-gate refusal
    row shape) — sourced from the per-attempt history, never re-executed
    here. A check that went green later in the same attempt is not quoted:
    the latest row per check is the state that failed."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance,
                  verification=["ls t1.marker", "true"]),
    ]))
    first = dispatch.run_slice(project)["host_required"][0]
    assert "A previous attempt" not in first["prompt"]  # no history yet

    dispatch.task_claim(project, "T001")
    check = dispatch.task_check(project, "T001")  # marker missing -> one red row
    assert check["summary"]["failed"] == 1
    failed_attempt = check["attempt"]
    dispatch.task_fail(project, "T001", "could not make the check green")
    dispatch.task_retry(project, "T001")

    entry = dispatch.run_slice(project)["host_required"][0]
    prompt = entry["prompt"]
    assert (project.root / entry["prompt_file"]).read_text() == prompt

    assert "A previous attempt at this task FAILED with:" in prompt
    assert "could not make the check green" in prompt
    assert f"(attempt {failed_attempt}; latest row per check)" in prompt
    # The gate-refusal row shape: command / exit code / error summary / log.
    assert "  - command: ls t1.marker" in prompt
    assert "    exit code: 1" in prompt
    assert "    error summary: ls: t1.marker: No such file or directory" in prompt
    assert "    log: .orx/runs/R001/check/T001/01-0000-command.log" in prompt
    log_line = next(
        line.strip() for line in prompt.splitlines() if line.strip().startswith("log:")
    )
    assert (project.root / log_line[len("log: "):]).exists()
    assert "  - command: true" not in prompt  # the green check is not quoted

    # Same attempt, check -> fix -> check: the entry's latest row is green,
    # so a later failure of that attempt no longer quotes the stale red row.
    dispatch.task_claim(project, "T001")
    (project.root / "t1.marker").write_text("done")
    green = dispatch.task_check(project, "T001")
    assert green["summary"]["passed"] == 2
    dispatch.task_fail(project, "T001", "abandoned for an unrelated reason")
    dispatch.task_retry(project, "T001")
    stale = dispatch.run_slice(project)["host_required"][0]["prompt"]
    assert "abandoned for an unrelated reason" in stale
    assert "Verification evidence retained" not in stale


def test_retry_guidance_classifies_blocked_vs_code_failures(project, goal, tmp_path):
    """An environment/tool block and a worker-reported code failure carry
    DIFFERENT repair guidance in the retried assignment: the block says fix
    the environment or report that routing must change; the code failure
    says go directly at the failing checks. Both quote the same retained
    evidence rows — only the classified guidance differs."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance,
                  verification=["orx-retry-no-such-tool --version"]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    assert dispatch.task_check(project, "T001")["summary"]["failed"] == 1

    blocked = write_evidence(
        tmp_path, status="blocked", summary="tool missing from sandbox",
        checks=[{"command": "orx-retry-no-such-tool --version",
                 "exit_code": 127, "log": None}],
    )
    result = dispatch.task_complete(project, "T001", str(blocked))
    assert result["delivery"]["status"] == "blocked"
    dispatch.task_retry(project, "T001")
    blocked_prompt = dispatch.run_slice(project)["host_required"][0]["prompt"]

    assert "delivery blocked (environment/tool blocked): tool missing from sandbox" \
        in blocked_prompt
    assert "environment/tool block, not a code defect" in blocked_prompt
    assert "routing needs to change" in blocked_prompt
    assert "go directly to the failing" not in blocked_prompt
    # The retained row is quoted for both classes.
    assert "  - command: orx-retry-no-such-tool --version" in blocked_prompt

    dispatch.task_claim(project, "T001")
    dispatch.task_check(project, "T001")  # still red
    failed = write_evidence(
        tmp_path, "e-code.json", status="failed", summary="check still red",
        checks=[{"command": "orx-retry-no-such-tool --version",
                 "exit_code": 127, "log": None}],
    )
    result = dispatch.task_complete(project, "T001", str(failed))
    assert result["delivery"]["status"] == "failed"
    dispatch.task_retry(project, "T001")
    code_prompt = dispatch.run_slice(project)["host_required"][0]["prompt"]

    assert "delivery failed (worker-reported failure): check still red" in code_prompt
    assert "go directly to the failing check(s) listed above" in code_prompt
    assert "environment/tool block, not a code defect" not in code_prompt
    assert "routing needs to change" not in code_prompt

    # The two classes never render the same guidance text.
    blocked_slice = blocked_prompt.split("FAILED with:")[1]
    code_slice = code_prompt.split("FAILED with:")[1]
    assert blocked_slice != code_slice


def test_retry_prompt_without_retained_history_matches_legacy_form(project, goal):
    """A failure with no surviving verification rows behaves exactly as
    before per-attempt history existed: the retry assignment carries the
    failure reason line only — byte-compatible legacy block, no evidence
    section, no classified guidance."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance,
                  verification=["test -f t1.marker"]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_fail(project, "T001", "cannot proceed further")
    dispatch.task_retry(project, "T001")
    prompt = dispatch.run_slice(project)["host_required"][0]["prompt"]

    legacy_block = (
        "\nA previous attempt at this task FAILED with:\n"
        "  cannot proceed further\n"
        "Fix that specific problem; do not redo the task blindly.\n"
    )
    assert legacy_block in prompt  # byte-compatible with the pre-history form
    assert "Verification evidence retained" not in prompt
    assert "environment/tool block" not in prompt
    assert "go directly to the failing" not in prompt


# -- same-session check-fix loop budget (R002 follow-up) ----------------------


def test_task_check_accumulates_rounds_on_the_attempt(project, goal):
    """`orx task check` rounds accumulate on the worker attempt: every run
    reports rounds used vs the configured budget (this run included), the
    count grows across calls on the same attempt, task list exposes it
    without running anything, and a retry's fresh attempt starts from zero."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["test -f t1.marker"]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    row = active_task(project, "T001")
    attempt = project.store.attempt_latest_for_task(row.revision_id, "T001")

    first = dispatch.task_check(project, "T001")
    assert first["check_rounds"] == {
        "attempt": attempt.id, "used": 1, "budget": 3,
        "remaining": 2, "exceeded": False,
    }
    second = dispatch.task_check(project, "T001")
    assert second["check_rounds"]["attempt"] == attempt.id
    assert second["check_rounds"]["used"] == 2

    listed = next(t for t in dispatch.task_list(project) if t["id"] == "T001")
    assert listed["check_rounds"] == {"used": 2, "budget": 3}

    # A retry routes a fresh attempt: the budget is per-attempt, not per-task.
    dispatch.task_fail(project, "T001", "budget exhausted, red checks remain")
    dispatch.task_retry(project, "T001")
    dispatch.run_slice(project)
    fresh_row = active_task(project, "T001")
    fresh_attempt = project.store.attempt_latest_for_task(fresh_row.revision_id, "T001")
    assert fresh_attempt.id != attempt.id
    restarted = dispatch.task_check(project, "T001")
    assert restarted["check_rounds"] == {
        "attempt": fresh_attempt.id, "used": 1, "budget": 3,
        "remaining": 2, "exceeded": False,
    }


def test_check_budget_configurable_exhaustion_and_gate_report(tmp_path, monkeypatch):
    """worker.max_check_rounds bounds what `orx task check` reports and what
    the worker prompt prescribes: with a budget of 1 the first round leaves
    zero remaining, the second is flagged exceeded, and a delivery-gate
    rejection carries the same rounds view (gate run included) so an
    exhausted budget routes to a structured exit instead of another loop."""
    from conftest import HOST_CONFIG_TOML, make_project

    root = tmp_path / "tight"
    root.mkdir()
    monkeypatch.chdir(root)
    project = make_project(root, config_toml=HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]',
        'profiles = ["host-worker", "host-external"]\nmax_check_rounds = 1',
    ))
    try:
        goal = dispatch.create_goal(project, "tight budget", ["criterion"], [], "")[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["criterion"],
                      verification=["test -f t1.marker"]),
        ]))
        prompt = dispatch.run_slice(project)["host_required"][0]["prompt"]
        assert "CHECK-FIX LOOP (budget: 1 check round(s) on this attempt)" in prompt

        dispatch.task_claim(project, "T001")
        row = active_task(project, "T001")
        attempt = project.store.attempt_latest_for_task(row.revision_id, "T001")
        first = dispatch.task_check(project, "T001")
        assert first["check_rounds"] == {
            "attempt": attempt.id, "used": 1, "budget": 1,
            "remaining": 0, "exceeded": False,
        }
        second = dispatch.task_check(project, "T001")
        assert second["check_rounds"]["used"] == 2
        assert second["check_rounds"]["exceeded"] is True
        assert second["check_rounds"]["remaining"] == 0

        # A passed-claim completion on a red workspace is gate-rejected, and
        # the rejection report shows where the gate round left the budget.
        with pytest.raises(DeliveryRejected) as excinfo:
            dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
        assert excinfo.value.kind == "gate"
        assert excinfo.value.report["check_rounds"] == {
            "attempt": attempt.id, "used": 3, "budget": 1,
            "remaining": 0, "exceeded": True,
        }
    finally:
        project.close()


def test_worker_prompt_carries_check_fix_budget_rules(project, goal):
    """The assignment prescribes the loop contract: repair in-session within
    the configured budget by check -> fix -> check, expected-red (TDD)
    checks never justify a restart, full checks run at stage boundaries —
    not after every edit — and an exhausted budget with red checks exits
    structured failed/blocked back to the Controller."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["test -f t1.marker"]),
    ]))
    prompt = dispatch.run_slice(project)["host_required"][0]["prompt"]

    assert "CHECK-FIX LOOP (budget: 3 check round(s) on this attempt)" in prompt
    assert "orx task check T001" in prompt
    assert "check_rounds" in prompt  # the observability the loop reads
    assert "check -> fix -> check" in prompt  # same-session repair
    assert "TDD" in prompt
    assert "do not\n   restart the task" in prompt
    assert "stage boundaries" in prompt
    assert "not after every edit" in prompt
    assert "structured failed/blocked delivery result" in prompt
    assert "Controller decides the next round" in prompt
    # Integrated into the delivery contract: after BLOCKED EXIT, before the
    # delivery gate — an extension of the same contract, not a second one.
    assert (
        prompt.index("BLOCKED EXIT")
        < prompt.index("CHECK-FIX LOOP")
        < prompt.index("DELIVERY GATE")
    )


# -- host worker progress reports (G006, docs/host-progress-contract.md §2-§5) --


def _heartbeat_setup(project, goal, verification=("true",)):
    """Active revision + one claimed running task; returns (row, attempt)."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance[:1], verification=list(verification)),
        task_spec("T002", deps=["T001"], acceptance=goal.acceptance[1:], verification=[]),
    ]))
    dispatch.run_slice(project)
    claim = dispatch.task_claim(project, "T001")
    row = active_task(project, "T001")
    attempt = project.store.attempt_get(claim["attempt"])
    return row, attempt


def _heartbeat_snapshot(project, revision_row_id, task_id):
    """Everything a heartbeat must never touch: task status and events,
    verification rows, attempt fields (session_ref included), usage rows,
    and the progress rows themselves (for rejection assertions)."""
    store = project.store
    return {
        "status": store.task_get(revision_row_id, task_id).status,
        "events": [(e.event, e.reason) for e in store.task_events(revision_row_id, task_id)],
        "verifications": [
            (v.id, v.attempt_id, v.passed)
            for v in store.verifications_for(revision_row_id, task_id)
        ],
        "attempts": [
            (a.id, a.started_at, a.ended_at, a.result, a.session_ref,
             a.actual_model, a.usage_missing_reason)
            for a in store.attempts_all()
        ],
        "usage": store.conn.execute(
            "SELECT COUNT(*) AS c FROM usage_observations").fetchone()["c"],
        "progress": store.conn.execute(
            "SELECT COUNT(*) AS c FROM attempt_progress").fetchone()["c"],
    }


def test_heartbeat_appends_reports_that_survive_reopen(project, goal):
    """A running task's current attempt accepts structured reports; the
    response carries every contract §5 field; append order survives a full
    close/reopen; and a not-yet-reporting attempt is honestly unknown (no
    claim-time or any other substitute is fabricated)."""
    row, attempt = _heartbeat_setup(project, goal)

    # Before any report: unknown, never back-filled from the claim.
    assert project.store.attempt_progress_latest(attempt.id) is None
    assert project.store.attempt_progress_all(attempt.id) == []

    first = dispatch.task_heartbeat(
        project, "T001", attempt.id, "  exploring  ", "   ",
    )
    assert first == {
        "task": "T001",
        "attempt": attempt.id,
        "sequence": 1,
        "phase": "exploring",          # stripped, otherwise untouched text
        "message": None,               # whitespace-only message == omitted
        "received_at": first["received_at"],
    }
    assert first["received_at"].endswith("+00:00") and "T" in first["received_at"]

    second = dispatch.task_heartbeat(
        project, "T001", attempt.id, "implementing", "half done",
    )
    assert second["sequence"] == 2
    assert second["phase"] == "implementing"
    assert second["message"] == "half done"
    assert second["received_at"] >= first["received_at"]

    # Observation only: task machine and attempt untouched.
    after = _heartbeat_snapshot(project, row.revision_id, "T001")
    assert after["status"] == "running"
    assert after["attempts"][-1][:5] == (
        attempt.id, attempt.started_at, None, None, attempt.session_ref,
    )
    assert after["progress"] == 2

    project.close()  # "crash": a new process reopens from SQLite
    reopened = dispatch.open_project()
    try:
        run = reopened.store.runs_all()[-1]
        revision = reopened.store.revision_active(run.id)
        reopened_attempt = reopened.store.attempt_latest_for_task(revision.id, "T001")
        assert reopened_attempt.id == attempt.id
        latest = reopened.store.attempt_progress_latest(reopened_attempt.id)
        assert (latest.sequence, latest.phase, latest.message) == (2, "implementing", "half done")
        history = reopened.store.attempt_progress_all(reopened_attempt.id)
        assert [(r.sequence, r.phase, r.message) for r in history] == [
            (1, "exploring", None),
            (2, "implementing", "half done"),
        ]
        assert reopened.store.task_get(revision.id, "T001").status == "running"
    finally:
        reopened.close()


def test_heartbeat_received_at_comes_from_the_orx_clock(project, goal, monkeypatch):
    """received_at is generated by ORX's UTC clock at receive time (the same
    seam as every other stored timestamp) — the caller has no way to supply
    a client timestamp through the interface."""
    row, attempt = _heartbeat_setup(project, goal)
    fixed = "2026-10-06T02:00:00.123456+00:00"
    monkeypatch.setattr(dispatch, "db_now", lambda: fixed)

    result = dispatch.task_heartbeat(project, "T001", attempt.id, "checking")
    assert result["received_at"] == fixed
    stored = project.store.attempt_progress_latest(attempt.id)
    assert stored.received_at == fixed


def test_heartbeat_input_bounds_are_usage_errors_before_any_lookup(project, goal):
    """Contract §3: strip first, then count Unicode characters. phase 1-64,
    message 0-512, whitespace-only message counts as omitted. Bounds
    violations raise input-validation errors (reason phase_invalid /
    message_invalid) — and they fire before any identity lookup, so a misuse
    is distinguishable from an identity/state refusal even when both apply."""
    row, attempt = _heartbeat_setup(project, goal)

    def rejects(phase, message, reason):
        with pytest.raises(Exception) as excinfo:
            dispatch.task_heartbeat(project, "T001", attempt.id, phase, message)
        assert getattr(excinfo.value, "reason", None) == reason, (
            f"{phase[:12]!r}/{message and message[:12]!r}: "
            f"expected {reason}, got {getattr(excinfo.value, 'reason', None)}"
        )

    rejects("   ", None, "phase_invalid")            # all-whitespace phase
    rejects("", None, "phase_invalid")               # empty phase
    rejects("x" * 65, None, "phase_invalid")         # one over the bound
    rejects("ok", "y" * 513, "message_invalid")      # one over the bound
    rejects(None, None, "phase_invalid")             # phase is required

    # Misuse precedes identity: a bad phase is reported even when the
    # attempt id is also bogus (exit-2 class wins over exit-1 class).
    with pytest.raises(Exception) as excinfo:
        dispatch.task_heartbeat(project, "T001", 999999, "   ", None)
    assert getattr(excinfo.value, "reason", None) == "phase_invalid"

    # Boundaries are inclusive: 64-char phase and 512-char message pass.
    ok_phase = "p" * 64
    ok_message = "m" * 512
    result = dispatch.task_heartbeat(
        project, "T001", attempt.id, ok_phase, ok_message,
    )
    assert result["phase"] == ok_phase
    assert result["message"] == ok_message

    # Unicode text is bounded by characters, not bytes.
    wide = dispatch.task_heartbeat(project, "T001", attempt.id, "实现" * 32)
    assert len(wide["phase"]) == 64

    # No progress row was written by any rejected call above (only the two
    # accepted boundary reports exist).
    assert _heartbeat_snapshot(project, row.revision_id, "T001")["progress"] == 2


def test_heartbeat_ownership_gates_follow_the_contract_table(project, goal):
    """Every exit-1 row of contract §5, each with its machine reason, and
    each rejection leaving the database byte-identical: no progress row, no
    task event, no attempt change, no verification, no usage. The foreign
    attempts below are test ARRANGEMENT (created/closed directly through
    the store); each baseline is taken after the arrangement so the
    comparison proves the rejected heartbeat itself wrote nothing."""
    from orx.records import ConflictError, NotFoundError

    row, attempt = _heartbeat_setup(project, goal)
    store = project.store
    revision_id = row.revision_id

    # An accepted report first, so rejections can prove they add nothing.
    dispatch.task_heartbeat(project, "T001", attempt.id, "checking", "one report")

    def rejected(task_id, attempt_id, reason, exc_type=ConflictError):
        with pytest.raises(exc_type) as excinfo:
            dispatch.task_heartbeat(project, task_id, attempt_id, "checking")
        assert getattr(excinfo.value, "reason", None) == reason
        assert str(attempt_id) in str(excinfo.value) or task_id in str(excinfo.value)
        assert _heartbeat_snapshot(project, revision_id, "T001") == baseline

    # task_not_found: the task is not one of the active revision.
    baseline = _heartbeat_snapshot(project, revision_id, "T001")
    rejected("T999", attempt.id, "task_not_found", NotFoundError)

    # task_not_running: T002 never left pending (gate fires before identity,
    # so even the right-shaped attempt id cannot report for it).
    rejected("T002", attempt.id, "task_not_running")

    # attempt_not_found.
    rejected("T001", 999999, "attempt_not_found", NotFoundError)

    # attempt_not_host_worker: a planner attempt and a cli-driver worker.
    planner = store.attempt_create(
        revision_id, "planner", "host-planner", "host", "zcode", "m", "high",
    )
    baseline = _heartbeat_snapshot(project, revision_id, "T001")
    rejected("T001", planner.id, "attempt_not_host_worker")
    cli_worker = store.attempt_create(
        revision_id, "worker", "cli-fake", "cli", "shell", "m", "low",
        task_id="T001",
    )
    baseline = _heartbeat_snapshot(project, revision_id, "T001")
    rejected("T001", cli_worker.id, "attempt_not_host_worker")

    # attempt_foreign: an attempt of another task in the same revision.
    foreign = store.attempt_create(
        revision_id, "worker", "host-worker", "host", "zcode", "m", "medium",
        task_id="T002",
    )
    baseline = _heartbeat_snapshot(project, revision_id, "T001")
    rejected("T001", foreign.id, "attempt_foreign")

    # attempt_superseded: a newer attempt owns the window while the claimed
    # one is still open — the report must not leak into the new window.
    newer = store.attempt_create(
        revision_id, "worker", "host-worker", "host", "zcode", "m", "medium",
        task_id="T001",
    )
    baseline = _heartbeat_snapshot(project, revision_id, "T001")
    rejected("T001", attempt.id, "attempt_superseded")
    assert store.attempt_progress_all(newer.id) == []

    # attempt_closed: the newest attempt (still the latest) closed while
    # the task keeps running.
    store.attempt_update(newer.id, ended_at=dispatch.db_now(), result="completed")
    baseline = _heartbeat_snapshot(project, revision_id, "T001")
    rejected("T001", newer.id, "attempt_closed")

    # The final baseline itself: exactly one report, nothing else moved.
    assert baseline["progress"] == 1
    assert baseline["usage"] == 0
    assert baseline["verifications"] == []


def test_heartbeat_after_fail_and_retry_only_the_new_attempt_reports(project, goal):
    """Contract §10: an explicit fail + retry opens a new attempt window —
    the old attempt's reports are history and its further reports (and any
    attempt to reach it) are refused, while the new attempt starts fresh."""
    row, attempt = _heartbeat_setup(project, goal)
    dispatch.task_heartbeat(project, "T001", attempt.id, "implementing", "mid work")
    dispatch.task_fail(project, "T001", "original worker confirmed dead")
    dispatch.task_retry(project, "T001")
    dispatch.run_slice(project)
    claim = dispatch.task_claim(project, "T001")
    fresh = claim["attempt"]
    assert fresh != attempt.id

    with pytest.raises(Exception) as excinfo:
        dispatch.task_heartbeat(project, "T001", attempt.id, "delivering")
    assert getattr(excinfo.value, "reason", None) in (
        "attempt_closed", "attempt_superseded",
    )

    # The new window starts at unknown and sequence 1; the old attempt's
    # report never leaks into it.
    store = project.store
    assert store.attempt_progress_latest(fresh) is None
    first = dispatch.task_heartbeat(project, "T001", fresh, "exploring")
    assert first["sequence"] == 1
    assert [(r.sequence, r.phase) for r in store.attempt_progress_all(fresh)] == [
        (1, "exploring"),
    ]
    assert [(r.sequence, r.phase) for r in store.attempt_progress_all(attempt.id)] == [
        (1, "implementing"),
    ]


def test_heartbeat_racing_completion_never_revives_the_attempt(project, goal, tmp_path):
    """Both safe orders of the complete/report race: a report first becomes
    history and the completion closes normally; a completion first closes
    the attempt and the late report is refused — never a half-write state."""
    row, attempt = _heartbeat_setup(project, goal)

    # heartbeat first, completion second: both land, report is history.
    dispatch.task_heartbeat(project, "T001", attempt.id, "delivering", "done, submitting")
    result = dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    assert result["status"] == "passed"
    assert result["attempt"] == attempt.id
    history = project.store.attempt_progress_all(attempt.id)
    assert [(r.sequence, r.phase) for r in history] == [(1, "delivering")]

    # completion first, heartbeat second: closed attempts stay closed.
    with pytest.raises(Exception) as excinfo:
        dispatch.task_heartbeat(project, "T001", attempt.id, "delivering", "late")
    assert getattr(excinfo.value, "reason", None) in (
        "attempt_closed", "task_not_running",
    )
    assert len(project.store.attempt_progress_all(attempt.id)) == 1


def test_heartbeat_never_touches_session_identity_or_usage(project, goal, monkeypatch):
    """heartbeat reads no session from anywhere: the environment's
    ORX_SESSION_REF is the Controller's, not the worker's, and a report
    must not overwrite the worker's bound ref (or fill a NULL one). It also
    consumes no check budget: rounds and usage stay exactly as they were."""
    monkeypatch.setenv("ORX_SESSION_REF", "sess_controller_env")

    row, attempt = _heartbeat_setup(project, goal)
    store = project.store

    # A bound worker ref survives reports verbatim.
    store.attempt_update(attempt.id, session_ref="sess_worker_own")
    dispatch.task_heartbeat(project, "T001", attempt.id, "checking")
    assert store.attempt_get(attempt.id).session_ref == "sess_worker_own"

    # A NULL ref stays NULL: the env value never leaks in through a report.
    dispatch.task_heartbeat(project, "T001", attempt.id, "checking")
    dispatch.task_heartbeat(project, "T001", attempt.id, "checking", "note")
    assert store.attempt_get(attempt.id).session_ref == "sess_worker_own"

    # Reports consume no check budget and record no usage.
    dispatch.task_check(project, "T001")  # one real round for contrast
    rounds_before = dispatch.task_check(project, "T001")["check_rounds"]["used"]
    dispatch.task_heartbeat(project, "T001", attempt.id, "checking", "after rounds")
    rounds_after = dispatch.attempt_check_rounds(
        store, attempt.id, store.task_get(row.revision_id, "T001"),
    )
    assert rounds_after == rounds_before
    assert store.conn.execute(
        "SELECT COUNT(*) AS c FROM usage_observations").fetchone()["c"] == 0

