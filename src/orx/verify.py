"""Verification: deterministic shell checks and agent verdicts.

- ``shell command...`` entries run through the runtime from the project root,
  with timeout and denylist. Their exit code decides pass/fail.
- ``agent:`` / ``agent[vision]:`` entries are never run as shell. A host Agent
  verifier submits its verdict through `orx verify submit`, which routes a
  verifier profile (vision entries require the vision capability).
- A task passes only when every entry passed. An empty list passes on the
  completion claim alone (there is nothing deterministic to run).
- ``run_task_check`` is the same-session worker self-check: it executes the
  command entries immediately and appends the results, but never judges —
  no task status transitions, and agent entries are never run.
- The delivery contract (R002 follow-up): `task complete` accepts only a
  structured evidence document (``load_delivery_evidence`` validates the
  schema), and a completion claiming success must pass the delivery gate —
  every command entry re-run fresh through ``run_task_check`` for the
  completing attempt. Agent entries and the independent verifier are
  outside the gate: a green gate never replaces them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from orx import machine, plan, runtime
from orx.records import ORXError, TaskStatus
from orx.state import Store, TaskRow, Verification


def entries_for(task: TaskRow) -> list[plan.VerificationItem]:
    items = []
    for raw in task.verification:
        items.append(plan.parse_verification_entry(raw))
    return items


def _log_path(project_root: Path, run_id: str, task_id: str, index: int, kind: str,
              attempt_id: int | None = None) -> Path:
    """Log path for a recorded (verify-time) command row. The attempt number
    is part of the filename, so the same entry's log from an earlier attempt
    is never overwritten by a later round — retries keep every round's output
    side by side. ``a000`` marks a row no attempt was bound to."""
    directory = project_root / ".orx" / "runs" / run_id / "verify" / task_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{index:02d}-a{attempt_id if attempt_id is not None else 0:03d}-{kind}.log"


def _check_log_path(project_root: Path, run_id: str, task_id: str, index: int,
                    seq: int) -> Path:
    """Logs for `orx task check` runs. A separate `check/` tree with a per-run
    sequence number: every call appends fresh rows, so the log of an earlier
    row is never overwritten by a later check of the same entry. The sequence
    counts across ALL revisions of the run (the caller passes it): task ids
    are per-revision identities, but the log directory is shared per
    (run, task id) — continuing the sequence across revisions is what keeps a
    same-numbered task in a new revision from overwriting the old revision's
    log while every already-written path stays valid and readable."""
    directory = project_root / ".orx" / "runs" / run_id / "check" / task_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{index:02d}-{seq:04d}-command.log"


def run_command_verifications(
    store: Store,
    project_root: Path,
    run_id: str,
    revision_row_id: int,
    task: TaskRow,
    timeout: int,
    attempt_id: int | None = None,
) -> list[Verification]:
    """Execute any command entry that does not have a recorded result yet.

    "Recorded" is the current attempt window (`verifications_current`): rows
    from earlier attempts are history and do not suppress a re-run, while a
    row recorded in the current round (a same-session `task check`, or a
    gate that already ran the entry) still does."""
    recorded: list[Verification] = []
    existing = {
        v.command for v in store.verifications_current(revision_row_id, task.task_id)
        if v.kind == "command"
    }
    for index, item in enumerate(entries_for(task), start=1):
        if item.kind != "command" or item.raw in existing:
            continue
        denial = runtime.forbidden_command(item.spec)
        if denial is not None:
            log = _log_path(project_root, run_id, task.task_id, index, "command",
                            attempt_id)
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
        log = _log_path(project_root, run_id, task.task_id, index, "command",
                        attempt_id)
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


# Earliest output line that reads like a failure. Deliberately a heuristic
# summary pointer, not a parser: it exists so a worker sees the first red
# line (pytest tracebacks, compiler errors, missing files) without opening
# the full log.
_FAILURE_LINE = re.compile(
    r"failed|failure|failures|error|traceback|assert|exception|fatal"
    r"|no such file|not found|permission denied|aborted|killed",
    re.IGNORECASE,
)
_ERROR_SUMMARY_MAX = 200


def error_summary(stdout: str, stderr: str) -> str | None:
    """The earliest line of a command's output that looks like a failure.

    Stdout is scanned first (pytest and compilers report on stdout), then
    stderr. When no line carries a failure marker, the first non-empty
    stderr line, then stdout line, stands in; empty output has no summary.
    """
    def first_marker(text: str) -> str | None:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and _FAILURE_LINE.search(stripped):
                return stripped
        return None

    found = first_marker(stdout) or first_marker(stderr)
    if found:
        return found[:_ERROR_SUMMARY_MAX]
    for text in (stderr, stdout):
        for line in text.splitlines():
            if line.strip():
                return line.strip()[:_ERROR_SUMMARY_MAX]
    return None


def run_task_check(
    store: Store,
    project_root: Path,
    run_id: str,
    revision_row_id: int,
    task: TaskRow,
    timeout: int,
    attempt_id: int | None = None,
) -> dict:
    """Worker self-check: execute the task's command entries NOW and record
    one verification row per entry.

    Recording semantics are identical to `run_command_verifications` (same
    denylist, same timeout via the shared runtime, same log layout, rows
    written through `verification_add` with the caller's `attempt_id`), but
    nothing here judges: no status transition, no verdict. Every call runs
    every command entry fresh and appends new rows — a check/fix/check loop
    accumulates history instead of skipping already-recorded entries. Agent
    entries are never executed and are only counted. Returns a structured
    report: per-entry command/exit_code/passed/error summary/log path plus
    summary counts, ready for a delivery gate built on this runner.
    """
    items = entries_for(task)
    command_items = [
        (index, item) for index, item in enumerate(items, start=1)
        if item.kind == "command"
    ]
    agent_entries = len(items) - len(command_items)
    results: list[dict] = []
    # The per-(run, task id) log sequence counts command rows across ALL
    # revisions of the run, not just this one: a replan that reuses a task
    # number starts a new revision with a zero per-revision count, and a
    # per-revision sequence would restart the filenames and overwrite the
    # earlier revision's logs. Counting across revisions keeps every
    # already-written log path valid (old rows stay readable) while new
    # revisions continue the sequence into fresh files.
    seq = sum(
        1 for v in store.verifications_for_task_in_run(run_id, task.task_id)
        if v.kind == "command"
    )
    for index, item in command_items:
        denial = runtime.forbidden_command(item.spec)
        if denial is not None:
            log = _check_log_path(project_root, run_id, task.task_id, index, seq)
            log.write_text(
                f"denied by verification denylist: {denial}\ncommand: {item.spec}\n"
            )
            row = store.verification_add(
                revision_row_id, task.task_id, "command", item.raw,
                passed=False, attempt_id=attempt_id, exit_code=None,
                output_path=str(log.relative_to(project_root)),
                required_capabilities=[],
            )
            seq += 1
            results.append({
                "index": index,
                "command": item.raw,
                "passed": False,
                "exit_code": None,
                "denied": True,
                "denial_reason": denial,
                "timed_out": False,
                "duration_sec": None,
                "error_summary": f"denied by verification denylist: {denial}",
                "log_path": row.output_path,
                "attempt_id": row.attempt_id,
                "verification_id": row.id,
            })
            continue
        result = runtime.run_shell(item.spec, cwd=project_root, timeout=timeout)
        log = _check_log_path(project_root, run_id, task.task_id, index, seq)
        header = (
            f"$ {item.spec}\n[exit {'timeout' if result.timed_out else result.exit_code}"
            f" in {result.duration_sec:.2f}s]\n"
        )
        log.write_text(
            header + result.stdout
            + ("\n[stderr]\n" + result.stderr if result.stderr else "")
        )
        row = store.verification_add(
            revision_row_id, task.task_id, "command", item.raw,
            passed=result.ok, attempt_id=attempt_id, exit_code=result.exit_code,
            output_path=str(log.relative_to(project_root)),
            required_capabilities=[],
        )
        seq += 1
        results.append({
            "index": index,
            "command": item.raw,
            "passed": result.ok,
            "exit_code": result.exit_code,
            "denied": False,
            "denial_reason": None,
            "timed_out": result.timed_out,
            "duration_sec": round(result.duration_sec, 3),
            "error_summary": (
                None if result.ok
                else error_summary(result.stdout, result.stderr)
            ),
            "log_path": row.output_path,
            "attempt_id": row.attempt_id,
            "verification_id": row.id,
        })
    summary = {
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "denied": sum(1 for r in results if r["denied"]),
    }
    summary["failed"] = summary["total"] - summary["passed"] - summary["denied"]
    return {
        "task": task.task_id,
        "results": results,
        "summary": summary,
        "agent_entries_not_run": agent_entries,
    }


# ---------------------------------------------------------------------------
# Structured delivery evidence (R002 follow-up: the completion contract).
#
# `orx task complete` used to accept any file at the evidence path. Now the
# file is the delivery result and must say what it is: status
# passed|failed|blocked, the checks the worker ran, the artifacts produced,
# and a summary. The schema is an additive superset of the legacy
# {summary, commands, artifacts} shape: those keys stay legal (commands is
# ignored — the gate re-runs checks itself and never trusts reported exit
# codes for a success claim), while status and checks are required. A
# missing or malformed field is rejected by name.

DELIVERY_STATUSES = ("passed", "failed", "blocked")

_CHECK_FIELDS = "command/exit_code/log"


class DeliveryRejected(ORXError):
    """`task complete` refused the delivery; nothing was recorded.

    Two refusal kinds share this class so the CLI renders both with full
    detail. ``fields`` carries the per-field evidence problems for a schema
    rejection (kind ``evidence``); ``report`` carries the delivery-gate
    report for a red gate (kind ``gate``) — per-check failure rows with
    command, exit code, error summary, and log path. Either way the task
    keeps its prior status and the attempt stays open.
    """

    def __init__(self, message: str, *, kind: str,
                 fields: list[str] | None = None,
                 report: dict | None = None):
        self.kind = kind
        self.fields = list(fields or [])
        self.report = report
        super().__init__(message)


def _describe_type(value) -> str:
    if isinstance(value, bool):
        return "true/false"
    names = {dict: "object", list: "list", str: "string", int: "integer",
             float: "number", type(None): "null"}
    return names.get(type(value), type(value).__name__)


def _validate_check_item(item, index: int, errors: list[str]) -> None:
    if not isinstance(item, dict):
        errors.append(
            f"field 'checks[{index}]' must be an object with {_CHECK_FIELDS},"
            f" got {_describe_type(item)}"
        )
        return
    command = item.get("command")
    if not isinstance(command, str) or not command.strip():
        errors.append(
            f"field 'checks[{index}].command' is missing or not a non-empty"
            " string (the check you ran)"
        )
    if "exit_code" not in item:
        errors.append(
            f"field 'checks[{index}].exit_code' is missing (an integer, or"
            " null when the check could not run)"
        )
    else:
        code = item["exit_code"]
        if code is not None and (isinstance(code, bool) or not isinstance(code, int)):
            errors.append(
                f"field 'checks[{index}].exit_code' must be an integer or"
                f" null, got {_describe_type(code)}"
            )
    log = item.get("log")
    if log is not None and (not isinstance(log, str) or not log.strip()):
        errors.append(
            f"field 'checks[{index}].log' must be a non-empty path string or"
            f" null (no log exists when the check never ran), got {_describe_type(log)}"
        )


def load_delivery_evidence(path: Path) -> tuple[dict | None, list[str]]:
    """Read a completion evidence file and validate the delivery schema.

    Returns ``(data, errors)``: ``data`` is the parsed JSON object (also
    returned when field-level problems exist, for caller context) or None
    when the file is unreadable, not JSON, or not an object; ``errors`` has
    one entry per missing or malformed field, naming the exact field. The
    caller rejects the delivery when ``errors`` is non-empty — the task and
    its attempt are untouched by this function.
    """
    errors: list[str] = []
    try:
        data = json.loads(path.read_text())
    except OSError as exc:
        return None, [f"cannot read evidence file: {exc}"]
    except json.JSONDecodeError as exc:
        return None, [f"evidence is not valid JSON: {exc}"]
    if not isinstance(data, dict):
        return None, [
            "evidence must be a JSON object with the delivery fields"
            f" status/checks/artifacts/summary, got {_describe_type(data)}"
        ]

    status = data.get("status")
    if "status" not in data:
        errors.append(
            "field 'status' is missing (the delivery result: one of"
            f" {' | '.join(DELIVERY_STATUSES)})"
        )
    elif not isinstance(status, str) or status not in DELIVERY_STATUSES:
        errors.append(
            "field 'status' must be one of"
            f" {' | '.join(DELIVERY_STATUSES)}, got {status!r}"
        )

    summary = data.get("summary")
    if "summary" not in data:
        errors.append(
            "field 'summary' is missing (a non-empty string: what was"
            " delivered, or why not)"
        )
    elif not isinstance(summary, str) or not summary.strip():
        errors.append(
            f"field 'summary' must be a non-empty string, got {_describe_type(summary)}"
        )

    if "checks" not in data:
        errors.append(
            "field 'checks' is missing (a list — one"
            f" {{{_CHECK_FIELDS}}} object per command check you ran;"
            " [] when none could run)"
        )
    elif not isinstance(data["checks"], list):
        errors.append(
            f"field 'checks' must be a list, got {_describe_type(data['checks'])}"
        )
    else:
        for index, item in enumerate(data["checks"]):
            _validate_check_item(item, index, errors)

    if "artifacts" not in data:
        errors.append(
            "field 'artifacts' is missing (a list of produced file paths;"
            " [] when none)"
        )
    elif not isinstance(data["artifacts"], list):
        errors.append(
            f"field 'artifacts' must be a list, got {_describe_type(data['artifacts'])}"
        )
    else:
        for index, item in enumerate(data["artifacts"]):
            if not isinstance(item, str):
                errors.append(
                    f"field 'artifacts[{index}]' must be a path string,"
                    f" got {_describe_type(item)}"
                )

    return data, errors


def gate_failures(report: dict) -> list[dict]:
    """The red rows of a delivery-gate run (failed or denied entries)."""
    return [row for row in report.get("results", []) if not row["passed"]]


def gate_failure_reason(report: dict, limit: int = 3) -> str:
    """One line describing every red row of a delivery-gate run.

    Goes into task events and failure reasons verbatim, so it carries the
    per-check command, exit code (or timeout/denial), the earliest failing
    output line, and the log path — everything a retry needs to go straight
    at the problem.
    """
    failures = gate_failures(report)
    total = report.get("summary", {}).get("total", len(report.get("results", [])))
    if not failures:
        return f"delivery gate green ({total} command check(s))"
    parts: list[str] = []
    for row in failures[:limit]:
        if row.get("denied"):
            parts.append(f"{row['command']} (denied: {row['denial_reason']})")
            continue
        exit_note = "timeout" if row.get("timed_out") else f"exit {row['exit_code']}"
        detail = f"{row['command']} ({exit_note}"
        if row.get("error_summary"):
            detail += f": {row['error_summary']}"
        if row.get("log_path"):
            detail += f"; log {row['log_path']}"
        parts.append(detail + ")")
    more = len(failures) - len(parts)
    if more > 0:
        parts.append(f"+{more} more")
    return (
        f"delivery gate: {len(failures)} of {total} command check(s) red: "
        + " | ".join(parts)
    )


def reported_check_detail(delivery: dict, limit: int = 3) -> str:
    """Compact detail of the checks a failed/blocked delivery reports.

    These exit codes are the worker's report, not ORX's own run: there is
    no success claim, so the gate does not re-run them. Only red or
    never-run entries are quoted (a green self-report proves nothing and
    is skipped).
    """
    parts: list[str] = []
    for item in delivery.get("checks", []):
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        code = item.get("exit_code")
        if code == 0:
            continue
        code_note = "not run" if code is None else f"exit {code}"
        parts.append(f"{command} ({code_note})")
    if not parts:
        return ""
    detail = " | ".join(parts[:limit])
    if len(parts) > limit:
        detail += f" | +{len(parts) - limit} more"
    return f"reported checks: {detail}"


def evaluate(store: Store, revision_row_id: int, task: TaskRow) -> str:
    """'passed' | 'failed' | 'pending' (agent verdicts still outstanding).

    The task's CURRENT verdict reads only the current attempt window
    (`verifications_current`): rows from earlier attempts are history and
    never fail a retried task. Within the window, each entry is judged by
    its LATEST recorded row: `orx task check` may append several rows for
    the same entry within one attempt (check -> fix -> check), and the
    freshest row is the current state of the workspace. With at most one
    row per entry in the window this is exactly the previous
    any-row-failed semantics."""
    items = entries_for(task)
    if not items:
        return "passed"
    rows = store.verifications_current(revision_row_id, task.task_id)
    for item in items:
        matches = [v for v in rows if v.command == item.raw and v.kind == item.kind]
        if not matches:
            return "pending"
        if not matches[-1].passed:  # id-ordered rows; the last is the latest
            return "failed"
    return "passed"


def pending_agent_entries(store: Store, revision_row_id: int, task: TaskRow) -> list[plan.VerificationItem]:
    rows = store.verifications_current(revision_row_id, task.task_id)
    pending = []
    for item in entries_for(task):
        if item.kind != "agent":
            continue
        if not any(v.command == item.raw and v.kind == "agent" for v in rows):
            pending.append(item)
    return pending


def first_failure(store: Store, revision_row_id: int, task: TaskRow) -> str | None:
    """The first entry whose LATEST current row failed, in row order.

    Rows superseded by a later same-session `task check` re-run are not
    failure hints — the fresh row already says whether that problem still
    exists — and rows from earlier attempts are not hints either: a retry
    re-runs the entry in the new round before any verdict is read."""
    rows = store.verifications_current(revision_row_id, task.task_id)
    latest: dict[tuple[str, str], int] = {}
    for v in rows:
        latest[(v.kind, v.command)] = v.id  # id order: the last write wins
    for v in rows:
        if latest[(v.kind, v.command)] == v.id and not v.passed:
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
