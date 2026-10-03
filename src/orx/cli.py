"""The `orx` CLI surface.

Every command accepts `--json`, which prints a JSON envelope to stdout:
`{"ok": true, ...}` or `{"ok": false, "error": "...", "errors": [...]}`.
Errors exit non-zero. Human text is the default.
"""

from __future__ import annotations

import functools
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from orx import __version__, config as config_mod, dispatch, doctor as doctor_mod
from orx import skills as skills_mod
from orx import update as update_mod
from orx.records import ConfigError, NotFoundError, ORXError

app = typer.Typer(
    name="orx",
    help="ORX: orchestrate host and CLI coding agents against one authoritative plan.",
    no_args_is_help=True,
    add_completion=False,
)
goal_app = typer.Typer(help="Goal commands.", no_args_is_help=True)
plan_app = typer.Typer(help="Plan commands.", no_args_is_help=True)
task_app = typer.Typer(help="Task commands.", no_args_is_help=True)
verify_app = typer.Typer(help="Verification commands.", no_args_is_help=True)
resource_app = typer.Typer(help="Resource runtime status.", no_args_is_help=True)
skill_app = typer.Typer(help="User-level skills.", no_args_is_help=True)
config_app = typer.Typer(
    help=(
        "Read and write layered configuration. "
        "Precedence, high to low: environment overrides, project .orx/, "
        "user config, built-in defaults. "
        "Exit 0 on success, 1 when a read or write is rejected, 2 on usage errors."
    ),
    no_args_is_help=True,
)

agent_app = typer.Typer(
    help=(
        "Discover agent harnesses without launching a completion, and show "
        "per-profile health. list shows adapter harnesses (codex, cursor, "
        "shell) and marks zcode host-only. info reads the latest persisted "
        "capability snapshot and the launch contract. probe runs the shared "
        "local probes and writes the snapshot. status shows PROFILE, STATE, "
        "SINCE, and REASON from resource_status. Exit 0 on success, 1 on a "
        "domain error, 2 on usage errors."
    ),
    no_args_is_help=True,
)

app.add_typer(goal_app, name="goal")
app.add_typer(plan_app, name="plan")
app.add_typer(task_app, name="task")
app.add_typer(verify_app, name="verify")
app.add_typer(resource_app, name="resource")
app.add_typer(skill_app, name="skill")
app.add_typer(config_app, name="config")
app.add_typer(agent_app, name="agent")


# ---------------------------------------------------------------------------
# Envelope helpers


def handle_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        json_out = bool(kwargs.get("json_out", False))
        try:
            return fn(*args, **kwargs)
        except ORXError as exc:
            errors = getattr(exc, "errors", None) or getattr(exc, "messages", None)
            if json_out:
                payload = {"ok": False, "error": str(exc)}
                if errors:
                    payload["errors"] = list(errors)
                typer.echo(json.dumps(payload, indent=2))
            else:
                typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
                for item in errors or []:
                    typer.echo(f"  - {item}", err=True)
            raise typer.Exit(1)
    return wrapper


def _ok(json_out: bool, **data) -> None:
    if json_out:
        typer.echo(json.dumps({"ok": True, **data}, indent=2))


JsonOpt = typer.Option(False, "--json", help="Emit a JSON envelope on stdout.")


# ---------------------------------------------------------------------------
# Basic commands


@app.command()
def version(json_out: bool = JsonOpt) -> None:
    """Print the package version."""
    _ok(json_out, version=__version__)
    if not json_out:
        typer.echo(f"orx {__version__}")


@app.command()
@handle_errors
def init(
    path: Path = typer.Argument(Path("."), help="Project directory (default: current)."),
    json_out: bool = JsonOpt,
) -> None:
    """Create .orx/ (config, profiles, SQLite state) if missing."""
    result = dispatch.init_project(Path(path))
    _ok(json_out, **result)
    if not json_out:
        typer.echo(f"initialized ORX project at {result['root']}")
        for item in result["created"]:
            typer.echo(f"  created {item}")


@app.command(name="doctor")
@handle_errors
def doctor(json_out: bool = JsonOpt) -> None:
    """Check environment and project health. Never starts an agent session."""
    root = dispatch.find_project_root()
    result = doctor_mod.run_doctor(root)
    _ok(json_out, **result)
    if not json_out:
        symbols = {"ok": "✓", "warn": "⚠", "fail": "✗"}
        typer.echo(f"orx doctor (version {result['version']})")
        for check in result["checks"]:
            symbol = symbols[check["state"]]
            line = f"{symbol} {check['name']}"
            if check["detail"]:
                line += f": {check['detail']}"
            typer.echo(line)
        typer.echo(
            f"{result['failures']} failure(s), {result['warnings']} warning(s)"
        )
    if not result["ok"]:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Configuration


