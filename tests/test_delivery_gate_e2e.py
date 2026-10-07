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

import json
import sqlite3
from pathlib import Path

import pytest

from orx import adapters, dispatch, runtime, verify
from orx.adapters.base import Launch, ProbeReport
from orx.verify import DeliveryRejected

from conftest import (
    HOST_CONFIG_TOML,
    HOST_PROFILES_TOML,
    active_task,
    ir_for,
    make_project,
    task_spec,
    write_evidence,
)

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


# ---------------------------------------------------------------------------
# G007 T001: the native agent definitions teach the contract the gate
# enforces.
#
# The 0.3.0 symptom: agents/orx-worker.md's TASK section demonstrated the
# legacy {summary, commands, artifacts} evidence — a worker following its
# own role definition would be rejected by name at `orx task complete`.
# These tests pin the three native roles (the ones the preset ships) to the
# CURRENT single-assignment protocol: the worker definition's evidence
# example must be accepted by the real evidence validator and pass the
# delivery gate in a stand-in task, and both verifier definitions keep the
# read-only independent two-line-verdict contract.


AGENTS_ROOT = REPO_ROOT / "agents"

WORKER_DEFINITION = (AGENTS_ROOT / "orx-worker.md").read_text()
VERIFIER_DEFINITIONS = {
    name: (AGENTS_ROOT / f"{name}.md").read_text()
    for name in ("orx-verifier", "orx-verifier-strong")
}


def _json_blocks(text: str) -> list:
    """Every parseable JSON object fenced as ```json in a markdown doc."""
    import re
    blocks = []
    for match in re.findall(r"```json\n(.*?)```", text, re.DOTALL):
        try:
            parsed = json.loads(match)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            blocks.append(parsed)
    return blocks


def _worker_evidence_example() -> dict:
    """The structured delivery result example in the worker definition —
    the one object in the doc carrying all four delivery fields."""
    candidates = [
        block for block in _json_blocks(WORKER_DEFINITION)
        if all(field in block for field in ("status", "summary", "checks", "artifacts"))
    ]
    assert len(candidates) == 1, (
        "agents/orx-worker.md must demonstrate exactly one structured"
        " delivery result (status/summary/checks/artifacts)"
    )
    return candidates[0]


def test_worker_definition_evidence_example_is_accepted_by_the_real_validator(tmp_path):
    """The demonstration JSON is not decorative: the actual evidence
    validator (the one `orx task complete` runs) accepts it as-is."""
    example = _worker_evidence_example()
    path = tmp_path / "worker-example.json"
    path.write_text(json.dumps(example))
    data, errors = verify.load_delivery_evidence(path)
    assert errors == []
    assert data["status"] in verify.DELIVERY_STATUSES


