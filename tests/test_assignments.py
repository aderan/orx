"""Assignment completeness contracts (M1.1 Task 1).

Workers receive Goal constraints, Goal context, task scope, acceptance,
verification requirements, and prior failure feedback. Verifiers receive the
task's acceptance, Goal constraints, gate results, and readable evidence
references. Host and CLI paths hand out the SAME composed assignment — proven
by capturing what the CLI process actually received on stdin. No network, no
real agent, no paid model.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from orx import dispatch

from conftest import HOST_CONFIG_TOML, HOST_PROFILES_TOML, ir_for, make_project, task_spec, write_evidence, active_task


def _goal_full(project):
    return dispatch.create_goal(
        project,
        objective="Ship the login fix",
        acceptance=["marker file exists", "summary is written"],
        constraints=["never touch data/", "single writer only"],
        context="the session cache module was rewritten last week; see docs/cache.md",
    )[0]


# ---------------------------------------------------------------------------
# host worker assignment: Goal context rides along with constraints


def test_host_worker_prompt_carries_goal_context(project):
    goal = _goal_full(project)
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=["marker file exists", "summary is written"])
    ]))
    out = dispatch.run_slice(project)
    prompt = out["host_required"][0]["prompt"]
    assert "Goal context (background; binding only where it repeats a constraint):" in prompt
    assert "docs/cache.md" in prompt
    assert "never touch data/" in prompt and "single writer only" in prompt


def test_worker_prompt_without_context_omits_the_block(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=["marker file exists", "summary is written"])
    ]))
    prompt = dispatch.worker_prompt(goal, active_task(project, "T001"))
    assert "Goal context" not in prompt


# ---------------------------------------------------------------------------
# CLI paths receive the identical assignment the host path composes


CLI_PROFILES_EXTRA = """

[profiles.cli-capture-worker]
driver = "cli"
harness = "shell"
executable = "/bin/sh"
args = ["-c", "cat > worker-prompt-captured.txt"]
prompt_transport = "stdin"
model = "fake-model"
class = "economy"
effort = "low"
capabilities = ["coding"]

