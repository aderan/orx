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


# ---------------------------------------------------------------------------
# G009 T002 (2026-10-07 incident): health learning semantics for CLI worker
# deliveries. The health seam may only learn PROVEN facts: an exit-0 run
# whose delivery is blocked — or missing/malformed/contradictory
# (protocol_error) — proves neither a healthy profile nor an account
# failure, so NOTHING is learned: an environment block must never clear
# failure streaks or cooldowns, and must never gate the profile either.
# A passed delivery with a red controller gate is a code fact: the profile
# served the request and must not be marked unavailable.


def _g009_cursor_payload(result_text: str) -> dict:
    """A Cursor-shaped success envelope around an arbitrary worker result
    text (the incident's transport shape: process exit 0, response ok)."""
    return {
        "type": "result", "subtype": "success", "is_error": False,
        "session_id": "g009-session",
        "result": result_text,
        "usage": {"inputTokens": 10, "outputTokens": 4, "cacheReadTokens": 50},
    }


G009_BLOCKED_TEXT = (
    "blocked: the start gate cannot run in this environment.\n\n"
    '```json\n{"status":"blocked","summary":"shell rejected",'
    '"checks":[],"artifacts":[]}\n```'
)
G009_PASSED_TEXT = (
    '```json\n{"status":"passed","summary":"done",'
    '"checks":[],"artifacts":[]}\n```'
)


def _g009_run_cursor_worker(tmp_path, monkeypatch, result_text: str,
                            check: str = "true", prepare=None):
    """One cursor-harness CLI worker attempt through the REAL dispatch stack
    (routing, launch seam, health seam, delivery gate, state machine) with
    only the model process replaced by a captured envelope. ``prepare`` runs
    after the project exists but before ``run_slice`` — the hook the
    health-state tests use to seed a failure streak or cooldown into the
    very store the attempt will learn against. Returns the open project;
    the caller closes it."""
    import json as _json
    from conftest import (HOST_CONFIG_TOML, HOST_PROFILES_TOML, ir_for,
                          make_project, task_spec)
    from orx import adapters, runtime
    from orx.adapters.base import Launch, ProbeReport

    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]', 'profiles = ["cli-fake"]')
    profiles = HOST_PROFILES_TOML.replace('harness = "shell"', 'harness = "cursor"')
    project = make_project(tmp_path, config, profiles)
    adapter = adapters.get_adapter("cursor")
    monkeypatch.setattr(adapter, "probe",
                        lambda: ProbeReport(True, "captured CLI stand-in"))
    monkeypatch.setattr(
        adapter, "build_worker_launch",
        lambda **kwargs: Launch(argv=["captured-agent"], cwd=tmp_path, timeout=5))
    monkeypatch.setattr(
        runtime, "run_launch",
        lambda launch: runtime.CommandResult(
            command="captured-agent", exit_code=0,
            stdout=_json.dumps(_g009_cursor_payload(result_text)),
            stderr="", duration_sec=0.01, timed_out=False))
    goal = dispatch.create_goal(project, "health replay", ["a1"])[0]
    dispatch.submit_plan(project, ir_for(goal, [task_spec(
        "T001", acceptance=goal.acceptance, verification=[check], complexity="low")]))
    if prepare is not None:
        prepare(project)
    dispatch.run_slice(project)
    return project


def test_blocked_cli_delivery_never_clears_failure_state(tmp_path, monkeypatch):
    """The incident's exact poisoning, pinned: an exit-0 response whose
    worker delivery says blocked must not be learned as a success — the
    failure streak, last error kind, and status are untouched."""
    seen = {}

    def prepare(project):
        from orx import health as health_mod
        # Two temporary failures: streak 2, still routable (threshold 3).
        health_mod.record_attempt_outcome(project.store, "cli-fake", ok=False,
                                          error_kind="temporary_failure")
        health_mod.record_attempt_outcome(project.store, "cli-fake", ok=False,
                                          error_kind="temporary_failure")
        seen["before"] = project.store.resource_row("cli-fake")
        assert seen["before"].failure_streak == 2

    project = _g009_run_cursor_worker(
        tmp_path, monkeypatch, G009_BLOCKED_TEXT, prepare=prepare)
    try:
        after = project.store.resource_row("cli-fake")
        assert after.failure_streak == 2, (
            "an exit-0 blocked delivery must not clear the failure streak")
        assert after.last_error_kind == "temporary_failure"
        assert after.status == seen["before"].status
    finally:
        project.close()


