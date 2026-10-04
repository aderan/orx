"""Phase C: reliability and delivery (docs/zcode-subagent-analysis.md §7).

Acceptance criteria:
- init respects an installed user preset (does not shadow it with project defaults);
- existing project overrides are preserved (preset writes only the user layer);
- role definitions (ZCode agent files) vs profiles consistency is checkable (doctor);
- recovery never creates a second writer for a running host task.
"""

from __future__ import annotations

import pytest

from orx import dispatch
from orx.config import ConfigError, load_effective
from orx.presets import install_preset, list_presets, preset_dir, source_agents_dir
from orx.records import ConflictError

from conftest import ir_for, make_project, task_spec, write_evidence


@pytest.fixture
def user_layer(tmp_path):
    """An isolated user layer directory wired via ORX_CONFIG_DIR."""
    import os
    directory = tmp_path / "userlayer"
    directory.mkdir()
    os.environ["ORX_CONFIG_DIR"] = str(directory)
    yield directory
    del os.environ["ORX_CONFIG_DIR"]


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    directory = tmp_path / "zcode-agents"
    directory.mkdir()
    monkeypatch.setenv("ORX_ZCODE_AGENTS_DIR", str(directory))
    return directory


# ---------------------------------------------------------------------------
# Packaged preset


def test_zcode_preset_packaged_files_are_valid():
    directory = preset_dir("zcode")
    assert (directory / "config.toml").exists()
    assert (directory / "profiles.toml").exists()
    from orx.config import load_config, load_profiles
    config = load_config(directory / "config.toml")
    profiles = load_profiles(directory / "profiles.toml")
    assert set(profiles) == {
        "zcode-controller", "zcode-worker",
        "zcode-verifier-flash", "zcode-verifier-strong",
    }
    worker = profiles["zcode-worker"]
    assert worker.host_mode == "subagent"
    assert worker.agent_ref == "orx-worker"
    # Every routing reference resolves inside the preset itself.
    assert config.controller_profile in profiles
    assert set(config.worker_profiles + config.verify_profiles) <= set(profiles)
    # The agent definitions the preset's agent_refs point at ship with ORX.
    source = source_agents_dir()
    for ref in ("orx-worker", "orx-verifier", "orx-verifier-strong"):
        assert (source / f"{ref}.md").exists(), ref


def test_list_presets_includes_zcode():
    presets = list_presets()
    assert any(p["name"] == "zcode" for p in presets)


# ---------------------------------------------------------------------------
# Preset install: user layer only, no silent overwrites


def test_install_into_empty_user_layer_writes_preset(user_layer):
    report = install_preset("zcode")
    assert report["profiles_added"] == [
        "zcode-controller", "zcode-verifier-flash",
        "zcode-verifier-strong", "zcode-worker",
    ] or set(report["profiles_added"]) == {
        "zcode-controller", "zcode-verifier-flash",
        "zcode-verifier-strong", "zcode-worker",
    }
    assert (user_layer / "profiles.toml").exists()
    assert (user_layer / "config.toml").exists()
    # The preset is effective for a project with no layers of its own.
    effective = load_effective(user_layer / "empty.toml", user_layer / "empty.toml")
    assert effective.config.controller_profile == "zcode-controller"
    assert effective.config.worker_profiles == ["zcode-worker"]
    assert effective.config.verify_profiles == ["zcode-verifier-flash", "zcode-verifier-strong"]


def test_install_refuses_conflicting_profile_and_touches_nothing(user_layer):
    (user_layer / "profiles.toml").write_text(
        "schema_version = 1\n\n"
        '[profiles.zcode-worker]\ndriver = "host"\nharness = "zcode"\n'
        'model = "other/model"\nclass = "strong"\neffort = "low"\n'
        'host_mode = "subagent"\nagent_ref = "someone-else"\ncapabilities = ["coding"]\n'
    )
    before = (user_layer / "profiles.toml").read_text()
    with pytest.raises(ConfigError, match="zcode-worker"):
        install_preset("zcode")
    assert (user_layer / "profiles.toml").read_text() == before


def test_install_preserves_existing_user_config_keys(user_layer):
    # The user already routes the controller to their own profile…
    (user_layer / "profiles.toml").write_text(
        "schema_version = 1\n\n"
        '[profiles.my-controller]\ndriver = "host"\nharness = "zcode"\n'
        'model = "m"\nclass = "strong"\neffort = "high"\ncapabilities = ["coding"]\n'
    )
    (user_layer / "config.toml").write_text(
        "schema_version = 1\n\n[controller]\nprofile = \"my-controller\"\n"
    )
    report = install_preset("zcode")
    assert "controller.profile" in report["config_preserved"]
    assert "worker.profiles" in report["config_added"]
    effective = load_effective(user_layer / "empty.toml", user_layer / "empty.toml")
    assert effective.config.controller_profile == "my-controller"  # user wins
    assert effective.config.worker_profiles == ["zcode-worker"]  # preset fills gaps


