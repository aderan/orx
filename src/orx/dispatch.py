"""Orchestration: project discovery/init and the plan/run/task/verify flows.

This module wires config + state + machine + routing + verify together. The
CLI is a thin translation layer over these functions.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from orx import adapters, machine, plan as plan_mod, probes, routing, runtime, verify
from orx.adapters.base import classify_failure, scan_marker
from orx import health
from orx import config as config_mod
from orx.config import Config, Profile, load_project_config
from orx.records import (
    ACTIVE_EXECUTION_STATUSES,
    AssignmentStatus,
    ConfigError,
    ConflictError,
    GoalStatus,
    NotFoundError,
    ORXError,
    PlanDepth,
    PlanValidationError,
    ResourceStatus,
    ReplanRejectedError,
    Role,
    RoutingError,
    RunStatus,
    TaskStatus,
    UNFINISHED_TASK_STATUSES,
)
from orx.state import Assignment, Goal, Run, Revision, Store, TaskRow, now as db_now

DEFAULT_CONFIG_TOML = """\
schema_version = 1

# Static definitions only. Runtime resource/quota state lives in SQLite
# (.orx/state.db) and is managed with `orx resource set`; it never rewrites
# these files or their ordering.

[controller]
profile = "orx-host"

[plan]
depth = "auto"
allow_class_downgrade = false

[plan.light]
profiles = ["orx-host"]

[plan.standard]
profiles = ["orx-host"]

[plan.deep]
profiles = ["orx-host"]

[worker]
profiles = ["orx-host"]

[verify]
profiles = ["orx-host"]

[runtime]
# M0 shares one working tree: effective CLI parallelism is 1 regardless of
# this value; a higher setting produces an explicit doctor warning.
max_parallel = 1
command_timeout_sec = 1800
"""

DEFAULT_PROFILES_TOML = """\
schema_version = 1

# The default profile routes every role to the host driver: work is done by
# you (or your host agent) and submitted back through the CLI. Replace or
# extend with cli/external profiles as you adopt real agent harnesses.

[profiles.orx-host]
driver = "host"
harness = "zcode"
model = "unconfigured"
class = "frontier"
effort = "medium"
capabilities = ["coding"]
"""


@dataclass
class Project:
    root: Path
    config: Config
    profiles: dict[str, Profile]
    store: Store
    # M1 layered configuration: where every effective value and profile
    # came from, plus the user-layer paths in play.
    origins: dict[str, str] = field(default_factory=dict)
    profile_origins: dict[str, str] = field(default_factory=dict)
    user_config: Path | None = None
    user_profiles: Path | None = None

    @property
    def config_path(self) -> Path:
        return self.root / ".orx" / "config.toml"

    @property
    def profiles_path(self) -> Path:
        return self.root / ".orx" / "profiles.toml"

    @property
    def db_path(self) -> Path:
        return self.root / ".orx" / "state.db"

    def close(self) -> None:
        self.store.close()


def load_profiles_only(config_path: Path, profiles_path: Path) -> dict:
    """Project-layer profiles alone (init fallback when the user layer is
    broken). Raises the project-layer error if that is the problem."""
    from orx.config import load_profiles
    return load_profiles(profiles_path)


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk parents looking for `.orx`. ORX_PROJECT env var wins."""
    env = os.environ.get("ORX_PROJECT")
    current = (Path(env) if env else (start or Path.cwd())).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".orx").is_dir():
            return candidate
    return None


def init_project(root: Path) -> dict:
    """Create .orx/ if missing; refuse to clobber a non-empty config."""
    root = root.resolve()
    orx_dir = root / ".orx"
    orx_dir.mkdir(parents=True, exist_ok=True)
    (orx_dir / "runs").mkdir(exist_ok=True)

    created: list[str] = []
    config_path = orx_dir / "config.toml"
    profiles_path = orx_dir / "profiles.toml"
    if config_path.exists() and config_path.stat().st_size > 0:
        raise ORXError(f"refusing to overwrite non-empty {config_path}")
    if profiles_path.exists() and profiles_path.stat().st_size > 0:
        raise ORXError(f"refusing to overwrite non-empty {profiles_path}")
    if not config_path.exists() or config_path.stat().st_size == 0:
        config_path.write_text(DEFAULT_CONFIG_TOML)
        created.append(".orx/config.toml")
    if not profiles_path.exists() or profiles_path.stat().st_size == 0:
        profiles_path.write_text(DEFAULT_PROFILES_TOML)
        created.append(".orx/profiles.toml")

    store = Store.open(orx_dir / "state.db")
    try:
        # M1: seeding covers the effective profile set (user layer merged),
        # so profiles referenced only from the user layer also get rows. A
        # broken user layer must not break init: degrade to the project
        # layer (doctor reports the layered failure).
        try:
            names = list(config_mod.load_effective(config_path, profiles_path).profiles)
        except ConfigError:
            names = list(load_profiles_only(config_path, profiles_path))
        store.seed_resources(names)
        created.append(".orx/state.db")
    finally:
        store.close()
    return {"root": str(root), "created": created}


def open_project() -> Project:
    root = find_project_root()
    if root is None:
        raise NotFoundError(
            "not an ORX project: no .orx/ directory found in this or any parent "
            "(run `orx init` first, or set ORX_PROJECT)"
        )
    orx_dir = root / ".orx"
    effective = config_mod.load_effective(orx_dir / "config.toml", orx_dir / "profiles.toml")
    store = Store.open(orx_dir / "state.db")
    return Project(
        root=root,
        config=effective.config,
        profiles=effective.profiles,
        store=store,
        origins=dict(effective.origins),
        profile_origins=dict(effective.profile_origins),
        user_config=effective.user_config,
        user_profiles=effective.user_profiles,
    )


# ---------------------------------------------------------------------------
# Context helpers


def _active_context(project: Project) -> tuple[Goal, Run]:
    """The active Goal, or the most recent Goal when the active one finished
    (`orx status` must keep working after the Run reaches done)."""
    goal = project.store.goal_active()
    if goal is None:
        row = project.store.conn.execute(
            "SELECT id FROM goals WHERE status != 'cancelled' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            goal = project.store.goal_get(row["id"])
    if goal is None:
        raise NotFoundError("no active Goal; run `orx goal new` first")
    run = project.store.run_for_goal(goal.id)
    if run is None:
        raise NotFoundError(f"goal {goal.id} has no Run")
    return goal, run


def _active_revision(project: Project, run: Run) -> Revision | None:
    return project.store.revision_active(run.id)


def _active_task(project: Project, run: Run, task_id: str) -> tuple[Revision, TaskRow]:
    revision = _active_revision(project, run)
    if revision is None:
        raise NotFoundError("no active plan revision; submit a plan first (`orx plan submit`)")
    task = project.store.task_get(revision.id, task_id)
    return revision, task


def resolve_session_ref(explicit: str | None) -> str | None:
    """Opaque session id for a host attempt. Never invented.

    An explicit `--session` value wins over `ORX_SESSION_REF`. Omitting it
    uses that variable when non-blank, otherwise None (stored as NULL).
    A blank environment value is unset. An explicit value that is empty or
    not a single opaque token fails before the caller writes anything.
    """
    if explicit is not None:
        token = explicit.strip()
        if (
            not token
            or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in token)
        ):
            raise ORXError(
                "session reference is empty or malformed; pass one opaque token "
                "without whitespace or control characters, or omit --session to "
                "use ORX_SESSION_REF (blank leaves the reference unset)"
            )
        return token
    return os.environ.get("ORX_SESSION_REF", "").strip() or None


def _host_session_ref() -> str | None:
    """Environment session only. Blank is unset. Never invented."""
    return resolve_session_ref(None)


def _known_capabilities(project: Project) -> set[str]:
    caps: set[str] = set()
    for profile in project.profiles.values():
        caps.update(profile.capabilities)
    return caps


def refresh_readiness(store: Store, run: Run) -> None:
    """Delegates to the state machine (single owner of status recomputation)."""
    machine.refresh_readiness(store, run)


def refresh_run(store: Store, goal: Goal, run: Run) -> None:
    machine.refresh_run(store, goal, run)


def refresh(store: Store, goal: Goal, run: Run) -> None:
    machine.refresh(store, goal, run)


# ---------------------------------------------------------------------------
# Goal


def create_goal(
    project: Project,
    objective: str,
    acceptance: list[str],
    constraints: list[str] | None = None,
    context: str = "",
) -> tuple[Goal, Run]:
    if not objective.strip():
        raise ORXError("objective must be a non-empty string")
    if not acceptance or not all(a.strip() for a in acceptance):
        raise ORXError("at least one non-empty --acceptance criterion is required")
    if project.store.goal_active() is not None:
        active = project.store.goal_active()
        raise ORXError(
            f"goal {active.id} is already active; ORX M0 keeps one active Goal per project"
        )
    return project.store.goal_create(objective, acceptance, constraints or [], context)


def goal_show(project: Project) -> dict:
    goal = project.store.goal_active()
    if goal is None:
        raise NotFoundError("no active Goal")
    run = project.store.run_for_goal(goal.id)
    return {
        "goal": {
            "id": goal.id,
            "objective": goal.objective,
            "constraints": goal.constraints,
            "acceptance": goal.acceptance,
            "context": goal.context,
            "status": goal.status,
        },
        "run": {"id": run.id, "status": run.status} if run else None,
    }


# ---------------------------------------------------------------------------
# Planning


def _check_replan_allowed(store: Store, run: Run) -> None:
    revision = store.revision_active(run.id)
    if revision is None:
        return
    busy = [
        t.task_id
        for t in store.tasks_all(revision.id)
        if TaskStatus(t.status) in (TaskStatus.RUNNING, TaskStatus.VERIFYING)
    ]
    if busy:
        raise ReplanRejectedError(
            "replan rejected while tasks are running or verifying: " + ", ".join(busy)
        )


def _resolve_depth(project: Project, goal: Goal, explicit: str | None) -> PlanDepth:
    if explicit:
        return plan_mod.resolve_depth(goal.objective, explicit)
    if project.config.plan_depth_default == "auto":
        return plan_mod.resolve_depth(goal.objective, None)
    return PlanDepth(project.config.plan_depth_default)


REPLAN_CONTEXT_MAX_BYTES = 64 * 1024


def load_replan_context(path_str: str) -> str:
    """The Controller's intent file for this replan round: why replanning,
    what this round should change, any supporting material. Validated before
    anything else happens — a bad file must fail before any model is called.
    The text supplements the Goal; no code path may write it into the Goal."""
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    try:
        text = path.read_text()
    except OSError as exc:
        raise ORXError(f"cannot read replan context file {path_str}: {exc}") from None
    if not text.strip():
        raise ORXError(
            f"replan context file {path_str} is empty; provide this round's reason,"
            " intent, and supporting material (or omit --context-file)"
        )
    if len(text.encode("utf-8")) > REPLAN_CONTEXT_MAX_BYTES:
        raise ORXError(
            f"replan context file {path_str} exceeds {REPLAN_CONTEXT_MAX_BYTES} bytes;"
            " keep the intent file focused"
        )
    return text


