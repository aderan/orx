"""Deterministic routing: resource-state filtering, pinning, class policy."""

from __future__ import annotations

import pytest

from orx import dispatch, routing
from orx.records import PlanDepth, ResourceStatus, Role, RoutingError

from conftest import HOST_CONFIG_TOML, HOST_PROFILES_TOML, active_task, ir_for, make_project, task_spec


def route(project, **kwargs):
    return routing.route(project.store, project.config, project.profiles,
                         routing.RouteRequest(**kwargs))


def test_primary_selection_in_configured_order(project):
    result = route(project, role=Role.WORKER)
    assert result.ok
    assert result.selected == "host-worker"
    assert result.reason == "primary"
    assert result.fallback_used is False


def test_unavailable_skipped_with_fallback_reason(project):
    project.store.resource_set("host-worker", ResourceStatus.UNAVAILABLE)
    result = route(project, role=Role.WORKER)
    assert result.selected == "host-external"
    assert result.reason == "fallback"
    assert result.fallback_used is True
    rejected = next(c for c in result.candidates if c.profile == "host-worker")
    assert rejected.reject_reason == "unavailable"


def test_exhausted_skipped(project):
    project.store.resource_set("host-worker", ResourceStatus.EXHAUSTED)
    result = route(project, role=Role.WORKER)
    assert result.selected == "host-external"
    rejected = next(c for c in result.candidates if c.profile == "host-worker")
    assert rejected.reject_reason == "exhausted"


def test_unknown_resource_status_remains_routable(project):
    # host-worker has no resource row at all: routing must still use it.
    rows = [r for r in project.store.resource_rows() if r.profile == "host-worker"]
    assert rows == []
    result = route(project, role=Role.WORKER)
    assert result.selected == "host-worker"
    candidate = next(c for c in result.candidates if c.profile == "host-worker")
    assert candidate.resource_status == "unknown" and candidate.kept


def test_constrained_is_last_resort(project):
    project.store.resource_set("host-worker", ResourceStatus.CONSTRAINED)
    result = route(project, role=Role.WORKER)
    # host-external (unknown) wins over constrained host-worker, as fallback
    assert result.selected == "host-external"
    assert result.reason == "fallback"
    assert result.fallback_used is True

    project.store.resource_set("host-external", ResourceStatus.UNAVAILABLE)
    result = route(project, role=Role.WORKER)
    assert result.selected == "host-worker"
    assert result.reason == "constrained_last_resort"


def test_all_non_routable_is_an_error(project):
    project.store.resource_set("host-worker", ResourceStatus.UNAVAILABLE)
    project.store.resource_set("host-external", ResourceStatus.EXHAUSTED)
    result = route(project, role=Role.WORKER)
    assert not result.ok
    assert "no usable profile" in result.error


def test_pinned_profile_never_falls_back(project):
    project.store.resource_set("host-worker", ResourceStatus.UNAVAILABLE)
    result = route(project, role=Role.WORKER, pinned_profile="host-worker")
    assert not result.ok
    assert "pinned" in result.error and "host-worker" in result.error


def test_pinned_missing_profile_is_an_error(project):
    result = route(project, role=Role.WORKER, pinned_profile="ghost")
    assert not result.ok
    assert any(c.reject_reason == "unknown_profile" for c in result.candidates)


def test_pinned_profile_reason_is_pinned(project):
    result = route(project, role=Role.WORKER, pinned_profile="host-external")
    assert result.ok and result.reason == "pinned" and result.fallback_used is False


def test_deep_planning_refuses_below_frontier(project):
    # [plan.deep] = ["host-frontier", "host-planner"]; make frontier unusable.
    project.store.resource_set("host-frontier", ResourceStatus.UNAVAILABLE)
    result = route(project, role=Role.PLANNER, depth=PlanDepth.DEEP,
                   allow_class_downgrade=False)
    assert not result.ok
    assert result.downgrade_blocked is True
    assert any(c.reject_reason == "class_below_frontier" for c in result.candidates)


def test_deep_planning_selects_frontier_when_available(project):
    result = route(project, role=Role.PLANNER, depth=PlanDepth.DEEP)
    assert result.ok
    assert result.selected == "host-frontier"
    assert result.downgrade_blocked is False


def test_deep_downgrade_allowed_by_policy(project):
    project.store.resource_set("host-frontier", ResourceStatus.UNAVAILABLE)
    result = route(project, role=Role.PLANNER, depth=PlanDepth.DEEP,
                   allow_class_downgrade=True)
    assert result.ok
    assert result.selected == "host-planner"  # strong, but policy allows it


