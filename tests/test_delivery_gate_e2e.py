"""End-to-end regression of the R002 delivery-protocol follow-up.

Each of the three R002 failure classes is replayed as ONE continuous story
through the dispatch API in a throwaway project, asserting the walk of the
new machinery — not just its pieces:

(a) a worker whose prescribed checks cannot run learns it BEFORE
    implementing: the parked assignment carries the static preflight with
    the blocked row visible, the prompt mandates the BLOCKED EXIT, and an
    obedient worker delivers ``status=blocked`` — recorded as an
    environment/tool block, no success ever claimed;
(b) a first delivery with a red command check is rejected by the delivery
    gate (task keeps its status, attempt stays open), and the same-session
    check -> fix -> check -> complete loop lands all green with NO new
    attempt created at any point of the story;
(c) a gate-red the worker cannot fix exits as a structured failed
    delivery; the retry deletes nothing — the old attempt's verification
    rows stay queryable per attempt with their logs on disk — and the new
    assignment quotes the failure rows plus repair guidance classified as
    a code failure.

A final section pins the skill documents to the machinery: the evidence
contract they teach is the one the gate actually enforces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orx import dispatch, verify
from orx.verify import DeliveryRejected

from conftest import active_task, ir_for, task_spec, write_evidence

REPO_ROOT = Path(__file__).resolve().parent.parent

# A first token that exists on no machine: the static preflight must flag
# it, and actually running it exits 127 everywhere.
MISSING_TOOL = "orx-e2e-r002-no-such-tool"


def _worker_attempt_count(project, revision_id: int, task_id: str) -> int:
    """How many worker attempts exist for the task — the 'no new attempt'
    assertion of the same-session repair story."""
    row = project.store.conn.execute(
        "SELECT COUNT(*) FROM attempts"
        " WHERE revision_id = ? AND task_id = ? AND role = 'worker'",
        (revision_id, task_id),
    ).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Scenario (a): preflight finds the environment unavailable before any work


def test_scenario_a_preflight_blocked_exit_without_investing_work(project, goal, tmp_path):
    """R002 class 1 replay — the worker that could not run its checks yet
    implemented, finished, and reported success. The walk is now: the parked
    assignment shows the blocked preflight row, the prompt orders the BLOCKED
    EXIT right after the START GATE, the worker's start gate confirms the
    tool is really missing (exit 127), and the blocked delivery is recorded
    as an environment/tool block."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T101", acceptance=goal.acceptance,
                  verification=["true", f"{MISSING_TOOL} --version"]),
    ]))
    entry = dispatch.run_slice(project)["host_required"][0]

    # The assignment payload carries the static preflight: the runnable
    # entry is ok, the missing tool is flagged blocked — before anything
    # was executed anywhere.
    rows = {r["raw"]: r for r in entry["preflight"]}
    assert rows["true"]["blocked"] is False
    missing = rows[f"{MISSING_TOOL} --version"]
    assert missing["kind"] == "command"
    assert missing["blocked"] is True
    assert missing["token"] == MISSING_TOOL
    assert missing["token_status"] == "missing"

    prompt = (project.root / entry["prompt_file"]).read_text()
    assert prompt == entry["prompt"]
    assert "[preflight:ok] command 'true'" in prompt
    assert f"[preflight:blocked] command '{MISSING_TOOL} --version'" in prompt
    assert "not on PATH" in prompt
    assert "report blocked immediately if it cannot run" in prompt
    assert prompt.index("START GATE") < prompt.index("BLOCKED EXIT") < prompt.index("DELIVERY GATE")

    # The obedient worker: claim, start gate proves the block is real, then
    # the blocked exit — no implementation, no success claim.
    dispatch.task_claim(project, "T101")
    row = active_task(project, "T101")
    gate = dispatch.task_check(project, "T101")
    missing_result = next(
        r for r in gate["results"] if r["command"] == f"{MISSING_TOOL} --version"
    )
    assert missing_result["passed"] is False
    assert missing_result["exit_code"] == 127

    blocked = write_evidence(
        tmp_path, status="blocked",
        summary=f"first token {MISSING_TOOL!r} not on PATH; the prescribed"
                " check cannot run in this environment",
        checks=[
            {"command": "true", "exit_code": 0, "log": gate["results"][0]["log_path"]},
            {"command": f"{MISSING_TOOL} --version", "exit_code": 127,
             "log": missing_result["log_path"]},
        ],
    )
    result = dispatch.task_complete(project, "T101", str(blocked))
    assert result["status"] == "failed"
    assert result["delivery"]["status"] == "blocked"
    reason = result["delivery"]["reason"]
    assert reason.startswith("delivery blocked (environment/tool blocked):")
    assert f"{MISSING_TOOL} --version (exit 127)" in reason  # the reported check

    task_row = active_task(project, "T101")
    assert task_row.status == "failed" and task_row.failure_reason == reason
    attempt = project.store.attempt_latest_for_task(row.revision_id, "T101")
    assert attempt.result == "blocked"  # an independent recorded delivery result
    assert attempt.failure_reason == reason

    # What the R002 worker actually did — implement anyway and claim
    # success — is no longer reachable: a passed claim on this workspace
    # would be gate-rejected because the missing-tool entry can only be red.
    dispatch.task_retry(project, "T101")
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T101")
    with pytest.raises(DeliveryRejected) as excinfo:
        dispatch.task_complete(project, "T101", str(write_evidence(tmp_path, "ta-lie.json")))
    assert excinfo.value.kind == "gate"
    assert any(
        f["command"] == f"{MISSING_TOOL} --version" for f in excinfo.value.report["failures"]
    )
    assert active_task(project, "T101").status == "running"  # rejection, not trust


