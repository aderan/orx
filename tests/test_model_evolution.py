"""G008 T004: catalog evolution resilience — the semantic configuration is
stable while the CLI's model catalog moves underneath it.

An independent evolution matrix (not the resolver's unit table): every
scenario installs a stand-in ``agent`` whose ``--list-models`` and ``--help``
serve FILES the test rewrites between generations, so one launch sequence
witnesses the catalog actually evolving. The semantic input of a scenario is
built ONCE as a module constant and reused verbatim across generations —
"the configuration never changed" is structural, not narrated.

Matrix axes (each maps to a Goal acceptance line):

* prefix rename + version bump (cursor-grok-4.6 -> grok-4.7): a FIXED
  version request fails explicitly — never the other fixed version, never
  auto, never a silent bracket guess; only an explicit FLOATING policy
  (version omitted, or version = "latest") may select the new version;
* variant add/delete: the same config becomes resolvable when a refresh
  adds the variant, and returns a diagnostic when the variant is removed
  (never a quieter level, never a speed downgrade);
* enumerated <-> parameterized transitions in both directions;
* parameter-evidence narrowing (help stops demonstrating a key);
* multi-candidate ambiguity on the REAL captured catalog (grok 4.5/4.6/4.7
  coexist -> floating is a diagnostic, never a guess);
* representation-only changes (prefix spelling, provider prefix) resolve
  to equivalent targets;
* catalog format change and discovery failure: diagnostics or in-family
  degradation — never a fake catalog, never a substituted model.

All harnesses are fakes on PATH; every catalog is local fake output (plus
the real CLI captures under tests/fixtures/model-catalog/). No network, no
completion, no interactive UI.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from orx import adapters, model_catalog, probes
from orx.adapters import cursor as cursor_adapter
from orx.adapters.base import ModelResolutionError
from orx.config import Profile
from orx.models import ModelSpec
from orx.records import Driver, Effort, Harness, ModelClass

FIXTURES = Path(__file__).parent / "fixtures" / "model-catalog"


def make_bin(directory: Path, name: str, script: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def bindir(tmp_path, monkeypatch):
    """Fake harness binaries first on PATH; shared caches reset around it.

    The stand-ins installed below SHADOW any real agent/codex on the host:
    every launch in this module sees the fake world only."""
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")
    cursor_adapter.reset_caches()
    yield directory
    cursor_adapter.reset_caches()


class FakeCLI:
    """The stand-in Cursor CLI as a mutable world.

    ``--list-models`` and ``--help`` serve files the test rewrites between
    "generations"; ``evolve()`` swaps the served text and drops the
    in-process launch-discovery cache, so the next launch sees the new
    catalog exactly as a refresh would. Flags: ``echo_model`` makes a
    launch echo the model argument it received (bracket echo evidence);
    ``models_fail`` makes the models channel exit non-zero (discovery
    failure). Installs itself on PATH — nothing here can fall through to
    a real binary."""

    def __init__(self, bindir: Path, tmp_path: Path, models: str,
                 help_text: str, *, echo_model: bool = False,
                 models_fail: bool = False):
        self.bindir = bindir
        self.models_file = tmp_path / "cli-list-models.txt"
        self.help_file = tmp_path / "cli-help.txt"
        self.calls = bindir / "agent-calls.txt"
        self.models_file.write_text(models)
        self.help_file.write_text(help_text)
        self.echo_model = echo_model
        self.models_fail = models_fail
        self._install()

    def _install(self) -> None:
        if self.models_fail:
            models_branch = (
                f'if [ "$1" = "--list-models" ]; then\n'
                f'  echo "agent: backend unavailable" >&2\n'
                f'  exit 1\n'
                f'fi\n'
            )
        else:
            models_branch = (
                f'if [ "$1" = "--list-models" ]; then\n'
                f'  cat "{self.models_file}"\n'
                f'  exit 0\n'
                f'fi\n'
            )
        if self.echo_model:
            emit = (
                'prev=""\nfor a in "$@"; do\n'
                '  if [ "$prev" = "--model" ]; then'
                ' echo "{\\"result\\": \\"done\\", \\"model\\": \\"$a\\"}";'
                ' exit 0; fi\n'
                '  prev="$a"\ndone\n'
            )
        else:
            emit = 'echo "{\\"result\\": \\"worker done\\"}"\nexit 0\n'
        script = f"""#!/bin/sh
