"""G004 stage 5: end-to-end replan regression on local stand-ins only.

One file walks the WHOLE replan contract through the dispatch API in
throwaway projects — no paid model is ever called; the host test process is
the worker, `true` / `test -f` are the checks:

- the full lifecycle (renumbered confirm, same-number-different-work, new
  and unfinished-continue, split, merge, redo with an explicit reason)
  asserting, at ONE revision switch, all four dimensions the goal names:
  the precheck DIFF REPORT (`orx plan check --file`, read-only), the
  PERSISTED reference chain (mapping rows, trace hops, artifact
  provenance bound to the source's own attempt/evidence identity), the
  ATOMIC revision switch (one active revision, unfinished cancelled,
  terminal facts preserved, report bound to the revision that landed),
  and INDEPENDENT VERIFICATION of every new task (runnable start, empty
  verification window, pass only through its own gate);
- a multi-round trace where the same task number is reused for different
  work and the chain — and the artifact provenance — resolve only through
  the declared correspondence across three revisions;
- a FAILED precheck that leaves the old plan in effect: the original
  revision keeps executing to done after the rejection;
- a confirm task whose CURRENT regression is red: the source's old pass
  cannot carry it — the new task fails, and passes only once its own
  check is green again.

A final section pins README and both skills to the flow the machinery
actually implements: precheck before activation, artifacts cited through
the correspondence, concrete redo reasons, and no auto-inheritance or
unverified token-saving claims.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orx import dispatch, plan as plan_mod, verify
from orx.verify import DeliveryRejected

from conftest import (
    active_task,
    ir_for,
    replan_task_entry,
    superseded_entry,
    task_spec,
    with_replan,
    write_evidence,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _three_part_goal(project):
    return dispatch.create_goal(
        project,
        objective="Ship the layered tool",
        acceptance=["foundation file exists", "summary file exists",
                    "metrics file exists"],
        constraints=[],
        context="",
    )[0]


def _run_id(project, goal):
    return project.store.run_for_goal(goal.id).id


def _pass(project, tmp_path, task_id, evidence_name, write=None):
    """The host 'worker': claim, do the work, deliver structured evidence."""
    dispatch.run_slice(project)
    dispatch.task_claim(project, task_id)
    revision_id = active_task(project, task_id).revision_id
    if write is not None:
        (project.root / write).write_text("done\n")
    result = dispatch.task_complete(
        project, task_id, str(write_evidence(tmp_path, evidence_name)))
    assert result["status"] == "passed"
    # read via the task row: completing the last task flips the Goal to
    # done and `goal_active()` no longer resolves
    assert project.store.task_get(revision_id, task_id).status == "passed"
    return result


def _parked(project):
    """Park every runnable task once and index the fresh prompts by id."""
    entries = dispatch.run_slice(project)["host_required"]
    return {entry["task"]: entry for entry in entries}


# ---------------------------------------------------------------------------
# The full lifecycle: every classification and correspondence shape in one
# revision switch, asserting the diff report, the persisted chain, the
# atomic switch, and independent verification of each new task.


def test_lifecycle_renumber_confirm_split_merge_new_continue(project, tmp_path):
    goal = _three_part_goal(project)
    foundation, summary, metrics = goal.acceptance

    # ---- revision 1: four tasks finish, two stay unfinished ---------------
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", objective="lay the foundation", acceptance=[foundation],
                  verification=["test -f foundation.txt"]),
        task_spec("T002", objective="build the old parser", acceptance=[summary],
                  verification=["true"]),
        task_spec("T003", objective="generate the interim summary",
                  acceptance=[summary], verification=["true"]),
        task_spec("T004", objective="collect metrics", acceptance=[metrics],
                  verification=["true"]),
        task_spec("T005", objective="benchmark part A", acceptance=[metrics],
                  verification=["true"]),
        task_spec("T006", objective="benchmark part B", acceptance=[metrics],
                  verification=["true"]),
    ]))
    revision1 = active_task(project, "T001").revision_id
    _pass(project, tmp_path, "T001", "rev1-t001.json", write="foundation.txt")
    _pass(project, tmp_path, "T003", "rev1-t003.json")
    _pass(project, tmp_path, "T005", "rev1-t005.json")
    _pass(project, tmp_path, "T006", "rev1-t006.json")
    # T002 and T004 stay waiting_host — unfinished work the replan must
    # dispose of explicitly (one dropped, one continued).

    # The Controller routes the replan; the planning assignment waits.
    routed = dispatch.plan_route(project)
    assert routed["replan"] is True
    assignment_id = routed["assignment"]["id"]

    e1 = write_evidence(tmp_path, "rev1-t001.json")  # already recorded evidence
    e3 = write_evidence(tmp_path, "rev1-t003.json")
    redo_split_reason = ("the summary layout changed; the half of the report"
                         " that renders the new layout must be regenerated")
    redo_merge_reason = ("the two benchmark halves measured different builds;"
                         " one merged measurement must be re-run as a whole")

    # ---- revision 2: renumbered confirm, same-number NEW work, continue,
    # split (confirm + redo halves), merge (redo), and one plain new task.
    ir2 = with_replan(ir_for(goal, [
        task_spec("T101", objective="keep the foundation true",
                  acceptance=[foundation],
                  verification=["test -f foundation.txt"]),
        task_spec("T002", objective="write the summary directly (new work,"
                    " unrelated to the abandoned parser)",
                  acceptance=[summary], verification=["true"]),
        task_spec("T103", objective="finish collecting metrics",
                  acceptance=[metrics], verification=["true"]),
        task_spec("T105", objective="keep the stable half of the summary",
                  acceptance=[summary], verification=["true"]),
        task_spec("T106", objective="regenerate the changed half",
                  allowed=("src/", "tests/"), acceptance=[summary],
                  verification=["true"]),
        task_spec("T107", objective="run the merged benchmark",
                  allowed=("src/", "tests/"), acceptance=[metrics],
                  verification=["true"]),
        task_spec("T108", objective="wire the release notes",
                  acceptance=[summary], verification=["true"]),
    ]), 1, [
        replan_task_entry("T101", "confirm", sources=[(1, "T001")],
                          confirm_verification=["test -f foundation.txt"],
                          artifacts=[str(e1)]),
        replan_task_entry("T002", "new"),
        replan_task_entry("T103", "continue", sources=[(1, "T004")]),
        replan_task_entry("T105", "confirm", sources=[(1, "T003", True)],
                          confirm_verification=["true"], artifacts=[str(e3)]),
        replan_task_entry("T106", "redo", sources=[(1, "T003", True)],
                          redo_reason=redo_split_reason),
        replan_task_entry("T107", "redo", sources=[(1, "T005"), (1, "T006")],
                          redo_reason=redo_merge_reason),
        replan_task_entry("T108", "new"),
    ], [
        superseded_entry(1, "T001", "confirmed", successors=["T101"]),
        superseded_entry(1, "T002", "dropped",
                         note="the parser approach was abandoned; the number"
                              " T002 is reused below by unrelated new work —"
                              " the number itself implies no correspondence"),
        superseded_entry(1, "T003", "split", successors=["T105", "T106"]),
        superseded_entry(1, "T004", "continued", successors=["T103"]),
        superseded_entry(1, "T005", "merged", successors=["T107"]),
        superseded_entry(1, "T006", "merged", successors=["T107"]),
    ])

    # ---- the precheck BEFORE activation: the read-only diff report --------
    plan_path = tmp_path / "replan-2.json"
    plan_path.write_text(json.dumps(ir2))
    report = dispatch.plan_check_file(project, str(plan_path))
    assert report["ok"] is True
    assert report["is_replan"] is True
    assert report["prior_revision"] == 1
    assert report["proposed_revision"] == 2
    assert report["classifications"] == {
        "new": ["T002", "T108"], "confirm": ["T101", "T105"],
        "redo": ["T106", "T107"], "continue": ["T103"],
    }
    # renumbering is traced in the report: numbers moved, the pairs did not
    assert report["renumbered"] == [
        {"from": "1:T001", "to": "T101"},
        {"from": "1:T004", "to": "T103"},
        {"from": "1:T003", "to": "T105"},
        {"from": "1:T003", "to": "T106"},
        {"from": "1:T005", "to": "T107"},
        {"from": "1:T006", "to": "T107"},
    ]
    # a redo without its reason is structurally impossible; the reason rides
    # the report verbatim — specific, not boilerplate
    assert report["redos"] == [
        {"task": "T106", "reason": redo_split_reason},
        {"task": "T107", "reason": redo_merge_reason},
    ]
    by_prior = {row["prior"]: row for row in report["superseded"]}
    assert [(p, by_prior[p]["disposition"], by_prior[p]["recorded_status"])
            for p in ("1:T001", "1:T002", "1:T003", "1:T004", "1:T005", "1:T006")] == [
        ("1:T001", "confirmed", "passed"),
        ("1:T002", "dropped", "waiting_host"),
        ("1:T003", "split", "passed"),
        ("1:T004", "continued", "waiting_host"),
        ("1:T005", "merged", "passed"),
        ("1:T006", "merged", "passed"),
    ]
    # the contract diff: what activation would change vs preserve
    assert report["contract_diff"]["would_cancel"] == ["T002", "T004"]
    assert report["contract_diff"]["terminal_preserved"] == {
        "T001": "passed", "T003": "passed", "T005": "passed", "T006": "passed",
    }
    # artifacts resolve through the DECLARED correspondence only
    corr = {row["task"]: row for row in report["correspondence"]}
    [a1] = corr["T101"]["artifacts"]
    assert a1["status"] == "source-evidence" and "1:T001" in a1["detail"]
    [a3] = corr["T105"]["artifacts"]
    assert a3["status"] == "source-evidence" and "1:T003" in a3["detail"]
    assert report["reference_issues"] == []
    # read-only: the plan check moved nothing, the assignment still waits
    assert project.store.revision_active(_run_id(project, goal)).revision == 1
    assert project.store.assignment_get(assignment_id).status == "waiting_host"

    # ---- activation: the atomic revision switch ---------------------------
    result = dispatch.submit_plan(project, json.loads(plan_path.read_text()))
    assert result["revision"] == 2
    assert result["superseded_revision"] == 1
    assert result["cancelled_tasks"] == ["T002", "T004"]
    assert result["replan_activation"]["classifications"] == {
        "new": 2, "confirm": 2, "redo": 2, "continue": 1,
    }
    assert project.store.assignment_get(assignment_id).status == "submitted"

    run_id = _run_id(project, goal)
    revision2 = project.store.revision_active(run_id)
    # one active revision; old statuses: terminal facts kept, unfinished cancelled
    assert [(r["revision"], r["status"]) for r in project.store.conn.execute(
        "SELECT revision, status FROM plan_revisions WHERE run_id = ?"
        " ORDER BY revision", (run_id,)
    ).fetchall()] == [(1, "superseded"), (2, "active")]
    old_statuses = {
        r["task_id"]: r["status"]
        for r in project.store.conn.execute(
            "SELECT task_id, status FROM tasks WHERE revision_id = ?",
            (revision1,))
    }
    assert old_statuses == {
        "T001": "passed", "T002": "cancelled", "T003": "passed",
        "T004": "cancelled", "T005": "passed", "T006": "passed",
    }

    # every new task starts from scratch: runnable, empty verification window
    statuses = {t["id"]: t["status"] for t in dispatch.task_list(project)}
    assert statuses == {"T101": "runnable", "T002": "runnable",
                        "T103": "runnable", "T105": "runnable",
                        "T106": "runnable", "T107": "runnable",
                        "T108": "runnable"}
    for task_id in statuses:
        assert project.store.verifications_current(revision2.id, task_id) == []

    # the declared mapping is persisted with the split's part flags and the
    # same-number-different-work fact (new T002 has NO sources)
    mapping = project.store.replan_mapping_for(revision2.id)
    assert mapping.prior_revision == 1
    assert [(t.task_id, t.classification) for t in mapping.tasks] == [
        ("T101", "confirm"), ("T002", "new"), ("T103", "continue"),
        ("T105", "confirm"), ("T106", "redo"), ("T107", "redo"),
        ("T108", "new"),
    ]
    sources_by_task = {
        t.task_id: [(s.source_revision, s.source_task_id, s.part)
                    for s in t.sources]
        for t in mapping.tasks
    }
    assert sources_by_task["T101"] == [(1, "T001", False)]
    assert sources_by_task["T002"] == []          # the number matches nothing
    assert sources_by_task["T105"] == [(1, "T003", True)]   # split part
    assert sources_by_task["T106"] == [(1, "T003", True)]   # split part
    assert sources_by_task["T107"] == [(1, "T005", False), (1, "T006", False)]
    assert [(s.source_task_id, s.disposition) for s in mapping.superseded] == [
        ("T001", "confirmed"), ("T002", "dropped"), ("T003", "split"),
        ("T004", "continued"), ("T005", "merged"), ("T006", "merged"),
    ]
    # the precheck report that gated the activation is bound to the revision
    # that landed; the earlier read-only audit stays unbound by name
    reports = project.store.replan_reports_for_run(run_id)
    assert len(reports) == 2
    assert all(r.payload["ok"] is True for r in reports)
    assert reports[0].revision_id is None            # read-only audit row
    assert reports[1].revision_id == revision2.id    # bound to what landed
    assert reports[1].id == result["replan_activation"]["report"]

    # the persisted chain answers the four correspondence questions directly
    def hops(rev, task_id):
        return [(s.from_revision, s.from_task_id, s.to_revision, s.to_task_id,
                 s.classification, s.part)
                for s in project.store.replan_trace_chain(run_id, rev, task_id)]
    assert hops(1, "T001") == [(1, "T001", 2, "T101", "confirm", False)]
    assert hops(1, "T003") == [(1, "T003", 2, "T105", "confirm", True),
                               (1, "T003", 2, "T106", "redo", True)]
    assert hops(1, "T004") == [(1, "T004", 2, "T103", "continue", False)]
    assert hops(1, "T005") == [(1, "T005", 2, "T107", "redo", False)]
    assert hops(1, "T006") == [(1, "T006", 2, "T107", "redo", False)]
    # the dropped old T002 — whose NUMBER was reused by new work — leaves no
    # hop anywhere: the number alone never created a correspondence
    assert hops(1, "T002") == []

    # ---- the prompts carry the classification semantics -------------------
    parked = _parked(project)
    assert set(parked) == set(statuses)

    confirm_prompt = parked["T101"]["prompt"]
    assert (project.root / parked["T101"]["prompt_file"]).read_text() \
        == confirm_prompt
    assert "a task number alone never implies correspondence" in confirm_prompt
    assert "- classification: confirm" in confirm_prompt
    assert ("  - 1:T001 (revision 1, task T001, part=false)"
            " — recorded status: passed") in confirm_prompt
    assert f"  - {e1} -> resolves to recorded source evidence: 1:T001" \
        in confirm_prompt
    assert "do NOT redo the work" in confirm_prompt
    assert "Current verification requirements for this confirmation" \
        in confirm_prompt
    assert "  * 'test -f foundation.txt'" in confirm_prompt

    redo_prompt = parked["T106"]["prompt"]
    assert f"Redo reason (why this work must be done again, from the" \
        f" replan declaration): {redo_split_reason}" in redo_prompt
    assert "Scope of the redo (the only paths this task may write): src/," \
        " tests/" in redo_prompt

    merge_prompt = parked["T107"]["prompt"]
    assert redo_merge_reason in merge_prompt
    assert ("  - 1:T005 (revision 1, task T005, part=false)"
            " — recorded status: passed") in merge_prompt
    assert ("  - 1:T006 (revision 1, task T006, part=false)"
            " — recorded status: passed") in merge_prompt

    continue_prompt = parked["T103"]["prompt"]
    # the unfinished source was cancelled BY this very activation; the chain
    # shows the recorded status as it stands now (the precheck report above
    # captured waiting_host at CHECK time — both are honest at their time)
    assert ("  - 1:T004 (revision 1, task T004, part=false)"
            " — recorded status: cancelled") in continue_prompt
    assert "finish the work and pass this task's own prescribed checks" \
        in continue_prompt

    # the same-numbered NEW T002 gets no correspondence block at all
    assert "Replan correspondence for this task" not in parked["T002"]["prompt"]

    # the verifier sees the same chain explicitly marked as recorded history
    verifier_block = dispatch._replan_reference_context(
        project.store, project.root, revision2.id, "T101", audience="verifier")
    assert "recorded HISTORY this task references" in verifier_block
    assert "never impersonates a current verification" in verifier_block

    # ---- the new plan executes: every task passes only its own gate -------
    # T101 (confirm): the foundation file is still there, so its own check is
    # green — the confirm passes through TODAY's check, not the old pass.
    dispatch.task_claim(project, "T101")
    delivered = dispatch.task_complete(
        project, "T101", str(write_evidence(tmp_path, "rev2-t101.json")))
    assert delivered["status"] == "passed"
    assert active_task(project, "T101").status == "passed"

    # the accepted delivery persisted the reference chain: provenance rows
    # cite the SOURCE task's own attempt and evidence identity
    [e1_row] = [r for r in project.store.evidence_rows_for_task(revision1, "T001")
                if r.kind == "completion"]
    [provenance] = project.store.replan_artifact_sources_for_task(
        revision2.id, "T101")
    assert (provenance.source_revision, provenance.source_task_id) == (1, "T001")
    assert provenance.artifact == str(e1)
    assert provenance.evidence_id == e1_row.id
    assert provenance.attempt_id == e1_row.attempt_id
    snapshot_doc = json.loads(
        (project.root / delivered["replan_delivery"]["snapshot"]).read_text())
    [snap_row] = snapshot_doc["artifacts"]
    assert snap_row["artifact"] == str(e1)
    assert snap_row["resolved_to_recorded_evidence"] is True
    assert snap_row["exists_at_delivery"] is True
    assert snap_row["digest_at_delivery"]

    # T105 (confirm, split part): its artifact provenance binds to 1:T003
    dispatch.task_claim(project, "T105")
    dispatch.task_complete(
        project, "T105", str(write_evidence(tmp_path, "rev2-t105.json")))
    [prov105] = project.store.replan_artifact_sources_for_task(
        revision2.id, "T105")
    assert (prov105.source_revision, prov105.source_task_id) == (1, "T003")

    # the rest — new, continue, the two redoes, plain new — same story
    for task_id in ("T002", "T103", "T106", "T107", "T108"):
        dispatch.task_claim(project, task_id)
        dispatch.task_complete(
            project, task_id,
            str(write_evidence(tmp_path, f"rev2-{task_id.lower()}.json")))
        assert project.store.task_get(
            revision2.id, task_id).status == "passed"

    data = dispatch.status_data(project)
    assert data["run"]["status"] == "done"
    assert data["goal"]["status"] == "done"
    assert data["verification"]["failed"] == 0
    # terminal facts survive the whole story
    assert old_statuses == {
        r["task_id"]: r["status"]
        for r in project.store.conn.execute(
            "SELECT task_id, status FROM tasks WHERE revision_id = ?",
            (revision1,))
    }


# ---------------------------------------------------------------------------
# Multi-round tracing: three revisions, one artifact, a reused task number.


def test_multi_round_trace_and_same_number_reuse(project, tmp_path):
    goal = dispatch.create_goal(
        project, objective="Ship the marker", acceptance=["marker file exists"],
        constraints=[], context="",
    )[0]
    criterion = goal.acceptance[0]

    # revision 1: T001 passes with evidence E1; T002 never starts.
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=[criterion], verification=["true"]),
        task_spec("T002", acceptance=[criterion], verification=["true"]),
    ]))
    revision1 = active_task(project, "T001").revision_id
    _pass(project, tmp_path, "T001", "round1.json")
    e1 = tmp_path / "round1.json"
    [e1_row] = [r for r in project.store.evidence_rows_for_task(revision1, "T001")
                if r.kind == "completion"]

    # revision 2: T001's work is confirmed by the RENUMBERED T101 citing E1;
    # the number T002 is reused by DIFFERENT work (classification new).
    dispatch.submit_plan(project, with_replan(ir_for(goal, [
        task_spec("T101", acceptance=[criterion], verification=["true"]),
        task_spec("T002", objective="unrelated new work under a reused number",
                  acceptance=[criterion], verification=["true"]),
    ]), 1, [
        replan_task_entry("T101", "confirm", sources=[(1, "T001")],
                          confirm_verification=["true"], artifacts=[str(e1)]),
        replan_task_entry("T002", "new"),
    ], [
        superseded_entry(1, "T001", "confirmed", successors=["T101"]),
        superseded_entry(1, "T002", "dropped",
                         note="the approach is abandoned; the reused number"
                              " implies nothing"),
    ]))
    revision2 = active_task(project, "T101").revision_id
    run_id = _run_id(project, goal)

    # the reused number created no hop; the renumbered confirm created one
    assert [(s.from_task_id, s.to_task_id) for s in
            project.store.replan_trace_chain(run_id, 1, "T002")] == []
    assert [(s.from_revision, s.from_task_id, s.to_revision, s.to_task_id)
            for s in project.store.replan_trace_chain(run_id, 1, "T001")] == [
        (1, "T001", 2, "T101"),
    ]

    # the rev-2 confirm delivers: provenance binds E1 to the source 1:T001
    # with the SOURCE's own attempt — not the confirming task's attempt.
    _pass(project, tmp_path, "T101", "round2-confirm.json")
    [prov2] = project.store.replan_artifact_sources_for_task(revision2, "T101")
    assert (prov2.source_revision, prov2.source_task_id) == (1, "T001")
    assert prov2.evidence_id == e1_row.id
    assert prov2.attempt_id == e1_row.attempt_id
    _pass(project, tmp_path, "T002", "round2-new.json")
    assert dispatch.status_data(project)["run"]["status"] == "done"

    # revision 3 (from a DONE run — it reopens on activation): T201 confirms
    # the immediate predecessor AND reaches back past prior_revision to the
    # original, so E1 still binds through a declared edge.
    dispatch.submit_plan(project, with_replan(ir_for(goal, [
        task_spec("T201", acceptance=[criterion], verification=["true"]),
    ]), 2, [
        replan_task_entry("T201", "confirm",
                          sources=[(2, "T101"), (1, "T001")],
                          confirm_verification=["true"], artifacts=[str(e1)]),
    ], [
        superseded_entry(2, "T101", "confirmed", successors=["T201"]),
        superseded_entry(2, "T002", "dropped",
                         note="finished in revision 2; not carried forward"),
    ]))
    revision3 = active_task(project, "T201").revision_id
    assert dispatch.status_data(project)["goal"]["status"] == "active"

    # the multi-round chain: renumbering hops AND the reach-back edge — all
    # through declared sources, three revisions deep.
    chain = [(s.from_revision, s.from_task_id, s.to_revision, s.to_task_id,
              s.classification)
             for s in project.store.replan_trace_chain(run_id, 1, "T001")]
    assert chain == [
        (1, "T001", 2, "T101", "confirm"),
        (1, "T001", 3, "T201", "confirm"),   # source reaching past prior_revision
        (2, "T101", 3, "T201", "confirm"),
    ]
    assert [(s.to_task_id, s.classification) for s in
            project.store.replan_trace_chain(run_id, 2, "T101")] == [
        ("T201", "confirm"),
    ]

    # the rev-3 prompt shows both sources, the artifact resolved through the
    # correspondence, and the UNCHANGED verdict against the rev-2 snapshot.
    parked = _parked(project)
    prompt = parked["T201"]["prompt"]
    assert ("  - 2:T101 (revision 2, task T101, part=false)"
            " — recorded status: passed") in prompt
    assert ("  - 1:T001 (revision 1, task T001, part=false)"
            " — recorded status: passed") in prompt
    assert f"  - {e1} -> resolves to recorded source evidence: 1:T001" in prompt
    assert ("state: file present, UNCHANGED since the delivery snapshot"
            " (revision 2, task T101, attempt") in prompt

    # the rev-3 delivery adds a SECOND provenance row citing the same source
    # and writes a second snapshot — nothing overwrote anything.
    _pass(project, tmp_path, "T201", "round3.json")
    for_source = project.store.replan_artifact_sources_for_source(
        run_id, 1, "T001")
    assert [(r.revision_id, r.task_id) for r in for_source] == [
        (revision2, "T101"), (revision3, "T201"),
    ]
    assert all(r.attempt_id == e1_row.attempt_id for r in for_source)
    snapshots = sorted(
        (project.root / r.path)
        for rev in (revision2, revision3)
        for tid in (("T101",) if rev == revision2 else ("T201",))
        for r in project.store.evidence_rows_for_task(rev, tid)
        if r.kind == "delivery_snapshot"
    )
    assert len(snapshots) == 2
    assert all(p.exists() for p in snapshots)
    assert dispatch.status_data(project)["run"]["status"] == "done"


# ---------------------------------------------------------------------------
# A failed precheck leaves the old plan in effect — it keeps EXECUTING.


def test_failed_precheck_old_plan_keeps_executing(project, tmp_path):
    goal = dispatch.create_goal(
        project, objective="Ship the tool",
        acceptance=["marker file exists", "summary is written"],
        constraints=[], context="",
    )[0]

    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance[:1],
                  verification=["test -f marker.txt"]),
        task_spec("T002", acceptance=goal.acceptance[1:],
                  verification=["true"]),
    ]))
    _pass(project, tmp_path, "T001", "e1.json", write="marker.txt")

    routed = dispatch.plan_route(project)
    assignment_id = routed["assignment"]["id"]

    # A replan that declares the UNFINISHED T002 as passed work (confirm):
    # the precheck must refuse it against recorded state.
    bad = with_replan(ir_for(goal, [
        task_spec("T101", acceptance=goal.acceptance, verification=["true"]),
    ]), 1, [
        replan_task_entry("T101", "confirm", sources=[(1, "T002")],
                          confirm_verification=["true"]),
    ], [
        superseded_entry(1, "T001", "dropped", note="not needed"),
        superseded_entry(1, "T002", "confirmed", successors=["T101"]),
    ])
    plan_path = tmp_path / "bad-replan.json"
    plan_path.write_text(json.dumps(bad))

    report = dispatch.plan_check_file(project, str(plan_path))
    assert report["ok"] is False
    assert report["prior_revision"] == 1
    assert any(e["category"] == "source" and "not passed" in e["message"]
               for e in report["errors"])

    with pytest.raises(plan_mod.ReplanCheckFailed) as excinfo:
        dispatch.submit_plan(project, json.loads(plan_path.read_text()))
    assert any(e["category"] == "source" for e in excinfo.value.report["errors"])

    run_id = _run_id(project, goal)
    # the original plan is still THE plan: revision 1 active, assignment
    # still waiting, both failed prechecks audited as unbound reports.
    assert project.store.revision_active(run_id).revision == 1
    assert project.store.assignment_get(assignment_id).status == "waiting_host"
    reports = project.store.replan_reports_for_run(run_id)
    assert reports and all(
        r.revision_id is None and r.payload["ok"] is False for r in reports)

    # and it keeps executing: the old revision's remaining task runs to done.
    _pass(project, tmp_path, "T002", "e2.json")
    data = dispatch.status_data(project)
    assert data["run"]["status"] == "done"
    assert data["goal"]["status"] == "done"
    old = {
        r["task_id"]: r["status"]
        for r in project.store.conn.execute(
            "SELECT task_id, status FROM tasks WHERE revision_id = ?",
            (project.store.revision_active(run_id).id,))
    }
    assert old == {"T001": "passed", "T002": "passed"}


# ---------------------------------------------------------------------------
# The confirm task whose CURRENT regression is red: the old pass cannot
# carry it; the new task fails — and later passes its OWN check.


def test_confirm_task_cannot_pass_when_regression_fails(project, tmp_path):
    goal = dispatch.create_goal(
        project, objective="Ship the marker",
        acceptance=["marker file exists"], constraints=[], context="",
    )[0]
    criterion = goal.acceptance[0]

    # revision 1: T001 passes — the regression ('test -f marker.txt') was
    # green at the time and the pass is a recorded terminal fact.
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=[criterion],
                  verification=["test -f marker.txt"]),
    ]))
    revision1 = active_task(project, "T001").revision_id
    _pass(project, tmp_path, "T001", "round1.json", write="marker.txt")
    e1 = tmp_path / "round1.json"
    assert dispatch.status_data(project)["run"]["status"] == "done"

    # revision 2: T101 confirms 1:T001 — with the SAME check as its current
    # verification requirement, exactly as the contract demands.
    dispatch.submit_plan(project, with_replan(ir_for(goal, [
        task_spec("T101", acceptance=[criterion],
                  verification=["test -f marker.txt"]),
    ]), 1, [
        replan_task_entry("T101", "confirm", sources=[(1, "T001")],
                          confirm_verification=["test -f marker.txt"],
                          artifacts=[str(e1)]),
    ], [
        superseded_entry(1, "T001", "confirmed", successors=["T101"]),
    ]))
    revision2 = active_task(project, "T101").revision_id

    # independent start: runnable, empty window — the old pass is NOT a
    # verification row of the new task.
    assert active_task(project, "T101").status == "runnable"
    assert project.store.verifications_current(revision2, "T101") == []

    # NOW the regression breaks: the marker file disappears.
    (project.root / "marker.txt").unlink()

    parked = _parked(project)
    prompt = parked["T101"]["prompt"]
    assert "do NOT redo the work" in prompt
    assert "run" in prompt and "necessary regression checks" in prompt
    assert "  * 'test -f marker.txt'" in prompt

    # the worker's own check sees the red regression (its NEW attempt's row)
    dispatch.task_claim(project, "T101")
    gate = dispatch.task_check(project, "T101")
    [red] = gate["results"]
    assert red["command"] == "test -f marker.txt"
    assert red["passed"] is False and red["exit_code"] == 1
    current = project.store.verifications_current(revision2, "T101")
    assert [(v.command, v.passed) for v in current if v.kind == "command"] == [
        ("test -f marker.txt", False),
    ]

    # a premature 'passed' claim is gate-rejected: the source's old pass
    # cannot carry the confirming task.
    with pytest.raises(DeliveryRejected) as excinfo:
        dispatch.task_complete(project, "T101",
                               str(write_evidence(tmp_path, "lie.json")))
    assert excinfo.value.kind == "gate"
    assert any(f["command"] == "test -f marker.txt"
               for f in excinfo.value.report["failures"])

    # the worker reports the failure it could not fix
    failed = write_evidence(
        tmp_path, "confirm-failed.json", status="failed",
        summary="the regression test -f marker.txt is red: the marker file"
                " this confirmation relies on is gone from the workspace",
        checks=[{"command": "test -f marker.txt", "exit_code": 1,
                 "log": red["log_path"]}],
    )
    result = dispatch.task_complete(project, "T101", str(failed))
    assert result["status"] == "failed"
    task_row = active_task(project, "T101")
    assert task_row.status == "failed"
    assert task_row.failure_reason.startswith(
        "delivery failed (worker-reported failure):")

    # the old pass stays a recorded fact of revision 1 — and ONLY there.
    assert project.store.task_get(revision1, "T001").status == "passed"
    assert verify.evaluate(project.store, revision2, task_row) == "failed"
    assert verify.first_failure(project.store, revision2, task_row)
    # no accepted delivery happened: no provenance row, no snapshot
    assert project.store.replan_artifact_sources_for_task(
        revision2, "T101") == []
    assert not list((project.root / ".orx" / "runs").rglob("T101-r02-*.json"))
    data = dispatch.status_data(project)
    assert data["run"]["status"] != "done"
    assert data["goal"]["status"] != "done"

    # the fix: restore what the regression checks, retry, and the SAME task
    # passes through its OWN now-green check — never through inheritance.
    dispatch.task_retry(project, "T101")
    _pass(project, tmp_path, "T101", "confirm-fixed.json", write="marker.txt")
    assert project.store.task_get(revision2, "T101").status == "passed"
    [prov] = project.store.replan_artifact_sources_for_task(
        revision2, "T101")
    assert (prov.source_revision, prov.source_task_id) == (1, "T001")
    assert dispatch.status_data(project)["run"]["status"] == "done"


# ---------------------------------------------------------------------------
# README and the skills teach the flow the machinery actually implements.


def _skill(name: str) -> str:
    return (REPO_ROOT / "skills" / name / "SKILL.md").read_text()


def test_docs_teach_the_real_replan_flow_and_its_boundaries():
    readme = (REPO_ROOT / "README.md").read_text()
    controller = _skill("orx-controller")
    agent = _skill("orx-agent")
    # markdown wraps lines; phrases must survive the wrapping
    flat = lambda text: " ".join(text.split())  # noqa: E731

    # The precheck-before-activation flow, in the controller skill and README.
    for text in map(flat, (readme, controller)):
        assert "orx plan check --file" in text       # read-only precheck
        assert "re-runs the same precheck" in text   # submit is the same gate
        assert "never by task number" in text        # artifacts via the mapping
        assert "semantic judgment" in text           # the review boundary
        assert "ever inherited" in text              # no auto-inheritance
        # ("never inherited" / "No passed state is ever inherited")
    for text in (readme, controller, agent):
        assert "12M" not in text                     # the withdrawn figure
    # the withdrawal itself is stated, once, where users read it
    assert "figure was withdrawn" in flat(readme)
    assert "claims no verified token savings" in flat(readme)

    # The planner-facing contract: declare the mapping, classify every task,
    # concrete redo reasons, current verification for confirms.
    agent_flat = flat(agent)
    for phrase in (
        "replan",
        "classification",
        "redo_reason",
        "confirm_verification",
        "same number",
        "never by task number",
    ):
        assert phrase in agent_flat, phrase
    # both skills keep teaching: nothing is auto-passed, a prior pass is
    # never the new task's verification.
    assert "Nothing in your plan is auto-passed" in agent_flat
    assert "never a verdict" in flat(controller)

    # The skills keep teaching the delivery contract (pinned also by
    # test_delivery_gate_e2e — kept here as the replan-era regression).
    for name in ("orx-agent", "orx-controller"):
        text = _skill(name)
        assert '"status": "passed"' in text
        for field in ("status", "checks", "artifacts", "summary"):
            assert f"`{field}`" in text, (name, field)
        for value in verify.DELIVERY_STATUSES:
            assert f"`{value}`" in text, (name, value)
        assert '"exit_code": 0' in text