[profiles.cli-verifier]
driver = "cli"
harness = "shell"
executable = "/bin/sh"
args = ["-c", "cat > verifier-prompt-captured.txt; printf 'ORX_REASON=checked against acceptance\\\\nORX_VERDICT=pass\\\\n'"]
prompt_transport = "stdin"
model = "fake-model"
class = "economy"
effort = "low"
capabilities = ["coding"]
"""


def _cli_project(tmp_path, monkeypatch, worker_profiles: str = '["cli-capture-worker"]'):
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]',
        f"profiles = {worker_profiles}",
    ).replace(
        'profiles = ["host-verifier", "host-vision"]',
        'profiles = ["cli-verifier"]',
    )
    project = make_project(tmp_path, config_toml=config,
                           profiles_toml=HOST_PROFILES_TOML + CLI_PROFILES_EXTRA)
    return project


def test_cli_worker_receives_the_same_prompt_composition(tmp_path, monkeypatch):
    project = _cli_project(tmp_path, monkeypatch)
    try:
        goal = _goal_full(project)
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["marker file exists", "summary is written"],
                      verification=["true"])
        ]))
        task = active_task(project, "T001")
        out = dispatch.run_slice(project)
        assert out["started"], out
        assert out["started"][0]["status"] == "passed"

        captured = (tmp_path / "worker-prompt-captured.txt").read_text()
        expected = dispatch.worker_prompt(goal, task)
        # The exact bytes the host path composes are what the CLI process got.
        assert captured == expected
        for marker in ("never touch data/", "single writer only", "docs/cache.md",
                       "marker file exists", "How your work will be checked", "true"):
            assert marker in captured
    finally:
        project.close()


def test_cli_verifier_prompt_captured_with_full_context(tmp_path, monkeypatch):
    """Verifier path: the CLI process sees acceptance, Goal constraints, the
    recorded gate result, and the completion evidence path — the same
    composition the host verify assignment carries."""
    project = _cli_project(tmp_path, monkeypatch,
                           worker_profiles='["host-worker", "host-external"]')
    try:
        goal = _goal_full(project)
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["marker file exists", "summary is written"],
                      verification=["true", "agent: review the diff against acceptance"])
        ]))
        dispatch.run_slice(project)
        dispatch.task_claim(project, "T001")
        evidence = write_evidence(tmp_path)
        dispatch.task_complete(project, "T001", str(evidence))

        out = dispatch.verify_dispatch(project)
        assert out["launched"], out
        assert out["launched"][0]["verdict"] == "pass"
        status = project.store.conn.execute(
            "SELECT t.status FROM tasks t JOIN plan_revisions r ON t.revision_id = r.id"
            " WHERE r.status = 'active' AND t.task_id = 'T001'"
        ).fetchone()["status"]
        assert status == "passed"

        captured = (tmp_path / "verifier-prompt-captured.txt").read_text()
        for marker in ("marker file exists", "summary is written",
                       "never touch data/", "single writer only",
                       "true", "exit 0", evidence.name):
            assert marker in captured, marker
        assert "review the diff against acceptance" in captured
    finally:
        project.close()


# ---------------------------------------------------------------------------
# resurfacing: a resumed Controller recovers parked assignments from state


def test_run_resurfaces_waiting_host_assignment(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance[:1]),
        task_spec("T002", deps=["T001"], acceptance=goal.acceptance[1:]),
    ]))
    dispatch.run_slice(project)  # parks T001
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(Path.cwd())))
    dispatch.run_slice(project)  # parks T002
    attempts_before = len(project.store.attempts_all())

    # A later `orx run` (e.g. a brand-new host session) re-surfaces T002 with
    # its prompt file, without routing a new attempt or touching statuses.
    out = dispatch.run_slice(project)
    entries = [e for e in out["host_required"] if e["task"] == "T002"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["resurfaced"] is True
    assert entry["status"] == "waiting_host"
    assert entry["claim"] == "orx task claim T002"
    assert (project.root / entry["prompt_file"]).read_text()
    assert active_task(project, "T002").status == "waiting_host"
    assert len(project.store.attempts_all()) == attempts_before

    # A fresh park in the SAME slice is never double-reported as resurfaced.
    assert not [e for e in out["host_required"] if e["task"] == "T001"]


def test_resurfaced_assignment_is_claimable_end_to_end(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["true"]),
    ]))
    dispatch.run_slice(project)
    out = dispatch.run_slice(project)  # resumed session sees the same park
    prompt_file = project.root / out["host_required"][0]["prompt_file"]
    prompt = prompt_file.read_text()
    assert "T001" in prompt and goal.acceptance[0] in prompt

    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(Path.cwd())))
    assert dispatch.status_data(project)["run"]["status"] == "done"


# ---------------------------------------------------------------------------
# Delivery contract + static preflight (R002 follow-up): the worker prompt
# mandates a start gate / blocked exit / delivery gate, and assignment
# assembly statically preflights every command verification entry (denylist
# + first-token PATH probe) on both the host park path and the CLI launch
# path. The preflight executes nothing; the independent verifier semantics
# are untouched.


def _contract_task():
    return task_spec(
        "T001",
        acceptance=["marker file exists", "summary is written"],
        verification=[
            "sh -c true",
            "sudo make test",
            "orx-preflight-no-such-tool-7 --version",
            "agent: review the diff against acceptance",
        ],
    )


def test_worker_prompt_states_the_delivery_contract(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["sh -c true"])
    ]))
    prompt = dispatch.worker_prompt(goal, active_task(project, "T001"))
    # Start gate: the checks must be proven runnable before any implementation.
    assert "orx task check T001" in prompt
    assert "before any implementation work" in prompt
    # Blocked exit: environment unavailable -> immediate non-zero exit with reason.
    assert "BLOCKED EXIT" in prompt
    assert "exit non-zero" in prompt
    assert "blocked: <why the checks cannot run>" in prompt
    assert "Do NOT invest in implementation first" in prompt
    # Delivery gate: prescribed checks green before completion may be claimed.
    assert "DELIVERY GATE" in prompt
    assert "every prescribed command check above must pass" in prompt
    assert "orx task complete T001" in prompt
    # A green self-check never replaces the independent verifier.
    assert "never replaces the independent verifier" in prompt


def test_preflight_rows_flag_denied_missing_and_agent_entries(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [_contract_task()]))
    rows = dispatch.preflight_task_checks(active_task(project, "T001"))
    by_raw = {row["raw"]: row for row in rows}

    ok = by_raw["sh -c true"]
    assert ok["kind"] == "command" and ok["blocked"] is False
    assert ok["denial"] is None
    assert ok["token"] == "sh"
    assert ok["token_status"] in ("found", "builtin")

    denied = by_raw["sudo make test"]
    assert denied["kind"] == "command"
    assert denied["blocked"] is True
    assert denied["denial"] == "sudo"

    missing = by_raw["orx-preflight-no-such-tool-7 --version"]
    assert missing["kind"] == "command"
    assert missing["blocked"] is True
    assert missing["denial"] is None
    assert missing["token"] == "orx-preflight-no-such-tool-7"
    assert missing["token_status"] == "missing"

    agent = by_raw["agent: review the diff against acceptance"]
    assert agent["kind"] == "agent"
    assert agent["blocked"] is False
    assert agent["token"] is None and agent["token_status"] is None


def test_worker_prompt_renders_preflight_results(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [_contract_task()]))
    prompt = dispatch.worker_prompt(goal, active_task(project, "T001"))
    assert "[preflight:ok] command 'sh -c true'" in prompt
    assert "[preflight:blocked] command 'sudo make test'" in prompt
    assert "[preflight:blocked] command 'orx-preflight-no-such-tool-7 --version'" in prompt
    assert "[preflight:not-run] agent 'agent: review the diff against acceptance'" in prompt
    assert (
        "Pre-flight summary: 3 command check(s): 1 ok, 2 blocked;"
        " 1 agent check(s) not preflighted." in prompt
    )
    # A denied entry is definitive (the completion gate shares the denylist);
    # a missing token is a must-verify flag, not a final verdict.
    assert "the verification denylist denies this check too:" in prompt
    assert "report blocked, do not implement" in prompt
    assert "verify it via the START GATE before implementing" in prompt


def test_preflight_does_not_block_path_like_tokens_the_task_may_create(project, goal):
    """A path-like first token can be a file the task itself is scoped to
    create; a static miss downgrades to undetermined instead of a false
    blocked verdict. Only a bare name absent from PATH is blocked."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance,
                  verification=["./not-yet-created-runner.sh --fast"])
    ]))
    rows = dispatch.preflight_task_checks(active_task(project, "T001"))
    row = rows[0]
    assert row["token"] == "./not-yet-created-runner.sh"
    assert row["token_status"] == "undetermined"
    assert row["blocked"] is False


