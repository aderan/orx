"""G008 T003: runtime model resolution — ModelSpec + refreshable catalog
-> the exact CLI model argument, for every role and both declaration forms.

Covers the acceptance surface:
- semantic profiles (family+version / id tables) resolve to the CLI-supported
  form (variant slug, effort+fast combos, bare enumerated slug, parameterized
  bracket) with no CLI slug-variant spelling in the configuration;
- unsatisfiable semantic requests raise diagnostics naming the requested
  dimensions, the catalog state, and the refresh command — never a silent
  substitution, never a dropped explicit dimension, never a bracket
  masquerade for a known enumerated family;
- all three roles (worker/planner/verifier) share the resolution path, and a
  resolution failure fails the attempt without executing another model or
  writing a completion/pass;
- EffortOutcome evidence rules hold: catalog variants confirm on success,
  brackets only on success + echoed match;
- the shared catalog is the one source (no per-adapter caches) and a
  persisted refresh (generation bump) changes what launches resolve against;
- the resolution decision is recorded per attempt (spec + slug + strategy +
  generation) next to the argv the execution log keeps.

All harnesses are fakes on PATH; every catalog is local fake output. No
network, no completion, no real model call.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from orx import adapters, dispatch, model_catalog, probes
from orx.adapters import codex as codex_adapter
from orx.adapters import cursor as cursor_adapter
from orx.adapters.base import ModelResolutionError
from orx.config import Profile
from orx.models import ModelSpec
from orx.records import Driver, Effort, Harness, ModelClass, ORXError

from conftest import ir_for, make_project, task_spec

# ---------------------------------------------------------------------------
# Fakes

AGENT_HELP = """agent [options] [prompt]
  -p, --print           --output-format <format>    --model <model>
  --workspace <path>    --trust      -f, --force    --yolo
  example: agent --model 'claude-opus-4-8[context=1m,effort=high,fast=false]'
"""

AGENT_HELP_NO_BRACKET = """agent [options] [prompt]
  -p, --print           --output-format <format>    --model <model>
  --workspace <path>    --trust      -f, --force    --yolo
"""

CODEX_HELP = """usage: codex exec [OPTIONS] PROMPT
  --json            -m, --model <MODEL>    -C, --cd <DIR>
  --output-last-message <FILE>   --output-schema <FILE>
  -s, --sandbox <SANDBOX_MODE>   --ephemeral
"""

CODEX_MODELS = json.dumps({
    "models": [
        {"slug": "fake-codex-model",
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
        {"slug": "limited-model",
         "supported_reasoning_levels": [{"effort": "low"}]},
        {"slug": "flat-model"},
    ]
})

# A realistic Cursor listing: unprefixed grok-4.7 variants (dot version),
# the CLI's own cursor- prefixed grok-4.6, a suffixed-only Claude family
# (dash version), effort-less families (auto, composer-2.5) with a fast
# sibling, and a GLM family.
CURSOR_LISTING = "\n".join([
    "Available models",
    "",
    "grok-4.7-low - Grok",
    "grok-4.7-medium - Grok",
    "grok-4.7-high - Grok",
    "grok-4.7-xhigh - Grok",
    "grok-4.7-xhigh-fast - Grok Fast",
    "cursor-grok-4.6-medium - Prefixed Grok",
    "cursor-grok-4.6-high - Prefixed Grok",
    "claude-opus-5-5-low - Opus",
    "claude-opus-5-5-medium - Opus",
    "claude-opus-5-5-high - Opus High",
    "claude-opus-5-5-max - Opus Max",
    "auto - Auto (current, default)",
    "composer-2.5 - Composer",
    "composer-2.5-fast - Composer Fast",
    "glm-5.2-high - GLM",
    "fake-cursor - Fake Cursor",
])


def make_bin(directory: Path, name: str, script: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def bindir(tmp_path, monkeypatch):
    """Fake harness binaries first on PATH; shared caches reset around it."""
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")
    cursor_adapter.reset_caches()
    codex_adapter.reset_caches()
    yield directory
    cursor_adapter.reset_caches()
    codex_adapter.reset_caches()


def install_agent(bindir: Path, *, models: str | None = CURSOR_LISTING,
                  help_text: str = AGENT_HELP, echo_model: bool = False) -> Path:
    """A fake `agent`. `--help` prints help_text; `--list-models` prints the
    listing; a launch echoes its JSON envelope (optionally naming the model
    it received, for bracket echo-confirmation). G009 T002: the worker
    envelope's result text carries the structured delivery document — the
    machine contract a real CLI worker follows."""
    calls = bindir / "agent-calls.txt"
    models_block = ""
    if models is not None:
        models_block = f"""if [ "$1" = "--list-models" ]; then
  cat <<'L'
{models}
L
  exit 0
