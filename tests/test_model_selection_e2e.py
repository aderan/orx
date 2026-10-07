"""G008 T004: model selection end-to-end through PATH stand-ins.

Everything here goes through the REAL surfaces — the TOML profile loader,
the dispatch planner/worker/verifier paths, the probe/refresh machinery,
the typer CLI — against stand-in ``agent``/``codex`` binaries installed
first on PATH (shadowing any real binary on the host). The Cursor
stand-in serves the REAL captured CLI output
(tests/fixtures/model-catalog/) unless a test needs a synthetic world.

Covers the acceptance surface T003's unit-level file left implicit:

* backward compatibility: the existing bare slugs (claude-opus-5-5,
  gpt-6.1-sol, grok-4.7, composer-2.5, GLM account ids), bracket
  direct-writes, and the five-level effort vocabulary all launch exactly
  as before, through the real loader and a real launch;
* semantic tables are the incremental option: same loader, same launch,
  equivalent targets;
* a persisted catalog refresh (`orx agent probe`) changes what the next
  launch resolves against — in the SAME process and in a NEW process
  (a genuine interpreter subprocess reading only the persisted snapshot);
* historical (pre-catalog) snapshots, auth failures, timeouts, and failed
  refreshes have explicit diagnostics and never fabricate a fresh catalog;
* the three roles' actual argv and EffortOutcome are checked on a full
  dispatch run, with the resolution decision recorded per attempt;
* the CLI contract (exit codes 0/1/2, --json envelope) holds for the
  probe/info surfaces that own the catalog;
* host account-id profiles and the routing ladder are untouched by the
  declaration form.

No network, no completion, no interactive UI: every catalog is local
stand-in output.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orx import adapters, dispatch, model_catalog, probes
from orx.adapters import cursor as cursor_adapter
from orx.adapters.base import ModelResolutionError
from orx.cli import app
from orx.config import load_profiles
from orx.records import Driver, Effort, Harness, ModelClass, ResourceStatus

from conftest import ir_for, make_project, task_spec

FIXTURES = Path(__file__).parent / "fixtures" / "model-catalog"
REAL_LISTING = (FIXTURES / "cursor-list-models.txt").read_text()
REAL_HELP = (FIXTURES / "agent-help.txt").read_text()

runner = CliRunner()


def make_bin(directory: Path, name: str, script: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class StandIn:
    """A probe-complete stand-in Cursor CLI.

    Serves --version/--help/--list-models/status from files the test
    rewrites; launches log their argv and answer per ``role_script``
    (plan/worker/verdict envelopes by call order when ``roles`` is set).
    ``evolve()`` swaps the served world and drops the in-process
    launch-discovery cache. Flags: ``models_fail`` (channel exits 1),
    ``models_hang`` (channel sleeps past the probe timeout)."""

    def __init__(self, bindir: Path, tmp_path: Path, *,
                 models: str = REAL_LISTING, help_text: str = REAL_HELP,
                 status_text: str = "Logged in as user@example.com",
                 roles: tuple[str, ...] | None = None,
                 models_fail: bool = False, models_hang: bool = False):
        self.bindir = bindir
        self.models_file = tmp_path / "cli-list-models.txt"
        self.help_file = tmp_path / "cli-help.txt"
        self.status_file = tmp_path / "cli-status.txt"
        self.counter = tmp_path / "agent-launch-count"
        self.calls = bindir / "agent-calls.txt"
        self.models_file.write_text(models)
        self.help_file.write_text(help_text)
        self.status_file.write_text(status_text)
        self.roles = roles
        self.models_fail = models_fail
        self.models_hang = models_hang
        self._install()

    def _models_branch(self) -> str:
        if self.models_fail:
            return ('if [ "$1" = "--list-models" ]; then\n'
                    '  echo "agent: backend unavailable" >&2\n'
                    '  exit 1\n'
                    'fi\n')
        if self.models_hang:
            return ('if [ "$1" = "--list-models" ]; then\n'
                    '  sleep 2\n'
                    f'  cat "{self.models_file}"\n'
                    '  exit 0\n'
                    'fi\n')
        return ('if [ "$1" = "--list-models" ]; then\n'
                f'  cat "{self.models_file}"\n'
                '  exit 0\n'
                'fi\n')

    def _install(self) -> None:
        if self.roles is None:
            body = 'echo "{\\"result\\": \\"worker done\\"}"\nexit 0\n'
        else:
            cases = []
            for index, envelope in enumerate(self.roles):
                envelope_file = self.bindir.parent / f"agent-role-{index}.txt"
                envelope_file.write_text(envelope + "\n")
                cases.append(
                    f'  {index}) cat "{envelope_file}"; exit 0;;')
            body = (
                f'count="{self.counter}"\n'
                'n=$(cat "$count" 2>/dev/null || echo 0)\n'
                'echo $((n + 1)) > "$count"\n'
                'case "$n" in\n' + "\n".join(cases) + "\n"
                'esac\nexit 0\n'
            )
        script = f"""#!/bin/sh
