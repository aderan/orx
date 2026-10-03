"""Config and profile validation tests."""

from __future__ import annotations

import pytest

from orx import dispatch
from orx.config import load_config, load_effective, load_profiles, load_project_config
from orx.records import ConfigError

from conftest import HOST_CONFIG_TOML, HOST_PROFILES_TOML, make_project


def _write(tmp_path, config=HOST_CONFIG_TOML, profiles=HOST_PROFILES_TOML):
    (tmp_path / ".orx").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".orx" / "config.toml").write_text(config)
    (tmp_path / ".orx" / "profiles.toml").write_text(profiles)


def test_valid_config_and_profiles_load(tmp_path):
    _write(tmp_path)
    config, profiles = load_project_config(
        tmp_path / ".orx" / "config.toml", tmp_path / ".orx" / "profiles.toml"
    )
    assert config.controller_profile == "host-planner"
    assert config.depth_profiles["deep"] == ["host-frontier", "host-planner"]
    assert profiles["host-vision"].capabilities == ("coding", "vision")
    assert profiles["cli-fake"].executable == "true"
    assert config.effective_parallelism == 1


@pytest.mark.parametrize("field,value", [
    ("class", 'class = "premium"'),
    ("effort", 'effort = "ludicrous"'),
    ("driver", 'driver = "daemon"'),
])
def test_invalid_profile_enums_rejected(tmp_path, field, value):
    # First occurrence in the file is the host-planner profile.
    _write(tmp_path, profiles=HOST_PROFILES_TOML.replace(
        {
            "class": 'class = "strong"',
            "effort": 'effort = "deep"',
            "driver": 'driver = "host"',
        }[field], value, 1))
    with pytest.raises(ConfigError) as excinfo:
        load_profiles(tmp_path / ".orx" / "profiles.toml")
    assert field in str(excinfo.value)
    assert "invalid" in str(excinfo.value)


def test_missing_profile_reference_rejected(tmp_path):
    _write(tmp_path, config=HOST_CONFIG_TOML.replace('profile = "host-planner"', 'profile = "ghost"'))
    with pytest.raises(ConfigError) as excinfo:
        load_project_config(
            tmp_path / ".orx" / "config.toml", tmp_path / ".orx" / "profiles.toml"
        )
    assert "ghost" in str(excinfo.value)


def test_missing_schema_version_rejected(tmp_path):
    _write(tmp_path, config=HOST_CONFIG_TOML.replace("schema_version = 1\n", "", 1))
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / ".orx" / "config.toml")
    assert "schema_version" in str(excinfo.value)


def test_newer_schema_version_rejected(tmp_path):
    _write(tmp_path, config=HOST_CONFIG_TOML.replace("schema_version = 1", "schema_version = 2", 1))
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / ".orx" / "config.toml")
    assert "schema_version" in str(excinfo.value)


def test_shell_profile_requires_executable(tmp_path):
    broken = HOST_PROFILES_TOML.replace('executable = "true"', "", 1)
    _write(tmp_path, profiles=broken)
    with pytest.raises(ConfigError) as excinfo:
        load_profiles(tmp_path / ".orx" / "profiles.toml")
    assert "executable" in str(excinfo.value)


def test_invalid_prompt_transport_rejected(tmp_path):
    broken = HOST_PROFILES_TOML.replace(
        'prompt_transport = "stdin"', 'prompt_transport = "telepathy"', 1
    )
    _write(tmp_path, profiles=broken)
    with pytest.raises(ConfigError) as excinfo:
        load_profiles(tmp_path / ".orx" / "profiles.toml")
    assert "prompt_transport" in str(excinfo.value)


def test_invalid_plan_depth_rejected(tmp_path):
    _write(tmp_path, config=HOST_CONFIG_TOML.replace('depth = "auto"', 'depth = "extreme"', 1))
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / ".orx" / "config.toml")
    assert "depth" in str(excinfo.value)


