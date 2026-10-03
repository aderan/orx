"""Plan IR validation and depth policy."""

from __future__ import annotations

import json

import pytest

from orx import plan as plan_mod
from orx.records import PlanSyntaxError

from conftest import ir_for, task_spec

KNOWN_CAPS = {"coding", "vision"}


def validate(project, goal, tasks):
    ir = plan_mod.parse_ir(ir_for(goal, tasks))
    return plan_mod.validate_ir(ir, goal.id, goal.acceptance, KNOWN_CAPS)


def test_valid_ir_parses_and_validates(goal):
    ir = plan_mod.parse_ir(ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ]))
    assert ir.goal == goal.id
    assert plan_mod.validate_ir(ir, goal.id, goal.acceptance, KNOWN_CAPS) == []


def test_extra_unknown_fields_ignored(goal):
    data = ir_for(goal, [task_spec("T001", acceptance=goal.acceptance)])
    data["notes"] = {"host_scratch": "anything"}
    data["tasks"][0]["host_note"] = "keep me"
    ir = plan_mod.parse_ir(data)
    assert plan_mod.validate_ir(ir, goal.id, goal.acceptance, KNOWN_CAPS) == []


def test_malformed_ir_raises_syntax_errors(goal):
    with pytest.raises(PlanSyntaxError):
        plan_mod.parse_ir({"goal": goal.id})  # missing exploration/approach/tasks
    with pytest.raises(PlanSyntaxError):
        plan_mod.parse_ir({**ir_for(goal, []), "tasks": [{"id": "T001"}]})  # missing scope/routing


def test_duplicate_task_ids(goal):
    errors = validate(None, goal, [
        task_spec("T001", acceptance=goal.acceptance),
        task_spec("T001", acceptance=goal.acceptance),
    ])
    assert any("duplicate" in e for e in errors)


def test_bad_task_id_format(goal):
    errors = validate(None, goal, [task_spec("task-1", acceptance=goal.acceptance)])
    assert any("T<digits>" in e for e in errors)


def test_missing_dependency_target(goal):
    errors = validate(None, goal, [
        task_spec("T001", deps=["T099"], acceptance=goal.acceptance),
    ])
    assert any("missing task" in e for e in errors)


def test_dependency_cycle_detected(goal):
    errors = validate(None, goal, [
        task_spec("T001", deps=["T002"], acceptance=goal.acceptance[:1]),
        task_spec("T002", deps=["T003"], acceptance=goal.acceptance[1:]),
        task_spec("T003", deps=["T001"]),
    ])
    assert any("cycle" in e for e in errors)


def test_self_dependency_is_a_cycle(goal):
    errors = validate(None, goal, [task_spec("T001", deps=["T001"], acceptance=goal.acceptance)])
    assert any("cycle" in e for e in errors)


@pytest.mark.parametrize("bad", ["../outside", "/etc", "src/../lib", "~", ""])
def test_scope_path_escapes_rejected(goal, bad):
    errors = validate(None, goal, [
        task_spec("T001", acceptance=goal.acceptance, allowed=[bad] if bad else []),
    ])
    assert any("scope" in e for e in errors)


def test_bad_complexity_rejected(goal):
    errors = validate(None, goal, [
        task_spec("T001", acceptance=goal.acceptance, complexity="extreme"),
    ])
    assert any("complexity" in e for e in errors)


def test_empty_task_objective_rejected(goal):
    errors = validate(None, goal, [task_spec("T001", objective="  ", acceptance=goal.acceptance)])
    assert any("objective" in e for e in errors)


def test_goal_mismatch_rejected(goal):
    ir = plan_mod.parse_ir(ir_for(goal, [task_spec("T001", acceptance=goal.acceptance)]))
    errors = plan_mod.validate_ir(ir, "G999", goal.acceptance, KNOWN_CAPS)
    assert any("G999" in e for e in errors)


def test_acceptance_coverage_requires_verbatim_copy(goal):
    # Paraphrase is rejected: ORX does not interpret meaning.
    errors = validate(None, goal, [
        task_spec("T001", acceptance=["the marker file exists"]),
    ])
    assert any("verbatim" in e and "marker file exists" in e for e in errors)


def test_acceptance_coverage_split_across_tasks(goal):
    errors = validate(None, goal, [
        task_spec("T001", acceptance=[goal.acceptance[0]]),
        task_spec("T002", acceptance=[goal.acceptance[1]]),
    ])
    assert errors == []