def replan_snapshot(store: Store, goal: Goal, run: Run) -> dict:
    """Deterministic fact snapshot for a replan: every revision of this run,
    every task with its recorded status, failure reason, evidence, and
    verification results. Derived purely from ORX state — never from a model
    or the ambient workspace."""
    revisions = sorted(
        (r for r in store.revisions_all() if r.run_id == run.id),
        key=lambda r: r.revision,
    )
    tasks_out: list[dict] = []
    for rev in revisions:
        for task in store.tasks_all(rev.id):
            tasks_out.append(
                {
                    "revision": rev.revision,
                    "task_id": task.task_id,
                    "objective": task.objective,
                    "status": task.status,
                    "dependencies": list(store.deps_for(rev.id, task.task_id)),
                    "failure_reason": task.failure_reason,
                    "evidence": [
                        {"kind": kind, "path": path}
                        for kind, path in store.evidence_for_task(rev.id, task.task_id)
                    ],
                    "verifications": [
                        {
                            "kind": v.kind,
                            "command": v.command,
                            "passed": v.passed,
                            "exit_code": v.exit_code,
                            "output_path": v.output_path,
                        }
                        for v in store.verifications_for(rev.id, task.task_id)
                    ],
                }
            )
    active = next((r for r in revisions if r.status == "active"), None)
    return {
        "run_id": run.id,
        "goal_id": goal.id,
        "generated_at": db_now(),
        "active_revision": (
            {
                "revision": active.revision,
                "depth": active.depth,
                "planner_profile": active.planner_profile,
            }
            if active
            else None
        ),
        "revisions": [{"revision": r.revision, "status": r.status} for r in revisions],
        "tasks": tasks_out,
    }


def plan_route(
    project: Project, depth_flag: str | None = None, profile_flag: str | None = None,
    context_file: str | None = None, session: str | None = None,
) -> dict:
    """Route the planner. Host driver -> waiting assignment. CLI execution is
    not implemented in the M0 core kernel, so a CLI-driver result is returned
    as `mode: incomplete` rather than faked. When a plan revision is already
    active this is a replan: the prompt carries a deterministic fact snapshot
    plus (when supplied) the Controller's --context-file intent."""
    store = project.store
    goal, run = _active_context(project)
    # A bad context file fails before routing, attempts, or any model call.
    intent = load_replan_context(context_file) if context_file else ""
    _check_replan_allowed(store, run)

    revision = _active_revision(project, run)
    facts_md = ""
    if revision is not None:
        facts_md = plan_mod.render_replan_facts(replan_snapshot(store, goal, run))

    # Reject a bad explicit session before routing or assignment writes.
    session_ref = resolve_session_ref(session)

    depth = _resolve_depth(project, goal, depth_flag)
    request = routing.RouteRequest(
        role=Role.PLANNER,
        depth=depth,
        pinned_profile=profile_flag,
        allow_class_downgrade=project.config.allow_class_downgrade,
    )
    result = routing.route(store, project.config, project.profiles, request)
    if not result.ok:
        routing.persist_decision(store, request, result)
        raise RoutingError(result.error or "planner routing failed")

    profile = result.profile
    assert profile is not None
    prompt = plan_mod.planner_prompt(goal, depth, replan_facts=facts_md, intent=intent)
    replan = revision is not None
    if profile.driver.value == "host":
        assignment = store.assignment_waiting(run.id)
        if assignment is None:
            if run.status == RunStatus.DONE.value:
                store.run_set_status(run.id, RunStatus.PLANNING)
                store.goal_set_status(goal.id, GoalStatus.ACTIVE)
            assignment = store.assignment_create(
                run.id, profile.name, depth.value, prompt
            )
            _write_assignment_file(project, run, assignment)
        elif assignment.prompt != prompt:
            # A waiting assignment predates this call: facts and/or intent have
            # moved on. Refresh prompt row + file so what the planner receives
            # is what ORX recorded, never a stale composition.
            store.assignment_update_prompt(assignment.id, prompt)
            assignment = store.assignment_get(assignment.id)
            _write_assignment_file(project, run, assignment)
        # Reuse the open planner attempt for this waiting assignment. A second
        # plan_route must not open a duplicate span.
        attempt = store.attempt_open_for_assignment(assignment.id)
        if attempt is None:
            attempt = store.attempt_create(
                revision_row_id=revision.id if revision else None,
                role=Role.PLANNER.value,
                profile=profile.name,
                driver=profile.driver.value,
                harness=profile.harness.value,
                model_id=profile.model,
                requested_effort=profile.effort.value,
                routing_reason=result.reason,
                fallback_used=result.fallback_used,
                assignment_id=assignment.id,
                isolation="prompt_only",
                session_ref=session_ref,
                run_id=run.id,
            )
        elif session_ref is not None:
            store.attempt_update(attempt.id, session_ref=session_ref)
        routing.persist_decision(store, request, result, attempt_id=attempt.id)
        return {
            "mode": "host_required",
            "depth": depth.value,
            "assignment": _assignment_payload(project, assignment, attempt),
            "routing": _routing_payload(result),
            "replan": replan,
            "context_file": context_file,
        }

    if profile.driver.value == "cli":
        return _run_cli_planner(project, goal, run, depth, profile, request, result,
                                prompt=prompt, replan=replan,
                                context_file=context_file)

    # driver external: ORX does not launch external planners; the human runs
    # the prompt wherever they want and submits the result by hand.
    routing.persist_decision(store, request, result)
    return {
        "mode": "incomplete",
        "reason": "external_execution_not_supported_for_planning",
        "depth": depth.value,
        "selected_profile": profile.name,
        "detail": (
            "External planners are not dispatched by ORX. Use a driver = 'host' "
            "profile to get a planning assignment, run the prompt externally "
            "yourself, then `orx plan submit --file plan.json`."
        ),
        "routing": _routing_payload(result),
        "replan": replan,
        "context_file": context_file,
    }


def _launch_dir(project: Project, run_id: str, label: str) -> Path:
    directory = project.root / ".orx" / "runs" / run_id / "launch" / label
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _record_execution_log(project: Project, run_id: str, label: str,
                          run_result) -> str:
    directory = project.root / ".orx" / "runs" / run_id / "exec"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{label}.log"
    path.write_text(
        f"$ {run_result.command}\n[exit {'timeout' if run_result.timed_out else run_result.exit_code}"
        f" in {run_result.duration_sec:.2f}s]\n{run_result.stdout}"
        + (f"\n[stderr]\n{run_result.stderr}" if run_result.stderr else "")
    )
    return str(path.relative_to(project.root))


def _run_cli_planner(project: Project, goal: Goal, run: Run, depth: PlanDepth,
                     profile, request: routing.RouteRequest,
                     result: routing.RouteResult, *, prompt: str,
                     replan: bool = False, context_file: str | None = None) -> dict:
    """Launch a CLI planner through its adapter, parse the Plan IR it printed,
    and submit it. Completion is real — never faked. The exact prompt handed
    to the planner is archived under the launch dir for later audit."""
    store = project.store
    adapter = adapters.get_adapter(profile.harness.value)
    probe = adapter.probe()
    if not probe.ok:
        routing.persist_decision(store, request, result)
        raise RoutingError(
            f"planner profile '{profile.name}': adapter capability_mismatch ({probe.detail})"
        )

    scratch = _launch_dir(project, run.id, "plan")
    schema_path = scratch / "plan-schema.json"
    schema_path.write_text(
        json.dumps(plan_mod.strict_json_schema(plan_mod.PlanIR.model_json_schema()), indent=2)
    )
    launch = adapter.build_planner_launch(
        root=project.root,
        scratch=scratch,
        profile=profile,
        prompt=prompt,
        timeout=project.config.command_timeout_sec,
        schema_path=schema_path,
    )
    attempt = store.attempt_create(
        revision_row_id=None,
        role=Role.PLANNER.value,
        profile=profile.name,
        driver="cli",
        harness=profile.harness.value,
        model_id=profile.model,
        requested_effort=profile.effort.value,
        routing_reason=result.reason,
        fallback_used=result.fallback_used,
        isolation=launch.sandbox,
        run_id=run.id,
    )
    routing.persist_decision(store, request, result, attempt_id=attempt.id)
    # Archive the exact planner input (goal + facts + intent + schema rules):
    # the file a later audit compares against what the model actually saw.
    prompt_path = scratch / f"prompt-{attempt.id}.md"
    prompt_path.write_text(prompt)

    run_result = runtime.run_launch(launch)
    # Always persist the raw transcript: paid planner calls need an audit
    # trail on success too, not only on failure.
    log = _record_execution_log(project, run.id, f"plan-{attempt.id}", run_result)
    effort = adapter.effort_outcome(launch, run_result)
    _record_usage(store, adapter, launch, run_result, attempt, profile.name,
                  run_id=run.id, task_id=None)
    health.record_attempt_outcome(
        store, profile.name, ok=run_result.ok,
        error_kind=None if run_result.ok else classify_failure(run_result))
    store.attempt_update(
        attempt.id,
        ended_at=db_now(),
        result="completed" if run_result.ok else "failed",
        actual_effort=effort.actual if effort.actual else None,
        effort_source=effort.source,
        failure_reason=None if run_result.ok else _failure_excerpt(run_result),
    )
    if not run_result.ok:
        raise ORXError(
            f"planner '{profile.name}' failed ({_failure_excerpt(run_result)}); log: {log}"
        )

    text = adapter.extract_text(launch, run_result)
    ir_data = json.loads(text) if text.strip().startswith("{") else None
    if ir_data is None:
        # Real CLI agents wrap the JSON document in narrative text even when
        # told to emit only JSON (Cursor, 2026-10-03); recover the embedded
        # object before giving up.
        ir_data = plan_mod.extract_json_object(text)
    if ir_data is None:
        raise ORXError(
            f"planner '{profile.name}' did not emit valid Plan IR JSON; log: {log}"
        )
    submitted = submit_plan(project, ir_data,
                            planner_profile=profile.name, depth_hint=depth.value)
    return {
        "mode": "completed",
        "depth": depth.value,
        "profile": profile.name,
        "routing": _routing_payload(result),
        "replan": replan,
        "context_file": context_file,
        "prompt_file": str(prompt_path.relative_to(project.root)),
        **submitted,
    }