def test_standard_depth_has_no_class_filter(project):
    result = route(project, role=Role.PLANNER, depth=PlanDepth.STANDARD)
    assert result.ok and result.selected == "host-planner"


def test_capability_filter_applies_to_workers(project):
    result = route(project, role=Role.WORKER, required_capabilities=("vision",))
    assert not result.ok  # neither worker profile has vision
    assert any(c.reject_reason and c.reject_reason.startswith("missing_capability")
               for c in result.candidates)


def test_vision_verifier_routed_from_verify_list(project):
    result = route(project, role=Role.VERIFIER, required_capabilities=("vision",))
    assert result.ok
    assert result.selected == "host-vision"
    first = next(c for c in result.candidates if c.profile == "host-verifier")
    assert first.reject_reason == "missing_capability:vision"


def test_decision_recorded_with_full_history(project):
    project.store.resource_set("host-worker", ResourceStatus.UNAVAILABLE)
    result = route(project, role=Role.WORKER)
    routing.persist_decision(project.store, routing.RouteRequest(role=Role.WORKER), result)

    [decision] = project.store.routing_decisions_all()
    assert decision.selected == "host-external"
    assert decision.reason == "fallback"
    assert decision.requested == {"role": "worker"}
    candidates = {c["profile"]: c for c in decision.candidates}
    assert candidates["host-worker"]["reject_reason"] == "unavailable"
    assert candidates["host-external"]["kept"] is True


def test_planner_routing_via_dispatch_records_attempt_and_assignment(project, goal):
    result = dispatch.plan_route(project)
    assert result["mode"] == "host_required"
    assignment = result["assignment"]
    assert assignment["id"] == "P001"
    assert assignment["role"] == "planner"
    assert "submit" in assignment and "schema" in assignment
    assert "marker file exists" in assignment["prompt"]

    decisions = project.store.routing_decisions_all()
    assert decisions[-1].role == "planner"
    assert decisions[-1].attempt_id is not None


def test_pinned_planner_profile_used_without_fallback(project, goal):
    result = dispatch.plan_route(project, profile_flag="host-planner")
    assert result["assignment"]["profile"] == "host-planner"
    assert result["routing"]["reason"] == "pinned"


def test_cli_planner_without_valid_json_fails_honestly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        '[plan.standard]\nprofiles = ["host-planner"]',
        '[plan.standard]\nprofiles = ["cli-fake"]',
    )
    project = make_project(tmp_path, config_toml=config)
    try:
        goal = dispatch.create_goal(project, "plain goal", ["a1"])[0]
        # cli-fake runs `true`: exits 0 but emits no Plan IR JSON.
        with pytest.raises(dispatch.ORXError) as excinfo:
            dispatch.plan_route(project)
        assert "valid Plan IR JSON" in str(excinfo.value)
        # Nothing was faked: no assignment, no revision, no tasks.
        assert project.store.assignment_waiting(goal.id) is None
        assert dispatch.task_list(project) == []
    finally:
        project.close()


def test_cli_worker_executes_via_shell_adapter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]', 'profiles = ["cli-fake"]'
    )
    project = make_project(tmp_path, config_toml=config)
    try:
        goal = dispatch.create_goal(project, "plain goal", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec("T001", acceptance=["a1"])]))
        result = dispatch.run_slice(project)
        # `true` exits 0; empty verification list passes on completion.
        assert [s["task"] for s in result["started"]] == ["T001"]
        assert result["started"][0]["status"] == "passed"
        assert dispatch.status_data(project)["run"]["status"] == "done"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Host-exclusive capability routing (R002 follow-up): a task that declares
# host_context parks for the host driver at `orx run` — a CLI worker is never
# started for it — and tasks without the declaration route exactly as before.


MIXED_PROFILES_TOML = HOST_PROFILES_TOML + """
[profiles.host-session]
driver = "host"
harness = "zcode"
model = "m-host"
class = "strong"
effort = "medium"
capabilities = ["coding", "host_context"]
"""

MIXED_WORKER_CONFIG_TOML = HOST_CONFIG_TOML.replace(
    'profiles = ["host-worker", "host-external"]',
    'profiles = ["cli-fake", "host-session"]',
)

CLI_ONLY_WORKER_CONFIG_TOML = HOST_CONFIG_TOML.replace(
    'profiles = ["host-worker", "host-external"]',
    'profiles = ["cli-fake"]',
)

