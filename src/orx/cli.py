"""The `orx` CLI surface.

Every command accepts `--json`, which prints a JSON envelope to stdout:
`{"ok": true, ...}` or `{"ok": false, "error": "...", "errors": [...]}`.
Errors exit non-zero. Human text is the default.
"""

from __future__ import annotations

import functools
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from orx import __version__, config as config_mod, dispatch, doctor as doctor_mod
from orx import skills as skills_mod
from orx import sources as sources_mod
from orx import update as update_mod
from orx.records import ConfigError, NotFoundError, ORXError
from orx.verify import DeliveryRejected

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
preset_app = typer.Typer(
    help=(
        "Install packaged presets into the USER layer only. A preset never "
        "reads or writes any project; project layers keep winning. Profile "
        "conflicts refuse the whole install (no partial writes); existing "
        "user config keys and live agent definitions are preserved."
    ),
    no_args_is_help=True,
)
inbox_app = typer.Typer(help="Inbox commands.", no_args_is_help=True)
auth_app = typer.Typer(help="Authentication status (display only).", no_args_is_help=True)
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
app.add_typer(preset_app, name="preset")
app.add_typer(config_app, name="config")
app.add_typer(agent_app, name="agent")
app.add_typer(inbox_app, name="inbox")
app.add_typer(auth_app, name="auth")

usage_app = typer.Typer(
    help=(
        "Show per-profile usage: tasks, runtime, and token sums. "
        "--profile selects one row. --json includes runtime_sec, input_tokens, "
        "and accuracy (exact, estimated, or unknown)."
    ),
    no_args_is_help=False,
)


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
SessionOpt = typer.Option(
    None,
    "--session",
    help=(
        "Opaque native session reference for this host attempt. Overrides "
        "ORX_SESSION_REF. An empty or malformed value fails before any write. "
        "When omitted, ORX_SESSION_REF is used if non-blank; a blank variable "
        "leaves the reference unset."
    ),
)


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
    session: Optional[str] = SessionOpt,
    json_out: bool = JsonOpt,
) -> None:
    """Route the planner. Host planners receive an assignment to submit back."""
    if ctx.invoked_subcommand is not None:
        return
    _run_plan(depth, profile, json_out, session=session)


