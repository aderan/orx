"""Plan IR: parse, validate, and depth policy.

The IR JSON document is the authoritative plan artifact. Markdown rendering is
derived and disposable. Unknown extra fields are ignored on parse so a host can
round-trip notes, but they are not persisted as authority.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orx import records
from orx.records import (
    PlanDepth,
    PlanSyntaxError,
    PlanValidationError,
    ReplanClassification,
    SupersededDisposition,
    TaskStatus,
    TERMINAL_TASK_STATUSES,
    parse_enum,
)

TASK_ID_RE = re.compile(r"^T\d+$")

HIGH_RISK_TOKENS = ("architect", "migration", "migrate", "refactor", "public api", "breaking")
SMALL_SCOPE_TOKENS = ("typo", "rename", "comment", "log line", "one line", "tiny")


class Exploration(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str = ""
    relevant_components: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class Approach(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str = ""
    decisions: list[str] = Field(default_factory=list)


class TaskScope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    allowed: list[str] = Field(default_factory=list)


class TaskRouting(BaseModel):
    model_config = ConfigDict(extra="ignore")
    complexity: str = ""
    required_capabilities: list[str] = Field(default_factory=list)


class PlanTask(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    objective: str = ""
    dependencies: list[str] = Field(default_factory=list)
    scope: TaskScope
    acceptance: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    preread: list[str] = Field(default_factory=list)
    routing: TaskRouting


class ReplanSource(BaseModel):
    """One prior-revision task a replan task corresponds to.

    Identity is the (revision, task_id) pair — never the task number alone:
    the same number in a different revision is different work. ``part=True``
    says the replan task covers only part of the prior task's objective
    (splits and partial correspondences).
    """

    model_config = ConfigDict(extra="ignore")
    revision: int
    task_id: str
    part: bool = False


class ReplanTaskMapping(BaseModel):
    """Work classification of one task in a replan revision (G004 contract).

    ``confirm`` (prior passed work relied on as-is) must list the CURRENT
    verification requirements in ``confirm_verification`` — a prior passed
    status is never a substitute. ``redo`` must carry ``redo_reason``.
    """

    model_config = ConfigDict(extra="ignore")
    task: str
    classification: str
    sources: list[ReplanSource] = Field(default_factory=list)
    redo_reason: str = ""
    confirm_verification: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


class ReplanSuperseded(BaseModel):
    """Declared destination of one prior-revision task (旧任务去向)."""

    model_config = ConfigDict(extra="ignore")
    revision: int
    task_id: str
    disposition: str
    successors: list[str] = Field(default_factory=list)
    note: str = ""


class ReplanMapping(BaseModel):
    """The old<->new correspondence a replan revision must declare.

    Absent (null) on a first plan and on historical IRs. Nothing in ORX
    derives this mapping from task numbers, and no rule inherits a prior
    passed status into the new plan.
    """

    model_config = ConfigDict(extra="ignore")
    prior_revision: int
    tasks: list[ReplanTaskMapping] = Field(default_factory=list)
    superseded: list[ReplanSuperseded] = Field(default_factory=list)


class PlanIR(BaseModel):
    model_config = ConfigDict(extra="ignore")
    goal: str
    exploration: Exploration
    approach: Approach
    tasks: list[PlanTask]
    replan: ReplanMapping | None = None

    def to_dict(self) -> dict:
        # exclude_none keeps first-plan output shaped exactly as before this
        # field existed; a declared mapping round-trips losslessly.
        return self.model_dump(exclude_none=True)


@dataclass(frozen=True)
class VerificationItem:
    raw: str
    kind: str  # "command" | "agent"
    spec: str  # shell command text or agent instruction
    capabilities: tuple[str, ...] = ()


def parse_verification_entry(raw: str) -> VerificationItem:
    """Verification syntax:
      ``shell command...``                      -> deterministic shell check
      ``agent: instruction``                    -> agent verification
      ``agent[vision]: instruction``            -> agent verification requiring vision
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("verification entry must be a non-empty string")
    if raw.startswith("agent["):
        close = raw.find("]")
        colon = raw.find(":")
        if close == -1 or colon != close + 1:
            raise ValueError(f"malformed agent verification (expected 'agent[cap]: ...'): {raw!r}")
        capability = raw[len("agent["):close].strip()
        instruction = raw[colon + 1:].strip()
        if capability != "vision":
            raise ValueError(
                f"unsupported agent capability bracket {capability!r} (M0 understands 'vision')"
            )
        if not instruction:
            raise ValueError("agent verification instruction is empty")
        return VerificationItem(raw=raw, kind="agent", spec=instruction, capabilities=("vision",))
    if raw.startswith("agent"):
        if raw.startswith("agent:"):
            instruction = raw[len("agent:"):].strip()
            if not instruction:
                raise ValueError("agent verification instruction is empty")
            return VerificationItem(raw=raw, kind="agent", spec=instruction)
        raise ValueError(
            f"malformed verification entry {raw!r}: an agent check must be 'agent: ...'"
            " or 'agent[vision]: ...'"
        )
    return VerificationItem(raw=raw.strip(), kind="command", spec=raw.strip())