if [ "$1" = "--version" ]; then echo "agent 2026.10.01"; exit 0; fi
if [ "$1" = "status" ]; then cat "{self.status_file}"; exit 0; fi
if [ "$1" = "--help" ]; then cat "{self.help_file}"; exit 0; fi
{self._models_branch()}n=$#
i=0
line=""
for a in "$@"; do
  i=$((i+1))
  if [ "$i" -lt "$n" ]; then line="$line $a"; fi
done
echo "$line" >> "{self.calls}"
{body}"""
        make_bin(self.bindir, "agent", script)

    def evolve(self, models: str | None = None, help_text: str | None = None,
               status_text: str | None = None, *,
               models_fail: bool | None = None,
               models_hang: bool | None = None) -> None:
        if models is not None:
            self.models_file.write_text(models)
        if help_text is not None:
            self.help_file.write_text(help_text)
        if status_text is not None:
            self.status_file.write_text(status_text)
        if models_fail is not None:
            self.models_fail = models_fail
        if models_hang is not None:
            self.models_hang = models_hang
        self._install()
        model_catalog.invalidate("cursor")

    def launched_models(self) -> list[str]:
        if not self.calls.exists():
            return []
        out = []
        for line in self.calls.read_text().splitlines():
            argv = line.split()
            if "--model" in argv:
                out.append(argv[argv.index("--model") + 1])
        return out


@pytest.fixture
def bindir(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")
    cursor_adapter.reset_caches()
    yield directory
    cursor_adapter.reset_caches()


class FakeResult:
    def __init__(self, stdout, exit_code=0):
        self.stdout, self.stderr = stdout, ""
        self.exit_code, self.timed_out = exit_code, False


def cursor_adapter_handle():
    return adapters.get_adapter("cursor")


def worker_launch(profile, tmp_path: Path):
    return cursor_adapter_handle().build_worker_launch(
        root=tmp_path, scratch=tmp_path / "scratch", profile=profile,
        prompt="x", timeout=5)


def model_arg(launch) -> str:
    argv = launch.argv
    return argv[argv.index("--model") + 1]


def write_profiles(tmp_path: Path, body: str) -> dict:
    """The REAL loader boundary: TOML on disk -> Profile objects."""
    path = tmp_path / "profiles.toml"
    path.write_text("schema_version = 1\n\n" + body)
    return load_profiles(path)


CLI_PROJECT_CONFIG = """\
schema_version = 1

[controller]
profile = "cursor-x"

[plan]
depth = "auto"

[plan.light]
profiles = ["cursor-x"]

[plan.standard]
profiles = ["cursor-x"]

[plan.deep]
profiles = ["cursor-x"]

[worker]
profiles = ["cursor-x"]

[verify]
profiles = ["cursor-x"]

[runtime]
max_parallel = 1
command_timeout_sec = 30
"""


# ---------------------------------------------------------------------------
# Backward compatibility: the existing profiles.toml population.


LEGACY_PROFILES = """
# The exact population the Goal names: bare slugs, an account id, and one
# bracket direct-write. None of these may require hand-editing.
[profiles.legacy-opus]
driver = "cli"
harness = "cursor"
model = "claude-opus-5-5"
class = "frontier"
effort = "high"
capabilities = ["coding"]

[profiles.legacy-gpt]
driver = "cli"
harness = "cursor"
model = "gpt-6.1-sol"
class = "frontier"
effort = "high"
capabilities = ["coding"]

[profiles.legacy-grok]
driver = "cli"
harness = "cursor"
model = "grok-4.7"
class = "strong"
effort = "xhigh"
capabilities = ["coding"]

[profiles.legacy-composer]
driver = "cli"
harness = "cursor"
model = "composer-2.5"
class = "economy"
effort = "high"
capabilities = ["coding"]

[profiles.legacy-account]
driver = "cli"
harness = "cursor"
model = "account:bigmodel-individual-coding-plan/GLM-5.3"
class = "strong"
effort = "high"
capabilities = ["coding"]