def test_host_park_assignment_file_carries_preflight_and_contract(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [_contract_task()]))
    out = dispatch.run_slice(project)
    entry = out["host_required"][0]
    assert entry["task"] == "T001"

    text = (project.root / entry["prompt_file"]).read_text()
    assert entry["prompt"] == text
    assert "orx task check T001" in text
    assert "[preflight:ok]" in text and "[preflight:blocked]" in text
    assert "DELIVERY GATE" in text

    # The park entry also carries the same rows as structured data.
    preflight = {row["raw"]: row for row in entry["preflight"]}
    assert preflight["sudo make test"]["blocked"] is True
    assert preflight["sh -c true"]["blocked"] is False
    assert preflight["agent: review the diff against acceptance"]["kind"] == "agent"


def test_cli_launch_path_shares_the_preflight_contract(tmp_path, monkeypatch):
    """The CLI execution path composes the same prompt (contract + preflight)
    and leaves the identical assignment file, even though ORX itself launched
    the worker."""
    project = _cli_project(tmp_path, monkeypatch)
    try:
        goal = _goal_full(project)
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["marker file exists", "summary is written"],
                      verification=["sh -c true", "sudo make test"])
        ]))
        out = dispatch.run_slice(project)
        # The worker (a shell `cat`) succeeded; the denied check then failed
        # verification, so the full outcome lands in `failed`.
        outcome = out["failed"][0]
        assert outcome["task"] == "T001"

        captured = (tmp_path / "worker-prompt-captured.txt").read_text()
        assert "orx task check T001" in captured
        assert "DELIVERY GATE" in captured
        assert "[preflight:ok] command 'sh -c true'" in captured
        assert "[preflight:blocked] command 'sudo make test'" in captured

        # The CLI path writes the same assignment file the park path writes.
        assignment = project.root / outcome["prompt_file"]
        assert assignment.name == "T001.md"
        assert assignment.read_text() == captured

        preflight = {row["raw"]: row for row in outcome["preflight"]}
        assert preflight["sudo make test"]["blocked"] is True
        assert preflight["sh -c true"]["blocked"] is False
    finally:
        project.close()


# ---------------------------------------------------------------------------
# G004 T004: the replan reference chain rides in the worker/verifier prompts.
# Confirm tasks get the supporting material plus THIS task's check
# requirements; redo tasks get the reason and scope; artifacts resolve through
# the declared correspondence with existence/change state; the verifier sees
# the same chain labeled history — never as this task's verification.


