"""Plan revision semantics: supersession, replan guard, one active graph.

G004: every replan submit declares its old<->new correspondence explicitly
(conftest.with_replan); nothing infers a relation from task numbers, and the
shared precheck gates every activation path.
"""

from __future__ import annotations

import json

import pytest

from orx import dispatch
from orx import plan as plan_mod
from orx.records import NotFoundError, ReplanRejectedError
from orx.state import Store

from conftest import (
    ir_for,
    replan_task_entry,
    superseded_entry,
    task_spec,
    with_replan,
    write_evidence,
)


@pytest.fixture
def planned(project, goal):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance[:1], verification=["true"]),
        task_spec("T002", deps=["T001"], acceptance=goal.acceptance[1:]),
    ]))
    return project


def _renumbered_plan(goal, **overrides):
    """A valid replan of revision 1: prior T002 continues as T101, prior T001
    (passed or not) is explicitly dropped with a note."""
    ir = ir_for(goal, [task_spec("T101", acceptance=goal.acceptance, **overrides)])
    return with_replan(ir, 1,
                       [replan_task_entry("T101", "continue", sources=[(1, "T002")])],
                       [superseded_entry(1, "T001", "dropped",
                                         note="phase-1 outcome no longer needed"),
                        superseded_entry(1, "T002", "continued", successors=["T101"])])


def test_new_revision_supersedes_and_cancels_unfinished(planned, goal, tmp_path):
    project = planned
    # pass T001 so we can verify terminal states survive supersession
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))

    result = dispatch.submit_plan(project, _renumbered_plan(goal))
    assert result["revision"] == 2
    assert result["superseded_revision"] == 1
    assert result["cancelled_tasks"] == ["T002"]  # pending -> cancelled

    statuses = {t["id"]: t["status"] for t in dispatch.task_list(project)}
    assert set(statuses) == {"T101"}  # only the active revision is listed
    assert statuses["T101"] == "runnable"

    # T001 stays passed (terminal states are not rewritten), T002 cancelled.
    goal_row = project.store.goal_active()
    run = project.store.run_for_goal(goal_row.id)
    old = [r for r in [project.store.revision_active(run.id)]]
    superseded = project.store.conn.execute(
        "SELECT * FROM plan_revisions WHERE run_id = ? ORDER BY revision", (run.id,)
    ).fetchall()
    assert [s["status"] for s in superseded] == ["superseded", "active"]
    old_tasks = {
        r["task_id"]: r["status"]
        for r in project.store.conn.execute(
            "SELECT task_id, status FROM tasks WHERE revision_id = ?", (superseded[0]["id"],)
        )
    }
    assert old_tasks == {"T001": "passed", "T002": "cancelled"}


def test_replan_rejected_while_running(planned):
    project = planned
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    with pytest.raises(ReplanRejectedError) as excinfo:
        dispatch.plan_route(project)
    assert "T001" in str(excinfo.value)
    with pytest.raises(ReplanRejectedError):
        dispatch.submit_plan(project, ir_for(project.store.goal_active(), [
            task_spec("T101", acceptance=["marker file exists"]),
        ]))


def test_replan_rejected_while_verifying(planned, goal):
    project = planned
    # replace revision 1 with a single agent-verified task (correspondence
    # declared explicitly); the busy guard is what must reject below
    dispatch.submit_plan(project, with_replan(ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=["agent: looks fine"]),
    ]), 1, [
        replan_task_entry("T001", "continue", sources=[(1, "T001")]),
    ], [
        superseded_entry(1, "T001", "continued", successors=["T001"]),
        superseded_entry(1, "T002", "dropped", note="folded into the new T001"),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(project.root)))
    with pytest.raises(ReplanRejectedError):
        dispatch.plan_route(project)


def test_replan_allowed_when_only_waiting(planned):
    project = planned
    dispatch.run_slice(project)  # T001 -> waiting_host (not running)
    result = dispatch.plan_route(project)  # must not raise
    assert result["mode"] == "host_required"


