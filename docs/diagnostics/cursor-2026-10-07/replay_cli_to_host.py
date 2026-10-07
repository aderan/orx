"""Replay rate-limited CLI -> host fallback without a real model.

Exit 1 is the current bug; exit 0 means dispatch produced a host assignment.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))

from conftest import HOST_CONFIG_TOML, HOST_PROFILES_TOML, ir_for, make_project, task_spec
from orx import dispatch
from orx.records import ORXError


def replay() -> bool:
    with tempfile.TemporaryDirectory(prefix="orx-fallback-replay-") as directory:
        with pytest.MonkeyPatch.context() as patch:
            root = Path(directory)
            patch.chdir(root)
            patch.setenv("ORX_CONFIG_DIR", str(root / "isolated-config"))
            patch.setenv("ORX_DATA_DIR", str(root / "isolated-data"))
            patch.setenv("ORX_QUOTA_PREFLIGHT", "0")
            config = HOST_CONFIG_TOML.replace(
                'profiles = ["host-worker", "host-external"]',
                'profiles = ["cli-fake", "host-worker"]',
            )
            profiles = HOST_PROFILES_TOML.replace(
                'executable = "true"',
                'executable = "/bin/sh"\n'
                'args = ["-c", "echo rate limit exceeded >&2; exit 1"]',
            )
            project = make_project(root, config, profiles)
            try:
                goal = dispatch.create_goal(
                    project, objective="Verify CLI rate limit switches to host",
                    acceptance=["done"], constraints=[], context="",
                )[0]
                dispatch.submit_plan(project, ir_for(goal, [task_spec(
                    "T001", acceptance=goal.acceptance, verification=["true"], complexity="low",
                )]))
                try:
                    outcome = dispatch.run_slice(project)
                except ORXError as exc:
                    print(f"RED: {exc}")
                    print("Expected host_required assignment for host-worker after CLI rate limit.")
                    return False
                recovered = any(
                    item["profile"] == "host-worker" for item in outcome["host_required"]
                )
                print("GREEN: host assignment produced" if recovered else f"RED: {outcome}")
                return recovered
            finally:
                project.close()


if __name__ == "__main__":
    raise SystemExit(0 if replay() else 1)
