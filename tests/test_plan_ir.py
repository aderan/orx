"""Plan IR validation and depth policy."""

from __future__ import annotations

import json
from types import SimpleNamespace

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


# -- replan correspondence & work classification contract (G004) ---------------
#
# The mapping is the ONLY link between revisions. Task numbers never carry
# meaning across revisions; no rule below inherits a prior passed status.


def prior(revision, task_id, status):
    return {"revision": revision, "task_id": task_id, "status": status}


def mapping(task, classification, sources=(), redo_reason="",
            confirm_verification=(), artifacts=()):
    return {
        "task": task,
        "classification": classification,
        "sources": [
            {"revision": rev, "task_id": tid, "part": part}
            for rev, tid, part in sources
        ],
        "redo_reason": redo_reason,
        "confirm_verification": list(confirm_verification),
        "artifacts": list(artifacts),
    }


def superseded(task_id, disposition, successors=(), note="", revision=1):
    return {
        "revision": revision,
        "task_id": task_id,
        "disposition": disposition,
        "successors": list(successors),
        "note": note,
    }


def check_replan(goal, tasks, replan, prior_tasks=None):
    data = ir_for(goal, tasks)
    data["replan"] = replan
    ir = plan_mod.parse_ir(data)
    errors = plan_mod.validate_ir(ir, goal.id, goal.acceptance, KNOWN_CAPS)
    if prior_tasks is not None:
        errors = errors + plan_mod.validate_replan(ir, prior_tasks)
    return errors


def test_replan_fields_survive_parse_and_serialize_round_trip(goal):
    data = ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["pytest -q"]),
    ])
    data["replan"] = {
        "prior_revision": 1,
        "tasks": [mapping("T001", "confirm", sources=[(1, "T001", False)],
                          confirm_verification=["pytest -q"],
                          artifacts=["reports/g001/first/run.zip"])],
        "superseded": [superseded("T001", "confirmed", ["T101"],
                                  note="carried intact")],
    }
    ir = plan_mod.parse_ir(data)
    assert ir.replan.prior_revision == 1
    assert ir.replan.tasks[0].classification == "confirm"
    assert ir.replan.tasks[0].sources[0].revision == 1
    assert ir.replan.tasks[0].sources[0].part is False
    assert ir.replan.tasks[0].artifacts == ["reports/g001/first/run.zip"]
    assert ir.replan.superseded[0].disposition == "confirmed"

    dumped = ir.to_dict()
    assert dumped["replan"]["superseded"][0]["successors"] == ["T101"]
    again = plan_mod.parse_ir(dumped)
    assert again.to_dict() == dumped


def test_strict_schema_requires_every_replan_field():
    strict = plan_mod.strict_json_schema(plan_mod.PlanIR.model_json_schema())
    for name in ("ReplanMapping", "ReplanTaskMapping", "ReplanSource",
                 "ReplanSuperseded"):
        node = strict["$defs"][name]
        assert node["additionalProperties"] is False
        assert sorted(node["required"]) == sorted(node["properties"])
    # PlanIR is the schema root: the nullable field is still required there,
    # so the strict planner output must carry "replan" (null on a first plan).
    assert "replan" in strict["required"]


def test_first_plan_and_historical_ir_without_replan_still_readable(goal):
    data = ir_for(goal, [task_spec("T001", acceptance=goal.acceptance)])
    ir = plan_mod.parse_ir(data)  # key absent: first plans and historical IRs
    assert ir.replan is None
    assert plan_mod.validate_ir(ir, goal.id, goal.acceptance, KNOWN_CAPS) == []
    explicit_null = plan_mod.parse_ir({**data, "replan": None})
    assert explicit_null.replan is None
    assert explicit_null.to_dict().get("replan") is None


def test_replan_unknown_extra_fields_keep_existing_ignore_behavior(goal):
    data = ir_for(goal, [task_spec("T001", acceptance=goal.acceptance)])
    data["replan"] = {
        "prior_revision": 1,
        "host_note": "anything",
        "tasks": [{**mapping("T001", "new"), "scratch": 1}],
        "superseded": [],
    }
    ir = plan_mod.parse_ir(data)
    assert ir.replan.tasks[0].classification == "new"