def test_old_revision_tasks_do_not_participate(planned, goal, tmp_path):
    project = planned
    dispatch.submit_plan(project, with_replan(ir_for(goal, [
        task_spec("T201", acceptance=goal.acceptance[:1], verification=["true"]),
        task_spec("T202", acceptance=goal.acceptance[1:], verification=["true"]),
    ]), 1, [
        replan_task_entry("T201", "continue", sources=[(1, "T001")]),
        replan_task_entry("T202", "continue", sources=[(1, "T002")]),
    ], [
        superseded_entry(1, "T001", "continued", successors=["T201"]),
        superseded_entry(1, "T002", "continued", successors=["T202"]),
    ]))
    # claim/complete the NEW tasks only; old ids are gone from the active set
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T201")
    dispatch.task_complete(project, "T201", str(write_evidence(tmp_path)))
    dispatch.task_claim(project, "T202")
    dispatch.task_complete(project, "T202", str(write_evidence(tmp_path)))

    data = dispatch.status_data(project)
    assert data["run"]["status"] == "done"
    assert data["goal"]["status"] == "done"

    # old ids no longer resolve against the active revision
    with pytest.raises(NotFoundError):
        dispatch.task_claim(project, "T001")


def test_assignment_marked_submitted(planned, goal):
    project = planned
    result = dispatch.plan_route(project)
    assignment_id = result["assignment"]["id"]
    # Same task NUMBER on revision 2 is different work; the mapping declares
    # the correspondence explicitly instead of relying on the number.
    dispatch.submit_plan(project, with_replan(ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ]), 1, [
        replan_task_entry("T001", "continue", sources=[(1, "T001")]),
    ], [
        superseded_entry(1, "T001", "continued", successors=["T001"]),
        superseded_entry(1, "T002", "dropped", note="folded into the new T001"),
    ]))
    assignment = planned.store.assignment_get(assignment_id)
    assert assignment.status == "submitted"
    assert assignment.submitted_at is not None


def test_only_one_active_revision_per_run(planned):
    goal_row = planned.store.goal_active()
    run = planned.store.run_for_goal(goal_row.id)
    rows = planned.store.conn.execute(
        "SELECT revision, status FROM plan_revisions WHERE run_id = ? AND status = 'active'",
        (run.id,),
    ).fetchall()
    assert len(rows) == 1


def test_replan_after_done_reopens_only_on_activation(planned, goal, tmp_path):
    """A finished Run is reopened only when a new revision actually lands:
    routing the replan and failing the precheck both keep the recorded
    completion (G004 T003)."""
    project = planned
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T002")
    dispatch.task_complete(project, "T002", str(write_evidence(tmp_path)))
    assert dispatch.status_data(project)["run"]["status"] == "done"
    done_run = project.store.run_for_goal(goal.id)
    assert done_run.completed_at is not None

    # Routing the replan does NOT reopen anything.
    result = dispatch.plan_route(project)
    assert result["mode"] == "host_required"
    after_route = dispatch.status_data(project)
    assert after_route["run"]["status"] == "done"
    assert after_route["goal"]["status"] == "done"
    assert project.store.run_for_goal(goal.id).completed_at == done_run.completed_at

    # A replan rejected by the precheck does not reopen anything either.
    with pytest.raises(plan_mod.ReplanCheckFailed):
        dispatch.submit_plan(project, ir_for(goal, [
            task_spec("T101", acceptance=goal.acceptance),
        ]))
    after_reject = dispatch.status_data(project)
    assert after_reject["run"]["status"] == "done"
    assert after_reject["goal"]["status"] == "done"
    assert project.store.run_for_goal(goal.id).completed_at == done_run.completed_at

    # A valid replan lands and reopens the Goal (the Run leaves done because
    # the new revision has unfinished work).
    dispatch.submit_plan(project, with_replan(ir_for(goal, [
        task_spec("T101", acceptance=goal.acceptance, verification=["true"]),
    ]), 1, [
        replan_task_entry("T101", "redo",
                          sources=[(1, "T001"), (1, "T002")],
                          redo_reason="the completion must be re-proven on the new plan"),
    ], [
        superseded_entry(1, "T001", "merged", successors=["T101"]),
        superseded_entry(1, "T002", "merged", successors=["T101"]),
    ]))
    after_submit = dispatch.status_data(project)
    assert after_submit["goal"]["status"] == "active"
    assert after_submit["run"]["status"] != "done"