CLI_PRETENDING_PROFILES_TOML = HOST_PROFILES_TOML + """
[profiles.cli-pretend-host]
driver = "cli"
harness = "shell"
executable = "true"
prompt_transport = "stdin"
model = "fake-pretend"
class = "economy"
effort = "low"
capabilities = ["coding", "host_context"]
"""


@pytest.fixture
def mixed_project(tmp_path, monkeypatch):
    """CLI-first worker ladder plus one host profile declaring host_context:
    the declaration, not the config order, must move selection to host."""
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path, config_toml=MIXED_WORKER_CONFIG_TOML,
                           profiles_toml=MIXED_PROFILES_TOML)
    yield project
    project.close()


def test_host_context_routes_to_host_profile_not_cli(mixed_project):
    result = route(mixed_project, role=Role.WORKER,
                   required_capabilities=(routing.HOST_CONTEXT_CAPABILITY,))
    assert result.ok
    assert result.selected == "host-session"
    assert result.profile.driver.value == "host"
    rejected = next(c for c in result.candidates if c.profile == "cli-fake")
    assert rejected.kept is False
    assert rejected.reject_reason == "missing_capability:host_context"


def test_without_capability_configured_cli_order_is_unchanged(mixed_project):
    # No host_context declaration: the first configured rung (cli-fake)
    # stays the primary selection — routing is exactly as it was.
    result = route(mixed_project, role=Role.WORKER)
    assert result.ok and result.selected == "cli-fake" and result.reason == "primary"
    assert result.fallback_used is False


def test_host_exclusive_capability_rejects_non_host_profile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]',
        'profiles = ["cli-pretend-host"]',
    )
    project = make_project(tmp_path, config_toml=config,
                           profiles_toml=CLI_PRETENDING_PROFILES_TOML)
    try:
        result = route(project, role=Role.WORKER,
                       required_capabilities=(routing.HOST_CONTEXT_CAPABILITY,))
        # Even a profile that CLAIMS host_context cannot serve it: the driver
        # gate makes the exclusivity structural, not a config convention.
        assert not result.ok
        candidate = result.candidates[0]
        assert candidate.kept is False
        assert candidate.reject_reason == "driver_not_host:host_context"
        assert "driver_not_host" in result.error
    finally:
        project.close()


def test_run_slice_parks_host_context_task_as_host(mixed_project):
    goal = dispatch.create_goal(mixed_project, "plain goal", ["a1"])[0]
    # Plan validation accepts the declaration because a profile declares it
    # (the known-capability set is the union of profile capabilities).
    dispatch.submit_plan(mixed_project, ir_for(
        goal, [task_spec("T001", acceptance=["a1"], caps=("host_context",))]
    ))
    result = dispatch.run_slice(mixed_project)
    assert result["started"] == []  # no CLI worker launched
    assert result["routing_errors"] == []
    assert [h["task"] for h in result["host_required"]] == ["T001"]
    parked = result["host_required"][0]
    assert parked["profile"] == "host-session"
    assert parked["claim"] == "orx task claim T001 --discover-session"
    assert active_task(mixed_project, "T001").status == "waiting_host"
    # The routing decision records what was requested, for the audit trail.
    decision = mixed_project.store.routing_decisions_all()[-1]
    assert decision.requested["required_capabilities"] == ["host_context"]
    assert decision.selected == "host-session"


def test_run_slice_still_executes_cli_worker_without_declaration(mixed_project):
    goal = dispatch.create_goal(mixed_project, "plain goal", ["a1"])[0]
    dispatch.submit_plan(mixed_project, ir_for(
        goal, [task_spec("T001", acceptance=["a1"])]  # caps default to ("coding",)
    ))
    result = dispatch.run_slice(mixed_project)
    assert [s["task"] for s in result["started"]] == ["T001"]
    assert result["started"][0]["status"] == "passed"
    assert result["host_required"] == []
    assert dispatch.status_data(mixed_project)["run"]["status"] == "done"