def test_health_seam_preserves_cooldown_for_blocked_and_protocol_error(tmp_path, monkeypatch):
    """Seam-level pin of the cooldown rule (a cooldown profile is not
    routable, so the end-to-end replay cannot stage it): with a live
    cooldown row, an exit-0 blocked or protocol_error delivery learns
    NOTHING — the retry moment is untouched, the error kind stands — while
    a passed delivery still clears it. The seam call is exactly the one
    the CLI worker path makes (same arguments, same delivery facts)."""
    from orx.runtime import CommandResult
    project, store = _store_with(tmp_path, monkeypatch, profiles=("cli-fake",))
    try:
        ok_run = CommandResult(command="x", exit_code=0, stdout="", stderr="",
                               duration_sec=0.0, timed_out=False)
        for outcome in ("blocked", "protocol_error"):
            health.record_attempt_outcome(store, "cli-fake", ok=False,
                                          error_kind="rate_limited")
            row = store.resource_row("cli-fake")
            assert row.status == "cooldown" and row.cooldown_until, outcome
            until = row.cooldown_until
            dispatch._record_attempt_health(
                store, "cli-fake", ok_run, delivery_outcome=outcome)
            after = store.resource_row("cli-fake")
            assert after.status == "cooldown", outcome
            assert after.cooldown_until == until, outcome
            assert after.last_error_kind == "rate_limited", outcome
            store.resource_clear("cli-fake")  # reset for the next outcome

        # The success transition still fires when the delivery proves it.
        health.record_attempt_outcome(store, "cli-fake", ok=False,
                                      error_kind="rate_limited")
        dispatch._record_attempt_health(
            store, "cli-fake", ok_run, delivery_outcome="passed")
        cleared = store.resource_row("cli-fake")
        assert cleared.status == "available"
        assert cleared.cooldown_until is None
        assert cleared.failure_streak == 0
    finally:
        project.close()


def test_protocol_error_delivery_learns_nothing(tmp_path, monkeypatch):
    """A missing delivery (a132's natural-language blocker) is a protocol
    failure, not a resource fact: the seeded row is untouched — no success
    learned, no failure streak, no gate."""
    project = _g009_run_cursor_worker(
        tmp_path, monkeypatch,
        "I could not run the checks: every shell invocation was rejected.")
    try:
        row = project.store.resource_row("cli-fake")
        # Nothing was learned in either direction: no success write (an
        # "available" row), no failure write (a streak or an error kind).
        # With no prior row, absence is itself the proof.
        assert row is None or (
            row.status == "unknown" and row.failure_streak == 0
            and row.last_error_kind is None
        ), row
    finally:
        project.close()


def test_passed_cli_delivery_with_red_gate_keeps_profile_healthy(tmp_path, monkeypatch):
    """Code red is not account red: a passed delivery whose controller gate
    is red keeps the profile available — the request was served; the red
    gate is a code fact for the task, never a resource transition."""
    project = _g009_run_cursor_worker(tmp_path, monkeypatch, G009_PASSED_TEXT,
                                      check="false")
    try:
        row = project.store.resource_row("cli-fake")
        assert row.status == "available"
        assert row.failure_streak == 0
        assert row.last_error_kind is None
        task = project.store.conn.execute(
            "SELECT status FROM tasks WHERE task_id='T001'").fetchone()
        assert task["status"] == "failed"
    finally:
        project.close()


def test_passed_cli_delivery_clears_the_failure_streak(tmp_path, monkeypatch):
    """Learning still works when the capability IS proven: a passed delivery
    on a served, gated-green request clears the failure streak — the
    success transition requires the delivery fact, and got it."""
    def prepare(project):
        health.record_attempt_outcome(project.store, "cli-fake", ok=False,
                                      error_kind="temporary_failure")
        health.record_attempt_outcome(project.store, "cli-fake", ok=False,
                                      error_kind="temporary_failure")
        assert project.store.resource_row("cli-fake").failure_streak == 2

    project = _g009_run_cursor_worker(
        tmp_path, monkeypatch, G009_PASSED_TEXT, prepare=prepare)
    try:
        row = project.store.resource_row("cli-fake")
        assert row.status == "available"
        assert row.failure_streak == 0
        assert row.last_error_kind is None
    finally:
        project.close()