[profiles.legacy-bracket]
driver = "cli"
harness = "cursor"
model = "gpt-6.1-sol[effort=max]"
class = "frontier"
effort = "max"
capabilities = ["coding"]
"""


def test_legacy_population_launches_unchanged_against_real_capture(
        tmp_path, bindir):
    """Old bare slugs (claude-opus-5-5, gpt-6.1-sol, grok-4.7,
    composer-2.5, the GLM account id) and a bracket direct-write, loaded
    by the real loader against the real captured catalog: the exact argv
    the pre-G008 ladder produced, and the same EffortOutcome rules."""
    stand_in = StandIn(bindir, tmp_path)
    profiles = write_profiles(tmp_path, LEGACY_PROFILES)
    adapter = cursor_adapter_handle()
    assert adapter.probe().ok

    expectations = {
        # claude-opus-5-5 is not enumerated by the real CLI; help
        # evidences the bracket channel -> the ladder's bracket rung
        "legacy-opus": "claude-opus-5-5[effort=high]",
        "legacy-gpt": "gpt-6.1-sol[effort=high]",
        # grok-4.7 IS enumerated with baked variants -> variant slug
        "legacy-grok": "grok-4.7-xhigh",
        # composer-2.5 has no effort dimension -> bare enumerated slug
        "legacy-composer": "composer-2.5",
        # the account id is an opaque id: unlisted + bracket evidence
        # keeps the legacy bracket rung, identity untouched
        "legacy-account":
            "account:bigmodel-individual-coding-plan/GLM-5.3[effort=high]",
        # a user bracket passes verbatim, never rewritten
        "legacy-bracket": "gpt-6.1-sol[effort=max]",
    }
    for name, expected in expectations.items():
        launch = worker_launch(profiles[name], tmp_path)
        assert model_arg(launch) == expected, name
        # every launch keeps the requested identity (no substitution)
        assert "auto" != model_arg(launch)

    # EffortOutcome rules on the two confirmation channels.
    grok = worker_launch(profiles["legacy-grok"], tmp_path)
    outcome = adapter.effort_outcome(grok, FakeResult('{"result": "done"}'))
    assert (outcome.actual, outcome.source) == ("xhigh", "requested_validated")

    opus = worker_launch(profiles["legacy-opus"], tmp_path)
    bracketed = model_arg(opus)
    echoed = json.dumps({"result": "done", "model": bracketed})
    ok = adapter.effort_outcome(opus, FakeResult(echoed))
    assert (ok.actual, ok.source) == ("high", "requested_validated")
    silent = adapter.effort_outcome(opus, FakeResult('{"result": "done"}'))
    assert silent.source is None and silent.actual == "provider_default"

    composer = worker_launch(profiles["legacy-composer"], tmp_path)
    assert composer.planned_effort.source is None
    assert composer.planned_effort.actual == "provider_default"


def test_semantic_tables_are_the_incremental_option(tmp_path, bindir):
    """The same models declared as semantic tables load through the same
    real loader and reach the same targets as the legacy strings."""
    stand_in = StandIn(bindir, tmp_path)
    profiles = write_profiles(tmp_path, """
[profiles.sem-opus]
driver = "cli"
harness = "cursor"
model = { family = "claude-opus", version = "5.5" }
class = "frontier"
effort = "high"
capabilities = ["coding"]

[profiles.sem-gpt]
driver = "cli"
harness = "cursor"
model = { family = "gpt", version = "6.1-sol" }
class = "frontier"
effort = "high"
capabilities = ["coding"]

[profiles.sem-grok]
driver = "cli"
harness = "cursor"
model = { family = "grok", version = "4.7" }
class = "strong"
effort = "xhigh"
capabilities = ["coding"]

[profiles.sem-composer]
driver = "cli"
harness = "cursor"
model = { family = "composer-2.5", speed = "fast" }
class = "economy"
effort = "high"
capabilities = ["coding"]
""")
    # identity strings are semantic, never CLI slugs
    assert profiles["sem-opus"].model == "claude-opus@5.5"
    assert profiles["sem-grok"].model == "grok@4.7"
    assert profiles["sem-gpt"].model_spec is not None
    assert profiles["sem-opus"].model_spec.version == "5.5"

    adapter = cursor_adapter_handle()
    equivalents = {
        # the bracket uses the DECLARED spelling (5.5); the legacy string
        # spelled it 5-5 — same model, representation-only difference
        "sem-opus": "claude-opus-5.5[effort=high]",
        "sem-gpt": "gpt-6.1-sol[effort=high]",
        "sem-grok": "grok-4.7-xhigh",
        "sem-composer": "composer-2.5-fast",
    }
    for name, expected in equivalents.items():
        assert model_arg(worker_launch(profiles[name], tmp_path)) == expected
    # the semantic opus bracket and the legacy opus bracket name the same
    # model: identical bracket structure, version spelling aside
    semantic_opus = model_arg(worker_launch(profiles["sem-opus"], tmp_path))
    legacy_opus = model_arg(worker_launch(
        write_profiles(tmp_path, LEGACY_PROFILES)["legacy-opus"], tmp_path))
    assert semantic_opus.replace("5.5", "5-5") == legacy_opus


def test_five_effort_levels_launch_through_loader_and_standin(tmp_path,
                                                              bindir):
    """low/medium/high/xhigh/max all resolve to their variant slug and are
    confirmed by the catalog-variant rule (success alone suffices)."""
    listing = "Available models\n\n" + "\n".join(
        f"grok-4.7-{level} - Grok {level}" for level in
        ("low", "medium", "high", "xhigh", "max")) + "\n"
    stand_in = StandIn(bindir, tmp_path, models=listing)
    adapter = cursor_adapter_handle()
    for level in ("low", "medium", "high", "xhigh", "max"):
        profiles = write_profiles(tmp_path, f"""
