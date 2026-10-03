"""CLI contract: --json envelopes, exit codes, and a CLI-driven fake lifecycle."""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orx import dispatch
from orx.cli import app

from conftest import HOST_CONFIG_TOML, HOST_PROFILES_TOML, ir_for, task_spec

runner = CliRunner()


@pytest.fixture
def cli_project(tmp_path, monkeypatch):
    monkeypatch.delenv("ORX_PROJECT", raising=False)
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


# -- orx config path / list / get / set ------------------------------------


def _toml(path):
    import tomllib
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def test_config_help_mentions_layers_keys_and_exits_zero():
    listed = invoke("config", "--help")
    assert listed.exit_code == 0
    for word in ("path", "list", "get", "set", "environment", "project", "user"):
        assert word in listed.stdout
    detailed = invoke("config", "set", "--help")
    assert detailed.exit_code == 0
    text = detailed.stdout
    for snippet in (
        "--user",
        "--json",
        "schema_version",
        "controller.profile",
        "worker.profiles",
        "runtime.max_parallel",
        "ORX_CONFIG_DIR",
        "true",
        "JSON array",
    ):
        assert snippet in text, snippet


def test_config_usage_exits_2():
    assert invoke("config", "get").exit_code == 2
    assert invoke("config", "set", "runtime.max_parallel").exit_code == 2
    assert invoke("config", "nope").exit_code == 2


def test_config_path_json_reports_user_and_project_paths(cli_project, tmp_path):
    result = invoke("config", "path", "--json")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    assert Path(body["user_config"]) == (tmp_path / "isolated-config" / "config.toml").resolve()
    assert Path(body["user_profiles"]) == (tmp_path / "isolated-config" / "profiles.toml").resolve()
    assert Path(body["user_data"]) == (tmp_path / "isolated-data").resolve()
    assert Path(body["project_config"]) == (cli_project / ".orx" / "config.toml").resolve()
    assert Path(body["project_profiles"]) == (cli_project / ".orx" / "profiles.toml").resolve()

    human = invoke("config", "path")
    assert human.exit_code == 0
    assert "user config:" in human.stdout
    assert str(body["project_config"]) in human.stdout


