"""CLI contract: --json envelopes, exit codes, and a CLI-driven fake lifecycle."""

from __future__ import annotations

import json
import shutil

import pytest
from typer.testing import CliRunner

from orx import dispatch
from orx.cli import app

from conftest import HOST_CONFIG_TOML, HOST_PROFILES_TOML, ir_for, task_spec

runner = CliRunner()


@pytest.fixture
def cli_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dispatch.init_project(tmp_path)
    (tmp_path / ".orx" / "config.toml").write_text(HOST_CONFIG_TOML)
    (tmp_path / ".orx" / "profiles.toml").write_text(HOST_PROFILES_TOML)
    return tmp_path


def invoke(*args):
    return runner.invoke(app, list(args))


def payload(result):
    return json.loads(result.stdout)


def test_version_envelope():
    result = invoke("version", "--json")
    assert result.exit_code == 0
    assert payload(result)["ok"] is True
    assert payload(result)["version"]


def test_init_creates_orx_layout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke("init", "--json")
    assert result.exit_code == 0
    body = payload(result)
    assert body["ok"] is True
    for name in (".orx/config.toml", ".orx/profiles.toml", ".orx/state.db"):
        assert (tmp_path / name).exists()
    # second init refuses to clobber
    result = invoke("init", "--json")
    assert result.exit_code == 1
    assert payload(result)["ok"] is False


def test_commands_outside_project_fail_with_envelope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke("status", "--json")
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False and "error" in body


def test_doctor_ok_on_valid_project(cli_project, monkeypatch):
    # Keep the environment deterministic: no external harness probes.
    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name: None if name in ("codex", "agent") else real_which(name)
    )
    result = invoke("doctor", "--json")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    names = {c["name"]: c for c in body["checks"]}
    assert names["config"]["state"] == "ok"
    assert names["state_db"]["state"] == "ok"
    assert names["codex"]["state"] == "warn"  # missing CLI is a warning, not failure


def test_doctor_fails_on_broken_config(cli_project):
    (cli_project / ".orx" / "config.toml").write_text("schema_version = 1\nnonsense = [\n")
    result = invoke("doctor", "--json")
    assert result.exit_code == 1


def test_goal_new_and_show_envelopes(cli_project):
    result = invoke("goal", "new", "--json",
                    "--objective", "ship it",
                    "--acceptance", "tests pass",
                    "--acceptance", "docs updated")
    assert result.exit_code == 0
    body = payload(result)
    assert body["goal"]["id"] == "G001"
    assert body["run"]["id"] == "R001"
    assert body["goal"]["acceptance"] == ["tests pass", "docs updated"]

    result = invoke("goal", "show", "--json")
    assert payload(result)["goal"]["id"] == "G001"


def test_plan_returns_host_assignment(cli_project):
    invoke("goal", "new", "--json", "--objective", "ship it", "--acceptance", "tests pass")
    result = invoke("plan", "--json")
    assert result.exit_code == 0
    body = payload(result)
    assert body["ok"] is True
    assert body["mode"] == "host_required"
    assert body["assignment"]["id"] == "P001"
    assert body["assignment"]["submit"].startswith("orx plan submit")
    assert "tests pass" in body["assignment"]["prompt"]


def test_plan_submit_rejects_invalid_plan_with_errors(cli_project):
    invoke("goal", "new", "--json", "--objective", "ship it", "--acceptance", "tests pass")
    plan_path = cli_project / "plan.json"
    plan_path.write_text(json.dumps({  # paraphrased acceptance: must be rejected
        "goal": "G001",
        "exploration": {}, "approach": {},
        "tasks": [{"id": "T001", "objective": "x", "dependencies": [],
                    "scope": {"allowed": ["src/"]}, "acceptance": ["the tests pass"],
                    "verification": [], "routing": {"complexity": "low"}}],
    }))
    result = invoke("plan", "submit", "--json", "--file", str(plan_path))
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False
    assert any("verbatim" in e for e in body["errors"])


def test_resource_commands_and_toml_untouched(cli_project):
    before = (cli_project / ".orx" / "profiles.toml").read_bytes()
    result = invoke("resource", "set", "--json", "host-worker", "constrained", "--note", "tight")
    assert result.exit_code == 0
    result = invoke("resource", "list", "--json")
    rows = payload(result)["resources"]
    row = next(r for r in rows if r["profile"] == "host-worker")
    assert row["status"] == "constrained"
    assert (cli_project / ".orx" / "profiles.toml").read_bytes() == before

    result = invoke("resource", "set", "--json", "ghost", "available")
    assert result.exit_code == 1


