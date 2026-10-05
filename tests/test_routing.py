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
    assert parked["claim"] == "orx task claim T001"
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