def test_replan_task_without_classification_error_locates_the_task(goal):
    errors = check_replan(goal, [
        task_spec("T001", acceptance=goal.acceptance),
        task_spec("T002"),
    ], {"prior_revision": 1, "tasks": [mapping("T001", "new")], "superseded": []})
    assert any("T002" in e and "classification" in e for e in errors)


def test_replan_unknown_classification_rejected(goal):
    errors = check_replan(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ], {"prior_revision": 1, "tasks": [mapping("T001", "inherited")],
        "superseded": []})
    assert any("T001" in e and "classification" in e for e in errors)


def test_new_task_must_not_declare_sources(goal):
    errors = check_replan(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ], {"prior_revision": 1,
        "tasks": [mapping("T001", "new", sources=[(1, "T001", False)])],
        "superseded": []})
    assert any("T001" in e and "new" in e and "source" in e for e in errors)


@pytest.mark.parametrize("cls", ["confirm", "redo", "continue"])
def test_corresponding_task_requires_source_declaration(goal, cls):
    errors = check_replan(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ], {"prior_revision": 1, "tasks": [mapping("T001", cls)],
        "superseded": []})
    assert any("T001" in e and "source" in e for e in errors)


def test_redo_requires_explicit_reason(goal):
    errors = check_replan(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ], {"prior_revision": 1,
        "tasks": [mapping("T001", "redo", sources=[(1, "T001", False)])],
        "superseded": []})
    assert any("T001" in e and "redo_reason" in e for e in errors)


def test_redo_reason_rejected_outside_redo(goal):
    errors = check_replan(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ], {"prior_revision": 1,
        "tasks": [mapping("T001", "continue", sources=[(1, "T002", False)],
                          redo_reason="because")],
        "superseded": []})
    assert any("T001" in e and "redo_reason" in e for e in errors)


def test_confirm_requires_current_verification_not_old_passed(goal):
    errors = check_replan(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["pytest -q"]),
    ], {"prior_revision": 1,
        "tasks": [mapping("T001", "confirm", sources=[(1, "T001", False)])],
        "superseded": []})
    assert any(
        "T001" in e and "confirm_verification" in e and "passed" in e
        for e in errors
    )


def test_confirm_verification_must_live_in_task_verification(goal):
    errors = check_replan(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["pytest -q"]),
    ], {"prior_revision": 1,
        "tasks": [mapping("T001", "confirm", sources=[(1, "T001", False)],
                          confirm_verification=["make verify"])],
        "superseded": []})
    assert any("T001" in e and "make verify" in e and "verification" in e
               for e in errors)


def test_confirm_verification_only_for_confirm(goal):
    errors = check_replan(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ], {"prior_revision": 1,
        "tasks": [mapping("T001", "new", confirm_verification=["pytest -q"])],
        "superseded": []})
    assert any("T001" in e and "confirm_verification" in e for e in errors)


def test_artifact_references_must_be_non_empty(goal):
    errors = check_replan(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ], {"prior_revision": 1,
        "tasks": [mapping("T001", "new", artifacts=["   "])],
        "superseded": []})
    assert any("T001" in e and "artifact" in e for e in errors)


def test_validate_replan_reports_missing_mapping_for_a_replan(goal):
    ir = plan_mod.parse_ir(ir_for(goal, [task_spec("T001", acceptance=goal.acceptance)]))
    errors = plan_mod.validate_replan(ir, [prior(1, "T001", "passed")])
    assert len(errors) == 1
    assert "mapping" in errors[0]


def test_source_must_name_recorded_prior_task(goal):
    errors = check_replan(goal, [
        task_spec("T101", acceptance=goal.acceptance, verification=["pytest -q"]),
    ], {"prior_revision": 1,
        "tasks": [mapping("T101", "confirm", sources=[(1, "T099", False)],
                          confirm_verification=["pytest -q"])],
        "superseded": []},
        prior_tasks=[prior(1, "T001", "passed")])
    assert any("T101" in e and "1:T099" in e for e in errors)


