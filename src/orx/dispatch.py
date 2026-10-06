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

from orx import adapters, machine, plan as plan_mod, probes, quota, routing, runtime, verify
from orx.adapters.base import classify_failure, quota_reset_from, scan_marker
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
    PlanSyntaxError,
    PlanValidationError,
    ResourceStatus,
    ReplanRejectedError,
    Role,
    RoutingError,
    RunStatus,
    TaskStatus,
    UNFINISHED_TASK_STATUSES,
)
from orx.state import (
    Assignment,
    Goal,
    Run,
    Revision,
    Store,
    TaskRow,
    Verification,
    mint_nonce,
    now as db_now,
)

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
# Same-session check-fix loop budget: how many `orx task check` rounds one
# worker attempt may use before the worker must exit structured
# failed/blocked back to the controller. Advisory contract (ORX never
# kills a session); it bounds what the worker prompt prescribes and what
# `orx task check` reports as check_rounds.
max_check_rounds = 3
# Long-silence threshold for host worker progress reports
# (`orx task heartbeat`), in minutes. Observation only: past this bound
# status/task list/recovery raise a check-the-original-session hint —
# never an automatic fail, retry, or second worker.
progress_timeout_min = 60

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


def _user_preset_installed() -> bool:
    """See config.user_preset_installed — single source for this rule."""
    return config_mod.user_preset_installed()