def _format_config_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return json.dumps(value)
    return str(value)


def _project_root() -> Path | None:
    return dispatch.find_project_root()


def _load_effective(project_root: Path | None):
    if project_root is None:
        absent = Path("/__orx_no_project__/config.toml")
        return config_mod.load_effective(
            absent, absent.with_name("profiles.toml"), check_references=False
        )
    orx_dir = project_root / ".orx"
    return config_mod.load_effective(
        orx_dir / "config.toml", orx_dir / "profiles.toml", check_references=False
    )


def _profile_names(project_root: Path | None) -> set[str]:
    names = config_mod.profile_names_in(config_mod.user_profiles_path())
    if project_root is not None:
        names |= config_mod.profile_names_in(project_root / ".orx" / "profiles.toml")
    return names


def _config_set_help() -> str:
    lines = [
        "Write one validated value to the project layer, or the user layer with --user.",
        "",
        "The project layer is .orx/config.toml (the project that contains the",
        "working directory, or ORX_PROJECT). --user writes only the user",
        "config.toml: ORX_CONFIG_DIR if set, else $XDG_CONFIG_HOME/orx,",
        "else ~/.config/orx/config.toml. A missing user or project file is",
        "created with schema_version = 1 and the single key being set.",
        "Project values, environment overrides, and built-in defaults are not copied in.",
        "",
        "The value is parsed and checked before either target file is changed.",
        "An invalid value or an unsupported key leaves the files byte-for-byte",
        "unchanged. schema_version cannot be set. Other keys already in the",
        "file, including ones this version does not interpret, are kept.",
        "",
        "config get reports the effective value after precedence is applied.",
        "A user-layer write can be hidden by the project file or by an",
        "environment override (ORX_RUNTIME_MAX_PARALLEL,",
        "ORX_RUNTIME_COMMAND_TIMEOUT_SEC). A project-layer write can be",
        "hidden by those environment overrides.",
        "",
        "Writable keys:",
    ]
    for key, desc in config_mod.WRITABLE_SPEC.items():
        lines.append(f"  {key}    {desc}")
    lines += [
        "",
        "Lists accept comma-separated names (a, b) or a JSON array.",
        "Booleans are true or false. Integers are decimal digits, with an",
        "optional leading minus. A value that starts with a dash is the",
        "value, then validated; it is not a flag. Put options before KEY.",
        "",
        "--json prints ok, key, value, layer, path, and created.",
        "value is typed (string, boolean, integer, or array), the same kinds",
        "config get returns. layer is project or user.",
        "Exit 1 rejects the write. Exit 2 is a usage error.",
    ]
    return "\n".join(lines)


@config_app.command("path")
@handle_errors
def config_path(json_out: bool = JsonOpt) -> None:
    """Show resolved user and project configuration paths.

    user_config and user_profiles honor ORX_CONFIG_DIR, then
    XDG_CONFIG_HOME/orx, then ~/.config/orx. user_data honors ORX_DATA_DIR,
    then XDG_DATA_HOME/orx, then ~/.local/share/orx. project_config and
    project_profiles are the .orx files of the project found from the
    working directory or ORX_PROJECT. Those two are null when no project
    applies. Paths are reported even when the files do not exist yet.

    --json fields: user_config, user_profiles, user_data, project_config,
    project_profiles.
    """
    paths = config_mod.resolved_paths(_project_root())
    _ok(json_out, **paths)
    if not json_out:
        typer.echo(f"user config:       {paths['user_config']}")
        typer.echo(f"user profiles:     {paths['user_profiles']}")
        typer.echo(f"user data:         {paths['user_data']}")
        typer.echo(f"project config:    {paths['project_config'] or '(none)'}")
        typer.echo(f"project profiles:  {paths['project_profiles'] or '(none)'}")


