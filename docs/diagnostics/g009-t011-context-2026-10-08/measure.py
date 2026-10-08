#!/usr/bin/env python3
"""G009 T011 static context measurement — prompt construction, before vs
after the compression, on the REAL task rows of this run (R009).

Deterministic and free: no model is called, no state is written. The ORX
state DB is copied to a temp dir and opened read-only there; the source
files are never touched.

Method (all three renderings share IDENTICAL inputs — same task row, same
goal, empty prior-failure/replan/identity blocks — so the delta isolates
the scaffolding change exactly):

  old  — the pre-T011 worker prompt renderer, quoted verbatim from the
         parent commit (git show <parent>:src/orx/dispatch.py). It listed
         every verification entry TWICE (a plain "prescribed checks" list
         plus the pre-flight row) and always inlined the four-clause
         delivery contract.
  new_full  — the T011 renderer, full contract form: every check once
         (the pre-flight row is the listing); CLI/external/self channels.
  new_brief — the T011 renderer, brief contract form: host workers whose
         profile runs under the agents/orx-worker.md definition (the
         harness injects it; it carries the four clauses verbatim).

The archived assignment files (.orx/runs/R009/assignments/*.md) are the
real dispatched baseline sizes (they include nonce + failure context, so
they are reported as raw facts beside the controlled comparison, never
subtracted from).

Token estimates use bytes/4 — a rough ASCII-ish heuristic, labeled as
such. Harness tool schemas and agent-definition injection are NOT
measured here: they are not ORX-generated (limitation, stated in the
report).

Usage: .venv/bin/python docs/diagnostics/g009-t011-context-2026-10-08/measure.py
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from orx import dispatch  # noqa: E402
from orx.adapters.base import DELIVERY_CHANNEL_MESSAGE  # noqa: E402
from orx.state import Goal as GoalRow, TaskRow  # noqa: E402

# ---------------------------------------------------------------------------
# The pre-T011 renderer, quoted verbatim from the parent commit (only the
# two format-time inputs _preflight_block/task/CLI_FINAL_DELIVERY_BLOCK are
# imported from the live module — both are unchanged by T011).


def _old_preflight_block(task: TaskRow) -> str:
    rows = dispatch.preflight_task_checks(task)
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


def old_worker_prompt(goal, task, check_budget: int = 3) -> str:
    """The pre-T011 template (git parent of the T011 commit), host/exit
    channel form — the form every host worker of R009 actually received."""
    acceptance = "\n".join(f"  - {item!r}" for item in task.acceptance) or "  (none listed)"
    verification = "\n".join(f"  - {entry}" for entry in task.verification) or "  (none)"
    allowed = ", ".join(task.scope.get("allowed", [])) or "(none)"
    constraints = "\n".join(f"  - {c}" for c in goal.constraints) or "  (none)"
    context_block = (
        "Goal context (background; binding only where it repeats a constraint):\n"
        + goal.context + "\n\n"
        if goal.context else ""
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

How your work will be checked (the prescribed checks):
{verification}

{_old_preflight_block(task)}Delivery contract (binding, in order):
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
   A CLI-launched worker does not control the process exit code — its
   blocked delivery is the structured document of clause 5.
3. CHECK-FIX LOOP (budget: {check_budget} check round(s) on this attempt):
   when a check is red, repair it in THIS session — check -> fix -> check
   again — while budget remains. `orx task check {task.task_id}` reports
   check_rounds (rounds used vs budget) for your attempt after every run.
   Red checks that are EXPECTED at this stage of development are part of
   the work, not failures (TDD: a test written before its implementation is
   deliberately red): keep implementing in the same session — do not
   restart the task or request a new attempt merely because a check is
   temporarily red. Run the full check set at stage boundaries only — when
   a new behavior is complete, when a module modification is complete, and
   when you are ready to deliver — not after every edit. When the budget
   is exhausted and checks are still red, stop iterating: submit the
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


# ---------------------------------------------------------------------------
# Read-only state access (copy first; never open the live DB)


def _read_only_conn(db_path: Path) -> sqlite3.Connection:
    tmp = Path(tempfile.mkdtemp(prefix="orx-t011-measure-"))
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(db_path) + suffix)
        if source.exists():
            shutil.copy2(source, tmp / (source.name))
    conn = sqlite3.connect(f"file:{tmp / db_path.name}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    db = PROJECT_ROOT / ".orx" / "state.db"
    conn = _read_only_conn(db)

    goal_row = conn.execute(
        "SELECT * FROM goals WHERE status = 'active' OR status = 'done'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    run_row = conn.execute(
        "SELECT * FROM runs WHERE goal_id = ? ORDER BY id DESC LIMIT 1",
        (goal_row["id"],)).fetchone()
    revision_row = conn.execute(
        "SELECT * FROM plan_revisions WHERE run_id = ? AND status = 'active'",
        (run_row["id"],)).fetchone()
    task_rows = conn.execute(
        "SELECT * FROM tasks WHERE revision_id = ? ORDER BY task_id",
        (revision_row["id"],)).fetchall()

    goal = GoalRow(
        id=goal_row["id"], objective=goal_row["objective"],
        acceptance=json.loads(goal_row["acceptance_json"]),
        constraints=json.loads(goal_row["constraints_json"]),
        context=goal_row["context"] or "", status=goal_row["status"],
        created_at=goal_row["created_at"], updated_at=goal_row["updated_at"],
    )

    entries = []
    totals = {"old": 0, "new_full": 0, "new_brief": 0, "archived": 0}
    for row in task_rows:
        task = TaskRow(
            row_id=row["id"], revision_id=row["revision_id"],
            task_id=row["task_id"], objective=row["objective"],
            scope=json.loads(row["scope_json"]),
            acceptance=json.loads(row["acceptance_json"]),
            verification=json.loads(row["verification_json"]),
            routing=json.loads(row["routing_json"]), status=row["status"],
            failure_reason=row["failure_reason"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            preread=json.loads(row["preread_json"] or "[]"),
        )
        old = old_worker_prompt(goal, task, check_budget=3)
        new_full = dispatch.worker_prompt(goal, task, check_budget=3)
        new_brief = dispatch.worker_prompt(
            goal, task, check_budget=3, contract="brief")
        new_cli = dispatch.worker_prompt(
            goal, task, check_budget=3,
            delivery_channel=DELIVERY_CHANNEL_MESSAGE)
        archived_path = (
            PROJECT_ROOT / ".orx" / "runs" / run_row["id"] / "assignments"
            / f"{task.task_id}.md")
        archived = (
            archived_path.read_text() if archived_path.exists() else None)
        entries.append({
            "task": task.task_id,
            "checks": len(task.verification),
            "bytes_old": len(old.encode()),
            "bytes_new_full": len(new_full.encode()),
            "bytes_new_cli_message": len(new_cli.encode()),
            "bytes_new_brief_host_subagent": len(new_brief.encode()),
            "bytes_archived_dispatched": (
                len(archived.encode()) if archived is not None else None),
            "tokens_estimate_old": len(old.encode()) // 4,
            "tokens_estimate_new_full": len(new_full.encode()) // 4,
            "tokens_estimate_new_brief": len(new_brief.encode()) // 4,
        })
        totals["old"] += len(old.encode())
        totals["new_full"] += len(new_full.encode())
        totals["new_brief"] += len(new_brief.encode())
        if archived is not None:
            totals["archived"] += len(archived.encode())

    fixed_scaffolding = {
        "agent_definition_bytes": (
            PROJECT_ROOT / "agents" / "orx-worker.md").stat().st_size,
        "note": (
            "the agent definition rides the subagent's context via the"
            " harness; it is the reason the brief form can delegate the"
            " four clauses instead of re-sending them"),
    }

    report = {
        "kind": "g009-t011-static-context-measurement",
        "method": (
            "controlled A/B on identical inputs (same task row + goal from"
            " the live R009 state, empty failure/replan blocks): the old"
            " renderer quoted from the parent commit vs the T011 renderer"
            " in full and brief contract forms. Archived assignment files"
            " are reported as raw dispatched facts (they carry nonce +"
            " failure context). Token estimates are bytes/4."),
        "run": run_row["id"],
        "tasks": entries,
        "totals_bytes": totals,
        "savings": {
            "host_subagent_per_task_bytes": totals["old"] - totals["new_brief"],
            "host_subagent_per_task_percent": round(
                100 * (totals["old"] - totals["new_brief"]) / totals["old"], 1),
            "cli_or_self_per_task_bytes": totals["old"] - totals["new_full"],
            "cli_or_self_per_task_percent": round(
                100 * (totals["old"] - totals["new_full"]) / totals["old"], 1),
        },
        "fixed_context_facts": fixed_scaffolding,
        "limitations": [
            "bytes/4 is a token heuristic, not a tokenizer measurement",
            "harness tool schemas and system scaffolding dominate worker"
            " first-input size (incident: median 72,245 tokens) and are not"
            " ORX-generated — not measured here",
            "the archived sizes include nonce and failure context; they are"
            " raw facts, not the controlled delta",
        ],
    }
    out_path = Path(__file__).parent / "static-measurement.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")

    print(f"run {run_row['id']}: {len(entries)} tasks")
    for entry in entries:
        print(
            f"  {entry['task']}: old {entry['bytes_old']}B"
            f" -> full {entry['bytes_new_full']}B"
            f" / brief {entry['bytes_new_brief_host_subagent']}B"
            f" (checks: {entry['checks']};"
            f" archived dispatched: {entry['bytes_archived_dispatched']}B)")
    print(f"totals: {totals}")
    print(f"savings: {report['savings']}")
    print(f"written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