def parse_ir(data: dict) -> PlanIR:
    try:
        return PlanIR.model_validate(data)
    except ValidationError as exc:
        errors = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "<root>"
            errors.append(f"{loc}: {err['msg']}")
        raise PlanSyntaxError(errors) from None


def is_safe_scope_path(path: str) -> bool:
    """Project-relative path: not absolute, no '.'/'..'/empty segments.
    A trailing slash is allowed (directory shorthand)."""
    if not path or not path.strip():
        return False
    if path.startswith("/") or path.startswith("~"):
        return False
    trimmed = path.rstrip("/")
    if not trimmed:
        return False
    segments = trimmed.split("/")
    return all(seg not in ("", ".", "..") for seg in segments) and "\\" not in path


def find_cycle(tasks: list[PlanTask]) -> list[str] | None:
    """Return one dependency cycle as [a, b, ..., a], or None."""
    graph = {t.id: list(t.dependencies) for t in tasks}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in graph}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for dep in graph.get(node, []):
            if dep not in graph:
                continue  # missing target reported separately
            if color[dep] == GRAY:
                idx = stack.index(dep)
                return stack[idx:] + [dep]
            if color[dep] == WHITE:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for tid in graph:
        if color[tid] == WHITE:
            found = visit(tid)
            if found:
                return found
    return None


def validate_ir(
    ir: PlanIR,
    active_goal_id: str,
    goal_acceptance: list[str],
    known_capabilities: set[str],
) -> list[str]:
    """Full semantic validation. Empty list means the plan is acceptable."""
    errors: list[str] = []

    if ir.goal != active_goal_id:
        errors.append(f"plan goal is {ir.goal!r} but the active Goal is {active_goal_id!r}")

    if not ir.tasks:
        errors.append("plan has no tasks")
        return errors

    seen: set[str] = set()
    for task in ir.tasks:
        if not TASK_ID_RE.fullmatch(task.id):
            errors.append(f"task id {task.id!r} must match T<digits> (e.g. T001)")
        if task.id in seen:
            errors.append(f"duplicate task id {task.id}")
        seen.add(task.id)

    all_ids = {t.id for t in ir.tasks}
    for task in ir.tasks:
        if not task.objective.strip():
            errors.append(f"task {task.id}: objective must be a non-empty string")
        if not task.scope.allowed:
            errors.append(f"task {task.id}: scope.allowed must list at least one project-relative path")
        for path in task.scope.allowed:
            if not is_safe_scope_path(path):
                errors.append(
                    f"task {task.id}: scope path {path!r} is not a valid project-relative path"
                )
        for path in task.preread:
            if not is_safe_scope_path(path):
                errors.append(
                    f"task {task.id}: preread path {path!r} is not a valid project-relative path"
                )
        if task.routing.complexity not in ("low", "medium", "high"):
            errors.append(
                f"task {task.id}: routing.complexity {task.routing.complexity!r}"
                " must be low | medium | high"
            )
        for cap in task.routing.required_capabilities:
            if not cap.strip():
                errors.append(f"task {task.id}: required_capabilities must be non-empty strings")
            elif known_capabilities is not None and cap not in known_capabilities:
                errors.append(
                    f"task {task.id}: required capability {cap!r} is not declared by any profile"
                )
        for entry in task.verification:
            try:
                parse_verification_entry(entry)
            except ValueError as exc:
                errors.append(f"task {task.id}: {exc}")
        for dep in task.dependencies:
            if dep not in all_ids:
                errors.append(f"task {task.id}: dependency {dep!r} names a missing task")

    cycle = find_cycle(ir.tasks)
    if cycle:
        errors.append("dependency cycle: " + " -> ".join(cycle))

    if ir.replan is not None:
        errors.extend(_validate_replan_internal(ir))

    # Goal acceptance coverage: verbatim strings, no interpretation.
    task_acceptance: set[str] = set()
    for task in ir.tasks:
        task_acceptance.update(task.acceptance)
    for criterion in goal_acceptance:
        if criterion.strip() and criterion not in task_acceptance:
            errors.append(
                "Goal acceptance criterion is not present verbatim in any task acceptance list:"
                f" {criterion!r}"
            )

    return errors