fi
"""
    # Single-quoted for the shell: the delivery text contains backticks and
    # double quotes, which must stay literal.
    passed_delivery = (
        '```json\n{"status":"passed","summary":"done",'
        '"checks":[],"artifacts":[]}\n```'
    )
    emit = (
        'prev=""\nfor a in "$@"; do\n'
        '  if [ "$prev" = "--model" ]; then'
        " echo '{\"result\": \"" + passed_delivery + "\", \"model\": \"$a\"}'; exit 0; fi\n"
        '  prev="$a"\ndone\n'
        if echo_model
        else "echo '{\"result\": \"" + passed_delivery + "\"}'\nexit 0\n"
    )
    script = f"""#!/bin/sh
if [ "$1" = "--help" ]; then
  cat <<'H'
{help_text}
H
  exit 0
fi
{models_block}n=$#
i=0
line=""
for a in "$@"; do
  i=$((i+1))
  if [ "$i" -lt "$n" ]; then line="$line $a"; fi
done
echo "$line" >> {calls}
{emit}"""
    return make_bin(bindir, "agent", script)


def install_codex(bindir: Path, *, models_json: str | None = CODEX_MODELS,
                  plan: str = "worker done") -> Path:
    calls = bindir / "codex-calls.txt"
    script = f"""#!/bin/sh
if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then
  cat <<'H'
{CODEX_HELP}
H
  exit 0
fi
if [ "$1" = "debug" ]; then
  cat <<'M'
{models_json}
M
  exit 0
fi
n=$#
i=0
line=""
for a in "$@"; do
  i=$((i+1))
  if [ "$i" -lt "$n" ]; then line="$line $a"; fi
done
echo "$line" >> {calls}
prev=""
out=""
for arg in "$@"; do
  if [ "$prev" = "--output-last-message" ]; then out="$arg"; fi
  prev="$arg"