def test_high_max_parallel_warns_and_forces_one(tmp_path):
    _write(tmp_path, config=HOST_CONFIG_TOML.replace("max_parallel = 1", "max_parallel = 4", 1))
    config = load_config(tmp_path / ".orx" / "config.toml")
    assert config.max_parallel == 4
    assert config.effective_parallelism == 1
    assert any("max_parallel" in w and "1" in w for w in config.warnings)


def test_invalid_max_parallel_rejected(tmp_path):
    _write(tmp_path, config=HOST_CONFIG_TOML.replace("max_parallel = 1", "max_parallel = 0", 1))
    with pytest.raises(ConfigError):
        load_config(tmp_path / ".orx" / "config.toml")


def test_cli_driver_with_zcode_harness_rejected(tmp_path):
    # driver=cli + harness=zcode can never dispatch: zcode is a host harness.
    broken = HOST_PROFILES_TOML.replace(
        'driver = "host"\nharness = "zcode"\nmodel = "m-planner"',
        'driver = "cli"\nharness = "zcode"\nmodel = "m-planner"', 1)
    _write(tmp_path, profiles=broken)
    with pytest.raises(ConfigError) as excinfo:
        load_profiles(tmp_path / ".orx" / "profiles.toml")
    assert "no adapter" in str(excinfo.value) and "zcode" in str(excinfo.value)


def test_force_profile_field_parses_and_validates(tmp_path):
    extended = HOST_PROFILES_TOML + """
[profiles.cursor-forced]
driver = "cli"
harness = "cursor"
model = "m-x[effort=high]"
class = "strong"
effort = "deep"
capabilities = ["coding"]
force = true
"""
    _write(tmp_path, profiles=extended)
    profiles = load_profiles(tmp_path / ".orx" / "profiles.toml")
    assert profiles["cursor-forced"].force is True
    assert profiles["host-planner"].force is False

    broken = extended.replace("force = true", 'force = "yes"')
    _write(tmp_path, profiles=broken)
    with pytest.raises(ConfigError) as excinfo:
        load_profiles(tmp_path / ".orx" / "profiles.toml")
    assert "force" in str(excinfo.value)


def test_resource_set_never_rewrites_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    proj = make_project(tmp_path)
    try:
        before = (tmp_path / ".orx" / "profiles.toml").read_bytes()
        dispatch.resource_set(proj, "host-worker", "exhausted", "quota gone")
        after = (tmp_path / ".orx" / "profiles.toml").read_bytes()
        assert before == after
        assert proj.store.resource_get("host-worker").value == "exhausted"
    finally:
        proj.close()


# -- M1 layered configuration -----------------------------------------------


def _write_user_layer(tmp_path, config_toml="", profiles_toml=""):
    user_dir = tmp_path / "user" / "orx"
    user_dir.mkdir(parents=True)
    (user_dir / "config.toml").write_text(config_toml)
    (user_dir / "profiles.toml").write_text(profiles_toml)
    return user_dir / "config.toml", user_dir / "profiles.toml"


def _project_files(tmp_path):
    orx = tmp_path / ".orx"
    orx.mkdir()
    (orx / "config.toml").write_text(
        'schema_version = 1\n[controller]\nprofile = "host-planner"\n'
    )
    (orx / "profiles.toml").write_text(
        'schema_version = 1\n[profiles.host-planner]\ndriver = "host"\n'
        'harness = "zcode"\nmodel = "m"\nclass = "strong"\n'
        'effort = "deep"\ncapabilities = ["coding"]\n'
    )
    return orx / "config.toml", orx / "profiles.toml"


