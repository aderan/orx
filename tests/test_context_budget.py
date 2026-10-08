"""G009 T011: context cost governance — prompt compression, budget gates,
and the diagnostic-stall bound.

Three seams, all deterministic (zero paid launches):

1. Worker prompt compression — every prescribed check is listed ONCE (the
   old prompt duplicated each entry: a plain list plus the preflight row),
   and host subagent dispatches carry a brief contract reference instead of
   the four-clause delivery contract their agent definition
   (agents/orx-worker.md) already carries verbatim. CLI/external/self
   prompts keep the full inline contract — no agent definition exists
   there. Nothing verification-carrying is removed.
2. Budget policy — configurable soft warn + hard stop on recorded usage
   (per task and per run), calibrated from the 2026-10-07 incident samples
   and the T009 microtask baseline, with unknown usage NEVER zero-filled.
3. runtime.check_timeout_sec — the check/gate command bound that turns a
   hanging diagnostic (a130: ~42 min stuck in one test stall) into a
   bounded red result instead of a silent 30-minute wait.

The parser linearity regression (model_catalog bracket scan) lives here
too: the O(n^2) fix is already in tree; this pins it.
"""

from __future__ import annotations

import time

import pytest

from orx import dispatch, routing
from orx.config import (
    BUILTIN_DEFAULTS,
    ConfigError,
    load_effective,
    parse_config_value,
)
from orx.adapters.base import HARNESS_CACHE_SEMANTICS
from orx.records import Role
from orx.state import TaskRow

from conftest import (
    HOST_CONFIG_TOML,
    HOST_PROFILES_TOML,
    active_task,
    ir_for,
    make_project,
    task_spec,
)

# A worker ladder with one subagent-mode host profile (zcode: the agent
# definition is injected by the harness) and one self-mode host profile.
SUBAGENT_PROFILES_TOML = HOST_PROFILES_TOML + """

[profiles.host-subworker]
driver = "host"
harness = "zcode"
model = "m-sub"
class = "strong"
effort = "medium"
capabilities = ["coding"]
host_mode = "subagent"
agent_ref = "orx-worker"
"""


def _task_row(task_id="T001", verification=("true",)):
    return TaskRow(
        row_id=1, revision_id=1, task_id=task_id,
        objective="compress the prompt", scope={"allowed": ["src/"]},
        acceptance=["marker exists"], verification=list(verification),
        routing={}, status="runnable", failure_reason=None,
        created_at="2026-10-08T00:00:00+00:00",
        updated_at="2026-10-08T00:00:00+00:00", preread=["README.md"],
    )


def _seed_attempt_with_usage(project, task_id, *, profile="host-worker",
                             input_tokens=None, output_tokens=None,
                             cached_input_tokens=None):
    """One closed worker attempt (+ optional usage row) on the active
    revision's task — the recorded history a budget decision walks. The
    attempt's harness comes from the profile so the normalization follows
    the profile's real cache semantics."""
    store = project.store
    goal = store.goal_active()
    run = store.run_for_goal(goal.id)
    revision = store.revision_active(run.id)
    harness = project.profiles[profile].harness.value
    attempt = store.attempt_create(
        revision_row_id=revision.id,
        role="worker",
        profile=profile,
        driver="host",
        harness=harness,
        model_id="m",
        requested_effort="medium",
        task_id=task_id,
        started=True,
        run_id=run.id,
    )
    store.attempt_update(attempt.id, ended_at="2026-10-08T00:00:00+00:00",
                         result="failed")
    if input_tokens is not None or output_tokens is not None:
        store.usage_add(
            attempt_id=attempt.id, profile=profile, run_id=run.id,
            task_id=task_id,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            source="native_cli", accuracy="exact",
        )
    return attempt


# ---------------------------------------------------------------------------
# 1. Worker prompt compression


def test_worker_prompt_lists_each_prescribed_check_once(project, goal):
    task = _task_row(verification=("true", "agent: review the diff for scope"))
    prompt = dispatch.worker_prompt(goal, task, check_budget=3)

    # The OLD prompt carried every entry twice (plain list + preflight
    # row). The compressed prompt lists each entry exactly once — the
    # preflight row IS the check listing now. (The preflight reason may
    # name the command token again inside its own line; the ENTRY marker
    # is what must appear once.)
    assert prompt.count("[preflight:ok] command 'true'") == 1
    assert prompt.count("agent: review the diff for scope") == 1
    # The header tests pin elsewhere stays stable.
    assert "How your work will be checked" in prompt
    assert "[preflight:ok] command 'true'" in prompt