def test_profiles_lists_definitions(cli_project):
    result = invoke("profiles", "--json")
    rows = payload(result)["profiles"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["host-vision"]["capabilities"] == ["coding", "vision"]
    assert by_name["host-vision"]["resource_status"] == "unknown"


def test_cli_fake_lifecycle_goal_to_done(cli_project):
    invoke("goal", "new", "--json", "--objective", "ship it",
           "--acceptance", "marker exists", "--acceptance", "summary written")
    invoke("plan", "--json")

    plan = ir_for(type("G", (), {"id": "G001"})(), [
        task_spec("T001", acceptance=["marker exists"], verification=["test -f done.marker"]),
        task_spec("T002", deps=["T001"], acceptance=["summary written"]),
    ])
    plan_path = cli_project / "plan.json"
    plan_path.write_text(json.dumps(plan))
    result = invoke("plan", "submit", "--json", "--file", str(plan_path))
    assert result.exit_code == 0
    assert payload(result)["revision"] == 1

    result = invoke("run", "--json")
    assert payload(result)["host_required"][0]["task"] == "T001"

    result = invoke("task", "list", "--json")
    assert payload(result)["count"] == 2

    result = invoke("task", "claim", "--json", "T001")
    assert payload(result)["status"] == "running"

    (cli_project / "done.marker").write_text("ok")
    (cli_project / "ev1.json").write_text('{"summary": "done"}')
    result = invoke("task", "complete", "--json", "T001", "--evidence", "ev1.json")
    assert payload(result)["status"] == "passed"

    result = invoke("run", "--json")
    assert payload(result)["host_required"][0]["task"] == "T002"
    invoke("task", "claim", "--json", "T002")
    result = invoke("task", "complete", "--json", "T002", "--evidence", "ev1.json")
    assert payload(result)["status"] == "passed"

    result = invoke("status", "--json")
    body = payload(result)
    assert body["run"]["status"] == "done"
    assert body["goal"]["status"] == "done"

    result = invoke("status")  # human layout
    assert result.exit_code == 0
    assert "DONE" in result.stdout
    assert "✓ T001" in result.stdout


def test_task_fail_and_retry_via_cli(cli_project):
    invoke("goal", "new", "--json", "--objective", "ship it", "--acceptance", "a1")
    invoke("plan", "--json")
    plan = ir_for(type("G", (), {"id": "G001"})(), [
        task_spec("T001", acceptance=["a1"], verification=["true"]),
    ])
    (cli_project / "plan.json").write_text(json.dumps(plan))
    invoke("plan", "submit", "--json", "--file", str(cli_project / "plan.json"))
    invoke("run", "--json")
    invoke("task", "claim", "--json", "T001")

    result = invoke("task", "fail", "--json", "T001", "--reason", "blocked upstream")
    assert payload(result)["status"] == "failed"

    result = invoke("task", "retry", "--json", "T001")
    assert payload(result)["status"] == "runnable"

    # verify submit requires a verifying task
    result = invoke("verify", "submit", "--json", "T001", "--result", "pass")
    assert result.exit_code == 1
    assert payload(result)["ok"] is False


def test_verify_submit_envelope(cli_project):
    invoke("goal", "new", "--json", "--objective", "ship it", "--acceptance", "a1")
    invoke("plan", "--json")
    plan = ir_for(type("G", (), {"id": "G001"})(), [
        task_spec("T001", acceptance=["a1"], verification=["agent: looks fine"]),
    ])
    (cli_project / "plan.json").write_text(json.dumps(plan))
    invoke("plan", "submit", "--json", "--file", str(cli_project / "plan.json"))
    invoke("run", "--json")
    invoke("task", "claim", "--json", "T001")
    (cli_project / "ev.json").write_text('{"summary": "done"}')
    result = invoke("task", "complete", "--json", "T001", "--evidence", "ev.json")
    assert payload(result)["status"] == "verifying"

    result = invoke("verify", "submit", "--json", "T001", "--result", "pass",
                    "--evidence", "ev.json")
    assert result.exit_code == 0
    body = payload(result)
    assert body["status"] == "passed"
    assert body["entry"] == "agent: looks fine"

    result = invoke("verify", "--json")  # nothing outstanding
    assert payload(result)["agent_required"] == []