def test_install_never_touches_project_layers(user_layer, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    install_preset("zcode")
    project = make_project(tmp_path)  # project writes its own layers after init
    effective = load_effective(project.config_path, project.profiles_path)
    assert effective.config.controller_profile == "host-planner"  # project wins
    project.close()


def test_install_is_idempotent(user_layer):
    install_preset("zcode")
    second = install_preset("zcode")
    assert second["profiles_added"] == []
    assert not second.get("conflicts")


def test_install_copies_missing_agent_definitions(user_layer, agents_dir):
    report = install_preset("zcode")
    installed = {a["name"]: a for a in report["agents"]}
    assert installed["orx-worker"]["installed"] is True
    assert (agents_dir / "orx-worker.md").exists()
    assert (agents_dir / "orx-verifier.md").exists()
    assert (agents_dir / "orx-verifier-strong.md").exists()


def test_install_preserves_existing_agent_definitions(user_layer, agents_dir):
    (agents_dir / "orx-worker.md").write_text("custom live definition\n")
    report = install_preset("zcode")
    installed = {a["name"]: a for a in report["agents"]}
    assert installed["orx-worker"]["installed"] is False
    assert installed["orx-worker"]["preserved_existing"] is True
    assert (agents_dir / "orx-worker.md").read_text() == "custom live definition\n"
    assert installed["orx-verifier"]["installed"] is True


# ---------------------------------------------------------------------------
# init respects an installed user preset


def test_init_inherits_when_user_preset_installed(user_layer, tmp_path, monkeypatch):
    install_preset("zcode")
    monkeypatch.chdir(tmp_path)
    report = dispatch.init_project(tmp_path)
    orx = tmp_path / ".orx"
    # No project config/profiles shadowing the preset…
    assert not (orx / "config.toml").exists() or (orx / "config.toml").read_text().strip() == ""
    assert not (orx / "profiles.toml").exists() or (orx / "profiles.toml").read_text().strip() == ""
    assert ".orx/state.db" in report["created"]
    # …and the effective project IS the preset.
    project = dispatch.open_project()
    try:
        assert project.config.controller_profile == "zcode-controller"
        assert project.config.worker_profiles == ["zcode-worker"]
        seeded = {row["profile"] for row in dispatch.resource_list(project)}
        assert "zcode-worker" in seeded
    finally:
        project.close()


def test_init_partial_user_layer_still_writes_defaults(tmp_path, monkeypatch, user_layer):
    """A user layer with only profiles.toml is NOT a preset: the old behavior
    (project defaults) must not change under it."""
    (user_layer / "profiles.toml").write_text(
        "schema_version = 1\n\n[profiles.extra]\ndriver = \"host\"\nharness = \"zcode\"\n"
        "model = \"x\"\nclass = \"strong\"\neffort = \"high\"\ncapabilities = [\"coding\"]\n"
    )
    monkeypatch.chdir(tmp_path)
    dispatch.init_project(tmp_path)
    assert "orx-host" in (tmp_path / ".orx" / "profiles.toml").read_text()


def test_existing_project_override_survives_preset_install(user_layer, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    project.close()
    install_preset("zcode")
    effective = load_effective(tmp_path / ".orx" / "config.toml",
                               tmp_path / ".orx" / "profiles.toml")
    assert effective.config.controller_profile == "host-planner"
    assert "host-worker" in effective.config.worker_profiles


# ---------------------------------------------------------------------------
# Doctor: role definitions vs profiles consistency


def _agent_definition(model: str, thought: str = "max") -> str:
    return (
        "---\n"
        f"description: probe agent\ncustom: true\nmodel: {model}\n"
        f"thoughtLevel: {thought}\ntools: [Read, Bash]\n"
        "---\n\nprobe\n"
    )


def test_doctor_flags_subagent_model_mismatch(tmp_path, monkeypatch, user_layer, agents_dir):
    from orx.doctor import run_doctor
    monkeypatch.chdir(tmp_path)
    install_preset("zcode")
    (agents_dir / "orx-worker.md").write_text(_agent_definition("account:x/GLM-4.5"))
    dispatch.init_project(tmp_path)
    result = run_doctor(tmp_path)
    checks = {c["name"]: c for c in result["checks"]}
    worker = checks["agent_def:orx-worker"]
    assert worker["state"] == "fail"
    assert "GLM-5.3" in worker["detail"] and "GLM-4.5" in worker["detail"]
    assert not result["ok"]


def test_doctor_warns_on_missing_agent_definition(tmp_path, monkeypatch, user_layer, agents_dir):
    from orx.doctor import run_doctor
    monkeypatch.chdir(tmp_path)
    install_preset("zcode")
    for existing in agents_dir.glob("*.md"):
        existing.unlink()  # simulate definitions never installed
    dispatch.init_project(tmp_path)  # inherit mode: no project files
    result = run_doctor(tmp_path)
    checks = {c["name"]: c for c in result["checks"]}
    # Inherited project: the config check must not fail on the missing file.
    assert checks["config"]["state"] == "ok"
    assert "inherited" in checks["config"]["detail"]
    assert checks["profiles"]["state"] == "ok"
    worker = checks.get("agent_def:orx-worker")
    assert worker is not None and worker["state"] == "warn"
    assert "not found" in worker["detail"]


def test_doctor_passes_consistent_definitions(tmp_path, monkeypatch, user_layer, agents_dir):
    from orx.doctor import run_doctor
    monkeypatch.chdir(tmp_path)
    install_preset("zcode")
    (agents_dir / "orx-worker.md").write_text(
        _agent_definition("account:bigmodel-individual-coding-plan/GLM-5.3")
    )
    (agents_dir / "orx-verifier.md").write_text(
        _agent_definition("account:bigmodel-individual-coding-plan/GLM-5.3-Flash")
    )
    (agents_dir / "orx-verifier-strong.md").write_text(
        _agent_definition("account:bigmodel-individual-coding-plan/GLM-5.3")
    )
    dispatch.init_project(tmp_path)
    result = run_doctor(tmp_path)
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["agent_def:orx-worker"]["state"] == "ok"
    assert checks["agent_def:orx-verifier"]["state"] == "ok"
    assert checks["agent_def:orx-verifier-strong"]["state"] == "ok"
    assert result["ok"]


# ---------------------------------------------------------------------------
# Recovery: never a second writer


@pytest.fixture
def running_host_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    goal = dispatch.create_goal(
        project, objective="recovery probe", acceptance=["a criterion"],
        constraints=[], context="",
    )[0]
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=[]),
    ]))
    dispatch.run_slice(project)
    claimed = dispatch.task_claim(project, "T001")
    yield project, claimed
    project.close()