@pytest.mark.parametrize("bad", [
    "agent",                # must be 'agent: ...'
    "agent:",               # empty instruction
    "agent[vision]:",       # empty instruction
    "agent[hearing]: loud", # unsupported capability bracket
    "agent vision: check",  # missing colon form
    "",                     # empty entry
])
def test_malformed_verification_entries(goal, bad):
    errors = validate(None, goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=[bad]),
    ])
    assert any("verification" in e or "agent" in e for e in errors)


def test_valid_verification_forms_accepted(goal):
    errors = validate(None, goal, [
        task_spec("T001", acceptance=goal.acceptance,
                  verification=["pytest -q", "agent: the output reads well",
                                "agent[vision]: the screenshot looks right"]),
    ])
    assert errors == []


def test_unknown_required_capability_rejected(goal):
    errors = validate(None, goal, [
        task_spec("T001", acceptance=goal.acceptance, caps=("telepathy",)),
    ])
    assert any("telepathy" in e for e in errors)


def test_empty_task_list_rejected(goal):
    errors = validate(None, goal, [])
    assert any("no tasks" in e for e in errors)


# -- depth ------------------------------------------------------------------


def test_explicit_depth_overrides_tokens():
    resolve = plan_mod.resolve_depth
    assert resolve("refactor the architecture", "light") is plan_mod.PlanDepth.LIGHT
    assert resolve("fix a typo", "deep") is plan_mod.PlanDepth.DEEP


def test_high_risk_tokens_mean_deep():
    for text in ("plan the migration", "refactor payments", "public api change", "breaking release"):
        assert plan_mod.resolve_depth(text) is plan_mod.PlanDepth.DEEP


def test_small_scope_tokens_mean_light():
    for text in ("fix a typo", "rename the variable", "add a comment", "tiny tweak"):
        assert plan_mod.resolve_depth(text) is plan_mod.PlanDepth.LIGHT


def test_high_risk_beats_small_scope():
    assert plan_mod.resolve_depth("refactor this tiny module") is plan_mod.PlanDepth.DEEP


def test_plain_goal_is_standard():
    assert plan_mod.resolve_depth("add an export button") is plan_mod.PlanDepth.STANDARD


# -- verification entry parsing ---------------------------------------------


def test_parse_verification_entry_kinds():
    cmd = plan_mod.parse_verification_entry("pytest -q ")
    assert cmd.kind == "command" and cmd.capabilities == ()
    agent = plan_mod.parse_verification_entry("agent: looks fine")
    assert agent.kind == "agent" and agent.capabilities == ()
    vision = plan_mod.parse_verification_entry("agent[vision]: ui ok")
    assert vision.kind == "agent" and vision.capabilities == ("vision",)


# -- strict schema for codex --output-schema ---------------------------------


def test_strict_json_schema_covers_every_object():
    strict = plan_mod.strict_json_schema(plan_mod.PlanIR.model_json_schema())

    def check(node):
        props = node.get("properties")
        if isinstance(props, dict) and props:
            assert node.get("additionalProperties") is False, node
            assert sorted(node.get("required", [])) == sorted(props.keys()), node
        for value in props.values() if isinstance(props, dict) else []:
            check(value)
        for defn in node.get("$defs", {}).values():
            check(defn)

    check(strict)


def test_strict_json_schema_does_not_mutate_input():
    original = plan_mod.PlanIR.model_json_schema()
    before = json.dumps(original, sort_keys=True)
    plan_mod.strict_json_schema(original)
    assert json.dumps(original, sort_keys=True) == before
    # Defaulted fields (e.g. PlanTask.objective) are missing from pydantic's
    # required but must be present in the strict form.
    plain_task = original["$defs"]["PlanTask"]
    assert "objective" not in plain_task.get("required", [])
    strict = plan_mod.strict_json_schema(original)
    assert "objective" in strict["$defs"]["PlanTask"]["required"]


def test_extract_json_object_recovers_wrapped_document():
    wrapped = (
        "I'll inspect the repo first and then produce the plan.\n"
        "Here is the plan:\n"
        + json.dumps({"goal": "G001", "tasks": [{"id": "T001"}]})
        + "\nThat completes the plan."
    )
    recovered = plan_mod.extract_json_object(wrapped)
    assert recovered == {"goal": "G001", "tasks": [{"id": "T001"}]}


def test_extract_json_object_handles_braces_inside_strings():
    text = 'Preamble with f"hello {name}" mention then {"goal": "G001"} done'
    assert plan_mod.extract_json_object(text) == {"goal": "G001"}
    assert plan_mod.extract_json_object("no object here") is None
