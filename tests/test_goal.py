"""Goal model: creation, single-active invariant, persistence."""

from __future__ import annotations

import json

import pytest

from orx import dispatch
from orx.records import GoalStatus, ORXError

from conftest import ir_for, task_spec, write_evidence


def test_goal_create_makes_goal_and_run(project):
    goal, run = dispatch.create_goal(
        project, "fix the bug", ["bug is fixed"], ["no new deps"], "context here"
    )
    assert goal.id == "G001"
    assert goal.status == "active"
    assert goal.acceptance == ["bug is fixed"]
    assert goal.constraints == ["no new deps"]
    assert run.id == "R001"
    assert run.status == "planning"
    assert run.goal_id == goal.id


def test_second_active_goal_refused(project):
    dispatch.create_goal(project, "first", ["a1"])
    with pytest.raises(ORXError) as excinfo:
        dispatch.create_goal(project, "second", ["a2"])
    assert "already active" in str(excinfo.value)


def test_goal_requires_objective_and_acceptance(project):
    with pytest.raises(ORXError):
        dispatch.create_goal(project, "  ", ["a"])
    with pytest.raises(ORXError):
        dispatch.create_goal(project, "objective", [])


def test_goal_persists_across_reopen(project, tmp_path):
    goal, run = dispatch.create_goal(project, "persist me", ["p1"])
    project.close()

    reopened = dispatch.open_project()
    try:
        shown = dispatch.goal_show(reopened)
        assert shown["goal"]["id"] == goal.id
        assert shown["goal"]["acceptance"] == ["p1"]
        assert shown["run"]["id"] == run.id
    finally:
        reopened.close()


def test_goal_done_at_run_completion(project, goal, tmp_path):
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    assert project.store.goal_active() is None  # no longer active: it is done
    stored = project.store.goal_get(goal.id)
    assert stored.status == "done"


# -- Goal pause / resume (G010 T002) ------------------------------------------


def test_goal_pause_persists_across_reopen_and_blocks_second_goal(project, tmp_path):
    goal, run = dispatch.create_goal(
        project, "pausable", ["p1"], [], ""
    )
    dispatch.goal_pause(project)
    assert project.store.goal_get(goal.id).status == "paused"
    project.close()

    reopened = dispatch.open_project()
    try:
        # paused survives the reopen: it is database state, not session state
        assert reopened.store.goal_get(goal.id).status == "paused"
        assert reopened.store.goal_active() is None  # paused is not active

        # a paused Goal is still unfinished: no second unfinished Goal
        with pytest.raises(ORXError) as excinfo:
            dispatch.create_goal(reopened, "second", ["a2"])
        assert "paused" in str(excinfo.value)
        assert reopened.store.goals_all()[0].id == goal.id  # nothing was created

        # goal show keeps working while paused
        shown = dispatch.goal_show(reopened)
        assert shown["goal"]["id"] == goal.id
        assert shown["goal"]["status"] == "paused"
        assert shown["run"]["id"] == run.id

        resumed = dispatch.goal_resume(reopened)
        assert resumed["status"] == "active" and resumed["changed"] is True
        assert reopened.store.goal_get(goal.id).status == "active"
    finally:
        reopened.close()


