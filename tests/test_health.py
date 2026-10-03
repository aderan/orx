"""Health auto-learning and routing gates (M1 P4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orx import dispatch, health
from orx.records import ResourceStatus, TaskStatus


def _store_with(tmp_path, monkeypatch, profiles=("host-worker",)):
    monkeypatch.chdir(tmp_path)
    from conftest import make_project
    project = make_project(tmp_path)
    store = project.store
    store.seed_resources(list(profiles))
    return project, store


def test_success_resets_streak(tmp_path, monkeypatch):
    project, store = _store_with(tmp_path, monkeypatch)
    try:
        health.record_attempt_outcome(store, "host-worker", ok=False, error_kind="temporary_failure")
        health.record_attempt_outcome(store, "host-worker", ok=False, error_kind="temporary_failure")
        assert store.resource_row("host-worker").failure_streak == 2
        health.record_attempt_outcome(store, "host-worker", ok=True)
        row = store.resource_row("host-worker")
        assert row.status == "available" and row.failure_streak == 0
        assert row.last_error_kind is None
    finally:
        project.close()


def test_auth_required_gates_routing(tmp_path, monkeypatch):
    project, store = _store_with(tmp_path, monkeypatch)
    try:
        health.record_attempt_outcome(store, "host-worker", ok=False, error_kind="auth_required")
        assert store.resource_row("host-worker").status == "auth_required"
        from orx import routing
        from orx.records import Role
        req = routing.RouteRequest(role=Role.WORKER, required_capabilities=("coding",),
                                   pinned_profile="host-worker")
        result = routing.route(store, project.config, project.profiles, req)
        assert not result.ok
        rejected = {c.profile: c.reject_reason for c in result.candidates}
        assert rejected.get("host-worker") == "auth_required"
    finally:
        project.close()


def test_rate_limit_sets_bounded_cooldown(tmp_path, monkeypatch):
    project, store = _store_with(tmp_path, monkeypatch)
    try:
        health.record_attempt_outcome(store, "host-worker", ok=False, error_kind="rate_limited")
        row = store.resource_row("host-worker")
        assert row.status == "cooldown" and row.cooldown_until
        until = datetime.fromisoformat(row.cooldown_until)
        delta = until - datetime.now(timezone.utc)
        assert timedelta(0) < delta <= timedelta(seconds=60 * 2 + 30)
        assert health.cooldown_active(row) is True

        # expiry re-opens routing: forge an expired cooldown
        store.resource_learn("host-worker", cooldown_until=(
            datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        assert health.cooldown_active(store.resource_row("host-worker")) is False
    finally:
        project.close()


def test_temporary_needs_three_consecutive(tmp_path, monkeypatch):
    project, store = _store_with(tmp_path, monkeypatch)
    try:
        for i in (1, 2):
            health.record_attempt_outcome(store, "host-worker", ok=False, error_kind="temporary_failure")
            assert store.resource_row("host-worker").status != "cooldown", i
        health.record_attempt_outcome(store, "host-worker", ok=False, error_kind="temporary_failure")
        assert store.resource_row("host-worker").status == "cooldown"
    finally:
        project.close()


def test_non_gating_kinds_record_but_never_gate(tmp_path, monkeypatch):
    project, store = _store_with(tmp_path, monkeypatch)
    try:
        for kind in ("model_unavailable", "context_exceeded", "invalid_request",
                     "process_failure", None):
            health.record_attempt_outcome(store, "host-worker", ok=False, error_kind=kind)
            assert store.resource_row("host-worker").status != "cooldown"
            assert store.resource_row("host-worker").status != "auth_required"
        assert store.resource_row("host-worker").failure_streak == 5
        assert store.resource_row("host-worker").last_error_kind is None
    finally:
        project.close()


def test_override_protects_and_clear_reenables(tmp_path, monkeypatch):
    project, store = _store_with(tmp_path, monkeypatch)
    try:
        store.resource_set("host-worker", ResourceStatus.UNAVAILABLE, "demo quota")
        health.record_attempt_outcome(store, "host-worker", ok=True)
        assert store.resource_row("host-worker").status == "unavailable"  # untouched
        store.resource_clear("host-worker")
        health.record_attempt_outcome(store, "host-worker", ok=True)
        assert store.resource_row("host-worker").status == "available"
    finally:
        project.close()


def test_failing_worker_flips_health_and_routing_falls_back(tmp_path, monkeypatch):
    """End-to-end: a shell worker whose output says 'rate limit' drives its
    profile into cooldown; the next run_slice falls through to the next
    worker in the configured list."""
    import os
    import stat
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ratey").write_text("#!/bin/sh\necho 'rate limit exceeded' >&2\nexit 1\n")
    (bindir / "ratey").chmod((bindir / "ratey").stat().st_mode | stat.S_IEXEC)
    (bindir / "oksh").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "oksh").chmod((bindir / "oksh").stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.chdir(tmp_path)

    from conftest import make_project
    project = make_project(
        tmp_path,
        profiles_toml="""schema_version = 1
[profiles.ratey]
driver = "cli"
harness = "shell"
executable = "ratey"
prompt_transport = "argument"
model = "m"
class = "strong"
effort = "medium"
capabilities = ["coding"]

[profiles.oksh]
driver = "cli"
harness = "shell"
executable = "oksh"
prompt_transport = "argument"
model = "m"
class = "strong"
effort = "medium"
capabilities = ["coding"]
""",
        config_toml="""schema_version = 1
