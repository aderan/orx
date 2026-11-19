"""Deterministic M0 routing.

Configured profile order only — no scoring, no adaptation. Routing considers
static profile definitions (TOML) plus temporary resource status (SQLite).
Resource status filters candidates but never rewrites the configured order.

Every result carries enough information to answer later: what was requested,
which profiles were considered, which were rejected and why, which was
selected, and whether fallback was used.
"""

from __future__ import annotations

import os
import shlex
from contextvars import ContextVar
from dataclasses import dataclass, field, replace

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

# CLI declarations live only for one invocation, including embedded CliRunner calls.
CONTROLLER_CONTEXT: ContextVar[ControllerContext | None] = ContextVar("orx_controller", default=None)


def bind_host_profile(profile: Profile, harness: str | None) -> Profile:
    """Resolve a current-host profile without rewriting its static definition."""
    if profile.harness is records.Harness.CURRENT and harness:
        return replace(profile, harness=records.Harness(harness))
    return profile


def host_rejection(profile: Profile, controller: ControllerContext | None) -> str | None:
    if profile.harness is records.Harness.CURRENT and controller is None:
        return "host_not_declared:use orx --host <codex|zcode|cursor|claude>"
    return controller.rejection(profile) if controller else None


def controller_command(command: str) -> str:
    """Keep an explicit Controller declaration on executable next actions."""
    controller = ControllerContext.from_environment()
    if controller is None or controller.source != "cli" or not command.startswith("orx "):
        return command
    args = ["orx", "--host", controller.harness]
    for agent in sorted(controller.agents):
        args.extend(["--host-agent", agent])
    if controller.session_ref:
        args.extend(["--host-session", controller.session_ref])
    return shlex.join(args) + command[3:]


@dataclass(frozen=True)
class ControllerContext:
    harness: str
    agents: frozenset[str]
    session_ref: str | None = None
    source: str = "environment"

    @classmethod
    def from_environment(cls) -> ControllerContext | None:
        declared = CONTROLLER_CONTEXT.get()
        if declared is not None:
            return declared
        harness = os.environ.get("ORX_CONTROLLER_HARNESS", "").strip()
        if not harness:
            return None  # Preserve existing callers that route without a host declaration.
        agents = os.environ.get("ORX_CONTROLLER_AGENTS", "")
        return cls(harness, frozenset(name.strip() for name in agents.split(",") if name.strip()),
                   os.environ.get("ORX_SESSION_REF", "").strip() or None)

    def rejection(self, profile: Profile) -> str | None:
        if profile.driver is not Driver.HOST:
            return None
        if profile.harness is not records.Harness.CURRENT and profile.harness.value != self.harness:
            return f"host_harness_mismatch:requires {profile.harness.value}, current {self.harness}"
        if profile.host_mode == "subagent" and profile.agent_ref not in self.agents:
            return f"host_agent_unavailable:{profile.agent_ref}"
        return None


@dataclass(frozen=True)
class RouteRequest:
    role: Role
    depth: records.PlanDepth | None = None
    required_capabilities: tuple[str, ...] = ()
    pinned_profile: str | None = None
    allow_class_downgrade: bool = False
    # G009 T004: task-scoped candidate exclusions (same-condition blocker
    # suppression). Unlike resource status, which is a profile-global fact,
    # a suppressed (task, profile) pair must not remove the profile for
    # other tasks — so the exclusion rides the request, not the store.
    exclude_profiles: tuple[str, ...] = ()
    controller: ControllerContext | None = field(default_factory=ControllerContext.from_environment)

    def describe(self) -> dict:
        out: dict = {"role": self.role.value}
        if self.depth:
            out["depth"] = self.depth.value
        if self.required_capabilities:
            out["required_capabilities"] = sorted(self.required_capabilities)
        if self.pinned_profile:
            out["pinned_profile"] = self.pinned_profile
        if self.exclude_profiles:
            out["exclude_profiles"] = sorted(self.exclude_profiles)
        if self.controller is not None:
            out["controller"] = {
                "harness": self.controller.harness,
                "agents": sorted(self.controller.agents),
            }
            if self.controller.session_ref:
                out["controller"]["session_ref"] = self.controller.session_ref
        return out


