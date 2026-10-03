"""The `orx` CLI surface.

Every command accepts `--json`, which prints a JSON envelope to stdout:
`{"ok": true, ...}` or `{"ok": false, "error": "...", "errors": [...]}`.
Errors exit non-zero. Human text is the default.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Optional

import typer

from orx import __version__, dispatch, doctor as doctor_mod
from orx import skills as skills_mod
from orx import update as update_mod
from orx.records import ORXError

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

app.add_typer(goal_app, name="goal")
app.add_typer(plan_app, name="plan")
app.add_typer(task_app, name="task")
app.add_typer(verify_app, name="verify")
app.add_typer(resource_app, name="resource")
app.add_typer(skill_app, name="skill")


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
    """List parsed profiles and their runtime resource status."""
    project = dispatch.open_project()
    rows = [
        {**profile.to_dict(), "resource_status": project.store.resource_get(name).value}
        for name, profile in project.profiles.items()
    ]
    _ok(json_out, profiles=rows)
    if not json_out:
        for row in rows:
            typer.echo(
                f"{row['name']:<24} driver={row['driver']:<8} harness={row['harness']:<6}"
                f" class={row['class']:<8} resource={row['resource_status']}"
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


if __name__ == "__main__":
    app()