def test_host_context_without_host_profile_in_ladder_is_routing_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # host-session stays defined (so plan validation knows the capability),
    # but the worker ladder has no host rung: the routing error is surfaced
    # and the task is never silently handed to a CLI worker.
    project = make_project(tmp_path, config_toml=CLI_ONLY_WORKER_CONFIG_TOML,
                           profiles_toml=MIXED_PROFILES_TOML)
    try:
        goal = dispatch.create_goal(project, "plain goal", ["a1"])[0]
        dispatch.submit_plan(project, ir_for(
            goal, [task_spec("T001", acceptance=["a1"], caps=("host_context",))]
        ))
        result = dispatch.run_slice(project)
        assert result["started"] == []
        assert [e["task"] for e in result["routing_errors"]] == ["T001"]
        assert "host_context" in result["routing_errors"][0]["error"]
        assert active_task(project, "T001").status == "runnable"
    finally:
        project.close()


def test_zcode_preset_registers_host_context_on_host_profiles():
    from orx.config import load_profiles
    from orx.presets import preset_dir

    profiles = load_profiles(preset_dir("zcode") / "profiles.toml")
    assert profiles
    for profile in profiles.values():
        assert profile.driver.value == "host"
        assert routing.HOST_CONTEXT_CAPABILITY in profile.capabilities


# ---------------------------------------------------------------------------
# Resource-failure reroute dispatches by the NEXT rung's driver (G009 T003).
#
# The incident (2026-10-07, docs/diagnostics/cursor-2026-10-07/
# replay_cli_to_host.py): a rate-limited CLI worker's reroute unconditionally
# re-executed as CLI, so a HOST next rung raised ORXError "no adapter for
# harness 'zcode'" instead of parking a host assignment. The branch matrix,
# each destination following its own execution contract:
#
#   CLI -> CLI      existing fallback rungs (test_health.py
#                   test_failing_worker_flips_health_and_routing_falls_back;
#                   the hop bound is pinned below)
#   CLI -> host     the incident: park a host assignment (waiting_host) with
#                   its OWN nonce/attempt; the failed CLI attempt stays history
#   CLI -> external the formal external contract only (waiting_external) —
#                   never an adapter masquerade
#   host -> CLI     host task's formal end fact (task fail) + operator health
#                   fact, then retry re-dispatches by driver through run_slice
#   host -> host    same, landing on the next HOST rung as a fresh assignment

# The incident shape: cli-fake's stand-in always fails with a rate limit, so
# the reroute fires in the same slice and the next rung decides the branch.
RATE_LIMIT_PROFILES_TOML = HOST_PROFILES_TOML.replace(
    'executable = "true"',
    'executable = "/bin/sh"\n'
    'args = ["-c", "echo rate limit exceeded >&2; exit 1"]',
)


def _worker_attempts(project):
    rows = project.store.conn.execute(
        "SELECT id,profile,driver,result,failure_reason,nonce FROM attempts"
        " WHERE role='worker' ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def test_reroute_to_host_rung_parks_host_assignment(tmp_path, monkeypatch):
    """CLI -> host (the incident). A rate-limited CLI worker whose next rung
    is a host profile must PARK a host assignment — host_required with a
    prompt file whose ORX_ASSIGNMENT nonce is the parked attempt's own —
    never raise "no adapter for harness 'zcode'". Identity stays bound: the
    failed CLI attempt keeps its recorded history, the parked host attempt is
    a NEW attempt."""
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]',
        'profiles = ["cli-fake", "host-worker"]',
    )
    project = make_project(tmp_path, config_toml=config,
                           profiles_toml=RATE_LIMIT_PROFILES_TOML)
    try:
        goal = dispatch.create_goal(
            project, objective="rate limit falls to host", acceptance=["done"],
            constraints=[], context="",
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec(
            "T001", acceptance=goal.acceptance, verification=["true"],
            complexity="low",
        )]))

        outcome = dispatch.run_slice(project)

        # The dispatch never crashed and the task is neither failed nor
        # claimed by a fake CLI execution.
        assert outcome["failed"] == []
        assert outcome["started"] == []
        [entry] = [e for e in outcome["host_required"] if e["task"] == "T001"]
        assert entry["profile"] == "host-worker"
        assert entry["fallback_from"] == "cli-fake"
        assert entry["claim"] == "orx task claim T001 --discover-session"
        assert active_task(project, "T001").status == "waiting_host"

        # The parked assignment carries its own identity: the nonce in the
        # prompt file's machine anchor is the parked attempt's nonce.
        prompt = (tmp_path / entry["prompt_file"]).read_text()
        assert prompt.startswith("ASSIGNMENT IDENTITY")
        assert f"ORX_ASSIGNMENT={entry['execution']['nonce']}\n" in prompt
        assert entry["execution"]["attempt"] == entry["attempt"]

        # Identity binding: the failed CLI attempt stays history, the host
        # attempt is new and still open.
        attempts = {a["profile"]: a for a in _worker_attempts(project)}
        assert attempts["cli-fake"]["result"] == "failed"
        assert "rate limit" in attempts["cli-fake"]["failure_reason"]
        assert attempts["host-worker"]["result"] is None
        assert attempts["host-worker"]["id"] != attempts["cli-fake"]["id"]
        assert attempts["host-worker"]["nonce"] != attempts["cli-fake"]["nonce"]

        # The failed rung is an environment fact for routing.
        row = project.store.resource_row("cli-fake")
        assert row.status == "cooldown" and row.last_error_kind == "rate_limited"
    finally:
        project.close()