def init_project(root: Path) -> dict:
    """Create .orx/ if missing; refuse to clobber a non-empty config.

    With an installed user preset (both user-layer files present), the
    project inherits routing and profiles from it: no project config.toml or
    profiles.toml is written, so the preset stays effective and future preset
    updates are not shadowed per-project."""
    root = root.resolve()
    orx_dir = root / ".orx"
    orx_dir.mkdir(parents=True, exist_ok=True)
    (orx_dir / "runs").mkdir(exist_ok=True)

    created: list[str] = []
    inherited: list[str] = []
    config_path = orx_dir / "config.toml"
    profiles_path = orx_dir / "profiles.toml"
    if config_path.exists() and config_path.stat().st_size > 0:
        raise ORXError(f"refusing to overwrite non-empty {config_path}")
    if profiles_path.exists() and profiles_path.stat().st_size > 0:
        raise ORXError(f"refusing to overwrite non-empty {profiles_path}")
    if _user_preset_installed():
        inherited = [".orx/config.toml", ".orx/profiles.toml"]
    else:
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
    return {"root": str(root), "created": created, "inherited": inherited}


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
                    # Evidence with row identity (evidence row id + producing
                    # attempt id): the planner facts quote these so a replan
                    # can cite prior results by their traceable identity, not
                    # by task number (G004 T004).
                    "evidence": [
                        {
                            "kind": row.kind,
                            "path": row.path,
                            "id": row.id,
                            "attempt_id": row.attempt_id,
                        }
                        for row in store.evidence_rows_for_task(rev.id, task.task_id)
                    ],
                    "verifications": [
                        {
                            "attempt_id": v.attempt_id,
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


# ---------------------------------------------------------------------------
# Replan precheck: the shared diff gate every activation path goes through
# (G004 T003). `orx plan check --file` runs it read-only; `submit_plan`
# (manual `orx plan submit` AND the CLI planner's auto-submit) runs it fresh
# and refuses to activate on any red row — there is no activation entry that
# skips it. Nothing here infers a correspondence from task numbers or
# inherits a prior passed status (docs/replan-contract.md §1-2).


def _snapshot_task_rows(snapshot: dict) -> list[dict]:
    """validate_replan rows from a replan snapshot: (revision, task_id,
    status) triples of every task this run has ever recorded."""
    return [
        {"revision": t["revision"], "task_id": t["task_id"], "status": t["status"]}
        for t in snapshot["tasks"]
    ]


def _evidence_owners(snapshot: dict) -> dict[str, set[tuple[int, str]]]:
    """Recorded evidence path -> the (revision, task_id) set that recorded
    it, for artifact binding checks. Identity is the pair, never a bare
    task number."""
    owners: dict[str, set[tuple[int, str]]] = {}
    for task in snapshot["tasks"]:
        key = (task["revision"], task["task_id"])
        for item in task["evidence"]:
            owners.setdefault(item["path"], set()).add(key)
    return owners


def _artifact_matches(artifact: str, path: str) -> bool:
    """Does artifact reference the recorded evidence at `path`? Exact match,
    or a project-relative artifact naming the tail of a recorded absolute
    path (manual completions record the resolved absolute evidence path)."""
    if artifact == path:
        return True
    return not artifact.startswith("/") and path.endswith("/" + artifact)


def _enrich_not_recorded(store: Store, run: Run, snapshot: dict, message: str) -> str | None:
    """Rewrite validate_replan's generic 'names a prior task that is not
    recorded' into the specific rejection reason: cross-run reference,
    future revision, or a task id that only exists in another revision of
    this run. Returns None when the message is not that error."""
    import re as _re

    match = _re.fullmatch(
        r"task (T\d+): source (\d+):(T\d+) names a prior task that is not recorded",
        message,
    )
    if match is None:
        return None
    task_id, revision, source_task = match.group(1), int(match.group(2)), match.group(3)
    runs_with = [
        row["run_id"]
        for row in store.conn.execute(
            "SELECT DISTINCT pr.run_id AS run_id FROM tasks t"
            " JOIN plan_revisions pr ON pr.id = t.revision_id"
            " WHERE pr.revision = ? AND t.task_id = ?",
            (revision, source_task),
        )
    ]
    foreign = sorted(run_id for run_id in runs_with if run_id != run.id)
    if foreign:
        return (
            f"task {task_id}: source {revision}:{source_task} belongs to run"
            f" {', '.join(foreign)}, not this run ({run.id}); sources must be"
            " this Run's own history — cross-run references are rejected"
        )
    recorded = sorted(rev["revision"] for rev in snapshot["revisions"])
    if revision not in recorded:
        kind = "a future revision" if recorded and revision > max(recorded) else "a nonexistent revision"
        return (
            f"task {task_id}: source {revision}:{source_task} names {kind} of"
            f" run {run.id} (revisions recorded: {recorded or 'none'});"
            " sources must name recorded history"
        )
    return (
        f"task {task_id}: source {revision}:{source_task} is not a task of"
        f" run {run.id}; task ids are per-revision — the same number in"
        " another revision is different work, and no correspondence is ever"
        " inferred from the number"
    )


def _replan_precheck(project: Project, ir, goal: Goal, run: Run) -> dict:
    """The shared precheck report over CURRENT recorded state. Read-only:
    no revision, task, Goal/Run, or assignment row is written.

    Composes the same validators a first plan faces (``validate_ir``) with
    the replan cross-check (``validate_replan``) against this run's task
    history, the prior-revision identity rule, and artifact binding. Every
    error is categorized ``{category, locus, message}``; the report body
    carries the old<->new correspondence, classifications, redo reasons,
    superseded dispositions, the contract diff vs the active plan, artifact
    reference statuses, and each source's recorded status at check time.
    """
    store = project.store
    old = store.revision_active(run.id)
    snapshot = replan_snapshot(store, goal, run)
    prior_rows = _snapshot_task_rows(snapshot)
    recorded = {(row["revision"], row["task_id"]): row["status"] for row in prior_rows}
    errors: list[dict] = []

    def err(category: str, locus: str, message: str) -> None:
        errors.append({"category": category, "locus": locus, "message": message})

    # 1. the structural validation every plan faces (goal match, acceptance
    #    coverage, task shape, plus the replan-internal rules when a mapping
    #    is declared).
    for message in plan_mod.validate_ir(
        ir, goal.id, goal.acceptance, _known_capabilities(project)
    ):
        category, locus = plan_mod.replan_error_category(message)
        err(category, locus, message)

    # 2. the replan cross-check against recorded history — mandatory
    #    whenever a prior revision exists OR the plan claims one.
    if old is not None or ir.replan is not None:
        for message in plan_mod.validate_replan(ir, prior_rows):
            enriched = _enrich_not_recorded(store, run, snapshot, message)
            final = enriched or message
            category, locus = plan_mod.replan_error_category(message)
            if enriched is not None:
                category, locus = "source", locus
            err(category, locus, final)

    replan = ir.replan
    if replan is not None and old is not None and replan.prior_revision != old.revision:
        err(
            "prior-revision",
            "plan",
            f"replan declares prior_revision {replan.prior_revision} but the"
            f" active revision is {old.revision}; the new plan replaces the"
            " active revision and must declare exactly its tasks superseded",
        )

    # 3. artifact binding: an artifact that names recorded evidence of this
    #    run must belong to a declared source of that task — references bind
    #    through the correspondence, never a task number. Unrecorded paths
    #    are semantic review (contract §7-8), reported, not rejected.
    owners = _evidence_owners(snapshot)
    artifact_rows: dict[str, list[dict]] = {}
    reference_issues: list[dict] = []
    correspondence: list[dict] = []
    renumbered: list[dict] = []
    redos: list[dict] = []
    buckets: dict[str, list[str]] = {"new": [], "confirm": [], "redo": [], "continue": []}
    source_states: dict[str, dict] = {}

    if replan is not None:
        for mapping in replan.tasks:
            source_keys = {(s.revision, s.task_id) for s in mapping.sources}
            rows: list[dict] = []
            for artifact in mapping.artifacts:
                matches = {
                    key
                    for path, keys in owners.items()
                    if _artifact_matches(artifact, path)
                    for key in keys
                }
                if not matches:
                    rows.append({
                        "artifact": artifact,
                        "status": "unresolved",
                        "detail": "not recorded evidence of this run; whether it"
                                  " points at the right prior result is semantic review",
                    })
                    continue
                if matches & source_keys:
                    rows.append({
                        "artifact": artifact,
                        "status": "source-evidence",
                        "detail": "recorded evidence of "
                                  + ", ".join(f"{r}:{t}" for r, t in sorted(matches & source_keys)),
                    })
                    continue
                bound = ", ".join(f"{r}:{t}" for r, t in sorted(matches))
                declared = ", ".join(f"{r}:{t}" for r, t in sorted(source_keys)) or "none"
                rows.append({
                    "artifact": artifact,
                    "status": "mis-bound",
                    "detail": f"recorded evidence of {bound}, not of the declared sources ({declared})",
                })
                problem = (
                    f"artifact {artifact!r} is recorded evidence of {bound},"
                    f" not of the declared sources ({declared}); artifacts"
                    " bind through the declared correspondence, never a task number"
                )
                err("artifact", mapping.task, f"task {mapping.task}: {problem}")
                reference_issues.append({
                    "task": mapping.task,
                    "artifact": artifact,
                    "problem": problem,
                })
            artifact_rows[mapping.task] = rows

            classification = mapping.classification
            if classification in buckets:
                buckets[classification].append(mapping.task)
            sources_out = []
            for source in mapping.sources:
                key = (source.revision, source.task_id)
                status = recorded.get(key)
                sources_out.append({
                    "source": f"{source.revision}:{source.task_id}",
                    "recorded_status": status,
                    "part": source.part,
                })
                source_states[f"{source.revision}:{source.task_id}"] = {
                    "source": f"{source.revision}:{source.task_id}",
                    "recorded_status": status,
                    "classified_by": sorted(
                        m.task for m in replan.tasks
                        for s in m.sources
                        if (s.revision, s.task_id) == key
                    ),
                }
                if source.task_id != mapping.task:
                    renumbered.append({
                        "from": f"{source.revision}:{source.task_id}",
                        "to": mapping.task,
                    })
            entry = {
                "task": mapping.task,
                "classification": classification,
                "sources": sources_out,
                "redo_reason": mapping.redo_reason or None,
                "confirm_verification": list(mapping.confirm_verification),
                "artifacts": rows,
            }
            correspondence.append(entry)
            if classification == "redo" and mapping.redo_reason.strip():
                redos.append({"task": mapping.task, "reason": mapping.redo_reason})

    superseded_out: list[dict] = []
    if replan is not None:
        for entry in replan.superseded:
            key = (entry.revision, entry.task_id)
            superseded_out.append({
                "prior": f"{entry.revision}:{entry.task_id}",
                "recorded_status": recorded.get(key),
                "disposition": entry.disposition,
                "successors": list(entry.successors),
                "note": entry.note or None,
            })

    # 4. the contract diff vs the active plan: what activation would change.
    would_cancel: list[str] = []
    terminal_preserved: dict[str, str] = {}
    if old is not None:
        for task in store.tasks_all(old.id):
            if TaskStatus(task.status) in UNFINISHED_TASK_STATUSES:
                would_cancel.append(task.task_id)
            else:
                terminal_preserved[task.task_id] = task.status
    acceptance_coverage = [
        {
            "criterion": criterion,
            "tasks": [t.id for t in ir.tasks if criterion in t.acceptance],
        }
        for criterion in goal.acceptance
        if criterion.strip()
    ]

    busy = [
        t.task_id
        for t in (store.tasks_all(old.id) if old is not None else [])
        if TaskStatus(t.status) in (TaskStatus.RUNNING, TaskStatus.VERIFYING)
    ]

    summary = {
        "errors": len(errors),
        "tasks": len(ir.tasks),
        "new": len(buckets["new"]),
        "confirm": len(buckets["confirm"]),
        "redo": len(buckets["redo"]),
        "continue": len(buckets["continue"]),
        "superseded": len(superseded_out),
        "renumbered": len(renumbered),
        "reference_issues": len(reference_issues),
    }
    return {
        "run_id": run.id,
        "goal_id": goal.id,
        "is_replan": old is not None,
        "prior_revision": old.revision if old is not None else None,
        "proposed_revision": (old.revision + 1) if old is not None else 1,
        "ok": not errors,
        "busy_tasks": sorted(busy),
        "summary": summary,
        "errors": errors,
        "classifications": buckets,
        "renumbered": renumbered,
        "redos": redos,
        "correspondence": correspondence,
        "superseded": superseded_out,
        "contract_diff": {
            "would_cancel": sorted(would_cancel),
            "terminal_preserved": dict(sorted(terminal_preserved.items())),
            "acceptance_coverage": acceptance_coverage,
        },
        "reference_issues": reference_issues,
        "sources": [source_states[key] for key in sorted(source_states)],
    }


def plan_check_file(project: Project, file_str: str) -> dict:
    """`orx plan check --file`: the read-only precheck of one plan document.

    Runs the shared gate and persists exactly one ``replan_reports`` row
    (revision unbound — the revision does not exist yet) when a prior
    revision exists; the row is the audit trail of what was checked and
    when. Beyond that report row this command writes nothing: no revision is
    created, no task is cancelled, and Goal/Run/assignment statuses are
    untouched. Whether a redo reason is justified or a confirm verification
    sufficient stays with semantic review (contract §7)."""
    path = Path(file_str).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    try:
        text = path.read_text()
    except OSError as exc:
        raise ORXError(f"cannot read plan file {file_str}: {exc}") from None
    try:
        ir_data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ORXError(f"cannot read plan file {file_str}: {exc}") from None

    goal, run = _active_context(project)
    report: dict
    try:
        ir = plan_mod.parse_ir(ir_data)
    except PlanSyntaxError as exc:
        report = {
            "run_id": run.id,
            "goal_id": goal.id,
            "is_replan": project.store.revision_active(run.id) is not None,
            "prior_revision": None,
            "proposed_revision": None,
            "ok": False,
            "busy_tasks": [],
            "summary": {"errors": len(exc.errors)},
            "errors": [
                {"category": "syntax", "locus": "plan", "message": message}
                for message in exc.errors
            ],
            "classifications": {},
            "renumbered": [],
            "redos": [],
            "correspondence": [],
            "superseded": [],
            "contract_diff": {},
            "reference_issues": [],
            "sources": [],
        }
        prior = project.store.revision_active(run.id)
        if prior is not None:
            report["prior_revision"] = prior.revision
            report["proposed_revision"] = prior.revision + 1
    else:
        report = _replan_precheck(project, ir, goal, run)

    old = project.store.revision_active(run.id)
    if old is not None:
        row = project.store.replan_report_add(run.id, old.revision, report)
        report["report_id"] = row.id
    return report


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
    # Same best-effort preflight as run_slice (G005): a quota-dead planner
    # rung is gated before the route, not discovered by paying for a failed
    # call.
    quota.refresh(project)
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
            # NOTE: a completed Run is NOT reopened here. Routing a replan
            # only creates the planning assignment; the Goal/Run keep their
            # recorded completion until a new revision actually lands in
            # submit_plan (a failed replan must not lose the completion).
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
    _record_attempt_health(store, profile.name, run_result)
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


def _record_attempt_health(store, profile_name: str, run_result) -> str | None:
    """The one health seam after every CLI launch (G005): classify the
    failure, learn the resource transition, and carry the quota reset the
    harness printed (codex names the exact moment) so an exhaustion can
    self-release once the reset passes. Returns the ErrorKind."""
    error_kind = None if run_result.ok else classify_failure(run_result)
    health.record_attempt_outcome(
        store, profile_name, ok=run_result.ok, error_kind=error_kind,
        quota_reset_at=(quota_reset_from(run_result)
                        if error_kind == "quota_exhausted" else None),
    )
    return error_kind


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
    attempt identity every later submission must quote. The nonce (v11) is
    the same token embedded at the top of the dispatch prompt."""
    if profile is None:
        return {
            "mode": "self",
            "agent_ref": None,
            "model": attempt.model,
            "effort": attempt.requested_effort,
            "harness": attempt.harness,
            "workdir": str(project.root),
            "attempt": attempt.id,
            "nonce": attempt.nonce,
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
        "nonce": attempt.nonce,
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
    """Activate a plan revision through the shared gate (G004 T003).

    Every activation path — manual `orx plan submit --file` and the CLI
    planner's auto-submit — lands here and runs the same fresh precheck
    (`_replan_precheck`) against CURRENT recorded state: a replan must
    declare the full correspondence, classifications, redo reasons, and
    superseded dispositions, sources must resolve to this run's recorded
    tasks, and artifacts recorded as evidence of this run must belong to a
    declared source. A red precheck raises ReplanCheckFailed with the
    categorized report and changes nothing: the previous revision stays
    active, its tasks keep their statuses, the planning assignment stays
    waiting, and a completed Run keeps its completion.

    Activation itself is one transaction: supersede the old revision and
    cancel its unfinished tasks, insert the new revision and tasks, persist
    the declared mapping, store and bind the precheck report, and close the
    planning assignment — a fault anywhere rolls the whole activation back
    (no partially effective revision can exist)."""
    store = project.store
    goal, run = _active_context(project)

    # The replan guard runs before validation: a rejected replan should say
    # why it was rejected, not surface unrelated plan errors first.
    _check_replan_allowed(store, run)
    # A malformed explicit session fails before the revision is written and
    # before the open planner attempt is closed.
    session_ref = resolve_session_ref(session)

    ir = plan_mod.parse_ir(ir_data)

    # The shared precheck gate — never skipped, never cached: source
    # statuses are re-read now, so a source that changed since an earlier
    # `orx plan check` is re-judged here (核对来源版本).
    precheck = _replan_precheck(project, ir, goal, run)
    if not precheck["ok"]:
        old = store.revision_active(run.id)
        if old is not None:
            # Audit the failed precheck (own transaction; nothing else moves).
            store.replan_report_add(run.id, old.revision, precheck)
        raise plan_mod.ReplanCheckFailed(precheck)

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
        report_row = None
        if ir.replan is not None and old is not None:
            report_row = store.replan_report_add(
                run.id, ir.replan.prior_revision, precheck
            )
        if old is not None:
            store.revision_mark_superseded(old.id)
            cancelled_tasks = [
                t for t in store.tasks_all(old.id)
                if TaskStatus(t.status) in UNFINISHED_TASK_STATUSES
            ]
            for task in cancelled_tasks:
                machine.transition(
                    store, old.id, task.task_id, "superseded", TaskStatus.CANCELLED,
                    reason=f"revision {old.revision} superseded",
                )
                cancelled_old.append(task.task_id)
            # G007 data integrity: the same activation also closes the
            # cancelled tasks' still-open attempts with a recorded
            # disposition. R006's replan left a parked, never-claimed attempt
            # as a zombie row (no result, no span), leaving the run's
            # history forever ambiguous; late heartbeats against it were
            # only rejected by the ownership gate, not by an honest final
            # state. started_at is never synthesized — unclaimed attempts
            # keep NULL and read "never claimed".
            store.attempts_close_for_tasks(
                old.id, cancelled_old,
                ended_at=db_now(), result="superseded",
                failure_reason=f"revision {old.revision} superseded",
            )

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
        if ir.replan is not None:
            # Anchor the declared correspondence to real task rows; inside
            # the activation transaction so a fault rolls it all back.
            store.replan_mapping_save(revision.id, ir.replan)
            if report_row is not None:
                store.replan_report_bind(report_row.id, revision.id)
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
        # The reopen of a completed Run happens only now that a new revision
        # has actually landed; a failed replan above never reaches this.
        store.goal_set_status(goal.id, GoalStatus.ACTIVE)
    refresh(store, goal, run)
    result = {
        "revision": revision.revision,
        "depth": revision.depth,
        "planner_profile": revision.planner_profile,
        "tasks": len(ir.tasks),
        "superseded_revision": old.revision if old else None,
        "cancelled_tasks": cancelled_old,
        "assignment": assignment.id if assignment else None,
    }
    if ir.replan is not None:
        result["replan_activation"] = {
            "prior_revision": ir.replan.prior_revision,
            "report": report_row.id if report_row is not None else None,
            "classifications": {
                key: len(ids) for key, ids in precheck["classifications"].items()
            },
        }
    return result


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
        "recovery": [],
        "failed": [],
        "deferred": [],
        "routing_errors": [],
    }
    if revision is None:
        out["note"] = "no active plan revision"
        return out

    # Best-effort live preflight (G005): providers reporting a reached limit
    # gate their profiles before the first route, an expired exhaustion is
    # released, and operator overrides always win. No signal -> no change.
    out["quota_preflight"] = quota.refresh(project)

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
            # Immediate rung fallback (G005): a quota/rate failure has already
            # marked the profile non-routable in health, so one re-route now
            # lands the next configured rung instead of parking the task as
            # FAILED until a manual `orx task retry`.
            if (outcome.get("status") == "failed"
                    and outcome.get("error_kind") in RESOURCE_FALLBACK_KINDS):
                outcome = (_reroute_resource_failure(
                    project, goal, run, revision, task.task_id, caps, outcome)
                    or outcome)
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
            # Re-computed static preflight of the same verification entries
            # the parked prompt file already carries; pure, no execution.
            "preflight": preflight_task_checks(task),
            "execution": (
                _execution_spec(project, project.profiles.get(attempt.profile), attempt)
                if attempt else None
            ),
            "resurfaced": True,
        }
        if status is TaskStatus.WAITING_HOST:
            # Same binding surface as a fresh park: a resumed Controller
            # re-pastes the archived prompt (which carries the nonce), so the
            # re-claim can discover the new subagent's session too.
            entry["claim"] = (
                f"orx task claim {task.task_id} --discover-session"
            )
            entry["isolation"] = "prompt_only"
            out["host_required"].append(entry)
        else:
            entry["finish_with"] = f"orx task complete {task.task_id} --evidence <file>"
            out["waiting_external"].append(entry)

    # Recovery surface (phase C): a RUNNING host task survived a session
    # break. ORX never starts a second writer for it — the attempt still
    # owns the task. This entry re-surfaces its identity and the exact
    # contract: check the original subagent first, bind late results with
    # --attempt, and only an explicit fail + retry routes a fresh attempt.
    for task in store.tasks_all(revision.id):
        if TaskStatus(task.status) is not TaskStatus.RUNNING:
            continue
        attempt = store.attempt_latest_for_task(revision.id, task.task_id)
        if attempt is None or attempt.driver != "host":
            continue  # CLI work finishes inside run_slice; only host work outlives it
        prompt_file = (
            project.root / ".orx" / "runs" / run.id / "assignments" / f"{task.task_id}.md"
        )
        out["recovery"].append({
            "task": task.task_id,
            "status": task.status,
            "attempt": attempt.id,
            "session_ref": attempt.session_ref,
            "prompt_file": (
                str(prompt_file.relative_to(project.root)) if prompt_file.exists() else None
            ),
            "execution": _execution_spec(
                project, project.profiles.get(attempt.profile), attempt
            ),
            # G006 §7/§10: the current window's latest report, its age, and
            # (when past the configured threshold) the check-the-original-
            # session hint ride with the re-surfaced identity. Observation
            # only — it never parks, fails, retries, or opens an attempt.
            "progress": progress_observation(project, attempt),
            "contract": (
                "no second writer: this attempt still owns the task. First check"
                " the original subagent (handle = session_ref) for a late result"
                f" and submit it with `orx task complete {task.task_id}"
                f" --attempt {attempt.id} --evidence <file>`; only if it is"
                f" confirmed dead: `orx task fail {task.task_id} --reason <why>`"
                f" then `orx task retry {task.task_id}` routes a fresh attempt"
            ),
        })

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
    # Mint the identity token before composing the prompt so the exact nonce
    # stored on the attempt row is the one embedded in the prompt (and its
    # archived assignment file) — one token, one attempt, no second minting.
    nonce = mint_nonce()
    prompt = worker_prompt(
        goal, task,
        prior_failure=_prior_failure_context(
            store, revision_row_id, task.task_id, project.root
        ),
        check_budget=project.config.worker_max_check_rounds,
        replan_context=_replan_reference_context(
            store, project.root, revision_row_id, task.task_id, audience="worker"
        ),
        identity=_identity_block(nonce),
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
        # Session identity is bound by the WORKER at claim/complete time
        # (`--session` / `--discover-session`), never stamped here: the
        # process parking the task is the Controller, and its env session
        # (ORX_SESSION_REF) is a different session than the subagent that
        # will execute. A NULL here is honest, not a gap.
        session_ref=None,
        run_id=run.id,
        nonce=nonce,
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
        "attempt": attempt.id,
        "prompt": prompt,
        "prompt_file": _write_task_assignment_file(project, run.id, task.task_id, prompt),
        # Structured static preflight (denylist + first-token PATH probe) for
        # the same rows the prompt text renders; nothing was executed.
        "preflight": preflight_task_checks(task),
        "preread": list(task.preread),
        "execution": _execution_spec(project, profile, attempt),
    }
    if kind == "host":
        entry["claim"] = (
            f"orx task claim {task.task_id} --discover-session"
        )
        entry["session_identity"] = (
            "claim binds the claiming subagent's zcode session id "
            "(deterministic first-prompt lookup); pass --session <id> "
            "instead when the caller knows its own id"
        )
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
    nonce = mint_nonce()
    prompt = worker_prompt(
        goal, task,
        prior_failure=_prior_failure_context(
            store, revision.id, task.task_id, project.root
        ),
        check_budget=project.config.worker_max_check_rounds,
        replan_context=_replan_reference_context(
            store, project.root, revision.id, task.task_id, audience="worker"
        ),
        identity=_identity_block(nonce),
    )
    # Same contract as the host/external park path: the preflight-bearing
    # prompt is archived as the assignment file even though ORX itself
    # launched the worker, so both launch paths leave the identical audit
    # artifact (prompt text with the static preflight baked in).
    prompt_file = _write_task_assignment_file(project, run.id, task.task_id, prompt)
    preflight = preflight_task_checks(task)
    launch = adapter.build_worker_launch(
        root=project.root,
        scratch=scratch,
        profile=profile,
        prompt=prompt,
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
        nonce=nonce,
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
        return {"task": task.task_id, "profile": profile.name, "status": "failed",
                "reason": reason, "prompt_file": prompt_file}

    run_result = runtime.run_launch(launch)
    log = _record_execution_log(project, run.id, f"{task.task_id}-{attempt.id}", run_result)
    effort = adapter.effort_outcome(launch, run_result)
    _record_usage(store, adapter, launch, run_result, attempt, profile.name,
                  run_id=run.id, task_id=task.task_id)
    error_kind = _record_attempt_health(store, profile.name, run_result)
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
                "reason": reason, "error_kind": error_kind,
                "log": log, "prompt_file": prompt_file}

    # Delivery gate, shared with `orx task complete`: every command entry
    # runs fresh through the same `task check` runner, bound to this
    # attempt — execution finished is not a green delivery. A red gate
    # follows the current FAILED flow (complete -> verify_fail) with the
    # full per-check detail in the task event; agent entries are not run
    # and stay with the independent verifier.
    gate = verify.run_task_check(
        store, project.root, run.id, revision.id,
        store.task_get(revision.id, task.task_id),
        timeout=project.config.command_timeout_sec,
        attempt_id=attempt.id,
    )
    # An accepted CLI delivery (green command gate) persists the replan
    # reference chain bound to this attempt — provenance rows plus the
    # delivery snapshot. Agent entries are still outstanding: nothing here
    # is a verdict and no verification result is inherited or substituted.
    replan_delivery = None
    if not verify.gate_failures(gate):
        replan_delivery = _record_replan_delivery(
            project, run, revision, store.task_get(revision.id, task.task_id),
            attempt, delivered_evidence=log,
        )
    machine.transition(store, revision.id, task.task_id, "complete", TaskStatus.VERIFYING,
                       reason="cli execution finished; verification starting")
    # The gate rows above are the latest row per command entry, so the
    # verdict judges exactly what the gate just ran.
    verdict = verify.apply_verdict(
        store, revision.id, store.task_get(revision.id, task.task_id),
        failure_hint=verify.gate_failure_reason(gate),
    )
    refresh(store, goal, run)
    result = {
        "task": task.task_id,
        "profile": profile.name,
        "status": store.task_get(revision.id, task.task_id).status,
        "verdict": verdict,
        "gate": gate,
        "log": log,
        "attempt": attempt.id,
        "prompt_file": prompt_file,
        "preflight": preflight,
        "actual_effort": effort.actual,
        "effort_source": effort.source,
    }
    if verdict == "failed":
        result["reason"] = store.task_get(revision.id, task.task_id).failure_reason
    if replan_delivery is not None:
        result["replan_delivery"] = replan_delivery
    return result


# Failure kinds that mean "this profile cannot serve requests right now, but
# the ladder's next rung can": quota exhaustion (until the reset passes) and
# rate limiting (cooldown). Everything else is task-shaped or config-shaped —
# a different profile would fail the same way, so the task stays FAILED.
RESOURCE_FALLBACK_KINDS = ("quota_exhausted", "rate_limited")


def _reroute_resource_failure(project, goal, run, revision, task_id, caps,
                              failed) -> dict | None:
    """One immediate hop down the worker ladder for a resource failure.

    The failed profile is already non-routable (health learned from the same
    launch), so a fresh route picks the next rung; when nothing else is
    routable the task stays FAILED exactly as before. The hop is bounded by
    construction — callers invoke it at most once per task per slice."""
    store = project.store
    task = store.task_get(revision.id, task_id)
    if task is None or TaskStatus(task.status) is not TaskStatus.FAILED:
        return None
    request = routing.RouteRequest(role=Role.WORKER, required_capabilities=caps)
    result = routing.route(store, project.config, project.profiles, request)
    if not result.ok or result.selected == failed.get("profile"):
        routing.persist_decision(store, request, result)
        return None
    machine.transition(
        store, revision.id, task_id, "retry", TaskStatus.RUNNABLE,
        reason=(f"resource fallback: profile '{failed.get('profile')}'"
                f" {failed.get('error_kind')}"),
    )
    outcome = _execute_cli_task(
        project, goal, run, revision, store.task_get(revision.id, task_id),
        result, request, result.profile,
    )
    outcome["fallback_from"] = failed.get("profile")
    return outcome


# How many retained failed checks a retry prompt spells out before folding
# the rest into a "+N more" line (same cap style as gate_failure_reason).
_RETRY_EVIDENCE_LIMIT = 5

# Repair guidance for the retrying worker, classified from the recorded
# delivery result. 'blocked' and 'failed' carry the delivery-result prefixes
# landed with the structured delivery result; the gate's red rows read as a
# code failure; anything else (operator `task fail`, unmarked reasons) falls
# back to the generic text — which is also where a scope/plan problem lands:
# such a failure is not fixable by redoing, and the worker must say so.
_RETRY_GUIDANCE = {
    "blocked": (
        "That failure was an environment/tool block, not a code defect: first"
        " make the checks runnable in your environment (install the missing"
        " tool, unblock the command) or report blocked explaining that routing"
        " needs to change (a different profile or harness); do not blindly"
        " redo the task."
    ),
    "code": (
        "That failure was a code/check failure: go directly to the failing"
        " check(s) listed above — open each log path, reproduce the failure,"
        " and fix that specific problem; do not redo the task blindly or"
        " re-explore unrelated work."
    ),
    "other": (
        "Fix that specific problem; do not redo the task blindly. If the"
        " failure means the assignment itself is wrong (scope too narrow,"
        " dependencies or plan incorrect), report blocked explaining that"
        " the assignment needs adjustment instead of forcing a redo."
    ),
}


def _classify_prior_failure(reason: str, attempt) -> str:
    """'blocked' | 'code' | 'other', from the failure markers ORX recorded:
    the attempt's independent delivery result, the delivery-result prefixes
    on the failure reason, and the delivery gate's red-row signature."""
    if attempt is not None and attempt.result == "blocked":
        return "blocked"
    if reason.startswith("delivery blocked"):
        return "blocked"
    if reason.startswith("delivery failed") or "delivery gate:" in reason:
        return "code"
    return "other"


def _retained_failure_rows(store: Store, revision_id: int, task_id: str):
    """(attempt, failed_rows): the failed command checks retained on the
    task's last worker attempt — the per-attempt history a retry prompt
    quotes. Prompt composition happens before the retry's new attempt is
    created, so the task's latest worker attempt IS the failed round's.

    Rows are the latest row per check entry inside that attempt (a
    same-session check -> fix -> check loop shows the fresh state, never a
    stale red row). Agent rows never qualify: the command gate does not
    judge them and the independent verifier is unchanged."""
    attempt = store.attempt_current_worker_for_task(revision_id, task_id)
    if attempt is None:
        return None, []
    latest: dict[str, Verification] = {}
    for v in store.verifications_for_attempt(attempt.id):
        if v.kind == "command":
            latest[v.command] = v  # id order: the last write wins
    return attempt, [v for v in latest.values() if not v.passed]


def _retained_check_detail(project_root: Path | None, row: Verification):
    """(error_summary, exit_note) for one retained verification row, parsed
    from its recorded log — the per-row fields a delivery-gate refusal
    carries. A missing or unreadable log degrades to None; nothing is
    invented."""
    text: str | None = None
    if project_root is not None and row.output_path:
        try:
            text = (project_root / row.output_path).read_text()
        except OSError:
            text = None
    if text is not None and text.startswith("denied by verification denylist:"):
        return text.splitlines()[0].strip(), "none (denied by the verification denylist)"
    timed_out = False
    stdout_part = text if text is not None else ""
    stderr_part = ""
    if text is not None:
        lines = text.split("\n")
        if lines and lines[0].startswith("$ "):
            timed_out = len(lines) > 1 and lines[1].startswith("[exit timeout")
            stdout_part = "\n".join(lines[2:])
        if "\n[stderr]\n" in stdout_part:
            stdout_part, stderr_part = stdout_part.split("\n[stderr]\n", 1)
    summary = verify.error_summary(stdout_part, stderr_part) if text is not None else None
    if row.exit_code is not None:
        exit_note = f"{row.exit_code}"
    elif timed_out:
        exit_note = "none (timed out)"
    elif text is None and row.output_path is None:
        exit_note = "none (not recorded)"
    else:
        exit_note = "none (not recorded; log unavailable)"
    return summary, exit_note


def _prior_failure_context(store: Store, revision_id: int, task_id: str,
                           project_root: Path | None = None) -> str:
    """The most recent recorded failure for a task, for retry prompts. The
    task row itself clears failure_reason on retry, so history is the
    source. Empty string for a first attempt.

    With retained verification history the block is enriched: the failure
    reason line plus every failed command check that survived on the failed
    attempt (command, exit code, error summary, log path — the same row
    shape a delivery-gate refusal reports), plus repair guidance classified
    from the recorded delivery result (environment/tool blocked vs code
    failure vs unmarked). Without retained rows the block is byte-compatible
    with the reason-line-only form that predates per-attempt history."""
    reason: str | None = None
    for event in reversed(store.task_events(revision_id, task_id)):
        if event.to_status == "failed" and (event.reason or ""):
            reason = event.reason
            break
    if reason is None:
        return ""
    attempt, failed_rows = _retained_failure_rows(store, revision_id, task_id)
    if not failed_rows:
        return (
            f"\nA previous attempt at this task FAILED with:\n"
            f"  {reason}\n"
            f"Fix that specific problem; do not redo the task blindly.\n"
        )
    lines = [
        "\nA previous attempt at this task FAILED with:",
        f"  {reason}",
        "Verification evidence retained from that failed attempt"
        f" (attempt {attempt.id}; latest row per check):",
    ]
    for row in failed_rows[:_RETRY_EVIDENCE_LIMIT]:
        summary, exit_note = _retained_check_detail(project_root, row)
        lines.append(f"  - command: {row.command}")
        lines.append(f"    exit code: {exit_note}")
        lines.append(
            "    error summary: "
            + (summary if summary else "(not retained; open the log)")
        )
        lines.append(
            "    log: " + (row.output_path if row.output_path else "(none recorded)")
        )
    hidden = len(failed_rows) - _RETRY_EVIDENCE_LIMIT
    if hidden > 0:
        lines.append(f"  (+{hidden} more failed check(s) not listed)")
    lines.append("")
    lines.append(_RETRY_GUIDANCE[_classify_prior_failure(reason, attempt)])
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Replan reference chain (G004 T004): resolve a replan task's declared
# artifact references through the RECORDED correspondence and show the chain
# — source revision + task, the source's own attempt/evidence row identity,
# and each referenced artifact's existence/change state — in the worker and
# verifier prompts. At an accepted delivery the chain is persisted bound to
# the completing attempt: one traceable provenance row per resolved
# (task <-> source) edge plus a delivery snapshot (existence + digest of every
# declared reference at delivery time) recorded as evidence on the attempt.
#
# Identity is always the (revision, task) pair plus attempt/evidence rows —
# never a bare task number. Nothing here writes verification rows, inherits a
# prior passed status, or turns a reference into a verdict: a referenced
# result is supporting material, and a missing or changed reference is never
# assumed valid by default (its traceable provenance is still shown).

DELIVERY_SNAPSHOT_KIND = "delivery_snapshot"


def _file_digest(path: Path) -> str | None:
    """sha256 of a file's bytes, or None when unreadable. Never invented."""
    import hashlib

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _artifact_fs_path(project_root: Path | None, artifact: str) -> Path | None:
    """Where an artifact reference lives on disk: absolute references are
    taken as-is (recorded evidence paths are absolute); project-relative
    references resolve under the project root. None when there is no root."""
    if project_root is None:
        return None
    candidate = Path(artifact).expanduser()
    return candidate if candidate.is_absolute() else project_root / candidate


def _artifact_source_evidence(
    store: Store, sources: list, artifact: str
) -> list[tuple[str, "object"]]:
    """Declared-source evidence rows an artifact reference names (full
    identity match, same rule as the precheck binding check): tuples of
    (source label "R:T", EvidenceRow). Resolution goes through the declared
    correspondence only — a same-numbered task in another revision never
    matches."""
    matches: list[tuple[str, object]] = []
    for source in sources:
        src_task = store.task_get_by_row_id(source.source_task_row_id)
        label = f"{source.source_revision}:{source.source_task_id}"
        for row in store.evidence_rows_for_task(src_task.revision_id, src_task.task_id):
            if _artifact_matches(artifact, row.path):
                matches.append((label, row))
    return matches


def _delivery_snapshot_baselines(
    store: Store, project_root: Path | None, revision_id: int, task_id: str,
    sources: list,
) -> dict[str, dict]:
    """artifact -> the newest recorded delivery snapshot entry for it, read
    from the evidence rows of THIS task and of its declared sources (the
    reference chain, oldest snapshot first so the latest delivery wins).
    An unreadable or corrupt snapshot provides no baseline; missing stays
    missing — nothing is guessed."""
    rows = list(store.evidence_rows_for_task(revision_id, task_id))
    for source in sources:
        src_task = store.task_get_by_row_id(source.source_task_row_id)
        rows.extend(
            store.evidence_rows_for_task(src_task.revision_id, src_task.task_id)
        )
    baselines: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: r.id):
        if row.kind != DELIVERY_SNAPSHOT_KIND:
            continue
        path = Path(row.path)
        if project_root is not None and not path.is_absolute():
            path = project_root / path
        try:
            doc = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        for entry in doc.get("artifacts", []):
            if not isinstance(entry, dict):
                continue
            artifact = entry.get("artifact")
            if not isinstance(artifact, str) or not artifact:
                continue
            baselines[artifact] = {
                "digest": entry.get("digest_at_delivery"),
                "existed": bool(entry.get("exists_at_delivery")),
                "revision": doc.get("revision"),
                "task_id": doc.get("task_id"),
                "attempt": doc.get("attempt"),
                "snapshot_row": row.id,
            }
    return baselines


def _artifact_state_line(
    project_root: Path | None, artifact: str, baseline: dict | None
) -> str:
    """Existence/change sentence for one artifact reference, judged against
    the delivery-snapshot baseline when one exists. Missing or changed is
    reported as such — never assumed valid."""
    fs = _artifact_fs_path(project_root, artifact)
    present = fs is not None and fs.is_file()
    if baseline is None:
        if present:
            return "file present (no delivery snapshot recorded yet — nothing to compare)"
        return "file MISSING on disk and no delivery snapshot recorded — the reference cannot be confirmed"
    where = (
        f"delivery snapshot (revision {baseline['revision']}, task"
        f" {baseline['task_id']}, attempt {baseline['attempt']}, evidence row"
        f" {baseline['snapshot_row']})"
    )
    if present:
        digest = _file_digest(fs)
        if baseline["existed"] and baseline["digest"] and digest == baseline["digest"]:
            return f"file present, UNCHANGED since the {where}"
        return (
            f"file present but CHANGED since the {where} — not assumed valid;"
            " re-verify what you rely on"
        )
    if baseline["existed"]:
        return f"file MISSING since the {where} — not assumed valid"
    return f"file still missing (absent at the {where} too)"


def _replan_reference_context(
    store: Store, project_root: Path | None, revision_id: int, task_id: str,
    *, audience: str,
) -> str:
    """The replan correspondence context block for one task's prompt: the
    declared classification, every source's full identity and recorded
    status, and each declared artifact reference resolved through the
    correspondence (provenance rows, source attempt/evidence identity,
    existence/change state). Empty string when the task has no declared
    sources — first plans and unmapped tasks get the unchanged prompt.

    The block is supporting material only: it never writes or substitutes a
    verification result, and both audiences get an explicit statement of
    that boundary."""
    mapping = store.replan_mapping_for(revision_id)
    if mapping is None:
        return ""
    entry = next((t for t in mapping.tasks if t.task_id == task_id), None)
    if entry is None or not entry.sources:
        return ""
    sources = store.replan_sources_for_task(revision_id, task_id)
    provenance = store.replan_artifact_sources_for_task(revision_id, task_id)
    prov_by_artifact: dict[str, list] = {}
    for row in provenance:
        prov_by_artifact.setdefault(row.artifact, []).append(row)
    baselines = (
        _delivery_snapshot_baselines(store, project_root, revision_id, task_id, sources)
        if entry.artifacts else {}
    )

    meaning = {
        "confirm": "prior passed work this task relies on AS-IS",
        "redo": "this work must be DONE AGAIN (implementation task)",
        "continue": "prior unfinished work this task carries forward",
    }.get(entry.classification, "declared correspondence with prior work")
    lines = [
        "Replan correspondence for this task (declared in this revision's"
        " replan mapping; a task number alone never implies correspondence —"
        " identity is the (revision, task) pair):",
        f"- classification: {entry.classification} — {meaning}.",
        "- sources (recorded history this task references):",
    ]
    for source in sources:
        src_task = store.task_get_by_row_id(source.source_task_row_id)
        lines.append(
            f"  - {source.source_revision}:{source.source_task_id}"
            f" (revision {source.source_revision}, task {source.source_task_id},"
            f" part={'true' if source.part else 'false'}) — recorded status:"
            f" {src_task.status}"
        )
        evidence_rows = store.evidence_rows_for_task(
            src_task.revision_id, src_task.task_id
        )
        if evidence_rows:
            for row in evidence_rows[-3:]:
                lines.append(
                    f"      evidence: [{row.kind}] {row.path}"
                    f" (evidence row {row.id}, attempt {row.attempt_id})"
                )
        else:
            lines.append("      evidence: (none recorded)")

    if entry.artifacts:
        lines.append(
            "- artifact references (resolved through the correspondence above,"
            " never by task number; existence and change state checked now):"
        )
        for artifact in entry.artifacts:
            matches = _artifact_source_evidence(store, sources, artifact)
            prov_rows = prov_by_artifact.get(artifact, [])
            if matches or prov_rows:
                identities = sorted(
                    {
                        f"{label} (source attempt {row.attempt_id},"
                        f" evidence row {row.id})"
                        for label, row in matches
                    }
                    | {
                        f"{r.source_revision}:{r.source_task_id}"
                        f" (source attempt {r.attempt_id}, evidence row {r.evidence_id})"
                        for r in prov_rows
                    }
                )
                provenance_note = (
                    "provenance recorded at delivery" if prov_rows
                    else "resolves to recorded source evidence"
                )
                lines.append(
                    f"  - {artifact} -> {provenance_note}: " + "; ".join(identities)
                )
            else:
                lines.append(
                    f"  - {artifact} -> names no recorded evidence of the declared"
                    " sources; whether it points at the right prior result is"
                    " semantic review"
                )
            lines.append(
                "      state: " + _artifact_state_line(
                    project_root, artifact, baselines.get(artifact)
                )
            )
        lines.append(
            "  A missing or changed reference is never assumed valid by"
            " default: verify what you rely on or report the problem — ORX"
            " never turns a reference into a pass."
        )

    if audience == "worker":
        if entry.classification == "confirm":
            lines.append(
                "Your job is ONLY to confirm this work still applies and to run"
                " the necessary regression checks below; do NOT redo the work."
                " A prior pass is supporting material, never this task's"
                " current verification — this task passes only through its own"
                " prescribed checks."
            )
            if entry.confirm_verification:
                lines.append(
                    "Current verification requirements for this confirmation"
                    " (they are this task's verification entries; a prior pass"
                    " never substitutes):"
                )
                lines.extend(f"  * {item!r}" for item in entry.confirm_verification)
        elif entry.classification == "redo":
            lines.append(
                "Redo reason (why this work must be done again, from the"
                f" replan declaration): {entry.redo_reason}"
            )
            allowed = ", ".join(
                store.task_get(revision_id, task_id).scope.get("allowed", [])
            )
            lines.append(f"Scope of the redo (the only paths this task may write): {allowed}")
        else:  # continue
            lines.append(
                "The sources' recorded statuses above are history, not results"
                " for this task: finish the work and pass this task's own"
                " prescribed checks."
            )
    else:  # verifier audience
        lines.append(
            "The correspondence, provenance, and source statuses above are"
            " recorded HISTORY this task references. They are NOT this task's"
            " verification results: judge only the check below against the"
            " current workspace — a historical pass never impersonates a"
            " current verification."
        )
    return "\n".join(lines) + "\n\n"


def _delivery_snapshot_path(
    project_root: Path, run_id: str, revision: int, task_id: str, attempt_id: int
) -> Path:
    """Delivery snapshots are addressed by revision AND attempt: a
    same-numbered task in another revision (or a retry's new attempt) writes
    its own file and never overwrites another delivery's snapshot."""
    directory = project_root / ".orx" / "runs" / run_id / "deliveries"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{task_id}-r{revision:02d}-a{attempt_id:03d}.json"


def _record_replan_delivery(
    project: Project, run: Run, revision: Revision, task: TaskRow,
    attempt, delivered_evidence: str | None,
) -> dict | None:
    """Persist the replan reference chain at an ACCEPTED delivery (G004
    T004), bound to the completing attempt:

    - one traceable provenance row (`replan_artifact_source_add`) for every
      (task <-> declared source) edge an artifact reference resolves to,
      carrying the source task's own attempt and evidence row identity;
    - a delivery snapshot: existence + sha256 of every declared artifact
      reference at delivery time, written under ``deliveries/`` addressed by
      revision and attempt (never overwritten by a same-numbered task in
      another revision) and recorded as evidence on the attempt so the
      database keeps finding it.

    Returns None (and writes nothing) when the task declared no artifacts.
    Writes no verification rows and inherits nothing: the chain supports
    later prompts and semantic review, never a verdict."""
    store = project.store
    mapping = store.replan_mapping_for(revision.id)
    if mapping is None:
        return None
    entry = next((t for t in mapping.tasks if t.task_id == task.task_id), None)
    if entry is None or not entry.artifacts:
        return None

    sources = store.replan_sources_for_task(revision.id, task.task_id)
    artifact_rows: list[dict] = []
    for artifact in entry.artifacts:
        row: dict = {"artifact": artifact, "resolved_sources": []}
        resolved = False
        for label, ev in _artifact_source_evidence(store, sources, artifact):
            resolved = True
            source = next(
                s for s in sources
                if f"{s.source_revision}:{s.source_task_id}" == label
            )
            store.replan_artifact_source_add(
                run.id, revision.id, task.task_id,
                source.source_revision, source.source_task_id, artifact,
                attempt_id=ev.attempt_id, evidence_id=ev.id,
            )
            row["resolved_sources"].append({
                "source": label,
                "attempt_id": ev.attempt_id,
                "evidence_id": ev.id,
            })
        row["resolved_to_recorded_evidence"] = resolved
        fs = _artifact_fs_path(project.root, artifact)
        present = fs is not None and fs.is_file()
        row["exists_at_delivery"] = present
        row["digest_at_delivery"] = _file_digest(fs) if present else None
        artifact_rows.append(row)

    snapshot = {
        "kind": "replan_delivery_snapshot",
        "run_id": run.id,
        "revision": revision.revision,
        "task_id": task.task_id,
        "classification": entry.classification,
        "attempt": attempt.id,
        "delivery_evidence": delivered_evidence,
        "recorded_at": db_now(),
        "artifacts": artifact_rows,
    }
    path = _delivery_snapshot_path(
        project.root, run.id, revision.revision, task.task_id, attempt.id
    )
    path.write_text(json.dumps(snapshot, indent=2) + "\n")
    rel = str(path.relative_to(project.root))
    store.evidence_add(attempt.id, DELIVERY_SNAPSHOT_KIND, rel)
    return {
        "snapshot": rel,
        "artifact_sources": sum(len(r["resolved_sources"]) for r in artifact_rows),
    }


# ---------------------------------------------------------------------------
# Assignment-time static preflight (R002 follow-up).
#
# The delivery contract in worker_prompt only helps if the worker can
# actually run its prescribed checks. At assignment assembly ORX therefore
# preflights every command verification entry statically: the shared
# denylist (runtime.forbidden_command — the same one verification applies at
# completion) plus a PATH probe of the command's first token. The result is
# written into the worker prompt and the assignment file on BOTH launch
# paths (CLI execution and host/external park). This targets one measured
# R002 failure class only: workers that could not run their checks yet
# implemented, finished, and reported success (T003/T005 logs). It executes
# nothing, touches no state, judges nothing, and never covers agent entries:
# those stay with the independent verifier.


def preflight_task_checks(task: TaskRow) -> list[dict]:
    """Static preflight rows for every verification entry of a task.

    Command rows carry the denylist verdict, the first command token, its
    PATH status, and a ``blocked`` flag (true only for a denied command or a
    token missing from PATH). Agent rows are reported as not preflighted —
    the command gate never judges them.
    """
    rows: list[dict] = []
    for item in verify.entries_for(task):
        if item.kind != "command":
            rows.append({
                "kind": "agent",
                "raw": item.raw,
                "command": None,
                "denial": None,
                "token": None,
                "token_status": None,
                "token_path": None,
                "blocked": False,
                "reason": (
                    "agent entries are judged by an independent verifier; "
                    "not part of the command gate"
                ),
            })
            continue
        probe = runtime.static_command_probe(item.spec)
        blocked = probe["blocked"]
        if probe["denial"] is not None:
            reason = f"denylist DENIED ({probe['denial']})"
        elif probe["token_status"] == "missing":
            reason = f"denylist allowed; first token {probe['token']!r} not on PATH"
        elif probe["token_status"] == "builtin":
            reason = f"denylist allowed; first token {probe['token']!r} is a shell builtin"
        elif probe["token_status"] == "found":
            reason = (
                f"denylist allowed; first token {probe['token']!r} found on PATH"
                f" ({probe['token_path']})"
            )
        else:
            reason = "denylist allowed; first token not statically determinable"
        rows.append({
            "kind": "command",
            "raw": item.raw,
            "command": item.spec,
            "denial": probe["denial"],
            "token": probe["token"],
            "token_status": probe["token_status"],
            "token_path": probe["token_path"],
            "blocked": blocked,
            "reason": reason,
        })
    return rows


def _preflight_block(task: TaskRow) -> str:
    """The preflight section of the worker prompt: one tagged line per
    verification entry plus a summary. Tags are stable for tooling:
    [preflight:ok], [preflight:blocked], [preflight:not-run]."""
    rows = preflight_task_checks(task)
    lines: list[str] = []
    command_total = 0
    blocked = 0
    agent_total = 0
    for row in rows:
        if row["kind"] == "agent":
            agent_total += 1
            lines.append(f"  - [preflight:not-run] agent {row['raw']!r} -> {row['reason']}")
            continue
        command_total += 1
        if row["blocked"]:
            blocked += 1
            if row["denial"] is not None:
                # The completion-time gate applies the same denylist, so a
                # denied entry is definitive: it can never pass.
                lines.append(
                    f"  - [preflight:blocked] command {row['raw']!r} -> {row['reason']};"
                    " the verification denylist denies this check too:"
                    " report blocked, do not implement"
                )
            else:
                lines.append(
                    f"  - [preflight:blocked] command {row['raw']!r} -> {row['reason']};"
                    " ORX cannot confirm this check runs in your environment:"
                    " verify it via the START GATE before implementing, and"
                    " report blocked immediately if it cannot run"
                )
        else:
            lines.append(f"  - [preflight:ok] command {row['raw']!r} -> {row['reason']}")
    if not lines:
        lines = ["  (none)"]
    summary = (
        f"Pre-flight summary: {command_total} command check(s): "
        f"{command_total - blocked} ok, {blocked} blocked; "
        f"{agent_total} agent check(s) not preflighted."
    )
    return (
        "Pre-flight of the prescribed checks (static; assembled by ORX at\n"
        "assignment time, nothing was executed. The START GATE below\n"
        "re-verifies it for real in your environment):\n"
        + "\n".join(lines) + "\n" + summary + "\n\n"
    )


def _identity_block(nonce: str | None) -> str:
    """The machine-readable assignment anchor riding at the very top of every
    host dispatch prompt (G007 identity contract, schema v11).

    R006 evidence: "attempt N" markers were a controller-side convention —
    5 of 7 worker prompts lost them entirely and one carried a closed
    attempt's number from stale evidence lineage. A nonce ORX itself minted
    and embedded cannot be paraphrased away by accident; when the Controller
    passes the prompt through unchanged (the skill's standing rule), the
    token lands in the subagent session's first text part and session
    discovery matches it exactly. Legacy attempts (nonce NULL) pass an empty
    block and keep the marker-fallback discovery."""
    if not nonce:
        return ""
    return (
        "ASSIGNMENT IDENTITY (machine anchor — do not remove or alter"
        " this line):\n"
        f"ORX_ASSIGNMENT={nonce}\n\n"
    )


def worker_prompt(goal: Goal, task: TaskRow, prior_failure: str = "",
                  check_budget: int = 3, replan_context: str = "",
                  identity: str = "") -> str:
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
    # Replan tasks get the reference chain: classification, sources with
    # full identity and recorded status, artifact provenance and
    # existence/change state, and the classification-specific instructions
    # (confirm -> applicability + regression only; redo -> reason + scope).
    # Empty for first-plan tasks: the prompt is byte-identical to before.
    replan_block = replan_context if replan_context else ""
    preread = "\n".join(f"  - {path}" for path in task.preread) or "  (not specified)"
    return f"""{identity}You are an ORX worker for Goal {goal.id}: {goal.objective}

Your assignment is ONE task. Do only this task and stay inside its scope.

Task {task.task_id}: {task.objective}

Goal constraints (binding for this work):
{constraints}

{context_block}{replan_block}Scope (write only inside these project-relative paths): {allowed}
Read these files first — before any exploration, and only these plus the
files you yourself create or modify (project-relative):
{preread}
Acceptance (your work is verified against these, verbatim):
{acceptance}
{prior_failure}
How your work will be checked (the prescribed checks):
{verification}

{_preflight_block(task)}Delivery contract (binding, in order):
1. START GATE: before any implementation work, prove the checks can run in
   your environment. Run `orx task check {task.task_id}` (or trial-run each
   prescribed check command yourself) and confirm the tools exist, the
   commands are permitted, and the test baseline executes. Do not write any
   task code before this gate is green or you know exactly why it cannot be.
2. BLOCKED EXIT: if the start gate shows the environment cannot run the
   checks (tool missing, command rejected by the sandbox, baseline not
   executable), stop immediately: leave the workspace unchanged, exit
   non-zero, and report `blocked: <why the checks cannot run>` in your
   output. Do NOT invest in implementation first — discovering this only
   after finishing the work is exactly the failure this contract prevents.
3. CHECK-FIX LOOP (budget: {check_budget} check round(s) on this attempt):
   when a check is red, repair it in THIS session — check -> fix -> check
   again — while budget remains. `orx task check {task.task_id}` reports
   check_rounds (rounds used vs budget) for your attempt after every run.
   Red checks that are EXPECTED at this stage of development are part of
   the work, not failures (TDD: a test written before its implementation
   is deliberately red): keep implementing in the same session — do not
   restart the task or request a new attempt merely because a check is
   temporarily red. Run the full check set at stage boundaries only — when
   a new behavior is complete, when a module modification is complete, and
   when you are ready to deliver — not after every edit. When the budget is
   exhausted and checks are still red, stop iterating: submit the
   structured failed/blocked delivery result (`orx task complete
   {task.task_id} --evidence <file>`) or `orx task fail {task.task_id}
   --reason <why>` so the Controller decides the next round. Do not keep
   working past the budget.
4. DELIVERY GATE: every prescribed command check above must pass (exit 0)
   before you may exit 0 or submit `orx task complete {task.task_id}
   --evidence <file>`. A green self-check is necessary, not sufficient: it
   never replaces the independent verifier — agent checks and independent
   acceptance still judge the work afterwards.

Rules:
- do not modify the Goal text or other tasks' scope
- if you are blocked, leave the workspace unchanged and exit non-zero with the
  blocker in your output; do not widen scope
- exit 0 only when the task is done AND the delivery gate is green
"""


def verifier_prompt(goal: Goal, task: TaskRow, item, gate_summary: str = "",
                    evidence_lines: str = "", prior_issues: str = "",
                    replan_context: str = "", identity: str = "") -> str:
    capabilities = ", ".join(item.capabilities) or "none"
    acceptance = "\n".join(f"  - {item!r}" for item in task.acceptance) or "  (none listed)"
    constraints = "\n".join(f"  - {c}" for c in goal.constraints) or "  (none)"
    context_blocks = ""
    if replan_context:
        # The reference chain this task's replan mapping declares, clearly
        # labeled HISTORY: the verifier sees the provenance but cannot read
        # it as this task's verification result.
        context_blocks += (
            "\nReplan correspondence this task references (recorded history"
            f" — see the boundary note at its end):\n{replan_context}"
        )
    if gate_summary:
        context_blocks += (
            "\nDeterministic checks already run for this task (do not re-run them):\n"
            f"{gate_summary}\n"
        )
    if evidence_lines:
        context_blocks += f"\nExecution evidence for this task:\n{evidence_lines}\n"
    if prior_issues:
        context_blocks += prior_issues
    return f"""{identity}You are an ORX verifier for Goal {goal.id}: {goal.objective}

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
    """Current command-check results for a task, for verifier context: the
    latest row per entry from the current attempt window only. A
    same-session check -> fix -> check loop shows the fresh row; rows from
    earlier attempts are history and are not handed to the verifier as if
    they were this round's result."""
    rows = [
        v for v in store.verifications_current(revision_id, task.task_id)
        if v.kind == "command"
    ]
    latest: dict[str, Verification] = {}
    for v in rows:
        latest[v.command] = v  # id order: the last write wins
    lines = []
    for v in latest.values():
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
    Verification rows survive retries as per-attempt history; task_events
    remains the cross-attempt failure narrative, so a re-dispatched verifier
    still sees what earlier rounds rejected."""
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
                # Check-budget observability without running anything:
                # rounds the task's latest attempt has already consumed vs
                # the configured budget (see attempt_check_rounds).
                "check_rounds": (
                    {
                        "used": attempt_check_rounds(
                            project.store, attempt.id, task
                        ),
                        "budget": project.config.worker_max_check_rounds,
                    }
                    if attempt
                    else None
                ),
                # G006 §7: current-window progress observation. Read-only;
                # `unknown` until this attempt reports, never a fallback to
                # an older attempt's history.
                "progress": progress_observation(project, attempt),
            }
        )
    return rows


def task_claim(project: Project, task_id: str, session: str | None = None,
               discover_session: bool = False) -> dict:
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
    discovery: dict | None = None
    if (discover_session and attempt is not None
            and attempt.session_ref is None):
        # A subagent cannot read its own zcode session id from the
        # environment, but its identity is deterministically discoverable.
        # v11 attempts match the one-time nonce ORX embedded at the top of
        # the dispatch prompt (immune to controller-side paraphrasing or
        # stale "attempt N" lineage); legacy attempts fall back to the
        # marker text. Directory equals the project root and a subagent has
        # a parent. Unique -> store (this is now ORX-written identity, not
        # a guess). None/ambiguous -> stays NULL and the answer says so.
        from . import zcode_sessions
        try:
            discovery = zcode_sessions.discover_attempt_session(
                attempt.id, project.root,
                created_within_ms=24 * 3600 * 1000,
                nonce=attempt.nonce,
            )
            sid = zcode_sessions.first_session_id(discovery)
            if sid is not None:
                store.attempt_update(attempt.id, session_ref=sid)
                attempt = store.attempt_get(attempt.id)
        except zcode_sessions.ZcodeDbUnavailable as e:
            discovery = {"decision": "unavailable", "error": str(e)}
    refresh_run(store, goal, run)
    return {
        "task": task.task_id,
        "status": "running",
        "attempt": attempt.id if attempt else None,
        "driver": "host",
        "session_ref": attempt.session_ref if attempt else None,
        "session_discovery": discovery,
        "execution": (
            _execution_spec(project, project.profiles.get(attempt.profile), attempt)
            if attempt else None
        ),
    }


def task_complete(
    project: Project, task_id: str, evidence: str,
    attempt_id: int | None = None, actual_model: str | None = None,
    session: str | None = None,
) -> dict:
    """Record a structured delivery and enter verification. Completion is
    NOT success: the task passes only when every verification entry passes.

    The delivery contract (R002 follow-up): the evidence file must be the
    structured delivery result (status passed|failed|blocked, checks[],
    artifacts[], summary — verified.load_delivery_evidence validates and
    names every missing field). status=passed additionally passes the
    delivery gate: every command entry is re-run fresh through the
    `task check` runner for the completing attempt — a prior self-check
    never exempts the delivery — and any red row rejects the completion
    with the structured per-check detail while the task keeps its prior
    status and the attempt stays open (fix and complete again). status=
    failed/blocked records a non-delivery: the task fails with a reason
    that distinguishes a worker-reported code failure from an
    environment/tool block. Agent entries are never run here and never
    gate the completion; the independent verifier is unchanged.

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
    # Late binding: a recovered/late worker may name its session now; the
    # value is validated before any delivery write happens.
    session_ref = resolve_session_ref(session)

    evidence_path = Path(evidence).expanduser()
    if not evidence_path.is_absolute():
        evidence_path = (Path.cwd() / evidence_path).resolve()
    if not evidence_path.exists():
        raise NotFoundError(f"evidence file not found: {evidence}")

    # The structured delivery result is validated before anything is
    # written: a non-structured or malformed document names its missing
    # fields and changes no state.
    delivery, evidence_errors = verify.load_delivery_evidence(evidence_path)
    if evidence_errors:
        raise verify.DeliveryRejected(
            f"evidence rejected for task {task_id}: the delivery result must"
            " be structured JSON (status, checks, artifacts, summary) — "
            + "; ".join(evidence_errors),
            kind="evidence",
            fields=evidence_errors,
        )

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
                # The completing caller IS this attempt's executor: an
                # explicit --session wins, the caller's env is the fallback.
                session_ref=session_ref if session_ref is not None
                else _host_session_ref(),
                run_id=run.id,
            )
    if session_ref is not None and attempt.session_ref is None:
        store.attempt_update(attempt.id, session_ref=session_ref)
        attempt = store.attempt_get(attempt.id)
    mismatch = _record_reported_model(store, attempt.id, attempt.model, actual_model)

    if delivery["status"] in ("failed", "blocked"):
        return _record_failed_delivery(
            project, store, goal, run, revision, task, attempt,
            delivery, evidence_path, mismatch,
        )

    # status == "passed": the delivery gate. Force every command entry to
    # run fresh through the same runner `orx task check` uses, bound to the
    # completing attempt — a green self-check earlier in the session is not
    # an exemption. Agent entries are not executed and never gate the
    # completion (the independent verifier still judges them).
    gate = verify.run_task_check(
        store, project.root, run.id, revision.id, task,
        timeout=project.config.command_timeout_sec,
        attempt_id=attempt.id,
    )
    failures = verify.gate_failures(gate)
    if failures:
        # Rejected: nothing below this line runs. No completion evidence is
        # attached, the attempt stays open, and the task keeps the status it
        # had (running / waiting_external), so the worker can fix the red
        # checks and complete again on the same attempt.
        raise verify.DeliveryRejected(
            f"delivery gate rejected completion for task {task_id}: "
            f"{len(failures)} of {gate['summary']['total']} command check(s)"
            f" not green; task stays {task.status} and attempt {attempt.id}"
            " stays open — fix the failures and complete again",
            kind="gate",
            report={
                "task": task.task_id,
                "task_status": task.status,
                "attempt": attempt.id,
                "summary": gate["summary"],
                "failures": failures,
                "agent_entries_not_run": gate["agent_entries_not_run"],
                # The gate run just consumed a round; show the worker where
                # that leaves the attempt's check budget so an exhausted
                # budget routes to a structured exit instead of another loop.
                "check_rounds": _check_rounds_payload(
                    store, attempt.id, task,
                    project.config.worker_max_check_rounds,
                ),
            },
        )

    store.evidence_add(attempt.id, "completion", str(evidence_path))
    # The accepted delivery persists the replan reference chain bound to the
    # completing attempt: provenance rows for every resolved (task <->
    # source) edge plus the delivery snapshot (existence + digest of each
    # declared reference at delivery time). Supporting material only — it
    # writes no verification rows and inherits no prior pass.
    replan_delivery = _record_replan_delivery(
        project, run, revision, task, attempt, delivered_evidence=str(evidence_path),
    )
    store.attempt_update(attempt.id, ended_at=db_now(), result="completed")
    machine.transition(
        store, revision.id, task.task_id, "complete", TaskStatus.VERIFYING,
        reason="completion claimed; verification starting",
    )

    # The gate above already recorded a fresh row for every command entry,
    # so this only covers entries with no row at all (there are none today);
    # apply_verdict then judges each entry by its latest row — the gate's.
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
        "delivery": {"status": "passed", "checks_run": gate["summary"]["total"]},
    }
    if replan_delivery is not None:
        result["replan_delivery"] = replan_delivery
    if mismatch is not None:
        result["model_mismatch"] = mismatch
    return result


