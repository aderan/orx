"""The new tier contract is explicit and coexists with deployed class profiles."""

import pytest

from orx import dispatch, routing
from orx.config import load_profiles, migrate_profiles_to_user
from orx.records import ConfigError, PlanDepth, Role

from conftest import make_project


def write_profile(tmp_path, fields):
    path = tmp_path / "profiles.toml"
    path.write_text(f'''schema_version = 1
[profiles.example]
driver = "cli"
harness = "codex"
model = "gpt-6.1-sol"
effort = "high"
{fields}
''')
    return path


@pytest.mark.parametrize("tier", ["T0", "T1", "T2", "T3", "S"])
def test_tier_profiles_load_without_legacy_class(tmp_path, tier):
    path = write_profile(tmp_path, f'tier = "{tier}"\ncapabilities = ["ocr"]')
    profile = load_profiles(path)["example"]
    assert profile.tier.value == tier
    assert profile.model_class is None
    assert profile.to_dict()["tier"] == tier


def test_legacy_class_and_xhigh_keep_their_meaning(tmp_path):
    path = write_profile(tmp_path, 'class = "strong"')
    path.write_text(path.read_text().replace('effort = "high"', 'effort = "xhigh"'))
    profile = load_profiles(path)["example"]
    assert profile.tier is None
    assert profile.model_class.value == "strong"
    assert profile.effort.value == "xhigh"


@pytest.mark.parametrize("fields", [
    'tier = "T4"',
    'tier = "T2"\nclass = "strong"',
    'tier = "S"\ncapabilities = ["coding"]',
    'tier = "S"\ncapabilities = []',
    'tier = "T2"\nnative_effort = "turbo"',
])
def test_invalid_new_profiles_fail_with_context(tmp_path, fields):
    with pytest.raises(ConfigError):
        load_profiles(write_profile(tmp_path, fields))


def test_tier_profiles_use_four_abstract_efforts(tmp_path):
    path = write_profile(tmp_path, 'tier = "T2"')
    path.write_text(path.read_text().replace('effort = "high"', 'effort = "xhigh"'))
    with pytest.raises(ConfigError, match="effort"):
        load_profiles(path)


def test_tier_and_native_effort_survive_user_migration(tmp_path):
    path = write_profile(tmp_path, 'tier = "T1"\nnative_effort = "ultra"')
    user = tmp_path / "user" / "profiles.toml"
    migrate_profiles_to_user(path, user)
    profile = load_profiles(user)["example"]
    assert profile.tier.value == "T1"
    assert profile.native_effort == "ultra"