def test_reroute_to_external_rung_parks_external_contract_only(tmp_path, monkeypatch):
    """CLI -> external. A rate-limited CLI worker whose next rung is an
    EXTERNAL profile parks the formal external contract (waiting_external,
    finish_with `orx task complete --evidence`) — ORX never launches an
    adapter for it and the entry never masquerades as a host assignment."""
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]',
        'profiles = ["cli-fake", "host-external"]',
    )
    project = make_project(tmp_path, config_toml=config,
                           profiles_toml=RATE_LIMIT_PROFILES_TOML)
    try:
        goal = dispatch.create_goal(
            project, objective="rate limit falls to external", acceptance=["done"],
            constraints=[], context="",
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec(
            "T001", acceptance=goal.acceptance, verification=["true"],
            complexity="low",
        )]))

        outcome = dispatch.run_slice(project)

        assert outcome["failed"] == [] and outcome["started"] == []
        assert outcome["host_required"] == []
        [entry] = [e for e in outcome["waiting_external"] if e["task"] == "T001"]
        assert entry["profile"] == "host-external"
        assert entry["fallback_from"] == "cli-fake"
        assert entry["finish_with"] == "orx task complete T001 --evidence <file>"
        assert "claim" not in entry  # external is not dispatched or claimed
        assert active_task(project, "T001").status == "waiting_external"
        assert (tmp_path / entry["prompt_file"]).exists()

        attempts = {a["profile"]: a for a in _worker_attempts(project)}
        assert attempts["cli-fake"]["result"] == "failed"
        assert attempts["host-external"]["driver"] == "external"
        assert attempts["host-external"]["result"] is None
    finally:
        project.close()


def test_reroute_hop_is_bounded_once_per_slice(tmp_path, monkeypatch):
    """The switch is bounded: at most ONE hop per task per slice. Two
    rate-limited CLI rungs mean the second rung's own resource failure is NOT
    re-hopped in the same slice (the task stays FAILED with exactly two
    worker attempts); a later explicit retry reaches the healthy rung."""
    monkeypatch.chdir(tmp_path)
    profiles = RATE_LIMIT_PROFILES_TOML + """
[profiles.cli-still-ratey]
driver = "cli"
harness = "shell"
executable = "/bin/sh"
args = ["-c", "echo rate limit exceeded >&2; exit 1"]
prompt_transport = "stdin"
model = "fake-model2"
class = "economy"
effort = "low"
capabilities = ["coding"]
"""
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]',
        'profiles = ["cli-fake", "cli-still-ratey", "host-worker"]',
    )
    project = make_project(tmp_path, config_toml=config, profiles_toml=profiles)
    try:
        goal = dispatch.create_goal(
            project, objective="bounded reroute", acceptance=["done"],
            constraints=[], context="",
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec(
            "T001", acceptance=goal.acceptance, verification=["true"],
            complexity="low",
        )]))

        outcome = dispatch.run_slice(project)

        # One hop only: two CLI attempts, then the task stays FAILED — the
        # third rung is NOT tried in the same slice (no loop, no unbounded
        # ladder walk), and no crash surfaced.
        attempts = _worker_attempts(project)
        assert [a["profile"] for a in attempts] == ["cli-fake", "cli-still-ratey"]
        assert active_task(project, "T001").status == "failed"
        [failed] = outcome["failed"]
        assert failed["profile"] == "cli-still-ratey"
        assert failed["fallback_from"] == "cli-fake"
        assert outcome["host_required"] == [] and outcome["started"] == []

        # The bound is per slice, not forever: an explicit retry routes the
        # healthy host rung as a normal park (by driver).
        dispatch.task_retry(project, "T001")
        second = dispatch.run_slice(project)
        [entry] = [e for e in second["host_required"] if e["task"] == "T001"]
        assert entry["profile"] == "host-worker"
        assert active_task(project, "T001").status == "waiting_host"
    finally:
        project.close()


