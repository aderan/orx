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

G009 T004 adds the RECOVERY vocabulary beside the resource one: failure
classes (permission/environment/code/resource/other), same-condition
blocker fingerprints, and the per-(task, profile) trailing blocker streak
— the recorded facts the bounded re-launch gate in dispatch reads.

G009 T005 calibrates the quota semantics on the 2026-10-07 incident: a
reset string is stored exactly as the provider printed it (a
timezone-less string keeps no zone — one is never invented), a
timezone-less or missing reset keeps the exhaustion gated (recovery time
unknown), and one succeeded request during an ACTIVE exhaustion (the
08:47 trivial probe) records its success facts without lifting the quota
blocker — probe success and quota sufficiency are different facts.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

BASE_COOLDOWN_SEC = 60
MAX_BACKOFF_EXPONENT = 5
JITTER_SEC = 30
TEMPORARY_FAILURE_THRESHOLD = 3

# Error kinds that never gate routing (the profile itself is healthy).
NON_GATING_KINDS = {"model_unavailable", "context_exceeded", "invalid_request",
                    "process_failure", "cancelled", None}


# ---------------------------------------------------------------------------
# G009 T004: recovery failure classes and same-condition blocker
# fingerprints. The 2026-10-07 incident (a131/a132/a133): three consecutive
# CLI attempts on the SAME profile hit the SAME permission blocker (shell
# approval denied in Cursor); every retry re-launched the profile and the
# retry prompts misdiagnosed the blocker as code failure. Recovery needs a
# vocabulary that tells those apart from recorded facts alone — the classes
# below classify a RECORDED (result, failure_reason) pair, which is all any
# later decision (retry, reroute, suppression) can honestly read.
#
#   permission  an approval/permission/sandbox denial — re-launching the
#               same conditions cannot change the answer
#   environment the launch environment could not serve the task as
#               configured (tool missing, auth, model unavailable) — the
#               conditions must change, not the code
#   code        a worker-reported code failure or a red controller gate —
#               a real repair opportunity; never suppressed
#   resource    quota/rate facts — T005's domain (health + the G005
#               reroute); classified here only so blockers can be told
#               apart from them
#   other       unproven (protocol errors, unpatterned crashes, timeouts) —
#               never suppressed

BLOCKER_CLASSES = ("permission", "environment")

# Lowercase needles that name a permission/approval system in a failure
# reason. Evidence: the incident's own texts ("internal shell approval
# denied", "Every shell invocation was rejected before execution",
# "shell rejected") plus the standard denial vocabulary of CLI harnesses.
PERMISSION_MARKERS: tuple[str, ...] = (
    "approval",
    "permission denied",
    "not approved",
    "approve the command",
    "command was rejected",
    "shell rejected",
    "rejected before execution",
    "denied by",
    "operation not permitted",
)

# Failure-result prefixes ORX itself writes (dispatch delivery recording);
# the fingerprint wants the condition, not ORX's envelope around it.
_REASON_ENVELOPE_PREFIXES = (
    "delivery blocked (environment/tool blocked): ",
    "delivery failed (worker-reported failure): ",
)
_TRANSPORT_PREFIX = re.compile(r"^exit (?:\d+|timeout):\s*")
_FINGERPRINT_REASON_MAX = 160


def resource_error_kind(text: str) -> str:
    """Total ErrorKind classification for one blob of failure text (G009
    T005): the adapters' verified English pattern pass first, then the
    Chinese provider vocabulary that pass cannot see (the GLM 1308
    incident's original message). Unrecognized text stays process_failure —
    the safe default that never auto-retries."""
    from orx.adapters.base import error_kind_from_text
    kind = error_kind_from_text(text)
    if kind != "process_failure":
        return kind
    from orx import quota
    return quota.provider_error_kind(text) or "process_failure"


def failure_class(result: str | None, failure_reason: str | None) -> str:
    """The recovery class of one recorded failure (see the block above).
    Ordered so the proven shapes win: a blocked delivery is a blocker by
    construction (its subclass comes from the permission markers); a
    worker-reported failure or a red gate is code by construction; only
    unmarked transport text falls through to pattern classification."""
    reason = failure_reason or ""
    lowered = reason.lower()
    if result == "blocked" or lowered.startswith("delivery blocked"):
        if any(marker in lowered for marker in PERMISSION_MARKERS):
            return "permission"
        return "environment"
    if result == "protocol_error":
        return "other"  # unproven which side failed the protocol
    if lowered.startswith("delivery failed") or "delivery gate:" in lowered:
        return "code"
    kind = resource_error_kind(reason)
    if kind in ("quota_exhausted", "rate_limited"):
        return "resource"
    if kind in ("auth_required", "model_unavailable"):
        return "environment"
    if any(marker in lowered for marker in PERMISSION_MARKERS):
        return "permission"
    return "other"


def normalize_blocker_reason(failure_reason: str | None) -> str:
    """The stable core of a blocker reason: lowercase, ORX's delivery
    envelope and the transport ``exit N:`` prefix stripped, whitespace
    collapsed, capped — so the same condition recorded through different
    envelopes (blocked delivery vs transport failure) fingerprints equal
    while a genuinely different condition does not."""
    text = (failure_reason or "").strip().lower()
    for prefix in _REASON_ENVELOPE_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = _TRANSPORT_PREFIX.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_FINGERPRINT_REASON_MAX]