CLASSIFICATION_VALUES = tuple(c.value for c in ReplanClassification)
DISPOSITION_VALUES = tuple(d.value for d in SupersededDisposition)
_SINGLE_SUCCESSOR_DISPOSITIONS = {
    SupersededDisposition.CONFIRMED: ReplanClassification.CONFIRM.value,
    SupersededDisposition.CONTINUED: ReplanClassification.CONTINUE.value,
    SupersededDisposition.REDONE: ReplanClassification.REDO.value,
}


def _validate_replan_internal(ir: PlanIR) -> list[str]:
    """Structural checks over the declared mapping itself — no recorded
    history needed. Errors locate the offending task.

    Whether a redo reason is *justified*, a confirm verification
    *sufficient*, or a dropped note honest is semantic review
    (docs/replan-contract.md) — decided by a verifier, never here. No rule
    in this module inherits a prior passed status.
    """
    replan = ir.replan
    errors: list[str] = []
    task_ids = {t.id for t in ir.tasks}
    tasks_by_id = {t.id: t for t in ir.tasks}

    mapped: dict[str, ReplanTaskMapping] = {}
    citations: dict[tuple[int, str], list[tuple[str, bool]]] = {}

    for m in replan.tasks:
        if m.task not in task_ids:
            errors.append(f"replan mapping: task {m.task!r} is not a task in this plan")
            continue
        if m.task in mapped:
            errors.append(f"task {m.task}: duplicate replan mapping entry")
            continue
        mapped[m.task] = m

        classification = parse_enum(ReplanClassification, m.classification)
        if classification is None:
            errors.append(
                f"task {m.task}: replan classification {m.classification!r} must be"
                f" one of {' | '.join(CLASSIFICATION_VALUES)}"
            )
        else:
            if classification is ReplanClassification.NEW and m.sources:
                errors.append(
                    f"task {m.task}: classification 'new' must declare no sources"
                    f" (got {len(m.sources)}); new work has no prior correspondence"
                )
            if classification is not ReplanClassification.NEW and not m.sources:
                errors.append(
                    f"task {m.task}: classification {m.classification!r} must"
                    " declare at least one source {revision, task_id}"
                )
            if classification is ReplanClassification.REDO and not m.redo_reason.strip():
                errors.append(
                    f"task {m.task}: classification 'redo' requires redo_reason"
                    " stating why this work must be done again"
                )
            if classification is not ReplanClassification.REDO and m.redo_reason.strip():
                errors.append(
                    f"task {m.task}: redo_reason is only valid with classification"
                    " 'redo'"
                )
            if classification is ReplanClassification.CONFIRM and not m.confirm_verification:
                errors.append(
                    f"task {m.task}: classification 'confirm' must list the current"
                    " verification requirements in confirm_verification; a prior"
                    " passed status is never a substitute"
                )
            if classification is not ReplanClassification.CONFIRM and m.confirm_verification:
                errors.append(
                    f"task {m.task}: confirm_verification is only valid with"
                    " classification 'confirm'"
                )
            if classification is ReplanClassification.CONFIRM and m.confirm_verification:
                task = tasks_by_id[m.task]
                for entry in m.confirm_verification:
                    try:
                        parse_verification_entry(entry)
                    except ValueError as exc:
                        errors.append(f"task {m.task}: {exc}")
                    if entry not in task.verification:
                        errors.append(
                            f"task {m.task}: confirm verification {entry!r} must"
                            " appear verbatim in the task's verification list"
                        )

        for source in m.sources:
            if not TASK_ID_RE.fullmatch(source.task_id):
                errors.append(
                    f"task {m.task}: source task id {source.task_id!r} must match"
                    " T<digits>"
                )
            if source.revision < 1:
                errors.append(
                    f"task {m.task}: source revision {source.revision} must be >= 1"
                )
            citations.setdefault((source.revision, source.task_id), []).append(
                (m.task, source.part)
            )
        for artifact in m.artifacts:
            if not artifact.strip():
                errors.append(
                    f"task {m.task}: artifact references must be non-empty strings"
                )

    for task in ir.tasks:
        if task.id not in mapped:
            errors.append(
                f"task {task.id}: replan classification missing; every task in a"
                f" replan must declare one of {' | '.join(CLASSIFICATION_VALUES)}"
            )

    for key in sorted(citations):
        cites = citations[key]
        if len(cites) > 1:
            for new_task, part in cites:
                if not part:
                    errors.append(
                        f"task {new_task}: source {key[0]}:{key[1]} claims the whole"
                        f" prior task but {len(cites)} tasks cite it; mark every"
                        " citation part=true (a split) or merge the successors"
                    )

    seen: set[tuple[int, str]] = set()
    for entry in replan.superseded:
        key = (entry.revision, entry.task_id)
        if not TASK_ID_RE.fullmatch(entry.task_id):
            errors.append(
                f"prior task {key[0]}:{key[1]}: task id must match T<digits>"
            )
            continue
        if entry.revision < 1:
            errors.append(
                f"prior task {key[0]}:{key[1]}: revision must be >= 1"
            )
            continue
        if key in seen:
            errors.append(f"prior task {key[0]}:{key[1]}: duplicate superseded entry")
            continue
        seen.add(key)
        disposition = parse_enum(SupersededDisposition, entry.disposition)
        if disposition is None:
            errors.append(
                f"prior task {key[0]}:{key[1]}: disposition {entry.disposition!r}"
                f" must be one of {' | '.join(DISPOSITION_VALUES)}"
            )
        for successor in entry.successors:
            if successor not in task_ids:
                errors.append(
                    f"prior task {key[0]}:{key[1]}: successor {successor!r} is not"
                    " a task in this plan"
                )
        if disposition is SupersededDisposition.SPLIT and len(entry.successors) < 2:
            errors.append(
                f"prior task {key[0]}:{key[1]}: disposition 'split' must list at"
                " least two successors"
            )
        if disposition is SupersededDisposition.DROPPED:
            if entry.successors:
                errors.append(
                    f"prior task {key[0]}:{key[1]}: disposition 'dropped' must"
                    " list no successors"
                )
            if not entry.note.strip():
                errors.append(
                    f"prior task {key[0]}:{key[1]}: disposition 'dropped' requires"
                    " a note explaining why this work is abandoned"
                )
    return errors