def _record_usage(store, adapter, launch, run_result, attempt, profile_name,
                  run_id: str, task_id: str | None) -> None:
    """Attach harness usage and session id to the attempt.

    The adapter reads the full captured stream (not the truncated log).
    A recognized session id is stored even when usage is missing. Tokens
    are never filled in. A finished CLI attempt with no storable observation
    records why: ``adapter_unsupported`` (no hook), ``harness_omitted``
    (no usage event), ``malformed_output``, ``truncated``, or
    ``execution_failure``.
    """
    interpret = getattr(adapter, "interpret_capture", None)
    if interpret is None:
        getter = getattr(adapter, "usage_observation", None)
        if getter is None:
            store.attempt_mark_usage_missing(attempt.id, "adapter_unsupported")
            return
        obs = getter(launch, run_result)
        session = None
        reason = None if obs else "harness_omitted"
    else:
        captured = interpret(launch, run_result)
        obs = captured.usage
        session = captured.session_ref
        reason = captured.miss_reason
    if session:
        store.attempt_update(attempt.id, session_ref=session)
    if obs:
        store.usage_add(
            attempt_id=attempt.id, profile=profile_name, run_id=run_id,
            task_id=task_id,
            input_tokens=obs.get("input_tokens"),
            output_tokens=obs.get("output_tokens"),
            cached_input_tokens=obs.get("cached_input_tokens"),
            source=obs.get("source", "native_cli"),
            accuracy=obs.get("accuracy", "unknown"),
        )
        return
    store.attempt_mark_usage_missing(attempt.id, reason or "harness_omitted")


def _captured(run_result, name: str) -> str:
    """Full redacted stream when the runtime kept one, else the log view."""
    full = getattr(run_result, f"{name}_full", None)
    if isinstance(full, str) and full:
        return full
    return getattr(run_result, name, "") or ""


def _failure_excerpt(run_result) -> str:
    if run_result.timed_out:
        return f"timed out after {run_result.duration_sec:.0f}s"
    # The log view is a prefix. The failure text is often at the end of the
    # stream the harness actually wrote.
    excerpt = (_captured(run_result, "stderr") or _captured(run_result, "stdout")).strip().splitlines()
    tail = " | ".join(excerpt[-3:]) if excerpt else "no output"
    return f"exit {run_result.exit_code}: {tail[:300]}"


def _assignment_payload(project: Project, assignment: Assignment,
                        attempt=None) -> dict:
    run_id = assignment.run_id
    prompt_file = project.root / ".orx" / "runs" / run_id / "assignments" / f"{assignment.id}.md"
    payload = {
        "id": assignment.id,
        "run": run_id,
        "role": "planner",
        "profile": assignment.profile,
        "depth": assignment.depth,
        "prompt": assignment.prompt,
        "prompt_file": str(prompt_file.relative_to(project.root)) if prompt_file.exists() else None,
        "schema": plan_mod.PLAN_IR_SCHEMA,
        "submit": "orx plan submit --file plan.json",
    }
    if attempt is not None:
        payload["execution"] = _execution_spec(
            project, project.profiles.get(assignment.profile), attempt
        )
    return payload


def _execution_spec(project: Project, profile: Profile | None, attempt) -> dict:
    """The complete host launch specification for one assignment (phase B
    execution contract): what the Controller must start, with which REQUESTED
    model/effort (profile facts — observed reality is recorded separately as
    actual_model with its source), in which working directory, and the stable
    attempt identity every later submission must quote."""
    if profile is None:
        return {
            "mode": "self",
            "agent_ref": None,
            "model": attempt.model,
            "effort": attempt.requested_effort,
            "harness": attempt.harness,
            "workdir": str(project.root),
            "attempt": attempt.id,
            "submit": "controller",
        }
    driver = profile.driver.value
    if driver == "host":
        mode = profile.host_mode
    else:
        # cli/external are not host execution modes; the spec still carries
        # the requested facts and the stable attempt identity.
        mode = driver
    return {
        "mode": mode,
        "agent_ref": profile.agent_ref if mode == "subagent" else None,
        "model": profile.model,
        "effort": profile.effort.value,
        "harness": profile.harness.value,
        "workdir": str(project.root),
        "attempt": attempt.id,
        "submit": "controller",
    }


def _write_assignment_file(project: Project, run: Run, assignment: Assignment) -> None:
    directory = project.root / ".orx" / "runs" / run.id / "assignments"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{assignment.id}.md").write_text(assignment.prompt)


def _routing_payload(result: routing.RouteResult) -> dict:
    return {
        "selected": result.selected,
        "reason": result.reason,
        "fallback_used": result.fallback_used,
        "downgrade_blocked": result.downgrade_blocked,
        "candidates": [c.to_dict() for c in result.candidates],
    }


def submit_plan(project: Project, ir_data: dict,
                planner_profile: str | None = None, depth_hint: str | None = None,
                session: str | None = None) -> dict:
    store = project.store
    goal, run = _active_context(project)

    # The replan guard runs before validation: a rejected replan should say
    # why it was rejected, not surface unrelated plan errors first.
    _check_replan_allowed(store, run)
    # A malformed explicit session fails before the revision is written and
    # before the open planner attempt is closed.
    session_ref = resolve_session_ref(session)

    ir = plan_mod.parse_ir(ir_data)
    errors = plan_mod.validate_ir(ir, goal.id, goal.acceptance, _known_capabilities(project))
    if errors:
        raise PlanValidationError(errors)

    assignment = store.assignment_waiting(run.id)
    depth_value = (
        assignment.depth if assignment
        else (depth_hint or _resolve_depth(project, goal, None).value)
    )
    planner = (
        assignment.profile if assignment
        else (planner_profile or "host-manual")
    )

    old = store.revision_active(run.id)
    cancelled_old: list[str] = []
    with store.tx():
        if old is not None:
            store.revision_mark_superseded(old.id)
            for task in store.tasks_all(old.id):
                if TaskStatus(task.status) in UNFINISHED_TASK_STATUSES:
                    machine.transition(
                        store, old.id, task.task_id, "superseded", TaskStatus.CANCELLED,
                        reason=f"revision {old.revision} superseded",
                    )
                    cancelled_old.append(task.task_id)

        revision = store.revision_create(run.id, depth_value, planner, ir_data)
        for task in ir.tasks:
            has_unmet = bool(task.dependencies)
            status = TaskStatus.PENDING if has_unmet else TaskStatus.RUNNABLE
            store.task_insert(
                revision.id,
                task.id,
                task.objective,
                task.scope.model_dump(),
                list(task.acceptance),
                list(task.verification),
                task.routing.model_dump(),
                status.value,
                preread=list(task.preread),
            )
            store.task_deps_insert(revision.id, task.id, list(task.dependencies))
            machine.record_insert(
                store, revision.id, task.id, status,
                reason="waiting on dependencies" if has_unmet else "no unmet dependencies",
            )
        if assignment is not None:
            store.assignment_set_status(
                assignment.id, AssignmentStatus.SUBMITTED, mark_submitted=True
            )
            # Close the planner span that belongs to this assignment. A
            # rejected submit never reaches here, so it cannot complete the
            # attempt. Direct submits with no assignment have nothing to close.
            open_attempt = store.attempt_open_for_assignment(assignment.id)
            if open_attempt is not None:
                store.attempt_update(
                    open_attempt.id, ended_at=db_now(), result="completed",
                    session_ref=session_ref,
                )

    if run.status == RunStatus.DONE.value:
        store.goal_set_status(goal.id, GoalStatus.ACTIVE)
    refresh(store, goal, run)
    return {
        "revision": revision.revision,
        "depth": revision.depth,
        "planner_profile": revision.planner_profile,
        "tasks": len(ir.tasks),
        "superseded_revision": old.revision if old else None,
        "cancelled_tasks": cancelled_old,
        "assignment": assignment.id if assignment else None,
    }


# ---------------------------------------------------------------------------
# Run slice (route runnable tasks; M0 parks host/external work)


def run_slice(project: Project) -> dict:
    """Route runnable tasks. Host/external work is parked; CLI work is
    executed through adapters, at most `effective_parallelism` (1) process
    per invocation — `orx run` returns so the Controller stays in the loop."""
    store = project.store
    goal, run = _active_context(project)
    revision = _active_revision(project, run)
    out = {
        "started": [],
        "host_required": [],
        "waiting_external": [],
        "failed": [],
        "deferred": [],
        "routing_errors": [],
    }
    if revision is None:
        out["note"] = "no active plan revision"
        return out

    started = 0
    cap = project.config.effective_parallelism
    parked_this_slice: set[str] = set()
    for task in store.tasks_all(revision.id):
        current = store.task_get(revision.id, task.task_id)
        if TaskStatus(current.status) is not TaskStatus.RUNNABLE:
            continue
        caps = tuple(current.routing.get("required_capabilities", []))
        request = routing.RouteRequest(role=Role.WORKER, required_capabilities=caps)
        result = routing.route(store, project.config, project.profiles, request)
        if not result.ok:
            routing.persist_decision(store, request, result)
            out["routing_errors"].append({"task": current.task_id, "error": result.error})
            continue
        profile = result.profile
        assert profile is not None
        if profile.driver.value == "host":
            parked_this_slice.add(task.task_id)
            out["host_required"].append(
                _park_task(project, goal, run, revision.id, current, result, request, profile, "host")
            )
        elif profile.driver.value == "external":
            parked_this_slice.add(task.task_id)
            out["waiting_external"].append(
                _park_task(project, goal, run, revision.id, current, result, request, profile, "external")
            )
        else:
            if started >= cap:
                out["deferred"].append(
                    {"task": current.task_id, "reason": "parallelism cap (M0: 1 process per run)"}
                )
                continue
            outcome = _execute_cli_task(project, goal, run, revision, current, result, request, profile)
            started += 1
            if outcome["status"] == "failed":
                out["failed"].append(outcome)
            else:
                out["started"].append(outcome)

    # Re-surface parked assignments: a resumed Controller (or a brand-new host
    # session) recovers the exact prompt file from state alone — no chat
    # context, no re-routing, no state change.
    for task in store.tasks_all(revision.id):
        if task.task_id in parked_this_slice:
            continue  # already reported as a fresh park above
        status = TaskStatus(task.status)
        if status not in (TaskStatus.WAITING_HOST, TaskStatus.WAITING_EXTERNAL):
            continue
        attempt = store.attempt_latest_for_task(revision.id, task.task_id)
        prompt_file = (
            project.root / ".orx" / "runs" / run.id / "assignments" / f"{task.task_id}.md"
        )
        entry = {
            "task": task.task_id,
            "status": task.status,
            "profile": attempt.profile if attempt else None,
            "prompt_file": (
                str(prompt_file.relative_to(project.root)) if prompt_file.exists() else None
            ),
            "preread": list(task.preread),
            "execution": (
                _execution_spec(project, project.profiles.get(attempt.profile), attempt)
                if attempt else None
            ),
            "resurfaced": True,
        }
        if status is TaskStatus.WAITING_HOST:
            entry["claim"] = f"orx task claim {task.task_id}"
            entry["isolation"] = "prompt_only"
            out["host_required"].append(entry)
        else:
            entry["finish_with"] = f"orx task complete {task.task_id} --evidence <file>"
            out["waiting_external"].append(entry)

    refresh(store, goal, run)
    return out