def test_layered_project_over_user_scalars(tmp_path):
    uc, up = _write_user_layer(
        tmp_path,
        config_toml='schema_version = 1\n[controller]\nprofile = "user-planner"\n'
                    '[runtime]\ncommand_timeout_sec = 99\n',
        profiles_toml='schema_version = 1\n[profiles.user-planner]\ndriver = "host"\n'
                      'harness = "zcode"\nmodel = "u"\nclass = "strong"\n'
                      'effort = "deep"\ncapabilities = ["coding"]\n',
    )
    pc, pp = _project_files(tmp_path)
    eff = load_effective(pc, pp, user_config=uc, user_profiles=up)
    assert eff.config.controller_profile == "host-planner"  # project wins
    assert eff.origins["controller.profile"] == "project"
    assert eff.config.command_timeout_sec == 99  # only user sets it
    assert eff.origins["runtime.command_timeout_sec"] == "user"
    assert eff.origins["plan.depth"] == "default"  # nobody sets it
    assert eff.profile_origins["host-planner"] == "project"


def test_layered_user_fills_unset_sections(tmp_path):
    uc, up = _write_user_layer(
        tmp_path,
        config_toml='schema_version = 1\n'
                    '[plan.standard]\nprofiles = ["user-planner"]\n'
                    '[worker]\nprofiles = ["user-worker"]\n',
        profiles_toml='schema_version = 1\n'
                      '[profiles.user-planner]\ndriver = "host"\nharness = "zcode"\n'
                      'model = "u1"\nclass = "strong"\neffort = "deep"\ncapabilities = ["coding"]\n'
                      '[profiles.user-worker]\ndriver = "host"\nharness = "zcode"\n'
                      'model = "u2"\nclass = "strong"\neffort = "standard"\ncapabilities = ["coding"]\n',
    )
    pc, pp = _project_files(tmp_path)
    eff = load_effective(pc, pp, user_config=uc, user_profiles=up)
    assert eff.config.depth_profiles["standard"] == ["user-planner"]
    assert eff.config.worker_profiles == ["user-worker"]
    # project layer sets no routing sections -> user's apply
    assert eff.origins["plan.standard.profiles"] == "user"
    assert eff.origins["worker.profiles"] == "user"


def test_layered_project_section_shadows_user_whole(tmp_path):
    uc, up = _write_user_layer(
        tmp_path,
        config_toml='schema_version = 1\n[worker]\nprofiles = ["user-worker"]\n',
        profiles_toml="",
    )
    orx = tmp_path / ".orx"
    orx.mkdir()
    (orx / "config.toml").write_text(
        'schema_version = 1\n[controller]\nprofile = "p"\n'
        '[worker]\nprofiles = ["proj-worker"]\n'
    )
    (orx / "profiles.toml").write_text(
        'schema_version = 1\n[profiles.p]\ndriver = "host"\nharness = "zcode"\n'
        'model = "m"\nclass = "strong"\neffort = "deep"\ncapabilities = ["coding"]\n'
        '[profiles.proj-worker]\ndriver = "host"\nharness = "zcode"\n'
        'model = "m2"\nclass = "strong"\neffort = "standard"\ncapabilities = ["coding"]\n'
    )
    eff = load_effective(orx / "config.toml", orx / "profiles.toml",
                         user_config=uc, user_profiles=up)
    assert eff.config.worker_profiles == ["proj-worker"]  # whole-section replace
    assert eff.origins["worker.profiles"] == "project"


def test_layered_same_name_profile_complete_replacement(tmp_path):
    uc, up = _write_user_layer(
        tmp_path,
        profiles_toml='schema_version = 1\n[profiles.shared]\ndriver = "host"\n'
                      'harness = "zcode"\nmodel = "user-model"\nclass = "strong"\n'
                      'effort = "deep"\ncapabilities = ["coding", "vision"]\n',
    )
    pc, pp = _project_files(tmp_path)
    # project redefines shared with different everything
    (pp).write_text((pp).read_text() +
                    '[profiles.shared]\ndriver = "host"\nharness = "zcode"\n'
                    'model = "proj-model"\nclass = "economy"\n'
                    'effort = "quick"\ncapabilities = ["coding"]\n')
    eff = load_effective(pc, pp, user_config=uc, user_profiles=up)
    assert eff.profile_origins["shared"] == "project"
    assert eff.profiles["shared"].model == "proj-model"
    assert eff.profiles["shared"].capabilities == ("coding",)  # not merged with user's vision


