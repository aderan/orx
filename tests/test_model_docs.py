"""G008 T005: the model-selection documentation is executable and honest.

docs/model-selection.md is not prose-only: its TOML examples are extracted
and loaded through the REAL config loader, its migration table is proven
against a stand-in Cursor CLI replaying the REAL captured 2026-10-07 output
(a simulation — no real CLI, no network, no completion), and its provenance
claims are pinned so the doc cannot drift into unsourced or overstated
statements.

Covers the G008 documentation acceptance:

* every documented semantic example loads through ``load_profiles``;
* the semantic examples are equivalent to the existing bare-slug population
  — model identity (same resolved target), class, effort, capabilities —
  and routing order is identical between declaration forms;
* the effort-verification evidence rules the doc states are the ones the
  adapter implements (catalog-variant success, bracket echo, silent
  unknown);
* provenance is explicit: the 2026-10-07 / agent 2026.10.01 background,
  the T002-session transient auth failure, the T005 re-verification, and
  the real-captured / rebuilt / simulated sample markers, with the
  no-real-model-run statement;
* refresh and view name only the programmable entries; Tune appears only
  as labeled background, never as a step;
* README and routing-strategy link the doc, README's launch description
  matches the resolver, and the routing tiers/defaults are unchanged;
* the IMPLEMENTATION_PLAN G008 record is complete and marked finished
  without touching other Goals' history or any release material.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest

from orx import adapters, model_catalog, routing
from orx.adapters import cursor as cursor_adapter
from orx.config import Profile, load_profiles
from orx.records import (Driver, Effort, Harness, ModelClass, PlanDepth,
                         ResourceStatus, Role)

ROOT = Path(__file__).parent.parent
DOC = ROOT / "docs" / "model-selection.md"
README = ROOT / "README.md"
ROUTING = ROOT / "docs" / "routing-strategy.md"
PLAN = ROOT / "IMPLEMENTATION_PLAN.md"
FIXTURES = Path(__file__).parent / "fixtures" / "model-catalog"


def flat(path: Path) -> str:
    """File text with runs of whitespace collapsed (line-wrap-immune)."""
    return " ".join(path.read_text().split())


# ---------------------------------------------------------------------------
# Extraction: the documented examples, verbatim out of the doc.


def doc_examples() -> dict[str, str]:
    """Every ```toml block the doc marks with `# orx-doc-example: <name>`."""
    text = DOC.read_text()
    examples: dict[str, str] = {}
    for match in re.finditer(r"```toml\n(.*?)```", text, re.DOTALL):
        block = match.group(1)
        first = block.splitlines()[0].strip()
        if first.startswith("# orx-doc-example:"):
            name = first.split(":", 1)[1].strip()
            assert name and name not in examples, name
            examples[name] = block
    return examples


def load_example(tmp_path: Path, name: str) -> dict[str, Profile]:
    """One documented example through the REAL loader boundary."""
    path = tmp_path / f"{name}.toml"
    path.write_text(doc_examples()[name])
    return load_profiles(path)


# ---------------------------------------------------------------------------
# The replayed world: a stand-in serving the real captured output.