def test_config_path_without_project_has_null_project_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("ORX_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    body = payload(invoke("config", "path", "--json"))
    assert body["ok"] is True
    assert body["project_config"] is None
    assert body["project_profiles"] is None
    assert body["user_config"]
    assert body["user_data"]
    human = invoke("config", "path")
    assert "(none)" in human.stdout


def test_config_list_and_get_share_effective_values(cli_project, monkeypatch):
    monkeypatch.setenv("ORX_RUNTIME_COMMAND_TIMEOUT_SEC", "7")
    listed = invoke("config", "list", "--json")
    assert listed.exit_code == 0, listed.stdout
    rows = {row["key"]: row for row in payload(listed)["values"]}
    assert rows["controller.profile"]["value"] == "host-planner"
    assert rows["controller.profile"]["origin"] == "project"
    assert rows["plan.allow_class_downgrade"]["value"] is False
    assert rows["worker.profiles"]["value"] == ["host-worker", "host-external"]
    assert rows["runtime.command_timeout_sec"]["value"] == 7
    assert rows["runtime.command_timeout_sec"]["origin"] == "env"
    assert rows["runtime.max_parallel"]["origin"] == "project"
    assert rows["runtime.max_parallel"]["value"] == 1

    for key, row in rows.items():
        result = invoke("config", "get", "--json", key)
        assert result.exit_code == 0, result.stdout
        got = payload(result)
        assert got["key"] == key
        assert got["value"] == row["value"]
        assert got["origin"] == row["origin"]

    human = invoke("config", "get", "controller.profile")
    assert human.exit_code == 0
    assert "host-planner" in human.stdout
    assert "project" in human.stdout

    missing = invoke("config", "get", "--json", "schema_version")
    assert missing.exit_code == 1
    body = payload(missing)
    assert body["ok"] is False
    assert "schema_version" in body["error"]
    assert body["errors"]


def test_config_set_round_trip_precedence_and_user_create(cli_project, tmp_path, monkeypatch):
    project_config = cli_project / ".orx" / "config.toml"
    project_profiles = cli_project / ".orx" / "profiles.toml"
    project_config.write_text(
        'schema_version = 1\nnote = "keep-me"\n\n[controller]\nprofile = "host-planner"\n'
        '[extra]\nflag = true\n\n[[widgets]]\nname = "a"\n'
    )
    user_config = tmp_path / "isolated-config" / "config.toml"
    assert not user_config.exists()

    monkeypatch.setenv("ORX_RUNTIME_MAX_PARALLEL", "9")
    created = invoke(
        "config", "set", "--user", "--json", "runtime.command_timeout_sec", "11"
    )
    assert created.exit_code == 0, created.stdout
    body = payload(created)
    assert body["ok"] is True
    assert body["created"] is True
    assert body["layer"] == "user"
    assert body["value"] == 11
    user_doc = _toml(user_config)
    assert user_doc == {"schema_version": 1, "runtime": {"command_timeout_sec": 11}}
    assert project_config.read_text() == (
        'schema_version = 1\nnote = "keep-me"\n\n[controller]\nprofile = "host-planner"\n'
        '[extra]\nflag = true\n\n[[widgets]]\nname = "a"\n'
    )

    listed = {row["key"]: row for row in payload(invoke("config", "list", "--json"))["values"]}
    assert listed["runtime.command_timeout_sec"] == {
        "key": "runtime.command_timeout_sec", "value": 11, "origin": "user",
    }
    assert listed["runtime.max_parallel"]["value"] == 9
    assert listed["runtime.max_parallel"]["origin"] == "env"
    assert "max_parallel" not in user_doc["runtime"]

    routing = invoke(
        "config", "set", "--user", "--json", "worker.profiles", "host-worker, host-external"
    )
    assert routing.exit_code == 0, routing.stdout
    assert payload(routing)["created"] is False
    assert payload(routing)["value"] == ["host-worker", "host-external"]
    got = payload(invoke("config", "get", "--json", "worker.profiles"))
    assert got["value"] == ["host-worker", "host-external"]
    assert got["origin"] == "user"

    project_before = project_config.read_bytes()
    profiles_before = project_profiles.read_bytes()
    written = invoke(
        "config", "set", "--json", "worker.profiles", '["host-verifier", "host-vision"]'
    )
    assert written.exit_code == 0, written.stdout
    assert payload(written)["layer"] == "project"
    assert payload(written)["value"] == ["host-verifier", "host-vision"]
    assert project_profiles.read_bytes() == profiles_before
    project_doc = _toml(project_config)
    assert project_doc["schema_version"] == 1
    assert project_doc["note"] == "keep-me"
    assert project_doc["extra"] == {"flag": True}
    assert project_doc["widgets"] == [{"name": "a"}]
    assert project_doc["controller"]["profile"] == "host-planner"
    assert project_doc["worker"]["profiles"] == ["host-verifier", "host-vision"]
    assert _toml(user_config)["worker"]["profiles"] == ["host-worker", "host-external"]
    got = payload(invoke("config", "get", "--json", "worker.profiles"))
    assert got["origin"] == "project"
    assert got["value"] == ["host-verifier", "host-vision"]

    invoke("config", "set", "--json", "runtime.command_timeout_sec", "22")
    assert payload(invoke("config", "get", "--json", "runtime.command_timeout_sec")) == {
        "ok": True, "key": "runtime.command_timeout_sec", "value": 22, "origin": "project",
    }
    assert _toml(user_config)["runtime"]["command_timeout_sec"] == 11
    monkeypatch.setenv("ORX_RUNTIME_COMMAND_TIMEOUT_SEC", "33")
    assert payload(invoke("config", "get", "--json", "runtime.command_timeout_sec"))["value"] == 33
    assert payload(invoke("config", "get", "--json", "runtime.command_timeout_sec"))["origin"] == "env"
    assert _toml(project_config)["runtime"]["command_timeout_sec"] == 22

    depth = invoke("config", "set", "--json", "plan.depth", "light")
    assert depth.exit_code == 0, depth.stdout
    assert payload(invoke("config", "get", "--json", "plan.depth"))["value"] == "light"
    flag = invoke("config", "set", "--json", "plan.allow_class_downgrade", "true")
    assert flag.exit_code == 0, flag.stdout
    assert payload(invoke("config", "get", "--json", "plan.allow_class_downgrade"))["value"] is True
    cleared = invoke("config", "set", "--json", "verify.profiles", "[]")
    assert cleared.exit_code == 0, cleared.stdout
    assert payload(invoke("config", "get", "--json", "verify.profiles"))["value"] == []
    assert project_before != project_config.read_bytes()


def test_config_set_rejects_without_mutating(cli_project, tmp_path):
    project_config = cli_project / ".orx" / "config.toml"
    before = project_config.read_bytes()
    user_config = tmp_path / "isolated-config" / "config.toml"

    cases = [
        ("schema_version", "2"),
        ("not.a.key", "1"),
        ("runtime.max_parallel", "0"),
        ("runtime.max_parallel", "true"),
        ("runtime.command_timeout_sec", "-3"),
        ("plan.depth", "extreme"),
        ("plan.allow_class_downgrade", "yes"),
        ("controller.profile", "ghost"),
        ("worker.profiles", "[1, 2]"),
        ("worker.profiles", "ghost"),
    ]
    for key, value in cases:
        result = invoke("config", "set", "--json", key, value)
        assert result.exit_code == 1, (key, value, result.stdout)
        body = payload(result)
        assert body["ok"] is False
        assert "error" in body
        assert project_config.read_bytes() == before
        assert not user_config.exists()

    broken = project_config.read_bytes()
    result = invoke("config", "set", "--user", "--json", "runtime.max_parallel", "nope")
    assert result.exit_code == 1
    assert payload(result)["ok"] is False
    assert project_config.read_bytes() == broken
    assert not user_config.exists()
    assert not user_config.parent.exists()

    project_config.write_text("schema_version = 1\n[runtime]\nmax_parallel = [\n")
    malformed = project_config.read_bytes()
    result = invoke("config", "set", "--json", "plan.depth", "light")
    assert result.exit_code == 1
    assert project_config.read_bytes() == malformed

    project_config.write_text('note = "only"\n[runtime]\nmax_parallel = 1\n')
    missing_schema = project_config.read_bytes()
    result = invoke("config", "set", "--json", "plan.depth", "light")
    assert result.exit_code == 1
    assert "schema_version" in payload(result)["error"]
    assert project_config.read_bytes() == missing_schema

    project_config.write_text("schema_version = 2\n[runtime]\nmax_parallel = 1\n")
    newer = project_config.read_bytes()
    result = invoke("config", "set", "--json", "plan.depth", "light")
    assert result.exit_code == 1
    assert project_config.read_bytes() == newer


def test_config_set_outside_project(tmp_path, monkeypatch):
    monkeypatch.delenv("ORX_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    refused = invoke("config", "set", "--json", "runtime.max_parallel", "2")
    assert refused.exit_code == 1
    assert payload(refused)["ok"] is False
    assert not (tmp_path / ".orx").exists()

    created = invoke("config", "set", "--user", "--json", "runtime.max_parallel", "2")
    assert created.exit_code == 0, created.stdout
    assert payload(created)["layer"] == "user"
    assert not (tmp_path / ".orx").exists()
    listed = {row["key"]: row for row in payload(invoke("config", "list", "--json"))["values"]}
    assert listed["runtime.max_parallel"]["value"] == 2
    assert listed["runtime.max_parallel"]["origin"] == "user"
    assert listed["plan.depth"]["origin"] == "default"


# -- orx agent list / info / probe ----------------------------------------


def _install_logged_fake(directory: Path, name: str, script: str, log: Path) -> None:
    body = f"#!/bin/sh\necho \"$*\" >> {str(log)!r}\n{script}\n"
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_agent_help_lists_subcommands():
    listed = invoke("agent", "--help")
    assert listed.exit_code == 0
    for word in ("list", "info", "probe", "status", "host-only", "completion"):
        assert word in listed.stdout
    probed = invoke("agent", "probe", "--help")
    assert probed.exit_code == 0
    for snippet in ("--json", "codex", "cursor", "zcode", "completion", "exit 1"):
        assert snippet in probed.stdout


def test_agent_usage_exits_2():
    assert invoke("agent", "info").exit_code == 2
    assert invoke("agent", "probe").exit_code == 2
    assert invoke("agent", "nope").exit_code == 2


def test_agent_list_marks_adapter_harnesses_and_host_only_zcode():
    result = invoke("agent", "list", "--json")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    rows = {row["harness"]: row for row in body["harnesses"]}
    assert list(rows) == ["codex", "cursor", "shell", "zcode"]
    assert rows["codex"]["adapter"] is True and rows["codex"]["probeable"] is True
    assert rows["codex"]["binary"] == "codex" and rows["codex"]["host_only"] is False
    assert rows["cursor"]["adapter"] is True and rows["cursor"]["binary"] == "agent"
    assert rows["shell"]["adapter"] is True and rows["shell"]["probeable"] is False
    assert rows["shell"]["binary"] is None
    assert rows["zcode"] == {
        "harness": "zcode",
        "binary": None,
        "adapter": False,
        "host_only": True,
        "probeable": False,
    }
    human = invoke("agent", "list")
    assert human.exit_code == 0
    assert "zcode" in human.stdout and "host-only" in human.stdout
    assert "codex" in human.stdout and "adapter" in human.stdout


def test_agent_probe_writes_snapshot_and_info_reads_it(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "codex-invocations"
    monkeypatch.setenv("PATH", str(bindir))
    _install_logged_fake(bindir, "codex", """
if [ "$1" = "--version" ]; then echo "codex-cli 0.160.0"; exit 0; fi
if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then
  printf '%s\\n' 'usage: codex exec' '--json' '-m, --model' '-C, --cd' '--output-last-message' 'resume' 'model_reasoning_effort'
  exit 0
fi
if [ "$1" = "login" ]; then echo "Logged in using ChatGPT"; exit 0; fi
if [ "$1" = "debug" ]; then echo '{"models":[{"slug":"m"}]}'; exit 0; fi
echo COMPLETION >> "$0.log"
exit 99
""".replace('"$0.log"', repr(str(tmp_path / "completion-flag"))), log)

    result = invoke("agent", "probe", "--json", "codex")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    snap = body["snapshot"]
    assert snap["harness"] == "codex"
    assert snap["version"] == "codex-cli 0.160.0"
    assert snap["auth"] == "logged_in"
    assert snap["models_discoverable"] is True
    assert snap["features"]["headless"] is True
    assert Path(body["path"]).is_file()
    assert json.loads(Path(body["path"]).read_text()) == snap
    calls = log.read_text().splitlines()
    assert calls == ["--version", "exec --help", "login status", "debug models"]
    assert not (tmp_path / "completion-flag").exists()

    info = invoke("agent", "info", "--json", "codex")
    assert info.exit_code == 0, info.stdout
    shown = payload(info)
    assert shown["ok"] is True
    assert shown["snapshot"] == snap
    assert shown["adapter"] is True
    assert shown["host_only"] is False
    assert "codex exec --json" in shown["launch"]["summary"]
    assert "never runs this completion" in shown["launch"]["summary"]
    human = invoke("agent", "info", "codex")
    assert human.exit_code == 0
    assert "codex-cli 0.160.0" in human.stdout
    assert "logged_in" in human.stdout


def test_agent_probe_cursor_fake_never_prints(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "agent-invocations"
    monkeypatch.setenv("PATH", str(bindir))
    _install_logged_fake(bindir, "agent", """
if [ "$1" = "--version" ]; then echo "agent 2026.10.01"; exit 0; fi
if [ "$1" = "--help" ]; then
  echo "--print --output-format --workspace --trust --model effort= --resume"
  exit 0
fi
if [ "$1" = "status" ]; then echo "Not logged in"; exit 0; fi
if [ "$1" = "--list-models" ]; then echo "auto - Auto"; exit 0; fi
echo COMPLETION
exit 99
""", log)
    result = invoke("agent", "probe", "cursor", "--json")
    assert result.exit_code == 0, result.stdout
    snap = payload(result)["snapshot"]
    assert snap["harness"] == "cursor"
    assert snap["auth"] == "not_logged_in"
    assert snap["binary"].endswith("/agent")
    assert log.read_text().splitlines() == ["--version", "--help", "status", "--list-models"]
    assert all(not line.startswith("--print") for line in log.read_text().splitlines())


def test_agent_probe_missing_binary_is_success(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    (tmp_path / "empty-bin").mkdir()
    result = invoke("agent", "probe", "--json", "codex")
    assert result.exit_code == 0, result.stdout
    snap = payload(result)["snapshot"]
    assert snap["binary"] is None
    assert snap["auth"] == "unknown"
    assert snap["features"]["headless"] is False
    info = payload(invoke("agent", "info", "--json", "codex"))
    assert info["snapshot"] == snap


def test_agent_info_without_snapshot_and_host_only_contract():
    result = invoke("agent", "info", "--json", "zcode")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["snapshot"] is None
    assert body["host_only"] is True
    assert body["adapter"] is False
    assert body["launch"]["kind"] == "host"
    assert "Host-only" in body["launch"]["summary"]
    human = invoke("agent", "info", "zcode")
    assert "host-only" in human.stdout
    assert "snapshot: (none)" in human.stdout

    shell = payload(invoke("agent", "info", "--json", "shell"))
    assert shell["adapter"] is True
    assert shell["snapshot"] is None
    assert "prompt_transport" in shell["launch"]["summary"]


def test_agent_probe_rejects_unprobeable_and_unknown():
    for name in ("zcode", "shell", "nope"):
        result = invoke("agent", "probe", "--json", name)
        assert result.exit_code == 1, (name, result.stdout)
        body = payload(result)
        assert body["ok"] is False
        assert "error" in body
    zcode = payload(invoke("agent", "probe", "--json", "zcode"))
    assert "no probe" in zcode["error"]
    unknown = payload(invoke("agent", "info", "--json", "nope"))
    assert unknown["ok"] is False
    assert "unknown harness" in unknown["error"]
    human = invoke("agent", "probe", "shell")
    assert human.exit_code == 1
    assert "error:" in human.stderr or "error:" in human.stdout


def test_agent_status_help_names_columns():
    result = invoke("agent", "status", "--help")
    assert result.exit_code == 0
    for word in ("PROFILE", "STATE", "SINCE", "REASON", "--json", "cooldown",
                 "quota", "override", "resource_status"):
        assert word in result.stdout


def test_agent_status_outside_project(tmp_path, monkeypatch):
    monkeypatch.delenv("ORX_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    result = invoke("agent", "status", "--json")
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False and "error" in body


def test_agent_status_rows_health_and_marks_override(cli_project):
    before = (cli_project / ".orx" / "profiles.toml").read_bytes()
    project = dispatch.open_project()
    try:
        project.store.resource_learn(
            "host-worker",
            status="cooldown",
            last_error_kind="rate_limited",
            note="slow down",
            cooldown_until="2099-01-01T00:00:00+00:00",
        )
        project.store.resource_learn(
            "cli-fake",
            status="exhausted",
            last_error_kind="quota_exhausted",
            quota_reset_at="2099-06-01T00:00:00+00:00",
        )
        worker_since = project.store.resource_row("host-worker").updated_at
        quota_since = project.store.resource_row("cli-fake").updated_at
    finally:
        project.close()

    held = invoke("resource", "set", "--json", "host-planner", "unavailable", "--note", "held")
    assert held.exit_code == 0, held.stdout

    result = invoke("agent", "status", "--json")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    rows = {row["profile"]: row for row in body["profiles"]}
    configured = {
        "host-planner", "host-worker", "host-frontier", "host-external",
        "host-verifier", "host-vision", "cli-fake",
    }
    assert configured <= set(rows)
    assert "orx-host" in rows  # seeded at init, no longer in profiles.toml

    worker = rows["host-worker"]
    assert worker["state"] == "cooldown"
    assert worker["since"] == worker_since == worker["updated_at"]
    assert worker["last_error_kind"] == "rate_limited"
    assert worker["note"] == "slow down"
    assert worker["cooldown_until"] == "2099-01-01T00:00:00+00:00"
    assert worker["override"] is False
    assert worker["reason"] == (
        "rate_limited; slow down; retry 2099-01-01T00:00:00+00:00"
    )

    quota = rows["cli-fake"]
    assert quota["state"] == "exhausted"
    assert quota["since"] == quota_since
    assert quota["quota_reset_at"] == "2099-06-01T00:00:00+00:00"
    assert quota["reason"] == "quota_exhausted; reset 2099-06-01T00:00:00+00:00"

    planner = rows["host-planner"]
    assert planner["state"] == "unavailable"
    assert planner["override"] is True
    assert planner["since"]
    assert "held" in planner["reason"] and planner["reason"].endswith("override")

    untouched = rows["host-frontier"]
    assert untouched["state"] == "unknown"
    assert untouched["since"] is None
    assert untouched["reason"] == ""
    assert untouched["override"] is False

    human = invoke("agent", "status")
    assert human.exit_code == 0
    lines = human.stdout.splitlines()
    header = lines[0]
    assert header.index("PROFILE") < header.index("STATE") < header.index("SINCE") < header.index("REASON")
    by_profile = {line.split()[0]: line for line in lines[1:]}
    assert set(configured) <= set(by_profile)
    assert worker_since in by_profile["host-worker"]
    assert "cooldown" in by_profile["host-worker"]
    assert "retry 2099-01-01T00:00:00+00:00" in by_profile["host-worker"]
    assert "rate_limited" in by_profile["host-worker"]
    assert "slow down" in by_profile["host-worker"]
    assert quota_since in by_profile["cli-fake"]
    assert "reset 2099-06-01T00:00:00+00:00" in by_profile["cli-fake"]
    assert planner["since"] in by_profile["host-planner"]
    assert "override" in by_profile["host-planner"]
    assert "held" in by_profile["host-planner"]
    assert (cli_project / ".orx" / "profiles.toml").read_bytes() == before


def test_config_list_json_error_on_bad_env(cli_project, monkeypatch):
    before = (cli_project / ".orx" / "config.toml").read_bytes()
    monkeypatch.setenv("ORX_RUNTIME_MAX_PARALLEL", "nope")
    result = invoke("config", "list", "--json")
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False
    assert "ORX_RUNTIME_MAX_PARALLEL" in body["error"]
    assert (cli_project / ".orx" / "config.toml").read_bytes() == before