def _replan_ir(goal, tasks, prior_revision, task_entries, superseded_entries):
    ir = ir_for(goal, tasks)
    ir["replan"] = {
        "prior_revision": prior_revision,
        "tasks": task_entries,
        "superseded": superseded_entries,
    }
    return ir


def _passed_prior(project, goal, tmp_path, name="prior-evidence.json"):
    """Revision 1 with T001 passed and its completion evidence on disk —
    the state a citing replan task references."""
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["true"]),
    ]))
    revision1 = active_task(project, "T001").revision_id
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    evidence = write_evidence(tmp_path, name)
    dispatch.task_complete(project, "T001", str(evidence))
    return revision1, evidence


def test_first_plan_worker_prompt_omits_the_replan_block(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance)
    ]))
    prompt = dispatch.worker_prompt(goal, active_task(project, "T001"))
    assert "Replan correspondence" not in prompt


def test_replan_confirm_worker_prompt_carries_the_reference_chain(
        project, goal, tmp_path):
    revision1, evidence = _passed_prior(project, goal, tmp_path)
    dispatch.submit_plan(project, _replan_ir(goal, [
        task_spec("T101", acceptance=goal.acceptance, verification=["true"]),
    ], 1, [
        {"task": "T101", "classification": "confirm",
         "sources": [{"revision": 1, "task_id": "T001"}],
         "confirm_verification": ["true"],
         "artifacts": [str(evidence)]},
    ], [
        {"revision": 1, "task_id": "T001", "disposition": "confirmed",
         "successors": ["T101"]},
    ]))
    entry = dispatch.run_slice(project)["host_required"][0]
    assert entry["task"] == "T101"
    prompt = entry["prompt"]
    # Byte-identical to the composition the dispatch layer builds.
    assert prompt == dispatch.worker_prompt(
        goal, active_task(project, "T101"),
        replan_context=dispatch._replan_reference_context(
            project.store, project.root,
            active_task(project, "T101").revision_id, "T101", audience="worker",
        ),
    )
    # Source identity: revision AND task, plus its recorded status.
    assert "Replan correspondence for this task" in prompt
    assert "a task number alone never implies correspondence" in prompt
    assert "classification: confirm — prior passed work this task relies on AS-IS" in prompt
    assert ("1:T001 (revision 1, task T001, part=false) — recorded status:"
            " passed") in prompt
    # attempt/evidence identity of the source's own evidence.
    assert "evidence: [completion]" in prompt
    assert "(evidence row " in prompt and ", attempt " in prompt
    # The artifact resolves through the correspondence, never by number.
    assert f"  - {evidence} -> resolves to recorded source evidence: 1:T001 (source attempt " in prompt
    assert "file present (no delivery snapshot recorded yet — nothing to compare)" in prompt
    assert "never assumed valid by default" in prompt
    # A confirm task confirms applicability + regression only.
    assert "ONLY to confirm this work still applies" in prompt
    assert "necessary regression checks" in prompt
    assert "do NOT redo the work" in prompt
    assert "Current verification requirements for this confirmation" in prompt
    assert "'true'" in prompt
    assert "a prior pass never substitutes" in prompt
    # The archived assignment file carries the same chain.
    assert (project.root / entry["prompt_file"]).read_text() == prompt


def test_replan_redo_worker_prompt_shows_reason_and_scope(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["false"]),
    ]))
    dispatch.run_slice(project)  # parks; revision 1's T001 stays waiting_host
    dispatch.submit_plan(project, _replan_ir(goal, [
        task_spec("T201", acceptance=goal.acceptance, verification=["true"]),
    ], 1, [
        {"task": "T201", "classification": "redo",
         "sources": [{"revision": 1, "task_id": "T001"}],
         "redo_reason": "the check contract changed; the old result cannot prove it"},
    ], [
        {"revision": 1, "task_id": "T001", "disposition": "redone",
         "successors": ["T201"]},
    ]))
    prompt = dispatch.run_slice(project)["host_required"][0]["prompt"]
    assert "classification: redo — this work must be DONE AGAIN (implementation task)" in prompt
    assert ("Redo reason (why this work must be done again, from the replan"
            " declaration): the check contract changed; the old result"
            " cannot prove it") in prompt
    assert "Scope of the redo (the only paths this task may write):" in prompt
    # The source's honest recorded status is shown as history: activation
    # cancelled the unfinished round-1 task (waiting_host -> cancelled).
    assert "recorded status: cancelled" in prompt