def validate_replan(ir: PlanIR, prior_tasks: list[dict]) -> list[str]:
    """Cross-check the declared replan mapping against recorded prior tasks.

    ``prior_tasks`` rows mirror the replan snapshot: ``revision``,
    ``task_id``, ``status`` (a TaskStatus value). Pure — no store access,
    no guessing; missing rows are reported, never inferred. Structural
    checks only: semantic review (is a redo reason justified, is a confirm
    verification sufficient, is a dropped note honest) lives outside this
    module per docs/replan-contract.md, and no rule here inherits a prior
    passed status into the new plan.
    """
    if ir.replan is None:
        return [
            "replan mapping missing: a replan revision must classify every task,"
            " declare its sources, and give every prior-revision task a"
            " disposition"
        ]
    replan = ir.replan
    errors: list[str] = []
    prior_revision = replan.prior_revision
    if prior_revision < 1:
        errors.append(f"replan: prior_revision {prior_revision} must be >= 1")

    recorded: dict[tuple[int, str], str] = {}
    for row in prior_tasks:
        recorded[(int(row["revision"]), str(row["task_id"]))] = str(row["status"])

    superseded_by_key = {(s.revision, s.task_id): s for s in replan.superseded}
    citations: dict[tuple[int, str], set[str]] = {}
    for m in replan.tasks:
        for source in m.sources:
            citations.setdefault((source.revision, source.task_id), set()).add(m.task)

    terminal = {status.value for status in TERMINAL_TASK_STATUSES}

    for m in replan.tasks:
        classification = parse_enum(ReplanClassification, m.classification)
        for source in m.sources:
            key = (source.revision, source.task_id)
            status = recorded.get(key)
            if status is None:
                errors.append(
                    f"task {m.task}: source {key[0]}:{key[1]} names a prior task"
                    " that is not recorded"
                )
                continue
            if (
                classification is ReplanClassification.CONFIRM
                and status != TaskStatus.PASSED.value
            ):
                errors.append(
                    f"task {m.task}: confirm source {key[0]}:{key[1]} is recorded"
                    f" {status!r}, not passed; confirm applies to completed work"
                    " only (classify the task redo or continue instead)"
                )
            if (
                classification is ReplanClassification.CONTINUE
                and status in terminal
            ):
                errors.append(
                    f"task {m.task}: continue source {key[0]}:{key[1]} is recorded"
                    f" {status!r}, a terminal status; continue applies to"
                    " unfinished work only"
                )

    # Every task of the superseded revision needs an explicit destination.
    for row in prior_tasks:
        key = (int(row["revision"]), str(row["task_id"]))
        if key[0] != prior_revision:
            continue
        if key not in superseded_by_key:
            errors.append(
                f"prior task {key[0]}:{key[1]}: destination missing; every task of"
                f" the superseded revision must declare one of"
                f" {' | '.join(DISPOSITION_VALUES)}"
            )

    for entry in replan.superseded:
        key = (entry.revision, entry.task_id)
        if key[0] != prior_revision:
            errors.append(
                f"prior task {key[0]}:{key[1]}: superseded entries must reference"
                f" the superseded revision {prior_revision}"
            )
            continue
        if key not in recorded:
            errors.append(
                f"prior task {key[0]}:{key[1]}: no such task is recorded in prior"
                f" revision {prior_revision}"
            )
            continue
        declared = set(entry.successors)
        citing = citations.get(key, set())
        if declared != citing:
            errors.append(
                f"prior task {key[0]}:{key[1]}: declared successors"
                f" {sorted(declared)} do not match the tasks citing it as a"
                f" source {sorted(citing)}"
            )
        disposition = parse_enum(SupersededDisposition, entry.disposition)
        if disposition is None:
            continue
        classifications = {
            m.classification
            for m in replan.tasks
            if m.task in citing
            for source in m.sources
            if (source.revision, source.task_id) == key
        }
        successor_prior_sources = {
            (source.revision, source.task_id)
            for m in replan.tasks
            if m.task in citing
            for source in m.sources
            if source.revision == prior_revision
        }
        if disposition in _SINGLE_SUCCESSOR_DISPOSITIONS:
            expected = _SINGLE_SUCCESSOR_DISPOSITIONS[disposition]
            if len(citing) != 1:
                errors.append(
                    f"prior task {key[0]}:{key[1]}: disposition"
                    f" {entry.disposition!r} requires exactly one citing task,"
                    f" found {sorted(citing)}"
                )
            if classifications and classifications != {expected}:
                errors.append(
                    f"prior task {key[0]}:{key[1]}: disposition"
                    f" {entry.disposition!r} but the successor classifies the"
                    f" source as {sorted(classifications)}"
                )
            if len(successor_prior_sources) > 1:
                errors.append(
                    f"prior task {key[0]}:{key[1]}: disposition"
                    f" {entry.disposition!r} but the successor combines several"
                    " prior-revision tasks; declare 'merged' instead"
                )
        elif disposition is SupersededDisposition.SPLIT:
            if len(citing) < 2:
                errors.append(
                    f"prior task {key[0]}:{key[1]}: disposition 'split' requires"
                    f" at least two citing tasks, found {sorted(citing)}"
                )
        elif disposition is SupersededDisposition.MERGED:
            if len(successor_prior_sources) < 2:
                errors.append(
                    f"prior task {key[0]}:{key[1]}: disposition 'merged' requires"
                    " the successor to combine at least two prior-revision tasks"
                )
    return errors


