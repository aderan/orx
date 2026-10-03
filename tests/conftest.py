from __future__ import annotations

import json
from pathlib import Path

import pytest

from orx import dispatch

HOST_PROFILES_TOML = """\
schema_version = 1

[profiles.host-planner]
driver = "host"
harness = "zcode"
model = "m-planner"
class = "strong"
effort = "deep"
capabilities = ["coding"]

[profiles.host-worker]
driver = "host"
harness = "zcode"
model = "m-worker"
class = "strong"
effort = "standard"
capabilities = ["coding"]

[profiles.host-frontier]
driver = "host"
harness = "zcode"
model = "m-frontier"
class = "frontier"
effort = "deep"
capabilities = ["coding"]

[profiles.host-external]
driver = "external"
harness = "zcode"
model = "m-ext"
class = "strong"
effort = "standard"
capabilities = ["coding"]

[profiles.host-verifier]
driver = "host"
harness = "zcode"
model = "m-verifier"
class = "strong"
effort = "standard"
capabilities = ["coding"]

[profiles.host-vision]
driver = "host"
harness = "zcode"
model = "m-vision"
class = "strong"
effort = "standard"
capabilities = ["coding", "vision"]

[profiles.cli-fake]
driver = "cli"
harness = "shell"
executable = "true"
prompt_transport = "stdin"
model = "fake-model"
class = "economy"
effort = "quick"
capabilities = ["coding"]
"""

HOST_CONFIG_TOML = """\
schema_version = 1

[controller]
profile = "host-planner"

[plan]
depth = "auto"
allow_class_downgrade = false

[plan.light]
profiles = ["host-planner"]

[plan.standard]
profiles = ["host-planner"]

[plan.deep]
profiles = ["host-frontier", "host-planner"]

[worker]
profiles = ["host-worker", "host-external"]

[verify]
profiles = ["host-verifier", "host-vision"]

[runtime]
max_parallel = 1
command_timeout_sec = 30
"""


def make_project(tmp_path: Path, config_toml: str = HOST_CONFIG_TOML,
                 profiles_toml: str = HOST_PROFILES_TOML) -> dispatch.Project:
    """Init a project and swap in the host-driver fixture configuration."""
    dispatch.init_project(tmp_path)
    (tmp_path / ".orx" / "config.toml").write_text(config_toml)
    (tmp_path / ".orx" / "profiles.toml").write_text(profiles_toml)
    return dispatch.open_project()


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    proj = make_project(tmp_path)
    yield proj
    proj.close()


@pytest.fixture
def goal(project):
    return dispatch.create_goal(
        project,
        objective="Ship the login fix",
        acceptance=["marker file exists", "summary is written"],
        constraints=[],
        context="",
    )[0]


def ir_for(goal, tasks):
    return {
        "goal": goal.id,
        "exploration": {
            "summary": "explored the repo",
            "relevant_components": ["src/"],
            "unknowns": [],
            "assumptions": [],
            "risks": [],
        },
        "approach": {"summary": "straightforward", "decisions": []},
        "tasks": tasks,
    }


def task_spec(tid, objective="do the thing", deps=(), acceptance=(), verification=(),
              caps=("coding",), complexity="medium", allowed=("src/",)):
    return {
        "id": tid,
        "objective": objective,
        "dependencies": list(deps),
        "scope": {"allowed": list(allowed)},
        "acceptance": list(acceptance),
        "verification": list(verification),
        "routing": {"complexity": complexity, "required_capabilities": list(caps)},
    }


def write_evidence(tmp_path: Path, name: str = "evidence.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"summary": "work finished", "commands": [], "artifacts": []}))
    return path


def active_task(project, task_id):
    goal = project.store.goal_active()
    run = project.store.run_for_goal(goal.id)
    revision = project.store.revision_active(run.id)
    return project.store.task_get(revision.id, task_id)
