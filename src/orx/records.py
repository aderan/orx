"""Domain enums and shared exceptions.

Static profile configuration lives in TOML; temporary runtime state lives in
SQLite. Nothing in this module reads either store.
"""

from __future__ import annotations

from enum import Enum


class ORXError(Exception):
    """Base class for all ORX domain errors."""


class ConfigError(ORXError):
    """config.toml / profiles.toml failed validation. Carries every message."""

    def __init__(self, messages: list[str] | None = None, message: str | None = None):
        self.messages = list(messages or [])
        super().__init__(message or "; ".join(self.messages) or "invalid configuration")


class MigrationError(ORXError):
    """SQLite schema version is missing, unknown, or newer than the code."""


class NotFoundError(ORXError):
    """A referenced entity (goal, task, assignment, ...) does not exist."""


class ConflictError(ORXError):
    """A compare-and-set style update lost (e.g. two hosts claimed one task)."""


class TransitionError(ORXError):
    """An illegal task state transition was requested."""


class PlanSyntaxError(ORXError):
    """Plan IR is not parseable as the IR shape. Carries per-field messages."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("invalid plan IR: " + "; ".join(self.errors))


class PlanValidationError(ORXError):
    """Plan IR parses but violates plan semantics. Carries every error."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("plan rejected: " + "; ".join(self.errors))


class RoutingError(ORXError):
    """No usable profile for a request (or a pinned profile is unusable)."""


class ReplanRejectedError(ORXError):
    """Replan attempted while tasks are running or verifying."""


class Role(str, Enum):
    CONTROLLER = "controller"
    PLANNER = "planner"
    WORKER = "worker"
    VERIFIER = "verifier"


class Driver(str, Enum):
    HOST = "host"
    CLI = "cli"
    EXTERNAL = "external"


class Harness(str, Enum):
    ZCODE = "zcode"
    CODEX = "codex"
    CURSOR = "cursor"
    SHELL = "shell"


class ModelClass(str, Enum):
    FRONTIER = "frontier"
    STRONG = "strong"
    ECONOMY = "economy"


class Effort(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class PlanDepth(str, Enum):
    LIGHT = "light"
    STANDARD = "standard"
    DEEP = "deep"


class ReplanClassification(str, Enum):
    """How a task in a replan revision relates to prior-revision work (G004).

    Task numbers never identify work across revisions — a same-numbered task
    in a different revision is DIFFERENT work. The explicit replan mapping in
    the plan IR is the only correspondence, and nothing ever inherits a prior
    passed status into the new plan.
    """

    NEW = "new"            # no prior correspondence; sources must be empty
    CONFIRM = "confirm"    # prior passed work relied on as-is; current verification mandatory
    REDO = "redo"          # prior work done again; redo_reason mandatory
    CONTINUE = "continue"  # prior unfinished work carried forward


class SupersededDisposition(str, Enum):
    """Where a prior-revision task's work goes in the new plan (G004)."""

    CONFIRMED = "confirmed"    # carried intact; confirmed by a successor
    CONTINUED = "continued"    # unfinished work continues in a successor
    REDONE = "redone"          # done again by a successor (reason lives there)
    SPLIT = "split"            # distributed over two or more successors
    MERGED = "merged"          # combined with other prior tasks into one successor
    DROPPED = "dropped"        # deliberately abandoned; note mandatory


class GoalStatus(str, Enum):
    ACTIVE = "active"
    DONE = "done"
    CANCELLED = "cancelled"


class RunStatus(str, Enum):
    PLANNING = "planning"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"


class RevisionStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class AssignmentStatus(str, Enum):
    WAITING_HOST = "waiting_host"
    SUBMITTED = "submitted"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNABLE = "runnable"
    RUNNING = "running"
    WAITING_HOST = "waiting_host"
    WAITING_EXTERNAL = "waiting_external"
    VERIFYING = "verifying"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class ErrorKind(str, Enum):
    """Adapter failure taxonomy (M1 P4). Classification drives health
    transitions; it never guesses a retry in-process (M2)."""

    AUTH_REQUIRED = "auth_required"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    TEMPORARY_FAILURE = "temporary_failure"
    MODEL_UNAVAILABLE = "model_unavailable"
    CONTEXT_EXCEEDED = "context_exceeded"
    INVALID_REQUEST = "invalid_request"
    PROCESS_FAILURE = "process_failure"
    CANCELLED = "cancelled"


class ResourceStatus(str, Enum):
    ABUNDANT = "abundant"
    AVAILABLE = "available"
    CONSTRAINED = "constrained"
    EXHAUSTED = "exhausted"
    UNAVAILABLE = "unavailable"
    COOLDOWN = "cooldown"
    AUTH_REQUIRED = "auth_required"
    UNKNOWN = "unknown"


# Terminal means "this task will never execute again without an explicit
# user-driven action (retry) or revision supersession".
TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.PASSED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)
UNFINISHED_TASK_STATUSES: frozenset[TaskStatus] = frozenset(TaskStatus) - TERMINAL_TASK_STATUSES

# Statuses that keep a Run in the `running` aggregate state.
ACTIVE_EXECUTION_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.RUNNABLE,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_HOST,
        TaskStatus.WAITING_EXTERNAL,
        TaskStatus.VERIFYING,
    }
)

# Resource statuses that never take part in routing.
NON_ROUTABLE_RESOURCE_STATUSES: frozenset[ResourceStatus] = frozenset(
    {ResourceStatus.UNAVAILABLE, ResourceStatus.EXHAUSTED, ResourceStatus.AUTH_REQUIRED}
)
# Preferred resource statuses, in configured list order.
PREFERRED_RESOURCE_STATUSES: frozenset[ResourceStatus] = frozenset(
    {ResourceStatus.ABUNDANT, ResourceStatus.AVAILABLE, ResourceStatus.UNKNOWN}
)
LAST_RESORT_RESOURCE_STATUS = ResourceStatus.CONSTRAINED


def parse_enum(enum_cls: type[Enum], value: str) -> Enum | None:
    """Return the member whose value equals `value`, or None."""
    try:
        return enum_cls(value)
    except ValueError:
        return None