# ---------------------------------------------------------------------------
# G004 T003: the shared precheck gate and the atomic activation path


def _state_vector(project):
    """The full replaceable state: revisions, task statuses, assignments,
    attempts, replan storage — anything a botched activation could move."""
    store = project.store
    goal = store.goal_active() or store.goals_all()[-1]
    run = store.run_for_goal(goal.id)
    revisions = [
        (r["revision"], r["status"])
        for r in store.conn.execute(
            "SELECT revision, status FROM plan_revisions ORDER BY revision"
        ).fetchall()
    ]
    tasks = sorted(
        (r["task_id"], r["status"])
        for r in store.conn.execute(
            "SELECT t.task_id, t.status FROM tasks t"
            " JOIN plan_revisions pr ON pr.id = t.revision_id"
            " WHERE pr.run_id = ?", (run.id,),
        ).fetchall()
    )
    assignments = [(a.id, a.status) for a in store.assignments_all()]
    attempts = [(a.id, a.result, a.ended_at) for a in store.attempts_all()]
    reports = [(r.id, r.revision_id) for r in store.replan_reports_for_run(run.id)]
    return {
        "goal": goal.status, "run": (run.id, run.status),
        "revisions": revisions, "tasks": tasks,
        "assignments": assignments, "attempts": attempts, "reports": reports,
    }


def test_replan_submit_requires_declared_mapping(planned, goal):
    """A replan without an explicit correspondence is rejected — same-number
    inference is exactly what must NOT happen."""
    with pytest.raises(plan_mod.ReplanCheckFailed) as excinfo:
        dispatch.submit_plan(planned, ir_for(goal, [
            task_spec("T101", acceptance=goal.acceptance),
        ]))
    report = excinfo.value.report
    assert report["ok"] is False
    assert any(e["category"] == "mapping" for e in report["errors"])
    assert any("replan mapping missing" in e["message"] for e in report["errors"])
    # the original plan is untouched
    run = planned.store.run_for_goal(goal.id)
    assert planned.store.revision_active(run.id).revision == 1


def test_failed_precheck_preserves_plan_assignment_and_attempt(planned, goal, tmp_path):
    """Rejection keeps the original plan AND the planning assignment: the
    assignment stays waiting, the planner attempt stays open."""
    project = planned
    dispatch.run_slice(project)  # T001 -> waiting_host
    routed = dispatch.plan_route(project)
    assignment_id = routed["assignment"]["id"]
    before = _state_vector(project)

    plan_path = tmp_path / "replan.json"
    plan_path.write_text(json.dumps(ir_for(goal, [
        task_spec("T101", acceptance=goal.acceptance),
    ])))
    report = dispatch.plan_check_file(project, str(plan_path))
    assert report["ok"] is False
    assert report["prior_revision"] == 1

    with pytest.raises(plan_mod.ReplanCheckFailed):
        dispatch.submit_plan(project, json.loads(plan_path.read_text()))

    after = _state_vector(project)
    assert {k: after[k] for k in ("goal", "run", "revisions", "tasks", "attempts")} == {
        k: before[k] for k in ("goal", "run", "revisions", "tasks", "attempts")
    }
    # the planning assignment survived untouched
    assert project.store.assignment_get(assignment_id).status == "waiting_host"
    open_attempt = project.store.attempt_open_for_assignment(assignment_id)
    assert open_attempt is not None and open_attempt.ended_at is None
    # the failed prechecks were audited as unbound reports
    assert [rid for rid, _bound in after["reports"]] and all(
        bound is None for _rid, bound in after["reports"]
    )