def test_brief_contract_is_shorter_and_keeps_task_facts(project, goal):
    task = _task_row()
    full = dispatch.worker_prompt(goal, task, check_budget=3)
    brief = dispatch.worker_prompt(goal, task, check_budget=3,
                                   contract="brief")

    # The four-clause contract is ~2.4 KB of scaffolding the agent
    # definition already carries; the brief form must save the bulk of it.
    assert len(brief) < len(full) - 1000
    # Task-bound facts stay explicit: the check budget, the task commands,
    # and the gate names the clauses carry.
    assert "orx task check T001" in brief
    assert "3 check round(s)" in brief
    assert "START GATE" in brief and "BLOCKED EXIT" in brief
    assert "DELIVERY GATE" in brief
    assert "independent verifier" in brief
    # The full clause-1 prose is the marker of the inline contract.
    assert "1. START GATE: before any implementation work" in full
    assert "1. START GATE: before any implementation work" not in brief


def test_full_contract_clauses_pinned_for_cli_channels(project, goal):
    """CLI/external/self workers have no agent definition: their prompt
    keeps the full inline contract, byte-pinned by its clause headings, and
    the CLI machine-delivery clause rides message-channel harnesses only."""
    task = _task_row()
    prompt = dispatch.worker_prompt(
        goal, task, check_budget=3, delivery_channel="message")
    assert "1. START GATE" in prompt
    assert "2. BLOCKED EXIT" in prompt
    assert "3. CHECK-FIX LOOP" in prompt
    assert "4. DELIVERY GATE" in prompt
    assert "FINAL DELIVERY MESSAGE" in prompt
    # ...and the exit-channel form keeps clauses 1-4 without clause 5.
    exit_prompt = dispatch.worker_prompt(goal, task, check_budget=3)
    assert "FINAL DELIVERY MESSAGE" not in exit_prompt


def test_park_contract_mode_follows_host_mode(tmp_path, monkeypatch):
    """A host worker whose profile declares host_mode = 'subagent' runs
    under the agents/orx-worker.md definition — the harness injects it, and
    that definition carries the four-clause contract verbatim — so the
    dispatch prompt carries the brief reference. A self-mode host park (the
    controller executes it itself, no definition provably loaded) keeps the
    full inline contract."""
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]',
        'profiles = ["host-subworker", "host-worker"]',
    )
    project = make_project(tmp_path, config_toml=config,
                           profiles_toml=SUBAGENT_PROFILES_TOML)
    try:
        goal = dispatch.create_goal(
            project, objective="brief park", acceptance=["done"],
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["done"], verification=["true"]),
            task_spec("T002", acceptance=["done"], verification=["true"]),
        ]))
        run = project.store.run_for_goal(goal.id)
        revision = project.store.revision_active(run.id)

        def park_pinned(task_id, profile_name):
            task = project.store.task_get(revision.id, task_id)
            request = routing.RouteRequest(
                role=Role.WORKER, pinned_profile=profile_name)
            result = routing.route(project.store, project.config,
                                   project.profiles, request)
            assert result.ok, result.error
            return dispatch._park_task(
                project, goal, run, revision.id, task, result, request,
                result.profile, "host")

        sub = park_pinned("T001", "host-subworker")
        assert "3 check round(s)" in sub["prompt"]
        assert "1. START GATE: before any implementation work" not in sub["prompt"]

        self_mode = park_pinned("T002", "host-worker")
        assert "1. START GATE: before any implementation work" in self_mode["prompt"]
    finally:
        project.close()


# ---------------------------------------------------------------------------
# 2. Budget configuration


def test_budget_config_defaults_are_the_calibrated_samples():
    """The builtin defaults ARE the recorded samples (T009 microtask and
    the 2026-10-07 incident), each cited where they are set — never an
    invented price or expectation."""
    assert BUILTIN_DEFAULTS["budget.soft_warn_task_processing_tokens"] == 8_000_000
    assert BUILTIN_DEFAULTS["budget.hard_stop_task_processing_tokens"] == 50_000_000
    assert BUILTIN_DEFAULTS["budget.hard_stop_task_attempts"] == 8
    assert BUILTIN_DEFAULTS["budget.hard_stop_run_processing_tokens"] == 150_000_000
    assert BUILTIN_DEFAULTS["runtime.check_timeout_sec"] == 600