def test_replan_worker_prompt_reports_unresolved_and_missing_artifacts(
        project, goal, tmp_path):
    _passed_prior(project, goal, tmp_path)
    dispatch.submit_plan(project, _replan_ir(goal, [
        task_spec("T102", acceptance=goal.acceptance, verification=["true"]),
    ], 1, [
        {"task": "T102", "classification": "confirm",
         "sources": [{"revision": 1, "task_id": "T001"}],
         "confirm_verification": ["true"],
         "artifacts": ["reports/never-created.md"]},
    ], [
        {"revision": 1, "task_id": "T001", "disposition": "confirmed",
         "successors": ["T102"]},
    ]))
    prompt = dispatch.run_slice(project)["host_required"][0]["prompt"]
    assert "  - reports/never-created.md -> names no recorded evidence of the declared sources" in prompt
    assert "semantic review" in prompt
    assert ("state: file MISSING on disk and no delivery snapshot recorded —"
            " the reference cannot be confirmed") in prompt
    assert "never assumed valid by default" in prompt


def test_replan_verifier_prompt_labels_the_chain_as_history(
        project, goal, tmp_path):
    revision1, evidence = _passed_prior(project, goal, tmp_path)
    dispatch.submit_plan(project, _replan_ir(goal, [
        task_spec("T101", acceptance=goal.acceptance,
                  verification=["true", "agent: review the confirmation"]),
    ], 1, [
        {"task": "T101", "classification": "confirm",
         "sources": [{"revision": 1, "task_id": "T001"}],
         "confirm_verification": ["true"],
         "artifacts": [str(evidence)]},
    ], [
        {"revision": 1, "task_id": "T001", "disposition": "confirmed",
         "successors": ["T101"]},
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T101")
    dispatch.task_complete(project, "T101", str(write_evidence(tmp_path, "citing.json")))

    out = dispatch.verify_dispatch(project)
    entry = next(e for e in out["agent_required"] if e["task"] == "T101")
    prompt = entry["prompt"]
    assert "Replan correspondence this task references (recorded history" in prompt
    assert "1:T001 (revision 1, task T001, part=false) — recorded status: passed" in prompt
    # Provenance was recorded at the delivery this verification round judges.
    assert f"-> provenance recorded at delivery: 1:T001 (source attempt " in prompt
    assert f"{evidence}" in prompt
    # The boundary: history never impersonates the current verification.
    assert "recorded HISTORY this task references" in prompt
    assert "NOT this task's verification results" in prompt
    assert "a historical pass never impersonates a current verification" in prompt
    assert "judge only the check below" in prompt
    assert (project.root / entry["prompt_file"]).read_text() == prompt


# ---------------------------------------------------------------------------
# G006 T004: the recovery face's progress observation is read-only.
#
# docs/host-progress-contract.md §7/§9: `orx run`'s recovery entry carries
# the current attempt's latest report and age, and observing it — however
# often — never changes task state, opens an attempt, writes a task event,
# or dispatches anything. Fixed clock; no real sleep.


def test_recovery_progress_observation_is_read_only(project, goal, monkeypatch):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    run = project.store.run_for_goal(goal.id)
    revision = project.store.revision_active(run.id)
    attempt = project.store.attempt_latest_for_task(revision.id, "T001")

    holder = {"now": datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)}

    def now():
        return holder["now"].isoformat(timespec="microseconds")

    monkeypatch.setattr("orx.state.now", now)
    monkeypatch.setattr("orx.dispatch.db_now", now)
    dispatch.task_heartbeat(project, "T001", attempt.id, "checking")
    holder["now"] = holder["now"] + timedelta(seconds=45)

    attempts_before = len(project.store.attempts_all())
    events_before = len(project.store.task_events_all())
    first = dispatch.run_slice(project)["recovery"][0]["progress"]
    second = dispatch.run_slice(project)["recovery"][0]["progress"]
    # Repeated observation of the same frozen instant is identical…
    assert first == second
    assert first["state"] == "reported"
    assert first["attempt"] == attempt.id
    assert first["age_sec"] == 45
    # …and wrote nothing: same attempts, same events, still running.
    assert len(project.store.attempts_all()) == attempts_before
    assert len(project.store.task_events_all()) == events_before
    assert active_task(project, "T001").status == "running"