@config_app.command("list")
@handle_errors
def config_list(json_out: bool = JsonOpt) -> None:
    """Show effective recognized values and the layer that won for each.

    Layers, high to low: env, project, user, default. Environment overrides
    are only ORX_RUNTIME_MAX_PARALLEL and ORX_RUNTIME_COMMAND_TIMEOUT_SEC.
    Routing lists (plan.light.profiles, plan.standard.profiles,
    plan.deep.profiles, worker.profiles, verify.profiles) come from the
    project file when that file sets the list, otherwise from the user file.

    --json field values is a list of objects with key, value, and origin.
    origin is env, project, user, or default. value is typed.
    Exit 1 when the effective configuration cannot be loaded.
    """
    entries = config_mod.config_entries(_load_effective(_project_root()))
    _ok(json_out, values=entries)
    if not json_out:
        width = max(len(row["key"]) for row in entries)
        for row in entries:
            rendered = _format_config_value(row["value"])
            typer.echo(f"{row['key']:<{width}}  {rendered}  ({row['origin']})")


@config_app.command("get")
@handle_errors
def config_get(
    key: str = typer.Argument(..., metavar="KEY", help="Dotted key, for example runtime.max_parallel."),
    json_out: bool = JsonOpt,
) -> None:
    """Show one effective value and the layer it came from.

    The value and origin match the entry config list reports for the same
    key. Recognized keys are the writable keys of config set. schema_version
    is not an effective value. An unknown key exits 1.

    --json fields: key, value, origin. value is typed (string, boolean,
    integer, or array), not the text stored on disk when a higher layer wins.
    """
    entries = config_mod.config_entries(_load_effective(_project_root()))
    found = next((row for row in entries if row["key"] == key), None)
    if found is None:
        known = ", ".join(config_mod.RECOGNIZED_KEYS)
        raise ConfigError([
            f"unknown config key {key!r}",
            f"recognized keys: {known}",
        ])
    _ok(json_out, **found)
    if not json_out:
        typer.echo(f"{found['key']} = {_format_config_value(found['value'])} ({found['origin']})")


@config_app.command(
    "set",
    help=_config_set_help(),
    context_settings={"allow_interspersed_args": False},
)
@handle_errors
def config_set(
    key: str = typer.Argument(..., metavar="KEY", help="Dotted key. See this command's help for the writable set."),
    value: str = typer.Argument(..., metavar="VALUE", help="New value: scalar text, comma-separated names, or a JSON array."),
    user_layer: bool = typer.Option(
        False,
        "--user",
        help="Write only the user config.toml. Default: write the project .orx/config.toml.",
    ),
    json_out: bool = JsonOpt,
) -> None:
    root = _project_root()
    if user_layer:
        target = config_mod.user_config_path()
        layer = "user"
    else:
        if root is None:
            raise NotFoundError(
                "not an ORX project: no .orx/ directory found "
                "(run `orx init`, set ORX_PROJECT, or pass --user to write the user layer)"
            )
        target = root / ".orx" / "config.toml"
        layer = "project"
    names = _profile_names(root) if key in config_mod.PROFILE_KEYS else None
    result = config_mod.set_config_value(
        target, key, value, create=True, profile_names=names
    )
    _ok(json_out, layer=layer, **result)
    if not json_out:
        verb = "created" if result["created"] else "updated"
        typer.echo(f"{verb} {layer} config {result['path']}")
        typer.echo(f"{result['key']} = {_format_config_value(result['value'])}")


# ---------------------------------------------------------------------------
# Agent discovery


def _render_agent_row(row: dict) -> str:
    kind = "host-only" if row["host_only"] else "adapter"
    binary = f"binary={row['binary']}" if row["binary"] else "no binary"
    probe = "probeable" if row["probeable"] else "no probe"
    return f"{row['harness']:<8} {kind:<10} {binary:<16} {probe}"


@agent_app.command("list")
@handle_errors
def agent_list(json_out: bool = JsonOpt) -> None:
    """List harnesses that have an adapter, plus host-only zcode.

    codex, cursor, and shell have adapters. zcode is host-only: the host
    does the work, and there is no CLI to probe. This command does not
    probe and does not launch a completion.

    --json field harnesses is a list of objects with harness, binary,
    adapter, host_only, and probeable. binary is null when the harness
    has no fixed CLI name. adapter is true for codex, cursor, and shell.
    host_only is true only for zcode. probeable is true for codex and cursor.
    """
    rows = dispatch.agent_list()
    _ok(json_out, harnesses=rows)
    if not json_out:
        for row in rows:
            typer.echo(_render_agent_row(row))