def test_budget_config_parses_and_validates(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "schema_version = 1\n"
        '\n[controller]\nprofile = "host-planner"\n'
        "\n[worker]\nprofiles = []\n"
        "\n[budget]\n"
        "soft_warn_task_processing_tokens = 5\n"
        "hard_stop_task_processing_tokens = 9\n"
        "hard_stop_task_attempts = 4\n"
        "hard_stop_run_processing_tokens = 99\n"
        "\n[runtime]\ncheck_timeout_sec = 7\n"
    )
    profiles = tmp_path / "profiles.toml"
    profiles.write_text(
        'schema_version = 1\n[profiles.host-planner]\ndriver = "host"\n'
        'harness = "zcode"\nmodel = "m"\nclass = "strong"\n'
        'effort = "medium"\ncapabilities = ["coding"]\n'
    )
    effective = load_effective(path, profiles)
    assert effective.config.soft_warn_task_processing_tokens == 5
    assert effective.config.hard_stop_task_processing_tokens == 9
    assert effective.config.hard_stop_task_attempts == 4
    assert effective.config.hard_stop_run_processing_tokens == 99
    assert effective.config.check_timeout_sec == 7
    assert effective.origins["budget.hard_stop_task_attempts"] == "project"

    bad = tmp_path / "bad.toml"
    bad.write_text(path.read_text().replace(
        "hard_stop_task_attempts = 4", "hard_stop_task_attempts = 0"))
    with pytest.raises(ConfigError) as exc:
        load_effective(bad, profiles)
    assert "hard_stop_task_attempts" in str(exc.value)

    with pytest.raises(ConfigError):
        parse_config_value("runtime.check_timeout_sec", "0")
    assert parse_config_value("budget.soft_warn_task_processing_tokens", "6") == 6
    assert parse_config_value("runtime.check_timeout_sec", "8") == 8


def test_budget_config_env_override(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        "schema_version = 1\n"
        '\n[controller]\nprofile = "host-planner"\n'
        "\n[worker]\nprofiles = []\n"
        "\n[budget]\nhard_stop_task_attempts = 4\n"
    )
    profiles = tmp_path / "profiles.toml"
    profiles.write_text(
        'schema_version = 1\n[profiles.host-planner]\ndriver = "host"\n'
        'harness = "zcode"\nmodel = "m"\nclass = "strong"\n'
        'effort = "medium"\ncapabilities = ["coding"]\n'
    )
    monkeypatch.setenv("ORX_BUDGET_HARD_STOP_TASK_ATTEMPTS", "12")
    effective = load_effective(path, profiles)
    assert effective.config.hard_stop_task_attempts == 12
    assert effective.origins["budget.hard_stop_task_attempts"] == "env"


# ---------------------------------------------------------------------------
# 3. Budget state: normalized sums, unknown usage never zero


def test_task_budget_state_normalizes_and_never_zero_fills(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cursor_profiles = HOST_PROFILES_TOML + (
        '\n[profiles.cursor-worker]\ndriver = "host"\nharness = "cursor"\n'
        'model = "m"\nclass = "strong"\neffort = "medium"\n'
        'capabilities = ["coding"]\n'
    )
    project = make_project(tmp_path, profiles_toml=cursor_profiles)
    try:
        goal = dispatch.create_goal(
            project, objective="budget state", acceptance=["done"],
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["done"], verification=["true"]),
        ]))

        # zcode semantics (cache_subset): processing = input + output.
        _seed_attempt_with_usage(
            project, "T001", input_tokens=100, output_tokens=20,
            cached_input_tokens=80)
        # cursor semantics (cache_separate): processing = input + cached +
        # output — the incident's cache-read >> input shape.
        _seed_attempt_with_usage(
            project, "T001", profile="cursor-worker",
            input_tokens=50, output_tokens=10, cached_input_tokens=70)
        # An attempt with NO usage observation: counted, never zero-filled.
        _seed_attempt_with_usage(project, "T001")

        state = dispatch.task_budget_state(project, "T001")

        assert HARNESS_CACHE_SEMANTICS["zcode"] == "cache_subset"
        assert HARNESS_CACHE_SEMANTICS["cursor"] == "cache_separate"
        assert state["attempts"] == 3
        assert state["attempts_with_usage"] == 2
        assert state["attempts_unassessed"] == 1
        assert state["processing_tokens_recorded"] == (100 + 20) + (50 + 70 + 10)
        assert "restore" in state
    finally:
        project.close()


