"""Replay the captured blocked CLI delivery through real ORX dispatch.

Run with the repository's development Python. No paid model, user database,
or user configuration is touched. Exit 1 is the known regression; exit 0
means both red and green workspace gates preserve the blocked delivery.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))

from conftest import HOST_CONFIG_TOML, HOST_PROFILES_TOML, ir_for, make_project, task_spec
from orx import adapters, dispatch, runtime
from orx.adapters.base import Launch, ProbeReport


def replay(check: str) -> dict:
    payload = {
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
        "usage": {"inputTokens": 46730, "outputTokens": 2341, "cacheReadTokens": 166272},
    }
    with tempfile.TemporaryDirectory(prefix="orx-cursor-replay-") as directory:
        with pytest.MonkeyPatch.context() as patch:
            root = Path(directory)
            patch.chdir(root)
            patch.setenv("ORX_CONFIG_DIR", str(root / "isolated-config"))
            patch.setenv("ORX_DATA_DIR", str(root / "isolated-data"))
            patch.setenv("ORX_QUOTA_PREFLIGHT", "0")
            config = HOST_CONFIG_TOML.replace(
                'profiles = ["host-worker", "host-external"]', 'profiles = ["cli-fake"]'
            )
            profiles = HOST_PROFILES_TOML.replace('harness = "shell"', 'harness = "cursor"')
            project = make_project(root, config, profiles)
            try:
                adapter = adapters.get_adapter("cursor")
                patch.setattr(adapter, "probe", lambda: ProbeReport(True, "captured CLI stand-in"))
                patch.setattr(
                    adapter, "build_worker_launch",
                    lambda **kwargs: Launch(argv=["captured-agent"], cwd=root, timeout=5),
                )
                patch.setattr(
                    runtime, "run_launch",
                    lambda launch: runtime.CommandResult(
                        command="captured-agent", exit_code=0,
                        stdout=json.dumps(payload), stderr="", duration_sec=0.01, timed_out=False,
                    ),
                )
                goal = dispatch.create_goal(
                    project, objective="Replay denied worker checks", acceptance=["checks runnable"],
                    constraints=[], context="",
                )[0]
                dispatch.submit_plan(project, ir_for(goal, [task_spec(
                    "T001", acceptance=goal.acceptance, verification=[check], complexity="low",
                )]))
                dispatch.run_slice(project)
                attempt = project.store.conn.execute(
                    "SELECT id,result,failure_reason FROM attempts WHERE role='worker'"
                ).fetchone()
                task = project.store.conn.execute(
                    "SELECT status,failure_reason FROM tasks WHERE task_id='T001'"
                ).fetchone()
                return {
                    "controller_check": check,
                    "worker_delivery": "blocked",
                    "process_exit": 0,
                    "attempt_result": attempt["result"],
                    "attempt_failure_reason": attempt["failure_reason"],
                    "task_status": task["status"],
                    "task_failure_reason": task["failure_reason"],
                }
            finally:
                project.close()


if __name__ == "__main__":
    outcomes = [replay(check) for check in ("false", "true")]
    print(json.dumps(outcomes, indent=2, ensure_ascii=False))
    preserved = all(result["attempt_result"] == "blocked" for result in outcomes)
    if not preserved:
        print("RED: CLI blocked delivery was discarded; a green controller gate can falsely pass it.")
    raise SystemExit(0 if preserved else 1)