@agent_app.command("info")
@handle_errors
def agent_info(
    harness: str = typer.Argument(..., metavar="HARNESS", help="codex, cursor, shell, or zcode."),
    json_out: bool = JsonOpt,
) -> None:
    """Show the latest capability snapshot and the launch contract.

    Reads the snapshot written by `orx agent probe` from the user data
    directory (ORX_DATA_DIR, else $XDG_DATA_HOME/orx, else ~/.local/share/orx)
    at probes/<harness>.json. Does not run probes and does not launch a
    completion. snapshot is null when no probe has been saved yet. shell
    and zcode have a launch contract and no probe; zcode is host-only.

    --json fields: harness, binary, adapter, host_only, snapshot, launch.
    launch has kind (adapter or host) and summary (the argv contract).
    An unknown harness exits 1. A missing HARNESS argument exits 2.
    """
    data = dispatch.agent_info(harness)
    _ok(json_out, **data)
    if not json_out:
        kind = "host-only" if data["host_only"] else "adapter"
        typer.echo(f"{data['harness']}  {kind}")
        snap = data["snapshot"]
        if snap is None:
            typer.echo("snapshot: (none)")
        else:
            typer.echo(f"snapshot: {snap.get('probed_at')}")
            typer.echo(f"  binary: {snap.get('binary')}")
            typer.echo(f"  version: {snap.get('version')}")
            typer.echo(f"  auth: {snap.get('auth')}")
            typer.echo(f"  models_discoverable: {snap.get('models_discoverable')}")
            features = snap.get("features") or {}
            rendered = ", ".join(f"{key}={value}" for key, value in features.items())
            typer.echo(f"  features: {rendered}")
        typer.echo(f"launch: {data['launch']['summary']}")


@agent_app.command("probe")
@handle_errors
def agent_probe(
    harness: str = typer.Argument(..., metavar="HARNESS", help="codex or cursor."),
    json_out: bool = JsonOpt,
) -> None:
    """Probe one harness and write its capability snapshot.

    Runs the shared local checks only: binary presence, version, help text,
    auth status, and the model catalog. Never launches a completion
    (no `codex exec` prompt, no `agent --print`). Writes
    probes/<harness>.json under the user data directory. A missing binary
    is still success: the snapshot records binary null and auth unknown.

    codex and cursor are probeable. shell and zcode exit 1 (nothing to
    probe; zcode is host-only). An unknown harness exits 1. A missing
    HARNESS argument exits 2.

    --json fields: harness, snapshot, path. snapshot is the capability
    object (harness, binary, version, probed_at, features, auth,
    models_discoverable).
    """
    data = dispatch.agent_probe(harness)
    _ok(json_out, **data)
    if not json_out:
        snap = data["snapshot"]
        typer.echo(f"probed {data['harness']}")
        typer.echo(f"  binary: {snap.get('binary')}")
        typer.echo(f"  version: {snap.get('version')}")
        typer.echo(f"  auth: {snap.get('auth')}")
        typer.echo(f"  wrote {data['path']}")


def _render_health_row(row: dict) -> str:
    since = row["since"] or ""
    return f"{row['profile']:<24} {row['state']:<14} {since:<32} {row['reason']}"


@agent_app.command("status")
@handle_errors
def agent_status(json_out: bool = JsonOpt) -> None:
    """Show per-profile health from resource_status.

    One row for every configured profile, then any resource_status row whose
    profile is no longer defined. Human columns are PROFILE, STATE, SINCE,
    and REASON. SINCE is that row's updated_at. REASON joins last_error_kind
    and note. A cooldown state includes `retry <cooldown_until>`.
    `reset <quota_reset_at>` appears when a quota reset time is known. A
    manual override from `orx resource set` is marked `override`;
    `orx resource clear` drops that mark. Reads SQLite only. Never launches
    a completion and never rewrites TOML.

    --json field profiles is a list of objects: profile, state, since (the
    same timestamp as updated_at), updated_at, reason, last_error_kind, note,
    cooldown_until, quota_reset_at, override. The envelope is
    {"ok": true, "profiles": [...]}. A missing project exits 1. Exit 0 on
    success, 1 on a domain error, 2 on usage errors.
    """
    project = dispatch.open_project()
    try:
        data = dispatch.agent_status(project)
    finally:
        project.close()
    _ok(json_out, **data)
    if not json_out:
        typer.echo(f"{'PROFILE':<24} {'STATE':<14} {'SINCE':<32} REASON")
        for row in data["profiles"]:
            typer.echo(_render_health_row(row))


# ---------------------------------------------------------------------------
# Goal