def blocker_fingerprint(profile: str, harness: str, failure_class_: str,
                        failure_reason: str | None) -> str:
    """Deterministic digest of one blocker CONDITION: normalized reason +
    profile + harness + class. Two failures fingerprint equal exactly when
    re-launching would face the same profile, the same harness, and the
    same normalized blocker text — the "unchanged conditions" test the
    suppression applies."""
    core = normalize_blocker_reason(failure_reason)
    digest = hashlib.sha256(
        f"{profile}|{harness}|{failure_class_}|{core}".encode("utf-8")
    ).hexdigest()
    return digest[:16]


@dataclass(frozen=True)
class BlockerStreak:
    """The trailing run of same-condition blocker failures on one profile
    of one task — the recorded fact the bounded-launch gate reads."""

    profile: str
    harness: str
    failure_class: str
    fingerprint: str
    count: int
    latest_reason: str | None


def blocker_streak(store, revision_row_id: int, task_id: str,
                   profile: str) -> BlockerStreak | None:
    """Count the TRAILING same-fingerprint blocker failures among one
    task's worker attempts on one profile (newest backwards). The run stops
    at the first attempt of that profile that is not the same blocker
    condition — a success, a code failure, or a different blocker — so a
    proven condition change resets the count honestly. None when the
    profile's newest attempt is not a blocker failure (nothing to
    suppress; code and resource failures never count)."""
    attempts = [
        a for a in store.attempts_for_task(revision_row_id, task_id)
        if a.role == "worker" and a.profile == profile
    ]
    fingerprint: str | None = None
    failure_class_: str | None = None
    harness = ""
    latest_reason: str | None = None
    count = 0
    for attempt in reversed(attempts):
        cls = failure_class(attempt.result, attempt.failure_reason)
        if cls not in BLOCKER_CLASSES:
            break
        fp = blocker_fingerprint(
            profile, attempt.harness, cls, attempt.failure_reason
        )
        if fingerprint is None:
            fingerprint = fp
            failure_class_ = cls
            harness = attempt.harness
            latest_reason = attempt.failure_reason
        elif fp != fingerprint:
            break
        count += 1
    if fingerprint is None or failure_class_ is None:
        return None
    return BlockerStreak(
        profile=profile,
        harness=harness,
        failure_class=failure_class_,
        fingerprint=fingerprint,
        count=count,
        latest_reason=latest_reason,
    )


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
        if row is not None and row.status == "exhausted" \
                and quota_exhaustion_active(row):
            # G009 T005 (the 08:47 trivial probe): one succeeded request
            # during an ACTIVE quota exhaustion proves that request at that
            # moment, never that the window recovered — quota is
            # size-proportional, so a small request passes while large
            # tasks still die. Record the success facts; the gate, the
            # reset, and its timezone honesty all stand. Re-eligibility is
            # explicit: the (comparable) reset passing, a live preflight
            # reporting the window healthy, or an operator decision. A
            # cooldown or an auth gate is a per-request binary fact and a
            # success still recovers those, exactly as before.
            store.resource_learn(profile, last_success_at=now,
                                 failure_streak=0, last_error_kind=None)
            return
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


def quota_reset_tz_source(reset_at: str | None) -> str | None:
    """Where a stored reset string's timezone stands: 'offset' when the
    source printed one (comparable to now), 'naive' when it is
    timezone-less or unparseable (a provider-local time of unknown zone —
    the GLM 1308 incident's string), None when there is no reset string."""
    if not reset_at:
        return None
    try:
        until = datetime.fromisoformat(reset_at)
    except ValueError:
        return "naive"
    return "offset" if until.tzinfo is not None else "naive"


def quota_exhaustion_active(row) -> bool:
    """True while an exhausted row's quota reset is still in the future
    (G005). A comparable reset time (one that carries its own offset — a
    local CLI render or a dashboard epoch) that already passed releases the
    profile — routing lets it back in as a CANDIDATE (unknown, never
    "available") and the next outcome re-learns the truth. A reset time
    that cannot be compared honestly — missing, unparseable, or
    TIMEZONE-LESS (G009 T005: the GLM 1308 incident's "2026-10-07
    04:24:23", whose zone is unknown and must not be invented) — keeps the
    gate standing: recovery time unknown means never auto-released, only
    explicit re-eligibility (`orx resource set`, or a live preflight
    reporting the window healthy) re-opens the profile."""
    if row is None or row.status != "exhausted":
        return False
    if not row.quota_reset_at:
        return True
    try:
        until = datetime.fromisoformat(row.quota_reset_at)
    except ValueError:
        return True
    if until.tzinfo is None:
        # A timezone-less provider string cannot be compared to now without
        # inventing a zone — the exact mistake (appending Z) the incident
        # documented. Unknown recovery time: the gate stands.
        return True
    return datetime.now(timezone.utc) < until
