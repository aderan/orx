"""Agent health auto-learning (M1 P4).

`record_attempt_outcome` turns CLI attempt results into resource-status
transitions. Rules (docs/m1-plan.md P4):

- success            -> available, streak 0
- AUTH_REQUIRED      -> auth_required (never retried by routing)
- QUOTA_EXHAUSTED    -> exhausted (+ quota_reset_at when known)
- RATE_LIMITED       -> cooldown until now + 60s * 2**min(streak,5) + jitter
- TEMPORARY_FAILURE  -> streak++, cooldown only after 3 consecutive
- MODEL_UNAVAILABLE / CONTEXT_EXCEEDED / INVALID_REQUEST are NOT resource
  problems (the profile is fine, the request was wrong): recorded, never
  gated
- PROCESS_FAILURE / CANCELLED / unknown: recorded, never gated

Writes go through `Store.resource_learn`, which refuses rows an operator
marked with override = 1 (manual `orx resource set`); `orx resource clear`
re-enables learning. No in-process retries here — that is M2.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

BASE_COOLDOWN_SEC = 60
MAX_BACKOFF_EXPONENT = 5
JITTER_SEC = 30
TEMPORARY_FAILURE_THRESHOLD = 3

# Error kinds that never gate routing (the profile itself is healthy).
NON_GATING_KINDS = {"model_unavailable", "context_exceeded", "invalid_request",
                    "process_failure", "cancelled", None}


def cooldown_until(streak: int, base: int = BASE_COOLDOWN_SEC,
                   jitter: int = JITTER_SEC) -> str:
    seconds = base * (2 ** min(max(streak, 1), MAX_BACKOFF_EXPONENT)) \
        + random.randint(0, jitter)
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="seconds")


def record_attempt_outcome(store, profile: str, ok: bool,
                           error_kind: str | None = None,
                           quota_reset_at: str | None = None) -> None:
    row = store.resource_row(profile)
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")

    if ok:
        store.resource_learn(profile, status="available",
                             last_success_at=now, failure_streak=0,
                             last_error_kind=None, cooldown_until=None,
                             quota_reset_at=None)
        return

    streak = (row.failure_streak if row else 0) + 1
    fields: dict = {"last_failure_at": now, "failure_streak": streak,
                    "last_error_kind": error_kind}

    if error_kind == "auth_required":
        fields["status"] = "auth_required"
        fields["cooldown_until"] = None
    elif error_kind == "quota_exhausted":
        fields["status"] = "exhausted"
        if quota_reset_at:
            fields["quota_reset_at"] = quota_reset_at
    elif error_kind == "rate_limited":
        fields["status"] = "cooldown"
        fields["cooldown_until"] = cooldown_until(streak)
    elif error_kind == "temporary_failure":
        if streak >= TEMPORARY_FAILURE_THRESHOLD:
            fields["status"] = "cooldown"
            fields["cooldown_until"] = cooldown_until(streak)
        # below the threshold: keep the current status, record the streak
    # every other kind (or none): failure fields only, never gated

    store.resource_learn(profile, **fields)


def cooldown_active(row) -> bool:
    """True while a cooldown row's retry time is still in the future."""
    if row is None or row.status != "cooldown" or not row.cooldown_until:
        return False
    try:
        until = datetime.fromisoformat(row.cooldown_until)
    except ValueError:
        return False
    return datetime.now(timezone.utc) < until


def quota_exhaustion_active(row) -> bool:
    """True while an exhausted row's quota reset is still in the future
    (G005). A reset time that already passed releases the profile — routing
    lets it back in and the next outcome re-learns the truth. An exhausted
    row without a usable reset time never self-recovers: it stays gated
    until `orx resource clear` (the reset time comes from harness output or
    a live preflight; when neither ever named one, guessing is worse)."""
    if row is None or row.status != "exhausted":
        return False
    if not row.quota_reset_at:
        return True
    try:
        until = datetime.fromisoformat(row.quota_reset_at)
    except ValueError:
        return True
    if until.tzinfo is None:
        return False  # a bare local timestamp cannot be compared honestly
    return datetime.now(timezone.utc) < until
