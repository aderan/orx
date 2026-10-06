"""Live quota preflight for the CLI harnesses (G005).

One snapshot per harness (codex / cursor / zcode), fetched from the same
unofficial dashboards orca reads (mechanisms and captured payloads:
docs/quota-preflight-research.md):

- codex  GET chatgpt.com/backend-api/wham/usage — Bearer token from
  ``~/.codex/auth.json``; reports ``rate_limit.limit_reached`` plus the
  5-hour and weekly windows with exact ``reset_at`` epochs.
- cursor GET cursor.com/api/usage-summary — the login-keychain JWT as a
  ``WorkosCursorSessionToken`` cookie (Origin/Referer are mandatory);
  reports the billing-cycle pool (included/bonus breakdown).
- zcode  GET <bigmodel baseURL>/api/monitor/usage/quota/limit — API key
  from ``~/.zcode/v2/config.json``; reports GLM plan windows with
  ``nextResetTime`` per window.

Everything is best effort: credentials may be missing, endpoints may
change, the network may be down. A fetcher never raises — failures return
an ``unknown`` snapshot, and callers treat that as "no signal" and leave
learned state untouched. Credentials are read for the request only; they
never appear in snapshots, summaries, or error strings.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Harnesses with a live quota source. Everything else (shell, generic
# profiles) has none by definition.
QUOTA_HARNESSES: tuple[str, ...] = ("codex", "cursor", "zcode")

# Kill switch for tests and offline machines: ORX_QUOTA_PREFLIGHT=0 makes
# refresh() a no-op (fetch_quota still works when called directly).
PREFLIGHT_ENV = "ORX_QUOTA_PREFLIGHT"

_FETCH_TIMEOUT_SEC = 10
_CACHE_TTL_SEC = 60
# The GLM endpoint reports integer percentages; the dashboard's own
# "reached" display is the 100% window. Half a percent of slack absorbs
# provider rounding without ever gating on a healthy 99% window.
_REACHED_PERCENT = 99.5

# (status_code, body) from a URL; transport errors propagate to the fetcher's
# own try/except. Injectable so tests never touch the network.
Fetch = Callable[[str, dict[str, str]], tuple[int, str]]


@dataclass(frozen=True)
class QuotaWindow:
    label: str  # 'session' | 'weekly' | 'monthly' | 'billing_cycle' | 'window'
    used_percent: float | None = None
    resets_at: str | None = None  # ISO 8601
    detail: str | None = None


@dataclass(frozen=True)
class QuotaSnapshot:
    harness: str
    status: str  # 'ok' | 'exhausted' | 'unknown'
    limit_reached: bool | None = None
    resets_at: str | None = None  # earliest blocking reset, ISO 8601
    plan: str | None = None
    summary: str = ""
    windows: tuple[QuotaWindow, ...] = ()
    error: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {
            "harness": self.harness,
            "status": self.status,
            "limit_reached": self.limit_reached,
            "resets_at": self.resets_at,
            "plan": self.plan,
            "summary": self.summary,
            "windows": [asdict(w) for w in self.windows],
        }
        if self.error:
            out["error"] = self.error
        if self.extra:
            out.update(self.extra)
        return out


def _unknown(harness: str, error: str) -> QuotaSnapshot:
    return QuotaSnapshot(harness=harness, status="unknown", error=error[:200])


def _brief(exc: Exception) -> str:
    """Short, credential-free reason for an unexpected transport error."""
    return f"{type(exc).__name__}: {exc}"[:200]


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _epoch_to_iso(seconds) -> str | None:
    value = _num(seconds)
    if value is None or value <= 0:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="seconds")


def _ms_to_iso(millis) -> str | None:
    value = _num(millis)
    if value is None or value <= 0:
        return None
    return _epoch_to_iso(value / 1000)


def _urllib_fetch(url: str, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SEC) as response:
        return response.status, response.read().decode("utf-8", "replace")


def _get_json(fetch: Fetch, url: str, headers: dict[str, str]) -> tuple[int, dict | None]:
    status, body = fetch(url, headers)
    try:
        payload = json.loads(body)
    except ValueError:
        return status, None
    return status, payload if isinstance(payload, dict) else None


# -- codex ----------------------------------------------------------------------

_CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def _codex_headers(codex_home: Path | None) -> tuple[dict[str, str], str | None]:
    home = codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        auth = json.loads((home / "auth.json").read_text())
    except (OSError, ValueError):
        return {}, "codex auth.json not readable"
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    token = tokens.get("access_token") if isinstance(tokens, dict) else None
    if not isinstance(token, str) or not token.strip():
        return {}, "codex auth.json has no access token"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "codex-cli",
        "Accept": "application/json",
    }
    account = tokens.get("account_id")
    if isinstance(account, str) and account.strip():
        headers["chatgpt-account-id"] = account
    return headers, None


def _codex_snapshot(payload: dict) -> QuotaSnapshot:
    rate = payload.get("rate_limit")
    rate = rate if isinstance(rate, dict) else {}
    primary = rate.get("primary_window")
    primary = primary if isinstance(primary, dict) else {}
    secondary = rate.get("secondary_window")
    secondary = secondary if isinstance(secondary, dict) else {}
    windows = (
        QuotaWindow("session", _num(primary.get("used_percent")),
                    _epoch_to_iso(primary.get("reset_at"))),
        QuotaWindow("weekly", _num(secondary.get("used_percent")),
                    _epoch_to_iso(secondary.get("reset_at"))),
    )
    reached = rate.get("limit_reached") is True or rate.get("allowed") is False
    plan = payload.get("plan_type")
    plan = plan if isinstance(plan, str) else None
    session_pct = windows[0].used_percent
    weekly_pct = windows[1].used_percent

    def pct(value: float | None) -> str:
        return "?" if value is None else f"{value:.0f}%"

    return QuotaSnapshot(
        harness="codex",
        status="exhausted" if reached else "ok",
        limit_reached=reached,
        resets_at=windows[0].resets_at if reached else None,
        plan=plan,
        summary=(f"{plan or 'plan?'}; session {pct(session_pct)},"
                 f" weekly {pct(weekly_pct)}"),
        windows=windows,
    )


def fetch_codex(fetch: Fetch | None = None, codex_home: Path | None = None) -> QuotaSnapshot:
    fetch = fetch or _urllib_fetch
    try:
        headers, why = _codex_headers(codex_home)
        if not headers:
            return _unknown("codex", why or "codex credentials unavailable")
        status, payload = _get_json(fetch, _CODEX_USAGE_URL, headers)
        if status != 200:
            return _unknown("codex", f"wham/usage HTTP {status}")
        if payload is None or not isinstance(payload.get("plan_type"), str):
            return _unknown("codex", "unrecognized wham/usage payload")
        return _codex_snapshot(payload)
    except Exception as exc:  # preflight must never raise into dispatch
        return _unknown("codex", _brief(exc))


# -- cursor ---------------------------------------------------------------------

_CURSOR_DASHBOARD = "https://cursor.com"
_CURSOR_USAGE_URL = f"{_CURSOR_DASHBOARD}/api/usage-summary"
# cursor-agent 2026.06+ keeps the session token in the login keychain
# (service/account names from orca's cursor-auth.ts).
_CURSOR_KEYCHAIN = ("cursor-access-token", "cursor-user")


def _read_cursor_keychain_token() -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", _CURSOR_KEYCHAIN[0], "-a", _CURSOR_KEYCHAIN[1], "-w"],
            capture_output=True, text=True, timeout=_FETCH_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = result.stdout.strip() if result.returncode == 0 else ""
    return token or None


def _cursor_cookie(token: str) -> str | None:
    """Dashboard session cookie from the bare login JWT: the cookie value is
    `<urlencoded WorkOS subject>::<urlencoded jwt>` and the subject lives in
    the JWT's own `sub` claim."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return None
    subject = claims.get("sub") if isinstance(claims, dict) else None
    if not isinstance(subject, str) or not subject:
        return None
    return (
        "WorkosCursorSessionToken="
        + urllib.parse.quote(subject, safe="")
        + "%3A%3A"
        + urllib.parse.quote(token, safe="")
    )


