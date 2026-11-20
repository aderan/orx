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
    monkeypatch.delenv("ORX_CONFIG_DIR")
    monkeypatch.setenv("ORX_HOME", str(user))
    monkeypatch.setenv("ORX_PROJECT", str(projects[0]))
    monkeypatch.chdir(projects[0])
    return user, projects[0], projects[1], tmp_path / "draft.json"


def invoke(project: Path | None, *args, host=None):
    scope = ["--scope", "user"] if project is None else [
        "--scope", "project", "--project-root", str(project)]
    prefix = ["--host", host] if host else []
    result = CliRunner().invoke(app, [*prefix, "config", "routing", "--json", *scope, *args])
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


def test_desktop_project_preview_reads_a_current_host_candidate(routing_world):
    _, _, project, _ = routing_world
    config = project / ".orx/config.toml"
    config.write_text(config.read_text().replace(
        '["first", "second"]', '["current-host", "second"]'))
    (project / ".orx/profiles.toml").write_text(
        'schema_version = 1\n[profiles.current-host]\ndriver = "host"\n'
        'harness = "current"\nmodel = "unconfigured"\neffort = "medium"\ntier = "T2"\n'
        'capabilities = ["coding", "host_context"]\n'
    )

    code, preview = invoke(project)

    assert code == 0, preview
    candidate = preview["model_selection"]["worker"]["candidates"][0]
    assert candidate["profile"] == "current-host"
    assert candidate["harness"] == "current"
    assert candidate["host_blocked_reason"].startswith("host_not_declared")
    assert candidate["capabilities"] == ["coding", "host_context"]

    _, bound = invoke(project, host="codex")
    assert bound["model_selection"]["controller"]["session_harness"] == "codex"
    bound_candidate = bound["model_selection"]["worker"]["candidates"][0]
    assert bound_candidate["declared_harness"] == "current"
    assert bound_candidate["harness"] == "codex"
    assert bound_candidate["host_blocked_reason"] is None


def test_desktop_contract_exposes_model_definition_and_one_default_depth(routing_world):
    user, _, project, request = routing_world
    config = project / ".orx/config.toml"
    config.write_text(config.read_text().replace('depth = "deep"', 'depth = "auto"'))
    with (user / "profiles.toml").open("a") as profiles:
        profiles.write(
            '[profiles.ocr]\ndriver = "cli"\nharness = "codex"\n'
            'model = { family = "example", version = "2", context = 32000 }\n'
            'tier = "S"\neffort = "max"\nnative_effort = "xhigh"\n'
            'capabilities = ["ocr"]\n'
        )
    _, initial = invoke(project)
    assert initial["model_selection"]["planner"]["depth"] == "standard"
    assert "effective_depth" not in initial["model_selection"]["planner"]
    draft(request, initial["revision"], **{"plan.depth": "standard", "worker.profiles": ["ocr"]})

    code, preview = invoke(project, "--request", str(request))

    assert code == 0, preview
    assert preview["contract"] == 1
    selection = preview["model_selection"]
    assert selection["planner"]["depth"] == "standard"
    assert "effective_depth" not in selection["planner"]
    candidate = selection["worker"]["candidates"][0]
    assert candidate["tier"] == "S"
    assert candidate["capabilities"] == ["ocr"]
    assert candidate["effort"] == "max"
    assert candidate["native_effort"] == "xhigh"
    assert candidate["model_spec"]["family"] == "example"
    assert candidate["model_spec"]["context"] == 32000