def _record_failed_delivery(project: Project, store: Store, goal: Goal, run: Run,
                            revision: Revision, task: TaskRow, attempt,
                            delivery: dict, evidence_path: Path,
                            mismatch: dict | None) -> dict:
    """Record a worker-reported failed or blocked delivery and fail the task.

    There is no success claim to gate, so the command checks are not re-run:
    the worker's reported red/not-run checks are quoted in the failure
    reason instead. blocked is an independent delivery result — the attempt
    closes with result 'blocked' — and the recorded failure reason carries
    the kind, so retries and analytics can tell an environment/tool block
    apart from a code failure."""
    status = delivery["status"]
    kind = "environment/tool blocked" if status == "blocked" else "worker-reported failure"
    reason = f"delivery {status} ({kind}): {delivery['summary'].strip()}"
    detail = verify.reported_check_detail(delivery)
    if detail:
        reason += f"; {detail}"
    store.evidence_add(attempt.id, "completion", str(evidence_path))
    store.attempt_update(
        attempt.id, ended_at=db_now(), result=status, failure_reason=reason,
    )
    machine.transition(
        store, revision.id, task.task_id, "fail", TaskStatus.FAILED,
        reason=reason, failure_reason=reason,
    )
    refresh(store, goal, run)
    result = {
        "task": task.task_id,
        "status": store.task_get(revision.id, task.task_id).status,
        "delivery": {"status": status, "reason": reason},
        "attempt": attempt.id,
        "verification": _verification_counts(store, revision),
    }
    if mismatch is not None:
        result["model_mismatch"] = mismatch
    return result