def test_confirm_source_must_be_recorded_passed(goal):
    errors = check_replan(goal, [
        task_spec("T101", acceptance=goal.acceptance, verification=["pytest -q"]),
    ], {"prior_revision": 1,
        "tasks": [mapping("T101", "confirm", sources=[(1, "T001", False)],
                          confirm_verification=["pytest -q"])],
        "superseded": [superseded("T001", "confirmed", ["T101"])]},
        prior_tasks=[prior(1, "T001", "failed")])
    assert any("T101" in e and "1:T001" in e and "passed" in e for e in errors)


def test_continue_source_must_be_unfinished(goal):
    def errors_for(status):
        return check_replan(goal, [
            task_spec("T101", acceptance=goal.acceptance),
        ], {"prior_revision": 1,
            "tasks": [mapping("T101", "continue", sources=[(1, "T002", False)])],
            "superseded": [superseded("T002", "continued", ["T101"])]},
            prior_tasks=[prior(1, "T002", status)])

    assert any("T101" in e and "1:T002" in e for e in errors_for("passed"))
    assert errors_for("runnable") == []


def test_prior_task_without_destination_error_locates_the_task(goal):
    errors = check_replan(goal, [
        task_spec("T101", acceptance=goal.acceptance, verification=["pytest -q"]),
    ], {"prior_revision": 1,
        "tasks": [mapping("T101", "confirm", sources=[(1, "T001", False)],
                          confirm_verification=["pytest -q"])],
        "superseded": [superseded("T001", "confirmed", ["T101"])]},
        prior_tasks=[prior(1, "T001", "passed"), prior(1, "T002", "pending")])
    assert any("1:T002" in e and "destination" in e for e in errors)


def test_declared_successors_must_match_citing_sources(goal):
    # Declares a successor that never cites the prior task as a source.
    errors = check_replan(goal, [
        task_spec("T101", acceptance=goal.acceptance),
    ], {"prior_revision": 1,
        "tasks": [mapping("T101", "new")],
        "superseded": [superseded("T001", "dropped", ["T101"], note="x")]},
        prior_tasks=[prior(1, "T001", "passed")])
    assert any("1:T001" in e and "successors" in e for e in errors)
    # Reverse direction: cites the prior task but declares no successor for it.
    errors = check_replan(goal, [
        task_spec("T101", acceptance=goal.acceptance, verification=["pytest -q"]),
    ], {"prior_revision": 1,
        "tasks": [mapping("T101", "confirm", sources=[(1, "T001", False)],
                          confirm_verification=["pytest -q"])],
        "superseded": [superseded("T001", "confirmed", [])]},
        prior_tasks=[prior(1, "T001", "passed")])
    assert any("1:T001" in e and "successors" in e for e in errors)


def test_renumbered_correspondence_is_traceable(goal):
    errors = check_replan(goal, [
        task_spec("T101", acceptance=goal.acceptance, verification=["pytest -q"]),
    ], {"prior_revision": 1,
        "tasks": [mapping("T101", "confirm", sources=[(1, "T001", False)],
                          confirm_verification=["pytest -q"],
                          artifacts=["reports/g001/first/run.zip"])],
        "superseded": [superseded("T001", "confirmed", ["T101"])]},
        prior_tasks=[prior(1, "T001", "passed")])
    assert errors == []


def test_same_numbered_tasks_are_not_auto_corresponded(goal):
    # Old revision had T001 passed; the new plan reuses the number T001 for
    # genuinely new work. Number coincidence links nothing: the old T001's
    # destination is still missing, and classifying as confirm without a
    # source is an error rather than a silent inherit.
    errors = check_replan(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ], {"prior_revision": 1, "tasks": [mapping("T001", "new")],
        "superseded": []},
        prior_tasks=[prior(1, "T001", "passed")])
    assert any("1:T001" in e and "destination" in e for e in errors)

    errors = check_replan(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["pytest -q"]),
    ], {"prior_revision": 1,
        "tasks": [mapping("T001", "confirm", confirm_verification=["pytest -q"])],
        "superseded": [superseded("T001", "confirmed", ["T001"])]},
        prior_tasks=[prior(1, "T001", "passed")])
    assert any("T001" in e and "source" in e for e in errors)