# ---------------------------------------------------------------------------
# Scenario (b): gate rejects red, same-session fix, re-complete all green


def test_scenario_b_gate_reject_then_same_attempt_fix_all_green(project, goal, tmp_path):
    """R002 class 2 replay — a red first delivery. The gate rejects it with
    the task's prior status kept and the attempt open; the worker repairs in
    the SAME session (check -> fix -> check) and completes again — all green,
    on the same attempt, with no new attempt created anywhere in the story."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T102", acceptance=goal.acceptance,
                  verification=["ls b.marker", "true"]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T102")
    row = active_task(project, "T102")
    attempt = project.store.attempt_latest_for_task(row.revision_id, "T102")

    # Check round 1 (the START GATE): one red row, bound to the attempt.
    first = dispatch.task_check(project, "T102")
    assert first["summary"] == {"total": 2, "passed": 1, "failed": 1, "denied": 0}
    assert first["check_rounds"] == {
        "attempt": attempt.id, "used": 1, "budget": 3,
        "remaining": 2, "exceeded": False,
    }
    red = first["results"][0]
    assert red["command"] == "ls b.marker"
    assert red["exit_code"] == 1
    assert red["error_summary"] == "ls: b.marker: No such file or directory"

    # Premature passed claim: the gate re-runs the entries itself and
    # rejects — nothing closes, nothing is recorded as delivered.
    with pytest.raises(DeliveryRejected) as excinfo:
        dispatch.task_complete(project, "T102", str(write_evidence(tmp_path)))
    assert excinfo.value.kind == "gate"
    report = excinfo.value.report
    assert report["task"] == "T102"
    assert report["task_status"] == "running"  # prior status kept
    assert report["attempt"] == attempt.id  # the attempt stays open
    assert report["summary"]["total"] == 2
    assert len(report["failures"]) == 1
    failure = report["failures"][0]
    assert failure["command"] == "ls b.marker"
    assert failure["exit_code"] == 1
    assert failure["error_summary"] == "ls: b.marker: No such file or directory"
    assert failure["log_path"].startswith(".orx/runs/R001/check/T102/")
    assert (project.root / failure["log_path"]).exists()
    assert report["check_rounds"] == {
        "attempt": attempt.id, "used": 2, "budget": 3,
        "remaining": 1, "exceeded": False,
    }  # the gate round is counted

    assert active_task(project, "T102").status == "running"
    attempt_now = project.store.attempt_latest_for_task(row.revision_id, "T102")
    assert attempt_now.id == attempt.id
    assert attempt_now.ended_at is None and attempt_now.result is None
    assert not [
        e for e in project.store.task_events(row.revision_id, "T102")
        if e.event == "complete"
    ]
    assert _worker_attempt_count(project, row.revision_id, "T102") == 1

    # Same session: fix the workspace, check again (round 3, budget edge),
    # then complete — every prescribed check green.
    (project.root / "b.marker").write_text("done")
    second = dispatch.task_check(project, "T102")
    assert second["summary"] == {"total": 2, "passed": 2, "failed": 0, "denied": 0}
    assert second["check_rounds"] == {
        "attempt": attempt.id, "used": 3, "budget": 3,
        "remaining": 0, "exceeded": False,
    }
    result = dispatch.task_complete(project, "T102", str(write_evidence(tmp_path, "tb2.json")))
    assert result["status"] == "passed"
    assert result["verdict"] == "passed"
    assert result["attempt"] == attempt.id  # same attempt answered both deliveries
    assert result["delivery"] == {"status": "passed", "checks_run": 2}

    # 全程无新 attempt: the whole reject -> fix -> re-complete story ran on
    # the single worker attempt the claim opened.
    assert _worker_attempt_count(project, row.revision_id, "T102") == 1
    assert dispatch.status_data(project)["run"]["status"] == "done"

    # Per-attempt history: every check of the loop was appended (2 entries
    # x 4 rounds: check, gate-reject, check, gate-pass) and the current
    # judgment reads only the latest row per entry — green.
    rows = project.store.verifications_for_attempt(attempt.id)
    assert len(rows) == 8
    assert all(v.attempt_id == attempt.id for v in rows)
    ls_rows = [v for v in rows if v.command == "ls b.marker"]
    assert [v.passed for v in ls_rows] == [False, False, True, True]
    latest = {
        v.command: v.passed
        for v in project.store.verifications_current(row.revision_id, "T102")
        if v.kind == "command"
    }
    assert latest == {"ls b.marker": True, "true": True}
    task_row = project.store.task_get(row.revision_id, "T102")  # goal is done
    assert verify.evaluate(project.store, row.revision_id, task_row) == "passed"
    assert verify.first_failure(project.store, row.revision_id, task_row) is None


# ---------------------------------------------------------------------------
# Scenario (c): gate red -> structured exit -> retry keeps history + guidance


def test_scenario_c_retry_preserves_attempt_history_and_classifies(project, goal, tmp_path):
    """R002 class 3 replay — the failed round's evidence lost on retry. The
    walk is now: the gate-red the worker cannot fix exits as a structured
    failed delivery; the retry deletes nothing (the old attempt's rows stay
    queryable per attempt, logs on disk included); the next assignment
    quotes the failure rows verbatim plus guidance classified as a code
    failure; and the retried round's current judgment never reads the old
    attempt's red rows."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T103", acceptance=goal.acceptance,
                  verification=["ls c.marker", "true"]),
    ]))
    first_park = dispatch.run_slice(project)["host_required"][0]
    assert "A previous attempt" not in first_park["prompt"]  # no history yet

    dispatch.task_claim(project, "T103")
    row = active_task(project, "T103")
    attempt_a = project.store.attempt_latest_for_task(row.revision_id, "T103")

    # Gate red: rejected, the gate's rows recorded bound to attempt A.
    with pytest.raises(DeliveryRejected):
        dispatch.task_complete(project, "T103", str(write_evidence(tmp_path)))
    gate_rows = project.store.verifications_for_attempt(attempt_a.id)
    assert len(gate_rows) == 2
    assert [(v.command, v.passed) for v in gate_rows] == [
        ("ls c.marker", False), ("true", True),
    ]

    # The worker cannot fix it and exits structured failed (budget spent).
    failed = write_evidence(
        tmp_path, "tc-failed.json", status="failed",
        summary="cannot produce c.marker within this scope",
        checks=[{"command": "ls c.marker", "exit_code": 1,
                 "log": gate_rows[0].output_path}],
    )
    result = dispatch.task_complete(project, "T103", str(failed))
    assert result["status"] == "failed"
    assert result["delivery"]["status"] == "failed"
    assert result["delivery"]["reason"].startswith(
        "delivery failed (worker-reported failure):"
    )
    assert active_task(project, "T103").failure_reason == result["delivery"]["reason"]

    # Retry: nothing is deleted — the old attempt's rows and logs survive.
    dispatch.task_retry(project, "T103")
    assert project.store.verifications_for_attempt(attempt_a.id) == gate_rows
    assert (project.root / gate_rows[0].output_path).exists()
    assert (project.root / gate_rows[1].output_path).exists()

    # The new assignment quotes the failure rows and classified guidance.
    entry = dispatch.run_slice(project)["host_required"][0]
    prompt = entry["prompt"]
    assert (project.root / entry["prompt_file"]).read_text() == prompt
    assert "A previous attempt at this task FAILED with:" in prompt
    assert "delivery failed (worker-reported failure): cannot produce c.marker within this scope" in prompt
    assert (
        f"Verification evidence retained from that failed attempt"
        f" (attempt {attempt_a.id}; latest row per check):" in prompt
    )
    assert "  - command: ls c.marker" in prompt
    assert "    exit code: 1" in prompt
    assert "    error summary: ls: c.marker: No such file or directory" in prompt
    log_line = next(
        line.strip() for line in prompt.splitlines() if line.strip().startswith("log:")
    )
    assert (project.root / log_line[len("log: "):]).exists()
    # Classified guidance: a code failure, never the environment-block text.
    assert "go directly to the failing check(s) listed above" in prompt
    assert "environment/tool block, not a code defect" not in prompt
    assert "  - command: true" not in prompt  # the green check is not quoted

    # The fresh attempt opens a clean current window: attempt A's red rows
    # are history and cannot fail the retried round.
    dispatch.task_claim(project, "T103")
    attempt_b = project.store.attempt_latest_for_task(row.revision_id, "T103")
    assert attempt_b.id != attempt_a.id
    assert _worker_attempt_count(project, row.revision_id, "T103") == 2
    assert project.store.verifications_current(row.revision_id, "T103") == []

    (project.root / "c.marker").write_text("fixed in the retry round")
    check = dispatch.task_check(project, "T103")
    assert check["summary"]["passed"] == 2
    assert check["check_rounds"] == {
        "attempt": attempt_b.id, "used": 1, "budget": 3,
        "remaining": 2, "exceeded": False,
    }  # the budget is per attempt: the retry starts from zero
    result = dispatch.task_complete(project, "T103", str(write_evidence(tmp_path, "tc2.json")))
    assert result["status"] == "passed"

    # History intact after the pass: attempt A's rows untouched, attempt B
    # green, and the current judgment reads only attempt B's delivery.
    assert project.store.verifications_for_attempt(attempt_a.id) == gate_rows
    assert all(
        v.passed for v in project.store.verifications_for_attempt(attempt_b.id)
        if v.kind == "command"
    )
    task_row = project.store.task_get(row.revision_id, "T103")  # goal is done
    assert verify.evaluate(project.store, row.revision_id, task_row) == "passed"
    assert dispatch.status_data(project)["run"]["status"] == "done"


