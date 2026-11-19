"""Display-layer contract: status mapping, identity keys, data health.

docs/menubar-app-design.md §2/§4 frozen rules:

- Execution status maps from ORX facts; the raw value is always preserved
  and an unrecognized value displays 未知状态 — never guessed into a
  terminal state.
- Identity keys are composite. A short number (T001/R001) is never a
  global key: Run = Project + run_id, Task = Project + Run + Revision +
  task_id, Attempt = Project + the source database's attempt row id.
- Data health (正常/更新延迟/来源不可读/版本不支持) is a third dimension,
  separate from execution status and attention by construction.
- Lifecycle layering (G011, docs/diagnostics/g011/lifecycle-matrix.md):
  the Goal control state (goals.status) and the Run execution facts
  (runs.status) are two explicit layers. ``lifecycle_view`` is the single
  classification point — it combines both into overview grouping and
  attention eligibility WITHOUT rewriting either raw status. Run blocked
  is 受阻 (execution obstructed); Goal paused is 已暂停 (control-level
  pause): the vocabularies never mix.
"""

from __future__ import annotations

from pathlib import Path

UNKNOWN_LABEL = "未知状态"

RUN_DISPLAY: dict[str, str] = {
    "planning": "规划中",
    "running": "执行中",
    "blocked": "受阻",
    "done": "完成",
}

TASK_DISPLAY: dict[str, str] = {
    "pending": "等待前置",
    "runnable": "待执行",
    "running": "执行中",
    "waiting_host": "等待宿主",
    "waiting_external": "等待外部",
    "verifying": "等待验证",
    "passed": "完成",
    "failed": "失败",
    "blocked": "受阻",
    "cancelled": "已取消",
}

GOAL_DISPLAY: dict[str, str] = {
    "active": "进行中",
    "paused": "已暂停",
    "cancelling": "取消中",
    "done": "已完成",
    "cancelled": "已取消",
}

_TERMINAL_DISPLAY = {"完成", "失败", "已取消"}

HEALTH_OK = "正常"
HEALTH_DELAYED = "更新延迟"
HEALTH_UNREADABLE = "来源不可读"
HEALTH_UNSUPPORTED = "版本不支持"
HEALTH_UNKNOWN = "未知"


def _status(raw, table: dict[str, str]) -> dict:
    known = raw in table
    return {
        "raw": raw,
        "known": known,
        "display": table[raw] if known else UNKNOWN_LABEL,
    }


def run_status(raw: str | None) -> dict:
    return _status(raw, RUN_DISPLAY)


def task_status(raw: str | None) -> dict:
    return _status(raw, TASK_DISPLAY)


def goal_status(raw: str | None) -> dict:
    return _status(raw, GOAL_DISPLAY)


def is_terminal(status: dict) -> bool:
    """True only for a KNOWN terminal display (完成/失败/已取消). An
    unknown value is never terminal — it is not a verdict."""
    return status["known"] and status["display"] in _TERMINAL_DISPLAY


# ------------------------------------------------------ lifecycle layering ---

#: Goal control states that end lifecycle visibility in the CURRENT lists
#: (matrix §2): a cancelled Goal is history-only — never current active,
#: never attention, never a notification.
GOAL_CANCELLED = "cancelled"


def lifecycle_view(goal_status: dict | None,
                   run_status: dict) -> dict:
    """Layered lifecycle classification of one run (G011 matrix).

    Inputs are display status BLOCKS (``goal_status`` may be ``None`` when
    there is no Goal row at all — the control layer is simply absent).
    The function never mutates either block; it only decides how the two
    layers combine:

    - ``bucket``: ``"active"`` (进行中), ``"recent_completed"`` (最近完成)
      or ``"historical"`` (query-only — never in the panel's current
      lists, fully queryable through runs.list / runs.get / tasks.get).
    - ``attention``: whether execution facts of this run may feed the
      attention list (blocked runs, current failed tasks). False only for
      a KNOWN cancelled Goal: its failures are history, not alerts.
    - ``goal_control``: ``active|paused|cancelling|cancelled|done|
      "unknown"`` — or ``None`` when no Goal row exists.

    Rules (docs/diagnostics/g011/lifecycle-matrix.md §2):

    - A KNOWN cancelled Goal is historical under EVERY execution fact —
      a cancel never destroys history, it only ends current visibility.
    - Otherwise a NON-terminal run status groups into the active bucket —
      a paused Goal does not starve in-flight executions and a cancelling
      Goal keeps its not-yet-closed executions visible (the wrap-up is
      pending), each with its control badge.
    - A terminal run status reaches recent_completed only when the
      control layer is not cancelling/cancelled: a completion racing a
      cancel keeps its facts but never aggregates as a completion, and a
      run finishing DURING a pause stays recent while its Goal control
      layer remains paused (it never masquerades as a Goal completion).
    - Unknown values (either layer) are never guessed terminal: grouping
      falls back to the execution facts, with the control layer unknown.
    """
    if goal_status is not None and goal_status["known"]:
        control = goal_status["raw"]
    elif goal_status is not None:
        control = "unknown"
    else:
        control = None
    if control == GOAL_CANCELLED:
        bucket = "historical"
    elif not is_terminal(run_status):
        bucket = "active"
    elif run_status["raw"] == "done" and control != "cancelling":
        bucket = "recent_completed"
    else:
        bucket = "historical"
    return {
        "bucket": bucket,
        "attention": control != GOAL_CANCELLED,
        "goal_control": control,
    }


def project_key(root: str | Path) -> str:
    """Canonical project identity: resolved absolute path, the exact
    transformation registry.add_project uses."""
    return str(Path(root).expanduser().resolve())


def run_identity(project: str, run_id: str) -> tuple[str, str]:
    """Compose identity keys from an already-canonical project key (see
    project_key). Pure composition — resolution is the caller's one job."""
    return (project, run_id)


def task_identity(project: str, run_id: str, revision_id: int,
                  task_id: str) -> tuple[str, int, int, str]:
    return (project, run_id, revision_id, task_id)


def attempt_identity(project: str, attempt_row_id: int) -> tuple[str, int]:
    return (project, attempt_row_id)


def data_health(source_readable: bool | None, schema_supported: bool | None,
                stale: bool | None) -> dict:
    """Classify data health from three facts. None means the fact is
    unknown — health then reports 未知, never a guessed 正常.
    Precedence: unreadable > unsupported > delayed > ok."""
    if source_readable is False:
        health = HEALTH_UNREADABLE
    elif schema_supported is False:
        health = HEALTH_UNSUPPORTED
    elif stale is True:
        health = HEALTH_DELAYED
    elif source_readable is None or schema_supported is None or stale is None:
        health = HEALTH_UNKNOWN
    else:
        health = HEALTH_OK
    return {
        "health": health,
        "source_readable": source_readable,
        "schema_supported": schema_supported,
        "stale": stale,
    }