def task_fail(project: Project, task_id: str, reason: str,
              session: str | None = None) -> dict:
    store = project.store
    goal, run = _active_context(project)
    revision, task = _active_task(project, run, task_id)
    if TaskStatus(task.status) not in (TaskStatus.RUNNING, TaskStatus.WAITING_EXTERNAL):
        raise ConflictError(
            f"task {task_id} is {task.status}; `task fail` requires running or waiting_external"
        )
    session_ref = resolve_session_ref(session)
    attempt = store.attempt_latest_for_task(revision.id, task.task_id)
    if attempt is not None:
        if session_ref is not None and attempt.session_ref is None:
            store.attempt_update(attempt.id, session_ref=session_ref)
            attempt = store.attempt_get(attempt.id)
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
    """Retry a failed task on the same plan: failed -> runnable. Not a replan.

    Verification history is retained: rows from earlier attempts stay in the
    table and stay queryable per attempt. The retry routes a fresh worker
    attempt, which opens a new current window — verdicts and 'current
    result' views read that window, never the superseded rows."""
    store = project.store
    goal, run = _active_context(project)
    revision, task = _active_task(project, run, task_id)
    if TaskStatus(task.status) is not TaskStatus.FAILED:
        raise ConflictError(f"task {task_id} is {task.status}; `task retry` requires failed")
    machine.transition(
        store, revision.id, task.task_id, "retry", TaskStatus.RUNNABLE,
        reason="operator requested retry; new attempt will be routed by `orx run`",
    )
    refresh_run(store, goal, run)
    return {"task": task.task_id, "status": "runnable"}