class ReplanCheckFailed(PlanValidationError):
    """The shared replan precheck rejected the plan (G004 T003).

    ``report`` carries the full structured precheck report: categorized
    ``errors`` rows (``{category, locus, message}``) plus the
    correspondence / classification / disposition / contract-diff /
    reference-issue sections. A rejected submission changes nothing — the
    previous revision stays active, its tasks keep their statuses, and the
    planning assignment (if any) stays waiting.
    """

    def __init__(self, report: dict):
        self.report = dict(report)
        super().__init__([row["message"] for row in report.get("errors", [])])


_ERROR_LOCUS_RES = (
    (re.compile(r"^replan mapping: task (T\d+)\b"), "task"),
    (re.compile(r"^task (T\d+)\b"), "task"),
    (re.compile(r"^prior task (\d+:T\d+)\b"), "prior"),
)

# Deterministic classification of validator messages into report categories.
# First matching rule wins; unknown shapes fall back to structure/plan. The
# predicates match the stable validator phrasings in this module only.
_ERROR_CATEGORY_RULES: tuple[tuple[str, str], ...] = (
    ("mapping", "replan mapping missing"),
    ("verification", "confirm verification"),
    ("verification", "verification list"),
    ("artifact", "artifact"),
    ("classification", "classification"),
    ("classification", "redo_reason"),
    ("source", "source"),
    ("source", "no such task is recorded"),
    ("disposition", "disposition"),
    ("disposition", "successors"),
    ("disposition", "superseded"),
    ("acceptance", "not present verbatim"),
    ("acceptance", "Goal acceptance criterion"),
)