def test_split_across_two_successors_is_expressible(goal):
    # Prior T001 (passed) is split: one part is confirmed as-is, the other
    # part must be done again with a reason.
    tasks = [
        task_spec("T101", acceptance=goal.acceptance[:1], verification=["pytest -q tests/a.py"]),
        task_spec("T102", acceptance=goal.acceptance[1:]),
    ]
    replan = {
        "prior_revision": 1,
        "tasks": [
            mapping("T101", "confirm", sources=[(1, "T001", True)],
                    confirm_verification=["pytest -q tests/a.py"]),
            mapping("T102", "redo", sources=[(1, "T001", True)],
                    redo_reason="the parser half of T001 validated badly; the acceptance changed"),
        ],
        "superseded": [superseded("T001", "split", ["T101", "T102"])],
    }
    assert check_replan(goal, tasks, replan,
                        prior_tasks=[prior(1, "T001", "passed")]) == []


def test_multi_cited_prior_task_requires_part_flags(goal):
    tasks = [
        task_spec("T101", acceptance=goal.acceptance[:1], verification=["pytest -q tests/a.py"]),
        task_spec("T102", acceptance=goal.acceptance[1:]),
    ]
    replan = {
        "prior_revision": 1,
        "tasks": [
            mapping("T101", "confirm", sources=[(1, "T001", False)],
                    confirm_verification=["pytest -q tests/a.py"]),
            mapping("T102", "redo", sources=[(1, "T001", True)],
                    redo_reason="the remainder must be redone"),
        ],
        "superseded": [superseded("T001", "split", ["T101", "T102"])],
    }
    errors = check_replan(goal, tasks, replan,
                          prior_tasks=[prior(1, "T001", "passed")])
    assert any("T101" in e and "part" in e for e in errors)


def test_split_disposition_requires_two_citing_tasks(goal):
    tasks = [
        task_spec("T101", acceptance=goal.acceptance[:1], verification=["pytest -q tests/a.py"]),
        task_spec("T102", acceptance=goal.acceptance[1:]),
    ]
    # Only T102 cites the prior task; declaring 'split' is wrong.
    replan = {
        "prior_revision": 1,
        "tasks": [
            mapping("T101", "new"),
            mapping("T102", "redo", sources=[(1, "T001", False)],
                    redo_reason="scope changed"),
        ],
        "superseded": [superseded("T001", "split", ["T101", "T102"])],
    }
    errors = check_replan(goal, tasks, replan,
                          prior_tasks=[prior(1, "T001", "passed")])
    assert any("1:T001" in e and "split" in e for e in errors)
    # And the declared-successors check flags T101 (declared, never cites).
    assert any("1:T001" in e and "successors" in e for e in errors)


def test_split_disposition_requires_two_successors(goal):
    errors = check_replan(goal, [
        task_spec("T101", acceptance=goal.acceptance),
    ], {"prior_revision": 1, "tasks": [mapping("T101", "new")],
        "superseded": [superseded("T001", "split", ["T101"])]},
        prior_tasks=[prior(1, "T001", "cancelled")])
    assert any("1:T001" in e and "split" in e for e in errors)


def test_merge_of_two_prior_tasks_is_expressible(goal):
    errors = check_replan(goal, [
        task_spec("T101", acceptance=goal.acceptance),
    ], {"prior_revision": 1,
        "tasks": [mapping("T101", "redo",
                          sources=[(1, "T001", False), (1, "T002", False)],
                          redo_reason="the two halves must be rebuilt as one module")],
        "superseded": [superseded("T001", "merged", ["T101"]),
                       superseded("T002", "merged", ["T101"])]},
        prior_tasks=[prior(1, "T001", "passed"), prior(1, "T002", "failed")])
    assert errors == []


def test_merged_requires_multi_source_successor(goal):
    errors = check_replan(goal, [
        task_spec("T101", acceptance=goal.acceptance),
    ], {"prior_revision": 1,
        "tasks": [mapping("T101", "new")],
        "superseded": [superseded("T001", "merged", ["T101"])]},
        prior_tasks=[prior(1, "T001", "cancelled")])
    assert any("1:T001" in e and "merged" in e for e in errors)


