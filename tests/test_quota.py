"""Quota preflight + exhaustion backoff (G005).

Covers the three layers landed together:
- adapters/base.py: reset-time parsing from real harness failure text
- health/routing: quota_reset_at expiry releases an exhausted profile
- quota.py: the three live fetchers (fixture payloads captured from the real
  dashboards on 2026-10-05, docs/quota-preflight-research.md), the TTL cache,
  and the resource_status refresh (override-safe, unknown = no signal)
- dispatch: the one-hop worker rung fallback inside run_slice
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import HOST_CONFIG_TOML, HOST_PROFILES_TOML, ir_for, make_project, task_spec
from orx import dispatch, quota, routing
from orx.adapters.base import classify_failure, parse_quota_reset, quota_reset_from
from orx.records import ResourceStatus, Role
from orx import health

# The exact message codex printed on 2026-10-05 with a dead Plus 5h window
# (unicode right single quote included — it must not break anything).
CODEX_QUOTA_MESSAGE = (
    "You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
    "visit https://chatgpt.com/codex/settings/usage to purchase more credits "
    "or try again at Oct 6th, 2026 2:16 AM."
)


def _future_reset(hours: int = 3) -> tuple[str, str]:
    """(message, expected_iso): a codex-shaped usage-limit failure whose
    reset is safely in the future, plus the ISO the parser must produce.
    The fallback e2e needs a future reset — an expired one exercises the
    release gate instead (routing correctly takes the same rung back)."""
    moment = datetime.now() + timedelta(hours=hours)
    ordinal = {1: "st", 2: "nd", 3: "rd"}.get(
        moment.day % 10 if moment.day % 100 not in (11, 12, 13) else 0, "th")
    text = moment.strftime(f"%b {moment.day}{ordinal}, %Y %I:%M %p")
    message = (
        "You’ve hit your usage limit. Upgrade to Pro, or try again at " + text + "."
    )
    # The message carries minute precision; the parse lands on :00 seconds.
    expected = moment.replace(second=0, microsecond=0).astimezone() \
        .isoformat(timespec="seconds")
    return message, expected


def _run_result(stdout: str = "", stderr: str = "", exit_code: int = 1) -> SimpleNamespace:
    return SimpleNamespace(ok=exit_code == 0, exit_code=exit_code, timed_out=False,
                           stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _fresh_cache():
    quota.clear_cache()
    yield
    quota.clear_cache()


# -- reset-time parsing -----------------------------------------------------------


def test_parse_quota_reset_from_real_codex_message():
    expected = datetime.strptime("Oct 6, 2026 2:16 AM", "%b %d, %Y %I:%M %p") \
        .astimezone().isoformat(timespec="seconds")
    assert parse_quota_reset(CODEX_QUOTA_MESSAGE) == expected
    # The JSONL event wraps the sentence in quotes; the parser must see
    # through the decoration.
    event = json.dumps({"type": "turn.failed",
                        "error": {"message": CODEX_QUOTA_MESSAGE}})
    assert parse_quota_reset(event) == expected


def test_parse_quota_reset_ordinal_variants_and_markers():
    for text in (
        "resets at Jan 1st, 2027 9:05 PM",
        "resets at Feb 2nd 2027 09:05",
        "try again on Mar 3rd, 2027 23:59",
        "reset at 2027-03-04 08:30",
    ):
        assert parse_quota_reset(text) is not None, text


def test_parse_quota_reset_ignores_absent_and_garbage():
    assert parse_quota_reset("quota exhausted, upgrade now") is None
    assert parse_quota_reset("try again at some point later") is None
    assert parse_quota_reset("") is None


def test_classify_and_reset_read_the_same_streams():
    run_result = _run_result(stdout=json.dumps(
        {"type": "turn.failed", "error": {"message": CODEX_QUOTA_MESSAGE}}) + "\n")
    assert classify_failure(run_result) == "quota_exhausted"
    assert quota_reset_from(run_result) == parse_quota_reset(CODEX_QUOTA_MESSAGE)


# -- health / routing expiry gate --------------------------------------------------


def test_quota_exhaustion_active_gate(project):
    def row_with(reset=None):
        if reset is not None:
            project.store.resource_learn("cli-fake", status="exhausted",
                                         quota_reset_at=reset)
        else:
            project.store.resource_learn("cli-fake", status="exhausted")
        return project.store.resource_row("cli-fake")

    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert health.quota_exhaustion_active(row_with(future)) is True
    assert health.quota_exhaustion_active(row_with(past)) is False
    # No reset ever named: stays gated until `orx resource clear`. (A later
    # exhaustion without a printed reset keeps the last known one — released,
    # as in row_with(past) above, because resource_learn leaves the column.)
    project.store.resource_learn("cli-unknown-reset", status="exhausted")
    row = project.store.resource_row("cli-unknown-reset")
    assert row.quota_reset_at is None
    assert health.quota_exhaustion_active(row) is True
    assert health.quota_exhaustion_active(None) is False


def test_success_during_active_exhaustion_keeps_gate_recovers_after_reset(project):
    """G009 T005 recalibration of the G005 contract: one succeeded request
    during an ACTIVE quota exhaustion proves only that request at that
    moment (the incident's 08:47 trivial probe), never that the window
    recovered — so the gate and the reset stand. Once the (comparable,
    offset-carrying) reset has passed, a success re-learns available as
    before: virtual clock via the forged reset value, no waiting."""
    store = project.store
    health.record_attempt_outcome(store, "cli-fake", ok=False,
                                  error_kind="quota_exhausted", quota_reset_at="2099-01-01T00:00:00+00:00")
    assert store.resource_row("cli-fake").quota_reset_at == "2099-01-01T00:00:00+00:00"
    # A success while the exhaustion is still active (probe-shaped or real).
    health.record_attempt_outcome(store, "cli-fake", ok=True)
    row = store.resource_row("cli-fake")
    assert row.status == "exhausted", "probe success must not lift a quota blocker"
    assert row.quota_reset_at == "2099-01-01T00:00:00+00:00"
    assert row.failure_streak == 0 and row.last_success_at  # the fact stands
    # Virtual clock past the reset: the gate is gone, a success re-learns.
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store.resource_learn("cli-fake", quota_reset_at=past)
    health.record_attempt_outcome(store, "cli-fake", ok=True)
    row = store.resource_row("cli-fake")
    assert row.status == "available" and row.quota_reset_at is None


def _worker_route(project):
    return routing.route(
        project.store, project.config, project.profiles,
        routing.RouteRequest(role=Role.WORKER, required_capabilities=("coding",)),
    )


def test_routing_rejects_exhausted_until_reset_passes(project):
    store = project.store
    # Worker ladder in the fixture config: host-worker -> host-external.
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    store.resource_learn("host-worker", status="exhausted", quota_reset_at=future)
    result = _worker_route(project)
    assert result.selected == "host-external" and result.fallback_used

    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store.resource_learn("host-worker", status="exhausted", quota_reset_at=past)
    result = _worker_route(project)
    assert result.selected == "host-worker"
    rejected = next(c for c in result.candidates if c.profile == "host-worker")
    assert rejected.kept  # released, and the next outcome re-learns the truth

    store.resource_learn("host-worker", status="exhausted",
                         quota_reset_at=None)  # no reset known
    result = _worker_route(project)
    assert result.selected == "host-external"


# -- fetchers (fixture payloads from the real dashboards) ---------------------------

CODEX_PAYLOAD = {
    "plan_type": "plus",
    "rate_limit": {
        "allowed": False,
        "limit_reached": True,
        "primary_window": {"used_percent": 100, "limit_window_seconds": 18000,
                           "reset_after_seconds": 8685, "reset_at": 1791224196},
        "secondary_window": {"used_percent": 44, "limit_window_seconds": 604800,
                             "reset_after_seconds": 394253, "reset_at": 1791609764},
    },
    "credits": {"has_credits": False, "balance": "0"},
}

CURSOR_PAYLOAD = {
    "billingCycleStart": "2026-09-20T06:15:28.000Z",
    "billingCycleEnd": "2026-10-20T06:15:28.000Z",
    "membershipType": "pro",
    "isUnlimited": False,
    "individualUsage": {"plan": {
        "enabled": True, "used": 2000, "limit": 2000, "remaining": 0,
        "breakdown": {"included": 2000, "bonus": 5459, "total": 7459},
        "totalPercentUsed": 15.07,
    }, "onDemand": {"enabled": False}},
}

GLM_PAYLOAD = {
    "code": 200, "msg": "Operation successful", "success": True,
    "data": {
        "limits": [
            {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 20,
             "nextResetTime": 1791224117032},
            {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 67,
             "nextResetTime": 1791629874999},
            {"type": "TIME_LIMIT", "unit": 5, "number": 1, "usage": 4000,
             "currentValue": 420, "remaining": 3580, "percentage": 10,
             "nextResetTime": 1791425186998,
             "usageDetails": [{"modelCode": "search-prime", "usage": 413}]},
        ],
        "level": "max",
    },
}


def _fake_fetch(status: int, payload, seen: list | None = None):
    body = payload if isinstance(payload, str) else json.dumps(payload)

    def fetch(url, headers):
        if seen is not None:
            seen.append((url, headers))
        return status, body

    return fetch


def _codex_home(tmp_path: Path) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    (home / "auth.json").write_text(json.dumps(
        {"tokens": {"access_token": "tok-123", "account_id": "acct-1"}}))
    return home


def test_fetch_codex_reports_exhaustion_with_epoch_reset(tmp_path):
    snap = quota.fetch_codex(fetch=_fake_fetch(200, CODEX_PAYLOAD),
                             codex_home=_codex_home(tmp_path))
    assert snap.status == "exhausted" and snap.limit_reached is True
    assert snap.plan == "plus"
    assert snap.resets_at == "2026-10-05T18:16:36+00:00"  # reset_at epoch
    labels = {w.label: w.used_percent for w in snap.windows}
    assert labels == {"session": 100.0, "weekly": 44.0}


def test_fetch_codex_sends_bearer_and_degrades_without_auth(tmp_path):
    seen: list = []
    payload = json.loads(json.dumps(CODEX_PAYLOAD))
    payload["rate_limit"] = {"allowed": True, "limit_reached": False}
    snap = quota.fetch_codex(fetch=_fake_fetch(200, payload, seen),
                             codex_home=_codex_home(tmp_path))
    assert snap.status == "ok" and snap.resets_at is None
    url, headers = seen[0]
    assert url == "https://chatgpt.com/backend-api/wham/usage"
    assert headers["Authorization"] == "Bearer tok-123"
    assert headers["chatgpt-account-id"] == "acct-1"

    empty_home = tmp_path / "empty"
    empty_home.mkdir()
    assert quota.fetch_codex(fetch=_fake_fetch(200, CODEX_PAYLOAD),
                             codex_home=empty_home).status == "unknown"
    assert quota.fetch_codex(fetch=_fake_fetch(403, "nope"),
                             codex_home=_codex_home(tmp_path)).status == "unknown"


def _jwt(sub: str) -> str:
    def part(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return part({"alg": "none"}) + "." + part({"sub": sub}) + ".sig"


def test_fetch_cursor_builds_cookie_and_uses_breakdown_total():
    seen: list = []
    snap = quota.fetch_cursor(fetch=_fake_fetch(200, CURSOR_PAYLOAD, seen),
                              access_token=_jwt("github|user_1"))
    # included is spent but bonus covers: not exhausted, and no reset gate.
    assert snap.status == "ok" and snap.limit_reached is False
    assert snap.plan == "pro" and "15%" in snap.summary
    url, headers = seen[0]
    assert url == "https://cursor.com/api/usage-summary"
    assert headers["Cookie"].startswith(
        "WorkosCursorSessionToken=github%7Cuser_1%3A%3A")
    assert headers["Origin"] == "https://cursor.com"


def test_fetch_cursor_flags_exhaustion_at_total_and_degrades():
    spent = json.loads(json.dumps(CURSOR_PAYLOAD))
    pool = spent["individualUsage"]["plan"]
    pool["used"] = 7459  # breakdown total
    snap = quota.fetch_cursor(fetch=_fake_fetch(200, spent), access_token=_jwt("s|u"))
    assert snap.status == "exhausted"
    assert snap.resets_at == "2026-10-20T06:15:28.000Z"

    assert quota.fetch_cursor(fetch=_fake_fetch(401, {"error": "no"}),
                              access_token=_jwt("s|u")).status == "unknown"
    assert quota._cursor_cookie("not-a-jwt") is None
    snap = quota.fetch_cursor(fetch=_fake_fetch(200, CURSOR_PAYLOAD),
                              access_token="not-a-jwt")
    assert snap.status == "unknown"


def _zcode_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"provider": {
        "builtin:bigmodel-coding-plan": {"options": {
            "apiKey": "sk-glm-1", "baseURL": "https://open.bigmodel.cn/api/anthropic"}},
    }}))
    return path


def test_fetch_zcode_maps_windows_and_sends_raw_key(tmp_path):
    seen: list = []
    snap = quota.fetch_zcode(fetch=_fake_fetch(200, GLM_PAYLOAD, seen),
                             config_path=_zcode_config(tmp_path))
    assert snap.status == "ok" and snap.plan == "max"
    url, headers = seen[0]
    assert url == "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
    assert headers["Authorization"] == "sk-glm-1"  # raw key, no Bearer
    by_label = {w.label: w for w in snap.windows}
    assert by_label["session"].used_percent == 20.0
    assert by_label["weekly"].used_percent == 67.0
    assert by_label["monthly"].used_percent == pytest.approx(10.5)  # 420/4000
    assert by_label["session"].resets_at == "2026-10-05T18:15:17+00:00"


def test_fetch_zcode_flags_reached_window_and_degrades(tmp_path):
    spent = json.loads(json.dumps(GLM_PAYLOAD))
    spent["data"]["limits"][0]["percentage"] = 100
    snap = quota.fetch_zcode(fetch=_fake_fetch(200, spent), config_path=_zcode_config(tmp_path))
    assert snap.status == "exhausted"
    assert snap.resets_at == "2026-10-05T18:15:17+00:00"

    assert quota.fetch_zcode(fetch=_fake_fetch(404, "nope"),
                             config_path=_zcode_config(tmp_path)).status == "unknown"
    empty = tmp_path / "none.json"
    assert quota.fetch_zcode(fetch=_fake_fetch(200, GLM_PAYLOAD),
                             config_path=empty).status == "unknown"


def test_fetch_quota_cache_and_unknown_harness(monkeypatch):
    calls = {"n": 0}

    def counting(harness, force=False):
        calls["n"] += 1
        return quota.QuotaSnapshot(harness=harness, status="ok")

    monkeypatch.setattr(quota, "fetch_codex", lambda **_: counting("codex"))
    assert quota.fetch_quota("codex", force=True).status == "ok"
    quota.fetch_quota("codex")
    quota.fetch_quota("codex")
    assert calls["n"] == 1  # second and third reads hit the cache
    other = quota.fetch_quota("shell")
    assert other.status == "unknown" and calls["n"] == 1


# -- refresh (resource_status preflight writes) -------------------------------------


def _ladder_tomls(tmp_path: Path, primary_harness: str, fail_text: str | None = None):
    """Worker ladder cli-quota -> cli-fake. The primary always fails; its
    harness is parameterized because preflight keys on codex while the e2e
    fallback test must stay hermetic (a codex-harness profile would probe
    the real binary)."""
    if fail_text is None:
        fail_text = json.dumps(
            {"type": "turn.failed", "error": {"message": _future_reset()[0]}})
    fail_script = tmp_path / "ladder-primary.sh"
    fail_script.write_text("#!/bin/sh\necho '" + fail_text + "'\nexit 1\n")
    fail_script.chmod(0o755)
    profiles = HOST_PROFILES_TOML + f"""
[profiles.cli-quota]
driver = "cli"
harness = "{primary_harness}"
executable = "{fail_script}"
prompt_transport = "stdin"
model = "m-quota"
class = "economy"
effort = "low"
capabilities = ["coding"]
"""
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]',
        'profiles = ["cli-quota", "cli-fake"]',
    )
    return config, profiles


@pytest.fixture
def quota_project(tmp_path, monkeypatch):
    """Preflight-facing project: the ladder primary is a codex-harness
    profile (refresh and quota_report key on the harness; nothing launches)."""
    monkeypatch.chdir(tmp_path)
    config, profiles = _ladder_tomls(tmp_path, primary_harness="codex")
    project = make_project(tmp_path, config_toml=config, profiles_toml=profiles)
    yield project
    project.close()


def test_refresh_writes_exhaustion_then_recovery(quota_project, monkeypatch):
    monkeypatch.setenv("ORX_QUOTA_PREFLIGHT", "1")
    store = quota_project.store
    reached = quota.QuotaSnapshot(
        harness="codex", status="exhausted", limit_reached=True,
        resets_at="2099-01-01T00:00:00+00:00", plan="plus",
        summary="plus; session 100%, weekly 44%")
    monkeypatch.setattr(quota, "fetch_quota", lambda h, force=False: reached)

    report = quota.refresh(quota_project)
    row = store.resource_row("cli-quota")
    assert row.status == "exhausted" and row.quota_reset_at == "2099-01-01T00:00:00+00:00"
    assert report[0]["changed"] is True

    healthy = quota.QuotaSnapshot(harness="codex", status="ok", limit_reached=False,
                                  plan="plus", summary="plus; session 5%, weekly 10%")
    monkeypatch.setattr(quota, "fetch_quota", lambda h, force=False: healthy)
    report = quota.refresh(quota_project)
    row = store.resource_row("cli-quota")
    assert row.status == "available" and row.quota_reset_at is None
    assert report[0]["changed"] is True


def test_refresh_never_touches_operator_overrides(quota_project, monkeypatch):
    monkeypatch.setenv("ORX_QUOTA_PREFLIGHT", "1")
    store = quota_project.store
    store.resource_set("cli-quota", ResourceStatus.AVAILABLE, "operator says fine")
    reached = quota.QuotaSnapshot(harness="codex", status="exhausted",
                                  limit_reached=True, summary="session 100%")
    monkeypatch.setattr(quota, "fetch_quota", lambda h, force=False: reached)
    report = quota.refresh(quota_project)
    row = store.resource_row("cli-quota")
    assert row.status == "available" and bool(row.override) is True
    assert report[0]["changed"] is False and report[0]["note"] == "operator override stands"


def test_refresh_disabled_by_env(quota_project, monkeypatch):
    monkeypatch.setenv("ORX_QUOTA_PREFLIGHT", "0")
    assert quota.refresh(quota_project) == []


def test_quota_report_maps_profiles(quota_project, monkeypatch):
    snap = quota.QuotaSnapshot(harness="codex", status="ok", limit_reached=False,
                               plan="plus", summary="plus; session 5%")
    monkeypatch.setattr(quota, "fetch_quota",
                        lambda h, force=False: snap if h == "codex" else
                        quota.QuotaSnapshot(harness=h, status="unknown", error="no source"))
    data = dispatch.quota_report(quota_project)
    by_harness = {s["harness"]: s for s in data["snapshots"]}
    assert by_harness["codex"]["profiles"] == ["cli-quota"]
    assert by_harness["codex"]["status"] == "ok"
    assert by_harness["cursor"]["status"] == "unknown"
    assert dispatch.quota_report(None)["snapshots"][0]["profiles"] == []


# -- dispatch: one-hop rung fallback inside run_slice --------------------------------


def _plan_one_task(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance[:1], verification=[]),
    ]))


def test_run_slice_falls_back_to_next_rung_on_quota_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    message, expected_reset = _future_reset()
    fail_text = json.dumps({"type": "turn.failed", "error": {"message": message}})
    config, profiles = _ladder_tomls(tmp_path, primary_harness="shell",
                                     fail_text=fail_text)
    project = make_project(tmp_path, config_toml=config, profiles_toml=profiles)
    goal = dispatch.create_goal(
        project, objective="ship it", acceptance=["done"], constraints=[], context="")[0]
    _plan_one_task(project, goal)
    out = dispatch.run_slice(project)

    assert out["quota_preflight"] == []  # isolated: preflight off in tests
    assert not out["failed"]
    started = out["started"]
    assert len(started) == 1
    assert started[0]["profile"] == "cli-fake"
    assert started[0]["fallback_from"] == "cli-quota"
    assert started[0]["status"] == "passed"  # empty verification list

    # The exhausted profile carries the reset parsed from the failure text.
    row = project.store.resource_row("cli-quota")
    assert row.status == "exhausted"
    assert row.quota_reset_at == expected_reset

    # The task's latest attempt is the fallback rung; the task passed.
    run = project.store.run_for_goal(goal.id)
    revision = dispatch._active_revision(project, run)
    latest = project.store.attempt_latest_for_task(revision.id, "T001")
    assert latest.profile == "cli-fake"

    # The next slice has nothing runnable; routing skips the exhausted rung.
    again = dispatch.run_slice(project)
    assert again["started"] == [] and again["failed"] == []
    assert dispatch.status_data(project)["tasks"][0]["status"] == "passed"
    project.close()


def test_run_slice_keeps_non_resource_failures_failed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config, profiles = _ladder_tomls(
        tmp_path, primary_harness="shell", fail_text="boom: assertion exploded")
    project = make_project(tmp_path, config_toml=config, profiles_toml=profiles)
    goal = dispatch.create_goal(
        project, objective="ship it", acceptance=["done"], constraints=[], context="")[0]
    _plan_one_task(project, goal)
    out = dispatch.run_slice(project)
    assert len(out["failed"]) == 1
    assert out["failed"][0]["error_kind"] == "process_failure"
    assert "fallback_from" not in out["failed"][0]
    assert project.store.resource_row("cli-quota").status != "exhausted"
    assert dispatch.status_data(project)["tasks"][0]["status"] == "failed"
    project.close()


# ---------------------------------------------------------------------------
# G009 T005 (2026-10-07 incident): provider quota vocabulary, timezone-honest
# reset strings, and the shared-account failure domain.
#
# The incident facts (docs/cursor-system-analysis-2026-10-07.md): the 02:05
# GLM 1308 quota error carried the reset moment WITHOUT a timezone
# ("2026-10-07 04:24:23"); later failure notes appended "Z" — turning a
# provider-local time into an assumed UTC — and no evidence supports that
# annotation. The same quota failure simultaneously hit controller and
# worker (one shared provider account), and the 08:47 trivial probe that
# succeeded proved only that one request at that moment.

# The injected original shape: Chinese provider message, quota vocabulary,
# the documented error code, and the timezone-less reset string verbatim.
GLM_1308_MESSAGE = (
    "1308: 当前套餐的5小时滚动窗口额度已用尽，重置时间 2026-10-07 04:24:23，"
    "请稍后重试或升级套餐。"
)
NAIVE_RESET = "2026-10-07 04:24:23"


def test_provider_error_kind_truth_table():
    """Chinese provider vocabulary the generic English pattern pass cannot
    see (quota/rate/auth told apart), plus the GLM 1308 code shapes from the
    incident's own records; unrecognized text stays None."""
    truth = [
        (GLM_1308_MESSAGE, "quota_exhausted"),
        ("您的账户余额不足，请充值后重试", "quota_exhausted"),
        ("当前API密钥配额已耗尽", "quota_exhausted"),
        ('{"error":{"code":"1308","message":"额度已用尽"}}', "quota_exhausted"),
        ("platform error 1308, window exhausted", "quota_exhausted"),
        ("请求过于频繁，请稍后再试", "rate_limited"),
        ("已触发限流，当前请求被拒绝", "rate_limited"),
        ("用户未登录或登录已过期", "auth_required"),
        ("鉴权失败：无效的账户", "auth_required"),
        ("ordinary crash: segfault in module", None),
        ("", None),
    ]
    for text, expected in truth:
        assert quota.provider_error_kind(text) == expected, text


def test_provider_quota_reset_preserves_string_verbatim():
    """The reset moment a REMOTE provider printed, kept exactly as printed —
    a timezone-less string stays timezone-less (no invented Z, no local
    anchoring), an explicit offset stays; the marker may sit on either side
    (Chinese before/after, the incident's English note shape); a timestamp
    with no reset marker nearby is not a reset."""
    # The incident's original: naive, marker before (重置时间).
    assert quota.provider_quota_reset(GLM_1308_MESSAGE) == NAIVE_RESET
    # Marker after the token (将于 ... 重置).
    assert quota.provider_quota_reset(
        "额度耗尽，将于 2026-10-07 04:24:23 重置") == NAIVE_RESET
    # The incident's worker-note shape (English, no "at"): still the bare
    # token, never reformatted, never given a zone.
    assert quota.provider_quota_reset(
        "platform error 1308, resets 2026-10-07 04:24:23") == NAIVE_RESET
    # An offset the provider itself printed is preserved verbatim.
    assert quota.provider_quota_reset(
        "额度耗尽，重置时间 2026-10-07T04:24:23+08:00") == "2026-10-07T04:24:23+08:00"
    # No reset marker near the timestamp: not a reset.
    assert quota.provider_quota_reset("logged at 2026-10-07 04:24:23 ok") is None
    assert quota.provider_quota_reset("额度耗尽，请充值") is None
    assert quota.provider_quota_reset("") is None


def test_chinese_glm1308_end_to_end_naive_reset_never_self_releases(
        tmp_path, monkeypatch):
    """The full injection through the real dispatch stack: a CLI worker whose
    captured output is the Chinese GLM 1308 message is diagnosed
    quota_exhausted (not process_failure), the timezone-less reset is
    recorded VERBATIM (no Z), the profile stays gated slice after slice (an
    unknown-zone recovery time never auto-releases), the ladder falls to the
    next rung, and the display names the timezone as unknown."""
    monkeypatch.chdir(tmp_path)
    fail_script = tmp_path / "glm-fail.sh"
    fail_script.write_text(
        "#!/bin/sh\necho '" + GLM_1308_MESSAGE + "'\nexit 1\n", encoding="utf-8")
    fail_script.chmod(0o755)
    profiles = HOST_PROFILES_TOML + (
        "\n[profiles.cli-glm]\n"
        'driver = "cli"\n'
        'harness = "shell"\n'
        f'executable = "{fail_script}"\n'
        'prompt_transport = "stdin"\n'
        'model = "m-glm"\n'
        'class = "economy"\n'
        'effort = "low"\n'
        'capabilities = ["coding"]\n'
    )
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]',
        'profiles = ["cli-glm", "cli-fake"]',
    )
    project = make_project(tmp_path, config_toml=config, profiles_toml=profiles)
    try:
        goal = dispatch.create_goal(
            project, objective="inject GLM 1308", acceptance=["done"],
            constraints=[], context="")[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=goal.acceptance[:1], verification=[]),
        ]))
        out = dispatch.run_slice(project)

        # Diagnosed as a resource failure and rerouted in the same slice.
        started = out["started"]
        assert len(started) == 1 and started[0]["profile"] == "cli-fake"
        assert started[0]["fallback_from"] == "cli-glm"

        # The exhaustion carries the reset VERBATIM — timezone-less.
        row = project.store.resource_row("cli-glm")
        assert row.status == "exhausted"
        assert row.quota_reset_at == NAIVE_RESET
        assert row.last_error_kind == "quota_exhausted"
        assert health.quota_reset_tz_source(row.quota_reset_at) == "naive"

        # The original message text survives in the recorded attempt.
        revision = dispatch._active_revision(
            project, project.store.run_for_goal(goal.id))
        attempts = project.store.conn.execute(
            "SELECT profile, failure_reason FROM attempts"
            " WHERE role='worker' ORDER BY id").fetchall()
        glm = next(a for a in attempts if a["profile"] == "cli-glm")
        assert "额度已用尽" in glm["failure_reason"]
        assert revision is not None

        # Unknown-zone recovery time: gated now AND on later slices (no
        # fake available, no auto-release) until explicit re-eligibility.
        again = dispatch.run_slice(project)
        assert again["started"] == [] and again["failed"] == []
        assert health.quota_exhaustion_active(row) is True

        # The operator view names the timezone as unknown.
        entries = {e["profile"]: e
                   for e in dispatch.agent_status(project)["profiles"]}
        assert "timezone unknown" in entries["cli-glm"]["reason"]

        # Explicit re-eligibility (operator decision) re-opens routing.
        project.store.resource_set("cli-glm", ResourceStatus.AVAILABLE,
                                   "operator topped up")
        assert health.quota_exhaustion_active(
            project.store.resource_row("cli-glm")) is False
    finally:
        project.close()