def test_submit_reruns_precheck_when_source_status_changed(planned, goal, tmp_path):
    """A source that changed state after `plan check` is re-judged at submit
    time: a continue whose source went terminal is refused (核对来源版本)."""
    project = planned
    dispatch.run_slice(project)  # T001 waiting_host (non-terminal)
    ir = with_replan(ir_for(goal, [
        task_spec("T101", acceptance=goal.acceptance, verification=["true"]),
    ]), 1, [
        replan_task_entry("T101", "continue", sources=[(1, "T001")]),
    ], [
        superseded_entry(1, "T001", "continued", successors=["T101"]),
        superseded_entry(1, "T002", "dropped", note="out of scope this round"),
    ])
    plan_path = tmp_path / "replan.json"
    plan_path.write_text(json.dumps(ir))

    ok_report = dispatch.plan_check_file(project, str(plan_path))
    assert ok_report["ok"] is True
    assert ok_report["sources"] == [
        {"source": "1:T001", "recorded_status": "waiting_host",
         "classified_by": ["T101"]}
    ]

    # the source goes terminal between the check and the submit
    dispatch.task_claim(project, "T001")
    dispatch.task_fail(project, "T001", "the assumption broke")

    with pytest.raises(plan_mod.ReplanCheckFailed) as excinfo:
        dispatch.submit_plan(project, ir)
    assert any(
        e["category"] == "source" and "terminal" in e["message"]
        for e in excinfo.value.report["errors"]
    )
    # and the original plan is still the active one
    assert _state_vector(project)["revisions"] == [(1, "active")]


def test_activation_fault_leaves_no_partial_revision(planned, goal, tmp_path,
                                                     monkeypatch):
    """A fault inside the activation transaction rolls the whole activation
    back: no revision 2, no cancelled tasks, no mapping, no report row."""
    project = planned
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    before = _state_vector(project)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full mid-activation")

    monkeypatch.setattr(Store, "replan_mapping_save", boom)
    with pytest.raises(RuntimeError):
        dispatch.submit_plan(project, _renumbered_plan(goal))
    monkeypatch.undo()

    assert _state_vector(project) == before
    # specifically: the old revision stayed active with its statuses intact
    run = project.store.run_for_goal(goal.id)
    active = project.store.revision_active(run.id)
    assert active.revision == 1
    statuses = {t.task_id: t.status for t in project.store.tasks_all(active.id)}
    assert statuses == {"T001": "passed", "T002": "runnable"}  # nothing cancelled


def test_successful_replan_persists_mapping_and_report(planned, goal, tmp_path):
    """Activation persists the declared mapping and binds the precheck report
    to the revision that landed."""
    project = planned
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))

    result = dispatch.submit_plan(project, _renumbered_plan(goal))
    assert result["replan_activation"]["prior_revision"] == 1
    assert result["replan_activation"]["report"] is not None
    assert result["replan_activation"]["classifications"] == {
        "new": 0, "confirm": 0, "redo": 0, "continue": 1,
    }

    run = project.store.run_for_goal(goal.id)
    revision = project.store.revision_active(run.id)
    mapping = project.store.replan_mapping_for(revision.id)
    assert mapping is not None
    assert mapping.prior_revision == 1
    assert [(t.task_id, t.classification) for t in mapping.tasks] == [
        ("T101", "continue"),
    ]
    assert [(s.source_revision, s.source_task_id) for s in mapping.tasks[0].sources] == [
        (1, "T002"),
    ]
    assert [(s.source_task_id, s.disposition) for s in mapping.superseded] == [
        ("T001", "dropped"), ("T002", "continued"),
    ]
    reports = project.store.replan_reports_for_run(run.id)
    assert reports and reports[-1].revision_id == revision.id
    assert reports[-1].payload["ok"] is True


