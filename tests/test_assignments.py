"""Assignment completeness contracts (M1.1 Task 1).

Workers receive Goal constraints, Goal context, task scope, acceptance,
verification requirements, and prior failure feedback. Verifiers receive the
task's acceptance, Goal constraints, gate results, and readable evidence
references. Host and CLI paths hand out the SAME composed assignment — proven
by capturing what the CLI process actually received on stdin. No network, no
real agent, no paid model.
"""

from __future__ import annotations

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