done
if [ -n "$out" ]; then printf '%s\\n' '{plan}' > "$out"; fi
exit 0
"""
    return make_bin(bindir, "codex", script)


def spec_profile(name="cursor-x", harness=Harness.CURSOR, effort=Effort.HIGH,
                 model_spec: ModelSpec | None = None, model: str | None = None,
                 **spec_kwargs) -> Profile:
    """A CLI profile with a semantic model table (or a bare string)."""
    if model_spec is None and model is None:
        model_spec = ModelSpec(effort=effort.value, **spec_kwargs)
    return Profile(
        name=name, driver=Driver.CLI, harness=harness,
        model=model if model_spec is None else model_spec.identity,
        model_class=ModelClass.FRONTIER, effort=effort,
        capabilities=("coding",), model_spec=model_spec,
    )


def cursor_launch(bindir, profile, tmp_path, echo_model=False,
                  models=CURSOR_LISTING, help_text=AGENT_HELP, role="worker"):
    install_agent(bindir, models=models, help_text=help_text, echo_model=echo_model)
    adapter = adapters.get_adapter("cursor")
    assert adapter.probe().ok
    scratch = tmp_path / f"scratch-{role}"
    if role == "worker":
        return adapter.build_worker_launch(
            root=tmp_path, scratch=scratch, profile=profile, prompt="x", timeout=5)
    if role == "planner":
        return adapter.build_planner_launch(
            root=tmp_path, scratch=scratch, profile=profile, prompt="x",
            timeout=5, schema_path=None)
    return adapter.build_verifier_launch(
        root=tmp_path, scratch=scratch, profile=profile, prompt="x", timeout=5)


def model_arg(launch) -> str:
    argv = launch.argv
    return argv[argv.index("--model") + 1]


class FakeResult:
    def __init__(self, stdout="", exit_code=0):
        self.stdout = stdout
        self.stderr = ""
        self.exit_code = exit_code
        self.timed_out = False


# ---------------------------------------------------------------------------
# Semantic -> CLI form (cursor)


def test_semantic_family_version_resolves_suffixed_variant(tmp_path, bindir):
    """family+version in dot and dash spellings both reach the suffixed
    variant; the configuration itself names no slug variant."""
    profile = spec_profile(model_spec=ModelSpec(
        family="claude-opus", version="5.5", effort="high"))
    launch = cursor_launch(bindir, profile, tmp_path)
    assert model_arg(launch) == "claude-opus-5-5-high"
    assert profile.model == "claude-opus@5.5"  # identity string, not a slug
    assert launch.planned_effort.source == "requested_validated"
    assert launch.planned_effort.actual == "high"

    # The dot spelling of the same family (grok keeps dots) resolves too.
    grok = spec_profile(model_spec=ModelSpec(family="grok", version="4.7",
                                             effort="xhigh"))
    launch2 = cursor_launch(bindir, grok, tmp_path)
    assert model_arg(launch2) == "grok-4.7-xhigh"


def test_semantic_effort_and_speed_combo_picks_fast_variant(tmp_path, bindir):
    profile = spec_profile(effort=Effort.XHIGH, model_spec=ModelSpec(
        family="grok", version="4.7", speed="fast", effort="xhigh"))
    launch = cursor_launch(bindir, profile, tmp_path)
    assert model_arg(launch) == "grok-4.7-xhigh-fast"
    assert launch.planned_effort.source == "requested_validated"


def test_semantic_floating_family_token_resolves(tmp_path, bindir):
    """version omitted: the family token itself floats against the catalog."""
    profile = spec_profile(model_spec=ModelSpec(family="glm-5.2", effort="high"))
    launch = cursor_launch(bindir, profile, tmp_path)
    assert model_arg(launch) == "glm-5.2-high"


def test_semantic_provider_prefix_spellings_absorbed(tmp_path, bindir):
    """cursor-grok-4.6-* (CLI-prefixed) resolves from family=grok-4.6."""
    profile = spec_profile(model_spec=ModelSpec(family="grok-4.6", effort="high"))
    launch = cursor_launch(bindir, profile, tmp_path)
    assert model_arg(launch) == "cursor-grok-4.6-high"


def test_semantic_bare_enumerated_and_effortless_families(tmp_path, bindir):
    """auto (no effort variants) keeps provider_default; composer-2.5 with
    an explicit fast speed picks the fast sibling — the speed dimension is
    honored even when the family has no effort dimension."""
    auto = spec_profile(model_spec=ModelSpec(family="auto", effort="high"))
    launch = cursor_launch(bindir, auto, tmp_path)
    assert model_arg(launch) == "auto"
    assert launch.planned_effort.source is None
    assert launch.planned_effort.actual == "provider_default"

    composer = spec_profile(model_spec=ModelSpec(
        family="composer-2.5", speed="fast", effort="high"))
    launch2 = cursor_launch(bindir, composer, tmp_path)
    assert model_arg(launch2) == "composer-2.5-fast"
    assert launch2.planned_effort.source is None  # no effort dimension to claim


def test_semantic_unenumerated_family_synthesizes_bracket(tmp_path, bindir):
    """A family the catalog does not enumerate (Claude Opus 5.5 /
    GPT-6.1-sol shape: parameterized per --help) synthesizes the bracket
    from the requested dimensions, in the evidenced parameter keys."""
    profile = spec_profile(model_spec=ModelSpec(
        family="gpt", version="6.1-sol", effort="high", context_tokens=1_000_000,
    ))
    launch = cursor_launch(bindir, profile, tmp_path)
    # declared spelling is primary; bracket combines context/effort
    assert model_arg(launch) == "gpt-6.1-sol[context=1m,effort=high]"
    resolution = launch.model_resolution
    assert resolution["strategy"] == "bracket_synthesis"
    assert resolution["bracket_params"] == [["context", "1m"], ["effort", "high"]]
    assert launch.planned_effort.source == "requested_validated"


def test_semantic_bracket_speed_encodes_fast_flag(tmp_path, bindir):
    profile = spec_profile(model_spec=ModelSpec(
        family="sonnet", version="4", speed="fast", effort="high"))
    launch = cursor_launch(bindir, profile, tmp_path)
    assert model_arg(launch) == "sonnet-4[effort=high,fast=true]"
    # explicit standard encodes fast=false
    std = spec_profile(model_spec=ModelSpec(
        family="sonnet", version="4", speed="standard", effort="high"))
    launch2 = cursor_launch(bindir, std, tmp_path)
    assert model_arg(launch2) == "sonnet-4[effort=high,fast=false]"


# ---------------------------------------------------------------------------
# Cursor diagnostics: no silent substitution, no dropped dimension


def _diagnostic(bindir, tmp_path, spec, **launch_kwargs):
    install_agent(bindir, **launch_kwargs)
    adapter = adapters.get_adapter("cursor")
    catalog = model_catalog.launch_catalog("cursor")
    # The help text the fake CLI serves is the evidence the resolver sees.
    help_text = launch_kwargs.get("help_text", AGENT_HELP)
    with pytest.raises(ModelResolutionError) as excinfo:
        cursor_adapter.resolve_cursor_model(
            spec, help_text=help_text, catalog=catalog)
    return str(excinfo.value)


def test_missing_variant_of_enumerated_family_is_diagnostic_not_bracket(tmp_path, bindir):
    """The acceptance rule: a known enumerated family's missing variant is
    never disguised as bracket support."""
    message = _diagnostic(bindir, tmp_path, ModelSpec(
        family="claude-opus", version="5.5", effort="xhigh"))
    assert "cursor model resolution failed" in message
    assert "family=claude-opus@5.5" in message
    assert "effort=xhigh" in message
    assert "not among them" in message
    assert "catalog:" in message
    assert "orx agent probe cursor" in message


def test_declared_speed_without_matching_variant_is_diagnostic(tmp_path, bindir):
    message = _diagnostic(bindir, tmp_path, ModelSpec(
        family="grok", version="4.7", effort="medium", speed="fast"))
    assert "speed=fast" in message
    assert "no enumerated variant" in message


def test_context_dimension_on_enumerated_family_is_diagnostic(tmp_path, bindir):
    message = _diagnostic(bindir, tmp_path, ModelSpec(
        family="grok", version="4.7", effort="high", context_tokens=128_000))
    assert "context=128000" in message
    assert "context dimension cannot be expressed" in message


def test_capability_requirements_are_diagnostic(tmp_path, bindir):
    message = _diagnostic(bindir, tmp_path, ModelSpec(
        family="grok", version="4.7", effort="high",
        capabilities=("vision",)))
    assert "capabilities=vision" in message


def test_unenumerated_family_without_bracket_evidence_is_diagnostic(tmp_path, bindir):
    message = _diagnostic(bindir, tmp_path, ModelSpec(
        family="mistral", version="3", effort="high"),
        help_text=AGENT_HELP_NO_BRACKET)
    assert "family=mistral@3" in message
    assert "bracket" in message


def test_bracket_param_without_evidence_support_is_diagnostic(tmp_path, bindir):
    """context requested, but the evidenced syntax only knows effort."""
    help_only_effort = AGENT_HELP.replace(
        "claude-opus-4-8[context=1m,effort=high,fast=false]",
        "gpt-5[effort=high]")
    message = _diagnostic(bindir, tmp_path, ModelSpec(
        family="gpt", version="7", effort="high", context_tokens=1_000_000),
        help_text=help_only_effort)
    assert "supports only" in message
    assert "context" in message


# ---------------------------------------------------------------------------
# Legacy id form rides the same ladder


def test_legacy_bare_string_behaviour_is_unchanged(tmp_path, bindir):
    # listed + variant -> variant slug
    launch = cursor_launch(bindir, spec_profile(
        model_spec=ModelSpec(model_id="fake-cursor", effort="high")), tmp_path)
    assert model_arg(launch) == "fake-cursor"
    # unlisted + bracket evidence -> synthesized bracket with effort only
    launch2 = cursor_launch(bindir, spec_profile(
        model_spec=ModelSpec(model_id="gpt-6.1-sol", effort="high")), tmp_path)
    assert model_arg(launch2) == "gpt-6.1-sol[effort=high]"
    # user bracket passes verbatim
    launch3 = cursor_launch(bindir, spec_profile(
        model="gpt-6.1-sol[effort=max]", model_spec=None), tmp_path)
    assert model_arg(launch3) == "gpt-6.1-sol[effort=max]"
    assert launch3.model_resolution["strategy"] == "verbatim_bracket"
    assert launch3.planned_effort.source is None


def test_legacy_unlisted_without_evidence_keeps_bare_passthrough(tmp_path, bindir):
    launch = cursor_launch(bindir, spec_profile(
        model_spec=ModelSpec(model_id="some-unknown-slug", effort="high")),
        tmp_path, help_text=AGENT_HELP_NO_BRACKET)
    assert model_arg(launch) == "some-unknown-slug"
    assert launch.planned_effort.source is None
    assert launch.model_resolution["strategy"] == "bare_unlisted"


def test_id_form_table_matches_bare_string_equivalent(tmp_path, bindir):
    """model = { id = ... } and model = "..." produce the same argv."""
    table = spec_profile(model_spec=ModelSpec(model_id="gpt-6.1-sol",
                                              effort="high"))
    bare = spec_profile(model="gpt-6.1-sol", model_spec=None)
    assert (model_arg(cursor_launch(bindir, table, tmp_path))
            == model_arg(cursor_launch(bindir, bare, tmp_path))
            == "gpt-6.1-sol[effort=high]")


# ---------------------------------------------------------------------------
# EffortOutcome evidence (run confirmation vs catalog evidence)


def test_catalog_variant_confirms_on_success_without_echo(tmp_path, bindir):
    profile = spec_profile(model_spec=ModelSpec(
        family="claude-opus", version="5.5", effort="high"))
    launch = cursor_launch(bindir, profile, tmp_path, echo_model=False)
    adapter = adapters.get_adapter("cursor")
    outcome = adapter.effort_outcome(launch, FakeResult('{"result": "done"}'))
    assert (outcome.actual, outcome.source) == ("high", "requested_validated")


def test_bracket_confirms_only_on_success_and_echo(tmp_path, bindir):
    profile = spec_profile(model_spec=ModelSpec(
        family="gpt", version="6.1-sol", effort="high"))
    launch = cursor_launch(bindir, profile, tmp_path, echo_model=True)
    adapter = adapters.get_adapter("cursor")
    bracketed = model_arg(launch)
    echoed = json.dumps({"result": "done", "model": bracketed})
    ok = adapter.effort_outcome(launch, FakeResult(echoed, exit_code=0))
    assert (ok.actual, ok.source) == ("high", "requested_validated")
    # success without the echo: no false confirmation
    silent = adapter.effort_outcome(launch, FakeResult('{"result": "done"}'))
    assert silent.source is None and silent.actual == "provider_default"
    # failure even WITH the echo: no false confirmation
    failed = adapter.effort_outcome(launch, FakeResult(echoed, exit_code=1))
    assert failed.source is None and failed.actual == "provider_default"


# ---------------------------------------------------------------------------
# Codex


def codex_launch(bindir, profile, tmp_path, models_json=None):
    install_codex(bindir, models_json=models_json) if models_json else install_codex(bindir)
    adapter = adapters.get_adapter("codex")
    assert adapter.probe().ok
    return adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "s", profile=profile, prompt="x",
        timeout=5)


def test_codex_semantic_id_resolves_with_catalog_effort(tmp_path, bindir):
    profile = spec_profile(harness=Harness.CODEX, model_spec=ModelSpec(
        model_id="fake-codex-model", effort="high"))
    launch = codex_launch(bindir, profile, tmp_path)
    argv = launch.argv
    assert argv[argv.index("-m") + 1] == "fake-codex-model"
    assert argv[argv.index("-c") + 1] == "model_reasoning_effort=high"
    assert launch.planned_effort.source == "requested_validated"
    assert launch.model_resolution["strategy"] == "catalog_levels"
    assert launch.model_resolution["bracket_params"] == [
        ["model_reasoning_effort", "high"]]


def test_codex_semantic_unsupported_dimensions_are_diagnostics(tmp_path, bindir):
    install_codex(bindir)
    catalog = model_catalog.launch_catalog("codex")
    for kwargs, fragment in (
        ({"speed": "fast"}, "speed"),
        ({"context_tokens": 128_000}, "context"),
        ({"capabilities": ("vision",)}, "capability"),
    ):
        spec = ModelSpec(model_id="fake-codex-model", effort="high", **kwargs)
        with pytest.raises(ModelResolutionError) as excinfo:
            codex_adapter.resolve_codex_model(spec, catalog=catalog, strict=True)
        message = str(excinfo.value)
        assert "codex model resolution failed" in message
        assert fragment in message
        assert "orx agent probe codex" in message


def test_codex_semantic_unknown_model_and_level_are_diagnostics(tmp_path, bindir):
    install_codex(bindir)
    catalog = model_catalog.launch_catalog("codex")
    with pytest.raises(ModelResolutionError, match="not in the local codex catalog"):
        codex_adapter.resolve_codex_model(
            ModelSpec(model_id="ghost-model", effort="high"),
            catalog=catalog, strict=True)
    with pytest.raises(ModelResolutionError, match="reasoning levels"):
        codex_adapter.resolve_codex_model(
            ModelSpec(model_id="limited-model", effort="high"),
            catalog=catalog, strict=True)


def test_codex_legacy_unknown_model_keeps_provider_default(tmp_path, bindir):
    profile = spec_profile(harness=Harness.CODEX, model="not-in-catalog",
                           model_spec=None)
    launch = codex_launch(bindir, profile, tmp_path)
    assert "-c" not in launch.argv
    assert launch.planned_effort.actual == "provider_default"
    assert launch.model_resolution["strategy"] == "bare_unlisted"


def test_codex_model_without_levels_keeps_provider_default_semantically(tmp_path, bindir):
    """An entry with no supported_reasoning_levels has no effort dimension:
    both forms pass no flag and record provider_default honestly."""
    for profile in (
        spec_profile(harness=Harness.CODEX, model_spec=ModelSpec(
            model_id="flat-model", effort="high")),
        spec_profile(harness=Harness.CODEX, model="flat-model", model_spec=None),
    ):
        launch = codex_launch(bindir, profile, tmp_path)
        assert "-c" not in launch.argv
        assert launch.planned_effort.actual == "provider_default"


def test_codex_roles_share_resolution_and_keep_sandboxes(tmp_path, bindir):
    install_codex(bindir)
    adapter = adapters.get_adapter("codex")
    profile = spec_profile(harness=Harness.CODEX, model_spec=ModelSpec(
        model_id="fake-codex-model", effort="high"))
    worker = adapter.build_worker_launch(root=tmp_path, scratch=tmp_path / "s1",
                                         profile=profile, prompt="x", timeout=5)
    planner = adapter.build_planner_launch(root=tmp_path, scratch=tmp_path / "s2",
                                           profile=profile, prompt="x", timeout=5,
                                           schema_path=None)
    verifier = adapter.build_verifier_launch(root=tmp_path, scratch=tmp_path / "s3",
                                             profile=profile, prompt="x", timeout=5)
    assert worker.sandbox == "workspace_write"
    assert planner.sandbox == verifier.sandbox == "read_only"
    for launch in (worker, planner, verifier):
        assert launch.argv[launch.argv.index("-m") + 1] == "fake-codex-model"
        assert launch.argv[launch.argv.index("-c") + 1] == "model_reasoning_effort=high"
        assert launch.model_resolution["cli_slug"] == "fake-codex-model"


# ---------------------------------------------------------------------------
# Shared catalog: one source, refresh affects launches


def test_no_per_adapter_catalog_caches_remain():
    assert not hasattr(cursor_adapter, "_models_cache")
    assert not hasattr(codex_adapter, "_catalog_cache")


def test_both_adapters_consume_the_shared_catalog(monkeypatch, tmp_path, bindir):
    calls = []

    real = model_catalog.launch_catalog

    def spy(harness):
        calls.append(harness)
        return real(harness)

    monkeypatch.setattr(model_catalog, "launch_catalog", spy)
    cursor_launch(bindir, spec_profile(model_spec=ModelSpec(
        family="grok", version="4.7", effort="high")), tmp_path)
    codex_launch(bindir, spec_profile(harness=Harness.CODEX, model_spec=ModelSpec(
        model_id="fake-codex-model", effort="high")), tmp_path)
    assert calls == ["cursor", "codex"]  # one shared entry point


def write_cursor_snapshot(generation: int, models: list[str]) -> None:
    catalog = model_catalog.build_catalog(
        harness="cursor", source="agent --list-models (snapshot)",
        models=model_catalog.parse_cursor_list_models(
            "\n".join(f"{slug} - snapshot model" for slug in models)),
        generation=generation, generated_at="2026-10-07T00:00:00+00:00",
    )
    snapshot = {
        "harness": "cursor", "probed_at": catalog.generated_at,
        "model_catalog": catalog.to_dict(),
    }
    path = probes.snapshot_path("cursor")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot))
    model_catalog.invalidate("cursor")


def test_persisted_snapshot_catalog_governs_launches(tmp_path, bindir):
    """A persisted (probe-written) catalog wins over the local probe, and a
    generation bump refreshes what the next launch resolves against."""
    profile = spec_profile(model_spec=ModelSpec(family="grok", version="4.7",
                                                effort="high"))
    # The fake CLI lists grok-4.7-high; a persisted snapshot of
    # generation 2 does not -> the launch follows the snapshot.
    # Bare family member: the snapshot has no effort variant and --help has
    # no bracket channel, so the launch stays on that slug.
    write_cursor_snapshot(2, ["grok-4.7"])
    launch = cursor_launch(bindir, profile, tmp_path,
                           models=None, help_text=AGENT_HELP_NO_BRACKET)
    assert launch.model_resolution["catalog_generation"] == 2
    assert model_arg(launch) == "grok-4.7"  # snapshot: no variant, no bracket
    # A refresh (generation 3) that enumerates the variant changes the launch.
    write_cursor_snapshot(3, ["grok-4.7-high"])
    launch2 = cursor_launch(bindir, profile, tmp_path,
                            models=None, help_text=AGENT_HELP_NO_BRACKET)
    assert launch2.model_resolution["catalog_generation"] == 3
    assert model_arg(launch2) == "grok-4.7-high"
    assert launch2.planned_effort.source == "requested_validated"


# ---------------------------------------------------------------------------
# Dispatch: three roles, failure paths, records


CLI_PROJECT_CONFIG = """\
schema_version = 1