if [ "$1" = "--help" ]; then
  cat "{self.help_file}"
  exit 0
fi
{models_branch}n=$#
i=0
line=""
for a in "$@"; do
  i=$((i+1))
  if [ "$i" -lt "$n" ]; then line="$line $a"; fi
done
echo "$line" >> "{self.calls}"
{emit}"""
        make_bin(self.bindir, "agent", script)

    def evolve(self, models: str | None = None, help_text: str | None = None,
               *, echo_model: bool | None = None,
               models_fail: bool | None = None) -> None:
        if models is not None:
            self.models_file.write_text(models)
        if help_text is not None:
            self.help_file.write_text(help_text)
        if echo_model is not None:
            self.echo_model = echo_model
        if models_fail is not None:
            self.models_fail = models_fail
        self._install()
        model_catalog.invalidate("cursor")

    def launched_models(self) -> list[str]:
        """Every --model value the stand-in was launched with, in order."""
        if not self.calls.exists():
            return []
        out = []
        for line in self.calls.read_text().splitlines():
            argv = line.split()
            if "--model" in argv:
                out.append(argv[argv.index("--model") + 1])
        return out


def spec_profile(model_spec: ModelSpec, name="cursor-x",
                 effort=Effort.HIGH) -> Profile:
    return Profile(
        name=name, driver=Driver.CLI, harness=Harness.CURSOR,
        model=model_spec.identity, model_class=ModelClass.FRONTIER,
        effort=effort, capabilities=("coding",), model_spec=model_spec,
    )


def legacy_profile(model: str, effort=Effort.HIGH, name="legacy") -> Profile:
    return Profile(
        name=name, driver=Driver.CLI, harness=Harness.CURSOR,
        model=model, model_class=ModelClass.FRONTIER,
        effort=effort, capabilities=("coding",),
    )


def worker_launch(cli: FakeCLI, tmp_path: Path, spec: ModelSpec,
                  effort=Effort.HIGH):
    """Behavior-level resolution: build the real worker launch against the
    stand-in CLI's current catalog state (never the resolver's privates)."""
    adapter = adapters.get_adapter("cursor")
    return adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "scratch", profile=spec_profile(
            spec, effort=effort),
        prompt="x", timeout=5)


def model_arg(launch) -> str:
    argv = launch.argv
    return argv[argv.index("--model") + 1]


def resolution_failure(cli: FakeCLI, tmp_path: Path, spec: ModelSpec) -> str:
    """The behavior contract of an unresolvable request: the launch itself
    raises the diagnostic (nothing is substituted, nothing starts)."""
    with pytest.raises(ModelResolutionError) as excinfo:
        worker_launch(cli, tmp_path, spec)
    return str(excinfo.value)


# ---------------------------------------------------------------------------
# Catalog generations and the never-edited semantic inputs.


HELP_BRACKET = """agent [options] [prompt]
  -p, --print           --output-format <format>    --model <model>
  --workspace <path>    --trust      -f, --force    --yolo
  example: agent --model 'claude-opus-4-8[context=1m,effort=high,fast=false]'
"""

HELP_NO_BRACKET = """agent [options] [prompt]
  -p, --print           --output-format <format>    --model <model>
  --workspace <path>    --trust      -f, --force    --yolo
"""

HELP_EFFORT_ONLY = """agent [options] [prompt]
  -p, --print           --output-format <format>    --model <model>
  --workspace <path>    --trust      -f, --force    --yolo
  example: agent --model 'gpt-5[effort=high]'
"""


def listing(*slugs: str) -> str:
    return "Available models\n\n" + "\n".join(
        f"{slug} - stand-in model" for slug in slugs) + "\n"


# The old world: the CLI prefixes the grok family and pins 4.6.
GEN_GROK46 = listing(
    "cursor-grok-4.6-low", "cursor-grok-4.6-medium", "cursor-grok-4.6-high",
    "cursor-grok-4.6-high-fast", "cursor-grok-4.6-xhigh", "auto",
)
# The new world: prefix dropped, version bumped, variants reshuffled.
GEN_GROK47 = listing(
    "grok-4.7-low", "grok-4.7-medium", "grok-4.7-high", "grok-4.7-high-fast",
    "grok-4.7-xhigh", "grok-4.7-xhigh-fast", "auto", "composer-2.5",
)
# No xhigh yet: the variant-addition starting point.
GEN_GROK47_NO_XHIGH = listing(
    "grok-4.7-low", "grok-4.7-medium", "grok-4.7-high", "auto",
)
# The high variant is gone (effort-variant removal).
GEN_GROK47_NO_HIGH = listing(
    "grok-4.7-low", "grok-4.7-medium", "grok-4.7-xhigh", "auto",
)
# Every fast variant is gone, all effort variants remain.
GEN_GROK47_NO_FAST = listing(
    "grok-4.7-low", "grok-4.7-medium", "grok-4.7-high", "grok-4.7-xhigh",
    "auto",
)
# A family that later leaves the enumeration for the parameter channel.
GEN_KIMI_ENUM = listing(
    "kimi-k3-low", "kimi-k3-high", "kimi-k3-max", "auto",
)
# A CLI whose listing format changed wholesale: parsable, zero models.
GEN_FORMAT_CHANGE = """MODEL CATALOG v2
  grok_4_7 :: high-speed reasoning
  (switch with: model select grok_4_7)
