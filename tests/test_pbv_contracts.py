"""PBV contract tests (Stage 2, docs/pbv-mapping.md §4).

The assignment/verification contracts the orx-pbv loop relies on:
preread persistence, Goal constraints in every assignment type, verifier
context handoff (acceptance, gate results, evidence, prior issues), host
assignment artifacts with isolation honesty, retry feedback, and
`verify submit --reason` reaching the recorded failure state.

No network, no real agent process, no paid model.
"""

from __future__ import annotations

from pathlib import Path

from orx import dispatch
from orx import plan as plan_mod
from conftest import HOST_CONFIG_TOML, ir_for, make_project, task_spec, write_evidence, active_task


# ---------------------------------------------------------------------------
# preread: IR field, defaults, validation


def test_preread_defaults_empty_and_old_plans_still_parse(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=["marker file exists", "summary is written"])
    ]))
    task = active_task(project, "T001")
    assert task.preread == []


def test_preread_valid_paths_validate_clean(project, goal):
    ir = ir_for(goal, [
        task_spec("T001", acceptance=["marker file exists", "summary is written"],
                  preread=["reports/pbv/round-1-plan.md", "src/store.py"])
    ])
    errors = plan_mod.validate_ir(plan_mod.parse_ir(ir), goal.id, list(goal.acceptance), {"coding"})
    assert errors == []


def test_preread_unsafe_paths_rejected(project, goal):
    ir = ir_for(goal, [
        task_spec("T001", acceptance=["marker file exists", "summary is written"],
                  preread=["/etc/passwd", "../outside.py", "ok.py"])
    ])
    errors = plan_mod.validate_ir(plan_mod.parse_ir(ir), goal.id, list(goal.acceptance), {"coding"})
    assert any("preread path '/etc/passwd'" in e for e in errors)
    assert any("preread path '../outside.py'" in e for e in errors)
    assert not any("'ok.py'" in e for e in errors)


def test_preread_persists_to_task_row(project, goal):
    dispatch.submit_plan(
        project,
        ir_for(goal, [task_spec("T001", acceptance=["marker file exists", "summary is written"],
                                preread=["docs/plan.md", "tests/test_store.py"])]),
    )
    assert active_task(project, "T001").preread == ["docs/plan.md", "tests/test_store.py"]


def test_planner_prompt_mentions_preread(project, goal):
    prompt = plan_mod.planner_prompt(goal, plan_mod.PlanDepth.STANDARD)
    assert "preread" in prompt


# ---------------------------------------------------------------------------
# worker assignments: constraints + preread + prior failure, host artifacts


def _goal_with_constraints(project):
    return dispatch.create_goal(
        project,
        objective="Ship the login fix",
        acceptance=["marker file exists", "summary is written"],
        constraints=["never touch data/", "single writer only"],
        context="",
    )[0]


def test_host_worker_assignment_carries_constraints_preread_and_artifacts(project):
    goal = _goal_with_constraints(project)
    dispatch.submit_plan(
        project,
        ir_for(goal, [task_spec(
            "T001",
            acceptance=["marker file exists", "summary is written"],
            verification=["true", "agent: review the diff"],
            preread=["reports/pbv/round-1-plan.md", "src/store.py"],
        )]),
    )
    out = dispatch.run_slice(project)
    assert out["host_required"], out
    entry = out["host_required"][0]
    assert entry["task"] == "T001"
    assert entry["isolation"] == "prompt_only"
    assert entry["preread"] == ["reports/pbv/round-1-plan.md", "src/store.py"]

    prompt = entry["prompt"]
    assert "never touch data/" in prompt and "single writer only" in prompt
    assert "reports/pbv/round-1-plan.md" in prompt and "src/store.py" in prompt
    assert "marker file exists" in prompt
    # Host and CLI assignments use the same composed prompt, including the
    # v11 identity header whose nonce is minted at park time.
    task = active_task(project, "T001")
    attempt = project.store.attempt_latest_for_task(task.revision_id, "T001")
    assert attempt is not None
    assert "ORX_ASSIGNMENT=orx-assignment:" in prompt.splitlines()[1]
    assert prompt == dispatch.worker_prompt(
        goal, task, identity=dispatch._identity_block(attempt.nonce))

    prompt_file = Path(entry["prompt_file"])
    assert prompt_file.parts[:3] == (".orx", "runs", prompt_file.parts[2])
    assert (project.root / prompt_file).read_text() == prompt