def test_run_slice_hard_stops_over_processing_budget(tmp_path, monkeypatch):
    """The bounded hard stop: no launch, an explained routing error, a task
    event, and the task stays runnable — raising the budget re-dispatches
    (the restore point; nothing is deleted)."""
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        "[runtime]",
        "[budget]\nhard_stop_task_processing_tokens = 1000\n\n[runtime]",
    )
    project = make_project(tmp_path, config_toml=config)
    try:
        goal = dispatch.create_goal(
            project, objective="hard stop", acceptance=["done"],
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["done"], verification=["true"]),
        ]))
        revision_id = project.store.revision_active(
            project.store.run_for_goal(goal.id).id).id
        _seed_attempt_with_usage(
            project, "T001", input_tokens=1200, output_tokens=10,
            cached_input_tokens=None)

        out = dispatch.run_slice(project)

        assert out["host_required"] == [] and out["started"] == []
        [error] = out["routing_errors"]
        assert error["task"] == "T001"
        assert error["budget"]["hard_stop"] is True
        assert error["budget"]["processing_tokens_recorded"] == 1210
        assert active_task(project, "T001").status == "runnable"
        events = project.store.task_events(revision_id, "T001")
        assert any(e.event == "dispatch_budget_stopped" for e in events)

        # Restore point: raise the limit, reopen, the same task dispatches.
        project.close()
        (tmp_path / ".orx" / "config.toml").write_text(
            HOST_CONFIG_TOML.replace(
                "[runtime]",
                "[budget]\nhard_stop_task_processing_tokens = 5000\n\n[runtime]",
            ))
        project2 = dispatch.open_project()
        try:
            out2 = dispatch.run_slice(project2)
            assert [p["task"] for p in out2["host_required"]] == ["T001"]
        finally:
            project2.close()
    finally:
        try:
            project.close()
        except Exception:
            pass


def test_attempt_bound_stops_counting_unknown_usage_as_free(tmp_path, monkeypatch):
    """Attempts are countable even when their usage is unknown: the attempt
    bound fires with attempts_unassessed disclosed — unknown usage is never
    treated as a free pass."""
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        "[runtime]",
        "[budget]\nhard_stop_task_attempts = 2\n\n[runtime]",
    )
    project = make_project(tmp_path, config_toml=config)
    try:
        goal = dispatch.create_goal(
            project, objective="attempt bound", acceptance=["done"],
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["done"], verification=["true"]),
        ]))
        _seed_attempt_with_usage(project, "T001")
        _seed_attempt_with_usage(project, "T001")

        out = dispatch.run_slice(project)

        [error] = out["routing_errors"]
        assert error["budget"]["hard_stop"] is True
        assert error["budget"]["attempts"] == 2
        assert error["budget"]["attempts_unassessed"] == 2
        assert active_task(project, "T001").status == "runnable"
    finally:
        project.close()


def test_soft_warn_surfaces_without_blocking(tmp_path, monkeypatch):
    """Over the soft threshold the surfaces CARRY the warning (task list,
    run entry) but nothing is stopped — the hard bound is the only gate."""
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        "[runtime]",
        "[budget]\nsoft_warn_task_processing_tokens = 500\n\n[runtime]",
    )
    project = make_project(tmp_path, config_toml=config)
    try:
        goal = dispatch.create_goal(
            project, objective="soft warn", acceptance=["done"],
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["done"], verification=["true"]),
        ]))
        _seed_attempt_with_usage(
            project, "T001", input_tokens=600, output_tokens=10)

        out = dispatch.run_slice(project)
        [park] = out["host_required"]
        assert park["budget"]["soft_warn"] is True
        assert park["budget"]["hard_stop"] is False

        [row] = dispatch.task_list(project)
        assert row["budget"]["soft_warn"] is True
        assert row["budget"]["processing_tokens_recorded"] == 610
    finally:
        project.close()