def make_bin(directory: Path, name: str, script: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def replay(tmp_path, monkeypatch):
    """A stand-in `agent` first on PATH serving the REAL captured fixtures.

    This is the documented "simulated" sample class: replayed capture, not
    a live CLI. Launches answer a fixed success envelope (no echo)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    make_bin(bindir, "agent", f"""#!/bin/sh
if [ "$1" = "--version" ]; then echo "agent 2026.10.01"; exit 0; fi
if [ "$1" = "--help" ]; then cat "{FIXTURES / 'agent-help.txt'}"; exit 0; fi
if [ "$1" = "--list-models" ]; then cat "{FIXTURES / 'cursor-list-models.txt'}"; exit 0; fi
echo '{{"result": "worker done"}}'
exit 0
""")
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    cursor_adapter.reset_caches()
    model_catalog.invalidate("cursor")
    yield bindir
    cursor_adapter.reset_caches()
    model_catalog.invalidate("cursor")


def worker_launch(profile: Profile, tmp_path: Path):
    return adapters.get_adapter("cursor").build_worker_launch(
        root=tmp_path, scratch=tmp_path / "scratch", profile=profile,
        prompt="x", timeout=5)


def model_arg(launch) -> str:
    argv = launch.argv
    return argv[argv.index("--model") + 1]


class FakeResult:
    def __init__(self, stdout, exit_code=0):
        self.stdout, self.stderr = stdout, ""
        self.exit_code, self.timed_out = exit_code, False


def legacy(name: str, model: str, effort: Effort,
           model_class: ModelClass = ModelClass.FRONTIER) -> Profile:
    return Profile(
        name=name, driver=Driver.CLI, harness=Harness.CURSOR,
        model=model, model_class=model_class, effort=effort,
        capabilities=("coding",))


# ---------------------------------------------------------------------------
# 1. The documented examples load through the real loader.


def test_doc_toml_examples_load_through_the_real_loader(tmp_path):
    examples = doc_examples()
    assert set(examples) == {
        "semantic-population", "floating-population", "legacy-population"}
    semantic = load_example(tmp_path, "semantic-population")
    floating = load_example(tmp_path, "floating-population")
    legacy_profiles = load_example(tmp_path, "legacy-population")

    # identity strings are semantic, never CLI slugs
    assert semantic["sem-opus"].model == "claude-opus@5.5"
    assert semantic["sem-gpt"].model == "gpt@6.1-sol"
    assert semantic["sem-grok"].model == "grok@4.7"
    assert semantic["sem-composer"].model == "composer-2.5"
    for profile in semantic.values():
        assert profile.model_spec is not None
    # "latest" and omitted are the same canonical floating form
    omitted, latest = floating["float-grok"], floating["float-grok-latest"]
    assert omitted.model_spec.version is None
    assert latest.model_spec.version is None
    assert omitted.model == latest.model == "grok"
    # the legacy population keeps bare strings and no specs
    for profile in legacy_profiles.values():
        assert profile.model_spec is None
    assert legacy_profiles["legacy-account"].model == \
        "account:bigmodel-individual-coding-plan/GLM-5.3"


# ---------------------------------------------------------------------------
# 2. Equivalence with the existing population, on the replayed capture.


def test_doc_semantic_examples_match_the_legacy_population(tmp_path, replay):
    """Every documented semantic profile is equivalent to its legacy twin:
    same class, effort, capabilities; same resolved CLI target on the real
    captured catalog (representation-only differences aside)."""
    semantic = load_example(tmp_path, "semantic-population")
    legacy_profiles = load_example(tmp_path, "legacy-population")

    # legacy twins for the doc's migration table rows whose legacy spelling
    # is not in the stored legacy block (high-effort grok, fast composer)
    twins = {
        "sem-grok-high": legacy("grok-high", "grok-4.7", Effort.HIGH,
                                ModelClass.STRONG),
        "sem-composer": legacy("composer-fast", "composer-2.5-fast",
                               Effort.HIGH, ModelClass.ECONOMY),
    }

    # (semantic target, legacy target) per documented migration row
    expected_targets = {
        # the bracket uses the DECLARED version spelling (5.5 vs 5-5) —
        # same model, representation-only difference (documented)
        "sem-opus": ("claude-opus-5.5[effort=high]",
                     "claude-opus-5-5[effort=high]"),
        "sem-gpt": ("gpt-6.1-sol[effort=high]",) * 2,
        "sem-gpt-max": ("gpt-6.1-sol[effort=max]",) * 2,
        "sem-grok": ("grok-4.7-xhigh",) * 2,
        "sem-grok-high": ("grok-4.7-high",) * 2,
        "sem-composer": ("composer-2.5-fast",) * 2,
    }
    legacy_for = {
        "sem-opus": legacy_profiles["legacy-opus"],
        "sem-gpt": legacy_profiles["legacy-gpt"],
        "sem-gpt-max": legacy_profiles["legacy-bracket"],
        "sem-grok": legacy_profiles["legacy-grok"],
        "sem-grok-high": twins["sem-grok-high"],
        "sem-composer": twins["sem-composer"],
    }

    for name, (semantic_slug, legacy_slug) in expected_targets.items():
        sem_profile, leg_profile = semantic[name], legacy_for[name]
        # profile-surface equivalence: class, effort, capabilities
        assert sem_profile.model_class == leg_profile.model_class, name
        assert sem_profile.effort == leg_profile.effort, name
        assert sem_profile.capabilities == leg_profile.capabilities, name
        # resolution equivalence on the replayed real capture
        assert model_arg(worker_launch(sem_profile, tmp_path)) == \
            semantic_slug, name
        assert model_arg(worker_launch(leg_profile, tmp_path)) == \
            legacy_slug, name
        assert semantic_slug.replace("5.5", "5-5") == legacy_slug, name

    # the account id has no semantic twin by design: opaque, host-side
    account = legacy_profiles["legacy-account"]
    assert model_arg(worker_launch(account, tmp_path)) == \
        "account:bigmodel-individual-coding-plan/GLM-5.3[effort=high]"


def test_doc_effort_evidence_rules_are_the_adapters_rules(tmp_path, replay):
    """The documented EffortOutcome channels, on the doc's own profiles:
    catalog-variant success confirms; a bracket launch confirms only on a
    matching echo; a silent success stays provider_default."""
    adapter = adapters.get_adapter("cursor")
    semantic = load_example(tmp_path, "semantic-population")

    grok = worker_launch(semantic["sem-grok"], tmp_path)
    assert grok.planned_effort.source == "requested_validated"
    outcome = adapter.effort_outcome(grok, FakeResult('{"result": "done"}'))
    assert (outcome.actual, outcome.source) == ("xhigh", "requested_validated")

    opus = worker_launch(semantic["sem-opus"], tmp_path)
    bracketed = model_arg(opus)
    echoed = adapter.effort_outcome(
        opus, FakeResult(json.dumps({"result": "done", "model": bracketed})))
    assert (echoed.actual, echoed.source) == ("high", "requested_validated")
    silent = adapter.effort_outcome(opus, FakeResult('{"result": "done"}'))
    assert silent.source is None and silent.actual == "provider_default"

    composer = worker_launch(semantic["sem-composer"], tmp_path)
    assert composer.planned_effort.source is None
    assert composer.planned_effort.actual == "provider_default"


# ---------------------------------------------------------------------------
# 3. Routing order equivalence between declaration forms.


def test_routing_order_is_identical_between_declaration_forms(
        tmp_path, monkeypatch):
    from conftest import make_project

    config = """\
schema_version = 1

[controller]
profile = "plan-a"

[plan]
depth = "auto"
allow_class_downgrade = false

[plan.light]
profiles = ["plan-a"]

[plan.standard]
profiles = ["plan-a"]

[plan.deep]
profiles = ["frontier-a", "plan-a"]

[worker]
profiles = ["work-a", "work-b"]

[verify]
profiles = ["verify-a", "verify-vision"]

[runtime]
max_parallel = 1
command_timeout_sec = 30
"""
    strong = 'driver = "cli"\nharness = "cursor"\nclass = "strong"\n' \
             'effort = "high"\ncapabilities = ["coding"]\n'

    def profiles_toml(form: str) -> str:
        def model(slug: str) -> str:
            if form == "bare":
                return f'model = "{slug}"\n'
            family = slug.rpartition("-4.7")[0] or slug
            return f'model = {{ family = "{family}", version = "4.7" }}\n'
        return f"""schema_version = 1

[profiles.plan-a]
{model("grok-4.7")}{strong}
[profiles.frontier-a]
{model("claude-x-4.7")}driver = "cli"
harness = "cursor"
class = "frontier"
effort = "high"
capabilities = ["coding"]

[profiles.work-a]
{model("grok-4.7")}{strong}
[profiles.work-b]
{model("kimi-4.7")}driver = "cli"
harness = "cursor"
class = "economy"
effort = "medium"
capabilities = ["coding"]

[profiles.verify-a]
{model("glm-4.7")}driver = "cli"
harness = "cursor"
class = "strong"
effort = "high"
capabilities = ["coding"]

[profiles.verify-vision]
{model("gpt-v-4.7")}driver = "cli"
harness = "cursor"
class = "strong"
effort = "high"
capabilities = ["coding", "vision"]
"""

    outcomes = {}
    for form in ("bare", "semantic"):
        directory = tmp_path / form
        directory.mkdir()
        monkeypatch.chdir(directory)
        project = make_project(directory, config_toml=config,
                               profiles_toml=profiles_toml(form))
        try:
            def route(**kwargs):
                return routing.route(
                    project.store, project.config, project.profiles,
                    routing.RouteRequest(**kwargs))

            snap = {
                "worker": route(role=Role.WORKER),
                "planner_deep": route(role=Role.PLANNER, depth=PlanDepth.DEEP),
                "verifier_vision": route(role=Role.VERIFIER,
                                         required_capabilities=("vision",)),
            }
            project.store.resource_set("work-a", ResourceStatus.UNAVAILABLE)
            snap["worker_fallback"] = route(role=Role.WORKER)
            project.store.resource_set("frontier-a",
                                       ResourceStatus.UNAVAILABLE)
            snap["deep_downgrade_blocked"] = route(
                role=Role.PLANNER, depth=PlanDepth.DEEP)
            outcomes[form] = {
                name: {
                    "selected": result.selected,
                    "reason": result.reason,
                    "fallback_used": result.fallback_used,
                    "downgrade_blocked": result.downgrade_blocked,
                    "candidates": [c.to_dict() for c in result.candidates],
                }
                for name, result in snap.items()
            }
        finally:
            project.close()

    assert outcomes["bare"] == outcomes["semantic"]
    # the ladder itself is the documented one (not vacuously equal)
    assert outcomes["semantic"]["worker"]["selected"] == "work-a"
    assert outcomes["semantic"]["worker_fallback"]["selected"] == "work-b"
    assert outcomes["semantic"]["planner_deep"]["selected"] == "frontier-a"
    assert outcomes["semantic"]["verifier_vision"]["selected"] == \
        "verify-vision"
    assert outcomes["semantic"]["deep_downgrade_blocked"]["downgrade_blocked"]


# ---------------------------------------------------------------------------
# 4. Provenance and sample marking are explicit and sourced.


def test_doc_provenance_is_explicit_per_source():
    text = flat(DOC)
    # the dated background and CLI version are named
    assert "2026-10-07" in text
    assert "2026.10.01-e373342" in text  # E4: the T005 re-verification
    # each evidence row carries its own source column
    for row in ("| E1 |", "| E2 |", "| E3 |", "| E4 |", "| E5 |"):
        assert row in DOC.read_text(), row
    assert "Controller local probe" in text               # E1/E2 source
    assert "Controller session during T002 development" in text  # E3/E5
    assert "T005 worker session" in text                  # E4 source
    # the transient auth failure is recorded as what it was, per session
    assert "authentication failure (transient)" in text
    # sample classes are marked: real capture, rebuilt shape, simulated
    assert "real captured CLI output" in text
    assert "rebuilt sample" in text and "trimmed" in text
    assert "simulated" in text
    # no real model run is claimed for this Goal
    assert "No real model run was executed" in text
    # the capture is evidence, not a maintained list
    assert "dated evidence, not a list ORX maintains" in text


def test_doc_separates_syntax_support_from_run_time_verification():
    text = flat(DOC)
    # Claude/GPT: usable but unlisted — the parameterized path
    assert "No Claude, GPT, or Gemini model appears as an enumerated row" \
        in text
    assert "bracket" in text and "parameterized" in text
    # the syntax-support vs run-verification boundary is stated
    assert "it does not prove that any specific model accepts any " \
        "specific parameter value" in text
    assert "echo" in text  # the per-run confirmation channel
    # grok prefix + version evolution, with the three-version instance
    for token in ("cursor-grok-4.5", "cursor-grok-4.6", "grok-4.7"):
        assert token in text, token
    assert "ambiguity diagnostic naming 4.5, 4.6, and 4.7" in text


def test_doc_states_the_strict_failure_boundary_and_diagnostics():
    text = flat(DOC)
    assert "No resolution path may substitute" in text
    for phrase in ("catalog: none available locally", "catalog: 0 models",
                   "supports only", "generation N", "another fixed version",
                   "provider_default", "requested_validated"):
        assert phrase in text, phrase
    # old-config compatibility and the migration table exist
    assert "Old configurations keep working" in text
    assert "bare string form is unchanged" in text
    # refresh/view are the programmable entries
    assert "orx agent probe cursor" in text
    assert "orx agent info" in text


# ---------------------------------------------------------------------------
# 5. No UI steps: Tune is background only; refresh is programmable only.


TUNE_STEPS = (
    "开启 Tune", "打开 Tune", "进入 Tune", "启动 Tune", "切换到 Tune",
    "enable Tune", "open Tune", "turn on Tune", "start Tune",
    "switch to Tune",
)


def test_tune_is_background_only_never_a_step():
    text = DOC.read_text()
    assert "Background only, not an operating procedure" in text
    for phrase in TUNE_STEPS:
        assert phrase not in text, phrase
    assert "Tune" not in README.read_text()
    assert "Tune" not in ROUTING.read_text()
    # no interactive-command instruction anywhere in the doc
    assert "/model " not in text


# ---------------------------------------------------------------------------
# 6. Links, README consistency, and unchanged routing policy.


def test_readme_and_routing_link_the_doc():
    doc_text = DOC.read_text()
    assert "](routing-strategy.md)" in doc_text       # doc -> policy
    assert "](../README.md" in doc_text               # doc -> config surface
    assert "docs/model-selection.md" in README.read_text()
    assert "model-selection.md" in ROUTING.read_text()


def test_readme_launch_description_matches_the_resolver():
    text = flat(README)
    # the stale single-form bracket claim is gone
    assert "`--model '<id>[effort=<mapped>]'`" not in text
    # the dual declaration form and the resolution outcomes are described
    assert "semantic `model` table" in text
    assert "enumerated variant slug" in text
    assert "bracket" in text
    assert "pre-launch diagnostic" in text


def test_routing_tiers_and_role_defaults_are_unchanged():
    text = ROUTING.read_text()
    # the tier table and the role defaults keep their adopted spelling
    for pinned in (
        "| `frontier` | latest flagships of codex / claude code |",
        "| `strong` | other vendors' pro models |",
        "| `economy` | flash / compose tiers |",
        "| Controller (`[controller]`) | strong + max (`orx-host`) |",
        "frontier + high (`codex-frontier`, `cursor-frontier`)",
        "economy + medium (`cursor-economy`)",
        "cursor-strong (strong+medium) → cursor-strong-high (strong+high)",
    ):
        assert pinned in text, pinned


def test_doc_declares_what_it_does_not_change():
    text = flat(DOC)
    assert "What this does not change" in text
    assert "routing-strategy.md" in text
    assert "No version release" in text


# ---------------------------------------------------------------------------
# 7. The IMPLEMENTATION_PLAN G008 record is complete and finished.


def g008_section() -> str:
    text = PLAN.read_text()
    assert "# G008" in text
    return text.split("# G008", 1)[1]


def test_implementation_plan_g008_is_finished_and_clean():
    section = g008_section()
    # every G008 stage is Done; no temporary in-progress markers remain
    assert "Not Started" not in section
    assert "In Progress" not in section
    for stage in ("### 阶段 1", "### 阶段 2", "### 阶段 3", "### 阶段 4",
                  "### 阶段 5"):
        assert stage in section, stage
    # stages 4 and 5 closed by this task's delivery
    assert "Done（T005，R8）" in section
    # the no-release boundary is recorded in the G008 record
    assert "不含版本发布" in section or "发布由用户决定" in section
    # the T005 round record exists and names the deliverables
    assert "T005" in section and "docs/model-selection.md" in section


def test_other_goals_history_is_preserved():
    text = PLAN.read_text()
    for header in ("# G004", "# G006", "# G007", "orx-pbv"):
        assert header in text, header
