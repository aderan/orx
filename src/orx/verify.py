"""Verification: deterministic shell checks and agent verdicts.

- ``shell command...`` entries run through the runtime from the project root,
  with timeout and denylist. Their exit code decides pass/fail.
- ``agent:`` / ``agent[vision]:`` entries are never run as shell. A host Agent
  verifier submits its verdict through `orx verify submit`, which routes a
  verifier profile (vision entries require the vision capability).
- A task passes only when every entry passed. An empty list passes on the
  completion claim alone (there is nothing deterministic to run).
"""

from __future__ import annotations

from pathlib import Path

from orx import machine, plan, runtime
from orx.records import TaskStatus
from orx.state import Store, TaskRow, Verification


def entries_for(task: TaskRow) -> list[plan.VerificationItem]:
    items = []
    for raw in task.verification:
        items.append(plan.parse_verification_entry(raw))
    return items


def _log_path(project_root: Path, run_id: str, task_id: str, index: int, kind: str) -> Path:
    directory = project_root / ".orx" / "runs" / run_id / "verify" / task_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{index:02d}-{kind}.log"


def run_command_verifications(
    store: Store,
    project_root: Path,
    run_id: str,
    revision_row_id: int,
    task: TaskRow,
    timeout: int,
    attempt_id: int | None = None,
) -> list[Verification]:
    """Execute any command entry that does not have a recorded result yet."""
    recorded: list[Verification] = []
    existing = {
        v.command for v in store.verifications_for(revision_row_id, task.task_id)
        if v.kind == "command"
    }
    for index, item in enumerate(entries_for(task), start=1):
        if item.kind != "command" or item.raw in existing:
            continue
        denial = runtime.forbidden_command(item.spec)
        if denial is not None:
            log = _log_path(project_root, run_id, task.task_id, index, "command")
            log.write_text(
                f"denied by verification denylist: {denial}\ncommand: {item.spec}\n"
            )
            recorded.append(
                store.verification_add(
                    revision_row_id, task.task_id, "command", item.raw,
                    passed=False, attempt_id=attempt_id, exit_code=None,
                    output_path=str(log.relative_to(project_root)),
                    required_capabilities=[],
                )
            )
            continue
        result = runtime.run_shell(item.spec, cwd=project_root, timeout=timeout)
        log = _log_path(project_root, run_id, task.task_id, index, "command")
        header = (
            f"$ {item.spec}\n[exit {'timeout' if result.timed_out else result.exit_code}"
            f" in {result.duration_sec:.2f}s]\n"
        )
        log.write_text(header + result.stdout + ("\n[stderr]\n" + result.stderr if result.stderr else ""))
        recorded.append(
            store.verification_add(
                revision_row_id, task.task_id, "command", item.raw,
                passed=result.ok, attempt_id=attempt_id, exit_code=result.exit_code,
                output_path=str(log.relative_to(project_root)),
                required_capabilities=[],
            )
        )
    return recorded


def evaluate(store: Store, revision_row_id: int, task: TaskRow) -> str:
    """'passed' | 'failed' | 'pending' (agent verdicts still outstanding)."""
    items = entries_for(task)
    if not items:
        return "passed"
    rows = store.verifications_for(revision_row_id, task.task_id)
    for item in items:
        matches = [v for v in rows if v.command == item.raw and v.kind == item.kind]
        if not matches:
            return "pending"
        if any(not v.passed for v in matches):
            return "failed"
    return "passed"


def pending_agent_entries(store: Store, revision_row_id: int, task: TaskRow) -> list[plan.VerificationItem]:
    rows = store.verifications_for(revision_row_id, task.task_id)
    pending = []
    for item in entries_for(task):
        if item.kind != "agent":
            continue
        if not any(v.command == item.raw and v.kind == "agent" for v in rows):
            pending.append(item)
    return pending


def first_failure(store: Store, revision_row_id: int, task: TaskRow) -> str | None:
    rows = store.verifications_for(revision_row_id, task.task_id)
    for v in rows:
        if not v.passed:
            return v.command
    return None


def apply_verdict(
    store: Store, revision_row_id: int, task: TaskRow, failure_hint: str | None = None
) -> str:
    """Move a `verifying` task to passed/failed per the recorded results.

    Returns the new status ('verifying' when verdicts are still outstanding).
    """
    verdict = evaluate(store, revision_row_id, task)
    if verdict == "passed":
        machine.transition(store, revision_row_id, task.task_id, "verify_pass", TaskStatus.PASSED)
    elif verdict == "failed":
        reason = failure_hint or first_failure(store, revision_row_id, task) or "verification failed"
        machine.transition(
            store, revision_row_id, task.task_id, "verify_fail", TaskStatus.FAILED,
            reason=f"verification failed: {reason}",
            failure_reason=f"verification failed: {reason}",
        )
    return verdict
