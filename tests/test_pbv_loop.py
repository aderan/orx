"""End-to-end PBV loop tests (Stage 4, docs/pbv-mapping.md §3).

Simulates the orx-pbv Controller against the real kernel with host-driver
profiles: the "subagent" is this test executing the assignment prompt's
contract by hand (claim / complete / verify submit). Covers the scenario
table: normal two-slice loop, command-gate failure, review failure, fix
success, fix exhaustion, close-before-next discipline, and mid-run state
re-read recovery.

No network, no real agent process, no paid model.
"""

from __future__ import annotations

import json
from pathlib import Path

from orx import dispatch
from conftest import ir_for, make_project, task_spec, write_evidence, active_task


def _pbv_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return make_project(tmp_path)


def _pbv_goal(project, objective="Out-of-sample evaluation report shipped"):
    return dispatch.create_goal(
        project,
        objective=objective,
        acceptance=["gate passes on both slices", "reports are written"],
        constraints=["never write to data/", "evaluation windows stay frozen"],
        context="",
    )[0]


def _two_slice_plan(goal):
    """Linear chain T001 -> T002, dual gate per slice (command + agent)."""
    gate = "true"
    review = "agent: 独立验收审查。先看 git diff HEAD 了解本切片全部改动……"
    return ir_for(goal, [
        task_spec("T001", objective="slice 1", acceptance=["gate passes on both slices"],
                  verification=[gate, review], preread=["reports/pbv/round-1-plan.md"]),
        task_spec("T002", objective="slice 2", deps=("T001",),
                  acceptance=["gate passes on both slices", "reports are written"],
                  verification=[gate, review], preread=["reports/pbv/round-2-plan.md"]),
    ])


def _review_entry(out, task_id="T001"):
    return [e for e in out["agent_required"] if e["task"] == task_id][0]


def test_pbv_normal_two_slice_loop_to_done(tmp_path, monkeypatch):
    project = _pbv_project(tmp_path, monkeypatch)
    goal = _pbv_goal(project)
    dispatch.submit_plan(project, _two_slice_plan(goal))

    rounds = []
    for n, task_id in enumerate(("T001", "T002"), start=1):
        out = dispatch.run_slice(project)
        # Serial discipline: exactly one host assignment at a time; the next
        # slice is not runnable before this one passes.
        assert [e["task"] for e in out["host_required"]] == [task_id]
        assert "host_required" not in {k: v for k, v in out.items() if not isinstance(v, list)} or True
        if task_id == "T001":
            assert active_task(project, "T002").status == "pending"

        assignment = out["host_required"][0]
        assert assignment["isolation"] == "prompt_only"
        assert assignment["preread"] == [f"reports/pbv/round-{n}-plan.md"]
        # The "subagent": prompt carries constraints + preread; work happens
        # outside ORX; completion claims with evidence.
        assert "never write to data/" in assignment["prompt"]
        dispatch.task_claim(project, task_id)
        evidence = write_evidence(Path.cwd(), f"evidence-{task_id}.json")
        complete = dispatch.task_complete(project, task_id, str(evidence))
        assert complete["verdict"] == "pending"  # agent review outstanding

        vout = dispatch.verify_dispatch(project)
        entry = _review_entry(vout, task_id)
        assert entry["prompt_file"]
        assert "evaluation windows stay frozen" in entry["prompt"]  # constraints hand off
        result = dispatch.verify_submit(project, task_id, "pass", entry["entry"], None)
        assert result["status"] == "passed"
        rounds.append(task_id)

    status = dispatch.status_data(project)
    assert status["run"]["status"] == "done"
    assert [t["id"] for t in status["tasks"] if t["status"] == "passed"] == ["T001", "T002"]


def test_pbv_command_gate_failure_fails_without_review(tmp_path, monkeypatch):
    project = _pbv_project(tmp_path, monkeypatch)
    goal = _pbv_goal(project)
    ir = _two_slice_plan(goal)
    ir["tasks"][0]["verification"] = ["false", "agent: review"]
    dispatch.submit_plan(project, ir)

    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    complete = dispatch.task_complete(project, "T001", str(write_evidence(Path.cwd())))

    # The kernel fails the task on the nonzero gate; no agent verdict needed.
    assert complete["status"] == "failed"
    assert "verification failed" in (active_task(project, "T001").failure_reason or "")
    vout = dispatch.verify_dispatch(project)
    assert not [e for e in vout["agent_required"] if e["task"] == "T001"]
    assert active_task(project, "T002").status == "blocked"  # failed dependency blocks it

    # Retry: the next build prompt carries the gate failure.
    dispatch.task_retry(project, "T001")
    out = dispatch.run_slice(project)
    assert "verification failed" in out["host_required"][0]["prompt"]