@goal_app.command("new")
@handle_errors
def goal_new(
    objective: str = typer.Option(..., "--objective", help="What must be true when the Goal is done."),
    acceptance: list[str] = typer.Option(..., "--acceptance", help="Acceptance criterion (repeatable, copied verbatim into tasks)."),
    constraints: list[str] = typer.Option([], "--constraint", help="Constraint (repeatable)."),
    context: str = typer.Option("", "--context", help="Extra context for planners/workers."),
    json_out: bool = JsonOpt,
) -> None:
    """Create the active Goal and its Run."""
    project = dispatch.open_project()
    goal, run = dispatch.create_goal(project, objective, acceptance, constraints, context)
    _ok(
        json_out,
        goal={"id": goal.id, "objective": goal.objective, "acceptance": goal.acceptance,
              "constraints": goal.constraints, "status": goal.status},
        run={"id": run.id, "status": run.status},
    )
    if not json_out:
        typer.echo(f"Goal {goal.id} created (run {run.id}, status {run.status})")
        for item in goal.acceptance:
            typer.echo(f"  acceptance: {item!r}")


@goal_app.command("show")
@handle_errors
def goal_show(json_out: bool = JsonOpt) -> None:
    """Show the active Goal and its Run."""
    project = dispatch.open_project()
    data = dispatch.goal_show(project)
    _ok(json_out, **data)
    if not json_out:
        goal, run = data["goal"], data["run"]
        typer.echo(f"Goal {goal['id']} [{goal['status']}]")
        typer.echo(f"  {goal['objective']}")
        for item in goal["acceptance"]:
            typer.echo(f"  acceptance: {item!r}")
        for item in goal["constraints"]:
            typer.echo(f"  constraint: {item}")
        if goal["context"]:
            typer.echo(f"  context: {goal['context']}")
        if run:
            typer.echo(f"Run {run['id']} [{run['status']}]")


# ---------------------------------------------------------------------------
# Plan


@plan_app.callback(invoke_without_command=True)
@handle_errors
def plan(
    ctx: typer.Context,
    depth: Optional[str] = typer.Option(None, "--depth", help="light | standard | deep (overrides config and tokens)."),
    profile: Optional[str] = typer.Option(None, "--profile", help="Pin the planner profile; disables fallback."),
    json_out: bool = JsonOpt,
) -> None:
    """Route the planner. Host planners receive an assignment to submit back."""
    if ctx.invoked_subcommand is not None:
        return
    _run_plan(depth, profile, json_out)