def test_replan_rejects_unknown_future_and_cross_run_sources(planned, goal,
                                                             tmp_path):
    """Sources must belong to this run's recorded history: an unknown task,
    a future revision, and another run's task are all refused by name."""
    project = planned
    run = project.store.run_for_goal(goal.id)

    cases = [
        ((1, "T999"), "not a task of run"),
        ((9, "T001"), "future revision"),
    ]
    for source, marker in cases:
        ir = with_replan(ir_for(goal, [
            task_spec("T101", acceptance=goal.acceptance, verification=["true"]),
        ]), 1, [
            replan_task_entry("T101", "redo", sources=[source],
                              redo_reason="why"),
        ], [
            superseded_entry(1, "T001", "redone", successors=["T101"]),
            superseded_entry(1, "T002", "dropped", note="not needed"),
        ])
        with pytest.raises(plan_mod.ReplanCheckFailed) as excinfo:
            dispatch.submit_plan(project, ir)
        assert any(
            e["category"] == "source" and marker in e["message"]
            for e in excinfo.value.report["errors"]
        ), (source, excinfo.value.report["errors"])
    assert project.store.revision_active(run.id).revision == 1

    # cross-run: a finished run's task is another run's history, not ours
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T002")
    dispatch.task_complete(project, "T002", str(write_evidence(tmp_path, "e2.json")))
    assert dispatch.status_data(project)["run"]["status"] == "done"

    goal2 = dispatch.create_goal(
        project, objective="Ship the second thing",
        acceptance=["second marker exists"], constraints=[], context="",
    )[0]
    dispatch.submit_plan(project, ir_for(goal2, [
        task_spec("T009", acceptance=["second marker exists"], verification=["true"]),
    ]))
    cross = with_replan(ir_for(goal2, [
        task_spec("T010", acceptance=["second marker exists"], verification=["true"]),
    ]), 1, [
        replan_task_entry("T010", "redo", sources=[(1, "T001")],
                          redo_reason="re-use the old result"),
    ], [
        superseded_entry(1, "T009", "dropped", note="superseded approach"),
    ])
    with pytest.raises(plan_mod.ReplanCheckFailed) as excinfo:
        dispatch.submit_plan(project, cross)
    assert any(
        "R001" in e["message"] and "cross-run" in e["message"]
        for e in excinfo.value.report["errors"]
    )


def test_replan_rejects_confirm_of_unfinished_source(planned, goal):
    """Declaring an unfinished source as completed work (confirm) is refused."""
    project = planned
    dispatch.run_slice(project)  # T001 only waiting_host
    ir = with_replan(ir_for(goal, [
        task_spec("T101", acceptance=goal.acceptance, verification=["true"]),
    ]), 1, [
        replan_task_entry("T101", "confirm", sources=[(1, "T001")],
                          confirm_verification=["true"]),
    ], [
        superseded_entry(1, "T001", "confirmed", successors=["T101"]),
        superseded_entry(1, "T002", "dropped", note="not needed"),
    ])
    with pytest.raises(plan_mod.ReplanCheckFailed) as excinfo:
        dispatch.submit_plan(project, ir)
    assert any(
        e["category"] == "source" and "not passed" in e["message"]
        for e in excinfo.value.report["errors"]
    )