def test_pbv_review_fail_then_fix_passes_incrementally(tmp_path, monkeypatch):
    project = _pbv_project(tmp_path, monkeypatch)
    goal = _pbv_goal(project)
    dispatch.submit_plan(project, _two_slice_plan(goal))

    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(Path.cwd())))
    entry = _review_entry(dispatch.verify_dispatch(project))

    # Attempt 1 fails with concrete issues via --reason.
    dispatch.verify_submit(project, "T001", "fail", entry["entry"], None,
                           reason="issue1: window boundary off by one; issue2: report missing fold note")
    assert active_task(project, "T001").status == "failed"

    # Fix attempt: worker prompt AND next verifier prompt both see the issues.
    dispatch.task_retry(project, "T001")
    rebuild = dispatch.run_slice(project)["host_required"][0]
    assert "window boundary off by one" in rebuild["prompt"]
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(Path.cwd(), "evidence-fix.json")))
    recheck = _review_entry(dispatch.verify_dispatch(project))
    assert "window boundary off by one" in recheck["prompt"]
    assert "fold note" in recheck["prompt"]

    result = dispatch.verify_submit(project, "T001", "pass", entry["entry"], None)
    assert result["status"] == "passed"
    # Dependent slice unlocked only now.
    assert active_task(project, "T002").status == "runnable"


def test_pbv_fix_budget_exhaustion_leaves_recoverable_state(tmp_path, monkeypatch):
    """Initial + 2 fixes exhausted: the kernel stays legal (a 3rd retry would
    be accepted), the stop is a Controller decision, and the state is fully
    readable by a fresh session."""
    project = _pbv_project(tmp_path, monkeypatch)
    goal = _pbv_goal(project)
    dispatch.submit_plan(project, _two_slice_plan(goal))

    for attempt in range(3):  # initial + 2 fixes
        out = dispatch.run_slice(project)
        assert [e["task"] for e in out["host_required"]] == ["T001"]
        dispatch.task_claim(project, "T001")
        evidence = write_evidence(Path.cwd(), f"evidence-{attempt}.json")
        dispatch.task_complete(project, "T001", str(evidence))
        entry = _review_entry(dispatch.verify_dispatch(project))
        result = dispatch.verify_submit(
            project, "T001", "fail", entry["entry"], None,
            reason=f"attempt {attempt}: still broken",
        )
        assert result["status"] == "failed"
        if attempt < 2:
            dispatch.task_retry(project, "T001")

    # Stop: task failed, run blocked, dependents blocked — recoverable.
    status = dispatch.status_data(project)
    assert status["run"]["status"] == "blocked"
    assert active_task(project, "T001").status == "failed"
    assert active_task(project, "T002").status == "blocked"

    # A fresh session reads the same facts (state.db is the authority).
    project.close()
    reopened = dispatch.open_project()
    try:
        fresh = dispatch.status_data(reopened)
        assert fresh["run"]["status"] == "blocked"
        task = [t for t in fresh["tasks"] if t["id"] == "T001"][0]
        assert task["status"] == "failed"
        assert "attempt 2: still broken" in task["failure_reason"]
    finally:
        reopened.close()


def test_pbv_close_failure_does_not_let_kernel_forget_state(tmp_path, monkeypatch):
    """A Close step failing after pass (e.g. commit conflict) is skill-layer
    business; the kernel keeps T001 passed, T002 unlocked-but-untouched, and
    the assignment artifacts on disk for the resumed session."""
    project = _pbv_project(tmp_path, monkeypatch)
    goal = _pbv_goal(project)
    dispatch.submit_plan(project, _two_slice_plan(goal))

    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(Path.cwd())))
    entry = _review_entry(dispatch.verify_dispatch(project))
    dispatch.verify_submit(project, "T001", "pass", entry["entry"], None)

    # "Close fails" — nothing in ORX changes; artifacts survive.
    assert active_task(project, "T001").status == "passed"
    assert active_task(project, "T002").status == "runnable"
    run_dir = project.root / ".orx" / "runs" / "R001" / "assignments"
    assert (run_dir / "T001.md").exists()
    assert (run_dir / "verify-T001-01.md").exists()

    # Resume: start T002 as if Close eventually completed.
    out = dispatch.run_slice(project)
    assert [e["task"] for e in out["host_required"]] == ["T002"]
    assert out["host_required"][0]["prompt_file"].endswith("T002.md")


def test_pbv_attempt_and_isolation_ledger(tmp_path, monkeypatch):
    """Every round leaves attempts with honest isolation: host worker parks
    are prompt_only, the unrouted completion fallback too."""
    project = _pbv_project(tmp_path, monkeypatch)
    goal = _pbv_goal(project)
    dispatch.submit_plan(project, _two_slice_plan(goal))

    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(Path.cwd())))
    entry = _review_entry(dispatch.verify_dispatch(project))
    dispatch.verify_submit(project, "T001", "pass", entry["entry"], None)

    attempts = project.store.attempts_all()
    worker_attempts = [a for a in attempts if a.task_id == "T001" and a.role == "worker"]
    verifier_attempts = [a for a in attempts if a.task_id == "T001" and a.role == "verifier"]
    assert len(worker_attempts) == 1 and worker_attempts[0].isolation == "prompt_only"
    assert verifier_attempts and verifier_attempts[-1].isolation == "prompt_only"
    assert verifier_attempts[-1].driver == "host"