def _write_task_assignment_file(project: Project, run_id: str, task_id: str,
                                prompt: str, label: str | None = None) -> str:
    """Persist a host/external assignment prompt next to the planner
    assignments, so the Controller's subagent contract survives the session."""
    directory = project.root / ".orx" / "runs" / run_id / "assignments"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{label or task_id}.md"
    path.write_text(prompt)
    return str(path.relative_to(project.root))


def _park_task(project: Project, goal: Goal, run: Run, revision_row_id: int,
               task: TaskRow, result, request, profile, kind: str) -> dict:
    """Park a task for host/external execution and return the assignment
    payload. The prompt is the same composition a CLI launch would receive
    (constraints, scope, preread, acceptance, prior failure included)."""
    store = project.store
    prompt = worker_prompt(
        goal, task,
        prior_failure=_prior_failure_context(store, revision_row_id, task.task_id),
    )
    attempt = store.attempt_create(
        revision_row_id=revision_row_id,
        role=Role.WORKER.value,
        profile=profile.name,
        driver=profile.driver.value,
        harness=profile.harness.value,
        model_id=profile.model,
        requested_effort=profile.effort.value,
        routing_reason=result.reason,
        fallback_used=result.fallback_used,
        task_id=task.task_id,
        started=False,
        # The host enforces nothing — the prompt is discipline, not a sandbox.
        isolation="prompt_only" if kind == "host" else None,
        session_ref=_host_session_ref() if kind == "host" else None,
        run_id=run.id,
    )
    routing.persist_decision(store, request, result, attempt_id=attempt.id)
    machine.transition(
        store, revision_row_id, task.task_id,
        "route_host" if kind == "host" else "route_external",
        TaskStatus.WAITING_HOST if kind == "host" else TaskStatus.WAITING_EXTERNAL,
        reason=f"routed to {kind} profile '{profile.name}'",
    )
    entry = {
        "task": task.task_id,
        "objective": task.objective,
        "profile": profile.name,
        "prompt": prompt,
        "prompt_file": _write_task_assignment_file(project, run.id, task.task_id, prompt),
        "preread": list(task.preread),
        "execution": _execution_spec(project, profile, attempt),
    }
    if kind == "host":
        entry["claim"] = f"orx task claim {task.task_id}"
        entry["isolation"] = "prompt_only"
    else:
        entry["finish_with"] = f"orx task complete {task.task_id} --evidence <file>"
    return entry


def _execute_cli_task(project: Project, goal: Goal, run: Run, revision: Revision,
                      task: TaskRow, result, request, profile) -> dict:
    """One CLI task execution: probe, launch, record, verify. Execution
    finished != passed — verification still decides."""
    store = project.store
    adapter = adapters.get_adapter(profile.harness.value)
    probe = adapter.probe()

    scratch = _launch_dir(project, run.id, f"worker-{task.task_id}")
    launch = adapter.build_worker_launch(
        root=project.root,
        scratch=scratch,
        profile=profile,
        prompt=worker_prompt(
            goal, task,
            prior_failure=_prior_failure_context(store, revision.id, task.task_id),
        ),
        timeout=project.config.command_timeout_sec,
    )
    attempt = store.attempt_create(
        revision_row_id=revision.id,
        role=Role.WORKER.value,
        profile=profile.name,
        driver="cli",
        harness=profile.harness.value,
        model_id=profile.model,
        requested_effort=profile.effort.value,
        routing_reason=result.reason,
        fallback_used=result.fallback_used,
        task_id=task.task_id,
        # Stamp the span at creation. started=False left completed CLI
        # workers with a null started_at (the update path only writes ended_at).
        started=True,
        isolation=launch.sandbox,
        run_id=run.id,
    )
    routing.persist_decision(store, request, result, attempt_id=attempt.id)
    machine.transition(
        store, revision.id, task.task_id, "route_cli", TaskStatus.RUNNING,
        reason=f"routed to cli profile '{profile.name}'",
    )

    if not probe.ok:
        reason = f"capability_mismatch: {probe.detail}"
        store.attempt_mark_usage_missing(attempt.id, "execution_failure")
        store.attempt_update(attempt.id, ended_at=db_now(), result="failed",
                             failure_reason=reason)
        machine.transition(store, revision.id, task.task_id, "fail", TaskStatus.FAILED,
                           reason=reason, failure_reason=reason)
        return {"task": task.task_id, "profile": profile.name, "status": "failed", "reason": reason}

    run_result = runtime.run_launch(launch)
    log = _record_execution_log(project, run.id, f"{task.task_id}-{attempt.id}", run_result)
    effort = adapter.effort_outcome(launch, run_result)
    _record_usage(store, adapter, launch, run_result, attempt, profile.name,
                  run_id=run.id, task_id=task.task_id)
    health.record_attempt_outcome(
        store, profile.name, ok=run_result.ok,
        error_kind=None if run_result.ok else classify_failure(run_result))
    store.evidence_add(attempt.id, "execution", log)
    store.attempt_update(
        attempt.id, ended_at=db_now(),
        result="completed" if run_result.ok else "failed",
        actual_effort=effort.actual if effort.actual else None,
        effort_source=effort.source,
        failure_reason=None if run_result.ok else _failure_excerpt(run_result),
    )

    if not run_result.ok:
        reason = _failure_excerpt(run_result)
        machine.transition(store, revision.id, task.task_id, "fail", TaskStatus.FAILED,
                           reason=reason, failure_reason=reason)
        return {"task": task.task_id, "profile": profile.name, "status": "failed",
                "reason": reason, "log": log}

    machine.transition(store, revision.id, task.task_id, "complete", TaskStatus.VERIFYING,
                       reason="cli execution finished; verification starting")
    verify.run_command_verifications(
        store, project.root, run.id, revision.id,
        store.task_get(revision.id, task.task_id),
        timeout=project.config.command_timeout_sec,
        attempt_id=attempt.id,
    )
    verdict = verify.apply_verdict(store, revision.id, store.task_get(revision.id, task.task_id))
    refresh(store, goal, run)
    return {
        "task": task.task_id,
        "profile": profile.name,
        "status": store.task_get(revision.id, task.task_id).status,
        "verdict": verdict,
        "log": log,
        "attempt": attempt.id,
        "actual_effort": effort.actual,
        "effort_source": effort.source,
    }


def _prior_failure_context(store: Store, revision_id: int, task_id: str) -> str:
    """The most recent recorded failure for a task, for retry prompts. The
    task row itself clears failure_reason on retry, so history is the
    source. Empty string for a first attempt."""
    events = store.task_events(revision_id, task_id)
    for event in reversed(events):
        if event.to_status == "failed" and (event.reason or ""):
            return (
                f"\nA previous attempt at this task FAILED with:\n"
                f"  {event.reason}\n"
                f"Fix that specific problem; do not redo the task blindly.\n"
            )
    return ""


def worker_prompt(goal: Goal, task: TaskRow, prior_failure: str = "") -> str:
    acceptance = "\n".join(f"  - {item!r}" for item in task.acceptance) or "  (none listed)"
    verification = "\n".join(f"  - {entry}" for entry in task.verification) or "  (none)"
    allowed = ", ".join(task.scope.get("allowed", [])) or "(none)"
    constraints = "\n".join(f"  - {c}" for c in goal.constraints) or "  (none)"
    context_block = (
        "Goal context (background; binding only where it repeats a constraint):\n"
        + goal.context
        + "\n\n"
        if goal.context
        else ""
    )
    preread = "\n".join(f"  - {path}" for path in task.preread) or "  (not specified)"
    return f"""You are an ORX worker for Goal {goal.id}: {goal.objective}

Your assignment is ONE task. Do only this task and stay inside its scope.

Task {task.task_id}: {task.objective}

Goal constraints (binding for this work):
{constraints}

{context_block}Scope (write only inside these project-relative paths): {allowed}
Read these files first — before any exploration, and only these plus the
files you yourself create or modify (project-relative):
{preread}
Acceptance (your work is verified against these, verbatim):
{acceptance}
{prior_failure}
How your work will be checked:
{verification}

Rules:
- do not modify the Goal text or other tasks' scope
- if you are blocked, leave the workspace unchanged and exit non-zero with the
  blocker in your output; do not widen scope
- exit 0 when the task is done
"""


def verifier_prompt(goal: Goal, task: TaskRow, item, gate_summary: str = "",
                    evidence_lines: str = "", prior_issues: str = "") -> str:
    capabilities = ", ".join(item.capabilities) or "none"
    acceptance = "\n".join(f"  - {item!r}" for item in task.acceptance) or "  (none listed)"
    constraints = "\n".join(f"  - {c}" for c in goal.constraints) or "  (none)"
    context_blocks = ""
    if gate_summary:
        context_blocks += (
            "\nDeterministic checks already run for this task (do not re-run them):\n"
            f"{gate_summary}\n"
        )
    if evidence_lines:
        context_blocks += f"\nExecution evidence for this task:\n{evidence_lines}\n"
    if prior_issues:
        context_blocks += prior_issues
    return f"""You are an ORX verifier for Goal {goal.id}: {goal.objective}

Verify ONE acceptance check for task {task.task_id} ({task.objective}).

Check to verify (exact instruction): {item.spec}
Required capabilities: {capabilities}

Goal constraints (the work must respect these):
{constraints}
Task acceptance criteria (judge against these, verbatim):
{acceptance}{context_blocks}
Look at the project workspace at the current working directory. Then print
EXACTLY two final lines, nothing after them:

ORX_REASON=<one short line: what you checked, and for a fail what is missing>
ORX_VERDICT=pass
or
ORX_VERDICT=fail

A fail WITHOUT a reason line is an invalid verdict: the Controller cannot
act on an unexplained failure. Nothing else counts as a verdict. Exit 0
after printing both lines.
"""


def _gate_summary(store: Store, revision_id: int, task: TaskRow) -> str:
    """Recorded command-check results for a task, for verifier context."""
    rows = [v for v in store.verifications_for(revision_id, task.task_id) if v.kind == "command"]
    lines = []
    for v in rows:
        exit_note = "denied" if v.exit_code is None else f"exit {v.exit_code}"
        lines.append(f"  - {v.command} -> {exit_note} ({'passed' if v.passed else 'FAILED'})")
    return "\n".join(lines)


def _evidence_lines(store: Store, revision_id: int, task_id: str) -> str:
    """Evidence paths attached to a task's attempts (execution logs, completion
    and verification evidence), most recent last."""
    rows = store.evidence_for_task(revision_id, task_id)
    return "\n".join(f"  - [{kind}] {path}" for kind, path in rows[-5:])


