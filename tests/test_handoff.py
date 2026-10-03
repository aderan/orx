"""Cross-phase handoff scenario (M1.1 Task 4) with fixed stub agents.

Phase 1 completes; phase 2 discovers a broken assumption; the Controller
supplies this round's intent via `replan --context-file`; the Planner's
assignment carries the passed-work facts and the failure evidence; a new plan
replaces the old one; the remaining work executes and verifies to done. All
inputs and state transitions are checked against the contracts — no model
guessing, no paid model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orx import dispatch
from orx.cli import app

from conftest import HOST_CONFIG_TOML, HOST_PROFILES_TOML, ir_for, task_spec, write_evidence, active_task

runner = CliRunner()


def test_cross_phase_handoff_full_scenario(project, tmp_path):
    goal = dispatch.create_goal(
        project,
        objective="Ship the two-phase inventory tool",
        acceptance=["phase one marker file exists", "phase two summary file exists"],
        constraints=["do not touch data/"],
        context="the cache module was rewritten last week; see docs/cache.md",
    )[0]

    # ---- phase 1 plan: T001 lands, T002 depends on it ---------------------
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=["phase one marker file exists"],
                  verification=["test -f marker.txt"], preread=["docs/cache.md"]),
        task_spec("T002", deps=["T001"], objective="write the summary",
                  acceptance=["phase two summary file exists"],
                  verification=["test -f summary.txt"]),
    ]))

    out = dispatch.run_slice(project)
    worker_prompt = out["host_required"][0]["prompt"]
    # the worker assignment is self-contained: goal context rides along
    assert "do not touch data/" in worker_prompt
    assert "docs/cache.md" in worker_prompt

    dispatch.task_claim(project, "T001")
    (project.root / "marker.txt").write_text("phase 1 done\n")  # the host "worker"
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    assert active_task(project, "T001").status == "passed"

    # ---- phase 2 discovers the assumption error ---------------------------
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T002")
    dispatch.task_fail(
        project, "T002",
        "assumption broken: the summary helper module this task assumed does not"
        " exist; phase-2 approach is invalid",
    )

    # ---- Controller intent file for this round ----------------------------
    intent = tmp_path / "round2-intent.md"
    intent.write_text(
        "Reason: phase 2 assumed summary_helper.py exists; it was removed.\n"
        "Intent: keep phase 1 as-is; plan the summary work without that helper.\n"
        "Supporting material: docs/cache.md line 12 confirms the removal.\n"
    )
    goal_before = goal_tuple(project)

    routed = dispatch.plan_route(project, context_file=str(intent))
    assert routed["replan"] is True

    # ---- the Planner's input separates goal / facts / intent --------------
    planner_prompt = routed["assignment"]["prompt"]
    assert goal.objective in planner_prompt                      # original goal
    assert "T001" in planner_prompt and "PASSED" in planner_prompt   # execution facts
    assert "test -f marker.txt" in planner_prompt                 # verification reference
    assert "summary_helper.py" in planner_prompt                  # failure evidence
    assert intent.read_text().strip() in planner_prompt           # this round's intent
    assert "never auto-passes" in planner_prompt
    # the input the planner receives is archived for later audit
    assert (project.root / routed["assignment"]["prompt_file"]).read_text() == planner_prompt

    # ---- the (stub) planner's new plan commits; boundary rules hold -------
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T101", objective="keep the phase-1 marker true",
                  acceptance=["phase one marker file exists"],
                  verification=["test -f marker.txt"]),
        task_spec("T102", deps=["T101"], objective="write the summary directly",
                  acceptance=["phase two summary file exists"],
                  verification=["test -f summary.txt"]),
    ]))
    assert goal_tuple(project) == goal_before  # intent never rewrote the Goal

    statuses = {t["id"]: t["status"] for t in dispatch.task_list(project)}
    assert statuses == {"T101": "runnable", "T102": "pending"}  # no auto-pass
    old = project.store.conn.execute(
        "SELECT t.task_id, t.status FROM tasks t JOIN plan_revisions r"
        " ON t.revision_id = r.id WHERE r.revision = 1 ORDER BY t.id"
    ).fetchall()
    # terminal states are never rewritten: passed stays a fact, the recorded
    # failure stays the evidence for why this replan happened
    assert [(r["task_id"], r["status"]) for r in old] == [
        ("T001", "passed"), ("T002", "failed")
    ]

    # ---- remaining work executes and verifies -----------------------------
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T101")
    dispatch.task_complete(project, "T101", str(write_evidence(tmp_path, "e-t101.json")))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T102")
    (project.root / "summary.txt").write_text("phase 2 done\n")  # the host "worker"
    result = dispatch.task_complete(project, "T102", str(write_evidence(tmp_path, "e-t102.json")))

    assert result["status"] == "passed"
    data = dispatch.status_data(project)
    assert data["run"]["status"] == "done"
    assert data["goal"]["status"] == "done"
    assert data["verification"]["failed"] == 0


def goal_tuple(project):
    rows = project.store.conn.execute(
        "SELECT id, objective, constraints_json, acceptance_json, context, status"
        " FROM goals ORDER BY id"
    ).fetchall()
    return [tuple(r) for r in rows]


# ---------------------------------------------------------------------------
# CLI surface: `orx replan --context-file`


@pytest.fixture
def cli_project(tmp_path, monkeypatch):
    monkeypatch.delenv("ORX_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    dispatch.init_project(tmp_path)
    (tmp_path / ".orx" / "config.toml").write_text(HOST_CONFIG_TOML)
    (tmp_path / ".orx" / "profiles.toml").write_text(HOST_PROFILES_TOML)
    return tmp_path


def _cli_phase1_failed(root: Path):
    project = dispatch.open_project()
    try:
        goal = dispatch.create_goal(
            project, objective="Ship the tool",
            acceptance=["marker file exists", "summary is written"], constraints=[],
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=goal.acceptance[:1], verification=["true"]),
            task_spec("T002", deps=["T001"], acceptance=goal.acceptance[1:]),
        ]))
        dispatch.run_slice(project)
        dispatch.task_claim(project, "T001")
        dispatch.task_complete(project, "T001", str(root / "evidence.json")
                               if (root / "evidence.json").exists() else str(_evidence(root)))
        dispatch.run_slice(project)
        dispatch.task_claim(project, "T002")
        dispatch.task_fail(project, "T002", "assumption broken mid-phase")
        return goal.id
    finally:
        project.close()


def _evidence(root: Path) -> Path:
    return write_evidence(root)


def test_cli_replan_context_file_flow(cli_project):
    goal_id = _cli_phase1_failed(cli_project)
    intent = cli_project / "intent.md"
    intent.write_text("Reason: phase 2 assumption broke.\nIntent: redo phase 2 only.\n")

    result = runner.invoke(app, ["replan", "--context-file", "intent.md", "--json"])
    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    assert body["ok"] is True
    assert body["replan"] is True
    assert body["context_file"] == "intent.md"

    prompt_file = cli_project / body["assignment"]["prompt_file"]
    prompt = prompt_file.read_text()
    assert goal_id in prompt
    assert "T001" in prompt and "PASSED" in prompt
    assert "assumption broken mid-phase" in prompt
    assert "Reason: phase 2 assumption broke." in prompt

    # the human surface points at the archived input
    human = runner.invoke(app, ["replan", "--context-file", "intent.md"])
    assert human.exit_code == 0, human.stdout
    assert "replan: execution-fact snapshot attached" in human.stdout
    assert "context file: intent.md" in human.stdout


def test_cli_replan_context_file_missing_fails_clean(cli_project):
    _cli_phase1_failed(cli_project)
    result = runner.invoke(app, ["replan", "--context-file", "missing.md", "--json"])
    assert result.exit_code == 1
    body = json.loads(result.stdout)
    assert body["ok"] is False
    assert "cannot read" in body["error"]