# ---------------------------------------------------------------------------
# Same-session check-fix loop budget (R002 follow-up).
#
# `worker.max_check_rounds` bounds how many check rounds one worker attempt
# may consume: check -> fix -> check again in the same session, then exit
# structured failed/blocked when the budget is exhausted and checks are still
# red. The budget is an advisory contract — ORX never kills a session — so
# the rounds are OBSERVED, not enforced: every check round (`orx task check`,
# or the delivery gate at `task complete`) appends exactly one command
# verification row per command entry, bound to the attempt. The count is
# derived from those attempt-bound rows (rows/entries, ceiling for an
# interrupted partial round): additive, no schema change, and rows recorded
# without an attempt (attempt_id NULL) never count against a worker's budget.


def _command_entry_count(task: TaskRow) -> int:
    return sum(1 for item in verify.entries_for(task) if item.kind == "command")


def attempt_check_rounds(store: Store, attempt_id: int | None, task: TaskRow) -> int:
    """Check rounds accumulated on one attempt (see the block above)."""
    entries = _command_entry_count(task)
    if attempt_id is None or entries <= 0:
        return 0
    rows = sum(
        1 for v in store.verifications_for_attempt(attempt_id) if v.kind == "command"
    )
    return (rows + entries - 1) // entries


def _check_rounds_payload(
    store: Store, attempt_id: int | None, task: TaskRow, budget: int
) -> dict:
    """The observability surface for the check budget: rounds used vs
    configured budget on the bound attempt. `exceeded` is the honest signal
    that the worker went past the budget (ORX does not prevent it);
    `remaining == 0` is the worker's cue to stop iterating and exit
    structured failed/blocked if checks are still red."""
    used = attempt_check_rounds(store, attempt_id, task)
    return {
        "attempt": attempt_id,
        "used": used,
        "budget": budget,
        "remaining": max(budget - used, 0),
        "exceeded": used > budget,
    }