def test_host_worker_prompt_on_retry_carries_prior_failure(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=["marker file exists", "summary is written"])
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_fail(project, "T001", "focused tests failed: imports collide")
    dispatch.task_retry(project, "T001")
    out = dispatch.run_slice(project)
    prompt = out["host_required"][0]["prompt"]
    assert "focused tests failed: imports collide" in prompt
    assert "do not redo the task blindly" in prompt


def test_external_worker_assignment_also_carries_prompt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace('profiles = ["host-worker", "host-external"]',
                                      'profiles = ["host-external"]')
    project = make_project(tmp_path, config_toml=config)
    try:
        goal = dispatch.create_goal(
            project, objective="o", acceptance=["a1"], constraints=["c1"], context="",
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["a1"], preread=["docs/x.md"])
        ]))
        out = dispatch.run_slice(project)
        entry = out["waiting_external"][0]
        assert "c1" in entry["prompt"] and "docs/x.md" in entry["prompt"]
        assert Path(entry["prompt_file"]).is_file()
        assert "isolation" not in entry or entry.get("isolation") is None
    finally:
        project.close()


# ---------------------------------------------------------------------------
# verifier assignments: acceptance, constraints, gate results, evidence, issues


def _review_flow(project, goal):
    dispatch.submit_plan(
        project,
        ir_for(goal, [task_spec(
            "T001",
            acceptance=["marker file exists", "summary is written"],
            verification=["true", "agent: review the diff against acceptance"],
            preread=["docs/plan.md"],
        )]),
    )
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    evidence = write_evidence(Path.cwd())
    dispatch.task_complete(project, "T001", str(evidence))
    return evidence


def test_verifier_assignment_carries_full_context(project):
    goal = _goal_with_constraints(project)
    _review_flow(project, goal)
    out = dispatch.verify_dispatch(project)
    entries = [e for e in out["agent_required"] if e.get("task") == "T001"]
    assert entries
    entry = entries[0]
    prompt = entry["prompt"]

    assert "never touch data/" in prompt and "single writer only" in prompt
    assert "marker file exists" in prompt
    # The recorded deterministic gate result and the worker's evidence hand off.
    assert "true" in prompt and "exit 0" in prompt
    assert "evidence.json" in prompt
    assert entry["isolation"] == "prompt_only"
    assert Path(entry["prompt_file"]).read_text() == prompt
    assert entry["submit_pass"].startswith("orx verify submit T001")


def test_verify_submit_reason_reaches_failure_state_and_next_prompts(project):
    goal = _goal_with_constraints(project)
    _review_flow(project, goal)

    out = dispatch.verify_dispatch(project)
    entry = [e for e in out["agent_required"] if e["task"] == "T001"][0]
    dispatch.verify_submit(project, "T001", "fail", entry["entry"], None,
                           reason="issue1: migration untested; issue2: leaked temp file")

    task = active_task(project, "T001")
    assert task.status == "failed"
    assert "migration untested" in (task.failure_reason or "")

    # Retry: the next worker prompt and the next verifier prompt both see the issues.
    dispatch.task_retry(project, "T001")
    run_out = dispatch.run_slice(project)
    assert "migration untested" in run_out["host_required"][0]["prompt"]

    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(Path.cwd(), "evidence2.json")))
    verify_out = dispatch.verify_dispatch(project)
    ventry = [e for e in verify_out["agent_required"] if e["task"] == "T001"][0]
    assert "migration untested" in ventry["prompt"]
    assert "leaked temp file" in ventry["prompt"]


def test_verify_submit_without_reason_keeps_entry_hint(project, goal):
    _review_flow(project, goal)
    out = dispatch.verify_dispatch(project)
    entry = [e for e in out["agent_required"] if e["task"] == "T001"][0]
    result = dispatch.verify_submit(project, "T001", "fail", entry["entry"], None)
    assert result["status"] == "failed"
    task = active_task(project, "T001")
    assert entry["entry"] in (task.failure_reason or "")