def test_refresh_healthy_preflight_lifts_naive_reset_exhaustion(
        quota_project, monkeypatch):
    """A live dashboard report is a quota fact, not a probe: preflight
    reporting the window healthy re-opens a naive-reset exhaustion (one
    explicit re-eligibility path), while an operator override still stands
    above it."""
    monkeypatch.setenv("ORX_QUOTA_PREFLIGHT", "1")
    store = quota_project.store
    # Stage the incident's state directly: exhausted with a naive reset.
    store.resource_learn("cli-quota", status="exhausted",
                         quota_reset_at=NAIVE_RESET,
                         last_error_kind="quota_exhausted")
    healthy = quota.QuotaSnapshot(harness="codex", status="ok",
                                  limit_reached=False, plan="plus",
                                  summary="plus; session 5%, weekly 10%")
    monkeypatch.setattr(quota, "fetch_quota", lambda h, force=False: healthy)
    report = quota.refresh(quota_project)
    row = store.resource_row("cli-quota")
    assert row.status == "available" and row.quota_reset_at is None
    assert report[0]["changed"] is True

    # The operator outranks the dashboard even on this path.
    store.resource_learn("cli-quota", status="exhausted",
                         quota_reset_at=NAIVE_RESET)
    store.resource_set("cli-quota", ResourceStatus.EXHAUSTED, "operator knows")
    reached = quota.QuotaSnapshot(harness="codex", status="ok",
                                  limit_reached=False, plan="plus",
                                  summary="plus; session 5%")
    monkeypatch.setattr(quota, "fetch_quota", lambda h, force=False: reached)
    quota.refresh(quota_project)
    row = store.resource_row("cli-quota")
    assert row.status == "exhausted" and bool(row.override) is True