def task_check(project: Project, task_id: str) -> dict:
    """Worker self-check: run the task's command verification entries NOW.

    Executes every command entry immediately (same denylist and timeout as
    verification) and appends one verification row per entry, bound to the
    task's current worker attempt — the latest worker attempt for the task,
    which is the attempt a claimed task runs under. Rounds accumulate on
    that attempt across calls: the report's check_rounds field carries
    rounds used vs the configured worker.max_check_rounds budget (this run
    included), so a worker in a check -> fix -> check loop can see how much
    budget remains. This never transitions the task's status and never
    closes the attempt: a green self-check is not a verdict, agent entries
    are not run, and `task complete` still verifies independently. Works on
    a task in any status so a Controller can also re-check finished work;
    without a worker attempt the rows are recorded unbound (attempt_id
    NULL), never attributed to a verifier, and count against no budget.
    """
    store = project.store
    goal, run = _active_context(project)
    revision, task = _active_task(project, run, task_id)
    attempt = store.attempt_latest_for_task(revision.id, task.task_id)
    attempt_id = (
        attempt.id
        if attempt is not None and attempt.role == Role.WORKER.value
        else None
    )
    report = verify.run_task_check(
        store, project.root, run.id, revision.id, task,
        timeout=project.config.command_timeout_sec,
        attempt_id=attempt_id,
    )
    # This round is included: the payload is computed after the rows were
    # appended, so `used` counts the check that just ran.
    report["check_rounds"] = _check_rounds_payload(
        store, attempt_id, task, project.config.worker_max_check_rounds
    )
    if report["summary"]["total"] == 0:
        report["note"] = (
            f"task {task_id} has no executable command verification entries; "
            "nothing was run"
        )
    # Status is reported, not changed: this is the same status read before
    # and after the check, proving the self-check left the machine alone.
    report["status"] = store.task_get(revision.id, task.task_id).status
    report["attempt"] = attempt_id
    return report