def _cursor_snapshot(payload: dict) -> QuotaSnapshot:
    usage = payload.get("individualUsage")
    pool = usage.get("plan") if isinstance(usage, dict) else None
    pool = pool if isinstance(pool, dict) else {}
    breakdown = pool.get("breakdown")
    breakdown = breakdown if isinstance(breakdown, dict) else {}
    used = _num(pool.get("used"))
    included = _num(breakdown.get("included"))
    total = _num(breakdown.get("total")) or _num(pool.get("limit"))
    reached = bool(used is not None and total is not None and total > 0 and used >= total)
    cycle_end = payload.get("billingCycleEnd")
    cycle_end = cycle_end if isinstance(cycle_end, str) else None
    plan = payload.get("membershipType")
    plan = plan if isinstance(plan, str) else None
    percent = _num(pool.get("totalPercentUsed"))

    def amount(value: float | None) -> str:
        return "?" if value is None else f"{value:.0f}"

    return QuotaSnapshot(
        harness="cursor",
        status="exhausted" if reached else "ok",
        limit_reached=reached,
        # The pool refills at the next billing cycle; that is the earliest
        # recovery the dashboard names.
        resets_at=cycle_end if reached else None,
        plan=plan,
        summary=(f"{plan or 'plan?'}; cycle {amount(percent)}% used"
                 f" ({amount(used)}/{amount(total)}, included {amount(included)})"),
        windows=(QuotaWindow("billing_cycle", percent, cycle_end),),
    )