def replan_error_category(message: str) -> tuple[str, str]:
    """(category, locus) for one validator error message.

    Category is one of mapping | classification | source | disposition |
    verification | artifact | acceptance | structure | plan; locus names
    the offending task (``T101``), prior task (``1:T001``), or ``plan``.
    Pure string classification over this module's own messages — enrichment
    that needs recorded state (cross-run detection and the like) belongs to
    the caller, not here.
    """
    locus = "plan"
    for pattern, kind in _ERROR_LOCUS_RES:
        match = pattern.match(message)
        if match:
            locus = match.group(1)
            break
    for category, marker in _ERROR_CATEGORY_RULES:
        if marker in message:
            return category, locus
    return ("structure" if message.startswith(("task ", "prior task ")) else "plan"), locus


def extract_json_object(text: str) -> dict | None:
    """First balanced JSON object embedded in ``text`` (or None). Real CLI
    agents (Cursor, 2026-10-03) wrap the requested JSON document in narrative
    text even when told to emit only JSON; the object may also be preceded by
    prose and followed by commentary."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def strict_json_schema(schema: dict) -> dict:
    """Rewrite a pydantic JSON schema into the strict form Codex's
    ``--output-schema`` endpoint accepts (verified against a real run,
    2026-10-03: the API rejects pydantic's plain output with
    ``invalid_json_schema ... 'additionalProperties' is required to be
    supplied and to be false``).

    Rules applied recursively (including ``$defs`` targets): every object
    declares ``additionalProperties: false`` and lists *every* property in
    ``required`` — strict mode forbids optional keys, and pydantic omits
    defaulted fields from ``required``. The returned dict is a deep copy;
    the input schema is never mutated.
    """
    import copy

    def walk(node: object) -> None:
        if not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict) and props:
            node["additionalProperties"] = False
            node["required"] = list(props.keys())
            for value in props.values():
                walk(value)
        for key in ("items", "prefixItems", "not"):
            if key in node:
                walk(node[key])
        for combinator in ("anyOf", "oneOf", "allOf"):
            for branch in node.get(combinator, []):
                walk(branch)
        defs = node.get("$defs")
        if isinstance(defs, dict):
            for defn in defs.values():
                walk(defn)

    strict = copy.deepcopy(schema)
    walk(strict)
    return strict


def resolve_depth(goal_text: str, explicit: str | None = None) -> PlanDepth:
    """--depth wins; otherwise deterministic keyword rules on the Goal text.
    Never calls a model."""
    if explicit:
        try:
            return PlanDepth(explicit)
        except ValueError:
            raise records.ConfigError(
                [f"--depth {explicit!r} invalid (expected light | standard | deep)"]
            ) from None
    text = goal_text.lower()
    if any(token in text for token in HIGH_RISK_TOKENS):
        return PlanDepth.DEEP
    if any(token in text for token in SMALL_SCOPE_TOKENS):
        return PlanDepth.LIGHT
    return PlanDepth.STANDARD


PLAN_IR_SCHEMA: dict = {
    "goal": "G001  (the active Goal id)",
    "exploration": {
        "summary": "string",
        "relevant_components": ["string"],
        "unknowns": ["string"],
        "assumptions": ["string"],
        "risks": ["string"],
    },
    "approach": {"summary": "string", "decisions": ["string"]},
    "tasks": [
        {
            "id": "T<digits>",
            "objective": "string",
            "dependencies": ["T<digits>"],
            "scope": {"allowed": ["project/relative/path"]},
            "acceptance": ["string (copy Goal acceptance criteria verbatim here)"],
            "verification": [
                "shell command...",
                "agent: instruction for an agent verifier",
                "agent[vision]: instruction requiring a vision-capable verifier",
            ],
            "preread": ["project/relative/file the worker must read first (tests included)"],
            "routing": {"complexity": "low | medium | high", "required_capabilities": ["coding"]},
        }
    ],
    "replan": {
        "prior_revision": 1,
        "tasks": [
            {
                "task": "T101  (an id from this plan's tasks)",
                "classification": "new | confirm | redo | continue",
                "sources": [
                    {"revision": 1, "task_id": "T001", "part": False},
                ],
                "redo_reason": "required iff classification is redo: why this work must be done again",
                "confirm_verification": [
                    "required iff classification is confirm: a current check that must also appear verbatim in the task's verification list",
                ],
                "artifacts": ["project-relative artifact/evidence path inherited via this correspondence"],
            }
        ],
        "superseded": [
            {
                "revision": 1,
                "task_id": "T001",
                "disposition": "confirmed | continued | redone | split | merged | dropped",
                "successors": ["T101"],
                "note": "required iff disposition is dropped: why this work is abandoned",
            }
        ],
    },
}


def render_replan_facts(snapshot: dict) -> str:
    """Render the deterministic ORX-state snapshot as the planner's facts
    block. Facts the state does not carry are labeled unknown/none-recorded —
    the planner must never guess them from exploration."""
    active = snapshot.get("active_revision")
    if active:
        head = (
            f"Active plan revision: {active['revision']} ({active['depth']},"
            f" planner {active['planner_profile']})."
        )
    else:
        head = "Active plan revision: none."
    history = "; ".join(
        f"rev {rev['revision']} {rev['status']}" for rev in snapshot["revisions"]
    ) or "(no revisions recorded)"
    lines = [
        "Execution facts — deterministic snapshot of ORX state (authoritative"
        f" history; generated {snapshot['generated_at']}; do NOT re-derive by"
        " exploring the repo):",
        f"Run {snapshot['run_id']}, Goal {snapshot['goal_id']}. {head}",
        f"Revision history: {history}.",
        "Task history across revisions:",
    ]
    tasks = snapshot["tasks"]
    if not tasks:
        lines.append("  (no tasks recorded — nothing has been planned or run yet)")
    for task in tasks:
        lines.append(
            f"  - [rev {task['revision']}] {task['task_id']}: {task['objective']}"
            f" — {task['status'].upper()}"
        )
        if task["dependencies"]:
            lines.append(
                f"      depends on: {', '.join(task['dependencies'])}"
            )
        if task["status"] == "failed":
            lines.append(
                "      failure: "
                + (task["failure_reason"] or "(unknown — no failure reason recorded)")
            )
        elif task["failure_reason"]:
            lines.append(f"      last recorded issue: {task['failure_reason']}")
        verifications = task["verifications"]
        if verifications:
            rendered = []
            for row in verifications:
                exit_note = (
                    "denied" if row["exit_code"] is None else f"exit {row['exit_code']}"
                )
                log = f" [log: {row['output_path']}]" if row["output_path"] else ""
                rendered.append(
                    f"{row['command']!r} ({row['kind']}, {exit_note},"
                    f" {'passed' if row['passed'] else 'FAILED'}){log}"
                )
            lines.append("      verified: " + "; ".join(rendered))
        else:
            lines.append("      verified: (no verification results recorded)")
        evidence = task["evidence"]
        if evidence:
            rendered_evidence = []
            for item in evidence:
                # Carry the recorded identity (evidence row id + producing
                # attempt id) so a replan can cite prior results traceably —
                # G004 T004; snapshots without ids render the path alone.
                identity = ""
                if item.get("id") is not None:
                    identity = (
                        f" (evidence row {item['id']},"
                        f" attempt {item.get('attempt_id')})"
                    )
                rendered_evidence.append(f"[{item['kind']}] {item['path']}{identity}")
            lines.append("      evidence: " + "; ".join(rendered_evidence))
        else:
            lines.append("      evidence: (none recorded)")
    return "\n".join(lines)


def planner_prompt(goal, depth: PlanDepth, replan_facts: str = "", intent: str = "") -> str:
    acceptance_lines = "\n".join(f"  - {item!r}" for item in goal.acceptance) or "  (none)"
    constraints_lines = "\n".join(f"  - {c}" for c in goal.constraints) or "  (none)"
    replan_header = (
        "\nThis is a REPLAN: an earlier plan revision exists. The Execution Facts\n"
        "below are ORX's recorded history of this Run.\n"
        if replan_facts
        else ""
    )
    facts_block = f"\n{replan_facts}\n" if replan_facts else ""
    intent_block = (
        "\nController's intent for THIS replan round (supplied via"
        " `replan --context-file`; it supplements the Goal — it can never"
        f" rewrite or replace it):\n{intent}\n"
        if intent
        else ""
    )
    replan_rules = (
        "\n- the Execution Facts above are authoritative history. Treat every task"
        "\n  marked PASSED as done work; do not re-plan or redo it unless this"
        "\n  round's intent explicitly changes it."
        "\n- task numbers never carry meaning across revisions: a task with the"
        "\n  same number in a different revision is DIFFERENT work. Never link"
        "\n  tasks by number — only the replan mapping you declare links them."
        "\n- \"replan\" is required on this revision: classify EVERY task in this"
        "\n  plan in \"replan\".\"tasks\" with a classification:"
        "\n    new      = no prior correspondence (no sources allowed)."
        "\n    confirm  = prior PASSED work relied on as-is; list the CURRENT"
        "\n               verification that proves it still holds in"
        "\n               confirm_verification (each entry must also appear"
        "\n               verbatim in the task's verification list) — a prior"
        "\n               passed status is never a substitute."
        "\n    redo     = work done again; redo_reason is MANDATORY and must say"
        "\n               why (what changed, what was wrong, what the new plan"
        "\n               needs that the old result cannot provide)."
        "\n    continue = prior unfinished work carried forward."
        "\n- every source must identify prior work by revision AND task id"
        "\n  ({\"revision\": 1, \"task_id\": \"T001\"}); set part=true when the task"
        "\n  covers only part of a prior task (splits, partial correspondence)."
        "\n- EVERY task of the prior revision must appear in"
        "\n  \"replan\".\"superseded\" with a disposition — confirmed | continued |"
        "\n  redone | split | merged | dropped (dropped needs a note) — and its"
        "\n  successors must exactly match the tasks citing it as a source."
        "\n- reference prior results and artifacts through this correspondence"
        "\n  (\"artifacts\"), never by task number alone. The Execution Facts"
        "\n  label each recorded evidence row with its evidence row id and"
        "\n  producing attempt id — cite the evidence paths in \"artifacts\";"
        "\n  when a citing task delivers, ORX records the provenance and a"
        "\n  digest snapshot of every referenced artifact, bound to the"
        "\n  completing attempt, so later rounds see existence and change."
        "\n- ORX never auto-passes tasks: every task in the plan you emit starts"
        "\n  pending or runnable. A Goal acceptance criterion already satisfied by"
        "\n  passed work must still appear VERBATIM in some task's acceptance —"
        "\n  give that task a cheap verification that the fact still holds, not a"
        "\n  redo of the work."
        if replan_facts
        else ""
    )
    return f"""You are the ORX planner for Goal {goal.id} at depth '{depth.value}'.
{replan_header}
Objective:
{goal.objective}