# ---------------------------------------------------------------------------
# Host worker progress reports (G006, docs/host-progress-contract.md §2-§5).
#
# heartbeat is an explicit, side-effect-free observation channel: the caller
# names its attempt (--attempt is the only identity entry; ORX never guesses
# and never reads ORX_SESSION_REF here — that variable is the Controller's
# session, not the worker's), ORX stamps received_at from its own UTC clock,
# and the row is appended to the attempt's history. A report never changes
# task status, verification rows, attempt fields (session_ref included),
# check rounds, or usage. Rejections carry a machine reason; input
# validation (phase/message bounds, exit 2 at the CLI) is checked before any
# state is read, so a misuse is told apart from an identity/state refusal
# (exit 1) even when both would apply.

#: Input-validation rejection reasons (contract §3): usage-class errors,
#: exit 2 at the CLI. Everything else below is an identity/state refusal.
HEARTBEAT_INPUT_REASONS = ("phase_invalid", "message_invalid")


def _heartbeat_input_error(reason: str, message: str) -> ORXError:
    """A bounds violation (contract §3). Existing exception family; the
    machine reason rides on the instance, no new exception type."""
    exc = ORXError(message)
    exc.reason = reason
    return exc


def _heartbeat_state_error(exc_class, reason: str, message: str):
    """An identity/ownership refusal (contract §4-§5). Same deal: an existing
    exception type carrying the frozen `reason` for the --json envelope."""
    exc = exc_class(message)
    exc.reason = reason
    return exc


def _heartbeat_clean_inputs(phase: str, message: str | None) -> tuple[str, str | None]:
    """Contract §3: phase and message are opaque text — ORX bounds them, it
    does not interpret them. Strip first, then measure Unicode characters:
    phase 1-64 (all-whitespace is invalid), message 0-512 with
    all-whitespace counting as omitted (stored NULL)."""
    phase_clean = (phase or "").strip()
    if not phase_clean or len(phase_clean) > 64:
        raise _heartbeat_input_error(
            "phase_invalid",
            "--phase must be 1-64 characters after stripping whitespace"
            f" (got {len(phase_clean)}); phase is free text, ORX only bounds it",
        )
    message_clean = None
    if message is not None:
        stripped = message.strip()
        if stripped:
            if len(stripped) > 512:
                raise _heartbeat_input_error(
                    "message_invalid",
                    "--message must be at most 512 characters after stripping"
                    f" whitespace (got {len(stripped)}); message is free text,"
                    " ORX only bounds it",
                )
            message_clean = stripped
    return phase_clean, message_clean