def test_layered_env_override_beats_files(tmp_path, monkeypatch):
    uc, up = _write_user_layer(
        tmp_path, config_toml='schema_version = 1\n[runtime]\nmax_parallel = 2\n')
    pc, pp = _project_files(tmp_path)
    (pc).write_text((pc).read_text() + "[runtime]\nmax_parallel = 1\n")
    monkeypatch.setenv("ORX_RUNTIME_MAX_PARALLEL", "3")
    eff = load_effective(pc, pp, user_config=uc, user_profiles=up)
    assert eff.config.max_parallel == 3
    assert eff.origins["runtime.max_parallel"] == "env"

    monkeypatch.setenv("ORX_RUNTIME_MAX_PARALLEL", "not-an-int")
    from orx.records import ConfigError
    with pytest.raises(ConfigError) as exc:
        load_effective(pc, pp, user_config=uc, user_profiles=up)
    assert "ORX_RUNTIME_MAX_PARALLEL" in str(exc.value.messages)


def test_layered_missing_user_layer_is_not_an_error(tmp_path):
    pc, pp = _project_files(tmp_path)
    eff = load_effective(pc, pp, user_config=tmp_path / "nope" / "config.toml",
                         user_profiles=tmp_path / "nope" / "profiles.toml")
    assert eff.config.controller_profile == "host-planner"
    assert eff.origins["controller.profile"] == "project"


def test_layered_malformed_user_layer_is_attributed_error(tmp_path):
    uc, up = _write_user_layer(tmp_path, config_toml="not [ valid toml {{{")
    pc, pp = _project_files(tmp_path)
    from orx.records import ConfigError
    with pytest.raises(ConfigError) as exc:
        load_effective(pc, pp, user_config=uc, user_profiles=up)
    assert any("[user]" in m for m in exc.value.messages)