@dataclass(frozen=True)
class Candidate:
    profile: str
    resource_status: str
    model_class: str | None
    kept: bool
    reject_reason: str | None = None
    tier: str | None = None

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "resource_status": self.resource_status,
            "class": self.model_class,
            "tier": self.tier,
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

    # G009 T005: shared-quota failure domains. Profiles that declare the
    # same `quota_domain` draw one provider account's quota; when ANY member
    # of a domain carries an ACTIVE exhaustion (a reset still in the future,
    # or a recovery time that cannot be compared honestly), the domain's
    # quota is dead and no sibling is independent capacity (the incident:
    # controller and worker died on the same GLM account at 02:05). An
    # operator override on a member outranks the learned domain fact for
    # THAT member only (checked per-candidate below).
    from orx.health import quota_exhaustion_active
    exhausted_domains: set[str] = set()
    for member in profiles.values():
        if not member.quota_domain:
            continue
        member_row = store.resource_row(member.name)
        if (member_row is not None
                and member_row.status == "exhausted"
                and quota_exhaustion_active(member_row)):
            exhausted_domains.add(member.quota_domain)

    for name in names:
        profile = profiles.get(name)
        if profile is None:
            result.candidates.append(
                Candidate(name, "-", "-", kept=False, reject_reason="unknown_profile")
            )
            continue
        resource = store.resource_get(name)  # missing row -> unknown, still routable
        reject: str | None = None
        if name in req.exclude_profiles:
            # Task-scoped exclusion (G009 T004): the same-condition blocker
            # streak for THIS task on THIS profile reached the configured
            # bound. The profile stays routable for every other task; here
            # it is skipped with the same candidate/reject audit shape the
            # resource gates use.
            reject = f"suppressed_blocker:{name}"
        elif resource in NON_ROUTABLE_RESOURCE_STATUSES:
            reject = resource.value  # 'unavailable' | 'exhausted' | 'auth_required'
            if resource is ResourceStatus.EXHAUSTED:
                # Time-bounded gate (G005): an exhaustion whose reset time is
                # known and has passed routes again — the next outcome
                # re-learns the truth. An unknown reset keeps the
                # manual-clear semantics. Released reads as `unknown`, not
                # `exhausted`: "should be usable, unverified" is exactly what
                # unknown means, and it lets the pool treat the rung as
                # preferred instead of dropping it behind the healthy ones.
                if not quota_exhaustion_active(store.resource_row(name)):
                    reject = None
                    resource = ResourceStatus.UNKNOWN
        elif resource is ResourceStatus.COOLDOWN:
            # Time-bounded gate: reject only while the retry time is in the
            # future; an expired cooldown routes again and re-learns from the
            # next outcome.
            from orx.health import cooldown_active
            if cooldown_active(store.resource_row(name)):
                reject = f"cooldown (retry {store.resource_row(name).cooldown_until})"
        if (reject is None and profile.quota_domain
                and profile.quota_domain in exhausted_domains):
            # The sibling's own row is healthy — the shared quota is what
            # died. An operator override on THIS profile (the operator
            # vouches it no longer shares that quota) outranks the learned
            # domain fact.
            own_row = store.resource_row(name)
            if own_row is None or not own_row.override:
                reject = f"quota_domain_exhausted:{profile.quota_domain}"
        if reject is None:
            reject = host_rejection(profile, req.controller)
        if reject is None and profile.tier is records.ModelTier.S:
            specialized = set(profile.capabilities) - records.GENERIC_CAPABILITIES
            if not specialized.intersection(req.required_capabilities):
                reject = "specialized_capability_required"
        if reject is None:
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
                model_class=profile.model_class.value if profile.model_class is not None else None,
                tier=profile.tier.value if profile.tier is not None else None,
                kept=reject is None,
                reject_reason=reject,
            )
        )

    kept = [c for c in result.candidates if c.kept]
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
            return result
        chosen = kept[0]
        result.selected = chosen.profile
        result.profile = bind_host_profile(profiles[chosen.profile], req.controller.harness if req.controller else None)
        result.reason = "pinned"
        return result

    preferred = [c for c in kept if ResourceStatus(c.resource_status) in PREFERRED_RESOURCE_STATUSES]
    pool = preferred or [c for c in kept if ResourceStatus(c.resource_status) is LAST_RESORT_RESOURCE_STATUS]
    if not pool:
        summary = ", ".join(
            f"{c.profile}: {c.reject_reason}" for c in result.candidates if not c.kept
        )
        detail = summary
        result.error = f"no usable profile for role '{req.role.value}' ({detail})"
        return result

    chosen = pool[0]
    result.selected = chosen.profile
    result.profile = bind_host_profile(profiles[chosen.profile], req.controller.harness if req.controller else None)
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
