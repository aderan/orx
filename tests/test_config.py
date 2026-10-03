"""Config and profile validation tests."""

from __future__ import annotations

import pytest

from orx import dispatch
from orx.config import load_config, load_profiles, load_project_config
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