def test_goal_pause_resume_cli_text_and_json_interfaces(project, goal, tmp_path):
    from typer.testing import CliRunner
    from orx.cli import app

    runner = CliRunner()
    env = {"ORX_PROJECT": str(project.root)}

    # JSON: first pause reports the transition
    out = runner.invoke(app, ["goal", "pause", "--json"], env=env)
    assert out.exit_code == 0, out.stdout
    body = json.loads(out.stdout)
    assert body["ok"] is True
    assert body["goal"] == goal.id
    assert body["status"] == "paused"
    assert body["changed"] is True
    assert body["run"]["id"] == "R001"

    # JSON: repeated pause is an explicit idempotent result, still exit 0
    again = runner.invoke(app, ["goal", "pause", "--json"], env=env)
    assert again.exit_code == 0, again.stdout
    repeat = json.loads(again.stdout)
    assert repeat["ok"] is True
    assert repeat["status"] == "paused"
    assert repeat["changed"] is False

    # Text: human lines, no JSON envelope
    text = runner.invoke(app, ["goal", "pause"], env=env)
    assert text.exit_code == 0, text.stdout
    assert "paused" in text.stdout
    assert "already in that state" in text.stdout

    # JSON resume: back to active
    out = runner.invoke(app, ["goal", "resume", "--json"], env=env)
    assert out.exit_code == 0, out.stdout
    body = json.loads(out.stdout)
    assert body["ok"] is True
    assert body["status"] == "active"
    assert body["changed"] is True
    assert body["in_flight"] == {"planning": [], "parked": [], "running": [], "verifying": []}

    # repeated resume: idempotent, exit 0
    again = runner.invoke(app, ["goal", "resume", "--json"], env=env)
    assert again.exit_code == 0, again.stdout
    assert json.loads(again.stdout)["changed"] is False

    # a done Goal refuses both, exit 1
    project.store.goal_set_status(goal.id, GoalStatus.DONE)
    done = runner.invoke(app, ["goal", "pause", "--json"], env=env)
    assert done.exit_code == 1
    assert "cannot pause" in json.loads(done.stdout)["error"]
    done = runner.invoke(app, ["goal", "resume", "--json"], env=env)
    assert done.exit_code == 1
    assert "cannot resume" in json.loads(done.stdout)["error"]


def test_goal_pause_resume_payload_carries_in_flight_ownership(project, goal):
    from conftest import ir_for, task_spec

    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance,
                  verification=["agent: honest work"]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    (project.root / "t1.marker").write_text("done")
    dispatch.task_complete(project, "T001", str(write_evidence(project.root)))
    dispatch.verify_dispatch(project)

    paused = dispatch.goal_pause(project)
    parked = paused["in_flight"]["verifying"]
    assert len(parked) == 1 and parked[0]["task"] == "T001"
    verifier = parked[0]["verifiers"][0]
    assert verifier["nonce"].startswith("orx-assignment:")

    # pause does not touch the verifier attempt's identity
    revision = project.store.revision_active("R001")
    open_attempt = project.store.attempt_open_verifier_for_entry(
        revision.id, "T001", "agent: honest work")
    assert open_attempt.id == verifier["attempt"]
    assert open_attempt.nonce == verifier["nonce"]


# -- Goal cancel (G010 T003) ---------------------------------------------------


def test_goal_cancel_from_planning_converges_and_persists(project, tmp_path):
    """The simplest cancel: a Goal still in planning (no revision) cancels
    outright — cancelling persists across reopen as its own state until
    convergence, and converged cancelled Goals still answer goal show."""
    goal = dispatch.create_goal(project, "cancel me", ["c1"])[0]
    out = dispatch.goal_cancel(project)
    assert out["changed"] is True and out["status"] == "cancelled"
    assert out["converged"] is True and out["unconfirmed"] == []
    assert project.store.goal_get(goal.id).status == "cancelled"
    assert project.store.goal_active() is None

    project.close()
    reopened = dispatch.open_project()
    try:
        assert reopened.store.goal_get(goal.id).status == "cancelled"
        assert reopened.store.schema_version() == 15
        # reads keep working on the cancelled Goal
        shown = dispatch.goal_show(reopened)
        assert shown["goal"]["id"] == goal.id
        assert shown["goal"]["status"] == "cancelled"
        # and the slot is free again: a new Goal is legal
        goal2 = dispatch.create_goal(reopened, "after cancel", ["n1"])[0]
        assert goal2.id == "G002"
    finally:
        reopened.close()