def test_confirmed_disposition_rejects_merge_shaped_successor(goal):
    errors = check_replan(goal, [
        task_spec("T101", acceptance=goal.acceptance, verification=["pytest -q"]),
    ], {"prior_revision": 1,
        "tasks": [mapping("T101", "confirm",
                          sources=[(1, "T001", False), (1, "T002", False)],
                          confirm_verification=["pytest -q"])],
        "superseded": [superseded("T001", "confirmed", ["T101"]),
                       superseded("T002", "confirmed", ["T101"])]},
        prior_tasks=[prior(1, "T001", "passed"), prior(1, "T002", "passed")])
    assert any("1:T001" in e and "merged" in e for e in errors)


def test_confirmed_disposition_requires_confirm_classification(goal):
    errors = check_replan(goal, [
        task_spec("T101", acceptance=goal.acceptance),
    ], {"prior_revision": 1,
        "tasks": [mapping("T101", "redo", sources=[(1, "T001", False)],
                          redo_reason="the old result cannot satisfy the new acceptance")],
        "superseded": [superseded("T001", "confirmed", ["T101"])]},
        prior_tasks=[prior(1, "T001", "passed")])
    assert any("1:T001" in e and "confirmed" in e for e in errors)


def test_dropped_requires_note_and_no_successors(goal):
    errors = check_replan(goal, [
        task_spec("T101", acceptance=goal.acceptance),
    ], {"prior_revision": 1, "tasks": [mapping("T101", "new")],
        "superseded": [superseded("T001", "dropped", ["T101"])]},
        prior_tasks=[prior(1, "T001", "cancelled")])
    assert any("1:T001" in e and "dropped" in e for e in errors)

    errors = check_replan(goal, [
        task_spec("T101", acceptance=goal.acceptance),
    ], {"prior_revision": 1, "tasks": [mapping("T101", "new")],
        "superseded": [superseded("T001", "dropped", note="   ")]},
        prior_tasks=[prior(1, "T001", "cancelled")])
    assert any("1:T001" in e and "dropped" in e and "note" in e for e in errors)

    errors = check_replan(goal, [
        task_spec("T101", acceptance=goal.acceptance),
    ], {"prior_revision": 1, "tasks": [mapping("T101", "new")],
        "superseded": [superseded("T001", "dropped", note="goal no longer needs it")]},
        prior_tasks=[prior(1, "T001", "cancelled")])
    assert errors == []


def test_sources_may_cite_older_revisions_without_new_dispositions(goal):
    # Work recorded in revision 1 can be confirmed by a revision-3 replan;
    # only revision-2 tasks (the superseded revision) need dispositions.
    errors = check_replan(goal, [
        task_spec("T101", acceptance=goal.acceptance, verification=["pytest -q"]),
    ], {"prior_revision": 2,
        "tasks": [mapping("T101", "confirm", sources=[(1, "T001", False)],
                          confirm_verification=["pytest -q"])],
        "superseded": [superseded("T201", "dropped", note="superseded design", revision=2)]},
        prior_tasks=[prior(1, "T001", "passed"), prior(2, "T201", "cancelled")])
    assert errors == []


def test_superseded_entry_outside_prior_revision_rejected(goal):
    errors = check_replan(goal, [
        task_spec("T101", acceptance=goal.acceptance),
    ], {"prior_revision": 2, "tasks": [mapping("T101", "new")],
        "superseded": [superseded("T001", "dropped", note="old", revision=1)]},
        prior_tasks=[prior(1, "T001", "passed")])
    assert any("1:T001" in e for e in errors)


# -- planner prompt carries the replan mapping contract -----------------------


def prompt_goal():
    return SimpleNamespace(id="G001", objective="ship it",
                           acceptance=["marker exists"], constraints=[],
                           context="")


def test_planner_prompt_replan_rounds_require_the_mapping():
    prompt = plan_mod.planner_prompt(
        prompt_goal(), plan_mod.PlanDepth.STANDARD,
        replan_facts="Execution facts — deterministic snapshot",
    )
    for marker in ("classification", "sources", "superseded", "redo_reason",
                   "confirm_verification", "never auto-passes", "part=true",
                   "same number"):
        assert marker in prompt, marker


def test_planner_prompt_first_plan_emits_null_replan():
    prompt = plan_mod.planner_prompt(prompt_goal(), plan_mod.PlanDepth.STANDARD)
    assert '"replan": null' in prompt
    assert "This is a REPLAN" not in prompt