[profiles.effort-{level}]
driver = "cli"
harness = "cursor"
model = {{ family = "grok", version = "4.7" }}
class = "frontier"
effort = "{level}"
capabilities = ["coding"]
""")
        profile = profiles[f"effort-{level}"]
        assert profile.effort.value == level  # vocabulary preserved
        launch = worker_launch(profile, tmp_path)
        assert model_arg(launch) == f"grok-4.7-{level}"
        assert launch.planned_effort.actual == level
        assert launch.planned_effort.source == "requested_validated"
        outcome = adapter.effort_outcome(
            launch, FakeResult('{"result": "done"}'))
        assert (outcome.actual, outcome.source) == (level,
                                                    "requested_validated")


# ---------------------------------------------------------------------------
# Full dispatch: three roles, actual argv, records.


FAKE_PLAN = json.dumps({
    "goal": "G001",
    "exploration": {"summary": "fake exploration",
                    "relevant_components": ["src/"], "unknowns": [],
                    "assumptions": [], "risks": []},
    "approach": {"summary": "fake approach", "decisions": []},
    "tasks": [
        {"id": "T001", "objective": "fake task", "dependencies": [],
         "scope": {"allowed": ["src/"]}, "acceptance": ["a1"],
         "verification": ["agent: the work is honest"],
         "routing": {"complexity": "low"}},
    ],
})


def test_dispatch_three_roles_argv_effort_and_records(tmp_path, bindir,
                                                      monkeypatch):
    """One full run — plan, work, verify — on the real captured catalog:
    each role's actual argv carries the resolved slug, each attempt's
    EffortOutcome is requested_validated, and the resolution decision is
    recorded per attempt next to the execution logs."""
    plan_envelope = json.dumps({"result": FAKE_PLAN})
    worker_envelope = json.dumps({"result": "worker done"})
    verdict_envelope = json.dumps({
        "result": "checked\nORX_VERDICT=pass\n"})
    stand_in = StandIn(bindir, tmp_path,
                       roles=(plan_envelope, worker_envelope, verdict_envelope))
    profiles_body = """
[profiles.cursor-x]
driver = "cli"
harness = "cursor"
model = { family = "grok", version = "4.7" }
class = "frontier"
effort = "high"
capabilities = ["coding"]
"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".orx").mkdir()
    (tmp_path / ".orx" / "config.toml").write_text(CLI_PROJECT_CONFIG)
    (tmp_path / ".orx" / "profiles.toml").write_text(
        "schema_version = 1\n" + profiles_body)
    project = dispatch.open_project()
    try:
        dispatch.create_goal(project, "goal text", ["a1"])
        planned = dispatch.plan_route(project)
        assert planned["mode"] == "completed"  # the plan is auto-submitted

        result = dispatch.run_slice(project)
        assert result["started"][0]["status"] == "verifying"
        assert result["started"][0]["actual_effort"] == "high"
        assert result["started"][0]["effort_source"] == "requested_validated"
        verified = dispatch.verify_dispatch(project)
        assert verified["launched"][0]["verdict"] == "pass"
        assert dispatch.status_data(project)["run"]["status"] == "done"

        # the actual argv of all three roles, in launch order
        assert stand_in.launched_models() == [
            "grok-4.7-high", "grok-4.7-high", "grok-4.7-high"]

        by_role = {}
        for attempt in project.store.attempts_all():
            by_role.setdefault(attempt.role, []).append(attempt)
        assert set(by_role) == {"planner", "worker", "verifier"}
        for role, rows in by_role.items():
            [attempt] = rows
            assert attempt.model == "grok@4.7", role  # identity contract
            assert attempt.actual_effort == "high", role
            assert attempt.effort_source == "requested_validated", role
            kinds = [row.kind for row in project.store.evidence_rows_for_task(
                attempt.revision_id, "T001")] if role != "planner" else []
            if role != "planner":
                assert "model_resolution" in kinds, role
                assert "execution" in kinds, role

        # the decision trail: one resolution.json per launched attempt
        resolution_files = sorted(
            (tmp_path / ".orx" / "runs").rglob("*.resolution.json"))
        assert len(resolution_files) == 3
        labels = {path.name.split(".")[0] for path in resolution_files}
        assert any(label.startswith("plan-") for label in labels)
        assert any(label.startswith("T001-") for label in labels)
        assert any(label.startswith("verify-T001-") for label in labels)
        for path in resolution_files:
            recorded = json.loads(path.read_text())
            assert recorded["spec"]["family"] == "grok"
            assert recorded["spec"]["version"] == "4.7"
            assert recorded["cli_slug"] == "grok-4.7-high"
            assert recorded["strategy"] == "catalog_variant"
    finally:
        project.close()