def _prior_issues(store: Store, revision_id: int, task_id: str) -> str:
    """Issues from earlier failed attempts/verifications, for re-verification.
    Verification rows are cleared on retry, so history lives in task_events."""
    seen: list[str] = []
    for event in store.task_events(revision_id, task_id):
        if event.to_status == "failed" and event.reason and event.reason not in seen:
            seen.append(event.reason)
    if not seen:
        return ""
    issues = "\n".join(f"  - {reason}" for reason in seen[-3:])
    return (
        "\nEarlier attempts at this task failed with (check whether these issues"
        f" are resolved and whether the fix introduced anything new):\n{issues}\n"
    )


# ---------------------------------------------------------------------------
# Task lifecycle


def task_list(project: Project) -> list[dict]:
    goal, run = _active_context(project)
    revision = _active_revision(project, run)
    if revision is None:
        return []
    rows = []
    for task in project.store.tasks_all(revision.id):
        attempt = project.store.attempt_latest_for_task(revision.id, task.task_id)
        deps = project.store.deps_for(revision.id, task.task_id)
        statuses = {
            t.task_id: TaskStatus(t.status) for t in project.store.tasks_all(revision.id)
        }
        blocked_by = [d for d in deps if statuses.get(d) is not TaskStatus.PASSED]
        rows.append(
            {
                "id": task.task_id,
                "objective": task.objective,
                "status": task.status,
                "profile": attempt.profile if attempt else None,
                "dependencies": deps,
                "blocked_by": blocked_by,
                "failure_reason": task.failure_reason,
                "session_ref": attempt.session_ref if attempt else None,
            }
        )
    return rows


def task_claim(project: Project, task_id: str, session: str | None = None) -> dict:
    store = project.store
    goal, run = _active_context(project)
    revision, task = _active_task(project, run, task_id)
    if TaskStatus(task.status) not in (TaskStatus.WAITING_HOST,):
        if TaskStatus(task.status) is TaskStatus.RUNNABLE:
            raise ConflictError(
                f"task {task_id} is runnable but not routed yet; run `orx run` first"
            )
        raise ConflictError(f"task {task_id} is {task.status}, not waiting_host")
    # Validate the caller's session before the claim transition.
    session_ref = resolve_session_ref(session)
    machine.claim(store, revision.id, task.task_id)
    attempt = store.attempt_latest_for_task(revision.id, task.task_id)
    if attempt is not None and attempt.started_at is None:
        store.attempt_update(attempt.id, started_at=db_now())
    if attempt is not None and session_ref is not None:
        store.attempt_update(attempt.id, session_ref=session_ref)
        attempt = store.attempt_get(attempt.id)
    refresh_run(store, goal, run)
    return {
        "task": task.task_id,
        "status": "running",
        "attempt": attempt.id if attempt else None,
        "driver": "host",
        "session_ref": attempt.session_ref if attempt else None,
        "execution": (
            _execution_spec(project, project.profiles.get(attempt.profile), attempt)
            if attempt else None
        ),
    }


def task_complete(
    project: Project, task_id: str, evidence: str,
    attempt_id: int | None = None, actual_model: str | None = None,
) -> dict:
    """Record evidence and enter verification. Completion is NOT success:
    the task passes only when every verification entry passes.

    Phase B: a completion may quote the attempt id it answers (`task claim`
    returned it). A quoted attempt that is closed, foreign, or no longer the
    task's latest attempt is a stale submission and is rejected — a late
    worker must not close the attempt a retry already replaced."""
    store = project.store
    goal, run = _active_context(project)
    revision, task = _active_task(project, run, task_id)
    if TaskStatus(task.status) not in (TaskStatus.RUNNING, TaskStatus.WAITING_EXTERNAL):
        raise ConflictError(
            f"task {task_id} is {task.status}; `task complete` requires running or waiting_external"
        )

    evidence_path = Path(evidence).expanduser()
    if not evidence_path.is_absolute():
        evidence_path = (Path.cwd() / evidence_path).resolve()
    if not evidence_path.exists():
        raise NotFoundError(f"evidence file not found: {evidence}")

    if attempt_id is not None:
        attempt = store.attempt_get(attempt_id)
        if attempt.role != Role.WORKER.value:
            raise ConflictError(
                f"attempt {attempt_id} is a {attempt.role} attempt, not a worker attempt"
            )
        if attempt.revision_id != revision.id or attempt.task_id != task.task_id:
            raise ConflictError(
                f"stale submission: attempt {attempt_id} belongs to a different"
                " revision/task than the active one"
            )
        if attempt.ended_at is not None:
            raise ConflictError(
                f"stale submission: attempt {attempt_id} is already closed"
                f" (result {attempt.result!r})"
            )
        latest = store.attempt_latest_for_task(revision.id, task.task_id)
        if latest is not None and latest.id != attempt_id:
            raise ConflictError(
                f"stale submission: attempt {attempt_id} is not the latest attempt"
                f" for task {task_id} (a newer attempt exists); a late completion"
                " must not close it"
            )
    else:
        attempt = store.attempt_latest_for_task(revision.id, task.task_id)
        if attempt is None:
            attempt = store.attempt_create(
                revision_row_id=revision.id,
                role=Role.WORKER.value,
                profile="unrouted-host",
                driver="host",
                harness="zcode",
                model_id="unknown",
                requested_effort="medium",
                task_id=task.task_id,
                isolation="prompt_only",
                session_ref=_host_session_ref(),
                run_id=run.id,
            )
    mismatch = _record_reported_model(store, attempt.id, attempt.model, actual_model)
    store.evidence_add(attempt.id, "completion", str(evidence_path))
    store.attempt_update(attempt.id, ended_at=db_now(), result="completed")
    machine.transition(
        store, revision.id, task.task_id, "complete", TaskStatus.VERIFYING,
        reason="completion claimed; verification starting",
    )

    verify.run_command_verifications(
        store,
        project.root,
        run.id,
        revision.id,
        store.task_get(revision.id, task.task_id),
        timeout=project.config.command_timeout_sec,
        attempt_id=attempt.id,
    )
    verdict = verify.apply_verdict(store, revision.id, store.task_get(revision.id, task.task_id))
    refresh(store, goal, run)
    counts = _verification_counts(store, revision)
    result = {
        "task": task.task_id,
        "status": store.task_get(revision.id, task.task_id).status,
        "verdict": verdict,
        "verification": counts,
        "attempt": attempt.id,
    }
    if mismatch is not None:
        result["model_mismatch"] = mismatch
    return result


def task_fail(project: Project, task_id: str, reason: str) -> dict:
    store = project.store
    goal, run = _active_context(project)
    revision, task = _active_task(project, run, task_id)
    if TaskStatus(task.status) not in (TaskStatus.RUNNING, TaskStatus.WAITING_EXTERNAL):
        raise ConflictError(
            f"task {task_id} is {task.status}; `task fail` requires running or waiting_external"
        )
    attempt = store.attempt_latest_for_task(revision.id, task.task_id)
    if attempt is not None:
        store.attempt_update(
            attempt.id,
            ended_at=db_now(),
            result="failed",
            failure_reason=reason,
        )
    machine.transition(
        store, revision.id, task.task_id, "fail", TaskStatus.FAILED, reason=reason,
        failure_reason=reason,
    )
    refresh(store, goal, run)
    return {"task": task.task_id, "status": "failed"}


def task_retry(project: Project, task_id: str) -> dict:
    """Retry a failed task on the same plan: failed -> runnable. Not a replan."""
    store = project.store
    goal, run = _active_context(project)
    revision, task = _active_task(project, run, task_id)
    if TaskStatus(task.status) is not TaskStatus.FAILED:
        raise ConflictError(f"task {task_id} is {task.status}; `task retry` requires failed")
    store.verifications_clear(revision.id, task.task_id)
    machine.transition(
        store, revision.id, task.task_id, "retry", TaskStatus.RUNNABLE,
        reason="operator requested retry; new attempt will be routed by `orx run`",
    )
    refresh_run(store, goal, run)
    return {"task": task.task_id, "status": "runnable"}


def _verification_counts(store: Store, revision: Revision) -> dict:
    rows = []
    for task in store.tasks_all(revision.id):
        rows.extend(store.verifications_for(revision.id, task.task_id))
    pending = 0
    for task in store.tasks_all(revision.id):
        if TaskStatus(task.status) is TaskStatus.VERIFYING:
            pending += len(verify.pending_agent_entries(store, revision.id, task))
    return {
        "passed": sum(1 for v in rows if v.passed),
        "failed": sum(1 for v in rows if not v.passed),
        "awaiting_agent": pending,
    }


# ---------------------------------------------------------------------------
# Verification dispatch + submission


def verify_dispatch(project: Project) -> dict:
    store = project.store
    goal, run = _active_context(project)
    revision = _active_revision(project, run)
    out = {"checked": [], "agent_required": [], "launched": []}
    if revision is None:
        out["note"] = "no active plan revision"
        return out

    for task in store.tasks_all(revision.id):
        if TaskStatus(task.status) is not TaskStatus.VERIFYING:
            continue
        verify.run_command_verifications(
            store, project.root, run.id, revision.id, task,
            timeout=project.config.command_timeout_sec,
        )
        verdict = verify.apply_verdict(store, revision.id, task)
        out["checked"].append({"task": task.task_id, "verdict": verdict})
        if TaskStatus(task.status) is not TaskStatus.VERIFYING:
            continue  # a failed command gate already failed the task
        for index, item in enumerate(verify.pending_agent_entries(store, revision.id, task), start=1):
            request = routing.RouteRequest(
                role=Role.VERIFIER, required_capabilities=item.capabilities
            )
            result = routing.route(store, project.config, project.profiles, request)
            if not result.ok:
                routing.persist_decision(store, request, result)
                out["agent_required"].append(
                    {
                        "task": task.task_id,
                        "entry": item.raw,
                        "required_capabilities": list(item.capabilities) or ["coding"],
                        "error": result.error,
                    }
                )
                continue
            routing.persist_decision(store, request, result)
            profile = result.profile
            assert profile is not None
            entry_out = {
                "task": task.task_id,
                "entry": item.raw,
                "required_capabilities": list(item.capabilities) or ["coding"],
                "profile": profile.name,
                "driver": profile.driver.value,
            }
            if profile.driver.value == "host":
                # Phase B execution contract: the verifier identity is fixed
                # HERE, at dispatch. A persistent attempt is opened (not yet
                # started) and bound to this exact entry; the verdict must
                # close it. Re-dispatch reuses the open attempt instead of
                # opening a second execution (duplicate dispatch never
                # executes twice), and routing edits between dispatch and
                # submit cannot move the attribution.
                attempt = store.attempt_open_verifier_for_entry(
                    revision.id, task.task_id, item.raw
                )
                if attempt is None:
                    attempt = store.attempt_create(
                        revision_row_id=revision.id,
                        role=Role.VERIFIER.value,
                        profile=profile.name,
                        driver=profile.driver.value,
                        harness=profile.harness.value,
                        model_id=profile.model,
                        requested_effort=profile.effort.value,
                        routing_reason=result.reason,
                        fallback_used=result.fallback_used,
                        task_id=task.task_id,
                        started=False,
                        isolation="prompt_only",
                        run_id=run.id,
                        verify_entry=item.raw,
                    )
                prompt = verifier_prompt(
                    goal, task, item,
                    gate_summary=_gate_summary(store, revision.id, task),
                    evidence_lines=_evidence_lines(store, revision.id, task.task_id),
                    prior_issues=_prior_issues(store, revision.id, task.task_id),
                )
                entry_out["attempt"] = attempt.id
                entry_out["execution"] = _execution_spec(project, profile, attempt)
                entry_out["prompt"] = prompt
                entry_out["prompt_file"] = _write_task_assignment_file(
                    project, run.id, task.task_id, prompt, label=f"verify-{task.task_id}-{index:02d}"
                )
                entry_out["isolation"] = "prompt_only"
                entry_out["submit_pass"] = (
                    f"orx verify submit {task.task_id} --result pass --entry {item.raw!r}"
                    f" --attempt {attempt.id}"
                )
                entry_out["submit_fail"] = (
                    f"orx verify submit {task.task_id} --result fail --entry {item.raw!r}"
                    f" --attempt {attempt.id} --reason \"<issues for the fix loop>\""
                )
                out["agent_required"].append(entry_out)
            else:
                out["launched"].append(
                    _run_cli_verifier(project, goal, run, revision, task, item,
                                      profile, request, result)
                )

    refresh(store, goal, run)
    return out