"""

# The semantic inputs. Built once; every generation below consumes these
# exact objects — the configuration is never edited across evolution.
SPEC_GROK_46_FIXED = ModelSpec(family="grok", version="4.6", effort="high")
SPEC_GROK_FLOAT = ModelSpec(family="grok", effort="high")
SPEC_GROK_FLOAT_LATEST = ModelSpec(family="grok", version="latest",
                                   effort="high")
SPEC_GROK_47_XHIGH = ModelSpec(family="grok", version="4.7", effort="xhigh")
SPEC_GROK_47_HIGH_FAST = ModelSpec(family="grok", version="4.7",
                                   effort="high", speed="fast")
SPEC_KIMI_HIGH = ModelSpec(family="kimi-k3", effort="high")
SPEC_GPT_CONTEXT = ModelSpec(family="gpt", version="7", effort="high",
                             context_tokens=1_000_000)


# ---------------------------------------------------------------------------
# Prefix rename + version bump: fixed fails, floating follows.


def test_fixed_version_request_fails_instead_of_switching(tmp_path, bindir):
    """cursor-grok-4.6 -> grok-4.7 with a FIXED 4.6 request: an explicit
    diagnostic naming the requested version and the offered one — never
    grok-4.7, never auto, never another family."""
    cli = FakeCLI(bindir, tmp_path, GEN_GROK46, HELP_BRACKET)
    # Old world: the prefixed spelling is absorbed, the target is 4.6.
    launch = worker_launch(cli, tmp_path, SPEC_GROK_46_FIXED)
    assert model_arg(launch) == "cursor-grok-4.6-high"
    assert launch.model_resolution["strategy"] == "catalog_variant"
    assert launch.planned_effort.source == "requested_validated"

    cli.evolve(GEN_GROK47)
    message = resolution_failure(cli, tmp_path, SPEC_GROK_46_FIXED)
    assert "cursor model resolution failed" in message
    assert "family=grok@4.6" in message
    assert "grok-4.7" in message  # the offered version is named
    assert "another fixed version" in message
    assert "catalog:" in message
    assert "orx agent probe cursor" in message
    # nothing was ever launched with a substituted model
    for slug in cli.launched_models():
        assert "4.6" in slug, slug


def test_floating_policy_selects_the_new_version(tmp_path, bindir):
    """The same floating declaration tracks the version bump: prefixed 4.6
    first, unprefixed 4.7 after — and version = "latest" is the same
    floating policy, not a third behavior."""
    cli = FakeCLI(bindir, tmp_path, GEN_GROK46, HELP_BRACKET)
    launch = worker_launch(cli, tmp_path, SPEC_GROK_FLOAT)
    assert model_arg(launch) == "cursor-grok-4.6-high"
    assert launch.model_resolution["strategy"] == "catalog_variant"
    assert launch.planned_effort.actual == "high"
    assert launch.planned_effort.source == "requested_validated"

    cli.evolve(GEN_GROK47)
    launch = worker_launch(cli, tmp_path, SPEC_GROK_FLOAT)
    assert model_arg(launch) == "grok-4.7-high"
    assert launch.model_resolution["strategy"] == "catalog_variant"
    assert launch.planned_effort.source == "requested_validated"

    latest = worker_launch(cli, tmp_path, SPEC_GROK_FLOAT_LATEST)
    assert model_arg(latest) == "grok-4.7-high"  # same floating outcome


def test_legacy_bare_slug_keeps_its_identity_across_rename(tmp_path, bindir):
    """A legacy bare slug is opaque: the old world rewrites it to the
    prefixed variant; after the rename it keeps the cursor-grok-4.6
    identity on the legacy ladder — it is never swapped to grok-4.7."""
    cli = FakeCLI(bindir, tmp_path, GEN_GROK46, HELP_BRACKET)
    adapter = adapters.get_adapter("cursor")
    legacy = legacy_profile("cursor-grok-4.6")
    launch = adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "s1", profile=legacy,
        prompt="x", timeout=5)
    assert model_arg(launch) == "cursor-grok-4.6-high"

    cli.evolve(GEN_GROK47)
    launch = adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "s2", profile=legacy,
        prompt="x", timeout=5)
    slug = model_arg(launch)
    assert slug.startswith("cursor-grok-4.6")  # identity preserved
    assert "grok-4.7" not in slug and slug != "auto"
    # unlisted id + evidenced bracket channel: the legacy ladder's bracket
    # rung, still naming the same model
    assert launch.model_resolution["strategy"] == "bracket_synthesis"
    assert slug == "cursor-grok-4.6[effort=high]"


# ---------------------------------------------------------------------------
# Variant add / delete.


def test_variant_added_by_refresh_unlocks_the_same_config(tmp_path, bindir):
    """xhigh does not exist yet -> diagnostic; the catalog refresh adds it
    -> the SAME spec object resolves. No configuration edit happened."""
    cli = FakeCLI(bindir, tmp_path, GEN_GROK47_NO_XHIGH, HELP_BRACKET)
    message = resolution_failure(cli, tmp_path, SPEC_GROK_47_XHIGH)
    assert "xhigh" in message
    assert "not among them" in message
    assert "family=grok@4.7" in message and "catalog:" in message

    cli.evolve(GEN_GROK47)
    launch = worker_launch(cli, tmp_path, SPEC_GROK_47_XHIGH)
    assert model_arg(launch) == "grok-4.7-xhigh"
    assert launch.planned_effort.actual == "xhigh"
    assert launch.planned_effort.source == "requested_validated"


def test_removed_effort_variant_is_a_diagnostic_never_a_downgrade(
        tmp_path, bindir):
    cli = FakeCLI(bindir, tmp_path, GEN_GROK47, HELP_BRACKET)
    high = ModelSpec(family="grok", version="4.7", effort="high")
    launch = worker_launch(cli, tmp_path, high)
    assert model_arg(launch) == "grok-4.7-high"

    cli.evolve(GEN_GROK47_NO_HIGH)
    message = resolution_failure(cli, tmp_path, high)
    assert "family=grok@4.7" in message
    assert "effort=high" in message
    assert "not among them" in message
    # the surviving levels are named so the diagnostic is actionable
    assert "low" in message and "medium" in message and "xhigh" in message


def test_removed_fast_variant_is_a_diagnostic_not_standard(tmp_path, bindir):
    cli = FakeCLI(bindir, tmp_path, GEN_GROK47, HELP_BRACKET)
    launch = worker_launch(cli, tmp_path, SPEC_GROK_47_HIGH_FAST)
    assert model_arg(launch) == "grok-4.7-high-fast"

    cli.evolve(GEN_GROK47_NO_FAST)
    message = resolution_failure(cli, tmp_path, SPEC_GROK_47_HIGH_FAST)
    assert "speed=fast" in message
    assert "no enumerated variant" in message


# ---------------------------------------------------------------------------
# Enumerated <-> parameterized transitions.


class FakeResult:
    def __init__(self, stdout, exit_code=0):
        self.stdout, self.stderr = stdout, ""
        self.exit_code, self.timed_out = exit_code, False


def test_enumerated_family_becomes_parameterized(tmp_path, bindir):
    """kimi-k3 leaves the enumeration for the bracket channel: the same
    semantic config resolves through bracket synthesis, confirmed only on
    echo — the honest degradation, still the same model identity."""
    cli = FakeCLI(bindir, tmp_path, GEN_KIMI_ENUM, HELP_BRACKET)
    launch = worker_launch(cli, tmp_path, SPEC_KIMI_HIGH)
    assert model_arg(launch) == "kimi-k3-high"
    assert launch.model_resolution["strategy"] == "catalog_variant"

    cli.evolve(listing("auto", "grok-4.7-high"), echo_model=True)
    adapter = adapters.get_adapter("cursor")
    launch = adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "s2",
        profile=spec_profile(SPEC_KIMI_HIGH), prompt="x", timeout=5)
    bracketed = model_arg(launch)
    assert bracketed == "kimi-k3[effort=high]"
    assert launch.model_resolution["strategy"] == "bracket_synthesis"

    echoed = json.dumps({"result": "done", "model": bracketed})
    ok = adapter.effort_outcome(launch, FakeResult(echoed, exit_code=0))
    assert (ok.actual, ok.source) == ("high", "requested_validated")
    silent = adapter.effort_outcome(
        launch, FakeResult('{"result": "done"}', exit_code=0))
    assert silent.source is None and silent.actual == "provider_default"


def test_parameterized_family_becomes_enumerated(tmp_path, bindir):
    """The reverse transition: gpt-6.1-sol is unlisted (bracket), then the
    CLI enumerates a baked variant — the bracket rung is dropped for the
    catalog-confirmed slug, never kept out of habit."""
    spec = ModelSpec(family="gpt", version="6.1-sol", effort="high")
    cli = FakeCLI(bindir, tmp_path, listing("auto", "grok-4.7-high"),
                  HELP_BRACKET)
    launch = worker_launch(cli, tmp_path, spec)
    assert model_arg(launch) == "gpt-6.1-sol[effort=high]"
    assert launch.model_resolution["strategy"] == "bracket_synthesis"

    cli.evolve(listing("auto", "grok-4.7-high", "gpt-6.1-sol-low",
                       "gpt-6.1-sol-high"))
    launch = worker_launch(cli, tmp_path, spec)
    assert model_arg(launch) == "gpt-6.1-sol-high"
    assert launch.model_resolution["strategy"] == "catalog_variant"
    assert launch.planned_effort.source == "requested_validated"


def test_help_parameter_evidence_narrowed_is_a_diagnostic(tmp_path, bindir):
    """Parameter capability missing after --help evolution: v1 help
    demonstrates context/effort/fast; v2 demonstrates effort only. The
    context request becomes an explicit unsupported-parameter diagnostic
    (never a bracket with a key the CLI no longer accepts)."""
    cli = FakeCLI(bindir, tmp_path, listing("auto"), HELP_BRACKET)
    launch = worker_launch(cli, tmp_path, SPEC_GPT_CONTEXT)
    assert model_arg(launch) == "gpt-7[context=1m,effort=high]"

    cli.evolve(help_text=HELP_EFFORT_ONLY)
    message = resolution_failure(cli, tmp_path, SPEC_GPT_CONTEXT)
    assert "supports only" in message
    assert "context" in message
    assert "effort" in message


# ---------------------------------------------------------------------------
# Ambiguity on the real captured catalog; representation equivalence.


def real_capture(tmp_path, bindir) -> FakeCLI:
    """The stand-in serving the real 2026-10-07 CLI captures verbatim."""
    return FakeCLI(bindir, tmp_path,
                   (FIXTURES / "cursor-list-models.txt").read_text(),
                   (FIXTURES / "agent-help.txt").read_text())


def test_real_capture_floating_ambiguity_is_a_diagnostic(tmp_path, bindir):
    """The real capture enumerates grok at three versions (cursor-grok-4.5,
    cursor-grok-4.6, grok-4.7): a floating request must name all three and
    refuse to guess — while fixed requests stay exact."""
    cli = real_capture(tmp_path, bindir)

    message = resolution_failure(cli, tmp_path, SPEC_GROK_FLOAT)
    assert "cursor model resolution failed" in message
    assert "family=grok" in message
    for version in ("grok-4.5", "grok-4.6", "grok-4.7"):
        assert version in message
    assert "will not guess" in message

    # Fixed requests on the same capture stay exact (controls).
    fixed_47 = worker_launch(cli, tmp_path, ModelSpec(
        family="grok", version="4.7", effort="xhigh"))
    assert model_arg(fixed_47) == "grok-4.7-xhigh"
    fixed_45 = worker_launch(cli, tmp_path, ModelSpec(
        family="grok", version="4.5", effort="high"))
    assert model_arg(fixed_45) == "cursor-grok-4.5-high"
    # A version the capture never had is a version-evolution failure that
    # names what IS offered — not a jump to the newest.
    message = resolution_failure(cli, tmp_path, ModelSpec(
        family="grok", version="4.4", effort="high"))
    assert "family=grok@4.4" in message
    assert "grok-4.7" in message and "another fixed version" in message


def test_representation_only_changes_resolve_to_equivalent_targets(
        tmp_path, bindir):
    """Prefix spelling, a declared provider, and the legacy bare slug of
    the same model all reach the same CLI target on the real capture."""
    cli = real_capture(tmp_path, bindir)

    equivalents = [
        ModelSpec(family="grok-4.6", effort="high"),          # family token
        ModelSpec(family="grok", version="4.6", effort="high"),  # semantic
        ModelSpec(provider="cursor", family="grok", version="4.6",
                  effort="high"),                               # provider
    ]
    slugs = {model_arg(worker_launch(cli, tmp_path, spec))
             for spec in equivalents}
    assert slugs == {"cursor-grok-4.6-high"}

    # The legacy bare slug of the same model rides the same target.
    adapter = adapters.get_adapter("cursor")
    legacy = legacy_profile("grok-4.7", effort=Effort.XHIGH)
    launch = adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "legacy", profile=legacy,
        prompt="x", timeout=5)
    assert model_arg(launch) == "grok-4.7-xhigh"
    semantic = worker_launch(cli, tmp_path, ModelSpec(
        family="grok", version="4.7", effort="xhigh"))
    assert model_arg(semantic) == model_arg(launch)


# ---------------------------------------------------------------------------
# Catalog format change and discovery failure.


def test_catalog_format_change_degrades_in_family_or_fails(tmp_path, bindir):
    """A listing whose format changed wholesale parses to a valid EMPTY
    catalog. With the bracket channel evidenced, a semantic request
    degrades IN FAMILY (bracket on the same identity); without bracket
    evidence it is an explicit diagnostic. auto and other families are
    never the answer; the legacy id keeps its bare passthrough."""
    cli = FakeCLI(bindir, tmp_path, GEN_GROK47, HELP_BRACKET)
    launch = worker_launch(cli, tmp_path, SPEC_GROK_FLOAT)
    assert model_arg(launch) == "grok-4.7-high"

    cli.evolve(GEN_FORMAT_CHANGE)
    degraded = worker_launch(cli, tmp_path, SPEC_GROK_FLOAT)
    assert model_arg(degraded) == "grok[effort=high]"  # same family, no guess
    assert degraded.model_resolution["strategy"] == "bracket_synthesis"

    cli.evolve(help_text=HELP_NO_BRACKET)
    message = resolution_failure(cli, tmp_path, SPEC_GROK_FLOAT)
    assert "cursor model resolution failed" in message
    assert "family=grok" in message
    assert "catalog: 0 models" in message
    assert "orx agent probe cursor" in message

    # legacy id + no bracket channel: bare passthrough, provider_default
    adapter = adapters.get_adapter("cursor")
    launch = adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "legacy",
        profile=legacy_profile("grok-4.7"), prompt="x", timeout=5)
    assert model_arg(launch) == "grok-4.7"
    assert launch.model_resolution["strategy"] == "bare_unlisted"
    assert launch.planned_effort.source is None


def test_discovery_failure_is_a_diagnostic_never_a_guess(tmp_path, bindir):
    """--list-models failing yields no catalog at all. Without bracket
    evidence the semantic request names the absence and the refresh
    command; the legacy id keeps the bare passthrough. Neither selects
    auto or another family."""
    cli = FakeCLI(bindir, tmp_path, GEN_GROK47, HELP_NO_BRACKET,
                  models_fail=True)
    message = resolution_failure(cli, tmp_path, SPEC_GROK_FLOAT)
    assert "cursor model resolution failed" in message
    assert "family=grok" in message
    assert "catalog: none available locally" in message
    assert "orx agent probe cursor" in message

    adapter = adapters.get_adapter("cursor")
    launch = adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "legacy",
        profile=legacy_profile("grok-4.7"), prompt="x", timeout=5)
    assert model_arg(launch) == "grok-4.7"
    assert launch.model_resolution["strategy"] == "bare_unlisted"
    assert launch.planned_effort.source is None


# ---------------------------------------------------------------------------
# Codex evolution: a persisted refresh changes the launch.


def install_codex(bindir: Path, models_json: str) -> None:
    make_bin(bindir, "codex", f"""#!/bin/sh
