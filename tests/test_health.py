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
    profile into cooldown, and since G005 the same run_slice immediately
    falls through to the next configured worker (the manual `task retry`
    round-trip is no longer needed for resource failures)."""
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
        # The rate-limited failure is recorded (attempts carry its reason)
        # while the same slice already re-routed to the next rung.
        assert first["failed"] == []
        started = first["started"][0]
        assert started["profile"] == "oksh" and started["fallback_from"] == "ratey"
        assert started["status"] == "passed"
        row = project.store.resource_row("ratey")
        assert row.status == "cooldown" and row.last_error_kind == "rate_limited"
    finally:
        project.close()


@pytest.mark.xfail(strict=True, reason=(
    "G009 red baseline (2026-10-07): the resource-failure reroute"
    " unconditionally re-executes as CLI, so a host next rung raises"
    " ORXError \"no adapter for harness 'zcode'\" instead of parking a host"
    " assignment"
    " (docs/diagnostics/cursor-2026-10-07/replay_cli_to_host.py). Remove"
    " this marker with the CLI->host dispatch fix, never by weakening the"
    " test."))
def test_rate_limited_cli_falls_back_to_host_parking(tmp_path, monkeypatch):
    """G009 T001 (2026-10-07 incident): replay_cli_to_host.py as a regular
    regression. The sibling test above covers CLI->CLI fallback; this one
    pins the rung whose next profile is a HOST driver: a rate-limited CLI
    worker must park a host assignment (host_required), never crash
    run_slice with ORXError "no adapter for harness 'zcode'" — the bug the
    diagnostic replay recorded against the real dispatch stack. The
    rate-limit facts themselves (cooldown learned, failed CLI attempt kept
    as history) already behave correctly today and must stay that way."""
    from conftest import (HOST_CONFIG_TOML, HOST_PROFILES_TOML, active_task,
                          ir_for, make_project, task_spec)
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]',
        'profiles = ["cli-fake", "host-worker"]',
    )
    profiles = HOST_PROFILES_TOML.replace(
        'executable = "true"',
        'executable = "/bin/sh"\n'
        'args = ["-c", "echo rate limit exceeded >&2; exit 1"]',
    )
    project = make_project(tmp_path, config, profiles)
    try:
        goal = dispatch.create_goal(
            project, objective="Verify CLI rate limit switches to host",
            acceptance=["done"], constraints=[], context="",
        )[0]
        dispatch.submit_plan(project, ir_for(goal, [task_spec(
            "T310", acceptance=goal.acceptance, verification=["true"],
            complexity="low",
        )]))

        # The defect: this call raises ORXError("no adapter for harness
        # 'zcode'") because the reroute unconditionally re-executes as CLI.
        outcome = dispatch.run_slice(project)

        # The desired behavior: the next rung is a host profile, so the
        # task is PARKED for host execution — an assignment the controller
        # can pick up, not a crash and not a task left failed.
        host_entries = [
            entry for entry in outcome["host_required"]
            if entry["profile"] == "host-worker"
        ]
        assert host_entries, outcome
        entry = host_entries[0]
        assert entry["task"] == "T310"
        assert entry["prompt_file"] and (tmp_path / entry["prompt_file"]).exists()
        assert active_task(project, "T310").status == "waiting_host"

        # The rate-limited CLI rung is recorded as an environment fact for
        # routing (this half is already correct today).
        row = project.store.resource_row("cli-fake")
        assert row.status == "cooldown"
        assert row.last_error_kind == "rate_limited"

        # And the failed CLI attempt stays as history: the parked host
        # attempt is a NEW attempt, the rate-limited one is not rewritten.
        revision_id = active_task(project, "T310").revision_id
        attempts = project.store.conn.execute(
            "SELECT profile,result,failure_reason FROM attempts"
            " WHERE role='worker' ORDER BY id"
        ).fetchall()
        by_profile = {row["profile"]: row for row in attempts}
        assert "cli-fake" in by_profile, attempts
        assert by_profile["cli-fake"]["result"] == "failed"
        assert "rate limit" in by_profile["cli-fake"]["failure_reason"]
        assert "host-worker" in by_profile, attempts
        assert by_profile["host-worker"]["result"] is None  # parked, open
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