def _run_cli_verifier(project: Project, goal: Goal, run: Run, revision: Revision,
                      task: TaskRow, item, profile, request, result) -> dict:
    """Launch a CLI verifier. The deterministic verdict contract is one line
    `ORX_VERDICT=pass` or `ORX_VERDICT=fail`; anything else is a failed
    verification (a broken verifier must not hang the run forever)."""
    store = project.store
    adapter = adapters.get_adapter(profile.harness.value)
    probe = adapter.probe()
    scratch = _launch_dir(project, run.id, f"verify-{task.task_id}")
    launch = adapter.build_verifier_launch(
        root=project.root, scratch=scratch, profile=profile,
        prompt=verifier_prompt(
            goal, task, item,
            gate_summary=_gate_summary(store, revision.id, task),
            evidence_lines=_evidence_lines(store, revision.id, task.task_id),
            prior_issues=_prior_issues(store, revision.id, task.task_id),
        ),
        timeout=project.config.command_timeout_sec,
    )
    attempt = store.attempt_create(
        revision_row_id=revision.id,
        role=Role.VERIFIER.value,
        profile=profile.name,
        driver="cli",
        harness=profile.harness.value,
        model_id=profile.model,
        requested_effort=profile.effort.value,
        routing_reason=result.reason,
        fallback_used=result.fallback_used,
        task_id=task.task_id,
        started=True,
        isolation=launch.sandbox,
        run_id=run.id,
    )
    routing.persist_decision(store, request, result, attempt_id=attempt.id)

    if not probe.ok:
        reason = f"capability_mismatch: {probe.detail}"
        store.attempt_mark_usage_missing(attempt.id, "execution_failure")
        store.attempt_update(attempt.id, ended_at=db_now(), result="failed", failure_reason=reason)
        store.verification_add(
            revision.id, task.task_id, "agent", item.raw, passed=False,
            attempt_id=attempt.id, required_capabilities=list(item.capabilities),
        )
        verify.apply_verdict(store, revision.id, task, failure_hint=item.raw)
        return {"task": task.task_id, "entry": item.raw, "verdict": "failed", "reason": reason}

    run_result = runtime.run_launch(launch)
    log = _record_execution_log(project, run.id, f"verify-{task.task_id}-{attempt.id}", run_result)
    _record_usage(store, adapter, launch, run_result, attempt, profile.name,
                  run_id=run.id, task_id=task.task_id)
    health.record_attempt_outcome(
        store, profile.name, ok=run_result.ok,
        error_kind=None if run_result.ok else classify_failure(run_result))
    # Scan the agent's extracted message FIRST: CLI agents wrap their output
    # in a JSON envelope (Cursor) where newlines are escaped, so a verdict
    # line inside the raw stdout never appears as a standalone line. The raw
    # stream remains the fallback (shell workers print markers directly).
    message = adapter.extract_text(launch, run_result)
    streams = message + "\n" + run_result.stdout + "\n" + run_result.stderr
    verdict_text = scan_marker(streams, "ORX_VERDICT")
    reason_text = scan_marker(streams, "ORX_REASON")
    if run_result.ok and verdict_text in ("pass", "fail"):
        passed = verdict_text == "pass"
    else:
        passed = False
        verdict_text = "no ORX_VERDICT line" if run_result.ok else _failure_excerpt(run_result)
    detail = verdict_text if not reason_text else f"{verdict_text}: {reason_text}"
    store.attempt_update(
        attempt.id, ended_at=db_now(),
        result="pass" if passed else "fail",
        failure_reason=None if passed else f"agent verifier: {detail}",
    )
    store.verification_add(
        revision.id, task.task_id, "agent", item.raw, passed=passed,
        attempt_id=attempt.id, output_path=log,
        required_capabilities=list(item.capabilities),
    )
    verify.apply_verdict(store, revision.id, task, failure_hint=detail if not passed else None)
    return {
        "task": task.task_id,
        "entry": item.raw,
        "verdict": "pass" if passed else "fail",
        "detail": detail,
        "log": log,
    }


def _normalize_model(name: str) -> str:
    """Compare models by their bare id: profiles carry provider-qualified ids
    (`account:…/GLM-5.3`) while executor receipts report the short form
    (`GLM-5.3`). Anything after the last `/` is the comparable name."""
    return name.rsplit("/", 1)[-1].strip()


def _record_reported_model(store: Store, attempt_id: int, requested: str,
                           actual_model: str | None) -> dict | None:
    """Store the executor-reported model (source: 'reported' — e.g. a ZCode
    dispatch receipt; db model_usage is the authority behind it). Returns a
    mismatch descriptor when the reported model is not the requested one —
    the verdict still counts, but the mismatch is visible, never silent."""
    if not actual_model or not actual_model.strip():
        return None
    actual_model = actual_model.strip()
    store.attempt_update(
        attempt_id, actual_model=actual_model, model_source="reported"
    )
    if _normalize_model(actual_model) != _normalize_model(requested):
        return {"requested": requested, "reported": actual_model}
    return None


def verify_submit(
    project: Project, task_id: str, result_value: str, entry: str | None, evidence: str | None,
    reason: str | None = None, session: str | None = None,
    attempt_id: int | None = None, actual_model: str | None = None,
) -> dict:
    """Record a host Agent verifier's verdict for one agent verification entry.
    `reason` carries the reviewer's issues on a fail; it becomes the recorded
    failure state and lands in the next worker/verifier prompt (fix loop).

    Phase B: the verdict closes the attempt whose identity was fixed at
    `verify` dispatch time. An explicit --attempt must be that open attempt;
    without one, the open dispatch attempt for the entry is preferred. Only a
    verdict with NO open dispatch attempt routes a verifier here (legacy
    submit-after-complete flow). Late or duplicate submissions are rejected."""
    store = project.store
    goal, run = _active_context(project)
    revision, task = _active_task(project, run, task_id)
    if TaskStatus(task.status) is not TaskStatus.VERIFYING:
        raise ConflictError(f"task {task_id} is {task.status}, not verifying")

    if result_value not in ("pass", "fail"):
        raise ORXError("--result must be 'pass' or 'fail'")
    # Host verifier attempts record the caller's real session. A malformed
    # explicit reference fails before the verdict or the attempt is written.
    # CLI verifier attempts do not copy it; a harness id is parsed later.
    session_ref = resolve_session_ref(session)
    passed = result_value == "pass"

    pending = verify.pending_agent_entries(store, revision.id, task)
    chosen = None
    if entry is not None:
        matches = [i for i in pending if i.raw == entry]
        if not matches:
            raise NotFoundError(f"no pending agent verification matching --entry {entry!r}")
        chosen = matches[0]
    elif pending:
        chosen = pending[0]
    else:
        raise NotFoundError(
            f"task {task_id} has no pending agent verification entries"
        )

    open_attempt = store.attempt_open_verifier_for_entry(
        revision.id, task.task_id, chosen.raw
    )

    if attempt_id is not None:
        # Submissions quote the dispatch identity; validate it fully before
        # anything is written. A closed, foreign, or superseded attempt is a
        # stale submission, not an error to paper over.
        attempt = store.attempt_get(attempt_id)
        if attempt.role != Role.VERIFIER.value:
            raise ConflictError(
                f"attempt {attempt_id} is a {attempt.role} attempt, not a verifier attempt"
            )
        if attempt.revision_id != revision.id or attempt.task_id != task.task_id:
            raise ConflictError(
                f"stale submission: attempt {attempt_id} belongs to a different"
                " revision/task than the active one"
            )
        if attempt.verify_entry is not None and attempt.verify_entry != chosen.raw:
            raise ConflictError(
                f"attempt {attempt_id} was dispatched for a different verification"
                f" entry ({attempt.verify_entry!r}), not {chosen.raw!r}"
            )
        if attempt.ended_at is not None:
            raise ConflictError(
                f"stale submission: attempt {attempt_id} is already closed"
                f" (result {attempt.result!r})"
            )
        bound = attempt
    elif open_attempt is not None:
        bound = open_attempt
    else:
        bound = None

    if bound is not None:
        if session_ref is not None:
            store.attempt_update(bound.id, session_ref=session_ref)
        store.attempt_update(
            bound.id,
            started_at=bound.started_at or db_now(),
            ended_at=db_now(),
            result="pass" if passed else "fail",
            failure_reason=None if passed else (
                f"agent verifier rejected: {reason or chosen.spec}"
            ),
        )
    else:
        # Legacy path: no dispatch-time attempt exists (verdict submitted
        # straight after task completion). Route a verifier now, as in M1.
        request = routing.RouteRequest(
            role=Role.VERIFIER, required_capabilities=chosen.capabilities
        )
        route_result = routing.route(store, project.config, project.profiles, request)
        if not route_result.ok:
            # A verifier must exist to accept a verdict on behalf of an agent.
            routing.persist_decision(store, request, route_result)
            raise RoutingError(route_result.error or "verifier routing failed")
        profile = route_result.profile
        assert profile is not None
        bound = store.attempt_create(
            revision_row_id=revision.id,
            role=Role.VERIFIER.value,
            profile=profile.name,
            driver=profile.driver.value,
            harness=profile.harness.value,
            model_id=profile.model,
            requested_effort=profile.effort.value,
            routing_reason=route_result.reason,
            fallback_used=route_result.fallback_used,
            task_id=task.task_id,
            isolation="prompt_only" if profile.driver.value == "host" else None,
            session_ref=(
                session_ref if profile.driver.value == "host" else None
            ),
            run_id=run.id,
            verify_entry=chosen.raw if profile.driver.value == "host" else None,
        )
        routing.persist_decision(store, request, route_result, attempt_id=bound.id)
        store.attempt_update(
            bound.id,
            ended_at=db_now(),
            result="pass" if passed else "fail",
            failure_reason=None if passed else (
                f"agent verifier rejected: {reason or chosen.spec}"
            ),
        )

    mismatch = _record_reported_model(store, bound.id, bound.model, actual_model)

    evidence_rel = None
    if evidence:
        evidence_path = Path(evidence).expanduser()
        if not evidence_path.is_absolute():
            evidence_path = (Path.cwd() / evidence_path).resolve()
        if not evidence_path.exists():
            raise NotFoundError(f"evidence file not found: {evidence}")
        evidence_rel = str(evidence_path)
        store.evidence_add(bound.id, "verification", evidence_rel)

    store.verification_add(
        revision.id, task.task_id, "agent", chosen.raw, passed=passed,
        attempt_id=bound.id, exit_code=None, output_path=evidence_rel,
        required_capabilities=list(chosen.capabilities),
    )

    verdict = verify.apply_verdict(
        store, revision.id, store.task_get(revision.id, task.task_id),
        failure_hint=reason or (chosen.raw if not passed else None),
    )
    refresh(store, goal, run)
    result = {
        "task": task.task_id,
        "entry": chosen.raw,
        "result": result_value,
        "status": store.task_get(revision.id, task.task_id).status,
        "verdict": verdict,
        "attempt": bound.id,
    }
    if mismatch is not None:
        result["model_mismatch"] = mismatch
    return result