# ---------------------------------------------------------------------------
# G009 T004: recovery failure classes, same-condition blocker fingerprints,
# and the bounded same-blocker streak. The incident (a131/a132/a133): three
# consecutive CLI attempts on the SAME profile hit the SAME permission
# blocker (shell approval denied); ORX kept re-launching with retry prompts
# that misdiagnosed it as code failure. Recovery decisions must key on
# (task, profile, blocker class, condition fingerprint): a blocker whose
# conditions have not changed must not be re-launched after a bounded
# number of attempts, while code-red failures keep every repair path open.
#
# Quota/rate-limit facts stay resource facts (health + the G005 reroute,
# T005's domain): they are classified here only so the taxonomy can tell
# them apart from blockers.


@pytest.mark.parametrize("result,reason,expected", [
    # The incident's shape: an exit-0 blocked delivery whose summary names
    # the permission system.
    ("blocked",
     "delivery blocked (environment/tool blocked): shell approval denied;"
     " every shell invocation was rejected before execution",
     "permission"),
    ("blocked",
     "delivery blocked (environment/tool blocked): command was rejected by"
     " the sandbox policy",
     "permission"),
    # A blocked delivery that is environmental but not a permission fact.
    ("blocked",
     "delivery blocked (environment/tool blocked): pytest binary missing"
     " from PATH",
     "environment"),
    # Worker-reported code failures and red controller gates: repairable.
    ("failed",
     "delivery failed (worker-reported failure): cannot produce c.marker"
     " within this scope",
     "code"),
    ("failed",
     "delivery gate: 2 of 3 command check(s) red: pytest -q exit 1",
     "code"),
    # Resource facts (T005's domain): transport text carrying the standard
    # vocabulary.
    ("failed", "exit 1: rate limit exceeded", "resource"),
    ("failed", "exit 1: usage limit reached; try again at Oct 8th, 2026", "resource"),
    # Environment facts from transport text: auth and model availability.
    ("failed", "exit 1: Error: Not logged in", "environment"),
    ("failed", "exit 1: cannot use this model on this plan", "environment"),
    # A permission blocker that surfaces as a transport failure instead of
    # a delivery document.
    ("failed", "exit 1: internal shell approval denied by policy", "permission"),
    # Unproven facts stay unclassified: no suppression either way.
    ("protocol_error", "delivery protocol_error (no delivery document)", "other"),
    ("failed", "exit 1: segfault somewhere unrelated", "other"),
    (None, None, "other"),
])
def test_failure_class_taxonomy(result, reason, expected):
    """Truth table for the recovery classification: permission/environment
    are blocker classes (same-condition re-launch cannot help), code keeps
    every repair path open, resource belongs to the health/reroute seam,
    anything unproven is 'other' and never suppresses."""
    assert health.failure_class(result, reason) == expected


def test_blocker_classes_are_permission_and_environment_only():
    assert health.BLOCKER_CLASSES == ("permission", "environment")


def test_blocker_fingerprint_is_deterministic_and_condition_keyed():
    """The fingerprint names the CONDITION, not the wording: identical
    blocker facts hash identically across calls (deterministic), the
    transport 'exit N:' prefix and the delivery-result prefix are noise,
    and a different profile, harness, class, or core reason is a different
    condition."""
    a = health.blocker_fingerprint(
        "cursor-strong", "cursor", "permission",
        "delivery blocked (environment/tool blocked): shell approval denied"
        " before execution")
    b = health.blocker_fingerprint(
        "cursor-strong", "cursor", "permission",
        "exit 1: shell approval denied   before execution")
    c = health.blocker_fingerprint(
        "cursor-strong", "cursor", "permission",
        "delivery blocked (environment/tool blocked): shell approval denied"
        " before execution")
    assert a == b == c
    assert len(a) == 16  # a stable, displayable digest

    # Different profile / harness / class / core reason: different condition.
    assert health.blocker_fingerprint(
        "cursor-frontier", "cursor", "permission", "shell approval denied"
    ) != a
    assert health.blocker_fingerprint(
        "cursor-strong", "codex", "permission", "shell approval denied"
    ) != a
    assert health.blocker_fingerprint(
        "cursor-strong", "cursor", "environment", "shell approval denied"
    ) != a
    assert health.blocker_fingerprint(
        "cursor-strong", "cursor", "permission", "pytest binary missing"
    ) != a