def fetch_cursor(fetch: Fetch | None = None,
                 access_token: str | None = None) -> QuotaSnapshot:
    fetch = fetch or _urllib_fetch
    try:
        token = access_token if access_token is not None else _read_cursor_keychain_token()
        if not token:
            return _unknown("cursor", "cursor session token unavailable (keychain)")
        cookie = _cursor_cookie(token)
        if not cookie:
            return _unknown("cursor", "cursor token is not a usable session JWT")
        headers = {
            "Cookie": cookie,
            "Accept": "application/json",
            "Origin": _CURSOR_DASHBOARD,
            "Referer": f"{_CURSOR_DASHBOARD}/dashboard",
            "User-Agent": "Mozilla/5.0",
        }
        status, payload = _get_json(fetch, _CURSOR_USAGE_URL, headers)
        if status == 401:
            return _unknown("cursor", "cursor session expired (cursor-agent login)")
        if status != 200:
            return _unknown("cursor", f"usage-summary HTTP {status}")
        if payload is None or "individualUsage" not in payload:
            return _unknown("cursor", "unrecognized usage-summary payload")
        return _cursor_snapshot(payload)
    except Exception as exc:
        return _unknown("cursor", _brief(exc))


# -- zcode / GLM coding plan ------------------------------------------------------

_ZCODE_CONFIG_PATHS = (".zcode/v2/config.json", ".zcode/cli/config.json")
_ZCODE_SUPPORTED_HOSTS = {"api.z.ai", "open.bigmodel.cn", "dev.bigmodel.cn"}
_ZCODE_QUOTA_PATH = "/api/monitor/usage/quota/limit"


def _zcode_credentials(config_path: Path | None) -> tuple[str, dict[str, str], str | None]:
    """(url, headers, error). The key with coding-plan in its provider name
    wins; any other key on a supported bigmodel host is a fallback. z.ai
    plan-proxy hosts are ignored — they do not serve the monitor API."""
    paths = [config_path] if config_path else [Path.home() / p for p in _ZCODE_CONFIG_PATHS]
    best: tuple[int, str, urllib.parse.ParseResult, str] | None = None
    for path in paths:
        try:
            config = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        providers = config.get("provider")
        if not isinstance(providers, dict):
            continue
        for name, entry in providers.items():
            options = entry.get("options") if isinstance(entry, dict) else None
            options = options if isinstance(options, dict) else {}
            key = options.get("apiKey")
            base = options.get("baseURL")
            if not (isinstance(key, str) and key.strip() and isinstance(base, str)):
                continue
            origin = urllib.parse.urlparse(base)
            if origin.scheme != "https" or origin.hostname not in _ZCODE_SUPPORTED_HOSTS:
                continue
            rank = 0 if "coding-plan" in name else 1
            if best is None or rank < best[0]:
                best = (rank, key, origin, name)
        if best is not None:
            break
    if best is None:
        return "", {}, "no bigmodel coding-plan apiKey in ~/.zcode config"
    _, key, origin, _name = best
    url = f"{origin.scheme}://{origin.netloc}{_ZCODE_QUOTA_PATH}"
    headers = {
        # The monitor API takes the raw key as the Authorization value.
        "Authorization": key,
        "Accept": "application/json",
        "Accept-Language": "en-US,en",
        "Content-Type": "application/json",
        "User-Agent": "zcode-cli",
    }
    return url, headers, None


def _glm_window_label(limit: dict) -> str:
    if limit.get("type") == "TIME_LIMIT":
        return "monthly"
    unit = _num(limit.get("unit"))
    if limit.get("type") == "TOKENS_LIMIT":
        if unit == 3:
            return "session"
        if unit == 6:
            return "weekly"
    return "window"


def _glm_window_percent(limit: dict) -> float | None:
    total = _num(limit.get("usage"))
    current = _num(limit.get("currentValue"))
    remaining = _num(limit.get("remaining"))
    if total is not None and total > 0 and (current is not None or remaining is not None):
        used = current if current is not None else total - (remaining or 0)
        return min(100.0, max(0.0, used / total * 100))
    return _num(limit.get("percentage"))