def test_user_paths_respect_env_and_xdg(monkeypatch, tmp_path):
    from orx import config as cfg
    monkeypatch.setenv("ORX_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("ORX_DATA_DIR")  # the autouse isolation sets it
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    assert cfg.user_config_dir() == tmp_path / "cfg"
    assert cfg.user_data_dir() == tmp_path / "xdg-data" / "orx"
    monkeypatch.delenv("ORX_CONFIG_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    assert cfg.user_config_dir() == tmp_path / "xdg-config" / "orx"


# -- T002: layered integration through open_project / doctor / profiles --


def _user_layer_files(tmp_path):
    d = tmp_path / "userlayer"
    d.mkdir()
    return d / "config.toml", d / "profiles.toml"


def test_open_project_routes_to_user_layer_profiles(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    uc, up = _user_layer_files(tmp_path)
    uc.write_text(
        'schema_version = 1\n'
        '[plan.standard]\nprofiles = ["user-planner"]\n'
        '[worker]\nprofiles = ["user-worker"]\n'
    )
    up.write_text(
        'schema_version = 1\n'
        '[profiles.user-planner]\ndriver = "host"\nharness = "zcode"\n'
        'model = "u1"\nclass = "strong"\neffort = "deep"\ncapabilities = ["coding"]\n'
        '[profiles.user-worker]\ndriver = "host"\nharness = "zcode"\n'
        'model = "u2"\nclass = "strong"\neffort = "standard"\ncapabilities = ["coding"]\n'
    )
    monkeypatch.setenv("ORX_CONFIG_DIR", str(tmp_path / "userlayer"))
    project = make_project(
        tmp_path,
        config_toml='schema_version = 1\n[controller]\nprofile = "user-planner"\n',
        profiles_toml="schema_version = 1\n",
    )
    try:
        assert project.config.depth_profiles["standard"] == ["user-planner"]
        assert project.config.worker_profiles == ["user-worker"]
        assert project.profile_origins["user-planner"] == "user"
        assert project.origins["worker.profiles"] == "user"
        # user-layer profiles get resource rows like project ones
        assert project.store.resource_get("user-worker").value == "unknown"
    finally:
        project.close()


def test_doctor_reports_user_layer_and_effective_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from orx.doctor import run_doctor
    uc, up = _user_layer_files(tmp_path)
    up.write_text(
        'schema_version = 1\n[profiles.extra]\ndriver = "host"\nharness = "zcode"\n'
        'model = "x"\nclass = "strong"\neffort = "deep"\ncapabilities = ["coding"]\n'
    )
    monkeypatch.setenv("ORX_CONFIG_DIR", str(tmp_path / "userlayer"))
    dispatch.init_project(tmp_path)
    result = run_doctor(tmp_path)
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["user_layer"]["state"] == "ok"
    assert "profiles.toml" in by_name["user_layer"]["detail"]
    assert by_name["effective_config"]["state"] == "ok"
    assert "user" in by_name["effective_config"]["detail"]
    assert result["ok"]


def test_doctor_effective_config_catches_user_layer_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from orx.doctor import run_doctor
    uc, up = _user_layer_files(tmp_path)
    uc.write_text("broken {{{")
    monkeypatch.setenv("ORX_CONFIG_DIR", str(tmp_path / "userlayer"))
    dispatch.init_project(tmp_path)
    result = run_doctor(tmp_path)
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["effective_config"]["state"] == "fail"
    assert not result["ok"]


def test_set_preserves_unknown_values_and_rejects_before_write(tmp_path):
    import tomllib
    from datetime import datetime, timezone

    from orx.config import set_config_value

    path = tmp_path / "config.toml"
    path.write_text(
        "schema_version = 1\n"
        'note = "keep"\n'
        "stamp = 2020-01-02T03:04:05Z\n"
        "ratio = 1.5\n"
        "\n[extra]\nflag = true\n\n[[widgets]]\nname = \"a\"\n"
    )
    before = path.read_bytes()
    with pytest.raises(ConfigError) as excinfo:
        set_config_value(path, "schema_version", "2", create=True)
    assert "schema_version" in str(excinfo.value)
    assert path.read_bytes() == before
    with pytest.raises(ConfigError):
        set_config_value(path, "runtime.max_parallel", "0", create=True)
    assert path.read_bytes() == before

    result = set_config_value(path, "runtime.command_timeout_sec", "12", create=True)
    assert result["value"] == 12
    assert result["created"] is False
    doc = tomllib.loads(path.read_text())
    assert doc["schema_version"] == 1
    assert doc["note"] == "keep"
    assert doc["stamp"] == datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert doc["ratio"] == 1.5
    assert doc["extra"] == {"flag": True}
    assert doc["widgets"] == [{"name": "a"}]
    assert doc["runtime"]["command_timeout_sec"] == 12


def test_set_creates_schema_only_document(tmp_path):
    import tomllib

    from orx.config import set_config_value

    path = tmp_path / "fresh" / "config.toml"
    with pytest.raises(ConfigError):
        set_config_value(path, "plan.depth", "extreme", create=True)
    assert not path.exists()
    assert not path.parent.exists()

    result = set_config_value(path, "plan.depth", "deep", create=True)
    assert result["created"] is True
    assert tomllib.loads(path.read_text()) == {
        "schema_version": 1,
        "plan": {"depth": "deep"},
    }


def test_profiles_listing_annotates_layer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # open_project resolves via cwd
    project = make_project(tmp_path)
    try:
        rows = [
            {**profile.to_dict(),
             "layer": project.profile_origins.get(name, "project")}
            for name, profile in project.profiles.items()
        ]
        assert rows and all(r["layer"] == "project" for r in rows)
    finally:
        project.close()


def test_retry_prompt_carries_prior_failure(tmp_path, monkeypatch):
    """A retried CLI worker must see WHY the previous attempt failed: the
    task row clears failure_reason on retry, so the prompt reads history."""
    from orx import machine
    from orx.dispatch import _prior_failure_context, worker_prompt

    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    try:
        goal = project.store.goal_create("goal text", ["a1"], [], "")[0]
        run = project.store.run_for_goal(goal.id)
        revision = project.store.revision_create(run.id, "standard", "host-planner",
                                                 {"goal": goal.id, "tasks": []})
        project.store.task_insert(revision.id, "T001", "obj", {}, [], [], {}, "runnable")
        from orx.records import TaskStatus
        machine.transition(project.store, revision.id, "T001", "route_cli", TaskStatus.RUNNING)
        machine.transition(project.store, revision.id, "T001", "fail", TaskStatus.FAILED,
                           reason="exit 1: boom", failure_reason="exit 1: boom")
        context = _prior_failure_context(project.store, revision.id, "T001")
        assert "exit 1: boom" in context
        assert "FAILED" in context

        task = project.store.task_get(revision.id, "T001")
        prompt = worker_prompt(goal, task, prior_failure=context)
        assert "exit 1: boom" in prompt
        assert "do not redo the task blindly" in prompt

        # first attempt (no history) -> no prior-failure section
        project.store.task_insert(revision.id, "T002", "obj2", {}, [], [], {}, "runnable")
        assert _prior_failure_context(project.store, revision.id, "T002") == ""
    finally:
        project.close()


def test_migrate_profiles_collision_safe_and_idempotent(tmp_path):
    from orx.config import migrate_profiles_to_user
    project = tmp_path / "profiles.toml"
    user = tmp_path / "user" / "profiles.toml"
    project.write_text(
        'schema_version = 1\n'
        '[profiles.alpha]\ndriver = "host"\nharness = "zcode"\nmodel = "a"\n'
        'class = "strong"\neffort = "deep"\ncapabilities = ["coding"]\n'
        '[profiles.beta]\ndriver = "cli"\nharness = "codex"\nmodel = "b"\n'
        'class = "frontier"\neffort = "deep"\ncapabilities = ["coding"]\n'
    )

    # 1. fresh migration moves everything verbatim
    report = migrate_profiles_to_user(project, user)
    assert report["moved"] == ["alpha", "beta"] and report["preserved"] == []
    migrated = load_profiles(user)
    assert set(migrated) == {"alpha", "beta"}
    assert migrated["beta"].model == "b"
    # caller step: bare the project layer after success
    project.write_text("schema_version = 1\n# migrated to user layer\n")

    # 2. idempotent: bare project layer -> nothing to do, files untouched
    before = user.read_text()
    report2 = migrate_profiles_to_user(project, user)
    assert report2["note"] and not report2["moved"]
    assert user.read_text() == before

    # 3. unrelated user entries preserved; same-name identical re-merge ok
    project.write_text(
        'schema_version = 1\n'
        '[profiles.alpha]\ndriver = "host"\nharness = "zcode"\nmodel = "a"\n'
        'class = "strong"\neffort = "deep"\ncapabilities = ["coding"]\n'
    )
    report3 = migrate_profiles_to_user(project, user)
    assert report3["moved"] == ["alpha"] and report3["preserved"] == ["beta"]
    assert set(load_profiles(user)) == {"alpha", "beta"}

    # 4. collision: same name, different definition -> refuse, no write
    project.write_text(
        'schema_version = 1\n'
        '[profiles.alpha]\ndriver = "cli"\nharness = "cursor"\nmodel = "CHANGED"\n'
        'class = "frontier"\neffort = "deep"\ncapabilities = ["coding"]\n'
    )
    before = user.read_text()
    with pytest.raises(ConfigError) as exc:
        migrate_profiles_to_user(project, user)
    assert "differ between project and user layers" in " ".join(exc.value.messages)
    assert user.read_text() == before  # no partial write


def test_migrate_profiles_preserves_resource_identity(tmp_path, monkeypatch):
    """Profile identity is the NAME: after migration, the same SQLite rows
    keep applying (resources, attempts) — verified through open_project."""
    from orx.config import migrate_profiles_to_user
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    try:
        from orx.records import ResourceStatus
        project.store.resource_set("host-planner", ResourceStatus.UNAVAILABLE, "pre-migration note")
        project.close()
        migrate_profiles_to_user(tmp_path / ".orx" / "profiles.toml",
                                 tmp_path / "user" / "profiles.toml")
        (tmp_path / ".orx" / "profiles.toml").write_text(
            "schema_version = 1\n# migrated to user layer\n")
        import os
        os.environ["ORX_CONFIG_DIR"] = str(tmp_path / "user")
        reopened = dispatch.open_project()
        try:
            assert reopened.store.resource_get("host-planner").value == "unavailable"
            notes = {r.profile: r.note for r in reopened.store.resource_rows()}
            assert "pre-migration" in notes["host-planner"]
            assert reopened.profile_origins["host-planner"] == "user"
        finally:
            reopened.close()
    finally:
        import os
        os.environ.pop("ORX_CONFIG_DIR", None)
        try:
            project.close()
        except Exception:
            pass


def test_env_layer_overrides_user_and_project(tmp_path, monkeypatch):
    uc, up = _write_user_layer(tmp_path, config_toml='schema_version = 1\n[runtime]\nmax_parallel = 4\n')
    pc, pp = _project_files(tmp_path)
    (pc).write_text((pc).read_text() + "[runtime]\nmax_parallel = 2\n")
    monkeypatch.setenv("ORX_RUNTIME_MAX_PARALLEL", "5")
    eff = load_effective(pc, pp, user_config=uc, user_profiles=up)
    assert eff.config.max_parallel == 5
    assert eff.origins["runtime.max_parallel"] == "env"


def test_routing_section_list_types_validated(tmp_path):
    pc, pp = _project_files(tmp_path)
    (pc).write_text((pc).read_text() + '[worker]\nprofiles = "not-a-list"\n')
    with pytest.raises(ConfigError):
        load_effective(pc, pp, user_config=tmp_path / "n" / "c.toml",
                       user_profiles=tmp_path / "n" / "p.toml")


def test_user_layer_profiles_validate_like_project(tmp_path):
    uc, up = _write_user_layer(
        tmp_path, profiles_toml='schema_version = 1\n[profiles.bad]\ndriver = "warp"\n')
    pc, pp = _project_files(tmp_path)
    with pytest.raises(ConfigError) as exc:
        load_effective(pc, pp, user_config=uc, user_profiles=up)
    assert any("user" in m for m in exc.value.messages)


def test_doctor_profile_references_resolve_across_layers(tmp_path, monkeypatch):
    """A project routing only to user-layer profiles must pass the
    profile_references check (validated against the effective merge)."""
    from orx.doctor import run_doctor
    monkeypatch.chdir(tmp_path)
    uc, up = _user_layer_files(tmp_path)
    up.write_text(
        'schema_version = 1\n[profiles.up-planner]\ndriver = "host"\nharness = "zcode"\n'
        'model = "u"\nclass = "strong"\neffort = "deep"\ncapabilities = ["coding"]\n')
    monkeypatch.setenv("ORX_CONFIG_DIR", str(tmp_path / "userlayer"))
    dispatch.init_project(tmp_path)
    (tmp_path / ".orx" / "config.toml").write_text(
        'schema_version = 1\n[controller]\nprofile = "up-planner"\n')
    (tmp_path / ".orx" / "profiles.toml").write_text("schema_version = 1\n")
    result = run_doctor(tmp_path)
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["profile_references"]["state"] == "ok"
    assert result["ok"]
