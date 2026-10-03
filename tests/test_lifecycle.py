"""Full fake lifecycle (no AI provider), restart/recovery, run completion."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from orx import dispatch

from conftest import ir_for, task_spec, write_evidence


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
        assert reopened.store.schema_version() == 1
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
