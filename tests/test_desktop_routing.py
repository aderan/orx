"""ORX's desktop config interface uses an explicit scope and atomic saves."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orx.cli import app


@pytest.fixture
def routing_world(tmp_path, monkeypatch):
    user = tmp_path / "user-config"
    user.mkdir()
    (user / "config.toml").write_text('schema_version = 1\n[plan]\ndepth = "standard"\n')
    profiles = user / "profiles.toml"
    profiles.write_text(
        'schema_version = 1\n[profiles.first]\ndriver = "cli"\nharness = "codex"\n'
        'model = "first-model"\neffort = "high"\ntier = "T1"\n'
        '[profiles.second]\ndriver = "cli"\nharness = "codex"\n'
        'model = "second-model"\neffort = "medium"\ntier = "T2"\n'
    )
    projects = []
    for name, depth in (("Project A", "light"), ("Project B", "deep")):
        project = tmp_path / name
        directory = project / ".orx"
        directory.mkdir(parents=True)
        (directory / "config.toml").write_text(
            f'schema_version = 1\n[plan]\ndepth = "{depth}"\n'
            '[worker]\nprofiles = ["first", "second"]\n'
        )
        (directory / "state.db").write_bytes(b"configuration may not open this database")
        projects.append(project)
    monkeypatch.setenv("ORX_CONFIG_DIR", str(user))
    monkeypatch.setenv("ORX_PROJECT", str(projects[0]))
    monkeypatch.chdir(projects[0])
    return user, projects[0], projects[1], tmp_path / "draft.json"


def invoke(project: Path | None, *args):
    scope = ["--scope", "user"] if project is None else [
        "--scope", "project", "--project-root", str(project)]
    result = CliRunner().invoke(app, ["config", "routing", "--json", *scope, *args])
    assert result.exit_code in (0, 1), (result.output, result.exception)
    return result.exit_code, json.loads(result.output)


def draft(path, revision, **changes):
    path.write_text(json.dumps({"revision": revision, "changes": changes}))


def test_preview_and_save_use_the_explicit_project(routing_world):
    user, first, second, request = routing_world
    _, original = invoke(second)
    assert original["model_selection"]["planner"]["depth"] == "deep"
    assert original["project_root"] == str(second)
    assert original["model_selection"]["worker"]["candidates"][0]["tier"] == "T1"
    state = (second / ".orx/state.db").read_bytes()
    config = second / ".orx/config.toml"
    original_text = config.read_text()

    draft(request, original["revision"], **{
        "plan.depth": "standard", "worker.profiles": ["second", "first"]})
    code, preview = invoke(second, "--request", str(request))
    assert code == 0
    assert preview["target_path"] == str(config)
    assert preview["model_selection"]["planner"]["depth"] == "standard"
    assert config.read_text() == original_text

    code, saved = invoke(second, "--request", str(request), "--apply")
    assert code == 0
    assert saved["model_selection"]["worker"]["candidates"][0]["profile"] == "second"
    assert saved["revision"] != original["revision"]
    assert invoke(first)[1]["model_selection"]["planner"]["depth"] == "light"
    assert (second / ".orx/state.db").read_bytes() == state


def test_stale_and_invalid_drafts_leave_every_value_unchanged(routing_world):
    user, _, project, request = routing_world
    config = project / ".orx/config.toml"
    _, original = invoke(project)
    external = config.read_bytes() + b"\n# external edit\n"
    config.write_bytes(external)
    draft(request, original["revision"], **{"plan.depth": "light"})
    code, result = invoke(project, "--request", str(request), "--apply")
    assert code == 1
    assert "changed" in str(result)
    assert config.read_bytes() == external

    _, latest = invoke(project)
    draft(request, latest["revision"], **{
        "plan.depth": "light", "worker.profiles": ["undefined"]})
    code, result = invoke(project, "--request", str(request), "--apply")
    assert code == 1
    assert "undefined" in str(result)
    assert config.read_bytes() == external


def test_user_scope_respects_user_defaults_and_project_overrides(routing_world):
    user, _, project, request = routing_world
    _, original = invoke(None)
    assert original["project_root"] is None
    assert original["target_path"] == str(user / "config.toml")
    draft(request, original["revision"], **{"plan.depth": "light"})
    code, saved = invoke(None, "--request", str(request), "--apply")
    assert code == 0
    assert saved["model_selection"]["planner"]["depth"] == "light"
    assert invoke(project)[1]["model_selection"]["planner"]["depth"] == "deep"