def test_dispatch_fixed_version_after_refresh_fails_without_launch(
        tmp_path, bindir, monkeypatch):
    """The e2e failure path: after a persisted refresh removes 4.7 in
    favor of 4.8, the pinned profile fails the worker with the
    model_resolution error kind and the CLI is never started."""
    v1 = "Available models\n\ngrok-4.7-high - Grok\nauto - Auto\n"
    v2 = "Available models\n\ngrok-4.8-high - Grok\nauto - Auto\n"
    stand_in = StandIn(bindir, tmp_path, models=v1)
    assert probes.capability_snapshot("cursor")["model_catalog"][
        "generation"] == 1
    stand_in.evolve(v2)
    assert probes.capability_snapshot("cursor")["model_catalog"][
        "generation"] == 2

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".orx").mkdir()
    (tmp_path / ".orx" / "config.toml").write_text(CLI_PROJECT_CONFIG)
    (tmp_path / ".orx" / "profiles.toml").write_text(
        'schema_version = 1\n\n[profiles.cursor-x]\ndriver = "cli"\n'
        'harness = "cursor"\nmodel = { family = "grok", version = "4.7" }\n'
        'class = "frontier"\neffort = "high"\ncapabilities = ["coding"]\n')
    project = dispatch.open_project()
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["a1"])]))
        result = dispatch.run_slice(project)
        failed = result["failed"][0]
        assert failed["error_kind"] == "model_resolution"
        assert "family=grok@4.7" in failed["reason"]
        assert "grok-4.8" in failed["reason"]
        assert "generation 2" in failed["reason"]  # the refreshed catalog
        assert "orx agent probe cursor" in failed["reason"]
        assert stand_in.launched_models() == []  # never started
        attempt = next(a for a in project.store.attempts_all()
                       if a.role == "worker")
        assert attempt.result == "failed"
        assert attempt.model == "grok@4.7"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Refresh semantics: same process, new process, precedence.


NEW_PROCESS_LAUNCH = textwrap.dedent("""
    import json, sys
    from orx import adapters
    from orx.config import Profile
    from orx.models import ModelSpec
    from orx.records import Driver, Effort, Harness, ModelClass
    spec = ModelSpec(family="grok", effort="high")
    profile = Profile(name="p", driver=Driver.CLI, harness=Harness.CURSOR,
                      model=spec.identity, model_class=ModelClass.FRONTIER,
                      effort=Effort.HIGH, capabilities=("coding",),
                      model_spec=spec)
    launch = adapters.get_adapter("cursor").build_worker_launch(
        root=sys.argv[1], scratch=sys.argv[2], profile=profile,
        prompt="x", timeout=5)
    argv = launch.argv
    print(json.dumps({
        "model": argv[argv.index("--model") + 1],
        "strategy": launch.model_resolution["strategy"],
        "generation": launch.model_resolution["catalog_generation"],
    }))
""")