# ---------------------------------------------------------------------------
# Resources / status


def resource_clear(project: Project, profile_name: str) -> dict:
    """Re-enable health auto-learning for a profile (drops the override)."""
    if profile_name not in project.profiles:
        raise NotFoundError(
            f"profile {profile_name!r} is not defined"
        )
    project.store.resource_clear(profile_name)
    return {"profile": profile_name, "override": False}


def resource_set(project: Project, profile_name: str, status_value: str, note: str = "") -> dict:
    if profile_name not in project.profiles:
        raise NotFoundError(
            f"profile {profile_name!r} is not defined in profiles.toml"
        )
    try:
        status = ResourceStatus(status_value)
    except ValueError:
        raise ORXError(
            f"invalid resource status {status_value!r} (expected one of: "
            + " | ".join(s.value for s in ResourceStatus) + ")"
        ) from None
    project.store.resource_set(profile_name, status, note)
    return {"profile": profile_name, "status": status.value, "note": note}


def resource_list(project: Project) -> list[dict]:
    rows = {r.profile: r for r in project.store.resource_rows()}
    out = []
    for name in project.profiles:
        row = rows.get(name)
        out.append(
            {
                "profile": name,
                "status": row.status if row else "unknown",
                "note": row.note if row else "",
                "updated_at": row.updated_at if row else None,
            }
        )
    for name, row in rows.items():
        if name not in project.profiles:
            out.append(
                {"profile": name, "status": row.status, "note": row.note,
                 "updated_at": row.updated_at}
            )
    return out


# ---------------------------------------------------------------------------
# Agent discovery (M1 P2). Probes never launch a completion.


_LAUNCH_CONTRACTS: dict[str, dict] = {
    "codex": {
        "kind": "adapter",
        "summary": (
            "codex exec --json -m <model> -C <root> -s workspace-write "
            "--ephemeral --output-last-message <file> "
            "[-c model_reasoning_effort=<validated>] <prompt>. "
            "Planner launches may add --output-schema. "
            "A probe never runs this completion."
        ),
    },
    "cursor": {
        "kind": "adapter",
        "summary": (
            "agent --print --output-format json --workspace <root> --trust "
            "--model '<id>[effort=<mapped>]' <prompt> "
            "(--force when the profile sets force). "
            "A probe never runs this completion."
        ),
    },
    "shell": {
        "kind": "adapter",
        "summary": (
            "Runs the profile executable. prompt_transport delivers the prompt "
            "on stdin, in an argv {prompt} slot, or via {prompt_file}. "
            "Nothing to probe; no fixed completion argv."
        ),
    },
    "zcode": {
        "kind": "host",
        "summary": (
            "Host-only harness. The host does the work; there is no CLI launch. "
            "Submit through orx task claim and orx task complete."
        ),
    },
}


def _harness_spec(name: str):
    try:
        return probes.HARNESSES[name]
    except KeyError:
        known = ", ".join(probes.HARNESSES)
        raise NotFoundError(
            f"unknown harness {name!r}; known harnesses: {known}"
        ) from None


def _has_adapter(name: str) -> bool:
    try:
        adapters.get_adapter(name)
    except ORXError:
        return False
    return True


def agent_list() -> list[dict]:
    """Harnesses that have an adapter, plus the host-only zcode harness."""
    rows = []
    for name, spec in probes.HARNESSES.items():
        host_only = spec.host_only
        rows.append(
            {
                "harness": name,
                "binary": spec.binary or None,
                "adapter": _has_adapter(name),
                "host_only": host_only,
                "probeable": not host_only and name != "shell",
            }
        )
    return rows


def agent_info(harness: str) -> dict:
    """Latest persisted snapshot (or null) plus the launch-contract summary.

    Does not probe and does not launch a completion.
    """
    spec = _harness_spec(harness)
    return {
        "harness": harness,
        "binary": spec.binary or None,
        "adapter": _has_adapter(harness),
        "host_only": spec.host_only,
        "snapshot": probes.load_snapshot(harness),
        "launch": dict(_LAUNCH_CONTRACTS[harness]),
    }


def _health_reason(row) -> str:
    """REASON column: last_error_kind and note, plus retry/reset/override."""
    if row is None:
        return ""
    parts: list[str] = []
    if row.last_error_kind:
        parts.append(row.last_error_kind)
    if row.note:
        parts.append(row.note)
    if row.status == "cooldown" and row.cooldown_until:
        parts.append(f"retry {row.cooldown_until}")
    if row.quota_reset_at:
        parts.append(f"reset {row.quota_reset_at}")
    if row.override:
        parts.append("override")
    return "; ".join(parts)


def _health_entry(name: str, row) -> dict:
    updated_at = row.updated_at if row else None
    return {
        "profile": name,
        "state": row.status if row else "unknown",
        "since": updated_at,
        "updated_at": updated_at,
        "reason": _health_reason(row),
        "last_error_kind": row.last_error_kind if row else None,
        "note": row.note if row else "",
        "cooldown_until": row.cooldown_until if row else None,
        "quota_reset_at": row.quota_reset_at if row else None,
        "override": bool(row.override) if row else False,
    }


def agent_status(project: Project) -> dict:
    """Per-profile health view sourced from resource_status.

    Every configured profile is included (a missing row is `unknown`).
    Rows left behind for profiles no longer defined are appended after.
    """
    rows = {r.profile: r for r in project.store.resource_rows()}
    profiles = [_health_entry(name, rows.get(name)) for name in project.profiles]
    for name, row in rows.items():
        if name not in project.profiles:
            profiles.append(_health_entry(name, row))
    return {"profiles": profiles}


def agent_probe(harness: str) -> dict:
    """Run the shared probes and persist the capability snapshot.

    Host-only and generic harnesses are a domain error. A missing binary is
    still a successful probe: the snapshot records binary=null.
    """
    _harness_spec(harness)
    try:
        snapshot = probes.capability_snapshot(harness)
    except ValueError as exc:
        raise ORXError(str(exc)) from None
    return {
        "harness": harness,
        "snapshot": snapshot,
        "path": str(probes.snapshot_path(harness)),
    }


def status_data(project: Project) -> dict:
    goal, run = _active_context(project)
    revision = _active_revision(project, run)
    plan_info = None
    if revision is not None:
        plan_info = {
            "revision": revision.revision,
            "depth": revision.depth,
            "planner_profile": revision.planner_profile,
            "status": revision.status,
        }
    return {
        "goal": {
            "id": goal.id,
            "objective": goal.objective,
            "acceptance": goal.acceptance,
            "constraints": goal.constraints,
            "status": goal.status,
        },
        "run": {
            "id": run.id,
            "status": run.status,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
        },
        "plan": plan_info,
        "tasks": task_list(project),
        "verification": (
            _verification_counts(project.store, revision) if revision
            else {"passed": 0, "failed": 0, "awaiting_agent": 0}
        ),
        "resources": resource_list(project),
        "result": run.status,
    }


# ---------------------------------------------------------------------------
# Timeline (read model; no new tables)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _ts_key(ts: str) -> tuple:
    try:
        return (0, _parse_ts(ts))
    except ValueError:
        return (1, ts)


def _detail(*parts: str) -> str:
    text = " ".join(part.strip() for part in parts if part and part.strip())
    return " ".join(text.split())


def _run_at(stamp: str | None, runs: list[Run]) -> str | None:
    """Run whose creation is the latest one at or before `stamp`."""
    if not runs:
        return None
    if stamp is None:
        return runs[0].id if len(runs) == 1 else None
    eligible = [run for run in runs if run.created_at <= stamp]
    if not eligible:
        return runs[0].id if len(runs) == 1 else None
    eligible.sort(key=lambda run: (run.created_at, run.id))
    return eligible[-1].id


def _nearest_attempt(event, attempts: list):
    related = [
        attempt for attempt in attempts
        if attempt.revision_id == event.revision_id and attempt.task_id == event.task_id
    ]
    if not related:
        return None
    best = None
    best_dist: float | None = None
    for attempt in related:
        stamps = [stamp for stamp in (attempt.started_at, attempt.ended_at) if stamp]
        if not stamps:
            dist = None
        else:
            try:
                dist = min(abs((_parse_ts(event.created_at) - _parse_ts(stamp)).total_seconds())
                           for stamp in stamps)
            except ValueError:
                dist = None
        if best is None or (dist is not None and (best_dist is None or dist < best_dist)):
            best = attempt
            best_dist = dist if dist is not None else best_dist
    return best