Constraints:
{constraints_lines}

Acceptance criteria (copy each string VERBATIM into the acceptance list of at
least one task; a paraphrase will be rejected):
{acceptance_lines}

{('Context: ' + goal.context) if goal.context else ''}
{intent_block}{facts_block}
Explore the repository, then produce ONLY a JSON document matching this Plan IR
schema (unknown extra fields are ignored):

{PLAN_IR_SCHEMA}

Rules:
- task ids are T followed by digits (T001, T002, ...) and must be unique.
- dependencies may only reference task ids in this plan; no cycles.
- scope.allowed lists project-relative paths only (never absolute, never '..').
- routing.complexity is low | medium | high.
- verification entries are either a shell command (run from the project root),
  'agent: <instruction>' for an agent verifier, or 'agent[vision]: <instruction>'
  when the verifier must see images or rendered UI.
- order verification deterministic-first: lead with shell checks (compile,
  tests, lint, fixtures); add agent checks only for what shell cannot prove,
  and 'agent[vision]' only for rendered-UI acceptance.
- preread lists the project-relative files a worker must read before starting
  (code and tests; first entry may be a per-round plan document). Keep it
  small — it bounds the worker's exploration surface.
- emit "replan": null on a first plan; on a replan revision "replan" is
  required and must follow the replan rules below.
- do not modify the Goal text.{replan_rules}
"""