def test_deep_uses_project_order_and_specialists_require_named_capability(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    path = project.root / ".orx" / "profiles.toml"
    with path.open("a") as f:
        f.write('''
[profiles.ocr]
driver = "external"
harness = "shell"
model = "ocr-tool"
executable = "ocr"
tier = "S"
effort = "low"
capabilities = ["ocr"]
''')
    project.close()
    project = dispatch.open_project()
    try:
        request = routing.RouteRequest(role=Role.PLANNER, depth=PlanDepth.DEEP,
                                       pinned_profile="host-planner")
        result = routing.route(project.store, project.config, project.profiles, request)
        assert result.selected == "host-planner"
        for capabilities, usable in [((), False), (("coding",), False), (("ocr",), True)]:
            result = routing.route(
                project.store, project.config, project.profiles,
                routing.RouteRequest(role=Role.WORKER, pinned_profile="ocr",
                                     required_capabilities=capabilities),
            )
            assert result.ok is usable
    finally:
        project.close()


@pytest.mark.parametrize("harness,effort,native", [
    ("zcode", "medium", "high"),
    ("claude", "max", "max"),
])
def test_abstract_effort_maps_without_changing_requested_value(tmp_path, harness, effort, native):
    from orx.adapters.base import effective_model_spec, native_model_spec

    path = write_profile(tmp_path, 'tier = "T1"')
    path.write_text(path.read_text().replace('harness = "codex"', f'harness = "{harness}"')
                    .replace('effort = "high"', f'effort = "{effort}"'))
    profile = load_profiles(path)["example"]
    assert native_model_spec(profile).effort == native
    assert effective_model_spec(profile).effort == effort


def test_codex_native_override_uses_catalog_validated_ultra(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from orx import model_catalog
    from orx.adapters.codex import CodexAdapter

    path = write_profile(tmp_path, 'tier = "T1"\nnative_effort = "ultra"')
    profile = load_profiles(path)["example"]
    catalog = SimpleNamespace(models=[
        SimpleNamespace(slug="gpt-6.1-sol", family="gpt-6.1-sol",
                        efforts=("high", "ultra"), effort=None),
    ], generation="test")
    monkeypatch.setattr(model_catalog, "launch_catalog", lambda _: catalog)
    launch = CodexAdapter().build_worker_launch(
        root=tmp_path, scratch=tmp_path / "scratch", profile=profile, prompt="check", timeout=5,
    )
    assert launch.argv[launch.argv.index("-c") + 1] == "model_reasoning_effort=ultra"
    assert launch.planned_effort.requested == "high"
    assert launch.model_resolution["native_effort"] == "ultra"
    assert launch.model_resolution["spec"]["effort"] == "high"


@pytest.mark.parametrize("family,version,catalog_family", [
    ("grok", "4.7", "grok-4.7"),
    ("grok", "4.6", "cursor-grok-4.6"),
    ("grok", None, "cursor-grok-4.6"),
])
def test_cursor_max_mapping_uses_resolved_family(tmp_path, family, version, catalog_family):
    from orx.adapters.cursor import resolve_cursor_model
    from orx.model_catalog import CatalogEntry, ModelCatalog
    from orx.models import ModelSpec

    catalog = ModelCatalog(harness="cursor", source="test", generated_at="", generation=1, models=(
        CatalogEntry(slug=f"{catalog_family}-xhigh", family=catalog_family, effort="xhigh"),
    ))
    resolved = resolve_cursor_model(
        ModelSpec(family=family, version=version, effort="max"),
        catalog=catalog, help_text="", map_max=True,
    )
    assert resolved.cli_slug == f"{catalog_family}-xhigh"
    assert resolved.spec.effort == "xhigh"


def test_codex_max_mapping_uses_catalog_and_legacy_still_rejects():
    from orx.adapters.codex import resolve_codex_model
    from orx.adapters.base import ModelResolutionError
    from orx.model_catalog import CatalogEntry, ModelCatalog
    from orx.models import ModelSpec

    catalog = ModelCatalog(harness="codex", source="test", generated_at="", generation=1, models=(
        CatalogEntry(slug="test", efforts=("high", "xhigh")),
    ))
    spec = ModelSpec(model_id="test", effort="max")
    mapped = resolve_codex_model(spec, catalog=catalog, strict=True, map_max=True)
    assert mapped.spec.effort == "xhigh"
    with pytest.raises(ModelResolutionError):
        resolve_codex_model(spec, catalog=catalog, strict=True)


def test_cursor_launch_preserves_abstract_max_and_records_native_xhigh(tmp_path, monkeypatch):
    from orx import model_catalog, runtime
    from orx.adapters.cursor import CursorAdapter
    from orx.model_catalog import CatalogEntry, ModelCatalog
    from orx.runtime import CommandResult

    path = write_profile(tmp_path, 'tier = "T2"')
    path.write_text(path.read_text().replace('harness = "codex"', 'harness = "cursor"')
                    .replace('model = "gpt-6.1-sol"', 'model = {family = "grok", version = "4.7"}')
                    .replace('effort = "high"', 'effort = "max"'))
    profile = load_profiles(path)["example"]
    catalog = ModelCatalog(harness="cursor", source="test", generated_at="", generation=1, models=(
        CatalogEntry(slug="grok-4.7-xhigh", family="grok-4.7", effort="xhigh"),
    ))
    monkeypatch.setattr(model_catalog, "launch_catalog", lambda _: catalog)
    monkeypatch.setattr(runtime, "run_argv", lambda *a, **kw: CommandResult(
        command="agent --help", exit_code=0, stdout="", stderr="", duration_sec=0, timed_out=False,
    ))
    launch = CursorAdapter().build_worker_launch(
        root=tmp_path, scratch=tmp_path / "scratch", profile=profile, prompt="check", timeout=5,
    )
    assert launch.argv[launch.argv.index("--model") + 1] == "grok-4.7-xhigh"
    assert launch.planned_effort.requested == "max"
    assert launch.planned_effort.actual == "xhigh"
    assert launch.model_resolution["native_effort"] == "xhigh"
    assert launch.model_resolution["spec"]["effort"] == "max"


def test_cli_profiles_displays_new_tiers_and_legacy_classes(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from orx.cli import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ORX_PROJECT", raising=False)
    project = make_project(tmp_path)
    path = project.root / ".orx" / "profiles.toml"
    path.write_text(path.read_text() + '''
[profiles.tier-profile]
driver = "cli"
harness = "claude"
model = "sonnet"
tier = "T1"
effort = "high"
capabilities = ["coding"]
''')
    project.close()
    result = CliRunner().invoke(app, ["profiles"])
    assert result.exit_code == 0, result.output
    assert "tier=T1" in result.output
    assert "class=strong" in result.output
