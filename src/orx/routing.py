"""Deterministic M0 routing.

Configured profile order only — no scoring, no adaptation. Routing considers
static profile definitions (TOML) plus temporary resource status (SQLite).
Resource status filters candidates but never rewrites the configured order.

Every result carries enough information to answer later: what was requested,
which profiles were considered, which were rejected and why, which was
selected, and whether fallback was used.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from orx import records
from orx.config import Config, Profile
from orx.records import (
    LAST_RESORT_RESOURCE_STATUS,
    NON_ROUTABLE_RESOURCE_STATUSES,
    PREFERRED_RESOURCE_STATUSES,
    Driver,
    ResourceStatus,
    Role,
)
from orx.state import Store

# Host-exclusive capabilities (R002 follow-up): capabilities that only a
# host-driver profile may satisfy. A task whose `routing.required_capabilities`
# names one of these is a host task by declaration — `orx run` parks it as a
# host assignment (waiting_host) and a CLI worker is never started for it.
# route() enforces the exclusivity structurally: a non-host profile that
# claims the capability is still rejected (`driver_not_host`), so the
# guarantee does not depend on profiles.toml being well-configured. Register
# the capability on host-driver profiles (the zcode preset does) — that also
# puts it in the known-capability set plan validation accepts.
HOST_CONTEXT_CAPABILITY = "host_context"
HOST_EXCLUSIVE_CAPABILITIES: frozenset[str] = frozenset({HOST_CONTEXT_CAPABILITY})


@dataclass(frozen=True)
class RouteRequest:
    role: Role
    depth: records.PlanDepth | None = None
    required_capabilities: tuple[str, ...] = ()
    pinned_profile: str | None = None
    allow_class_downgrade: bool = False

    def describe(self) -> dict:
        out: dict = {"role": self.role.value}
        if self.depth:
            out["depth"] = self.depth.value
        if self.required_capabilities:
            out["required_capabilities"] = sorted(self.required_capabilities)
        if self.pinned_profile:
            out["pinned_profile"] = self.pinned_profile
        return out


@dataclass(frozen=True)
class Candidate:
    profile: str
    resource_status: str
    model_class: str
    kept: bool
    reject_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "resource_status": self.resource_status,
            "class": self.model_class,
            "kept": self.kept,
            "reject_reason": self.reject_reason,
        }


@dataclass
class RouteResult:
    selected: str | None = None
    profile: Profile | None = None
    reason: str | None = None
    fallback_used: bool = False
    downgrade_blocked: bool = False
    candidates: list[Candidate] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.selected is not None


def route(store: Store, config: Config, profiles: dict[str, Profile], req: RouteRequest) -> RouteResult:
    result = RouteResult()

    if req.pinned_profile is not None:
        names = [req.pinned_profile]
    else:
        names = config.candidate_profiles(req.role, req.depth)
    if not names:
        result.error = (
            f"no profiles configured for role '{req.role.value}'"
            + (f" at depth '{req.depth.value}'" if req.depth else "")
        )
        return result

    class_filter_active = (
        req.role is Role.PLANNER
        and req.depth is records.PlanDepth.DEEP
        and not req.allow_class_downgrade
    )

    for name in names:
        profile = profiles.get(name)
        if profile is None:
            result.candidates.append(
                Candidate(name, "-", "-", kept=False, reject_reason="unknown_profile")
            )
            continue
        resource = store.resource_get(name)  # missing row -> unknown, still routable
        reject: str | None = None
        if resource in NON_ROUTABLE_RESOURCE_STATUSES:
            reject = resource.value  # 'unavailable' | 'exhausted' | 'auth_required'
        elif resource is ResourceStatus.COOLDOWN:
            # Time-bounded gate: reject only while the retry time is in the
            # future; an expired cooldown routes again and re-learns from the
            # next outcome.
            from orx.health import cooldown_active
            if cooldown_active(store.resource_row(name)):
                reject = f"cooldown (retry {store.resource_row(name).cooldown_until})"
        if reject is None and class_filter_active and profile.model_class is not records.ModelClass.FRONTIER:
            reject = "class_below_frontier"
        elif reject is None:
            missing = sorted(set(req.required_capabilities) - set(profile.capabilities))
            if missing:
                reject = "missing_capability:" + ",".join(missing)
            else:
                # Host-exclusive capabilities gate on the driver, not just the
                # declaration: a profile that is not host can never satisfy
                # them, so a host-declared task cannot land on a CLI worker.
                host_only = sorted(
                    set(req.required_capabilities) & HOST_EXCLUSIVE_CAPABILITIES
                )
                if host_only and profile.driver is not Driver.HOST:
                    reject = "driver_not_host:" + ",".join(host_only)
        result.candidates.append(
            Candidate(
                profile=name,
                resource_status=resource.value,
                model_class=profile.model_class.value,
                kept=reject is None,
                reject_reason=reject,
            )
        )

    kept = [c for c in result.candidates if c.kept]
    result.downgrade_blocked = class_filter_active and not kept and any(
        c.reject_reason == "class_below_frontier" for c in result.candidates
    )

    if req.pinned_profile is not None:
        # An explicitly pinned profile never falls back.
        if not kept:
            reasons = ", ".join(
                f"{c.profile}: {c.reject_reason}" for c in result.candidates if not c.kept
            )
            result.error = (
                f"pinned profile '{req.pinned_profile}' is not usable and fallback is"
                f" disabled for pinned profiles ({reasons})"
            )
            if result.downgrade_blocked:
                result.error += " [deep planning refuses classes below frontier;"
                result.error += " set [plan] allow_class_downgrade = true to permit it]"
            return result
        chosen = kept[0]
        result.selected = chosen.profile
        result.profile = profiles[chosen.profile]
        result.reason = "pinned"
        return result

    preferred = [c for c in kept if ResourceStatus(c.resource_status) in PREFERRED_RESOURCE_STATUSES]
    pool = preferred or [c for c in kept if ResourceStatus(c.resource_status) is LAST_RESORT_RESOURCE_STATUS]
    if not pool:
        summary = ", ".join(
            f"{c.profile}: {c.reject_reason}" for c in result.candidates if not c.kept
        )
        detail = "downgrade blocked: every candidate is below frontier class" if result.downgrade_blocked else summary
        result.error = f"no usable profile for role '{req.role.value}' ({detail})"
        return result

    chosen = pool[0]
    result.selected = chosen.profile
    result.profile = profiles[chosen.profile]
    index = [c.profile for c in result.candidates].index(chosen.profile)
    result.fallback_used = index != 0
    if not preferred:
        result.reason = "constrained_last_resort"
    elif index == 0:
        result.reason = "primary"
    else:
        result.reason = "fallback"
    return result


def persist_decision(
    store: Store, req: RouteRequest, result: RouteResult, attempt_id: int | None = None
) -> None:
    store.routing_decision_add(
        role=req.role.value,
        requested=req.describe(),
        candidates=[c.to_dict() for c in result.candidates],
        selected=result.selected,
        reason=result.reason,
        downgrade_blocked=result.downgrade_blocked,
        attempt_id=attempt_id,
    )