def _domain_ladder_tomls(tmp_path: Path):
    """Worker ladder glm-a (fails with the Chinese GLM 1308 message) ->
    glm-b (healthy, SAME quota domain) -> free-c (healthy, standalone)."""
    fail_script = tmp_path / "glm-a.sh"
    fail_script.write_text(
        "#!/bin/sh\necho '" + GLM_1308_MESSAGE + "'\nexit 1\n", encoding="utf-8")
    fail_script.chmod(0o755)
    ok_script = tmp_path / "ok.sh"
    ok_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ok_script.chmod(0o755)

    def shell_profile(name: str, executable: Path, domain: str | None) -> str:
        block = (
            f"\n[profiles.{name}]\n"
            'driver = "cli"\n'
            'harness = "shell"\n'
            f'executable = "{executable}"\n'
            'prompt_transport = "stdin"\n'
            'model = "m"\n'
            'class = "economy"\n'
            'effort = "low"\n'
            'capabilities = ["coding"]\n'
        )
        if domain is not None:
            block += f'quota_domain = "{domain}"\n'
        return block

    profiles = (HOST_PROFILES_TOML
                + shell_profile("glm-a", fail_script, "glm-main")
                + shell_profile("glm-b", ok_script, "glm-main")
                + shell_profile("free-c", ok_script, None))
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]',
        'profiles = ["glm-a", "glm-b", "free-c"]',
    )
    return config, profiles