[controller]
profile = "oksh"
[plan.standard]
profiles = ["oksh"]
[worker]
profiles = ["ratey", "oksh"]
[verify]
profiles = ["oksh"]
[runtime]
command_timeout_sec = 30
""",
    )
    try:
        goal = dispatch.create_goal(project, "goal text", ["a1"])[0]
        dispatch.submit_plan(project, __import__("conftest").ir_for(goal, [
            __import__("conftest").task_spec("T001", acceptance=["a1"], verification=[]),
        ]))
        first = dispatch.run_slice(project)
        assert first["failed"] and "rate limit" in first["failed"][0]["reason"]
        row = project.store.resource_row("ratey")
        assert row.status == "cooldown" and row.last_error_kind == "rate_limited"

        dispatch.task_retry(project, "T001")
        second = dispatch.run_slice(project)
        started = second["started"][0]
        assert started["profile"] == "oksh"  # cooldown pushed routing to the fallback
        assert started["status"] == "passed"
    finally:
        project.close()


def test_backoff_grows_with_streak_and_caps(tmp_path, monkeypatch):
    import time
    from datetime import datetime, timedelta, timezone
    from orx.health import cooldown_until, record_attempt_outcome
    project, store = _store_with(tmp_path, monkeypatch, profiles=("host-worker",))
    try:
        import time
        windows = []
        for i in range(1, 8):
            before = datetime.now(timezone.utc)
            until = datetime.fromisoformat(cooldown_until(i))
            windows.append((until - before).total_seconds())
        # exponential growth with the 2**5 cap: window(7) ~= window(6) (capped)
        assert windows[1] > windows[0]              # 2x streak 1
        assert abs(windows[-1] - windows[-2]) < 90  # capped at 2**5, jitter only
        base_cap = 60 * 2**5 + 30
        assert windows[-1] <= base_cap
        # success clears cooldown fields
        record_attempt_outcome(store, "host-worker", ok=False, error_kind="rate_limited")
        assert store.resource_row("host-worker").cooldown_until is not None
        record_attempt_outcome(store, "host-worker", ok=True)
        assert store.resource_row("host-worker").cooldown_until is None
    finally:
        project.close()


def test_quota_records_reset_when_known(tmp_path, monkeypatch):
    project, store = _store_with(tmp_path, monkeypatch, profiles=("host-worker",))
    try:
        health.record_attempt_outcome(store, "host-worker", ok=False,
                                      error_kind="quota_exhausted",
                                      quota_reset_at="2099-01-01T00:00:00+00:00")
        row = store.resource_row("host-worker")
        assert row.status == "exhausted" and row.quota_reset_at == "2099-01-01T00:00:00+00:00"
    finally:
        project.close()


def test_error_kinds_each_drive_expected_status(tmp_path, monkeypatch):
    """Parametrized truth table: kind -> resulting status (or None = never gates)."""
    project, store = _store_with(tmp_path, monkeypatch, profiles=("host-worker",))
    expectations = {
        "auth_required": "auth_required",
        "quota_exhausted": "exhausted",
        "rate_limited": "cooldown",
        "model_unavailable": None,
        "context_exceeded": None,
        "invalid_request": None,
        "process_failure": None,
        "cancelled": None,
    }
    try:
        for kind, expected in expectations.items():
            store.resource_set("host-worker", ResourceStatus.UNKNOWN)
            store.resource_clear("host-worker")
            health.record_attempt_outcome(store, "host-worker", ok=False, error_kind=kind)
            status = store.resource_row("host-worker").status
            if expected is None:
                assert status not in ("cooldown", "auth_required", "exhausted"), kind
            else:
                assert status == expected, kind
            assert store.resource_row("host-worker").last_failure_at is not None
    finally:
        project.close()


def test_planner_failure_learns_health_too(tmp_path, monkeypatch):
    """The health hook is on every launch path, planner included."""
    project, store = _store_with(tmp_path, monkeypatch, profiles=("host-planner",))
    try:
        from orx.adapters.base import classify_failure

        class FakeResult:
            exit_code = 1
            stdout = ""
            stderr = "Error: Not logged in"

        assert classify_failure(FakeResult()) == "auth_required"
        health.record_attempt_outcome(store, "host-planner", ok=False,
                                      error_kind=classify_failure(FakeResult()))
        assert store.resource_row("host-planner").status == "auth_required"
    finally:
        project.close()


def test_success_after_auth_required_recovers(tmp_path, monkeypatch):
    project, store = _store_with(tmp_path, monkeypatch, profiles=("host-worker",))
    try:
        health.record_attempt_outcome(store, "host-worker", ok=False, error_kind="auth_required")
        assert store.resource_row("host-worker").status == "auth_required"
        # operator re-login + a successful run must clear the gate
        health.record_attempt_outcome(store, "host-worker", ok=True)
        assert store.resource_row("host-worker").status == "available"
    finally:
        project.close()


def test_cooldown_expiry_exact_boundary(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    project, store = _store_with(tmp_path, monkeypatch, profiles=("host-worker",))
    try:
        from orx import health as health_mod
        row_now = (datetime.now(timezone.utc)).isoformat()
        # exactly now -> not active (retry time reached)
        store.seed_resources(["host-worker"])
        store.resource_learn("host-worker", status="cooldown",
                             cooldown_until=datetime.now(timezone.utc).isoformat())
        assert health_mod.cooldown_active(store.resource_row("host-worker")) is False
    finally:
        project.close()