def timeline(
    project: Project,
    run_id: str | None = None,
    task_id: str | None = None,
    profile: str | None = None,
    limit: int | None = None,
) -> dict:
    """Merge existing audit rows into time-ordered `{ts, actor, event, detail}`.

    Sources: goal and run creation, planning assignments, routing decisions,
    attempts (planner, worker, verifier), task events, and verifications.
    `limit` keeps the newest N entries, still oldest-first.
    """
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ORXError("limit must be a positive integer")

    store = project.store
    goals = store.goals_all()
    runs = store.runs_all()
    if run_id is not None and not any(run.id == run_id for run in runs):
        raise NotFoundError(f"run {run_id} not found")
    known_tasks = {task.task_id for task in store.tasks_every()}
    if task_id is not None and task_id not in known_tasks:
        raise NotFoundError(f"task {task_id} not found")

    revisions = store.revisions_all()
    rev_run = {revision.id: revision.run_id for revision in revisions}
    attempts = store.attempts_all()
    attempts_by_id = {attempt.id: attempt for attempt in attempts}
    first_run: dict[str, str] = {}
    for run in runs:
        first_run.setdefault(run.goal_id, run.id)

    raw: list[dict] = []
    seq = 0

    def add(ts: str | None, actor: str | None, event: str, detail: str, *,
            run: str | None, task: str | None, prof: str | None) -> None:
        nonlocal seq
        if not ts:
            return
        seq += 1
        raw.append({
            "ts": ts,
            "actor": actor or "orx",
            "event": event,
            "detail": detail or "-",
            "_run": run,
            "_task": task,
            "_profile": prof,
            "_seq": seq,
        })

    for goal in goals:
        add(goal.created_at, "orx", "goal.created", _detail(goal.id, goal.objective),
            run=first_run.get(goal.id), task=None, prof=None)
    for run in runs:
        add(run.created_at, "orx", "run.created", _detail(run.id, "goal", run.goal_id),
            run=run.id, task=None, prof=None)

    for assignment in store.assignments_all():
        add(assignment.created_at, assignment.profile, "plan.assign",
            _detail(assignment.id, assignment.depth),
            run=assignment.run_id, task=None, prof=assignment.profile)
        if assignment.submitted_at:
            add(assignment.submitted_at, assignment.profile, "plan.submit",
                _detail(assignment.id, "submitted"),
                run=assignment.run_id, task=None, prof=assignment.profile)

    for attempt in attempts:
        run = None
        if attempt.revision_id is not None:
            run = rev_run.get(attempt.revision_id)
        if run is None:
            run = _run_at(attempt.started_at or attempt.ended_at, runs)
        label = attempt.task_id or attempt.assignment_id or "-"
        if attempt.started_at:
            add(attempt.started_at, attempt.profile, "attempt.start",
                _detail(attempt.role, label),
                run=run, task=attempt.task_id, prof=attempt.profile)
        if attempt.ended_at:
            add(attempt.ended_at, attempt.profile, "attempt.end",
                _detail(attempt.role, label, attempt.result or "-"),
                run=run, task=attempt.task_id, prof=attempt.profile)

    for decision in store.routing_decisions_all():
        attempt = attempts_by_id.get(decision.attempt_id) if decision.attempt_id else None
        if attempt is not None and attempt.revision_id is not None:
            run = rev_run.get(attempt.revision_id)
        else:
            run = _run_at(decision.created_at, runs)
        task = attempt.task_id if attempt is not None else None
        prof = attempt.profile if attempt is not None else decision.selected
        add(decision.created_at, prof, "route",
            _detail(decision.role, decision.selected or "-", decision.reason or ""),
            run=run, task=task, prof=prof)

    for event in store.task_events_all():
        nearest = _nearest_attempt(event, attempts)
        prof = nearest.profile if nearest is not None else None
        actor = prof or "orx"
        add(event.created_at, actor, event.event,
            _detail(event.task_id, f"{event.from_status or '-'} -> {event.to_status}",
                    event.reason or ""),
            run=rev_run.get(event.revision_id), task=event.task_id, prof=prof)

    for verification in store.verifications_all():
        attempt = attempts_by_id.get(verification.attempt_id) if verification.attempt_id else None
        prof = attempt.profile if attempt is not None else None
        outcome = "verify.pass" if verification.passed else "verify.fail"
        add(verification.created_at, prof, outcome,
            _detail(verification.task_id, verification.command),
            run=rev_run.get(verification.revision_id), task=verification.task_id, prof=prof)

    selected = []
    for entry in raw:
        if run_id is not None and entry["_run"] != run_id:
            continue
        if task_id is not None and entry["_task"] != task_id:
            continue
        if profile is not None and entry["_profile"] != profile:
            continue
        selected.append(entry)

    selected.sort(key=lambda entry: (_ts_key(entry["ts"]), entry["_seq"]))
    if limit is not None:
        selected = selected[-limit:]

    entries = [
        {"ts": entry["ts"], "actor": entry["actor"], "event": entry["event"], "detail": entry["detail"]}
        for entry in selected
    ]
    return {"entries": entries, "count": len(entries)}


# ---------------------------------------------------------------------------
# Usage (read model over attempts + usage_observations)


_ACCURACY_RANK = {"exact": 0, "estimated": 1, "unknown": 2}


def _attempt_runtime_sec(attempt) -> float:
    """Seconds between started_at and ended_at. Incomplete attempts add nothing."""
    if not attempt.started_at or not attempt.ended_at:
        return 0.0
    try:
        delta = (_parse_ts(attempt.ended_at) - _parse_ts(attempt.started_at)).total_seconds()
    except ValueError:
        return 0.0
    return delta if delta > 0 else 0.0


def _token_sum(rows: list, key: str) -> int | None:
    """Sum one token column. None when there is no complete observation of it.

    A missing value is not zero: publishing a partial sum would invent a total.
    """
    if not rows:
        return None
    values = [row[key] for row in rows]
    if any(value is None for value in values):
        return None
    return sum(int(value) for value in values)


def _accuracy_label(attempts: list, rows: list) -> str:
    """Coverage-aware label. All attempts covered and all rows exact -> exact;
    SOME attempts covered -> estimated (the token sums are real but partial —
    pre-v3 history and shell runs record nothing); no rows at all -> unknown.

    Unknown is a legal result: shell runs and truncated streams record no
    tokens. A missing observation never zero-fills or discards observed sums.
    """
    if not rows:
        return "unknown"
    covered = {row["attempt_id"] for row in rows}
    labels = [row["accuracy"] for row in rows]
    worst = max(labels, key=lambda label: _ACCURACY_RANK.get(label, 2))
    if any(attempt.id not in covered for attempt in attempts):
        return "estimated" if worst != "unknown" else "unknown"
    return worst


def _usage_entry(name: str, attempts: list, rows: list) -> dict:
    runtime = sum(_attempt_runtime_sec(attempt) for attempt in attempts)
    tasks = {attempt.task_id for attempt in attempts if attempt.task_id}
    return {
        "profile": name,
        "tasks": len(tasks),
        "runtime_sec": round(runtime, 6),
        "input_tokens": _token_sum(rows, "input_tokens"),
        "output_tokens": _token_sum(rows, "output_tokens"),
        "cached_input_tokens": _token_sum(rows, "cached_input_tokens"),
        "accuracy": _accuracy_label(attempts, rows),
    }


def _measurement_accuracy(rows: list) -> str:
    """Accuracy of stored observations only. Unobserved attempts do not change it.

    This is independent of `_accuracy_label`, which folds coverage into the
    profile aggregate. No observation stays `unknown`. Counts are not invented.
    """
    if not rows:
        return "unknown"
    labels = [row["accuracy"] for row in rows]
    return max(labels, key=lambda label: _ACCURACY_RANK.get(label, 2))


def _coverage_entry(name: str, attempts: list, rows: list) -> dict:
    covered = {row["attempt_id"] for row in rows}
    return {
        "profile": name,
        "attempts": len(attempts),
        "observed": sum(1 for attempt in attempts if attempt.id in covered),
        "measurement_accuracy": _measurement_accuracy(rows),
    }


def _observation_payload(row, session_ref: str | None) -> dict:
    return {
        "id": row["id"],
        "attempt_id": row["attempt_id"],
        "profile": row["profile"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cached_input_tokens": row["cached_input_tokens"],
        "source": row["source"],
        "accuracy": row["accuracy"],
        "created_at": row["created_at"],
        "session_ref": session_ref,
    }


def usage(project: Project, profile: str | None = None) -> dict:
    """Per-profile aggregates. Task counts and runtime come from attempts.

    Token sums come from usage_observations. Accuracy is exact, estimated, or
    unknown; unknown is success, not an error. A missing observation does not
    zero-fill tokens.

    Additive fields sit beside `profiles` and do not change that aggregate:
    `observations` (including `host_report`), `coverage` (measurement accuracy
    kept apart from how many attempts were observed), `sessions`, and `runs`
    lifecycle timestamps.
    """
    store = project.store
    attempts = store.attempts_all()
    rows = store.usage_rows(None)
    by_profile_attempts: dict[str, list] = {}
    for attempt in attempts:
        by_profile_attempts.setdefault(attempt.profile, []).append(attempt)
    by_profile_rows: dict[str, list] = {}
    for row in rows:
        by_profile_rows.setdefault(row["profile"], []).append(row)

    active = set(by_profile_attempts) | set(by_profile_rows)
    if profile is not None:
        if profile not in project.profiles and profile not in active:
            raise NotFoundError(f"unknown profile {profile}")
        names = [profile]
    else:
        names = [name for name in project.profiles if name in active]
        names.extend(sorted(active.difference(project.profiles)))

    profiles = [
        _usage_entry(name, by_profile_attempts.get(name, []), by_profile_rows.get(name, []))
        for name in names
    ]
    selected = set(names)
    attempts_by_id = {attempt.id: attempt for attempt in attempts}
    observations = [
        _observation_payload(
            row,
            attempts_by_id[row["attempt_id"]].session_ref
            if row["attempt_id"] in attempts_by_id else None,
        )
        for row in rows
        if row["profile"] in selected
    ]
    coverage = [
        _coverage_entry(
            name, by_profile_attempts.get(name, []), by_profile_rows.get(name, []),
        )
        for name in names
    ]
    sessions = [
        {
            "attempt": attempt.id,
            "role": attempt.role,
            "profile": attempt.profile,
            "task_id": attempt.task_id,
            "run_id": attempt.run_id,
            "session_ref": attempt.session_ref,
            "started_at": attempt.started_at,
            "ended_at": attempt.ended_at,
        }
        for attempt in attempts
        if attempt.profile in selected
    ]
    runs = [
        {
            "id": run.id,
            "status": run.status,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
        }
        for run in store.runs_all()
    ]
    return {
        "profiles": profiles,
        "observations": observations,
        "coverage": coverage,
        "sessions": sessions,
        "runs": runs,
    }


def usage_record(
    project: Project,
    attempt_id: int,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int | None,
    accuracy: str,
) -> dict:
    """Record one host_report. Metadata is read from the attempt, not the caller."""
    return project.store.usage_record_host(
        attempt_id, input_tokens, output_tokens, cached_input_tokens, accuracy,
    )