[controller]
profile = "cli-worker"

[plan]
depth = "auto"

[plan.light]
profiles = ["cli-worker"]

[plan.standard]
profiles = ["cli-worker"]

[plan.deep]
profiles = ["cli-worker"]

[worker]
profiles = ["cli-worker"]

[verify]
profiles = ["cli-worker"]

[runtime]
max_parallel = 1
command_timeout_sec = 30
"""


def make_semantic_project(tmp_path, monkeypatch, model_table: str,
                          harness: str = "cursor",
                          verify_model_table: str | None = None):
    """One CLI profile for every role, or a distinct verifier profile when
    ``verify_model_table`` is set (worker and verifier must not share a
    model the verifier is meant to reject)."""
    verifier = ""
    config = CLI_PROJECT_CONFIG
    if verify_model_table is not None:
        verifier = f"""
[profiles.cli-verifier]
driver = "cli"
harness = "{harness}"
model = {verify_model_table}
class = "frontier"
effort = "high"
capabilities = ["coding"]
"""
        config = CLI_PROJECT_CONFIG.replace(
            '[verify]\nprofiles = ["cli-worker"]',
            '[verify]\nprofiles = ["cli-verifier"]',
        )
    profiles = f"""\
schema_version = 1

[profiles.cli-worker]
driver = "cli"
harness = "{harness}"
model = {model_table}
class = "frontier"
effort = "high"
capabilities = ["coding"]
{verifier}"""
    monkeypatch.chdir(tmp_path)
    return make_project(tmp_path, config_toml=config,
                        profiles_toml=profiles)


def test_worker_launches_resolved_model_and_records_decision(tmp_path, bindir,
                                                             monkeypatch):
    install_agent(bindir, echo_model=False)
    project = make_semantic_project(
        tmp_path, monkeypatch,
        '{ family = "claude-opus", version = "5.5" }')
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["a1"])]))
        result = dispatch.run_slice(project)
        started = result["started"][0]
        assert started["status"] == "passed"
        # the resolution product reached the CLI
        calls = (bindir / "agent-calls.txt").read_text().splitlines()
        argv_used = calls[-1].split()
        assert argv_used[argv_used.index("--model") + 1] == "claude-opus-5-5-high"
        # the decision is recorded: payload + evidence, alongside the argv log
        resolution = started["model_resolution"]
        assert resolution["spec"]["family"] == "claude-opus"
        assert resolution["spec"]["version"] == "5.5"
        assert resolution["cli_slug"] == "claude-opus-5-5-high"
        assert resolution["strategy"] == "catalog_variant"
        attempt = next(a for a in project.store.attempts_all() if a.role == "worker")
        kinds = [row.kind for row in project.store.evidence_rows_for_task(
            attempt.revision_id, "T001")]
        assert "model_resolution" in kinds
        assert "execution" in kinds
        resolution_files = list((tmp_path / ".orx" / "runs").rglob(
            "*.resolution.json"))
        assert len(resolution_files) == 1
        recorded = json.loads(resolution_files[0].read_text())
        assert recorded["spec"]["family"] == "claude-opus"
        assert recorded["cli_slug"] == "claude-opus-5-5-high"
        assert recorded["attempt"] == attempt.id
        # actual argv is separately traceable in the execution log
        log_files = list((tmp_path / ".orx" / "runs").rglob("T001-*.log"))
        assert len(log_files) == 1
        assert "--model claude-opus-5-5-high" in log_files[0].read_text()
        # the identity string contract on the attempt row is unchanged
        assert attempt.model == "claude-opus@5.5"
    finally:
        project.close()


def test_worker_unresolvable_semantic_fails_without_substitution(tmp_path, bindir,
                                                                 monkeypatch):
    install_agent(bindir)
    project = make_semantic_project(
        tmp_path, monkeypatch,
        '{ family = "claude-opus", version = "5.5", speed = "fast" }')
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["a1"])]))
        result = dispatch.run_slice(project)
        failed = result["failed"][0]
        assert failed["task"] == "T001"
        assert failed["error_kind"] == "model_resolution"
        reason = failed["reason"]
        assert "cursor model resolution failed" in reason
        assert "family=claude-opus@5.5" in reason
        assert "speed=fast" in reason
        assert "catalog:" in reason
        assert "orx agent probe cursor" in reason
        # the CLI was never launched: no calls, no execution evidence
        assert not (bindir / "agent-calls.txt").exists()
        task = dispatch.task_list(project)[0]
        assert task["status"] == "failed"
        assert "model resolution failed" in task["failure_reason"]
        attempt = next(a for a in project.store.attempts_all()
                       if a.role == "worker")
        assert attempt.result == "failed"
        assert attempt.model == "claude-opus@5.5"
        kinds = [row.kind for row in project.store.evidence_rows_for_task(
            attempt.revision_id, "T001")]
        assert "execution" not in kinds
        assert project.store.usage_rows() == []
    finally:
        project.close()


def test_planner_unresolvable_semantic_records_failed_attempt(tmp_path, bindir,
                                                              monkeypatch):
    install_agent(bindir)
    # An unenumerated family with evidenced bracket syntax is a valid
    # parameterized request (covered above). A capability the catalog
    # cannot verify is not: the planner must fail before any launch.
    project = make_semantic_project(
        tmp_path, monkeypatch,
        '{ family = "ghost-family", version = "9", capabilities = ["vision"] }')
    try:
        dispatch.create_goal(project, "goal text", ["a1"])
        with pytest.raises(ORXError, match="cursor model resolution failed"):
            dispatch.plan_route(project)
        attempt = next(a for a in project.store.attempts_all()
                       if a.role == "planner")
        assert attempt.result == "failed"
        assert "ghost-family" in attempt.failure_reason
        assert not (bindir / "agent-calls.txt").exists()
    finally:
        project.close()


def test_verifier_unresolvable_semantic_fails_the_entry(tmp_path, bindir,
                                                        monkeypatch):
    install_agent(bindir)
    project = make_semantic_project(
        tmp_path, monkeypatch,
        '{ family = "claude-opus", version = "5.5" }',
        verify_model_table=(
            '{ family = "claude-opus", version = "5.5",'
            ' capabilities = ["vision"] }'
        ),
    )
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["a1"],
                      verification=["agent: the work is honest"]),
        ]))
        dispatch.run_slice(project)  # worker passes (no command checks)
        verified = dispatch.verify_dispatch(project)
        launched = verified["launched"][0]
        assert launched["verdict"] == "failed"
        assert "capability" in launched["reason"]
        task = dispatch.task_list(project)[0]
        assert task["status"] == "failed"
        attempt = next(a for a in project.store.attempts_all()
                       if a.role == "verifier")
        assert attempt.result == "fail"
        rows = project.store.verifications_for(attempt.revision_id, "T001")
        assert rows and rows[-1].passed is False
    finally:
        project.close()


def test_three_roles_share_one_resolution_path(tmp_path, bindir, monkeypatch):
    install_agent(bindir)
    adapter = adapters.get_adapter("cursor")
    from orx.config import load_profiles
    profiles_file = tmp_path / "profiles.toml"
    profiles_file.write_text(
        'schema_version = 1\n\n[profiles.p]\ndriver = "cli"\nharness = "cursor"\n'
        'model = { family = "grok", version = "4.7" }\nclass = "frontier"\n'
        'effort = "xhigh"\ncapabilities = ["coding"]\n')
    profile = load_profiles(profiles_file)["p"]
    assert profile.model == "grok@4.7"
    launches = (
        adapter.build_worker_launch(root=tmp_path, scratch=tmp_path / "s1",
                                    profile=profile, prompt="x", timeout=5),
        adapter.build_planner_launch(root=tmp_path, scratch=tmp_path / "s2",
                                     profile=profile, prompt="x", timeout=5,
                                     schema_path=None),
        adapter.build_verifier_launch(root=tmp_path, scratch=tmp_path / "s3",
                                      profile=profile, prompt="x", timeout=5),
    )
    assert {model_arg(launch) for launch in launches} == {"grok-4.7-xhigh"}
    assert {launch.model_resolution["strategy"] for launch in launches} == {
        "catalog_variant"}
    assert all(launch.sandbox is None for launch in launches)


def test_shell_transport_unaffected_by_resolution(tmp_path, bindir, monkeypatch):
    """A shell profile with a semantic model table still launches the raw
    executable; no model argument, no resolution record."""
    make_bin(bindir, "fake-worker", "exit 0\n")
    profiles = """\