def test_refreshed_catalog_governs_same_and_new_process(tmp_path, bindir):
    """A persisted refresh (the `orx agent probe` surface) changes what
    the next launch resolves against — in this process, and in a GENUINE
    new process (a fresh interpreter that can only see the persisted
    snapshot). A stale process cache never serves the old world after a
    refresh, and local CLI drift never overrides the persisted catalog."""
    v1 = ("Available models\n\n"
          "cursor-grok-4.6-high - Grok 4.6\nauto - Auto\n")
    v2 = ("Available models\n\n"
          "grok-4.8-low - Grok 4.8\n grok-4.8-high - Grok 4.8 High\n"
          "auto - Auto\n")
    stand_in = StandIn(bindir, tmp_path, models=v1)

    # Before any probe: in-process discovery (generation 0).
    profiles = write_profiles(tmp_path, """
[profiles.float-grok]
driver = "cli"
harness = "cursor"
model = { family = "grok" }
class = "frontier"
effort = "high"
capabilities = ["coding"]
""")
    launch = worker_launch(profiles["float-grok"], tmp_path)
    assert model_arg(launch) == "cursor-grok-4.6-high"
    assert launch.model_resolution["catalog_generation"] == 0

    # Refresh #1 persists generation 1 (same world).
    snap = probes.capability_snapshot("cursor")
    assert snap["model_catalog"]["generation"] == 1
    launch = worker_launch(profiles["float-grok"], tmp_path)
    assert launch.model_resolution["catalog_generation"] == 1

    # The CLI evolves; refresh #2 persists generation 2 (new world).
    stand_in.evolve(v2)
    snap = probes.capability_snapshot("cursor")
    assert snap["model_catalog"]["generation"] == 2

    # Same process: the next launch follows the refreshed catalog.
    launch = worker_launch(profiles["float-grok"], tmp_path)
    assert model_arg(launch) == "grok-4.8-high"
    assert launch.model_resolution["catalog_generation"] == 2

    # Local drift does not override the persisted snapshot: the CLI's
    # listing moves again WITHOUT a refresh; launches keep generation 2.
    stand_in.evolve("Available models\n\nkimi-k3-high - Kimi\nauto - Auto\n")
    launch = worker_launch(profiles["float-grok"], tmp_path)
    assert model_arg(launch) == "grok-4.8-high"
    assert launch.model_resolution["catalog_generation"] == 2

    # New process: a fresh interpreter sees only the persisted snapshot.
    proc = subprocess.run(
        [sys.executable, "-c", NEW_PROCESS_LAUNCH, str(tmp_path),
         str(tmp_path / "np-scratch")],
        capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    reported = json.loads(proc.stdout.strip().splitlines()[-1])
    assert reported == {"model": "grok-4.8-high",
                        "strategy": "catalog_variant",
                        "generation": 2}


def test_failed_refresh_never_fakes_a_fresh_catalog(tmp_path, bindir):
    """A refresh whose models channel breaks: the snapshot records the
    failure and carries NO catalog; a semantic launch then reports 'no
    catalog available locally' (never a fabricated fresh empty catalog),
    and the legacy id keeps its bare passthrough."""
    stand_in = StandIn(bindir, tmp_path)
    good = probes.capability_snapshot("cursor")
    assert good["model_catalog"] is not None

    stand_in.evolve(models_fail=True)
    refreshed = probes.capability_snapshot("cursor")
    assert refreshed["models_discoverable"] is False
    assert refreshed["model_catalog"] is None
    assert refreshed["catalog_error"] == "models probe exited 1"
    persisted = probes.load_snapshot("cursor")
    assert persisted["model_catalog"] is None
    assert persisted["catalog_error"] == "models probe exited 1"
    assert model_catalog.cached_catalog("cursor") is None

    # A no-bracket help world makes the missing catalog the diagnostic
    # (with bracket evidence the family would ride the parameter channel).
    stand_in.evolve(help_text="agent [options]\n  --model <model>\n")
    profiles = write_profiles(tmp_path, """
[profiles.sem]
driver = "cli"
harness = "cursor"
model = { family = "grok", version = "4.7" }
class = "frontier"
effort = "high"
capabilities = ["coding"]

[profiles.legacy]
driver = "cli"
harness = "cursor"
model = "grok-4.7"
class = "frontier"
effort = "high"
capabilities = ["coding"]
""")
    with pytest.raises(ModelResolutionError) as excinfo:
        worker_launch(profiles["sem"], tmp_path)
    message = str(excinfo.value)
    assert "catalog: none available locally" in message
    assert "0 models" not in message  # no fabricated fresh empty catalog
    assert "orx agent probe cursor" in message

    launch = worker_launch(profiles["legacy"], tmp_path)
    assert model_arg(launch) == "grok-4.7"
    assert launch.model_resolution["strategy"] == "bare_unlisted"


def test_auth_failure_and_timeout_are_explicit_not_catalog_fabrication(
        tmp_path, bindir, monkeypatch):
    """Auth is orthogonal to the catalog; a probe timeout records the
    timeout. Neither fabricates nor hides a catalog."""
    stand_in = StandIn(bindir, tmp_path,
                       status_text="Not logged in")
    snap = probes.capability_snapshot("cursor")
    assert snap["auth"] == "not_logged_in"
    assert snap["models_discoverable"] is True  # the catalog is real
    assert snap["model_catalog"] is not None
    assert snap["catalog_error"] is None

    # timeout on the models channel: recorded, no catalog
    monkeypatch.setattr(probes, "MODELS_PROBE_TIMEOUT", 0.4)
    stand_in.evolve(models_hang=True)
    snap = probes.capability_snapshot("cursor")
    assert snap["models_discoverable"] is False
    assert snap["model_catalog"] is None
    assert "timed out" in snap["catalog_error"]


def test_historical_and_stale_snapshots_are_explicit(tmp_path, bindir):
    """A pre-catalog snapshot stays readable and yields no catalog; a
    STALE persisted catalog (generation 1, old world) keeps governing
    launches over a CLI that already moved — the diagnostic names the
    persisted generation and the refresh command."""
    # A pre-G008 snapshot on disk: readable, no catalog keys.
    legacy_snapshot = {
        "harness": "cursor", "binary": "/old/agent", "version": "agent old",
        "probed_at": "2026-01-01T00:00:00+00:00",
        "features": {"headless": True, "json_output": True,
                     "model_selection": True, "effort_selection": True,
                     "resume": False},
        "auth": "logged_in", "models_discoverable": True,
    }
    path = probes.snapshot_path("cursor")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy_snapshot))
    assert probes.load_snapshot("cursor") == legacy_snapshot
    model_catalog.invalidate("cursor")
    stand_in = StandIn(bindir, tmp_path)  # real capture on the CLI now
    profiles = write_profiles(tmp_path, """
[profiles.sem]
driver = "cli"
harness = "cursor"
model = { family = "grok", version = "4.7" }
class = "frontier"
effort = "high"
capabilities = ["coding"]
""")
    launch = worker_launch(profiles["sem"], tmp_path)
    assert model_arg(launch) == "grok-4.7-high"
    assert launch.model_resolution["catalog_generation"] == 0  # discovered

    # A STALE persisted catalog wins over the newer local CLI.
    catalog = model_catalog.build_catalog(
        harness="cursor", source="agent --list-models (snapshot)",
        models=model_catalog.parse_cursor_list_models(
            "cursor-grok-4.6-high - Old Grok\n"),
        generation=1, generated_at="2026-01-02T00:00:00+00:00")
    path.write_text(json.dumps({
        "harness": "cursor", "probed_at": catalog.generated_at,
        "model_catalog": catalog.to_dict(),
    }))
    model_catalog.invalidate("cursor")
    cursor_adapter.reset_caches()
    # the snapshot's old world governs: 4.7 is gone, 4.6 is offered
    with pytest.raises(ModelResolutionError) as excinfo:
        worker_launch(profiles["sem"], tmp_path)
    message = str(excinfo.value)
    assert "family=grok@4.7" in message
    assert "generation 1" in message  # names the persisted generation
    assert "orx agent probe cursor" in message
    # a floating policy against the same stale snapshot resolves the old
    # world — the user's refresh is the lever, not silent drift
    floating = write_profiles(tmp_path, """
[profiles.float]
driver = "cli"
harness = "cursor"
model = { family = "grok" }
class = "frontier"
effort = "high"
capabilities = ["coding"]
""")
    launch = worker_launch(floating["float"], tmp_path)
    assert model_arg(launch) == "cursor-grok-4.6-high"
    assert launch.model_resolution["catalog_generation"] == 1


