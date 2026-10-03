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
from orx.records import PlanDepth, PlanSyntaxError

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


class PlanIR(BaseModel):
    model_config = ConfigDict(extra="ignore")
    goal: str
    exploration: Exploration
    approach: Approach
    tasks: list[PlanTask]

    def to_dict(self) -> dict:
        return self.model_dump()


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
            lines.append(
                "      evidence: "
                + "; ".join(f"[{kind}] {path}" for kind, path in evidence)
            )
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
- do not modify the Goal text.{replan_rules}
"""