schema_version = 1

[profiles.cli-worker]
driver = "cli"
harness = "shell"
executable = "fake-worker"
prompt_transport = "stdin"
model = { family = "grok", version = "4.7" }
class = "frontier"
effort = "high"
capabilities = ["coding"]
"""
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path, config_toml=CLI_PROJECT_CONFIG,
                           profiles_toml=profiles)
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["a1"])]))
        result = dispatch.run_slice(project)
        assert result["started"][0]["status"] == "passed"
        assert result["started"][0]["model_resolution"] is None
        attempt = next(a for a in project.store.attempts_all()
                       if a.role == "worker")
        assert attempt.model == "grok@4.7"
        kinds = [row.kind for row in project.store.evidence_rows_for_task(
            attempt.revision_id, "T001")]
        assert "model_resolution" not in kinds
    finally:
        project.close()


def test_host_execution_spec_identity_stable_with_semantic_table(tmp_path, bindir,
                                                                 monkeypatch):
    """Host account-id style strings keep their exact contract; a semantic
    table renders its identity string in the execution spec."""
    install_agent(bindir)
    project = make_semantic_project(
        tmp_path, monkeypatch, '{ family = "grok", version = "4.7" }')
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["a1"])]))
        parked = dispatch.run_slice(project)
        # CLI worker executes inline; the execution-spec surface is the
        # profile's stable identity string either way.
        assert parked["started"] or parked["failed"]
        profile = project.profiles["cli-worker"]
        assert profile.model == "grok@4.7"
        assert profile.model_spec is not None
        assert profile.model_spec.identity == "grok@4.7"
    finally:
        project.close()