@app.command()
@handle_errors
def replan(
    depth: Optional[str] = typer.Option(None, "--depth"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    context_file: Optional[Path] = typer.Option(
        None,
        "--context-file",
        help=(
            "This round's intent file: why replanning, what should change, "
            "supporting material. Passed to the planner verbatim alongside the "
            "Goal and the execution-fact snapshot; never rewrites the Goal."
        ),
    ),
    session: Optional[str] = SessionOpt,
    json_out: bool = JsonOpt,
) -> None:
    """Plan again: a new revision replaces the active one (rules apply)."""
    _run_plan(depth, profile, json_out, context_file, session=session)


def _run_plan(
    depth: Optional[str],
    profile: Optional[str],
    json_out: bool,
    context_file: Optional[Path] = None,
    session: Optional[str] = None,
) -> None:
    project = dispatch.open_project()
    result = dispatch.plan_route(
        project, depth, profile,
        str(context_file) if context_file else None,
        session=session,
    )
    _ok(json_out, **result)
    if not json_out:
        if result["mode"] == "host_required":
            a = result["assignment"]
            typer.echo(f"host assignment {a['id']} (planner profile {a['profile']}, depth {a['depth']})")
            if result.get("replan"):
                typer.echo("  replan: execution-fact snapshot attached to the prompt")
            if result.get("context_file"):
                typer.echo(f"  context file: {result['context_file']} (verbatim in the prompt)")
            typer.echo(f"  prompt file: {a['prompt_file'] or '(not written)'}")
            typer.echo(f"  submit with: {a['submit']}")
        elif result["mode"] == "completed":
            typer.echo(
                f"plan completed by '{result['profile']}' (depth {result['depth']}):"
                f" revision {result['revision']} active with {result['tasks']} task(s)"
            )
            if result.get("prompt_file"):
                typer.echo(f"  planner input archived: {result['prompt_file']}")
        else:
            typer.echo(
                f"incomplete: {result['reason']} (selected profile {result['selected_profile']})"
            )
            typer.echo(f"  {result['detail']}")


@plan_app.command("submit")
@handle_errors
def plan_submit(
    file: Path = typer.Option(..., "--file", help="Path to a Plan IR JSON document."),
    session: Optional[str] = SessionOpt,
    json_out: bool = JsonOpt,
) -> None:
    """Validate a Plan IR document and make it the active revision."""
    project = dispatch.open_project()
    try:
        ir_data = json.loads(Path(file).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ORXError(f"cannot read plan file {file}: {exc}") from None
    result = dispatch.submit_plan(project, ir_data, session=session)
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
            via = f" via {item['profile']}" if item.get("profile") else ""
            note = " (resurfaced waiting assignment)" if item.get("resurfaced") else ""
            typer.echo(f"host required: {item['task']}{via} ({item['claim']}){note}")
            if item.get("prompt_file"):
                typer.echo(f"  prompt file: {item['prompt_file']}")
        for item in result["waiting_external"]:
            via = f" via {item['profile']}" if item.get("profile") else ""
            note = " (resurfaced waiting assignment)" if item.get("resurfaced") else ""
            typer.echo(f"waiting external: {item['task']}{via}{note}")
            if item.get("prompt_file"):
                typer.echo(f"  prompt file: {item['prompt_file']}")
        for item in result["deferred"]:
            typer.echo(f"deferred: {item['task']} ({item['reason']}; run `orx run` again)")
        for item in result["routing_errors"]:
            typer.echo(f"routing error: {item['task']}: {item['error']}")
        for item in result.get("recovery", []):
            typer.echo(
                f"recovery: {item['task']} is RUNNING under attempt {item['attempt']}"
                + (f" (session {item['session_ref']})" if item.get("session_ref") else "")
            )
            typer.echo(f"  {item['contract']}")
            if item.get("prompt_file"):
                typer.echo(f"  prompt file: {item['prompt_file']}")
        if not any(
            result[k]
            for k in ("started", "failed", "host_required", "waiting_external",
                      "deferred", "routing_errors", "recovery")
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
    typer.echo(
        f"  lifecycle: started_at {run.get('started_at') or '-'}"
        f"  completed_at {run.get('completed_at') or '-'}"
    )
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
        if task.get("session_ref"):
            line += f"  session {task['session_ref']}"
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
    session: Optional[str] = SessionOpt,
    json_out: bool = JsonOpt,
) -> None:
    """Host claim: move a waiting_host task to running (single winner)."""
    project = dispatch.open_project()
    result = dispatch.task_claim(project, task_id, session=session)
    _ok(json_out, **result)
    if not json_out:
        typer.echo(f"task {result['task']} claimed by host; status running")


@task_app.command("check")
@handle_errors
def task_check(
    task_id: str = typer.Argument(..., help="Task id, e.g. T001."),
    json_out: bool = JsonOpt,
) -> None:
    """Run the task's command checks immediately (same-session self-check).

    Executes every `shell ...` verification entry of the task now, from the
    project root, with the same denylist and timeout as `orx verify`, and
    appends one verification row per entry bound to the current worker
    attempt. Each result reports the command, exit code, passed flag, the
    earliest failing output line, and the log path, with summary counts.

    Check rounds accumulate on that attempt across calls: the check_rounds
    field (and the human "check rounds" line) reports rounds used vs the
    configured worker.max_check_rounds budget, this run included. When the
    budget is spent and red checks remain, the output says so: the worker
    contract is to submit the structured failed/blocked delivery result and
    hand the task back to the controller, not to keep iterating.

    This is a worker self-check, not a verdict: the task's status never
    changes, agent verification entries are not run, and a green check does
    not replace the independent verification at `orx task complete`. A
    denied (denylist) entry records exit_code null and passed false, like
    verify. Exit 0 after running the checks (failing entries included);
    exit 1 when the task or the plan revision does not exist.
    """
    project = dispatch.open_project()
    try:
        result = dispatch.task_check(project, task_id)
    finally:
        project.close()
    _ok(json_out, **result)
    if not json_out:
        if result.get("note"):
            typer.echo(f"task {result['task']}: {result['note']}")
        for item in result["results"]:
            if item["denied"]:
                typer.echo(
                    f"  denied  {item['command']} (denylist: {item['denial_reason']})"
                )
            elif item["passed"]:
                typer.echo(f"  ok      {item['command']}")
            else:
                exit_note = "timeout" if item["timed_out"] else f"exit {item['exit_code']}"
                typer.echo(f"  FAIL    {item['command']} ({exit_note})")
            if item.get("error_summary"):
                typer.echo(f"          {item['error_summary']}")
            if item.get("log_path"):
                typer.echo(f"          log: {item['log_path']}")
        summary = result["summary"]
        line = (
            f"task {result['task']} [{result['status']}]: {summary['total']}"
            f" command check(s): {summary['passed']} passed,"
            f" {summary['failed']} failed, {summary['denied']} denied"
        )
        if result["agent_entries_not_run"]:
            line += (
                f"; {result['agent_entries_not_run']} agent entries not run"
                " (check covers command entries only)"
            )
        typer.echo(line)
        rounds = result.get("check_rounds")
        if rounds is not None:
            where = (
                f"attempt {rounds['attempt']}" if rounds["attempt"] is not None
                else "no worker attempt bound (counts against no budget)"
            )
            typer.echo(
                f"  check rounds: {rounds['used']}/{rounds['budget']} used ({where};"
                " budget: worker.max_check_rounds)"
            )
            red = summary["failed"] or summary["denied"]
            if rounds["exceeded"]:
                typer.echo(
                    "  check budget EXCEEDED: stop here — submit the structured"
                    " failed/blocked delivery result and hand the task back to"
                    " the controller; do not run further rounds"
                )
            elif rounds["remaining"] == 0 and red:
                typer.echo(
                    "  check budget exhausted with red checks: do not iterate"
                    " further — submit the structured failed/blocked delivery"
                    " result and hand the task back to the controller"
                )
        typer.echo(
            "  self-check only: status unchanged; `orx task complete` still verifies"
        )


def _render_delivery_rejected(exc: DeliveryRejected, json_out: bool) -> None:
    """A refused delivery: full structured detail on both surfaces.

    The task keeps its prior status and the attempt stays open, so the
    worker can fix and complete again — the output must carry everything
    needed for that: each missing evidence field, or each red check's
    command, exit code, error summary, and log path.
    """
    if json_out:
        payload = {"ok": False, "error": str(exc)}
        if exc.fields:
            payload["evidence_errors"] = list(exc.fields)
        if exc.report is not None:
            payload["gate"] = exc.report
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        for field_error in exc.fields:
            typer.echo(f"  - {field_error}", err=True)
        if exc.report is not None:
            for row in exc.report["failures"]:
                if row.get("denied"):
                    exit_note = f"denied: {row['denial_reason']}"
                elif row.get("timed_out"):
                    exit_note = "timeout"
                else:
                    exit_note = f"exit {row['exit_code']}"
                typer.echo(f"  FAIL {row['command']} ({exit_note})", err=True)
                if row.get("error_summary"):
                    typer.echo(f"        {row['error_summary']}", err=True)
                if row.get("log_path"):
                    typer.echo(f"        log: {row['log_path']}", err=True)


@task_app.command("complete")
@handle_errors
def task_complete(
    task_id: str = typer.Argument(...),
    evidence: Path = typer.Option(
        ..., "--evidence",
        help=(
            "Structured delivery result JSON: status=passed|failed|blocked,"
            " checks[] (each command/exit_code/log), artifacts[], summary."
            " Missing or malformed fields are rejected by name."
        ),
    ),
    attempt: Optional[int] = typer.Option(
        None, "--attempt", help="Attempt id this completion answers (from `task claim` / the park payload)."
    ),
    actual_model: Optional[str] = typer.Option(
        None, "--actual-model",
        help="Model the executor actually ran (e.g. from the ZCode dispatch receipt), reported not guessed.",
    ),
    json_out: bool = JsonOpt,
) -> None:
    """Execution finished. This is NOT success: verification decides passed/failed.

    The evidence file is the structured delivery result. status=passed runs
    the delivery gate first: every command verification entry is re-run
    fresh (a prior self-check is not an exemption) and any red row rejects
    the completion — the task keeps its current status, the attempt stays
    open, and the response lists each failure's command, exit code, error
    summary, and log path. status=failed/blocked records a non-delivery
    and fails the task with a reason that says which kind it was. Agent
    verification entries are never run here and never gate the completion.
    """
    project = dispatch.open_project()
    try:
        result = dispatch.task_complete(
            project, task_id, str(evidence),
            attempt_id=attempt, actual_model=actual_model,
        )
    except DeliveryRejected as exc:
        _render_delivery_rejected(exc, json_out)
        raise typer.Exit(1) from None
    finally:
        project.close()
    _ok(json_out, **result)
    if not json_out:
        verdict = result.get("verdict")
        line = f"task {result['task']}: status {result['status']}"
        if verdict:
            line += f" (verdict {verdict})"
        typer.echo(line)
        delivery = result.get("delivery")
        if delivery:
            line = f"  delivery: {delivery['status']}"
            if delivery.get("reason"):
                line += f" — {delivery['reason']}"
            typer.echo(line)
        v = result.get("verification")
        if v:
            typer.echo(
                f"  verification: {v['passed']} passed / {v['failed']} failed / {v['awaiting_agent']} awaiting agent"
            )
        if result.get("model_mismatch"):
            m = result["model_mismatch"]
            typer.echo(
                f"  WARNING model mismatch: requested {m['requested']}, reported {m['reported']}"
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
    reason: Optional[str] = typer.Option(None, "--reason", help="On a fail: the reviewer's issues; recorded as the failure state and fed to the next attempt."),
    session: Optional[str] = SessionOpt,
    attempt: Optional[int] = typer.Option(
        None, "--attempt", help="Verifier attempt id this verdict closes (from `orx verify` dispatch)."
    ),
    actual_model: Optional[str] = typer.Option(
        None, "--actual-model",
        help="Model the verifier actually ran (e.g. from the ZCode dispatch receipt), reported not guessed.",
    ),
    json_out: bool = JsonOpt,
) -> None:
    """Submit a host Agent verifier's verdict for one agent verification entry."""
    project = dispatch.open_project()
    data = dispatch.verify_submit(
        project, task_id, result, entry, str(evidence) if evidence else None, reason,
        session=session, attempt_id=attempt, actual_model=actual_model,
    )
    _ok(json_out, **data)
    if not json_out:
        typer.echo(
            f"task {data['task']}: agent verdict {data['result']} for {data['entry']!r};"
            f" status {data['status']}"
        )
        if data.get("model_mismatch"):
            m = data["model_mismatch"]
            typer.echo(
                f"  WARNING model mismatch: requested {m['requested']}, reported {m['reported']}"
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


@app.command()
@handle_errors
def completion(
    shell: str = typer.Argument(..., help="zsh | bash | fish"),
    json_out: bool = JsonOpt,
) -> None:
    """Emit a shell completion script for the orx command tree.

    Usage: eval "$(orx completion zsh)". The script drives the hidden
    _ORX_COMPLETE protocol; no project required; exit 0 on success, 1 when
    the shell name is unknown, 2 on usage errors.
    """
    # typer >= 0.27 ships its own completion script generator
    try:
        from typer._completion_shared import get_completion_script
        script = get_completion_script(prog_name="orx",
                                       complete_var="_ORX_COMPLETE", shell=shell)
    except ImportError:  # pragma: no cover - older typer
        from click.shell_completion import get_completion_class
        comp_cls = get_completion_class(shell)
        comp = comp_cls(cli=app, ctx_args={}, prog_name="orx", complete_var="_ORX_COMPLETE")
        script = comp.source()
    if json_out:
        _ok(True, shell=shell, script=script)
    else:
        typer.echo(script)


# ---------------------------------------------------------------------------
# Skills / update


@skill_app.command("install")
@handle_errors
def skill_install(
    names: list[str] = typer.Argument(
        None,
        help=(
            "Skills to install, e.g. orx-pbv (default: the packaged default "
            "set orx-controller orx-agent)."
        ),
    ),
    json_out: bool = JsonOpt,
) -> None:
    """Install packaged skills to ~/.agents/skills (+ symlinks).

    With no NAMES, installs the default set (orx-controller, orx-agent).
    Explicit names install only those skills; an already-installed skill is
    refreshed. Unknown names are rejected with the list of available skills.
    """
    result = skills_mod.install_skills(names=names or None)
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


# ---------------------------------------------------------------------------
# Presets


@preset_app.command("list")
@handle_errors
def preset_list(json_out: bool = JsonOpt) -> None:
    """List packaged presets."""
    from orx import presets as presets_mod
    rows = presets_mod.list_presets()
    _ok(json_out, presets=rows)
    if not json_out:
        for row in rows:
            typer.echo(f"{row['name']:<12} {row['profiles']} profile(s)")


@preset_app.command("install")
@handle_errors
def preset_install(
    name: str = typer.Argument(..., help="Preset name, e.g. zcode."),
    json_out: bool = JsonOpt,
) -> None:
    """Install a preset into the USER layer (never touches any project)."""
    from orx import presets as presets_mod
    report = presets_mod.install_preset(name)
    _ok(json_out, **report)
    if not json_out:
        typer.echo(f"preset {name} -> user layer")
        typer.echo(f"  profiles added: {', '.join(report['profiles_added']) or '(none)'}")
        typer.echo(f"  profiles preserved: {', '.join(report['profiles_preserved']) or '(none)'}")
        typer.echo(
            f"  config keys added: {len(report['config_added'])},"
            f" preserved: {len(report['config_preserved'])}"
        )
        for agent in report["agents"]:
            state = "installed" if agent["installed"] else (
                "preserved existing" if agent["preserved_existing"] else "MISSING (install manually)"
            )
            typer.echo(f"  agent {agent['name']}: {state}")


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


def _format_runtime(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def _token_cell(value) -> str:
    return "-" if value is None else str(value)


def _render_usage_row(row: dict) -> str:
    return (
        f"{row['profile']:<24} {row['tasks']:>5}  "
        f"{_format_runtime(row['runtime_sec']):>10}  "
        f"{_token_cell(row['input_tokens']):>10}  "
        f"{_token_cell(row['output_tokens']):>10}  "
        f"{_token_cell(row['cached_input_tokens']):>10}  "
        f"{row['accuracy']}"
    )


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


@usage_app.callback(invoke_without_command=True)
@handle_errors
def usage(
    ctx: typer.Context,
    profile: Optional[str] = typer.Option(
        None, "--profile", help="Only the aggregate for this profile."
    ),
    json_out: bool = JsonOpt,
) -> None:
    """Show per-profile usage: tasks, runtime, and token sums.

    Tasks are distinct task ids on attempts. Runtime is the sum of each
    attempt's started_at..ended_at span (incomplete attempts add no time).
    Token columns sum usage_observations. A column is `-` / null when no
    observation recorded that field, or when any observation omitted it.
    Missing tokens are not stored as zero. An attempt with no observation
    does not erase sums from the rows that exist.

    Accuracy is `exact`, `estimated`, or `unknown`. Unknown is a successful
    result: an attempt with no observation (shell, or a stream that carried
    no usage) makes the profile `unknown` while tasks, runtime, and any
    observed token sums still report. Exact wins only when every attempt
    has an observation and every observation is exact. That label mixes
    coverage into the aggregate. `coverage` reports attempts, observed, and
    measurement_accuracy separately: measurement_accuracy is only the stored
    observations and stays `unknown` when there are none.

    Human columns are PROFILE, TASKS, RUNTIME, INPUT, OUTPUT, CACHED,
    ACCURACY. RUNTIME is `H:MM:SS`. A coverage line follows the table.
    host_report rows are listed when any exist. Cached tokens are not
    subtracted from input, and no fee is computed.

    --json keeps field profiles (profile, tasks, runtime_sec, input_tokens,
    output_tokens, cached_input_tokens, accuracy) and adds observations
    (source, accuracy, session_ref; host_report included), coverage,
    sessions (session_ref), and runs (started_at, completed_at). The
    envelope is {"ok": true, ...}. An unknown --profile exits 1. A missing
    project exits 1. Exit 0 on success, 1 on a domain error, 2 on usage
    errors. `orx usage record` writes one host_report.
    """
    if ctx.invoked_subcommand is not None:
        return
    project = dispatch.open_project()
    try:
        result = dispatch.usage(project, profile=profile)
    finally:
        project.close()
    _ok(json_out, **result)
    if not json_out:
        typer.echo(
            f"{'PROFILE':<24} {'TASKS':>5}  {'RUNTIME':>10}  "
            f"{'INPUT':>10}  {'OUTPUT':>10}  {'CACHED':>10}  ACCURACY"
        )
        for row in result["profiles"]:
            typer.echo(_render_usage_row(row))
        for row in result["coverage"]:
            typer.echo(
                f"coverage {row['profile']:<24} attempts {row['attempts']}"
                f"  observed {row['observed']}"
                f"  measurement {row['measurement_accuracy']}"
            )
        reports = [
            obs for obs in result["observations"] if obs["source"] == "host_report"
        ]
        for obs in reports:
            typer.echo(
                f"host_report attempt {obs['attempt_id']}"
                f"  in {_token_cell(obs['input_tokens'])}"
                f"  out {_token_cell(obs['output_tokens'])}"
                f"  cached {_token_cell(obs['cached_input_tokens'])}"
                f"  {obs['accuracy']}"
                f"  session {obs['session_ref'] or '-'}"
            )
        for run in result["runs"]:
            if run["started_at"] or run["completed_at"]:
                typer.echo(
                    f"run {run['id']}  started_at {run['started_at'] or '-'}"
                    f"  completed_at {run['completed_at'] or '-'}"
                )


@usage_app.command("record")
@handle_errors
def usage_record(
    attempt: int = typer.Option(..., "--attempt", help="Attempt id to attach the observation to."),
    input_tokens: int = typer.Option(..., "--input", help="Input token count (nonnegative integer)."),
    output_tokens: int = typer.Option(..., "--output", help="Output token count (nonnegative integer)."),
    cached: Optional[int] = typer.Option(
        None, "--cached",
        help="Cached input tokens. Omit to store NULL. May exceed --input. Not subtracted from input.",
    ),
    accuracy: str = typer.Option(
        "exact", "--accuracy",
        help="Measurement accuracy: exact | estimated. Default exact. Not a coverage claim.",
    ),
    json_out: bool = JsonOpt,
) -> None:
    """Record one host_report observation for an attempt.

    Counts must be nonnegative integers. --cached is optional and stays NULL
    when omitted; a cached count larger than input is stored as given. There
    is no token normalization across runners and no token-to-fee conversion.

    Profile, run, and task are read from the attempt and its stored run
    association. This command does not accept those as arguments. An attempt
    with no run association is rejected rather than guessed.

    Repeating the same counts and accuracy is idempotent. A different report
    for that attempt is rejected and does not increase totals. --accuracy is
    exact or estimated (the measurement). Coverage is reported by `orx usage`,
    separately.

    Exit 0 on success, 1 on a domain error (unknown attempt, bad count,
    conflicting report), 2 on usage errors.
    """
    project = dispatch.open_project()
    try:
        result = dispatch.usage_record(
            project, attempt, input_tokens, output_tokens, cached, accuracy,
        )
    finally:
        project.close()
    _ok(json_out, **result)
    if not json_out:
        cached_cell = _token_cell(result["cached_input_tokens"])
        note = " (already recorded)" if result["idempotent"] else ""
        typer.echo(
            f"host_report attempt {result['attempt']} profile {result['profile']}"
            f" run {result['run_id']} task {result['task_id'] or '-'}"
            f" in {result['input_tokens']} out {result['output_tokens']}"
            f" cached {cached_cell} accuracy {result['accuracy']}{note}"
        )


app.add_typer(usage_app, name="usage")


# ---------------------------------------------------------------------------
# Inbox / watch / auth


def _render_inbox_row(row: dict) -> str:
    return (
        f"{row['id']:<4} {row['status']:<10} {row['source'] or '-':<8} "
        f"{row['created_at']:<32} {row['title']}"
    )


@inbox_app.command("list")
@handle_errors
def inbox_list(
    status: Optional[str] = typer.Option(
        None, "--status", help="Filter: pending | accepted | rejected | dismissed (default: all)."
    ),
    json_out: bool = JsonOpt,
) -> None:
    """List inbox items, oldest first.

    Each row joins the item with its external event. Requires a project.
    --json field items is a list of objects with id, event_id, source,
    external_id, kind, title, body, url, status, goal_id, created_at, and
    decided_at. An invalid --status value exits 1. Exit 0 on success, 1 on a
    domain error, 2 on usage errors.
    """
    project = dispatch.open_project()
    try:
        items = project.store.inbox_items(status)
    finally:
        project.close()
    _ok(json_out, items=items, count=len(items))
    if not json_out:
        if not items:
            typer.echo("inbox is empty")
            return
        typer.echo(f"{'ID':<4} {'STATUS':<10} {'SOURCE':<8} {'CREATED':<32} TITLE")
        for row in items:
            typer.echo(_render_inbox_row(row))


@inbox_app.command("show")
@handle_errors
def inbox_show(
    item_id: int = typer.Argument(..., metavar="ID", help="Inbox item id from `orx inbox list`."),
    json_out: bool = JsonOpt,
) -> None:
    """Show one inbox item in full.

    --json field item is the same object `orx inbox list` reports. The body
    and the linked goal_id (set by accept) are included. An unknown id
    exits 1. Exit 0 on success, 1 on a domain error, 2 on usage errors.
    """
    project = dispatch.open_project()
    try:
        item = project.store.inbox_item_get(item_id)
    finally:
        project.close()
    _ok(json_out, item=item)
    if not json_out:
        typer.echo(f"item {item['id']} [{item['status']}] from {item['source']} {item['kind']} {item['external_id']}")
        typer.echo(f"  title: {item['title']}")
        if item["url"]:
            typer.echo(f"  url: {item['url']}")
        if item["body"]:
            typer.echo(f"  body: {item['body']}")
        typer.echo(f"  created_at: {item['created_at']}")
        if item["decided_at"]:
            typer.echo(f"  decided_at: {item['decided_at']}")
        if item["goal_id"]:
            typer.echo(f"  goal: {item['goal_id']}")


def _inbox_decide(item_id: int, status: str, json_out: bool) -> None:
    project = dispatch.open_project()
    try:
        item = project.store.inbox_decide(item_id, status)
    finally:
        project.close()
    _ok(json_out, item=item)
    if not json_out:
        typer.echo(f"inbox item {item['id']}: {status}")


@inbox_app.command("accept")
@handle_errors
def inbox_accept(
    item_id: int = typer.Argument(..., metavar="ID", help="Inbox item id from `orx inbox list`."),
    json_out: bool = JsonOpt,
) -> None:
    """Accept an inbox item: create the active Goal and link it.

    The Goal objective is the item title plus a one-line body summary;
    acceptance is `the source issue <external_id> is resolved`. ORX keeps one
    active Goal per project: when a Goal is already active this errors loudly
    and exits 1, leaving the item pending. The item must be pending.
    --json fields: item, goal, run. Never launches a completion.
    """
    project = dispatch.open_project()
    try:
        result = sources_mod.accept_item(project, item_id)
    finally:
        project.close()
    _ok(json_out, **result)
    if not json_out:
        typer.echo(f"inbox item {item_id} accepted -> Goal {result['goal']['id']} (run {result['run']['id']})")
        typer.echo(f"  objective: {result['goal']['objective']}")
        for criterion in result["goal"]["acceptance"]:
            typer.echo(f"  acceptance: {criterion!r}")


@inbox_app.command("reject")
@handle_errors
def inbox_reject(
    item_id: int = typer.Argument(..., metavar="ID", help="Inbox item id from `orx inbox list`."),
    json_out: bool = JsonOpt,
) -> None:
    """Reject an inbox item without creating a Goal.

    Sets status rejected and decided_at; goal_id stays null. The item must
    exist; an unknown id exits 1. --json field item is the updated row.
    Exit 0 on success, 1 on a domain error, 2 on usage errors.
    """
    _inbox_decide(item_id, "rejected", json_out)


@inbox_app.command("dismiss")
@handle_errors
def inbox_dismiss(
    item_id: int = typer.Argument(..., metavar="ID", help="Inbox item id from `orx inbox list`."),
    json_out: bool = JsonOpt,
) -> None:
    """Dismiss an inbox item without creating a Goal.

    Sets status dismissed and decided_at; goal_id stays null. Use dismiss for
    not-now and reject for not-ever. An unknown id exits 1. --json field item
    is the updated row. Exit 0 on success, 1 on a domain error, 2 on usage errors.
    """
    _inbox_decide(item_id, "dismissed", json_out)


@app.command()
@handle_errors
def watch(
    once: bool = typer.Option(False, "--once", help="Run a single poll pass and exit."),
    interval: int = typer.Option(
        300, "--interval", min=1, help="Seconds between passes in long mode (default 300)."
    ),
    json_out: bool = JsonOpt,
) -> None:
    """Poll GitHub issues into the inbox. Never launches a completion.

    One pass runs `gh issue list --json` (gh owns credentials; ORX stores no
    token), dedupes on (source, external_id) into external_events, and adds a
    pending inbox item per new event. Policy comes from [inbox]: github_labels
    filters the query (no filter when empty), auto_accept accepts one new
    item per pass via the `orx inbox accept` routine — only while no Goal is
    active. --once prints one report; without it the command loops every
    --interval seconds until Ctrl-C (clean exit 0). Requires a project and a
    working `gh` (missing or unauthenticated gh exits 1). --json fields per
    pass: fetched, new_events, new_items, skipped. Exit 0 on success, 1 on a
    domain error, 2 on usage errors.
    """
    project = dispatch.open_project()
    try:
        gh_ok, gh_detail = sources_mod.check_gh()
        if not gh_ok:
            raise ORXError(f"watch: {gh_detail}")
        labels = project.config.inbox_github_labels
        auto_accept = project.config.inbox_auto_accept

        def one_pass() -> dict:
            return sources_mod.watch_once(
                project.store, labels, auto_accept=auto_accept, project=project
            )

        if once:
            report = one_pass()
            _ok(json_out, **report)
            if not json_out:
                typer.echo(
                    f"watch: fetched={report['fetched']} new_events={report['new_events']}"
                    f" new_items={report['new_items']} skipped={report['skipped']}"
                )
            return
        if not json_out:
            typer.echo(f"watching every {interval}s (Ctrl-C to stop)")
        while True:
            report = one_pass()
            _ok(json_out, **report)
            if not json_out:
                typer.echo(
                    f"watch: fetched={report['fetched']} new_events={report['new_events']}"
                    f" new_items={report['new_items']} skipped={report['skipped']}"
                )
            time.sleep(interval)
    except KeyboardInterrupt:
        # Ctrl-C is the intended way to stop the long mode: clean exit.
        if not json_out:
            typer.echo("watch stopped")
        raise typer.Exit(0)
    finally:
        project.close()


@auth_app.command("status")
@handle_errors
def auth_status(json_out: bool = JsonOpt) -> None:
    """Show GitHub CLI authentication status (display only).

    Delegates to `gh auth status`; ORX never stores or reads a GitHub token
    and offers no login/logout. Works outside a project. Exit 0 when gh is
    present and authenticated, 1 otherwise (the reason is the error).
    --json fields: gh (bool) and detail (the gh auth summary line).
    """
    gh_ok, detail = sources_mod.check_gh()
    if not gh_ok:
        raise ORXError(detail)
    _ok(json_out, gh=True, detail=detail)
    if not json_out:
        typer.echo(f"gh auth: {detail}")


if __name__ == "__main__":
    app()