def task_heartbeat(project: Project, task_id: str, attempt_id: int,
                   phase: str, message: str | None = None) -> dict:
    """Append one host worker progress report (contract §2, §4-§5).

    Input bounds are validated first (usage-class error, nothing read). The
    ownership gates and the append then run inside ONE transaction
    (BEGIN IMMEDIATE): the task must be running in the active revision, and
    the attempt must exist, be a host-worker attempt of exactly this
    revision/task, be the task's latest attempt, and be unclosed — all
    re-checked at write time, so a concurrent `task complete` either commits
    first (heartbeat then sees the closed attempt and rejects, never
    reviving it) or lands after the report (the report becomes history and
    the completion closes normally). Any refusal rolls back having written
    nothing: no progress row, no task event, no attempt change, no
    verification or usage row. received_at comes from ORX's UTC clock (the
    same seam as every other stored timestamp); the caller cannot supply it.
    """
    phase_clean, message_clean = _heartbeat_clean_inputs(phase, message)
    store = project.store
    received_at = db_now()
    with store.tx():
        try:
            goal, run = _active_context(project)
        except NotFoundError:
            raise _heartbeat_state_error(
                NotFoundError, "task_not_found",
                f"task {task_id}: no active Goal/Run; heartbeat reports the"
                " active revision's tasks only",
            ) from None
        revision = _active_revision(project, run)
        if revision is None:
            raise _heartbeat_state_error(
                NotFoundError, "task_not_found",
                f"task {task_id}: no active plan revision; heartbeat reports"
                " the active revision's tasks only",
            )
        try:
            task = store.task_get(revision.id, task_id)
        except NotFoundError:
            raise _heartbeat_state_error(
                NotFoundError, "task_not_found",
                f"task {task_id} is not a task of the active revision;"
                " heartbeat cannot report it",
            ) from None
        if TaskStatus(task.status) is not TaskStatus.RUNNING:
            raise _heartbeat_state_error(
                ConflictError, "task_not_running",
                f"task {task_id} is {task.status}; only a running task"
                " accepts progress reports",
            )
        try:
            attempt = store.attempt_get(attempt_id)
        except NotFoundError:
            raise _heartbeat_state_error(
                NotFoundError, "attempt_not_found",
                f"attempt {attempt_id} not found (task {task_id})",
            ) from None
        if attempt.role != Role.WORKER.value or attempt.driver != "host":
            raise _heartbeat_state_error(
                ConflictError, "attempt_not_host_worker",
                f"attempt {attempt_id} is role {attempt.role}, driver"
                f" {attempt.driver}; heartbeat is for host worker attempts"
                " only",
            )
        if attempt.revision_id != revision.id or attempt.task_id != task.task_id:
            raise _heartbeat_state_error(
                ConflictError, "attempt_foreign",
                f"attempt {attempt_id} belongs to a different revision/task"
                f" than the active revision's {task.task_id}; same-numbered"
                " tasks across revisions never share reports",
            )
        if attempt.ended_at is not None:
            raise _heartbeat_state_error(
                ConflictError, "attempt_closed",
                f"attempt {attempt_id} is closed (result {attempt.result!r});"
                " a closed attempt is never revived by a report",
            )
        latest = store.attempt_latest_for_task(revision.id, task.task_id)
        if latest is None or latest.id != attempt_id:
            raise _heartbeat_state_error(
                ConflictError, "attempt_superseded",
                f"attempt {attempt_id} is not the latest attempt for task"
                f" {task_id}; a newer attempt owns the current window",
            )
        store.attempt_progress_add(
            attempt_id, phase_clean, message_clean, received_at
        )
        row = store.attempt_progress_all(attempt_id)[-1]
    return {
        "task": task.task_id,
        "attempt": attempt_id,
        "sequence": row.sequence,
        "phase": row.phase,
        "message": row.message,
        "received_at": row.received_at,
    }


# --- read-only observation (contract §7-§8) --------------------------------
#
# The current window of task T is its latest attempt
# (`attempt_latest_for_task` of the active revision) — the ONLY window any
# observation reads. A window without reports is `unknown`; it never falls
# back to an older attempt's history (a retry's fresh attempt starts
# unknown even though the closed one reported). The read clock is the same
# injectable ORX UTC seam received_at was written with; an age that cannot
# be computed honestly (missing/unparseable received_at, or a received_at
# after the read clock) keeps state `reported`, nulls age_sec, notes the
# anomaly, and never raises the overdue hint. age >= timeout_sec (closed
# boundary: exactly equal IS overdue) yields state `overdue` plus a hint
# that only suggests checking the original session — observing never
# changes task state, opens attempts, or dispatches models.

PROGRESS_NOTE_UNPARSEABLE = "clock anomaly: received_at missing or unparseable"
PROGRESS_NOTE_FUTURE = "clock anomaly: received_at after read clock"


def progress_observation(project: Project, attempt, timeout_min: int | None = None) -> dict:
    """The §7 current-window progress observation for one task's latest
    attempt. Pure read: store + config + the ORX clock seam, nothing else."""
    if timeout_min is None:
        timeout_min = project.config.worker_progress_timeout_min
    timeout_sec = timeout_min * 60

    def block(state, report=None, *, age_sec=None, hint=None, note=None) -> dict:
        return {
            "attempt": attempt.id if attempt is not None else None,
            "state": state,
            "phase": report.phase if report is not None else None,
            "message": report.message if report is not None else None,
            "received_at": report.received_at if report is not None else None,
            "age_sec": age_sec,
            "timeout_sec": timeout_sec,
            "hint": hint,
            "note": note,
        }

    if attempt is None:
        return block("unknown")
    report = project.store.attempt_progress_latest(attempt.id)
    if report is None:
        return block("unknown")
    try:
        if not report.received_at:
            raise ValueError("received_at missing")
        age_sec = (_parse_ts(db_now()) - _parse_ts(report.received_at)).total_seconds()
    except ValueError:
        return block("reported", report, note=PROGRESS_NOTE_UNPARSEABLE)
    if age_sec < 0:
        return block("reported", report, note=PROGRESS_NOTE_FUTURE)
    if age_sec >= timeout_sec:
        handle = attempt.session_ref if attempt.session_ref else "unknown"
        return block(
            "overdue", report, age_sec=age_sec,
            hint=(
                f"no progress report for {int(age_sec // 60)}m"
                f" (>= {timeout_min}m threshold); check the original worker"
                f" session (handle: {handle}) before any fail/retry"
            ),
        )
    return block("reported", report, age_sec=age_sec)


def _verification_counts(store: Store, revision: Revision) -> dict:
    """Current-view verification counts: each task contributes only the rows
    in its current attempt window (`verifications_current`), so per-attempt
    history retained across retries is not double-counted as pending or
    resolved checks."""
    rows = []
    for task in store.tasks_all(revision.id):
        rows.extend(store.verifications_current(revision.id, task.task_id))
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

    quota.refresh(project)  # best-effort preflight before verifier routing

    for task in store.tasks_all(revision.id):
        if TaskStatus(task.status) is not TaskStatus.VERIFYING:
            continue
        # Rows the dispatch fills in bind to the task's latest attempt (the
        # completing worker attempt on a first dispatch) so verification
        # history stays queryable per attempt.
        bound = store.attempt_latest_for_task(revision.id, task.task_id)
        verify.run_command_verifications(
            store, project.root, run.id, revision.id, task,
            timeout=project.config.command_timeout_sec,
            attempt_id=bound.id if bound else None,
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
                # HERE, at dispatch. A persistent attempt is opened and bound
                # to this exact entry; the verdict must close it. Re-dispatch
                # reuses the open attempt instead of opening a second
                # execution (duplicate dispatch never executes twice), and
                # routing edits between dispatch and submit cannot move the
                # attribution. The span starts at dispatch (R006 a113 had a
                # structural zero-second span because both ends were stamped
                # at verdict time) and the nonce rides in the prompt header
                # so verifier sessions are discoverable like worker sessions.
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
                        started=True,
                        isolation="prompt_only",
                        run_id=run.id,
                        verify_entry=item.raw,
                    )
                prompt = verifier_prompt(
                    goal, task, item,
                    gate_summary=_gate_summary(store, revision.id, task),
                    evidence_lines=_evidence_lines(store, revision.id, task.task_id),
                    prior_issues=_prior_issues(store, revision.id, task.task_id),
                    replan_context=_replan_reference_context(
                        store, project.root, revision.id, task.task_id,
                        audience="verifier",
                    ),
                    identity=_identity_block(attempt.nonce),
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
                    f" --attempt {attempt.id} --discover-session"
                )
                entry_out["submit_fail"] = (
                    f"orx verify submit {task.task_id} --result fail --entry {item.raw!r}"
                    f" --attempt {attempt.id} --discover-session"
                    f" --reason \"<issues for the fix loop>\""
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
            replan_context=_replan_reference_context(
                store, project.root, revision.id, task.task_id, audience="verifier"
            ),
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
    _record_attempt_health(store, profile.name, run_result)
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
    discover_session: bool = False,
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

    discovery: dict | None = None
    if bound is not None:
        if session_ref is not None:
            store.attempt_update(bound.id, session_ref=session_ref)
        discovery: dict | None = None
        if (discover_session and bound.driver == "host"
                and bound.harness == "zcode" and bound.session_ref is None):
            # Verifier sessions are discoverable exactly like worker
            # sessions: the dispatch prompt carries the attempt's nonce as
            # its first lines, so the verdict submitter's own session is
            # found by token. Unique -> store; none/ambiguous -> NULL + say.
            from . import zcode_sessions
            try:
                discovery = zcode_sessions.discover_attempt_session(
                    bound.id, project.root,
                    created_within_ms=24 * 3600 * 1000,
                    nonce=bound.nonce,
                )
                sid = zcode_sessions.first_session_id(discovery)
                if sid is not None:
                    store.attempt_update(bound.id, session_ref=sid)
            except zcode_sessions.ZcodeDbUnavailable as e:
                discovery = {"decision": "unavailable", "error": str(e)}
        bound = store.attempt_get(bound.id)
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
    if discovery is not None:
        result["session_discovery"] = discovery
        result["session_ref"] = bound.session_ref
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
        if row.status == "exhausted" and not health.quota_exhaustion_active(row):
            parts.append("reset passed (routes again)")
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


def quota_report(project: Project | None, force: bool = False) -> dict:
    """Live quota snapshots for the three preflight harnesses plus, inside a
    project, the CLI profiles each harness backs. Never raises: an
    unreachable provider reports status unknown."""
    by_harness: dict[str, list[str]] = {h: [] for h in quota.QUOTA_HARNESSES}
    if project is not None:
        for name, profile in project.profiles.items():
            harness = profile.harness.value
            if harness in by_harness and profile.driver.value == "cli":
                by_harness[harness].append(name)
    snapshots = []
    for harness in quota.QUOTA_HARNESSES:
        snapshot = quota.fetch_quota(harness, force=force)
        entry = snapshot.to_dict()
        entry["profiles"] = sorted(by_harness[harness])
        snapshots.append(entry)
    return {"snapshots": snapshots}


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
        # G006 §7: every stored progress report is history, in append order.
        # The detail carries the FULL identity (role, task, attempt number,
        # sequence, phase, message), so an old attempt's report can never be
        # mistaken for the current window's — the current view is the
        # separate `current` block below, never these rows.
        for report in store.attempt_progress_all(attempt.id):
            add(report.received_at, attempt.profile, "attempt.report",
                _detail(attempt.role, label, f"a{attempt.id}",
                        f"#{report.sequence}", report.phase,
                        *(["—", report.message] if report.message else [])),
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

    # Current window (G006 §7): the active revision's tasks with their
    # latest-attempt observation, explicitly labeled as the current view so
    # the history above (attempt.report rows of any age, including closed
    # attempts') can never impersonate it. Follows the task filter only —
    # run/profile filters select history entries, the current window is
    # defined by the active revision alone. Read-only, like the rest of
    # this read model.
    current: list[dict] = []
    try:
        active_goal, active_run = _active_context(project)
        active_revision = _active_revision(project, active_run)
    except NotFoundError:
        active_revision = None
    if active_revision is not None:
        for task in store.tasks_all(active_revision.id):
            if task_id is not None and task.task_id != task_id:
                continue
            attempt = store.attempt_latest_for_task(active_revision.id, task.task_id)
            current.append({
                "task": task.task_id,
                "progress": progress_observation(project, attempt),
            })
    return {"entries": entries, "count": len(entries), "current": current}


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