# ---------------------------------------------------------------------------
# The skills teach the contract the gate actually enforces


def _skill(name: str) -> str:
    return (REPO_ROOT / "skills" / name / "SKILL.md").read_text()


def test_skills_teach_the_structured_evidence_contract():
    """Both skills document the delivery result the gate validates — field
    names, the status enumeration, the check-item shape — and neither
    teaches the legacy {summary, commands, artifacts} evidence anymore."""
    for name in ("orx-agent", "orx-controller"):
        text = _skill(name)
        assert '"status": "passed"' in text  # the JSON example is the schema
        for field in ("status", "checks", "artifacts", "summary"):
            assert f"`{field}`" in text, (name, field)
        for value in verify.DELIVERY_STATUSES:  # the exact enumeration
            assert f"`{value}`" in text, (name, value)
        assert '"exit_code": 0' in text  # the check-item shape
        assert '"commands": []' not in text  # the rejected legacy shape is gone


def test_orx_agent_skill_matches_the_delivery_contract_flow():
    text = _skill("orx-agent")
    for gate in ("START GATE", "BLOCKED EXIT", "CHECK-FIX LOOP", "DELIVERY GATE"):
        assert gate in text, gate
    assert "orx task check" in text          # the same-session self-check
    assert "check_rounds" in text            # the budget observability
    assert "worker.max_check_rounds" in text
    assert "attempt stays open" in text      # the same-attempt recovery path
    # A green self-check never replaces the independent verifier.
    assert "never replaces the independent verifier" in text
    assert "orx task complete" in text and "--evidence" in text


def test_orx_controller_skill_matches_complete_and_retry_semantics():
    text = _skill("orx-controller")
    assert "structured delivery" in text
    assert "delivery gate" in text
    # The worker contract the controller passes through and acts on.
    for gate in ("START GATE", "BLOCKED EXIT", "DELIVERY GATE"):
        assert gate in text, gate
    # A gate rejection is not a failure: the attempt stays open for a fix.
    assert "attempt stays open" in text
    # Delivery kinds and the retry feedback derived from them.
    assert "environment/tool blocked" in text
    assert "worker-reported failure" in text
    assert "classified" in text
    # The worker's same-session check loop and its budget surface.
    assert "orx task check" in text
    assert "check_rounds" in text
    assert "--attempt" in text  # completions quote the attempt they answer