if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then
  cat <<'H'
usage: codex exec [OPTIONS] PROMPT
  --json            -m, --model <MODEL>    -C, --cd <DIR>
  --output-last-message <FILE>   --output-schema <FILE>
  -s, --sandbox <SANDBOX_MODE>   --ephemeral
H
  exit 0
fi
if [ "$1" = "debug" ]; then
  cat <<'M'
{models_json}
M
  exit 0
fi
exit 0
""")


def test_codex_refresh_removing_a_level_re_disables_the_flag(tmp_path, bindir,
                                                             monkeypatch):
    """The codex side of evolution: a persisted probe refresh (the
    `orx agent probe codex` surface) drops 'high' from the model's
    reasoning levels; the same semantic config then fails explicitly
    against the NEW generation, and the legacy bare string keeps its
    tolerated passthrough."""
    from orx.adapters import codex as codex_adapter

    v1 = json.dumps({"models": [
        {"slug": "fake-codex-model",
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
    ]})
    install_codex(bindir, v1)
    spec = ModelSpec(model_id="fake-codex-model", effort="high")
    profile = Profile(
        name="codex-x", driver=Driver.CLI, harness=Harness.CODEX,
        model=spec.identity, model_class=ModelClass.FRONTIER,
        effort=Effort.HIGH, capabilities=("coding",), model_spec=spec,
    )
    adapter = adapters.get_adapter("codex")
    launch = adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "s1", profile=profile,
        prompt="x", timeout=5)
    assert launch.argv[launch.argv.index("-c") + 1] == \
        "model_reasoning_effort=high"
    assert launch.model_resolution["catalog_generation"] == 0  # discovered

    # A persisted refresh (generation 1) without the high level.
    monkeypatch.setenv("ORX_DATA_DIR", str(tmp_path / "data"))
    v2 = json.dumps({"models": [
        {"slug": "fake-codex-model",
         "supported_reasoning_levels": [{"effort": "low"}]},
    ]})
    install_codex(bindir, v2)
    snap = probes.capability_snapshot("codex")
    assert snap["model_catalog"]["generation"] == 1

    codex_adapter.reset_caches()
    with pytest.raises(ModelResolutionError) as excinfo:
        adapter.build_worker_launch(
            root=tmp_path, scratch=tmp_path / "s2", profile=profile,
            prompt="x", timeout=5)
    message = str(excinfo.value)
    assert "codex model resolution failed" in message
    assert "reasoning levels" in message
    assert "generation 1" in message  # decided against the refreshed catalog

    legacy = Profile(
        name="codex-legacy", driver=Driver.CLI, harness=Harness.CODEX,
        model="fake-codex-model", model_class=ModelClass.FRONTIER,
        effort=Effort.HIGH, capabilities=("coding",),
    )
    launch = adapter.build_worker_launch(
        root=tmp_path, scratch=tmp_path / "s3", profile=legacy,
        prompt="x", timeout=5)
    assert "-c" not in launch.argv
    assert launch.planned_effort.actual == "provider_default"
    assert launch.model_resolution["catalog_generation"] == 1


# ---------------------------------------------------------------------------
# The no-substitution sweep: every matrix outcome stays in-family.


def test_no_matrix_outcome_ever_selects_auto_or_another_family(tmp_path,
                                                               bindir):
    """Structural guard over the whole matrix: for every (spec, catalog)
    pair, a successful resolution's slug starts with the requested
    family's own token (modulo the CLI prefix), and a failing one raises.
    auto and unrelated families can never be the answer to a grok or
    claude request."""
    cli = FakeCLI(bindir, tmp_path, GEN_GROK47, HELP_BRACKET)
    matrix = [
        (SPEC_GROK_46_FIXED, "grok"),      # fails (version evolution)
        (SPEC_GROK_FLOAT, "grok"),         # grok-4.7-high
        (SPEC_GROK_47_XHIGH, "grok"),      # grok-4.7-xhigh
        (SPEC_GROK_47_HIGH_FAST, "grok"),  # grok-4.7-high-fast
        (ModelSpec(family="claude-opus", version="5.5", effort="high"),
         "claude-opus"),                   # unlisted -> bracket
    ]
    for spec, stem in matrix:
        try:
            launch = worker_launch(cli, tmp_path, spec)
        except ModelResolutionError:
            continue  # a diagnostic is a legitimate matrix outcome
        slug = model_arg(launch).split("[")[0]
        base = (slug[len("cursor-"):] if slug.startswith("cursor-")
                else slug)
        assert base == stem or base.startswith(stem + "-"), (
            f"{spec.identity} resolved out of family to {slug!r}")
        assert slug != "auto"