def test_goal_cancel_cli_text_and_json_interfaces(project, goal):
    from typer.testing import CliRunner
    from orx.cli import app

    runner = CliRunner()
    env = {"ORX_PROJECT": str(project.root)}

    out = runner.invoke(app, ["goal", "cancel", "--json"], env=env)
    assert out.exit_code == 0, out.stdout
    body = json.loads(out.stdout)
    assert body["ok"] is True
    assert body["goal"] == goal.id
    assert body["status"] == "cancelled"  # planning-only Goal converges now
    assert body["changed"] is True

    # repeated cancel: idempotent, exit 0, no re-run
    again = runner.invoke(app, ["goal", "cancel", "--json"], env=env)
    assert again.exit_code == 0, again.stdout
    repeat = json.loads(again.stdout)
    assert repeat["changed"] is False and repeat["status"] == "cancelled"
    text_repeat = runner.invoke(app, ["goal", "cancel"], env=env)
    assert text_repeat.exit_code == 0
    assert "cancel already requested" in text_repeat.stdout

    # human rendering of an unconfirmed wrap-up: create a second Goal with a
    # running task so the cancel lists the unconfirmed execution
    goal2 = dispatch.create_goal(project, "with a runner", ["r1"])[0]
    from conftest import ir_for, task_spec
    dispatch.submit_plan(project, ir_for(goal2, [
        task_spec("T001", acceptance=goal2.acceptance, verification=["true"]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    text = runner.invoke(app, ["goal", "cancel"], env=env)
    assert text.exit_code == 0, text.stdout
    assert "cancel requested" in text.stdout
    assert "cancelling" in text.stdout
    assert "unconfirmed executions" in text.stdout
    assert "orx task observe T001" in text.stdout
    assert "silence will NOT close it" in text.stdout

    # status lists the unconfirmed execution while cancelling
    status = runner.invoke(app, ["status", "--json"], env=env)
    assert status.exit_code == 0, status.stdout
    payload = json.loads(status.stdout)
    assert payload["goal"]["status"] == "cancelling"
    assert [e["task"] for e in payload["cancellation"]["unconfirmed"]] == ["T001"]

    # a done Goal refuses the cancel, exit 1
    dispatch.task_fail(project, "T001", "closed for the test")
    assert project.store.goal_get(goal2.id).status == "cancelled"
    goal3 = dispatch.create_goal(project, "finish me", ["f1"])[0]
    from conftest import ir_for as _ir
    dispatch.submit_plan(project, _ir(goal3, [
        task_spec("T001", acceptance=goal3.acceptance, verification=["true"]),
    ]))
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    dispatch.task_complete(project, "T001", str(write_evidence(project.root)))
    assert project.store.goal_get(goal3.id).status == "done"
    done = runner.invoke(app, ["goal", "cancel", "--json"], env=env)
    assert done.exit_code == 1
    assert "cannot cancel from done" in json.loads(done.stdout)["error"]


# -- operation cookbook pins (G010 T006) ---------------------------------------


def test_goal_lifecycle_doc_provides_the_operation_cookbook():
    """docs/goal-lifecycle.md carries a worked example for every lifecycle
    operation an operator needs: pause, resume, cancel, confirm-termination
    (recover --termination-fact), re-verification after a permission fix,
    and accepted-delivery retransmission — each as a real command with its
    expected envelope shape, not prose only."""
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent
            / "docs" / "goal-lifecycle.md").read_text()
    assert "## Operation cookbook" in text
    sections = {
        "pause": ("### Pause", "orx goal pause --json"),
        "resume": ("### Resume", "orx goal resume --json"),
        "cancel": ("### Cancel", "orx goal cancel --json"),
        "confirm termination": (
            "### Confirm termination", "orx task recover T001"
            " --termination-fact"),
        "re-verify": ("### Re-verify", "orx task reverify T001"),
        "retransmit": ("### Retransmit", "orx task complete T001"),
    }
    for label, (heading, command) in sections.items():
        assert heading in text, label
        assert command in text, label
    # the honest semantics ride along with the examples
    assert "changed: false" in text          # idempotent repeats
    assert "never a second executor" in text
    assert "never concluded from silence" in text
    assert "writes\nnothing" in text or "writes nothing" in text  # replay
    # the cookbook points at its cross-process rehearsal
    assert "tests/test_host_progress_e2e.py" in text