def test_host_resource_failure_redispatches_to_cli_by_driver(tmp_path, monkeypatch):
    """host -> CLI through the formal machinery: the host task ends with a
    formal fact (`task fail`), the operator records the resource fact, the
    explicit retry re-enters run_slice — which dispatches by the surviving
    rung's driver (execute as CLI), keeping the dead host attempt as
    history."""
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]',
        'profiles = ["host-worker", "cli-fake"]',
    )
    project = make_project(tmp_path, config_toml=config)
    try:
        goal = dispatch.create_goal(
            project, objective="host quota death", acceptance=["done"],
            constraints=[], context="",
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec(
            "T001", acceptance=goal.acceptance, verification=["true"],
            complexity="low",
        )]))
        parked = dispatch.run_slice(project)
        assert [e["task"] for e in parked["host_required"]] == ["T001"]
        host_attempt = parked["host_required"][0]["attempt"]

        # The host worker dies on a quota error (GLM 1308 shape); the formal
        # end fact is the explicit fail, and the operator records exhaustion.
        dispatch.task_claim(project, "T001", session="sess-host-a")
        dispatch.task_fail(project, "T001", "GLM 1308: quota exceeded on host")
        project.store.resource_set("host-worker", ResourceStatus.EXHAUSTED,
                                   "GLM 1308 quota death")

        dispatch.task_retry(project, "T001")
        outcome = dispatch.run_slice(project)

        assert outcome["host_required"] == []
        [started] = outcome["started"]
        assert started["task"] == "T001"
        assert started["profile"] == "cli-fake"
        assert started["status"] == "passed"
        assert dispatch.status_data(project)["run"]["status"] == "done"

        # Identity binding: the failed host attempt keeps its reason; the CLI
        # execution is a distinct new attempt.
        attempts = {a["id"]: a for a in _worker_attempts(project)}
        assert attempts[host_attempt]["result"] == "failed"
        assert "GLM 1308" in attempts[host_attempt]["failure_reason"]
        cli_rows = [a for a in attempts.values() if a["profile"] == "cli-fake"]
        assert len(cli_rows) == 1 and cli_rows[0]["result"] is not None
    finally:
        project.close()


def test_host_resource_failure_redispatches_to_next_host_rung(tmp_path, monkeypatch):
    """host -> host: the same formal end + operator facts, landing on the
    next HOST rung — a FRESH assignment with its own attempt and nonce, the
    dead attempt untouched in history."""
    monkeypatch.chdir(tmp_path)
    profiles = HOST_PROFILES_TOML + """
[profiles.host-worker-b]
driver = "host"
harness = "zcode"
model = "m-worker-b"
class = "strong"
effort = "medium"
capabilities = ["coding"]
"""
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]',
        'profiles = ["host-worker", "host-worker-b"]',
    )
    project = make_project(tmp_path, config_toml=config, profiles_toml=profiles)
    try:
        goal = dispatch.create_goal(
            project, objective="host to host switch", acceptance=["done"],
            constraints=[], context="",
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec(
            "T001", acceptance=goal.acceptance, verification=["true"],
            complexity="low",
        )]))
        parked = dispatch.run_slice(project)["host_required"][0]
        assert parked["profile"] == "host-worker"

        dispatch.task_claim(project, "T001", session="sess-host-b")
        dispatch.task_fail(project, "T001", "GLM 1308: quota exceeded on host")
        project.store.resource_set("host-worker", ResourceStatus.EXHAUSTED,
                                   "GLM 1308 quota death")

        dispatch.task_retry(project, "T001")
        outcome = dispatch.run_slice(project)

        [entry] = [e for e in outcome["host_required"] if e["task"] == "T001"]
        assert entry["profile"] == "host-worker-b"
        assert active_task(project, "T001").status == "waiting_host"
        assert entry["attempt"] != parked["attempt"]
        prompt = (tmp_path / entry["prompt_file"]).read_text()
        assert f"ORX_ASSIGNMENT={entry['execution']['nonce']}\n" in prompt

        # The dead host attempt stays recorded; the new one is open.
        attempts = {a["profile"]: a for a in _worker_attempts(project)}
        assert attempts["host-worker"]["result"] == "failed"
        assert "GLM 1308" in attempts["host-worker"]["failure_reason"]
        assert attempts["host-worker-b"]["result"] is None
    finally:
        project.close()