def test_run_level_hard_stop_defers_new_launches(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        "[runtime]",
        "[budget]\nhard_stop_run_processing_tokens = 100\n\n[runtime]",
    )
    project = make_project(tmp_path, config_toml=config)
    try:
        goal = dispatch.create_goal(
            project, objective="run bound", acceptance=["done"],
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["done"], verification=["true"]),
            task_spec("T002", acceptance=["done"], verification=["true"]),
        ]))
        # Recorded history already exceeds the run budget.
        _seed_attempt_with_usage(
            project, "T001", input_tokens=150, output_tokens=5)

        out = dispatch.run_slice(project)

        assert out["host_required"] == []
        deferred = {d["task"]: d for d in out["deferred"]}
        assert set(deferred) == {"T001", "T002"}
        assert deferred["T002"]["budget"]["hard_stop"] is True
        assert active_task(project, "T002").status == "runnable"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# 4. Diagnostic-stall bound: check/gate commands get their own timeout


def test_task_check_uses_check_timeout_sec(tmp_path, monkeypatch):
    """A hanging check escalates at runtime.check_timeout_sec (default
    600s — the a130 stall waited ~42 min inside one pytest run under the
    old shared 1800s bound) instead of waiting silently."""
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        "command_timeout_sec = 30",
        "command_timeout_sec = 30\ncheck_timeout_sec = 1",
    )
    project = make_project(tmp_path, config_toml=config)
    try:
        goal = dispatch.create_goal(
            project, objective="check timeout", acceptance=["a1"],
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=["a1"], verification=["sleep 3"]),
        ]))
        dispatch.run_slice(project)
        dispatch.task_claim(project, "T001")

        started = time.monotonic()
        report = dispatch.task_check(project, "T001")
        elapsed = time.monotonic() - started

        result = report["results"][0]
        assert result["timed_out"] is True
        assert result["passed"] is False
        # Escalated near the 1s bound, not the 3s command runtime and not
        # the 30s launch bound.
        assert elapsed < 2.5
    finally:
        project.close()


# ---------------------------------------------------------------------------
# 5. Parser linearity regression (the a130 quadratic backtracking fix)


def test_bracket_param_discovery_stays_linear_on_long_no_bracket_text():
    """model_catalog's parameterized-syntax scan must stay linear on a long
    slug-shaped token run with no bracket: the pre-fix regex family-then-
    bracket pattern backtracked quadratically (measured 110 s on a 200 KB
    run) and cost the a130 round ~42 minutes of stall diagnosis before the
    parameterized-parser fix landed. This pins the fix that is already in
    tree: bounded absolute time AND bounded growth."""
    from orx import model_catalog

    def slug_run(size: int) -> str:
        token = "model-name-42."
        text = token * (size // len(token) + 1)
        return text[:size]

    small = slug_run(100_000)
    large = slug_run(200_000)

    t0 = time.monotonic()
    result_small = model_catalog.parse_parameterized_syntax({"help": small})
    t_small = time.monotonic() - t0
    t0 = time.monotonic()
    result_large = model_catalog.parse_parameterized_syntax({"help": large})
    t_large = time.monotonic() - t0

    # Correctness on the pathological input: no bracket evidence, no
    # invented availability.
    assert result_small.available is False
    assert result_large.available is False

    # Absolute bound: the old quadratic behavior measured 110 s on 200 KB;
    # 5 s is a 22x margin while still catching a reintroduced backtrack.
    assert t_large < 5.0, f"200 KB scan took {t_large:.2f}s"
    # Growth bound: doubling the input must not quadruple the time
    # (generous 3x to stay stable on loaded CI machines).
    if t_small > 0.05:
        assert t_large < 3 * t_small, (
            f"scan growth is superlinear: {t_small:.3f}s -> {t_large:.3f}s")

    # And the parser still finds a real bracket example after a long run.
    with_bracket = slug_run(150_000) + " family[context=1m,effort=high]"
    found = model_catalog.parse_parameterized_syntax({"help": with_bracket})
    assert found.available is True
    assert "context" in found.param_keys and "effort" in found.param_keys
