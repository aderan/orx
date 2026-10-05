from __future__ import annotations

import json
from pathlib import Path

import pytest

from orx import dispatch


@pytest.fixture(autouse=True)
def isolate_user_layer(tmp_path, monkeypatch):
    """Every test sees an empty M1 user layer: a real ~/.config/orx on the
    developer machine must never leak into test projects."""
    monkeypatch.setenv("ORX_CONFIG_DIR", str(tmp_path / "isolated-config"))
    monkeypatch.setenv("ORX_DATA_DIR", str(tmp_path / "isolated-data"))
    yield

HOST_PROFILES_TOML = """\
schema_version = 1

[profiles.host-planner]
driver = "host"
harness = "zcode"
model = "m-planner"
class = "strong"
effort = "high"
capabilities = ["coding"]

[profiles.host-worker]
driver = "host"
harness = "zcode"
model = "m-worker"
class = "strong"
effort = "medium"
capabilities = ["coding"]

[profiles.host-frontier]
driver = "host"
harness = "zcode"
model = "m-frontier"
class = "frontier"
effort = "high"
capabilities = ["coding"]

[profiles.host-external]
driver = "external"
harness = "zcode"
model = "m-ext"
class = "strong"
effort = "medium"
capabilities = ["coding"]

[profiles.host-verifier]
driver = "host"
harness = "zcode"
model = "m-verifier"
class = "strong"
effort = "medium"
capabilities = ["coding"]

[profiles.host-vision]
driver = "host"
harness = "zcode"
model = "m-vision"
class = "strong"
effort = "medium"
capabilities = ["coding", "vision"]

[profiles.cli-fake]
driver = "cli"
harness = "shell"
executable = "true"
prompt_transport = "stdin"
model = "fake-model"
class = "economy"
effort = "low"
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


# -- replan mapping declaration helpers (G004) ------------------------------
#
# The correspondence between old and new tasks is DECLARED, never inferred
# from task numbers: a replan IR carries an explicit "replan" mapping built
# from these literals (docs/replan-contract.md §2-6).


def replan_source(revision, task_id, part=False):
    return {"revision": revision, "task_id": task_id, "part": part}


def replan_task_entry(task, classification, sources=(), redo_reason="",
                      confirm_verification=(), artifacts=()):
    return {
        "task": task,
        "classification": classification,
        "sources": [
            s if isinstance(s, dict) else replan_source(*s) for s in sources
        ],
        "redo_reason": redo_reason,
        "confirm_verification": list(confirm_verification),
        "artifacts": list(artifacts),
    }


def superseded_entry(revision, task_id, disposition, successors=(), note=""):
    return {
        "revision": revision,
        "task_id": task_id,
        "disposition": disposition,
        "successors": list(successors),
        "note": note,
    }


def with_replan(ir, prior_revision, task_entries, superseded_entries):
    """Copy of a first-plan IR carrying the declared replan mapping."""
    out = dict(ir)
    out["replan"] = {
        "prior_revision": prior_revision,
        "tasks": list(task_entries),
        "superseded": list(superseded_entries),
    }
    return out


def task_spec(tid, objective="do the thing", deps=(), acceptance=(), verification=(),
              caps=("coding",), complexity="medium", allowed=("src/",), preread=()):
    return {
        "id": tid,
        "objective": objective,
        "dependencies": list(deps),
        "scope": {"allowed": list(allowed)},
        "acceptance": list(acceptance),
        "verification": list(verification),
        "routing": {"complexity": complexity, "required_capabilities": list(caps)},
        "preread": list(preread),
    }


def write_evidence(tmp_path: Path, name: str = "evidence.json", *, status: str = "passed",
                   checks=(), artifacts=(), summary: str = "work finished") -> Path:
    """A structured delivery result — the evidence shape `orx task complete`
    validates: status/checks/artifacts/summary. The delivery gate re-runs the
    command entries itself, so an empty checks list is a legal success claim.
    The legacy {summary, commands, artifacts} keys stay legal alongside the
    new ones; tests that need a failed/blocked delivery pass status=
    (and report the red checks they saw)."""
    path = tmp_path / name
    path.write_text(json.dumps({
        "status": status,
        "summary": summary,
        "checks": list(checks),
        "artifacts": list(artifacts),
        # legacy shape keys: still accepted by the evidence schema
        "commands": [],
    }))
    return path


def active_task(project, task_id):
    goal = project.store.goal_active()
    run = project.store.run_for_goal(goal.id)
    revision = project.store.revision_active(run.id)
    return project.store.task_get(revision.id, task_id)