def _blocker_attempt(store, revision_id, task_id, *, profile, harness="cursor",
                     result="blocked", reason="delivery blocked (environment/tool"
                     " blocked): shell approval denied before execution"):
    attempt = store.attempt_create(
        revision_row_id=revision_id, role="worker", profile=profile,
        driver="cli", harness=harness, model_id="m", requested_effort="medium",
        task_id=task_id,
    )
    store.attempt_update(attempt.id, ended_at="2026-10-07T00:00:00+00:00",
                         result=result, failure_reason=reason)
    return attempt


def _planned_single_task(project, task_id="T001"):
    goal = dispatch.create_goal(project, "blocker streaks", ["a1"])[0]
    dispatch.submit_plan(project, __import__("conftest").ir_for(goal, [
        __import__("conftest").task_spec(task_id, acceptance=["a1"],
                                         verification=["true"]),
    ]))
    from conftest import active_task
    return active_task(project, task_id)


def test_blocker_streak_counts_trailing_same_fingerprint_per_profile(
        tmp_path, monkeypatch):
    """The streak is the trailing run of blocker failures with the SAME
    fingerprint on ONE profile of ONE task — exactly the fact the incident
    lacked. A different fingerprint, class, or an intervening success on
    that profile breaks the run; another profile's attempts are that
    profile's own streak, never this one's."""
    monkeypatch.chdir(tmp_path)
    from conftest import make_project
    project = make_project(tmp_path)
    try:
        task = _planned_single_task(project)
        rid = task.revision_id

        for _ in range(3):
            _blocker_attempt(project.store, rid, "T001", profile="cursor-strong")
        streak = health.blocker_streak(project.store, rid, "T001", "cursor-strong")
        assert streak is not None
        assert streak.count == 3
        assert streak.failure_class == "permission"
        assert streak.profile == "cursor-strong"
        assert "shell approval denied" in streak.latest_reason

        # A success on the profile breaks the trailing run.
        ok = project.store.attempt_create(
            revision_row_id=rid, role="worker", profile="cursor-strong",
            driver="cli", harness="cursor", model_id="m",
            requested_effort="medium", task_id="T001")
        project.store.attempt_update(ok.id, ended_at="2026-10-07T01:00:00+00:00",
                                     result="completed")
        assert health.blocker_streak(
            project.store, rid, "T001", "cursor-strong") is None

        # A code failure after two blockers also breaks the run.
        _blocker_attempt(project.store, rid, "T001", profile="cursor-strong")
        _blocker_attempt(project.store, rid, "T001", profile="cursor-strong")
        code = project.store.attempt_create(
            revision_row_id=rid, role="worker", profile="cursor-strong",
            driver="cli", harness="cursor", model_id="m",
            requested_effort="medium", task_id="T001")
        project.store.attempt_update(
            code.id, ended_at="2026-10-07T02:00:00+00:00", result="failed",
            failure_reason="delivery gate: 1 of 1 command check(s) red")
        assert health.blocker_streak(
            project.store, rid, "T001", "cursor-strong") is None

        # A DIFFERENT blocker condition starts its own streak at 1.
        _blocker_attempt(
            project.store, rid, "T001", profile="cursor-strong",
            reason="delivery blocked (environment/tool blocked): pytest"
                   " binary missing from PATH")
        streak = health.blocker_streak(project.store, rid, "T001", "cursor-strong")
        assert streak.count == 1
        assert streak.failure_class == "environment"

        # Another profile's blockers never count for this profile.
        _blocker_attempt(project.store, rid, "T001", profile="codex-frontier",
                         harness="codex")
        other = health.blocker_streak(
            project.store, rid, "T001", "codex-frontier")
        assert other is not None and other.count == 1
        own = health.blocker_streak(
            project.store, rid, "T001", "cursor-strong")
        assert own.count == 1
    finally:
        project.close()