@app.command()
@handle_errors
def replan(
    depth: Optional[str] = typer.Option(None, "--depth"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    json_out: bool = JsonOpt,
) -> None:
    """Plan again: a new revision replaces the active one (rules apply)."""
    _run_plan(depth, profile, json_out)


def _run_plan(depth: Optional[str], profile: Optional[str], json_out: bool) -> None:
    project = dispatch.open_project()
    result = dispatch.plan_route(project, depth, profile)
    _ok(json_out, **result)
    if not json_out:
        if result["mode"] == "host_required":
            a = result["assignment"]
            typer.echo(f"host assignment {a['id']} (planner profile {a['profile']}, depth {a['depth']})")
            typer.echo(f"  prompt file: {a['prompt_file'] or '(not written)'}")
            typer.echo(f"  submit with: {a['submit']}")
        elif result["mode"] == "completed":
            typer.echo(
                f"plan completed by '{result['profile']}' (depth {result['depth']}):"
                f" revision {result['revision']} active with {result['tasks']} task(s)"
            )
        else:
            typer.echo(
                f"incomplete: {result['reason']} (selected profile {result['selected_profile']})"
            )
            typer.echo(f"  {result['detail']}")


@plan_app.command("submit")
@handle_errors
def plan_submit(
    file: Path = typer.Option(..., "--file", help="Path to a Plan IR JSON document."),
    json_out: bool = JsonOpt,
) -> None:
    """Validate a Plan IR document and make it the active revision."""
    project = dispatch.open_project()
    try:
        ir_data = json.loads(Path(file).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ORXError(f"cannot read plan file {file}: {exc}") from None
    result = dispatch.submit_plan(project, ir_data)
    _ok(json_out, **result)
    if not json_out:
        typer.echo(
            f"plan revision {result['revision']} active"
            f" (depth {result['depth']}, planner {result['planner_profile']},"
            f" {result['tasks']} task(s))"
        )
        if result["superseded_revision"] is not None:
            typer.echo(
                f"  superseded revision {result['superseded_revision']};"
                f" cancelled tasks: {', '.join(result['cancelled_tasks']) or 'none'}"
            )


# ---------------------------------------------------------------------------
# Run / Status


@app.command()
@handle_errors
def run(json_out: bool = JsonOpt) -> None:
    """Execute up to the CLI parallel cap (1); park host/external work. Returns."""
    project = dispatch.open_project()
    result = dispatch.run_slice(project)
    _ok(json_out, **result)
    if not json_out:
        if result.get("note"):
            typer.echo(result["note"])
        for item in result["started"]:
            typer.echo(
                f"started: {item['task']} via {item['profile']}"
                f" -> {item['status']} (verdict {item.get('verdict')}, log {item.get('log')})"
            )
        for item in result["failed"]:
            typer.echo(f"failed: {item['task']} via {item['profile']}: {item.get('reason')}")
        for item in result["host_required"]:
            typer.echo(f"host required: {item['task']} via {item['profile']} ({item['claim']})")
        for item in result["waiting_external"]:
            typer.echo(f"waiting external: {item['task']} via {item['profile']}")
        for item in result["deferred"]:
            typer.echo(f"deferred: {item['task']} ({item['reason']}; run `orx run` again)")
        for item in result["routing_errors"]:
            typer.echo(f"routing error: {item['task']}: {item['error']}")
        if not any(
            result[k]
            for k in ("started", "failed", "host_required", "waiting_external",
                      "deferred", "routing_errors")
        ):
            typer.echo("nothing to dispatch")


@app.command()
@handle_errors
def status(json_out: bool = JsonOpt) -> None:
    """Show Goal, Run, plan revision, tasks, verification, and resources."""
    project = dispatch.open_project()
    data = dispatch.status_data(project)
    _ok(json_out, **data)
    if not json_out:
        _render_status(data)


def _render_status(data: dict) -> None:
    goal = data["goal"]
    run = data["run"]
    banner = {"running": "RUNNING", "done": "DONE", "blocked": "BLOCKED", "planning": "PLANNING"}
    typer.echo(f"Goal {goal['id']} [{goal['status']}]: {goal['objective']}")
    typer.echo(f"Run {run['id']}: {banner.get(run['status'], run['status'].upper())}")
    if data["plan"]:
        p = data["plan"]
        typer.echo(
            f"Plan: revision {p['revision']} ({p['depth']}, planner {p['planner_profile']}) [{p['status']}]"
        )
    markers = {
        "passed": "✓", "failed": "✗", "cancelled": "–",
        "running": "●", "waiting_host": "●", "waiting_external": "●", "verifying": "●",
    }
    for task in data["tasks"]:
        marker = markers.get(task["status"], "○")
        line = f"  {marker} {task['id']} {task['status']:<16} {task['objective']}"
        if task["profile"]:
            line += f"  ({task['profile']})"
        if task["blocked_by"]:
            line += f"  blocked_by: {', '.join(task['blocked_by'])}"
        if task["failure_reason"]:
            line += f"  [{task['failure_reason']}]"
        typer.echo(line)
    v = data["verification"]
    typer.echo(
        f"Verification: {v['passed']} passed / {v['failed']} failed / {v['awaiting_agent']} awaiting agent"
    )
    for resource in data["resources"]:
        typer.echo(f"  resource {resource['profile']:<24} {resource['status']}")
    typer.echo(f"Result: {banner.get(run['status'], run['status'].upper())}")


# ---------------------------------------------------------------------------
# Task


@task_app.command("list")
@handle_errors
def task_list(json_out: bool = JsonOpt) -> None:
    """List tasks of the active plan revision."""
    project = dispatch.open_project()
    tasks = dispatch.task_list(project)
    _ok(json_out, tasks=tasks, count=len(tasks))
    if not json_out:
        if not tasks:
            typer.echo("no active plan revision")
            return
        for task in tasks:
            line = f"{task['id']} {task['status']:<16} {task['objective']}"
            if task["blocked_by"]:
                line += f"  blocked_by: {', '.join(task['blocked_by'])}"
            typer.echo(line)


@task_app.command("claim")
@handle_errors
def task_claim(
    task_id: str = typer.Argument(..., help="Task id, e.g. T001."),
    json_out: bool = JsonOpt,
) -> None:
    """Host claim: move a waiting_host task to running (single winner)."""
    project = dispatch.open_project()
    result = dispatch.task_claim(project, task_id)
    _ok(json_out, **result)
    if not json_out:
        typer.echo(f"task {result['task']} claimed by host; status running")


@task_app.command("complete")
@handle_errors
def task_complete(
    task_id: str = typer.Argument(...),
    evidence: Path = typer.Option(..., "--evidence", help="Evidence file produced by the worker."),
    json_out: bool = JsonOpt,
) -> None:
    """Execution finished. This is NOT success: verification decides passed/failed."""
    project = dispatch.open_project()
    result = dispatch.task_complete(project, task_id, str(evidence))
    _ok(json_out, **result)
    if not json_out:
        typer.echo(f"task {result['task']}: status {result['status']} (verdict {result['verdict']})")
        v = result["verification"]
        typer.echo(
            f"  verification: {v['passed']} passed / {v['failed']} failed / {v['awaiting_agent']} awaiting agent"
        )


@task_app.command("fail")
@handle_errors
def task_fail(
    task_id: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason", help="Why the attempt failed."),
    json_out: bool = JsonOpt,
) -> None:
    """Mark a running/waiting_external task failed."""
    project = dispatch.open_project()
    result = dispatch.task_fail(project, task_id, reason)
    _ok(json_out, **result)
    if not json_out:
        typer.echo(f"task {result['task']}: failed ({reason})")


@task_app.command("retry")
@handle_errors
def task_retry(
    task_id: str = typer.Argument(...),
    json_out: bool = JsonOpt,
) -> None:
    """Retry a failed task on the same plan (not a replan)."""
    project = dispatch.open_project()
    result = dispatch.task_retry(project, task_id)
    _ok(json_out, **result)
    if not json_out:
        typer.echo(f"task {result['task']}: runnable again; `orx run` will route a new attempt")


# ---------------------------------------------------------------------------
# Verify


@verify_app.callback(invoke_without_command=True)
@handle_errors
def verify(
    ctx: typer.Context,
    json_out: bool = JsonOpt,
) -> None:
    """Run deterministic checks and report outstanding agent verifications."""
    if ctx.invoked_subcommand is not None:
        return
    project = dispatch.open_project()
    result = dispatch.verify_dispatch(project)
    _ok(json_out, **result)
    if not json_out:
        for item in result["checked"]:
            typer.echo(f"task {item['task']}: {item['verdict']}")
        for item in result["agent_required"]:
            typer.echo(
                f"agent verification required: task {item['task']} entry {item['entry']!r}"
                f" (capabilities {item['required_capabilities']})"
                + (f" via {item['profile']}" if item.get("profile") else f" ERROR: {item.get('error')}")
            )
        for item in result["launched"]:
            typer.echo(
                f"verifier launched: task {item['task']} entry {item['entry']!r}"
                f" -> {item['verdict']} ({item.get('detail')})"
            )


@verify_app.command("submit")
@handle_errors
def verify_submit(
    task_id: str = typer.Argument(...),
    result: str = typer.Option(..., "--result", help="pass | fail"),
    entry: Optional[str] = typer.Option(None, "--entry", help="Exact agent verification entry this verdict answers."),
    evidence: Optional[Path] = typer.Option(None, "--evidence", help="Optional evidence file."),
    json_out: bool = JsonOpt,
) -> None:
    """Submit a host Agent verifier's verdict for one agent verification entry."""
    project = dispatch.open_project()
    data = dispatch.verify_submit(
        project, task_id, result, entry, str(evidence) if evidence else None
    )
    _ok(json_out, **data)
    if not json_out:
        typer.echo(
            f"task {data['task']}: agent verdict {data['result']} for {data['entry']!r};"
            f" status {data['status']}"
        )


# ---------------------------------------------------------------------------
# Profiles / resources


@app.command()
@handle_errors
def profiles(json_out: bool = JsonOpt) -> None:
    """List parsed profiles (with config layer origin) and resource status."""
    project = dispatch.open_project()
    rows = [
        {**profile.to_dict(),
         "layer": project.profile_origins.get(name, "project"),
         "resource_status": project.store.resource_get(name).value}
        for name, profile in project.profiles.items()
    ]
    _ok(json_out, profiles=rows)
    if not json_out:
        for row in rows:
            typer.echo(
                f"{row['name']:<24} driver={row['driver']:<8} harness={row['harness']:<6}"
                f" class={row['class']:<8} layer={row['layer']:<8}"
                f" resource={row['resource_status']}"
            )


@resource_app.command("list")
@handle_errors
def resource_list(json_out: bool = JsonOpt) -> None:
    """Show runtime resource status per profile (SQLite only, never TOML)."""
    project = dispatch.open_project()
    rows = dispatch.resource_list(project)
    _ok(json_out, resources=rows)
    if not json_out:
        for row in rows:
            typer.echo(f"{row['profile']:<24} {row['status']:<12} {row['note']}")


@resource_app.command("set")
@handle_errors
def resource_set(
    profile: str = typer.Argument(...),
    status: str = typer.Argument(...),
    note: str = typer.Option("", "--note"),
    json_out: bool = JsonOpt,
) -> None:
    """Set a profile's runtime resource status. Writes only SQLite."""
    project = dispatch.open_project()
    result = dispatch.resource_set(project, profile, status, note)
    _ok(json_out, **result)
    if not json_out:
        typer.echo(f"{result['profile']}: {result['status']}")


@resource_app.command("clear")
@handle_errors
def resource_clear(
    profile: str = typer.Argument(...),
    json_out: bool = JsonOpt,
) -> None:
    """Drop a manual override; health auto-learning resumes for the profile."""
    project = dispatch.open_project()
    result = dispatch.resource_clear(project, profile)
    _ok(json_out, **result)
    if not json_out:
        typer.echo(f"{result['profile']}: auto-learning re-enabled")


# ---------------------------------------------------------------------------
# Skills / update


@skill_app.command("install")
@handle_errors
def skill_install(json_out: bool = JsonOpt) -> None:
    """Install packaged skills to ~/.agents/skills (+ symlinks)."""
    result = skills_mod.install_skills()
    _ok(json_out, **result)
    if not json_out:
        typer.echo(f"canonical: {result['canonical_root']}")
        for name in result["installed"]:
            typer.echo(f"  installed {name}")
        for name in result["refreshed"]:
            typer.echo(f"  refreshed {name}")
        for target in result["symlinked_into"]:
            typer.echo(f"  symlinked into {target}")


@skill_app.command("update")
@handle_errors
def skill_update(json_out: bool = JsonOpt) -> None:
    """Replace installed skills with the packaged copies and refresh symlinks."""
    result = skills_mod.update_skills()
    _ok(json_out, **result)
    if not json_out:
        if not result["installed"] and not result["refreshed"]:
            typer.echo("no installed skills to update; run `orx skill install` first")
        for name in result["refreshed"]:
            typer.echo(f"  refreshed {name}")
        for target in result["symlinked_into"]:
            typer.echo(f"  symlinked into {target}")


@app.command()
@handle_errors
def update(
    check: bool = typer.Option(False, "--check", help="Report source and upgrade path; no mutation."),
    json_out: bool = JsonOpt,
) -> None:
    """Upgrade orx (uv tool installs only)."""
    if check:
        result = update_mod.check_update()
        _ok(json_out, **result)
        if not json_out:
            typer.echo(f"orx {result['version']} via {result['source']} ({result['detail']})")
            typer.echo(f"command: {result['command'] or '(none — this install cannot self-upgrade)'}")
        return
    result = update_mod.run_update()
    _ok(json_out, **result)
    if not json_out:
        typer.echo(f"ran: {result['command']} (exit {result['exit_code']})")
        if result["output"]:
            typer.echo(result["output"])


def _timeline_clock(ts: str) -> str:
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[11:19] if "T" in ts and len(ts) >= 19 else ts
    return parsed.strftime("%H:%M:%S")


@app.command()
@handle_errors
def timeline(
    run_id: Optional[str] = typer.Option(None, "--run", help="Only entries for this run (R###)."),
    task_id: Optional[str] = typer.Option(None, "--task", help="Only entries for this task (T###)."),
    profile: Optional[str] = typer.Option(None, "--profile", help="Only entries for this profile."),
    limit: Optional[int] = typer.Option(
        None, "--limit", min=1, help="Newest N entries, still oldest-first."
    ),
    json_out: bool = JsonOpt,
) -> None:
    """Show a strictly time-ordered history merged from existing tables.

    Sources: goal and run creation, planning assignments, routing decisions,
    attempts (including planner and verifier rows), task events, and
    verifications. Human lines are `HH:MM:SS  actor  event  detail`.
    Exit 0 on success, 1 on a domain error, 2 on usage errors.
    """
    project = dispatch.open_project()
    try:
        result = dispatch.timeline(
            project, run_id=run_id, task_id=task_id, profile=profile, limit=limit
        )
    finally:
        project.close()
    _ok(json_out, **result)
    if not json_out:
        for entry in result["entries"]:
            typer.echo(
                f"{_timeline_clock(entry['ts'])}  {entry['actor']}  "
                f"{entry['event']}  {entry['detail']}"
            )


if __name__ == "__main__":
    app()