# ---------------------------------------------------------------------------
# CLI contract for the surfaces that own the catalog.


def test_cli_probe_and_info_catalog_contract(tmp_path, bindir, monkeypatch):
    """`orx agent probe` / `orx agent info`: --json envelopes, exit codes
    0/1/2, generation advancing across refreshes, and honest human
    rendering of discovery failure."""
    stand_in = StandIn(bindir, tmp_path)

    result = runner.invoke(app, ["agent", "probe", "--json", "cursor"])
    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    assert body["ok"] is True
    assert body["snapshot"]["model_catalog"]["generation"] == 1
    assert body["snapshot"]["catalog_error"] is None

    stand_in.evolve("Available models\n\ngrok-4.8-high - Grok\nauto - Auto\n")
    result = runner.invoke(app, ["agent", "probe", "--json", "cursor"])
    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    assert body["snapshot"]["model_catalog"]["generation"] == 2

    info = runner.invoke(app, ["agent", "info", "--json", "cursor"])
    assert info.exit_code == 0, info.stdout
    shown = json.loads(info.stdout)
    assert shown["ok"] is True
    assert shown["snapshot"]["model_catalog"]["generation"] == 2
    human = runner.invoke(app, ["agent", "info", "cursor"])
    assert human.exit_code == 0
    assert "generation 2" in human.stdout

    # usage and host-only exits keep the 0/1/2 contract
    assert runner.invoke(app, ["agent", "probe"]).exit_code == 2
    assert runner.invoke(app, ["agent", "probe", "zcode"]).exit_code == 1
    assert runner.invoke(app, ["agent", "info", "nope"]).exit_code == 1

    # a failed refresh renders as failure, never as a fake success
    stand_in.evolve(models_fail=True)
    failed = runner.invoke(app, ["agent", "probe", "--json", "cursor"])
    assert failed.exit_code == 0, failed.stdout
    body = json.loads(failed.stdout)
    assert body["snapshot"]["model_catalog"] is None
    assert body["snapshot"]["catalog_error"] == "models probe exited 1"
    human = runner.invoke(app, ["agent", "info", "cursor"])
    assert "discovery failed" in human.stdout


# ---------------------------------------------------------------------------
# Host account ids and routing are untouched by the declaration form.