def test_worker_definition_evidence_example_passes_the_gate_in_a_stand_in_task(
        project, goal, tmp_path):
    """The same example, filled with a real check row the way a worker
    would, completes a stand-in task through the delivery gate."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T201", acceptance=goal.acceptance, verification=["true"]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T201")
    check = dispatch.task_check(project, "T201")
    assert check["summary"]["passed"] == 1

    delivery = json.loads(json.dumps(_worker_evidence_example()))
    delivery["summary"] = "worker-definition example exercised end to end"
    delivery["checks"] = [
        {"command": row["command"], "exit_code": row["exit_code"],
         "log": row["log_path"]}
        for row in check["results"]
    ]
    delivery["artifacts"] = ["agents/orx-worker.md"]
    path = tmp_path / "worker-example-delivery.json"
    path.write_text(json.dumps(delivery))
    result = dispatch.task_complete(project, "T201", str(path))
    assert result["status"] == "passed"
    assert result["verdict"] == "passed"


def test_worker_definition_teaches_the_current_delivery_contract():
    """The worker role definition matches the contract the dispatch prompt
    and the orx-agent skill teach: the four gates in order, the same-session
    check loop with its budget, claim/attempt identity, heartbeat reporting,
    and the legacy evidence shape explicitly rejected."""
    for gate in ("START GATE", "BLOCKED EXIT", "CHECK-FIX LOOP", "DELIVERY GATE"):
        assert gate in WORKER_DEFINITION, gate
    assert WORKER_DEFINITION.index("START GATE") < WORKER_DEFINITION.index(
        "BLOCKED EXIT") < WORKER_DEFINITION.index("DELIVERY GATE")
    assert "orx task claim" in WORKER_DEFINITION          # claim first
    assert "--attempt" in WORKER_DEFINITION               # attempt identity
    assert "orx task check" in WORKER_DEFINITION          # the self-check loop
    assert "check_rounds" in WORKER_DEFINITION            # budget observability
    assert "orx task heartbeat" in WORKER_DEFINITION      # progress reports
    assert "never replaces the independent" in WORKER_DEFINITION
    assert "orx task complete" in WORKER_DEFINITION and "--evidence" in WORKER_DEFINITION
    assert "ORX_ASSIGNMENT=orx-assignment:" in WORKER_DEFINITION
    # The rejected legacy shape is gone from the examples it teaches.
    assert '"commands": []' not in WORKER_DEFINITION
    assert '"commands"' not in json.dumps(_worker_evidence_example())


@pytest.mark.parametrize("name", ["orx-verifier", "orx-verifier-strong"])
def test_verifier_definitions_keep_the_read_only_two_line_verdict_contract(name):
    """Both native verifiers stay aligned with skills/orx-agent's VERIFICATION
    contract and dispatch's verifier prompt: one instruction, independent
    read-only judgment against the acceptance criteria as written, and the
    exact two-line verdict with a mandatory reason on fail."""
    text = VERIFIER_DEFINITIONS[name]
    assert "ONE" in text and "verification instruction" in text
    assert "independent judge" in text
    assert "never relax or reinterpret" in text
    assert "fix the work; you report it" in text    # report, never fix
    assert "Do not create, modify, or delete" in text       # read-only posture
    assert "Bash only for inspection" in text
    assert "ORX_REASON=" in text and "ORX_VERDICT=pass" in text
    assert "ORX_VERDICT=fail" in text
    assert "reason is invalid" in text                 # fail requires a reason
    assert "ORX_ASSIGNMENT=orx-assignment:" in text         # the identity anchor
    # The two-line verdict is the shared contract with the skill and the
    # dispatch verifier prompt — same tokens in all three places.
    skill = _skill("orx-agent")
    for token in ("ORX_REASON=", "ORX_VERDICT=pass", "ORX_VERDICT=fail"):
        assert token in skill, token


def test_native_worker_definition_matches_the_skill_evidence_example_shape():
    """The worker definition's example and the orx-agent skill's example
    are the SAME schema: identical field sets and check-item keys, so a
    worker following either document produces evidence the gate accepts."""
    skill = _skill("orx-agent")
    [skill_example] = [
        block for block in _json_blocks(skill)
        if all(field in block for field in ("status", "summary", "checks", "artifacts"))
    ]
    worker_example = _worker_evidence_example()
    assert set(skill_example) == set(worker_example)
    assert len(skill_example["checks"]) == len(worker_example["checks"]) == 1
    assert set(skill_example["checks"][0]) == set(worker_example["checks"][0]) == {
        "command", "exit_code", "log",
    }


# ---------------------------------------------------------------------------
# G007 T002: the controller's analytics watchdog is an explicitly
# configured, optional external integration. The 0.3.0 skill hardcoded one
# local checkout path as the executable (any machine without that checkout
# followed a command that could not run). These tests pin the taught
# contract — configurable source with no default location, optional
# execution with skip / start-failure / cleanup boundaries, the loop never
# blocked by a missing analytics tool — and the read contract's matching
# removal of the fixed-local-path wording.


def test_orx_controller_skill_teaches_configured_optional_analytics_watchdog():
    """The controller skill's watchdog branch: the executable comes from
    explicit configuration (ORX_ANALYTICS_BIN) with no default local path,
    unconfigured / not installed / start-failure all continue the Goal, and
    stop is bounded to watchers this session started. Whitespace is
    flattened first: the contract is the words, not the line wrapping."""
    import re
    text = re.sub(r"\s+", " ", _skill("orx-controller"))
    # No local machine path is baked in as the tool's location — not the
    # 0.3.0 checkout, not any pre-resolved venv binary.
    assert "~/Sources" not in text
    assert ".venv/bin" not in text
    # The executable source is explicit configuration, and nothing is
    # assumed or probed when it is absent.
    assert "ORX_ANALYTICS_BIN" in text
    assert "no default location" in text
    # Optional: unconfigured, missing, and failed starts all continue.
    assert "OPTIONAL" in text
    assert "Unconfigured, not installed, or start fails" in text
    # No integration never blocks any stage of the loop.
    assert ("planning, execution, verification, and wrap-up"
            " are never blocked") in text
    # The taught subcommands stay start/status/stop.
    for subcommand in ("watch start", "watch status", "watch stop"):
        assert subcommand in text, subcommand
    # Cleanup boundary: stop only a watcher this session started.
    assert "ONLY for a watcher this session started" in text
    assert "another session or the user started is not yours to stop" in text
    # The analysis implementation stays external; ORX ships no built-in
    # analysis layer, and the watchdog observes — no auto-recovery claim.
    assert "separate repository" in text
    assert "no analysis layer of its own" in text
    assert "never restarts, resumes, or recovers" in text


def test_observability_contract_names_no_fixed_local_analytics_path():
    """The read contract no longer presents the analytics checkout as a
    fixed local directory: the separate-repository statement stays, no
    checkout location is named as a contract fact, and historical paths
    are declared history — while the read-only boundaries the
    observability tests pin remain untouched. Whitespace is flattened
    first: the contract is the words, not the line wrapping."""
    import re
    text = re.sub(
        r"\s+", " ",
        (REPO_ROOT / "docs" / "observability-contract.md").read_text(),
    )
    assert "~/Sources" not in text
    assert "orx-analytics" in text          # the external tool stays named
    assert "separate repository" in text    # and stays external
    assert "not shipped here" in text
    assert "names no fixed checkout location" in text
    assert "history, not defaults" in text  # real paths stay historical
    # The read-only posture the contract tests assert is preserved.
    assert "Do not call Store.open" in text


# ---------------------------------------------------------------------------
# G004 T004: the reference chain across revisions, end to end.
#
# One continuous story: revision 1 delivers passed work with evidence; a
# replan revision 2 confirms it citing the evidence as an artifact; the
# accepted delivery records provenance (the SOURCE task's own attempt and
# evidence identity) plus a delivery snapshot bound to the completing
# attempt; revision 3 reuses the SAME task number and — resolving only
# through the declared correspondence — shows the chain with an UNCHANGED
# digest verdict, flags the artifact CHANGED/MISSING honestly once the file
# moves (never assuming it valid), and its own delivery writes a second
# snapshot that does not overwrite the first. No old passed record ever
# becomes a new task's verification result: every revision's task starts
# runnable with an empty current window and passes only its own gate.

def test_replan_reference_chain_end_to_end(project, goal, tmp_path):
    # --- revision 1: T001 passes with recorded completion evidence E1.
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["true"]),
    ]))
    revision1 = active_task(project, "T001").revision_id
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    e1 = write_evidence(tmp_path, "round1.json")
    dispatch.task_complete(project, "T001", str(e1))
    # (The Run reaches done here; the Goal reopens when revision 2 lands.)
    assert project.store.task_get(revision1, "T001").status == "passed"

    # --- revision 2: T101 confirms 1:T001, citing E1 through the mapping.
    ir2 = ir_for(goal, [
        task_spec("T101", acceptance=goal.acceptance, verification=["true"]),
    ])
    ir2["replan"] = {
        "prior_revision": 1,
        "tasks": [{
            "task": "T101", "classification": "confirm",
            "sources": [{"revision": 1, "task_id": "T001"}],
            "confirm_verification": ["true"],
            "artifacts": [str(e1)],
        }],
        "superseded": [{
            "revision": 1, "task_id": "T001", "disposition": "confirmed",
            "successors": ["T101"],
        }],
    }
    dispatch.submit_plan(project, ir2)
    revision2 = active_task(project, "T101").revision_id
    # The confirming task starts fresh: runnable, empty verification window,
    # nothing inherited from the passed source.
    assert active_task(project, "T101").status == "runnable"
    assert project.store.verifications_current(revision2, "T101") == []

    park2 = dispatch.run_slice(project)["host_required"][0]
    assert "1:T001 (revision 1, task T001, part=false) — recorded status: passed" in park2["prompt"]
    assert f"  - {e1} -> resolves to recorded source evidence: 1:T001 (source attempt " in park2["prompt"]
    assert "no delivery snapshot recorded yet — nothing to compare" in park2["prompt"]

    dispatch.task_claim(project, "T101")
    e2 = write_evidence(tmp_path, "round2.json")
    delivered2 = dispatch.task_complete(project, "T101", str(e2))
    assert delivered2["verdict"] == "passed"
    snapshot2 = project.root / delivered2["replan_delivery"]["snapshot"]
    assert snapshot2.name.startswith("T101-r02-a")
    assert snapshot2.exists()
    [provenance2] = project.store.replan_artifact_sources_for_task(
        revision2, "T101")
    assert (provenance2.source_revision, provenance2.source_task_id) == (1, "T001")
    # Provenance names the SOURCE task's own evidence row for E1.
    [e1_row] = [
        r for r in project.store.evidence_rows_for_task(revision1, "T001")
        if r.kind == "completion"
    ]
    assert provenance2.evidence_id == e1_row.id
    assert provenance2.attempt_id == e1_row.attempt_id

    # --- revision 3: the SAME task number T101, different work; sources
    # cite BOTH the immediate predecessor (2:T101) and the original (1:T001)
    # so E1 still binds through a declared edge — numbers alone prove nothing.
    ir3 = ir_for(goal, [
        task_spec("T101", acceptance=goal.acceptance, verification=["true"]),
    ])
    ir3["replan"] = {
        "prior_revision": 2,
        "tasks": [{
            "task": "T101", "classification": "confirm",
            "sources": [{"revision": 2, "task_id": "T101"},
                        {"revision": 1, "task_id": "T001"}],
            "confirm_verification": ["true"],
            "artifacts": [str(e1)],
        }],
        "superseded": [{
            "revision": 2, "task_id": "T101", "disposition": "confirmed",
            "successors": ["T101"],
        }],
    }
    dispatch.submit_plan(project, ir3)
    revision3 = active_task(project, "T101").revision_id
    # Same number, fresh work: runnable, empty window; revision 2's rows are
    # untouched history the new revision never reads as its own results.
    assert active_task(project, "T101").status == "runnable"
    assert project.store.verifications_current(revision3, "T101") == []
    assert project.store.verifications_for(revision2, "T101")  # history kept

    park3 = dispatch.run_slice(project)["host_required"][0]
    prompt3 = park3["prompt"]
    assert "2:T101 (revision 2, task T101, part=false) — recorded status: passed" in prompt3
    assert "1:T001 (revision 1, task T001, part=false) — recorded status: passed" in prompt3
    # The delivery snapshot is the baseline: E1 is untouched, so UNCHANGED —
    # with its full traceable provenance.
    assert ("state: file present, UNCHANGED since the delivery snapshot"
            " (revision 2, task T101, attempt") in prompt3

    # --- the referenced file changes: the next composition says CHANGED,
    # never assumes validity, and keeps the historical provenance.
    e1.write_text('{"status": "failed", "tampered": true}')
    goal_row = project.store.goal_active()
    task3 = active_task(project, "T101")
    recomposed = dispatch.worker_prompt(
        goal_row, task3,
        replan_context=dispatch._replan_reference_context(
            project.store, project.root, revision3, "T101", audience="worker",
        ),
    )
    assert "file present but CHANGED since the delivery snapshot" in recomposed
    assert "not assumed valid" in recomposed
    assert "re-verify what you rely on" in recomposed
    assert "(revision 2, task T101, attempt" in recomposed  # provenance kept

    # --- the referenced file disappears: MISSING since the snapshot, still
    # with the traceable historical origin.
    e1.unlink()
    recomposed_missing = dispatch.worker_prompt(
        goal_row, task3,
        replan_context=dispatch._replan_reference_context(
            project.store, project.root, revision3, "T101", audience="worker",
        ),
    )
    assert "file MISSING since the delivery snapshot" in recomposed_missing
    assert "not assumed valid" in recomposed_missing
    assert "(revision 2, task T101, attempt" in recomposed_missing

    # --- the reference is supporting material, never a gate: revision 3's
    # own delivery passes ITS check ('true') regardless of E1's state, and
    # writes a SECOND snapshot that does not overwrite revision 2's.
    dispatch.task_claim(project, "T101")
    e3 = write_evidence(tmp_path, "round3.json")
    delivered3 = dispatch.task_complete(project, "T101", str(e3))
    assert delivered3["verdict"] == "passed"
    snapshot3 = project.root / delivered3["replan_delivery"]["snapshot"]
    assert snapshot3.name.startswith("T101-r03-a")
    assert snapshot3 != snapshot2
    assert snapshot2.exists() and snapshot3.exists()
    # The new snapshot records the honest state of the changed reference.
    doc3 = json.loads(snapshot3.read_text())
    [row3] = doc3["artifacts"]
    assert row3["artifact"] == str(e1)
    assert row3["resolved_to_recorded_evidence"] is True  # evidence rows persist
    assert row3["exists_at_delivery"] is False
    assert row3["digest_at_delivery"] is None

    # The database still finds BOTH revisions' snapshots and history.
    snapshots = {
        (r.attempt_id, r.path)
        for rev in (revision2, revision3)
        for r in project.store.evidence_rows_for_task(rev, "T101")
        if r.kind == "delivery_snapshot"
    }
    assert len(snapshots) == 2
    for _attempt_id, path in snapshots:
        assert (project.root / path).exists()


# ---------------------------------------------------------------------------
# G007 T003: docs/upgrade-0.3.1.md is complete and linked from README — the
# backup/migration/rollback story and the per-channel update steps match the
# machinery, and the evidence example it teaches is the one the gate accepts.


def _flat(path) -> str:
    """Whitespace-normalized file text — the contract is the words, not the
    line wrapping (same discipline as the skill-pinning tests above)."""
    import re
    return re.sub(r"\s+", " ", Path(path).read_text())


UPGRADE_DOC_FLAT = _flat(REPO_ROOT / "docs" / "upgrade-0.3.1.md")


def test_readme_links_the_upgrade_doc_and_states_the_supported_versions():
    """README links docs/upgrade-0.3.1.md, and its observability statement
    matches what the read contract actually accepts: v8/v9/v10/v11 — not
    the stale v8/v9-only wording — with the v8-established/v9-v10-v11-added
    history kept as context."""
    flat = _flat(REPO_ROOT / "README.md")
    assert "docs/upgrade-0.3.1.md" in flat            # the doc is linked
    assert "v8/v9/v10/v11" in flat                    # current observation
    assert "8, 9, 10, and 11" in flat                 # the exact allowlist
    assert "8 and 9 exactly" not in flat              # stale wording is gone
    assert "v8 established the observability schema" in flat  # history kept
    # The upgrade callout teaches the load-bearing order: backup before the
    # new version first opens the old database, restore-only rollback.
    assert "before the new version first opens the old database" in flat
    assert "never a hand-edited" in flat


def test_upgrade_doc_orders_backup_before_first_open_and_handles_wal():
    """The backup is a SQLite-consistent snapshot (VACUUM INTO / .backup —
    WAL-committed frames included, never a bare cp of the main file), it
    covers config and run materials too, and §2's step list puts it before
    the first open that triggers the automatic migration."""
    flat = UPGRADE_DOC_FLAT
    assert "VACUUM INTO" in flat and ".backup" in flat  # consistent backup
    assert "state.db-wal" in flat                       # WAL is named
    assert "config.toml" in flat and "profiles.toml" in flat
    assert ".orx/runs" in flat                          # run materials too
    # §2's ordered steps: backup (step 2) strictly before the first open
    # that migrates (step 4).
    backup_step = flat.find("备份（§6.1）")
    first_open_step = flat.find("触发自动迁移")
    assert backup_step != -1 and first_open_step != -1
    assert backup_step < first_open_step
    # §6.1's heading states the timing rule itself.
    assert "首次打开旧库之前" in flat


def test_upgrade_doc_migration_and_rollback_story_matches_the_store():
    """v8 -> v11 is automatic on first open, additive (nothing backfilled),
    copy-then-replace (a failed migration leaves the original usable); an
    older ORX refuses the newer file without touching it; rollback restores
    the pre-upgrade backup and never hand-edits schema_version."""
    flat = UPGRADE_DOC_FLAT
    assert "自动" in flat and "v8 → v9" in flat and "v11" in flat
    assert "不回填历史" in flat                 # additive, no backfill
    assert "复制-替换" in flat                  # copy-then-replace policy
    assert "newer than supported version 8" in flat  # old code refuses v11
    assert "回退：必须恢复升级前备份" in flat
    assert "never hand-edit" in flat
    assert "schema_version" in flat
    # Restore hygiene: stale WAL sidecars are removed with the migrated file.
    assert "rm -f .orx/state.db .orx/state.db-wal" in flat


def test_upgrade_doc_channel_steps_match_the_update_module():
    """Editable users switch the checkout and explicitly do NOT run
    uv tool upgrade (the exact refusal update.py raises); wheel users get
    the orx update / uv tool upgrade path; skills refresh only through
    orx skill update, stated separately from the role-file update."""
    flat = UPGRADE_DOC_FLAT
    assert "git fetch && git checkout v0.3.1" in flat   # editable path
    assert "不做 `uv tool upgrade`" in flat              # and no tool upgrade
    # The refusal text quoted in the doc is the one update.py raises.
    assert "orx is an editable uv tool install" in flat
    assert "uv tool upgrade orx-agent" in flat          # wheel/uv-tool path
    # Skills and role definitions are separate, explicitly.
    assert "orx skill update" in flat
    assert "不会触碰角色定义文件" in flat
    assert "两个入口分开" in flat


def test_upgrade_doc_evidence_example_is_accepted_by_the_real_validator(tmp_path):
    """The doc's §3.4 example is the current structured delivery result —
    the one object in the doc carrying all four delivery fields passes the
    real evidence validator — and the doc names the legacy
    {summary, commands, artifacts} shape as rejected by name."""
    candidates = [
        block for block in _json_blocks(
            (REPO_ROOT / "docs" / "upgrade-0.3.1.md").read_text())
        if all(field in block for field in ("status", "summary", "checks", "artifacts"))
    ]
    assert len(candidates) == 1, (
        "docs/upgrade-0.3.1.md must demonstrate exactly one structured"
        " delivery result (status/summary/checks/artifacts)"
    )
    path = tmp_path / "upgrade-doc-example.json"
    path.write_text(json.dumps(candidates[0]))
    data, errors = verify.load_delivery_evidence(path)
    assert errors == []
    assert data["status"] in verify.DELIVERY_STATUSES
    flat = UPGRADE_DOC_FLAT
    # The full status enumeration and the check-item shape are taught.
    for status in verify.DELIVERY_STATUSES:
        assert status in flat, status
    assert "{command, exit_code, log}" in flat
    # The legacy shape is explicitly called out as rejected.
    assert '"commands", "artifacts"' in flat


# ---------------------------------------------------------------------------
# G009 T001 (2026-10-07 incident): freeze the two known-red diagnostic
# replays and the CLI delivery boundary as regular behavior regressions.
#
# docs/diagnostics/cursor-2026-10-07/replay.py drove the REAL dispatch
# stack with attempt a131's captured Cursor output — a worker that
# explicitly delivered status=blocked in the result text while the CLI
# process exited 0 — and recorded the defect both ways: the attempt closes
# as `completed` with failure_reason=NULL (worker's own delivery never
# parsed), and with a green controller gate the TASK PASSES. The same
# envelope carries usage and a session id, which today ARE recorded
# correctly — that half of the seam must stay correct when the delivery
# half is fixed (Stage 1 of docs/cursor-system-analysis-2026-10-07.md:
# "usage/session 仍照常记录").
#
# Until the Stage 1 CLI delivery fix lands, these regressions pin the
# DESIRED behavior as strict xfail red baselines. Each one fails on the
# defect itself (the recorded attempt/task rows), not on fixtures; strict
# xfail means the moment the behavior changes the suite demands the
# marker's removal — the baseline can never silently rot. Do not weaken an
# assertion to make one pass; remove its marker with the fix.
#
# The boundary matrix (missing / malformed / contradictory deliveries)
# pins the same seam from the plan's success criteria: an unparseable or
# self-contradictory worker delivery is an explicit protocol_error — never
# a silent pass, never a clean "completed" attempt.

# a131's captured result envelope, frozen: process exit 0, harness
# envelope success, the worker's blocked delivery fenced in the result
# text, real usage buckets. Original evidence — do not edit casually; it
# mirrors docs/diagnostics/cursor-2026-10-07/replay.py byte for byte.
CAPTURED_CURSOR_BLOCKED_DELIVERY = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": "24eea185-1608-4ead-99e7-791734bdade1",
    "result": (
        "blocked: the start gate cannot run in this environment. "
        "Every shell invocation was rejected before execution.\n\n"
        '```json\n{"status":"blocked","summary":"shell rejected",'
        '"checks":[],"artifacts":[]}\n```'
    ),
    "usage": {"inputTokens": 46730, "outputTokens": 2341,
              "cacheReadTokens": 166272},
}

CAPTURED_CURSOR_SESSION = "24eea185-1608-4ead-99e7-791734bdade1"
CAPTURED_CURSOR_USAGE = {"inputTokens": 46730, "outputTokens": 2341,
                         "cacheReadTokens": 166272}


def _captured_envelope(result_text: str, **overrides) -> dict:
    """A Cursor-shaped result envelope around an arbitrary worker result
    text (the boundary variants derive from the same captured shape)."""
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": CAPTURED_CURSOR_SESSION,
        "result": result_text,
        "usage": dict(CAPTURED_CURSOR_USAGE),
    }
    payload.update(overrides)
    return payload


def _replay_cli_delivery(monkeypatch, tmp_path, payload: dict, check: str = "true"):
    """The replay logic of docs/diagnostics/cursor-2026-10-07/replay.py,
    parameterized on the captured process output: one CLI worker task
    through the REAL dispatch stack (route, launch seam, state machine,
    delivery gate, storage) with only the model process replaced by the
    captured Cursor envelope. Returns the recorded (attempt, task, usage)
    rows so regressions assert persisted facts, not narrative.

    The worker profile is a cursor-harness stand-in whose probe and launch
    are pinned to the captured shape; the controller gate runs `check`
    ("true" = green, "false" = red) exactly as the diagnostic replay does.
    """
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]', 'profiles = ["cli-fake"]'
    )
    profiles = HOST_PROFILES_TOML.replace('harness = "shell"', 'harness = "cursor"')
    project = make_project(tmp_path, config, profiles)
    try:
        adapter = adapters.get_adapter("cursor")
        monkeypatch.setattr(
            adapter, "probe",
            lambda: ProbeReport(True, "captured CLI stand-in"),
        )
        monkeypatch.setattr(
            adapter, "build_worker_launch",
            lambda **kwargs: Launch(
                argv=["captured-agent"], cwd=tmp_path, timeout=5),
        )
        monkeypatch.setattr(
            runtime, "run_launch",
            lambda launch: runtime.CommandResult(
                command="captured-agent", exit_code=0,
                stdout=json.dumps(payload), stderr="",
                duration_sec=0.01, timed_out=False,
            ),
        )
        goal = dispatch.create_goal(
            project, objective="Replay the captured CLI delivery",
            acceptance=["checks runnable"], constraints=[], context="",
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec(
            "T310", acceptance=goal.acceptance, verification=[check],
            complexity="low",
        )]))
        dispatch.run_slice(project)
        attempt = project.store.conn.execute(
            "SELECT id,result,failure_reason,session_ref FROM attempts"
            " WHERE role='worker'"
        ).fetchone()
        task = project.store.conn.execute(
            "SELECT status,failure_reason FROM tasks WHERE task_id='T310'"
        ).fetchone()
        usage = project.store.conn.execute(
            "SELECT input_tokens,output_tokens,cached_input_tokens"
            " FROM usage_observations WHERE attempt_id = ?",
            (attempt["id"],),
        ).fetchall()
        return attempt, task, usage
    finally:
        project.close()


def test_cli_blocked_delivery_survives_a_green_controller_gate(tmp_path, monkeypatch):
    """The incident's false pass, frozen: a CLI worker that explicitly
    delivers status=blocked while the process exits 0 must never pass —
    not even when every controller check is green. The blocked delivery is
    the recorded attempt result (blocked stays blocked; exit 0 is
    transport, not a task verdict), and the task fails with the block as
    its cause — the same classification the host delivery path records.
    Usage and session ride the envelope and stay recorded regardless."""
    attempt, task, usage = _replay_cli_delivery(
        monkeypatch, tmp_path, CAPTURED_CURSOR_BLOCKED_DELIVERY, check="true")

    # The worker's own delivery, not the process exit code, closes the
    # attempt: blocked is an independent recorded delivery result.
    assert attempt["result"] == "blocked", (
        f"exit 0 overrode the worker's blocked delivery:"
        f" result={attempt['result']!r},"
        f" failure_reason={attempt['failure_reason']!r}"
    )
    assert attempt["failure_reason"].startswith(
        "delivery blocked (environment/tool blocked):")

    # A green controller gate cannot turn a blocked delivery into a pass:
    # the task fails on the block, so retries get environment-block
    # guidance instead of "fix the red tests".
    assert task["status"] == "failed", task["status"]
    assert task["failure_reason"].startswith(
        "delivery blocked (environment/tool blocked):")

    # The half of the seam that already works must keep working: usage and
    # session are recorded from the envelope whatever the delivery says.
    assert attempt["session_ref"] == CAPTURED_CURSOR_SESSION
    assert [tuple(row) for row in usage] == [(46730, 2341, 166272)]


def test_cli_blocked_delivery_survives_a_red_controller_gate(tmp_path, monkeypatch):
    """The incident's misclassification, frozen: with the controller gate
    red, the blocked delivery is still the worker's recorded fact — the
    attempt closes blocked with the block as its failure reason, not as a
    clean `completed` whose task failure then reads as a code defect the
    worker should fix."""
    attempt, task, usage = _replay_cli_delivery(
        monkeypatch, tmp_path, CAPTURED_CURSOR_BLOCKED_DELIVERY, check="false")

    assert attempt["result"] == "blocked", (
        f"exit 0 overrode the worker's blocked delivery:"
        f" result={attempt['result']!r},"
        f" failure_reason={attempt['failure_reason']!r}"
    )
    assert attempt["failure_reason"].startswith(
        "delivery blocked (environment/tool blocked):")
    # The blocked delivery is the primary failure fact: the retrying worker
    # is told about the environment block, not sent to fix red tests it
    # cannot even run.
    assert task["status"] == "failed", task["status"]
    assert task["failure_reason"].startswith(
        "delivery blocked (environment/tool blocked):")
    assert attempt["session_ref"] == CAPTURED_CURSOR_SESSION
    assert [tuple(row) for row in usage] == [(46730, 2341, 166272)]


# The delivery boundary matrix. Each shape is a real failure mode from the
# 2026-10-07 incident report (a132/a133 delivered natural language only;
# truncated or contradictory machine output is the neighboring protocol
# hazard). The desired behavior is the plan's success criterion: an
# explicit protocol_error — never a silent pass, never a clean completed
# attempt — while usage/session recording stays untouched.
BOUNDARY_DELIVERIES = {
    # a132/a133: natural-language blocker, no machine-readable delivery.
    "missing": _captured_envelope(
        "I could not run the checks: every shell invocation of uv, pytest"
        " and orx was rejected before execution. No code was modified."
    ),
    # The worker tried the protocol but the JSON is not parseable.
    "malformed": _captured_envelope(
        "blocked: the start gate cannot run in this environment.\n\n"
        '```json\n{"status":"blocked","summary":"shell rejected"\n```'
    ),
    # Two fenced deliveries that disagree — passed and blocked cannot both
    # be the worker's final answer.
    "contradictory": _captured_envelope(
        "The work is finished.\n\n"
        '```json\n{"status":"passed","summary":"all checks green",'
        '"checks":[],"artifacts":[]}\n```\n\n'
        "Correction: the shell was rejected, nothing ran.\n"
        '```json\n{"status":"blocked","summary":"shell rejected",'
        '"checks":[],"artifacts":[]}\n```'
    ),
    # The harness envelope itself contradicts the claim: is_error=true
    # while the fenced delivery says passed.
    "envelope-contradicts-claim": _captured_envelope(
        '```json\n{"status":"passed","summary":"done",'
        '"checks":[],"artifacts":[]}\n```',
        subtype="error_during_execution", is_error=True,
    ),
}


@pytest.mark.parametrize("shape", sorted(BOUNDARY_DELIVERIES))
def test_cli_unparseable_delivery_is_an_explicit_protocol_error(
        tmp_path, monkeypatch, shape):
    """Missing, malformed, and mutually contradictory deliveries are an
    explicit protocol_error (docs/cursor-system-analysis-2026-10-07.md
    Stage 1: 缺失或歧义 delivery 显式 protocol_error): the attempt closes
    with the protocol problem recorded, the task never passes, and the
    envelope's usage/session facts are still stored — the protocol failure
    never destroys the observations."""
    attempt, task, usage = _replay_cli_delivery(
        monkeypatch, tmp_path, BOUNDARY_DELIVERIES[shape], check="true")

    assert attempt["result"] == "protocol_error", (
        f"{shape}: an unparseable delivery must not close the attempt as"
        f" {attempt['result']!r} (failure_reason="
        f"{attempt['failure_reason']!r})"
    )
    assert attempt["failure_reason"], f"{shape}: the protocol problem itself must be recorded"
    assert task["status"] == "failed", (
        f"{shape}: task reached {task['status']!r} from an unparseable"
        " delivery — never a silent pass"
    )
    assert task["failure_reason"], f"{shape}: the task failure names the protocol problem"
    # Observations survive the protocol failure.
    assert attempt["session_ref"] == CAPTURED_CURSOR_SESSION
    assert [tuple(row) for row in usage] == [(46730, 2341, 166272)]


# G009 T002: the positive and transport sides of the same seam. A delivery
# that says passed is exactly what the gate is FOR; a non-zero exit is a
# transport fact that dominates the text; and the CLI worker prompt must
# carry the machine contract (the fenced final-message delivery) so the
# worker knows the protocol before it runs.


def test_cli_passed_delivery_flows_to_gate_and_verdict(tmp_path, monkeypatch):
    """An explicit passed delivery is the one claim the delivery gate
    exists to judge: the attempt closes completed (the worker delivered),
    the gate runs the controller checks, and with a green gate the task
    passes. The three facts stay separately recorded: process exit 0
    (transport), delivery passed (worker), gate green (controller)."""
    payload = _captured_envelope(
        "The work is finished.\n\n"
        '```json\n{"status":"passed","summary":"all checks green",'
        '"checks":[],"artifacts":[]}\n```'
    )
    attempt, task, usage = _replay_cli_delivery(
        monkeypatch, tmp_path, payload, check="true")
    assert attempt["result"] == "completed"
    assert attempt["failure_reason"] is None
    assert task["status"] == "passed", task["status"]
    assert attempt["session_ref"] == CAPTURED_CURSOR_SESSION
    assert [tuple(row) for row in usage] == [(46730, 2341, 166272)]


def test_cli_passed_delivery_with_red_gate_fails_on_the_gate(tmp_path, monkeypatch):
    """A passed delivery with a red controller gate fails as a code
    failure — the delivery result stays `completed` (the worker DID
    deliver a success claim; the gate is what rejected it), and the task
    failure names the red gate, not a protocol problem."""
    payload = _captured_envelope(
        '```json\n{"status":"passed","summary":"done",'
        '"checks":[],"artifacts":[]}\n```'
    )
    attempt, task, usage = _replay_cli_delivery(
        monkeypatch, tmp_path, payload, check="false")
    assert attempt["result"] == "completed"
    assert task["status"] == "failed", task["status"]
    assert task["failure_reason"], "the red gate is the recorded cause"
    assert "delivery gate" in task["failure_reason"], task["failure_reason"]


def test_cli_failed_delivery_closes_failed_without_running_the_gate(tmp_path, monkeypatch):
    """An explicit failed delivery has no success claim to gate: the
    attempt closes with the worker-reported failure (the same prefix the
    host delivery path records), the task fails, and the controller gate
    is NOT run — no verification rows appear for the attempt."""
    payload = _captured_envelope(
        "Two checks stay red after the budget.\n\n"
        '```json\n{"status":"failed","summary":"tests red after 3 rounds",'
        '"checks":[{"command":"pytest -q","exit_code":1,"log":null}],'
        '"artifacts":[]}\n```'
    )
    attempt, task, usage = _replay_cli_delivery(
        monkeypatch, tmp_path, payload, check="true")
    assert attempt["result"] == "failed"
    assert attempt["failure_reason"].startswith("delivery failed (worker-reported failure):")
    assert "tests red after 3 rounds" in attempt["failure_reason"]
    assert task["status"] == "failed", task["status"]
    assert task["failure_reason"].startswith("delivery failed (worker-reported failure):")
    # No success claim -> no gate: the controller check ("true") never ran.
    rows = _replay_verifications(tmp_path, attempt["id"])
    assert rows == [], "a failed delivery must not run the controller gate"
    # The worker's delivery document is persisted as attempt evidence —
    # the audit trail the host path records for the same contract.
    evidence = _replay_evidence(tmp_path, attempt["id"])
    assert any(kind == "completion" for kind, _ in evidence), evidence
    # Observations still recorded.
    assert attempt["session_ref"] == CAPTURED_CURSOR_SESSION
    assert [tuple(row) for row in usage] == [(46730, 2341, 166272)]


def test_cli_nonzero_exit_dominates_the_result_text(tmp_path, monkeypatch):
    """A non-zero exit is a transport failure even when the result text
    carries a delivery document: the process fact is primary, the attempt
    fails with the process reason, and the text is neither parsed into a
    structured success nor into a blocked verdict."""
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]', 'profiles = ["cli-fake"]'
    )
    profiles = HOST_PROFILES_TOML.replace('harness = "shell"', 'harness = "cursor"')
    project = make_project(tmp_path, config, profiles)
    try:
        adapter = adapters.get_adapter("cursor")
        monkeypatch.setattr(
            adapter, "probe", lambda: ProbeReport(True, "captured CLI stand-in"))
        monkeypatch.setattr(
            adapter, "build_worker_launch",
            lambda **kwargs: Launch(argv=["captured-agent"], cwd=tmp_path, timeout=5))
        monkeypatch.setattr(
            runtime, "run_launch",
            lambda launch: runtime.CommandResult(
                command="captured-agent", exit_code=3,
                stdout=json.dumps(CAPTURED_CURSOR_BLOCKED_DELIVERY),
                stderr="harness crashed", duration_sec=0.01, timed_out=False,
            ))
        goal = dispatch.create_goal(
            project, objective="Transport failure dominates",
            acceptance=["checks runnable"], constraints=[], context="",
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec(
            "T310", acceptance=goal.acceptance, verification=["true"],
            complexity="low",
        )]))
        dispatch.run_slice(project)
        attempt = project.store.conn.execute(
            "SELECT id,result,failure_reason FROM attempts WHERE role='worker'"
        ).fetchone()
        task = project.store.conn.execute(
            "SELECT status,failure_reason FROM tasks WHERE task_id='T310'"
        ).fetchone()
        assert attempt["result"] == "failed"
        assert "exit 3" in attempt["failure_reason"], attempt["failure_reason"]
        assert not attempt["failure_reason"].startswith("delivery")
        assert task["status"] == "failed", task["status"]
    finally:
        project.close()


def _replay_verifications(tmp_path, attempt_id: int):
    """Verification rows a replayed attempt recorded (helper: re-reads the
    still-open project of the caller's replay — kept trivial by querying
    the single state db under tmp_path)."""
    conn = sqlite3.connect(tmp_path / ".orx" / "state.db")
    try:
        return conn.execute(
            "SELECT command FROM verifications WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchall()
    finally:
        conn.close()


def _replay_evidence(tmp_path, attempt_id: int):
    """(kind, path) evidence rows a replayed attempt recorded."""
    conn = sqlite3.connect(tmp_path / ".orx" / "state.db")
    try:
        return conn.execute(
            "SELECT kind, path FROM evidence WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchall()
    finally:
        conn.close()


def test_cli_worker_prompt_carries_the_final_delivery_contract(tmp_path, monkeypatch):
    """The machine contract rides in the prompt: a message-channel CLI
    worker is told the exit code is transport and its final message must
    end with the fenced delivery document — the protocol ORX actually
    parses. A host worker's prompt keeps the plain contract (the host
    delivers through `orx task complete --evidence`)."""
    payload = _captured_envelope(
        '```json\n{"status":"passed","summary":"done",'
        '"checks":[],"artifacts":[]}\n```'
    )
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]', 'profiles = ["cli-fake"]'
    )
    profiles = HOST_PROFILES_TOML.replace('harness = "shell"', 'harness = "cursor"')
    project = make_project(tmp_path, config, profiles)
    try:
        adapter = adapters.get_adapter("cursor")
        monkeypatch.setattr(
            adapter, "probe", lambda: ProbeReport(True, "captured CLI stand-in"))
        monkeypatch.setattr(
            adapter, "build_worker_launch",
            lambda **kwargs: Launch(argv=["captured-agent"], cwd=tmp_path, timeout=5))
        monkeypatch.setattr(
            runtime, "run_launch",
            lambda launch: runtime.CommandResult(
                command="captured-agent", exit_code=0,
                stdout=json.dumps(payload), stderr="",
                duration_sec=0.01, timed_out=False,
            ))
        goal = dispatch.create_goal(
            project, objective="Prompt contract", acceptance=["a1"],
            constraints=[], context="",
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec(
            "T310", acceptance=goal.acceptance, verification=["true"],
            complexity="low",
        )]))
        dispatch.run_slice(project)
        cli_prompt_files = list((tmp_path / ".orx" / "runs").rglob("T310.md"))
        assert cli_prompt_files, "the CLI worker prompt must be archived"
        prompt = cli_prompt_files[0].read_text()
        assert "FINAL DELIVERY MESSAGE" in prompt
        assert '"status"' in prompt
        assert "transport, not your verdict" in prompt
        assert "protocol error" in prompt
        # The block sits inside the delivery contract, after the gate.
        assert prompt.index("START GATE") < prompt.index("BLOCKED EXIT") \
            < prompt.index("DELIVERY GATE") < prompt.index("FINAL DELIVERY MESSAGE")
    finally:
        project.close()

    # A host worker's prompt keeps the plain contract: the machine block is
    # CLI-only because the host delivers through `orx task complete`.
    host_root = tmp_path / "host-project"
    host_root.mkdir()
    monkeypatch.chdir(host_root)
    host_project = make_project(host_root, HOST_CONFIG_TOML, HOST_PROFILES_TOML)
    try:
        host_goal = dispatch.create_goal(
            host_project, objective="Prompt contract", acceptance=["a1"],
            constraints=[], context="",
        )[0]
        dispatch.submit_plan(host_project, ir_for(host_goal, [task_spec(
            "T310", acceptance=host_goal.acceptance, verification=["true"],
            complexity="low",
        )]))
        host_entry = dispatch.run_slice(host_project)["host_required"][0]
        assert "FINAL DELIVERY MESSAGE" not in host_entry["prompt"]
    finally:
        host_project.close()