def test_replan_artifact_bindings_resolve_through_correspondence(planned, goal,
                                                                 tmp_path):
    """Artifacts that name recorded evidence bind to the DECLARED source: an
    artifact naming another task's evidence is a mis-bound reference."""
    project = planned
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    e1 = write_evidence(tmp_path, "e-t1.json")
    dispatch.task_complete(project, "T001", str(e1))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T002")
    e2 = write_evidence(tmp_path, "e-t2.json")
    dispatch.task_complete(project, "T002", str(e2))

    def replan(artifacts):
        return with_replan(ir_for(goal, [
            task_spec("T101", acceptance=goal.acceptance, verification=["true"]),
        ]), 1, [
            replan_task_entry("T101", "confirm", sources=[(1, "T001")],
                              confirm_verification=["true"],
                              artifacts=artifacts),
        ], [
            superseded_entry(1, "T001", "confirmed", successors=["T101"]),
            superseded_entry(1, "T002", "dropped", note="not needed this round"),
        ])

    good_path = tmp_path / "good.json"
    good_path.write_text(json.dumps(replan([str(e1)])))
    report = dispatch.plan_check_file(project, str(good_path))
    assert report["ok"] is True
    [artifact] = report["correspondence"][0]["artifacts"]
    assert artifact["status"] == "source-evidence"
    assert "1:T001" in artifact["detail"]
    assert report["reference_issues"] == []

    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(replan([str(e2)])))
    report = dispatch.plan_check_file(project, str(bad_path))
    assert report["ok"] is False
    assert report["reference_issues"] and report["reference_issues"][0]["task"] == "T101"
    assert any(
        e["category"] == "artifact" and "1:T002" in e["message"]
        for e in report["errors"]
    )
    with pytest.raises(plan_mod.ReplanCheckFailed):
        dispatch.submit_plan(project, json.loads(bad_path.read_text()))


def test_plan_check_report_shows_correspondence_and_diff(planned, goal, tmp_path):
    """The success report carries the renumbered correspondence, the four
    classifications, redo reasons, dispositions, and the contract diff."""
    project = planned
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))

    ir = with_replan(ir_for(goal, [
        task_spec("T101", acceptance=goal.acceptance[:1], verification=["true"]),
        task_spec("T102", deps=["T101"], acceptance=goal.acceptance[1:],
                  verification=["true"]),
        task_spec("T103", acceptance=goal.acceptance[1:],
                  verification=["true"]),
    ]), 1, [
        replan_task_entry("T101", "confirm", sources=[(1, "T001")],
                          confirm_verification=["true"]),
        replan_task_entry("T102", "redo", sources=[(1, "T002")],
                          redo_reason="the old approach cannot produce the summary"),
        replan_task_entry("T103", "new"),
    ], [
        superseded_entry(1, "T001", "confirmed", successors=["T101"]),
        superseded_entry(1, "T002", "redone", successors=["T102"]),
    ])
    plan_path = tmp_path / "replan.json"
    plan_path.write_text(json.dumps(ir))

    before = _state_vector(project)
    report = dispatch.plan_check_file(project, str(plan_path))
    assert report["ok"] is True
    assert report["is_replan"] is True
    assert report["prior_revision"] == 1
    assert report["proposed_revision"] == 2
    assert report["classifications"] == {
        "new": ["T103"], "confirm": ["T101"], "redo": ["T102"], "continue": [],
    }
    assert report["renumbered"] == [
        {"from": "1:T001", "to": "T101"}, {"from": "1:T002", "to": "T102"},
    ]
    assert report["redos"] == [
        {"task": "T102", "reason": "the old approach cannot produce the summary"},
    ]
    by_prior = {row["prior"]: row for row in report["superseded"]}
    assert by_prior["1:T001"]["disposition"] == "confirmed"
    assert by_prior["1:T001"]["recorded_status"] == "passed"
    assert by_prior["1:T002"]["disposition"] == "redone"
    assert by_prior["1:T002"]["recorded_status"] == "runnable"
    assert report["contract_diff"]["would_cancel"] == ["T002"]
    assert report["contract_diff"]["terminal_preserved"] == {"T001": "passed"}
    covered = {c["criterion"]: c["tasks"] for c in
               report["contract_diff"]["acceptance_coverage"]}
    assert set(covered) == set(goal.acceptance)
    # read-only: nothing moved
    after = _state_vector(project)
    assert {k: after[k] for k in ("goal", "run", "revisions", "tasks", "assignments",
                                  "attempts")} == {
        k: before[k] for k in ("goal", "run", "revisions", "tasks", "assignments",
                               "attempts")}