def test_host_account_id_profiles_unchanged(tmp_path, monkeypatch):
    """The zcode preset's GLM account ids load verbatim through the real
    loader (bare strings, no spec); a semantic host table renders its
    identity string into the host execution spec — the host contract is
    independent of cursor resolution."""
    from orx.presets import preset_dir

    profiles = load_profiles(preset_dir("zcode") / "profiles.toml")
    assert profiles
    account_ids = {p.model for p in profiles.values()}
    assert account_ids == {
        "account:bigmodel-individual-coding-plan/GLM-5.3",
        "account:bigmodel-individual-coding-plan/GLM-5.3-Flash",
    }
    for profile in profiles.values():
        assert profile.model_spec is None  # bare strings, never rewritten
        assert profile.effort.value in ("high", "max")

    # Host execution specs: the bare account id verbatim, the semantic
    # table's identity string — both without any adapter resolution.
    config = """\
schema_version = 1

[controller]
profile = "host-plan"

[plan]
depth = "auto"

[plan.light]
profiles = ["host-plan"]

[plan.standard]
profiles = ["host-plan"]

[plan.deep]
profiles = ["host-plan"]

[worker]
profiles = ["host-work"]

[verify]
profiles = ["host-plan"]

[runtime]
max_parallel = 1
command_timeout_sec = 30
"""
    profiles_body = """
[profiles.host-plan]
driver = "host"
harness = "zcode"
model = "account:bigmodel-individual-coding-plan/GLM-5.3"
class = "strong"
effort = "high"
capabilities = ["coding"]

[profiles.host-work]
driver = "host"
harness = "zcode"
model = { family = "grok", version = "4.7" }
class = "strong"
effort = "medium"
capabilities = ["coding"]
"""
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path, config_toml=config,
                           profiles_toml="schema_version = 1\n" + profiles_body)
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        planned = dispatch.plan_route(project)
        assert planned["mode"] == "host_required"
        execution = planned["assignment"]["execution"]
        assert execution["model"] == \
            "account:bigmodel-individual-coding-plan/GLM-5.3"
        assert planned["assignment"]["profile"] == "host-plan"

        parked = dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["a1"])]))
        # host worker parks for the host driver: execution spec carries
        # the semantic identity, no CLI launch ever happens
        assert parked["tasks"] == 1
        run = dispatch.run_slice(project)
        assert run["started"] == []
        assert [h["task"] for h in run["host_required"]] == ["T001"]
        assert run["host_required"][0]["execution"]["model"] == "grok@4.7"
    finally:
        project.close()


def test_routing_ladder_identical_between_declaration_forms(tmp_path,
                                                            monkeypatch):
    """Equivalent configs (bare slug vs semantic table for the same
    profile slots) route identically: candidate order, selection,
    fallback, class gating, and capability filtering see the Profile
    surface, which the declaration form never changes."""
    from orx import routing
    from orx.records import PlanDepth, Role

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
    worker_a = ('driver = "cli"\nharness = "cursor"\n'
                'class = "strong"\neffort = "high"\n'
                'capabilities = ["coding"]\n')

    def profiles_toml(form: str) -> str:
        def model(name):
            if form == "bare":
                return f'model = "{name}"\n'
            family, _, version = name.rpartition("-4.7")
            return (f'model = {{ family = "{family or name}", '
                    f'version = "{version or "4.7"}" }}\n')
        return f"""schema_version = 1

[profiles.plan-a]
{model("grok-4.7")}{worker_a}
[profiles.frontier-a]
{model("claude-x-4.7")}driver = "cli"
harness = "cursor"
class = "frontier"
effort = "high"
capabilities = ["coding"]

[profiles.work-a]
{model("grok-4.7")}{worker_a}
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

    def route(project, **kwargs):
        return routing.route(project.store, project.config, project.profiles,
                             routing.RouteRequest(**kwargs))

    outcomes = {}
    for form in ("bare", "semantic"):
        directory = tmp_path / form
        directory.mkdir()
        monkeypatch.chdir(directory)
        project = make_project(directory, config_toml=config,
                               profiles_toml=profiles_toml(form))
        try:
            snap = {
                "worker": route(project, role=Role.WORKER),
                "planner_standard": route(project, role=Role.PLANNER,
                                          depth=PlanDepth.STANDARD),
                "planner_deep": route(project, role=Role.PLANNER,
                                      depth=PlanDepth.DEEP),
                "verifier_vision": route(project, role=Role.VERIFIER,
                                         required_capabilities=("vision",)),
            }
            # fallback order is part of the ladder, not of the form
            project.store.resource_set("work-a", ResourceStatus.UNAVAILABLE)
            snap["worker_fallback"] = route(project, role=Role.WORKER)
            project.store.resource_set("frontier-a",
                                       ResourceStatus.UNAVAILABLE)
            snap["deep_downgrade_blocked"] = route(
                project, role=Role.PLANNER, depth=PlanDepth.DEEP)
            outcomes[form] = {
                name: {
                    "selected": result.selected,
                    "reason": result.reason,
                    "fallback_used": result.fallback_used,
                    "downgrade_blocked": result.downgrade_blocked,
                    "candidates": [candidate.to_dict()
                                   for candidate in result.candidates],
                }
                for name, result in snap.items()
            }
        finally:
            project.close()

    assert outcomes["bare"] == outcomes["semantic"]
    # and the ladder itself is the documented one (not vacuously equal)
    assert outcomes["semantic"]["worker"]["selected"] == "work-a"
    assert outcomes["semantic"]["worker_fallback"]["selected"] == "work-b"
    assert outcomes["semantic"]["planner_deep"]["selected"] == "frontier-a"
    assert outcomes["semantic"]["verifier_vision"]["selected"] == \
        "verify-vision"
    assert outcomes["semantic"]["deep_downgrade_blocked"]["downgrade_blocked"]