def test_running_host_task_surfaces_as_recovery_not_redispatch(running_host_task):
    project, claimed = running_host_task
    out = dispatch.run_slice(project)
    # The running task is reported for recovery with its identity…
    assert [e["task"] for e in out["recovery"]] == ["T001"]
    entry = out["recovery"][0]
    assert entry["attempt"] == claimed["attempt"]
    assert entry["execution"]["attempt"] == claimed["attempt"]
    assert "task complete" in entry["contract"] and "task fail" in entry["contract"]
    # …never re-parked, never a second attempt, still running.
    assert all(e["task"] != "T001" for e in out["host_required"])
    goal = project.store.goal_active()
    run = project.store.run_for_goal(goal.id)
    revision = project.store.revision_active(run.id)
    attempts = [a for a in project.store.attempts_all() if a.task_id == "T001"]
    assert len(attempts) == 1
    assert project.store.task_get(revision.id, "T001").status == "running"


def test_late_completion_binds_original_attempt_after_recovery(running_host_task, tmp_path):
    project, claimed = running_host_task
    dispatch.run_slice(project)  # recovery pass in between changes nothing
    result = dispatch.task_complete(
        project, "T001", str(write_evidence(tmp_path)),
        attempt_id=claimed["attempt"],
    )
    assert result["status"] == "passed"


def test_second_writer_only_after_explicit_fail_and_retry(running_host_task, tmp_path):
    project, claimed = running_host_task
    dispatch.task_fail(project, "T001", "subagent died")
    dispatch.task_retry(project, "T001")
    out = dispatch.run_slice(project)
    parked = [e for e in out["host_required"] if e["task"] == "T001"]
    assert len(parked) == 1
    assert parked[0]["execution"]["attempt"] != claimed["attempt"]
    # The new round is claimed; the old attempt's late completion is stale.
    dispatch.task_claim(project, "T001")
    with pytest.raises(ConflictError, match="stale"):
        dispatch.task_complete(
            project, "T001", str(write_evidence(tmp_path, "late.json")),
            attempt_id=claimed["attempt"],
        )