def _glm_snapshot(payload: dict) -> QuotaSnapshot:
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    raw_limits = data.get("limits")
    windows = []
    for raw in raw_limits if isinstance(raw_limits, list) else []:
        if not isinstance(raw, dict):
            continue
        windows.append(QuotaWindow(
            _glm_window_label(raw),
            _glm_window_percent(raw),
            _ms_to_iso(raw.get("nextResetTime")),
            detail=raw.get("type") if isinstance(raw.get("type"), str) else None,
        ))
    gating = [w for w in windows
              if w.label in ("session", "weekly")
              and w.used_percent is not None
              and w.used_percent >= _REACHED_PERCENT]
    reached = bool(gating)
    level = data.get("level")
    level = level if isinstance(level, str) else None

    def show(label: str) -> str:
        for window in windows:
            if window.label == label and window.used_percent is not None:
                return f"{window.used_percent:.0f}%"
        return "?"

    return QuotaSnapshot(
        harness="zcode",
        status="exhausted" if reached else "ok",
        limit_reached=reached,
        resets_at=min((w.resets_at for w in gating if w.resets_at), default=None),
        plan=level,
        summary=f"session {show('session')}, weekly {show('weekly')},"
                f" monthly {show('monthly')}",
        windows=tuple(windows),
    )


def fetch_zcode(fetch: Fetch | None = None,
                config_path: Path | None = None) -> QuotaSnapshot:
    fetch = fetch or _urllib_fetch
    try:
        url, headers, error = _zcode_credentials(config_path)
        if error:
            return _unknown("zcode", error)
        status, payload = _get_json(fetch, url, headers)
        if status != 200:
            return _unknown("zcode", f"quota/limit HTTP {status}")
        if payload is None or payload.get("success") is not True or not isinstance(
                payload.get("data"), dict):
            return _unknown("zcode", "unrecognized quota/limit payload")
        return _glm_snapshot(payload)
    except Exception as exc:
        return _unknown("zcode", _brief(exc))


# -- cache + harness dispatch -----------------------------------------------------

_CACHE: dict[str, tuple[float, QuotaSnapshot]] = {}


def clear_cache() -> None:
    _CACHE.clear()


def fetch_quota(harness: str, force: bool = False) -> QuotaSnapshot:
    """Cached snapshot for a harness. Unknown harnesses need no network."""
    if harness not in QUOTA_HARNESSES:
        return QuotaSnapshot(harness=harness, status="unknown",
                             error="no live quota source for this harness")
    cached = _CACHE.get(harness)
    if not force and cached and time.monotonic() - cached[0] < _CACHE_TTL_SEC:
        return cached[1]
    if harness == "codex":
        snapshot = fetch_codex()
    elif harness == "cursor":
        snapshot = fetch_cursor()
    else:
        snapshot = fetch_zcode()
    _CACHE[harness] = (time.monotonic(), snapshot)
    return snapshot


# -- state refresh ----------------------------------------------------------------

def refresh(project) -> list[dict]:
    """Preflight every CLI-harness profile in the project and write what the
    provider reports into resource_status (through resource_learn, so
    operator overrides always win):

    - limit reached  -> exhausted + quota_reset_at (the routing gate in
      routing.py releases the profile once the reset passes)
    - healthy again  -> an auto-learned exhausted row returns to available

    Unreachable providers are "no signal": state is left untouched. Returns
    one report entry per profile. ORX_QUOTA_PREFLIGHT=0 disables entirely
    (tests, offline machines)."""
    if os.environ.get(PREFLIGHT_ENV, "1") == "0":
        return []

    store = project.store
    by_harness: dict[str, list[str]] = {}
    for name, profile in project.profiles.items():
        harness = profile.harness.value
        if profile.driver.value == "cli" and harness in QUOTA_HARNESSES:
            by_harness.setdefault(harness, []).append(name)

    report: list[dict] = []
    for harness, names in by_harness.items():
        snapshot = fetch_quota(harness)
        for name in names:
            row = store.resource_row(name)
            changed = False
            note = ""
            if row is not None and row.override:
                # An operator decision outranks the dashboard.
                note = "operator override stands"
            elif snapshot.status == "exhausted":
                changed = row is None or row.status != "exhausted"
                store.resource_learn(
                    name, status="exhausted", quota_reset_at=snapshot.resets_at,
                    last_error_kind="quota_exhausted",
                    note=f"preflight: {snapshot.summary}",
                )
            elif (snapshot.status == "ok" and row is not None
                    and row.status == "exhausted"):
                changed = True
                store.resource_learn(
                    name, status="available", quota_reset_at=None,
                    last_error_kind=None, note=f"preflight: {snapshot.summary}",
                )
            report.append({
                "profile": name,
                "harness": harness,
                "quota": snapshot.status,
                "resets_at": snapshot.resets_at,
                "summary": snapshot.summary,
                "changed": changed,
                "note": note,
            })
    return report