def test_shared_domain_reroute_skips_sibling_rung(tmp_path, monkeypatch):
    """Shared account, multiple profiles: when one profile's GLM 1308 marks
    the shared quota domain, the immediate resource reroute must NOT land on
    a sibling of the same domain as if it were independent capacity — it
    falls through to a rung outside the domain."""
    monkeypatch.chdir(tmp_path)
    config, profiles = _domain_ladder_tomls(tmp_path)
    project = make_project(tmp_path, config_toml=config, profiles_toml=profiles)
    try:
        goal = dispatch.create_goal(
            project, objective="shared domain reroute", acceptance=["done"],
            constraints=[], context="")[0]
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T001", acceptance=goal.acceptance[:1], verification=[]),
        ]))
        out = dispatch.run_slice(project)
        # The sibling rung was never launched: no attempt for glm-b.
        started = out["started"]
        assert [s["profile"] for s in started] == ["free-c"]
        assert started[0]["fallback_from"] == "glm-a"
        attempts = project.store.conn.execute(
            "SELECT DISTINCT profile FROM attempts WHERE role='worker'"
        ).fetchall()
        launched = {a["profile"] for a in attempts}
        assert "glm-b" not in launched, launched
        # The domain gate is visible in the routing audit.
        decision = project.store.routing_decisions_all()[-1]
        sibling = next(c for c in decision.candidates if c["profile"] == "glm-b")
        assert sibling["reject_reason"] == "quota_domain_exhausted:glm-main"
        assert sibling["kept"] is False
    finally:
        project.close()
