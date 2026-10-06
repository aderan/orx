"""CLI contract: --json envelopes, exit codes, and a CLI-driven fake lifecycle."""

from __future__ import annotations

import json
import re
import shutil
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orx import dispatch
from orx.cli import app

from conftest import HOST_CONFIG_TOML, HOST_PROFILES_TOML, ir_for, task_spec

runner = CliRunner()


@pytest.fixture
def cli_project(tmp_path, monkeypatch):
    monkeypatch.delenv("ORX_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    dispatch.init_project(tmp_path)
    (tmp_path / ".orx" / "config.toml").write_text(HOST_CONFIG_TOML)
    (tmp_path / ".orx" / "profiles.toml").write_text(HOST_PROFILES_TOML)
    return tmp_path


def invoke(*args):
    return runner.invoke(app, list(args))


def payload(result):
    return json.loads(result.stdout)


def test_version_envelope():
    result = invoke("version", "--json")
    assert result.exit_code == 0
    assert payload(result)["ok"] is True
    assert payload(result)["version"]


def test_init_creates_orx_layout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke("init", "--json")
    assert result.exit_code == 0
    body = payload(result)
    assert body["ok"] is True
    for name in (".orx/config.toml", ".orx/profiles.toml", ".orx/state.db"):
        assert (tmp_path / name).exists()
    # second init refuses to clobber
    result = invoke("init", "--json")
    assert result.exit_code == 1
    assert payload(result)["ok"] is False


def test_commands_outside_project_fail_with_envelope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke("status", "--json")
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False and "error" in body


def test_doctor_ok_on_valid_project(cli_project, monkeypatch):
    # Keep the environment deterministic: no external harness probes.
    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name: None if name in ("codex", "agent") else real_which(name)
    )
    result = invoke("doctor", "--json")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    names = {c["name"]: c for c in body["checks"]}
    assert names["config"]["state"] == "ok"
    assert names["state_db"]["state"] == "ok"
    assert names["codex"]["state"] == "warn"  # missing CLI is a warning, not failure


def test_doctor_fails_on_broken_config(cli_project):
    (cli_project / ".orx" / "config.toml").write_text("schema_version = 1\nnonsense = [\n")
    result = invoke("doctor", "--json")
    assert result.exit_code == 1


def test_goal_new_and_show_envelopes(cli_project):
    result = invoke("goal", "new", "--json",
                    "--objective", "ship it",
                    "--acceptance", "tests pass",
                    "--acceptance", "docs updated")
    assert result.exit_code == 0
    body = payload(result)
    assert body["goal"]["id"] == "G001"
    assert body["run"]["id"] == "R001"
    assert body["goal"]["acceptance"] == ["tests pass", "docs updated"]

    result = invoke("goal", "show", "--json")
    assert payload(result)["goal"]["id"] == "G001"


def test_plan_returns_host_assignment(cli_project):
    invoke("goal", "new", "--json", "--objective", "ship it", "--acceptance", "tests pass")
    result = invoke("plan", "--json")
    assert result.exit_code == 0
    body = payload(result)
    assert body["ok"] is True
    assert body["mode"] == "host_required"
    assert body["assignment"]["id"] == "P001"
    assert body["assignment"]["submit"].startswith("orx plan submit")
    assert "tests pass" in body["assignment"]["prompt"]


def test_plan_submit_rejects_invalid_plan_with_errors(cli_project):
    invoke("goal", "new", "--json", "--objective", "ship it", "--acceptance", "tests pass")
    plan_path = cli_project / "plan.json"
    plan_path.write_text(json.dumps({  # paraphrased acceptance: must be rejected
        "goal": "G001",
        "exploration": {}, "approach": {},
        "tasks": [{"id": "T001", "objective": "x", "dependencies": [],
                    "scope": {"allowed": ["src/"]}, "acceptance": ["the tests pass"],
                    "verification": [], "routing": {"complexity": "low"}}],
    }))
    result = invoke("plan", "submit", "--json", "--file", str(plan_path))
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False
    assert any("verbatim" in e for e in body["errors"])


# -- orx plan check: read-only replan precheck (G004 T003) -------------------


def _replan_cli_state(cli_project):
    """Revision 1 with T001 passed: the state a replan prechecks against."""
    invoke("goal", "new", "--json", "--objective", "ship it",
           "--acceptance", "marker exists", "--acceptance", "summary written")
    plan = ir_for(type("G", (), {"id": "G001"})(), [
        task_spec("T001", acceptance=["marker exists"], verification=["true"]),
        task_spec("T002", deps=["T001"], acceptance=["summary written"]),
    ])
    (cli_project / "plan.json").write_text(json.dumps(plan))
    assert invoke("plan", "submit", "--json", "--file", "plan.json").exit_code == 0
    invoke("run", "--json")
    invoke("task", "claim", "--json", "T001")
    (cli_project / "ev.json").write_text(
        json.dumps({"status": "passed", "summary": "done", "checks": [], "artifacts": []})
    )
    invoke("task", "complete", "--json", "T001", "--evidence", "ev.json")


def _write_replan_plan(cli_project, name, tasks_entries, superseded_entries,
                       tasks=None):
    from conftest import with_replan
    if tasks is None:
        tasks = [
            task_spec("T101", acceptance=["marker exists"], verification=["true"]),
            task_spec("T102", deps=["T101"], acceptance=["summary written"],
                      verification=["true"]),
        ]
    plan = with_replan(ir_for(type("G", (), {"id": "G001"})(), tasks), 1,
                       tasks_entries, superseded_entries)
    (cli_project / name).write_text(json.dumps(plan))
    return name


def test_plan_check_ok_envelope_and_human(cli_project):
    from conftest import replan_task_entry, superseded_entry
    _replan_cli_state(cli_project)
    # a waiting planning assignment exists: the read-only check must not
    # touch it (规划指派状态不变)
    assert payload(invoke("plan", "--json"))["mode"] == "host_required"
    name = _write_replan_plan(
        cli_project, "replan.json",
        [replan_task_entry("T101", "confirm", sources=[(1, "T001")],
                           confirm_verification=["true"]),
         replan_task_entry("T102", "redo", sources=[(1, "T002")],
                           redo_reason="the old summary approach cannot work")],
        [superseded_entry(1, "T001", "confirmed", successors=["T101"]),
         superseded_entry(1, "T002", "redone", successors=["T102"])],
    )

    result = invoke("plan", "check", "--json", "--file", name)
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    check = body["check"]
    assert check["ok"] is True
    assert check["is_replan"] is True
    assert check["prior_revision"] == 1 and check["proposed_revision"] == 2
    assert check["classifications"]["confirm"] == ["T101"]
    assert check["classifications"]["redo"] == ["T102"]
    assert check["summary"]["errors"] == 0

    # Read-only: the active revision, task statuses, Goal/Run statuses, and
    # the planning assignment state are all unchanged; the only write is one
    # unbound preflight report row.
    project = dispatch.open_project()
    try:
        run = project.store.run_for_goal("G001")
        revision = project.store.revision_active(run.id)
        assert revision.revision == 1
        statuses = {t.task_id: t.status for t in project.store.tasks_all(revision.id)}
        assert statuses["T001"] == "passed" and statuses["T002"] == "runnable"
        assignment = project.store.assignment_waiting(run.id)
        assert assignment is not None and assignment.status == "waiting_host"
        assert dispatch.status_data(project)["run"]["status"] == "running"
        reports = project.store.replan_reports_for_run(run.id)
        assert len(reports) == 1 and reports[0].revision_id is None
        assert reports[0].payload["ok"] is True
    finally:
        project.close()

    human = invoke("plan", "check", "--file", name)
    assert human.exit_code == 0
    for marker in ("result: OK", "T101 confirm", "T102 redo",
                   "superseded: 1:T001 (passed) -> confirmed",
                   "renumbered: 1:T002 -> T102",
                   "reference issues: none", "sources checked: 1:T001=passed"):
        assert marker in human.stdout, marker


def test_plan_check_failure_envelope_categories_and_exit_1(cli_project):
    _replan_cli_state(cli_project)
    # no mapping declared: the check reports the categorized reason and
    # keeps everything untouched
    plan = ir_for(type("G", (), {"id": "G001"})(), [
        task_spec("T101", acceptance=["marker exists", "summary written"]),
    ])
    (cli_project / "nomap.json").write_text(json.dumps(plan))

    result = invoke("plan", "check", "--json", "--file", "nomap.json")
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False
    assert any(e["category"] == "mapping" for e in body["check"]["errors"])
    assert body["errors"]
    assert body["check"]["prior_revision"] == 1

    human = invoke("plan", "check", "--file", "nomap.json")
    assert human.exit_code == 1
    assert "[mapping]" in human.stdout
    assert "REJECTED" in human.stdout
    assert "stays effective" in human.stdout

    project = dispatch.open_project()
    try:
        run = project.store.run_for_goal("G001")
        assert project.store.revision_active(run.id).revision == 1
    finally:
        project.close()


def test_plan_check_usage_error_exits_2(cli_project):
    assert invoke("plan", "check").exit_code == 2


def test_plan_submit_precheck_rejection_envelope(cli_project):
    _replan_cli_state(cli_project)
    invoke("plan", "--json")  # a waiting planning assignment exists
    plan = ir_for(type("G", (), {"id": "G001"})(), [
        task_spec("T101", acceptance=["marker exists", "summary written"]),
    ])
    (cli_project / "nomap.json").write_text(json.dumps(plan))

    result = invoke("plan", "submit", "--json", "--file", "nomap.json")
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False
    assert body["errors"]
    assert any(e["category"] == "mapping" for e in body["precheck"]["errors"])
    assert body["precheck"]["prior_revision"] == 1

    human = invoke("plan", "submit", "--file", "nomap.json")
    assert human.exit_code == 1
    assert "[mapping]" in human.output
    assert "remains effective" in human.output

    # the original plan and the planning assignment both survived
    project = dispatch.open_project()
    try:
        run = project.store.run_for_goal("G001")
        assert project.store.revision_active(run.id).revision == 1
        assignment = project.store.assignment_waiting(run.id)
        assert assignment is not None and assignment.status == "waiting_host"
        attempt = project.store.attempt_open_for_assignment(assignment.id)
        assert attempt is not None and attempt.ended_at is None
    finally:
        project.close()


def test_resource_commands_and_toml_untouched(cli_project):
    before = (cli_project / ".orx" / "profiles.toml").read_bytes()
    result = invoke("resource", "set", "--json", "host-worker", "constrained", "--note", "tight")
    assert result.exit_code == 0
    result = invoke("resource", "list", "--json")
    rows = payload(result)["resources"]
    row = next(r for r in rows if r["profile"] == "host-worker")
    assert row["status"] == "constrained"
    assert (cli_project / ".orx" / "profiles.toml").read_bytes() == before

    result = invoke("resource", "set", "--json", "ghost", "available")
    assert result.exit_code == 1


def test_profiles_lists_definitions(cli_project):
    result = invoke("profiles", "--json")
    rows = payload(result)["profiles"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["host-vision"]["capabilities"] == ["coding", "vision"]
    assert by_name["host-vision"]["resource_status"] == "unknown"


def test_cli_fake_lifecycle_goal_to_done(cli_project):
    invoke("goal", "new", "--json", "--objective", "ship it",
           "--acceptance", "marker exists", "--acceptance", "summary written")
    invoke("plan", "--json")

    plan = ir_for(type("G", (), {"id": "G001"})(), [
        task_spec("T001", acceptance=["marker exists"], verification=["test -f done.marker"]),
        task_spec("T002", deps=["T001"], acceptance=["summary written"]),
    ])
    plan_path = cli_project / "plan.json"
    plan_path.write_text(json.dumps(plan))
    result = invoke("plan", "submit", "--json", "--file", str(plan_path))
    assert result.exit_code == 0
    assert payload(result)["revision"] == 1

    result = invoke("run", "--json")
    assert payload(result)["host_required"][0]["task"] == "T001"

    result = invoke("task", "list", "--json")
    assert payload(result)["count"] == 2

    result = invoke("task", "claim", "--json", "T001")
    assert payload(result)["status"] == "running"

    (cli_project / "done.marker").write_text("ok")
    (cli_project / "ev1.json").write_text(
        json.dumps({"status": "passed", "summary": "done", "checks": [], "artifacts": []})
    )
    result = invoke("task", "complete", "--json", "T001", "--evidence", "ev1.json")
    assert payload(result)["status"] == "passed"

    result = invoke("run", "--json")
    assert payload(result)["host_required"][0]["task"] == "T002"
    invoke("task", "claim", "--json", "T002")
    result = invoke("task", "complete", "--json", "T002", "--evidence", "ev1.json")
    assert payload(result)["status"] == "passed"

    result = invoke("status", "--json")
    body = payload(result)
    assert body["run"]["status"] == "done"
    assert body["goal"]["status"] == "done"
    assert body["goal"]["objective"] == "ship it"
    assert body["run"]["started_at"]
    assert body["run"]["completed_at"]

    result = invoke("status")  # human layout
    assert result.exit_code == 0
    assert "DONE" in result.stdout
    assert "✓ T001" in result.stdout
    assert "lifecycle:" in result.stdout
    assert "started_at" in result.stdout and "completed_at" in result.stdout


def test_task_fail_and_retry_via_cli(cli_project):
    invoke("goal", "new", "--json", "--objective", "ship it", "--acceptance", "a1")
    invoke("plan", "--json")
    plan = ir_for(type("G", (), {"id": "G001"})(), [
        task_spec("T001", acceptance=["a1"], verification=["true"]),
    ])
    (cli_project / "plan.json").write_text(json.dumps(plan))
    invoke("plan", "submit", "--json", "--file", str(cli_project / "plan.json"))
    invoke("run", "--json")
    invoke("task", "claim", "--json", "T001")

    result = invoke("task", "fail", "--json", "T001", "--reason", "blocked upstream")
    assert payload(result)["status"] == "failed"

    result = invoke("task", "retry", "--json", "T001")
    assert payload(result)["status"] == "runnable"

    # verify submit requires a verifying task
    result = invoke("verify", "submit", "--json", "T001", "--result", "pass")
    assert result.exit_code == 1
    assert payload(result)["ok"] is False


def _check_project(cli_project, verification):
    """Goal + plan + run + claim for one task, returning the claimed task id."""
    invoke("goal", "new", "--json", "--objective", "ship it",
           "--acceptance", "marker exists")
    invoke("plan", "--json")
    plan = ir_for(type("G", (), {"id": "G001"})(), [
        task_spec("T001", acceptance=["marker exists"], verification=verification),
    ])
    (cli_project / "plan.json").write_text(json.dumps(plan))
    invoke("plan", "submit", "--json", "--file", str(cli_project / "plan.json"))
    invoke("run", "--json")
    invoke("task", "claim", "--json", "T001")


def test_task_check_envelope_reports_each_command_result(cli_project):
    _check_project(cli_project, ["test -f done.marker", "true"])

    result = invoke("task", "check", "--json", "T001")
    assert result.exit_code == 0
    body = payload(result)
    assert body["ok"] is True
    assert body["task"] == "T001"
    assert body["status"] == "running"  # unchanged by the self-check
    assert body["attempt"] is not None
    first, second = body["results"]
    assert first["command"] == "test -f done.marker"
    assert first["exit_code"] == 1
    assert first["passed"] is False
    assert first["log_path"].startswith(".orx/runs/R001/check/T001/")
    assert (cli_project / first["log_path"]).exists()
    assert second["command"] == "true"
    assert second["passed"] is True
    assert second["error_summary"] is None
    assert body["summary"] == {"total": 2, "passed": 1, "failed": 1, "denied": 0}

    # Human output names the failure and stays explicit about semantics.
    result = invoke("task", "check", "T001")
    assert result.exit_code == 0
    assert "FAIL" in result.stdout
    assert "test -f done.marker" in result.stdout
    assert "self-check only" in result.stdout

    # check -> fix -> check -> complete: a green loop still completes green.
    (cli_project / "done.marker").write_text("ok")
    result = invoke("task", "check", "--json", "T001")
    assert payload(result)["summary"]["passed"] == 2
    (cli_project / "ev.json").write_text(
        json.dumps({"status": "passed", "summary": "done", "checks": [], "artifacts": []})
    )
    result = invoke("task", "complete", "--json", "T001", "--evidence", "ev.json")
    assert payload(result)["status"] == "passed"


def test_task_check_denied_entry_envelope(cli_project):
    _check_project(cli_project, ["sudo rm /tmp/x"])

    result = invoke("task", "check", "--json", "T001")
    assert result.exit_code == 0
    item = payload(result)["results"][0]
    assert item["denied"] is True
    assert item["exit_code"] is None
    assert item["passed"] is False
    assert payload(result)["summary"]["denied"] == 1

    result = invoke("task", "check", "T001")
    assert "denied" in result.stdout
    assert "sudo" in result.stdout


def test_task_check_without_command_entries_is_clear_not_exception(cli_project):
    _check_project(cli_project, ["agent: reads well"])

    result = invoke("task", "check", "--json", "T001")
    assert result.exit_code == 0
    body = payload(result)
    assert body["ok"] is True
    assert body["results"] == []
    assert body["agent_entries_not_run"] == 1
    assert "no executable command verification entries" in body["note"]

    result = invoke("task", "check", "T001")
    assert result.exit_code == 0
    assert "nothing was run" in result.stdout


def test_task_check_unknown_task_envelope(cli_project):
    _check_project(cli_project, ["true"])

    result = invoke("task", "check", "--json", "T999")
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False
    assert "T999" in body["error"]


def test_task_check_usage_error_exits_2(cli_project):
    result = invoke("task", "check")
    assert result.exit_code == 2


# -- task complete: structured delivery result + delivery gate --------------


def _structured_evidence(cli_project, name="ev.json", **overrides):
    body = {"status": "passed", "summary": "done", "checks": [], "artifacts": []}
    body.update(overrides)
    (cli_project / name).write_text(json.dumps(body))
    return name


def test_task_complete_rejects_plain_evidence_with_field_names(cli_project):
    _check_project(cli_project, ["true"])
    (cli_project / "ev.json").write_text(
        json.dumps({"summary": "done", "commands": [], "artifacts": []})
    )

    result = invoke("task", "complete", "--json", "T001", "--evidence", "ev.json")
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False
    joined = "; ".join(body["evidence_errors"])
    assert "'status'" in joined and "'checks'" in joined

    # Human output names the missing fields too, and nothing changed.
    result = invoke("task", "complete", "T001", "--evidence", "ev.json")
    assert result.exit_code == 1
    assert "status" in result.output and "checks" in result.output
    assert invoke("task", "list", "--json").stdout and (
        payload(invoke("task", "list", "--json"))["tasks"][0]["status"] == "running"
    )


def test_task_complete_gate_rejection_envelope_and_recovery(cli_project):
    _check_project(cli_project, ["ls done.marker"])
    _structured_evidence(cli_project)

    # Red gate: rejected with the structured per-failure detail.
    result = invoke("task", "complete", "--json", "T001", "--evidence", "ev.json")
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False
    gate = body["gate"]
    assert gate["task"] == "T001"
    assert gate["task_status"] == "running"  # kept the re-completable state
    assert gate["attempt"] is not None
    assert gate["summary"]["total"] == 1 and gate["summary"]["passed"] == 0
    failure = gate["failures"][0]
    for key in ("command", "exit_code", "error_summary", "log_path"):
        assert key in failure
    assert failure["command"] == "ls done.marker"
    assert failure["exit_code"] != 0
    assert failure["log_path"].startswith(".orx/runs/R001/check/T001/")

    # Human output shows the failure and its log path.
    result = invoke("task", "complete", "T001", "--evidence", "ev.json")
    assert result.exit_code == 1
    assert "FAIL" in result.output and "log:" in result.output

    # Task still running, same attempt: fix and complete again -> passed.
    assert payload(invoke("task", "list", "--json"))["tasks"][0]["status"] == "running"
    (cli_project / "done.marker").write_text("ok")
    result = invoke("task", "complete", "--json", "T001", "--evidence", "ev.json")
    assert result.exit_code == 0
    body = payload(result)
    assert body["status"] == "passed"
    assert body["attempt"] == gate["attempt"]  # recovery reused the open attempt


def test_task_complete_human_success_path_renders_delivery(cli_project):
    """The accepted-success human output (delivery passed, no reason key)
    must render cleanly — a post-transition rendering crash would exit 1
    after the task already passed."""
    _check_project(cli_project, ["test -f done.marker"])
    _structured_evidence(cli_project)
    (cli_project / "done.marker").write_text("ok")
    result = invoke("task", "complete", "T001", "--evidence", "ev.json")
    assert result.exit_code == 0
    assert "status passed" in result.stdout
    assert "delivery: passed" in result.stdout
    assert "Traceback" not in result.stdout


def test_task_complete_blocked_delivery_envelope(cli_project):
    _check_project(cli_project, ["true"])
    _structured_evidence(
        cli_project, status="blocked", summary="sandbox denies shell execution",
        checks=[{"command": "true", "exit_code": None, "log": None}],
    )

    result = invoke("task", "complete", "--json", "T001", "--evidence", "ev.json")
    assert result.exit_code == 0  # the delivery was recorded, not accepted as success
    body = payload(result)
    assert body["ok"] is True
    assert body["status"] == "failed"
    assert body["delivery"]["status"] == "blocked"
    assert body["delivery"]["reason"].startswith("delivery blocked (environment/tool blocked):")

    # The task failed with the distinguishable reason, and can be retried.
    row = payload(invoke("task", "list", "--json"))["tasks"][0]
    assert row["status"] == "failed"
    assert row["failure_reason"].startswith("delivery blocked (environment/tool blocked):")


def test_task_complete_usage_error_exits_2(cli_project):
    result = invoke("task", "complete")
    assert result.exit_code == 2


# -- task heartbeat: explicit progress reports (G006) -----------------------


def _claimed_attempt_id(task_id="T001"):
    """Read the task's current worker attempt id from state."""
    project = dispatch.open_project()
    try:
        run = project.store.run_for_goal("G001")
        revision = project.store.revision_active(run.id)
        return project.store.attempt_latest_for_task(revision.id, task_id).id
    finally:
        project.close()


def test_task_heartbeat_usage_errors_exit_2(cli_project):
    _check_project(cli_project, ["true"])
    # --attempt is mandatory: identity is never guessed or defaulted.
    missing_attempt = invoke("task", "heartbeat", "T001", "--phase", "checking")
    assert missing_attempt.exit_code == 2
    missing_phase = invoke("task", "heartbeat", "T001", "--attempt", "1")
    assert missing_phase.exit_code == 2
    bad_attempt = invoke("task", "heartbeat", "T001", "--attempt", "not-an-int",
                         "--phase", "checking")
    assert bad_attempt.exit_code == 2


def test_task_heartbeat_input_validation_envelope_exits_2(cli_project):
    _check_project(cli_project, ["true"])
    attempt = _claimed_attempt_id()

    blank = invoke("task", "heartbeat", "--json", "T001",
                   "--attempt", str(attempt), "--phase", "   ")
    assert blank.exit_code == 2
    body = payload(blank)
    assert body["ok"] is False
    assert body["error"]["reason"] == "phase_invalid"
    assert "1-64" in body["error"]["message"]

    overlong = invoke("task", "heartbeat", "--json", "T001",
                      "--attempt", str(attempt), "--phase", "checking",
                      "--message", "y" * 513)
    assert overlong.exit_code == 2
    body = payload(overlong)
    assert body["error"]["reason"] == "message_invalid"

    # Usage-class failures are distinguishable from identity rejections
    # even in one call: the bounds error wins over a bogus attempt id.
    both = invoke("task", "heartbeat", "--json", "T999",
                  "--attempt", "999999", "--phase", "")
    assert both.exit_code == 2
    assert payload(both)["error"]["reason"] == "phase_invalid"

    # Nothing was recorded by any rejected call.
    assert invoke("task", "list", "--json").stdout and (
        payload(invoke("task", "list", "--json"))["tasks"][0]["status"] == "running"
    )


def test_task_heartbeat_success_envelope_and_human_output(cli_project):
    _check_project(cli_project, ["true"])
    attempt = _claimed_attempt_id()

    first = invoke("task", "heartbeat", "--json", "T001",
                   "--attempt", str(attempt), "--phase", "  checking  ")
    assert first.exit_code == 0, first.stdout
    body = payload(first)
    assert body["ok"] is True
    assert body["task"] == "T001"
    assert body["attempt"] == attempt
    assert body["sequence"] == 1
    assert body["phase"] == "checking"        # stripped, otherwise opaque
    assert body["message"] is None            # key present, value null
    assert body["received_at"].endswith("+00:00")

    second = invoke("task", "heartbeat", "--json", "T001",
                    "--attempt", str(attempt), "--phase", "implementing",
                    "--message", "round 2/3 red, fixing")
    assert second.exit_code == 0
    body = payload(second)
    assert body["sequence"] == 2
    assert body["message"] == "round 2/3 red, fixing"
    assert body["received_at"] >= payload(first)["received_at"]

    # Human output states the same facts.
    human = invoke("task", "heartbeat", "T001",
                   "--attempt", str(attempt), "--phase", "delivering")
    assert human.exit_code == 0
    assert "progress report recorded" in human.stdout
    assert f"attempt {attempt} #3" in human.stdout
    assert "phase: delivering" in human.stdout
    assert "message: (none)" in human.stdout
    assert "received_at:" in human.stdout

    # Observation only: the task and its status are untouched.
    assert _task_status("T001") == "running"


def test_task_heartbeat_identity_rejections_exit_1(cli_project):
    _check_project(cli_project, ["true"])
    attempt = _claimed_attempt_id()

    missing = invoke("task", "heartbeat", "--json", "T001",
                     "--attempt", "999999", "--phase", "checking")
    assert missing.exit_code == 1
    body = payload(missing)
    assert body["ok"] is False
    assert body["error"]["reason"] == "attempt_not_found"
    assert "999999" in body["error"]["message"]

    human = invoke("task", "heartbeat", "T001",
                   "--attempt", "999999", "--phase", "checking")
    assert human.exit_code == 1
    assert "reason: attempt_not_found" in human.output

    # A completed task no longer accepts reports for its closed attempt.
    _structured_evidence(cli_project)
    done = invoke("task", "complete", "--json", "T001", "--evidence", "ev.json",
                  "--attempt", str(attempt))
    assert done.exit_code == 0
    late = invoke("task", "heartbeat", "--json", "T001",
                  "--attempt", str(attempt), "--phase", "delivering")
    assert late.exit_code == 1
    body = payload(late)
    assert body["error"]["reason"] in ("attempt_closed", "task_not_running")


def test_task_heartbeat_never_binds_the_controller_session(cli_project, monkeypatch):
    monkeypatch.delenv("ORX_SESSION_REF", raising=False)
    # One plan, two independent host tasks: T001 claimed with no session
    # anywhere, T002 claimed with the worker's own explicit session.
    invoke("goal", "new", "--json", "--objective", "ship it",
           "--acceptance", "marker exists")
    invoke("plan", "--json")
    plan = ir_for(type("G", (), {"id": "G001"})(), [
        task_spec("T001", acceptance=["marker exists"], verification=["true"]),
        task_spec("T002", acceptance=["marker exists"], verification=["true"]),
    ])
    (cli_project / "plan.json").write_text(json.dumps(plan))
    invoke("plan", "submit", "--json", "--file", str(cli_project / "plan.json"))
    invoke("run", "--json")
    invoke("task", "claim", "--json", "T001")
    invoke("task", "claim", "--json", "T002", "--session", "sess_worker_cli")
    attempt_null = _claimed_attempt_id("T001")
    attempt_bound = _claimed_attempt_id("T002")

    # The Controller's env session exists while the worker reports: a
    # report must neither fill the worker's NULL ref with it nor overwrite
    # the worker's bound ref.
    monkeypatch.setenv("ORX_SESSION_REF", "sess_controller_env")
    for task_id, attempt_id in (("T001", attempt_null), ("T002", attempt_bound)):
        ok = invoke("task", "heartbeat", "--json", task_id,
                    "--attempt", str(attempt_id), "--phase", "checking")
        assert ok.exit_code == 0, ok.stdout

    project = dispatch.open_project()
    try:
        stored_null = project.store.attempt_get(attempt_null)
        assert stored_null.session_ref is None  # env did not leak in
        stored_bound = project.store.attempt_get(attempt_bound)
        assert stored_bound.session_ref == "sess_worker_cli"  # env did not win
        for stored in (stored_null, stored_bound):
            assert stored.ended_at is None and stored.result is None
        assert project.store.attempt_progress_all(attempt_null)[-1].phase == "checking"
        assert project.store.attempt_progress_all(attempt_bound)[-1].phase == "checking"
    finally:
        project.close()


# -- G006 T004: progress observation on status / task list / run / timeline --
#
# docs/host-progress-contract.md §7-§8: every observation face shows the
# current attempt's latest report, its time and age, `unknown` when there
# is none, and the overdue CHECK HINT past the configured threshold. The
# CLI text states the same facts as the --json envelope; clocks are the
# injectable ORX seam, never a real sleep.


def _frozen_cli_clock(monkeypatch):
    holder = {"now": datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)}

    def now():
        return holder["now"].isoformat(timespec="microseconds")

    monkeypatch.setattr("orx.state.now", now)
    monkeypatch.setattr("orx.dispatch.db_now", now)
    return holder


def test_status_and_task_list_carry_current_progress(cli_project, monkeypatch):
    _check_project(cli_project, ["true"])
    attempt = _claimed_attempt_id()

    # Before any report: unknown, no guesses, builtin 60-minute threshold.
    progress = payload(invoke("status", "--json"))["tasks"][0]["progress"]
    assert progress == {
        "attempt": attempt,
        "state": "unknown",
        "phase": None,
        "message": None,
        "received_at": None,
        "age_sec": None,
        "timeout_sec": 3600,
        "hint": None,
        "note": None,
    }
    listed = payload(invoke("task", "list", "--json"))["tasks"][0]
    assert listed["progress"] == progress

    # One report under a frozen clock: reported, with the honest age.
    clock = _frozen_cli_clock(monkeypatch)
    ok = invoke("task", "heartbeat", "--json", "T001",
                "--attempt", str(attempt), "--phase", "checking",
                "--message", "round 1/3 red")
    assert ok.exit_code == 0, ok.stdout
    clock["now"] = clock["now"] + timedelta(seconds=120)
    progress = payload(invoke("task", "list", "--json"))["tasks"][0]["progress"]
    assert progress["state"] == "reported"
    assert progress["phase"] == "checking"
    assert progress["message"] == "round 1/3 red"
    assert progress["age_sec"] == 120
    assert progress["hint"] is None

    # Human text states the same facts on both faces.
    human_status = invoke("status")
    assert "progress: reported" in human_status.stdout
    assert "phase checking" in human_status.stdout
    assert "age 2m" in human_status.stdout
    human_list = invoke("task", "list")
    assert "progress: reported" in human_list.stdout
    assert "phase checking" in human_list.stdout


def test_run_recovery_reports_progress_and_overdue_hint(cli_project, monkeypatch):
    # A 1-minute threshold makes the overdue hint reachable by clock math.
    (cli_project / ".orx" / "config.toml").write_text(
        HOST_CONFIG_TOML.replace(
            '[worker]\nprofiles = ["host-worker", "host-external"]\n',
            '[worker]\nprofiles = ["host-worker", "host-external"]\n'
            "progress_timeout_min = 1\n",
        )
    )
    _check_project(cli_project, ["true"])
    attempt = _claimed_attempt_id()
    clock = _frozen_cli_clock(monkeypatch)
    ok = invoke("task", "heartbeat", "--json", "T001",
                "--attempt", str(attempt), "--phase", "implementing")
    assert ok.exit_code == 0, ok.stdout

    recovery = payload(invoke("run", "--json"))["recovery"]
    assert [e["task"] for e in recovery] == ["T001"]
    assert recovery[0]["progress"]["state"] == "reported"
    assert recovery[0]["progress"]["timeout_sec"] == 60
    assert recovery[0]["progress"]["age_sec"] == 0

    # Past the threshold: the hint appears (envelope and text), and the
    # observation itself still changes nothing.
    clock["now"] = clock["now"] + timedelta(seconds=90)
    recovery = payload(invoke("run", "--json"))["recovery"]
    progress = recovery[0]["progress"]
    assert progress["state"] == "overdue"
    assert progress["age_sec"] == 90
    assert "check the original worker session" in progress["hint"]
    assert _task_status("T001") == "running"

    human = invoke("run")
    assert "recovery: T001 is RUNNING under attempt" in human.stdout
    assert "progress: overdue" in human.stdout
    assert "overdue check:" in human.stdout
    assert "check the original worker session" in human.stdout


def test_timeline_cli_shows_current_window_and_report_history(
        cli_project, monkeypatch):
    _check_project(cli_project, ["true"])
    attempt = _claimed_attempt_id()
    clock = _frozen_cli_clock(monkeypatch)
    ok = invoke("task", "heartbeat", "--json", "T001",
                "--attempt", str(attempt), "--phase", "checking",
                "--message", "round 1/3 red, fixing")
    assert ok.exit_code == 0, ok.stdout

    body = payload(invoke("timeline", "--json"))
    current = {c["task"]: c["progress"] for c in body["current"]}
    assert current["T001"]["state"] == "reported"
    assert current["T001"]["attempt"] == attempt
    # The report is history too, with its full identity.
    reports = [e for e in body["entries"] if e["event"] == "attempt.report"]
    assert [e["detail"] for e in reports] == [
        f"worker T001 a{attempt} #1 checking — round 1/3 red, fixing"
    ]

    # Overdue surfaces on the current window with the hint, in text too.
    clock["now"] = clock["now"] + timedelta(seconds=3700)
    human = invoke("timeline")
    assert f"current window: T001 (attempt {attempt})" in human.stdout
    assert "progress: overdue" in human.stdout
    assert "overdue check:" in human.stdout
    assert "attempt.report" in human.stdout
    # The history entry itself stays a plain stamped row.
    entry_lines = [
        line for line in human.stdout.splitlines()
        if "attempt.report" in line
    ]
    assert re.match(r"^\d{2}:\d{2}:\d{2}  \S+  attempt\.report  .+", entry_lines[-1])


def test_verify_submit_envelope(cli_project):
    invoke("goal", "new", "--json", "--objective", "ship it", "--acceptance", "a1")
    invoke("plan", "--json")
    plan = ir_for(type("G", (), {"id": "G001"})(), [
        task_spec("T001", acceptance=["a1"], verification=["agent: looks fine"]),
    ])
    (cli_project / "plan.json").write_text(json.dumps(plan))
    invoke("plan", "submit", "--json", "--file", str(cli_project / "plan.json"))
    invoke("run", "--json")
    invoke("task", "claim", "--json", "T001")
    (cli_project / "ev.json").write_text(
        json.dumps({"status": "passed", "summary": "done", "checks": [], "artifacts": []})
    )
    result = invoke("task", "complete", "--json", "T001", "--evidence", "ev.json")
    assert payload(result)["status"] == "verifying"

    result = invoke("verify", "submit", "--json", "T001", "--result", "pass",
                    "--evidence", "ev.json")
    assert result.exit_code == 0
    body = payload(result)
    assert body["status"] == "passed"
    assert body["entry"] == "agent: looks fine"

    result = invoke("verify", "--json")  # nothing outstanding
    assert payload(result)["agent_required"] == []


# -- orx config path / list / get / set ------------------------------------


def _toml(path):
    import tomllib
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def test_config_help_mentions_layers_keys_and_exits_zero():
    listed = invoke("config", "--help")
    assert listed.exit_code == 0
    for word in ("path", "list", "get", "set", "environment", "project", "user"):
        assert word in listed.stdout
    detailed = invoke("config", "set", "--help")
    assert detailed.exit_code == 0
    text = detailed.stdout
    for snippet in (
        "--user",
        "--json",
        "schema_version",
        "controller.profile",
        "worker.profiles",
        "runtime.max_parallel",
        "ORX_CONFIG_DIR",
        "true",
        "JSON array",
    ):
        assert snippet in text, snippet


def test_config_usage_exits_2():
    assert invoke("config", "get").exit_code == 2
    assert invoke("config", "set", "runtime.max_parallel").exit_code == 2
    assert invoke("config", "nope").exit_code == 2


def test_config_path_json_reports_user_and_project_paths(cli_project, tmp_path):
    result = invoke("config", "path", "--json")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    assert Path(body["user_config"]) == (tmp_path / "isolated-config" / "config.toml").resolve()
    assert Path(body["user_profiles"]) == (tmp_path / "isolated-config" / "profiles.toml").resolve()
    assert Path(body["user_data"]) == (tmp_path / "isolated-data").resolve()
    assert Path(body["project_config"]) == (cli_project / ".orx" / "config.toml").resolve()
    assert Path(body["project_profiles"]) == (cli_project / ".orx" / "profiles.toml").resolve()

    human = invoke("config", "path")
    assert human.exit_code == 0
    assert "user config:" in human.stdout
    assert str(body["project_config"]) in human.stdout


def test_config_path_without_project_has_null_project_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("ORX_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    body = payload(invoke("config", "path", "--json"))
    assert body["ok"] is True
    assert body["project_config"] is None
    assert body["project_profiles"] is None
    assert body["user_config"]
    assert body["user_data"]
    human = invoke("config", "path")
    assert "(none)" in human.stdout


def test_config_list_and_get_share_effective_values(cli_project, monkeypatch):
    monkeypatch.setenv("ORX_RUNTIME_COMMAND_TIMEOUT_SEC", "7")
    listed = invoke("config", "list", "--json")
    assert listed.exit_code == 0, listed.stdout
    rows = {row["key"]: row for row in payload(listed)["values"]}
    assert rows["controller.profile"]["value"] == "host-planner"
    assert rows["controller.profile"]["origin"] == "project"
    assert rows["plan.allow_class_downgrade"]["value"] is False
    assert rows["worker.profiles"]["value"] == ["host-worker", "host-external"]
    assert rows["runtime.command_timeout_sec"]["value"] == 7
    assert rows["runtime.command_timeout_sec"]["origin"] == "env"
    assert rows["runtime.max_parallel"]["origin"] == "project"
    assert rows["runtime.max_parallel"]["value"] == 1

    for key, row in rows.items():
        result = invoke("config", "get", "--json", key)
        assert result.exit_code == 0, result.stdout
        got = payload(result)
        assert got["key"] == key
        assert got["value"] == row["value"]
        assert got["origin"] == row["origin"]

    human = invoke("config", "get", "controller.profile")
    assert human.exit_code == 0
    assert "host-planner" in human.stdout
    assert "project" in human.stdout

    missing = invoke("config", "get", "--json", "schema_version")
    assert missing.exit_code == 1
    body = payload(missing)
    assert body["ok"] is False
    assert "schema_version" in body["error"]
    assert body["errors"]


def test_config_set_round_trip_precedence_and_user_create(cli_project, tmp_path, monkeypatch):
    project_config = cli_project / ".orx" / "config.toml"
    project_profiles = cli_project / ".orx" / "profiles.toml"
    project_config.write_text(
        'schema_version = 1\nnote = "keep-me"\n\n[controller]\nprofile = "host-planner"\n'
        '[extra]\nflag = true\n\n[[widgets]]\nname = "a"\n'
    )
    user_config = tmp_path / "isolated-config" / "config.toml"
    assert not user_config.exists()

    monkeypatch.setenv("ORX_RUNTIME_MAX_PARALLEL", "9")
    created = invoke(
        "config", "set", "--user", "--json", "runtime.command_timeout_sec", "11"
    )
    assert created.exit_code == 0, created.stdout
    body = payload(created)
    assert body["ok"] is True
    assert body["created"] is True
    assert body["layer"] == "user"
    assert body["value"] == 11
    user_doc = _toml(user_config)
    assert user_doc == {"schema_version": 1, "runtime": {"command_timeout_sec": 11}}
    assert project_config.read_text() == (
        'schema_version = 1\nnote = "keep-me"\n\n[controller]\nprofile = "host-planner"\n'
        '[extra]\nflag = true\n\n[[widgets]]\nname = "a"\n'
    )

    listed = {row["key"]: row for row in payload(invoke("config", "list", "--json"))["values"]}
    assert listed["runtime.command_timeout_sec"] == {
        "key": "runtime.command_timeout_sec", "value": 11, "origin": "user",
    }
    assert listed["runtime.max_parallel"]["value"] == 9
    assert listed["runtime.max_parallel"]["origin"] == "env"
    assert "max_parallel" not in user_doc["runtime"]

    routing = invoke(
        "config", "set", "--user", "--json", "worker.profiles", "host-worker, host-external"
    )
    assert routing.exit_code == 0, routing.stdout
    assert payload(routing)["created"] is False
    assert payload(routing)["value"] == ["host-worker", "host-external"]
    got = payload(invoke("config", "get", "--json", "worker.profiles"))
    assert got["value"] == ["host-worker", "host-external"]
    assert got["origin"] == "user"

    project_before = project_config.read_bytes()
    profiles_before = project_profiles.read_bytes()
    written = invoke(
        "config", "set", "--json", "worker.profiles", '["host-verifier", "host-vision"]'
    )
    assert written.exit_code == 0, written.stdout
    assert payload(written)["layer"] == "project"
    assert payload(written)["value"] == ["host-verifier", "host-vision"]
    assert project_profiles.read_bytes() == profiles_before
    project_doc = _toml(project_config)
    assert project_doc["schema_version"] == 1
    assert project_doc["note"] == "keep-me"
    assert project_doc["extra"] == {"flag": True}
    assert project_doc["widgets"] == [{"name": "a"}]
    assert project_doc["controller"]["profile"] == "host-planner"
    assert project_doc["worker"]["profiles"] == ["host-verifier", "host-vision"]
    assert _toml(user_config)["worker"]["profiles"] == ["host-worker", "host-external"]
    got = payload(invoke("config", "get", "--json", "worker.profiles"))
    assert got["origin"] == "project"
    assert got["value"] == ["host-verifier", "host-vision"]

    invoke("config", "set", "--json", "runtime.command_timeout_sec", "22")
    assert payload(invoke("config", "get", "--json", "runtime.command_timeout_sec")) == {
        "ok": True, "key": "runtime.command_timeout_sec", "value": 22, "origin": "project",
    }
    assert _toml(user_config)["runtime"]["command_timeout_sec"] == 11
    monkeypatch.setenv("ORX_RUNTIME_COMMAND_TIMEOUT_SEC", "33")
    assert payload(invoke("config", "get", "--json", "runtime.command_timeout_sec"))["value"] == 33
    assert payload(invoke("config", "get", "--json", "runtime.command_timeout_sec"))["origin"] == "env"
    assert _toml(project_config)["runtime"]["command_timeout_sec"] == 22

    depth = invoke("config", "set", "--json", "plan.depth", "light")
    assert depth.exit_code == 0, depth.stdout
    assert payload(invoke("config", "get", "--json", "plan.depth"))["value"] == "light"
    flag = invoke("config", "set", "--json", "plan.allow_class_downgrade", "true")
    assert flag.exit_code == 0, flag.stdout
    assert payload(invoke("config", "get", "--json", "plan.allow_class_downgrade"))["value"] is True
    cleared = invoke("config", "set", "--json", "verify.profiles", "[]")
    assert cleared.exit_code == 0, cleared.stdout
    assert payload(invoke("config", "get", "--json", "verify.profiles"))["value"] == []
    assert project_before != project_config.read_bytes()


def test_config_set_rejects_without_mutating(cli_project, tmp_path):
    project_config = cli_project / ".orx" / "config.toml"
    before = project_config.read_bytes()
    user_config = tmp_path / "isolated-config" / "config.toml"

    cases = [
        ("schema_version", "2"),
        ("not.a.key", "1"),
        ("runtime.max_parallel", "0"),
        ("runtime.max_parallel", "true"),
        ("runtime.command_timeout_sec", "-3"),
        ("plan.depth", "extreme"),
        ("plan.allow_class_downgrade", "yes"),
        ("controller.profile", "ghost"),
        ("worker.profiles", "[1, 2]"),
        ("worker.profiles", "ghost"),
    ]
    for key, value in cases:
        result = invoke("config", "set", "--json", key, value)
        assert result.exit_code == 1, (key, value, result.stdout)
        body = payload(result)
        assert body["ok"] is False
        assert "error" in body
        assert project_config.read_bytes() == before
        assert not user_config.exists()

    broken = project_config.read_bytes()
    result = invoke("config", "set", "--user", "--json", "runtime.max_parallel", "nope")
    assert result.exit_code == 1
    assert payload(result)["ok"] is False
    assert project_config.read_bytes() == broken
    assert not user_config.exists()
    assert not user_config.parent.exists()

    project_config.write_text("schema_version = 1\n[runtime]\nmax_parallel = [\n")
    malformed = project_config.read_bytes()
    result = invoke("config", "set", "--json", "plan.depth", "light")
    assert result.exit_code == 1
    assert project_config.read_bytes() == malformed

    project_config.write_text('note = "only"\n[runtime]\nmax_parallel = 1\n')
    missing_schema = project_config.read_bytes()
    result = invoke("config", "set", "--json", "plan.depth", "light")
    assert result.exit_code == 1
    assert "schema_version" in payload(result)["error"]
    assert project_config.read_bytes() == missing_schema

    project_config.write_text("schema_version = 2\n[runtime]\nmax_parallel = 1\n")
    newer = project_config.read_bytes()
    result = invoke("config", "set", "--json", "plan.depth", "light")
    assert result.exit_code == 1
    assert project_config.read_bytes() == newer


def test_config_set_outside_project(tmp_path, monkeypatch):
    monkeypatch.delenv("ORX_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    refused = invoke("config", "set", "--json", "runtime.max_parallel", "2")
    assert refused.exit_code == 1
    assert payload(refused)["ok"] is False
    assert not (tmp_path / ".orx").exists()

    created = invoke("config", "set", "--user", "--json", "runtime.max_parallel", "2")
    assert created.exit_code == 0, created.stdout
    assert payload(created)["layer"] == "user"
    assert not (tmp_path / ".orx").exists()
    listed = {row["key"]: row for row in payload(invoke("config", "list", "--json"))["values"]}
    assert listed["runtime.max_parallel"]["value"] == 2
    assert listed["runtime.max_parallel"]["origin"] == "user"
    assert listed["plan.depth"]["origin"] == "default"


# -- orx agent list / info / probe ----------------------------------------


def _install_logged_fake(directory: Path, name: str, script: str, log: Path) -> None:
    body = f"#!/bin/sh\necho \"$*\" >> {str(log)!r}\n{script}\n"
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_agent_help_lists_subcommands():
    listed = invoke("agent", "--help")
    assert listed.exit_code == 0
    for word in ("list", "info", "probe", "status", "host-only", "completion"):
        assert word in listed.stdout
    probed = invoke("agent", "probe", "--help")
    assert probed.exit_code == 0
    for snippet in ("--json", "codex", "cursor", "zcode", "completion", "exit 1"):
        assert snippet in probed.stdout


def test_agent_usage_exits_2():
    assert invoke("agent", "info").exit_code == 2
    assert invoke("agent", "probe").exit_code == 2
    assert invoke("agent", "nope").exit_code == 2


def test_agent_list_marks_adapter_harnesses_and_host_only_zcode():
    result = invoke("agent", "list", "--json")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    rows = {row["harness"]: row for row in body["harnesses"]}
    assert list(rows) == ["codex", "cursor", "shell", "zcode"]
    assert rows["codex"]["adapter"] is True and rows["codex"]["probeable"] is True
    assert rows["codex"]["binary"] == "codex" and rows["codex"]["host_only"] is False
    assert rows["cursor"]["adapter"] is True and rows["cursor"]["binary"] == "agent"
    assert rows["shell"]["adapter"] is True and rows["shell"]["probeable"] is False
    assert rows["shell"]["binary"] is None
    assert rows["zcode"] == {
        "harness": "zcode",
        "binary": None,
        "adapter": False,
        "host_only": True,
        "probeable": False,
    }
    human = invoke("agent", "list")
    assert human.exit_code == 0
    assert "zcode" in human.stdout and "host-only" in human.stdout
    assert "codex" in human.stdout and "adapter" in human.stdout


def test_agent_probe_writes_snapshot_and_info_reads_it(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "codex-invocations"
    monkeypatch.setenv("PATH", str(bindir))
    _install_logged_fake(bindir, "codex", """
if [ "$1" = "--version" ]; then echo "codex-cli 0.160.0"; exit 0; fi
if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then
  printf '%s\\n' 'usage: codex exec' '--json' '-m, --model' '-C, --cd' '--output-last-message' 'resume' 'model_reasoning_effort'
  exit 0
fi
if [ "$1" = "login" ]; then echo "Logged in using ChatGPT"; exit 0; fi
if [ "$1" = "debug" ]; then echo '{"models":[{"slug":"m"}]}'; exit 0; fi
echo COMPLETION >> "$0.log"
exit 99
""".replace('"$0.log"', repr(str(tmp_path / "completion-flag"))), log)

    result = invoke("agent", "probe", "--json", "codex")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    snap = body["snapshot"]
    assert snap["harness"] == "codex"
    assert snap["version"] == "codex-cli 0.160.0"
    assert snap["auth"] == "logged_in"
    assert snap["models_discoverable"] is True
    assert snap["features"]["headless"] is True
    assert Path(body["path"]).is_file()
    assert json.loads(Path(body["path"]).read_text()) == snap
    calls = log.read_text().splitlines()
    assert calls == ["--version", "exec --help", "login status", "debug models"]
    assert not (tmp_path / "completion-flag").exists()

    info = invoke("agent", "info", "--json", "codex")
    assert info.exit_code == 0, info.stdout
    shown = payload(info)
    assert shown["ok"] is True
    assert shown["snapshot"] == snap
    assert shown["adapter"] is True
    assert shown["host_only"] is False
    assert "codex exec --json" in shown["launch"]["summary"]
    assert "never runs this completion" in shown["launch"]["summary"]
    human = invoke("agent", "info", "codex")
    assert human.exit_code == 0
    assert "codex-cli 0.160.0" in human.stdout
    assert "logged_in" in human.stdout


def test_agent_probe_cursor_fake_never_prints(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "agent-invocations"
    monkeypatch.setenv("PATH", str(bindir))
    _install_logged_fake(bindir, "agent", """
if [ "$1" = "--version" ]; then echo "agent 2026.10.01"; exit 0; fi
if [ "$1" = "--help" ]; then
  echo "--print --output-format --workspace --trust --model effort= --resume"
  exit 0
fi
if [ "$1" = "status" ]; then echo "Not logged in"; exit 0; fi
if [ "$1" = "--list-models" ]; then echo "auto - Auto"; exit 0; fi
echo COMPLETION
exit 99
""", log)
    result = invoke("agent", "probe", "cursor", "--json")
    assert result.exit_code == 0, result.stdout
    snap = payload(result)["snapshot"]
    assert snap["harness"] == "cursor"
    assert snap["auth"] == "not_logged_in"
    assert snap["binary"].endswith("/agent")
    assert log.read_text().splitlines() == ["--version", "--help", "status", "--list-models"]
    assert all(not line.startswith("--print") for line in log.read_text().splitlines())


def test_agent_probe_missing_binary_is_success(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    (tmp_path / "empty-bin").mkdir()
    result = invoke("agent", "probe", "--json", "codex")
    assert result.exit_code == 0, result.stdout
    snap = payload(result)["snapshot"]
    assert snap["binary"] is None
    assert snap["auth"] == "unknown"
    assert snap["features"]["headless"] is False
    info = payload(invoke("agent", "info", "--json", "codex"))
    assert info["snapshot"] == snap


def test_agent_info_without_snapshot_and_host_only_contract():
    result = invoke("agent", "info", "--json", "zcode")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["snapshot"] is None
    assert body["host_only"] is True
    assert body["adapter"] is False
    assert body["launch"]["kind"] == "host"
    assert "Host-only" in body["launch"]["summary"]
    human = invoke("agent", "info", "zcode")
    assert "host-only" in human.stdout
    assert "snapshot: (none)" in human.stdout

    shell = payload(invoke("agent", "info", "--json", "shell"))
    assert shell["adapter"] is True
    assert shell["snapshot"] is None
    assert "prompt_transport" in shell["launch"]["summary"]


def test_agent_probe_rejects_unprobeable_and_unknown():
    for name in ("zcode", "shell", "nope"):
        result = invoke("agent", "probe", "--json", name)
        assert result.exit_code == 1, (name, result.stdout)
        body = payload(result)
        assert body["ok"] is False
        assert "error" in body
    zcode = payload(invoke("agent", "probe", "--json", "zcode"))
    assert "no probe" in zcode["error"]
    unknown = payload(invoke("agent", "info", "--json", "nope"))
    assert unknown["ok"] is False
    assert "unknown harness" in unknown["error"]
    human = invoke("agent", "probe", "shell")
    assert human.exit_code == 1
    assert "error:" in human.stderr or "error:" in human.stdout


def test_agent_status_help_names_columns():
    result = invoke("agent", "status", "--help")
    assert result.exit_code == 0
    for word in ("PROFILE", "STATE", "SINCE", "REASON", "--json", "cooldown",
                 "quota", "override", "resource_status"):
        assert word in result.stdout


def test_agent_status_outside_project(tmp_path, monkeypatch):
    monkeypatch.delenv("ORX_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    result = invoke("agent", "status", "--json")
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False and "error" in body


def test_agent_status_rows_health_and_marks_override(cli_project):
    before = (cli_project / ".orx" / "profiles.toml").read_bytes()
    project = dispatch.open_project()
    try:
        project.store.resource_learn(
            "host-worker",
            status="cooldown",
            last_error_kind="rate_limited",
            note="slow down",
            cooldown_until="2099-01-01T00:00:00+00:00",
        )
        project.store.resource_learn(
            "cli-fake",
            status="exhausted",
            last_error_kind="quota_exhausted",
            quota_reset_at="2099-06-01T00:00:00+00:00",
        )
        worker_since = project.store.resource_row("host-worker").updated_at
        quota_since = project.store.resource_row("cli-fake").updated_at
    finally:
        project.close()

    held = invoke("resource", "set", "--json", "host-planner", "unavailable", "--note", "held")
    assert held.exit_code == 0, held.stdout

    result = invoke("agent", "status", "--json")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    rows = {row["profile"]: row for row in body["profiles"]}
    configured = {
        "host-planner", "host-worker", "host-frontier", "host-external",
        "host-verifier", "host-vision", "cli-fake",
    }
    assert configured <= set(rows)
    assert "orx-host" in rows  # seeded at init, no longer in profiles.toml

    worker = rows["host-worker"]
    assert worker["state"] == "cooldown"
    assert worker["since"] == worker_since == worker["updated_at"]
    assert worker["last_error_kind"] == "rate_limited"
    assert worker["note"] == "slow down"
    assert worker["cooldown_until"] == "2099-01-01T00:00:00+00:00"
    assert worker["override"] is False
    assert worker["reason"] == (
        "rate_limited; slow down; retry 2099-01-01T00:00:00+00:00"
    )

    quota = rows["cli-fake"]
    assert quota["state"] == "exhausted"
    assert quota["since"] == quota_since
    assert quota["quota_reset_at"] == "2099-06-01T00:00:00+00:00"
    assert quota["reason"] == "quota_exhausted; reset 2099-06-01T00:00:00+00:00"

    planner = rows["host-planner"]
    assert planner["state"] == "unavailable"
    assert planner["override"] is True
    assert planner["since"]
    assert "held" in planner["reason"] and planner["reason"].endswith("override")

    untouched = rows["host-frontier"]
    assert untouched["state"] == "unknown"
    assert untouched["since"] is None
    assert untouched["reason"] == ""
    assert untouched["override"] is False

    human = invoke("agent", "status")
    assert human.exit_code == 0
    lines = human.stdout.splitlines()
    header = lines[0]
    assert header.index("PROFILE") < header.index("STATE") < header.index("SINCE") < header.index("REASON")
    by_profile = {line.split()[0]: line for line in lines[1:]}
    assert set(configured) <= set(by_profile)
    assert worker_since in by_profile["host-worker"]
    assert "cooldown" in by_profile["host-worker"]
    assert "retry 2099-01-01T00:00:00+00:00" in by_profile["host-worker"]
    assert "rate_limited" in by_profile["host-worker"]
    assert "slow down" in by_profile["host-worker"]
    assert quota_since in by_profile["cli-fake"]
    assert "reset 2099-06-01T00:00:00+00:00" in by_profile["cli-fake"]
    assert planner["since"] in by_profile["host-planner"]
    assert "override" in by_profile["host-planner"]
    assert "held" in by_profile["host-planner"]
    assert (cli_project / ".orx" / "profiles.toml").read_bytes() == before


def _seed_attempt(store, profile, task_id, started_at, ended_at):
    attempt = store.attempt_create(
        None, "worker", profile, "cli", "codex", "m", "medium",
        task_id=task_id, started=False,
    )
    store.attempt_update(attempt.id, started_at=started_at, ended_at=ended_at)
    return attempt


def test_usage_help_names_contract():
    result = invoke("usage", "--help")
    assert result.exit_code == 0
    for word in ("--profile", "--json", "tasks", "runtime", "accuracy",
                 "unknown", "input_tokens", "runtime_sec"):
        assert word in result.stdout


def test_usage_outside_project(tmp_path, monkeypatch):
    monkeypatch.delenv("ORX_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    result = invoke("usage", "--json")
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False and "error" in body


def test_usage_bad_flag_exits_2(cli_project):
    result = invoke("usage", "--limit", "1")
    assert result.exit_code == 2


def test_usage_empty_and_unknown_profile(cli_project):
    result = invoke("usage", "--json")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    assert body["profiles"] == []
    assert body["observations"] == []
    assert body["coverage"] == []
    assert body["sessions"] == []
    assert body["runs"] == []

    idle = invoke("usage", "--json", "--profile", "host-planner")
    assert idle.exit_code == 0, idle.stdout
    row = payload(idle)["profiles"][0]
    assert row == {
        "profile": "host-planner",
        "tasks": 0,
        "runtime_sec": 0.0,
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "accuracy": "unknown",
    }

    missing = invoke("usage", "--json", "--profile", "no-such-profile")
    assert missing.exit_code == 1
    err = payload(missing)
    assert err["ok"] is False
    assert "unknown profile" in err["error"]


def test_usage_aggregates_tokens_runtime_and_accuracy(cli_project):
    project = dispatch.open_project()
    try:
        store = project.store
        first = _seed_attempt(
            store, "host-worker", "T001",
            "2026-10-03T00:00:00+00:00", "2026-10-03T00:01:00+00:00",
        )
        second = _seed_attempt(
            store, "host-worker", "T001",
            "2026-10-03T00:01:00+00:00", "2026-10-03T00:01:30+00:00",
        )
        store.usage_add(
            first.id, "host-worker", "R001", "T001", 100, 10, 40, "native_cli", "exact",
        )
        store.usage_add(
            second.id, "host-worker", "R001", "T001", 50, 5, 20, "native_cli", "exact",
        )
        _seed_attempt(
            store, "host-verifier", "T002",
            "2026-10-03T00:00:00+00:00", "2026-10-03T00:00:10+00:00",
        )
        estimated = _seed_attempt(
            store, "host-frontier", "T003",
            "2026-10-03T00:00:00+00:00", "2026-10-03T00:00:05+00:00",
        )
        store.usage_add(
            estimated.id, "host-frontier", "R001", "T003", 7, 1, 0, "output_estimate", "estimated",
        )
        covered = _seed_attempt(
            store, "cli-fake", "T004",
            "2026-10-03T00:00:00+00:00", "2026-10-03T00:00:04+00:00",
        )
        _seed_attempt(
            store, "cli-fake", "T005",
            "2026-10-03T00:00:00+00:00", "2026-10-03T00:00:06+00:00",
        )
        store.usage_add(
            covered.id, "cli-fake", "R001", "T004", 9, 2, 1, "native_cli", "exact",
        )
    finally:
        project.close()

    result = invoke("usage", "--json")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    rows = {row["profile"]: row for row in body["profiles"]}
    assert set(rows) == {"host-worker", "host-verifier", "host-frontier", "cli-fake"}

    worker = rows["host-worker"]
    assert worker["tasks"] == 1
    assert worker["runtime_sec"] == 90.0
    assert worker["input_tokens"] == 150
    assert worker["output_tokens"] == 15
    assert worker["cached_input_tokens"] == 60
    assert worker["accuracy"] == "exact"

    verifier = rows["host-verifier"]
    assert verifier["tasks"] == 1
    assert verifier["runtime_sec"] == 10.0
    assert verifier["input_tokens"] is None
    assert verifier["output_tokens"] is None
    assert verifier["cached_input_tokens"] is None
    assert verifier["accuracy"] == "unknown"

    frontier = rows["host-frontier"]
    assert frontier["tasks"] == 1
    assert frontier["runtime_sec"] == 5.0
    assert frontier["input_tokens"] == 7
    assert frontier["accuracy"] == "estimated"

    # One attempt has no observation: tokens stay the observed sum.
    # The profile accuracy label still folds that gap in. Coverage states
    # the measurement on its own: the stored row is exact.
    shell = rows["cli-fake"]
    assert shell["tasks"] == 2
    assert shell["runtime_sec"] == 10.0
    assert shell["input_tokens"] == 9
    assert shell["output_tokens"] == 2
    assert shell["cached_input_tokens"] == 1
    assert shell["accuracy"] == "estimated"
    shell_coverage = next(row for row in body["coverage"] if row["profile"] == "cli-fake")
    assert shell_coverage["attempts"] == 2
    assert shell_coverage["observed"] == 1
    assert shell_coverage["measurement_accuracy"] == "exact"
    worker_coverage = next(row for row in body["coverage"] if row["profile"] == "host-worker")
    assert worker_coverage == {
        "profile": "host-worker",
        "attempts": 2,
        "observed": 2,
        "measurement_accuracy": "exact",
    }
    assert {obs["source"] for obs in body["observations"]} == {"native_cli", "output_estimate"}
    assert "fee" not in body and "cost" not in body

    filtered = payload(invoke("usage", "--json", "--profile", "host-worker"))
    assert [row["profile"] for row in filtered["profiles"]] == ["host-worker"]
    assert filtered["profiles"][0]["accuracy"] == "exact"

    human = invoke("usage")
    assert human.exit_code == 0
    lines = human.stdout.splitlines()
    header = lines[0]
    assert header.index("PROFILE") < header.index("TASKS") < header.index("RUNTIME")
    assert "ACCURACY" in header
    text = human.stdout
    assert "host-worker" in text and "exact" in text
    assert "host-verifier" in text and "unknown" in text
    assert "0:01:30" in text


def test_config_list_json_error_on_bad_env(cli_project, monkeypatch):
    before = (cli_project / ".orx" / "config.toml").read_bytes()
    monkeypatch.setenv("ORX_RUNTIME_MAX_PARALLEL", "nope")
    result = invoke("config", "list", "--json")
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False
    assert "ORX_RUNTIME_MAX_PARALLEL" in body["error"]
    assert (cli_project / ".orx" / "config.toml").read_bytes() == before


def test_completion_emits_scripts_and_rejects_unknown_shell():
    result = invoke("completion", "zsh")
    assert result.exit_code == 0
    assert "_ORX_COMPLETE" in result.stdout
    assert invoke("completion", "bash").exit_code == 0
    assert invoke("completion", "fish").exit_code == 0
    bad = invoke("completion", "tcsh")
    assert bad.exit_code == 1


def test_resource_clear_cli_roundtrip(cli_project):
    result = invoke("resource", "set", "host-worker", "unavailable", "--note", "demo")
    assert result.exit_code == 0
    cleared = invoke("resource", "clear", "host-worker")
    assert cleared.exit_code == 0
    listing = invoke("resource", "list", "--json")
    import json as _json
    rows = {r["profile"]: r for r in _json.loads(listing.stdout)["resources"]}
    assert rows["host-worker"]["status"] == "unavailable"  # clear drops override, keeps status


def test_completion_json_envelope():
    result = invoke("completion", "zsh", "--json")
    assert result.exit_code == 0
    import json as _json
    payload = _json.loads(result.stdout)
    assert payload["ok"] and "_ORX_COMPLETE" in payload["script"]


def test_usage_unknown_profile_exits_one(cli_project):
    assert invoke("usage", "--profile", "no-such").exit_code == 1


def test_config_set_user_layer_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from conftest import make_project
    project = make_project(tmp_path)
    project.close()
    monkeypatch.setenv("ORX_CONFIG_DIR", str(tmp_path / "u"))
    result = invoke("config", "set", "--user", "plan.depth", "light")  # options precede positionals (typer 0.27)
    assert result.exit_code == 0
    # the project layer explicitly sets plan.depth, so it correctly wins;
    # the user write must still have landed in the user layer file
    assert "light" in (tmp_path / "u" / "config.toml").read_text()
    import json as _json
    got = _json.loads(invoke("config", "get", "plan.depth", "--json").stdout)
    assert got["value"] == "auto" and "project" in str(got)


def test_agent_status_json_envelope(cli_project):
    result = invoke("agent", "status", "--json")
    assert result.exit_code == 0
    import json as _json
    payload = _json.loads(result.stdout)
    assert payload["ok"] and isinstance(payload["profiles"], list)


def test_agent_info_without_snapshot_is_honest():
    result = invoke("agent", "info", "codex", "--json")
    assert result.exit_code in (0, 1)  # honest either way; snapshot optional


# ---------------------------------------------------------------------------
# Skill install: default set, explicit names, unknown-name envelope


def _fake_cli_packaged_skills(tmp_path, monkeypatch, names=("orx-controller", "orx-agent", "orx-pbv")):
    from orx import skills as skills_mod
    root = tmp_path / "cli-packaged-skills"
    for name in names:
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\n")
    monkeypatch.setattr(skills_mod, "packaged_skills_dir", lambda: root)
    return root


def test_skill_install_cli_default_set(tmp_path, monkeypatch):
    home = tmp_path / "cli-home"
    (home / ".zcode" / "skills").mkdir(parents=True)
    _fake_cli_packaged_skills(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: home)

    result = invoke("skill", "install", "--json")

    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    assert sorted(body["installed"]) == ["orx-agent", "orx-controller"]
    assert body["refreshed"] == []
    assert body["canonical_root"] == str(home / ".agents" / "skills")
    assert not (home / ".agents" / "skills" / "orx-pbv").exists()


def test_skill_install_cli_explicit_name(tmp_path, monkeypatch):
    home = tmp_path / "cli-home"
    (home / ".zcode" / "skills").mkdir(parents=True)
    _fake_cli_packaged_skills(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: home)

    result = invoke("skill", "install", "orx-pbv", "--json")

    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    assert body["installed"] == ["orx-pbv"]
    assert (home / ".agents" / "skills" / "orx-pbv" / "SKILL.md").is_file()
    assert (home / ".zcode" / "skills" / "orx-pbv").is_symlink()
    assert not (home / ".agents" / "skills" / "orx-agent").exists()

    # human output matches the command's existing style
    text = invoke("skill", "install", "orx-pbv")
    assert text.exit_code == 0
    assert f"canonical: {home / '.agents' / 'skills'}" in text.stdout
    assert "refreshed orx-pbv" in text.stdout


def test_skill_install_cli_unknown_name_error_envelope(tmp_path, monkeypatch):
    home = tmp_path / "cli-home"
    _fake_cli_packaged_skills(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: home)

    result = invoke("skill", "install", "nope", "--json")

    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False
    assert "nope" in body["error"]
    for available in ("orx-controller", "orx-agent", "orx-pbv"):
        assert available in body["error"]
    assert not (home / ".agents").exists()


def test_one_active_goal_still_rejected(cli_project):
    first = invoke("goal", "new", "--json", "--objective", "ship it", "--acceptance", "tests pass")
    assert first.exit_code == 0
    assert payload(first)["goal"]["objective"] == "ship it"
    second = invoke("goal", "new", "--json", "--objective", "another", "--acceptance", "tests pass")
    assert second.exit_code == 1
    body = payload(second)
    assert body["ok"] is False
    assert "G001" in body["error"]


def _open_attempts():
    project = dispatch.open_project()
    try:
        return list(project.store.attempts_all())
    finally:
        project.close()


def test_session_flag_precedes_env_on_host_paths(cli_project, monkeypatch):
    monkeypatch.setenv("ORX_SESSION_REF", "from-env")
    invoke("goal", "new", "--json", "--objective", "ship it", "--acceptance", "tests pass")

    rejected = invoke("plan", "--json", "--session", "bad ref")
    assert rejected.exit_code == 1
    assert payload(rejected)["ok"] is False
    assert "malformed" in payload(rejected)["error"]
    assert _open_attempts() == []

    planned = invoke("plan", "--json", "--session", "from-flag")
    assert planned.exit_code == 0, planned.stdout
    planner = next(a for a in _open_attempts() if a.role == "planner")
    assert planner.session_ref == "from-flag"
    assert planner.ended_at is None

    plan = ir_for(type("G", (), {"id": "G001"})(), [
        task_spec("T001", acceptance=["tests pass"], verification=["agent: looks fine"]),
    ])
    plan_path = cli_project / "plan.json"
    plan_path.write_text(json.dumps(plan))
    bad_submit = invoke(
        "plan", "submit", "--json", "--session", "   ", "--file", str(plan_path),
    )
    assert bad_submit.exit_code == 1
    assert "malformed" in payload(bad_submit)["error"] or "empty" in payload(bad_submit)["error"]
    planner = next(a for a in _open_attempts() if a.role == "planner")
    assert planner.session_ref == "from-flag"
    assert planner.ended_at is None
    project = dispatch.open_project()
    try:
        run = project.store.run_for_goal("G001")
        assert project.store.revision_active(run.id) is None
    finally:
        project.close()

    submitted = invoke(
        "plan", "submit", "--json", "--session", "from-submit", "--file", str(plan_path),
    )
    assert submitted.exit_code == 0, submitted.stdout
    planner = next(a for a in _open_attempts() if a.role == "planner")
    assert planner.session_ref == "from-submit"
    assert planner.ended_at is not None

    invoke("run", "--json")
    bad_claim = invoke("task", "claim", "--json", "T001", "--session", "")
    assert bad_claim.exit_code == 1
    assert payload(bad_claim)["ok"] is False
    assert "empty" in payload(bad_claim)["error"] or "malformed" in payload(bad_claim)["error"]
    assert _task_status("T001") == "waiting_host"
    worker = next(a for a in _open_attempts() if a.role == "worker")
    # parking stamps nothing: from-env names the controller, not the
    # worker subagent that will claim (G005)
    assert worker.session_ref is None
    assert worker.started_at is None

    claimed = invoke("task", "claim", "--json", "T001", "--session", "from-claim")
    assert claimed.exit_code == 0, claimed.stdout
    assert payload(claimed)["session_ref"] == "from-claim"
    assert payload(claimed)["status"] == "running"
    worker = next(a for a in _open_attempts() if a.role == "worker")
    assert worker.session_ref == "from-claim"

    (cli_project / "ev.json").write_text(
        json.dumps({"status": "passed", "summary": "done", "checks": [], "artifacts": []})
    )
    finished = invoke("task", "complete", "--json", "T001", "--evidence", "ev.json")
    assert payload(finished)["status"] == "verifying"

    bad_verify = invoke(
        "verify", "submit", "--json", "T001", "--result", "pass", "--session", "has space",
    )
    assert bad_verify.exit_code == 1
    assert "malformed" in payload(bad_verify)["error"]
    assert not any(a.role == "verifier" for a in _open_attempts())
    assert _task_status("T001") == "verifying"

    verdict = invoke(
        "verify", "submit", "--json", "T001", "--result", "pass", "--session", "from-verify",
    )
    assert verdict.exit_code == 0, verdict.stdout
    assert payload(verdict)["status"] == "passed"
    assert payload(verdict)["result"] == "pass"
    verifier = next(a for a in _open_attempts() if a.role == "verifier")
    assert verifier.session_ref == "from-verify"


def test_absent_session_stays_null_and_park_writes_no_ref(cli_project, monkeypatch):
    monkeypatch.delenv("ORX_SESSION_REF", raising=False)
    invoke("goal", "new", "--json", "--objective", "ship it", "--acceptance", "tests pass")
    assert invoke("plan", "--json").exit_code == 0
    planner = next(a for a in _open_attempts() if a.role == "planner")
    assert planner.session_ref is None

    monkeypatch.setenv("ORX_SESSION_REF", "parked-sess")
    plan = ir_for(type("G", (), {"id": "G001"})(), [
        task_spec("T001", acceptance=["tests pass"]),
    ])
    (cli_project / "plan.json").write_text(json.dumps(plan))
    assert invoke("plan", "submit", "--json", "--file", str(cli_project / "plan.json")).exit_code == 0
    invoke("run", "--json")
    # parking under a set env still writes no ref for the worker attempt
    worker = next(a for a in _open_attempts() if a.role == "worker")
    assert worker.session_ref is None
    monkeypatch.delenv("ORX_SESSION_REF", raising=False)
    claimed = invoke("task", "claim", "--json", "T001")
    assert claimed.exit_code == 0, claimed.stdout
    worker = next(a for a in _open_attempts() if a.role == "worker")
    assert worker.session_ref is None
    assert payload(claimed)["session_ref"] is None


def _task_status(task_id):
    body = payload(invoke("task", "list", "--json"))
    return next(task["status"] for task in body["tasks"] if task["id"] == task_id)


def _seed_host_attempt(session_ref="sess-host"):
    invoke("goal", "new", "--json", "--objective", "ship it", "--acceptance", "tests pass")
    invoke("plan", "--json")
    plan = ir_for(type("G", (), {"id": "G001"})(), [
        task_spec("T001", acceptance=["tests pass"]),
    ])
    (Path.cwd() / "plan.json").write_text(json.dumps(plan))
    invoke("plan", "submit", "--json", "--file", "plan.json")
    project = dispatch.open_project()
    try:
        run = project.store.run_for_goal("G001")
        revision = project.store.revision_active(run.id)
        attempt = project.store.attempt_create(
            revision.id, "worker", "host-worker", "host", "zcode", "m", "medium",
            task_id="T001", session_ref=session_ref,
        )
        bare = project.store.attempt_create(
            None, "worker", "host-worker", "host", "zcode", "m", "medium",
        )
        return attempt.id, bare.id, run.id
    finally:
        project.close()


def test_usage_record_round_trip_and_conflicts(cli_project, monkeypatch):
    monkeypatch.delenv("ORX_SESSION_REF", raising=False)
    attempt_id, bare_id, run_id = _seed_host_attempt()

    missing = invoke("usage", "record", "--attempt", str(attempt_id), "--output", "1")
    assert missing.exit_code == 2
    guessed = invoke(
        "usage", "record", "--attempt", str(attempt_id),
        "--input", "3", "--output", "1", "--profile", "other",
    )
    assert guessed.exit_code == 2

    recorded = invoke(
        "usage", "record", "--json",
        "--attempt", str(attempt_id), "--input", "3", "--output", "1",
    )
    assert recorded.exit_code == 0, recorded.stdout
    body = payload(recorded)
    assert body["ok"] is True
    assert body["source"] == "host_report"
    assert body["accuracy"] == "exact"
    assert body["idempotent"] is False
    assert body["profile"] == "host-worker"
    assert body["run_id"] == run_id
    assert body["task_id"] == "T001"
    assert body["input_tokens"] == 3
    assert body["output_tokens"] == 1
    assert body["cached_input_tokens"] is None
    assert "fee" not in body and "cost" not in body

    again = invoke(
        "usage", "record", "--json",
        "--attempt", str(attempt_id), "--input", "3", "--output", "1",
    )
    assert again.exit_code == 0, again.stdout
    assert payload(again)["idempotent"] is True

    conflict = invoke(
        "usage", "record", "--json",
        "--attempt", str(attempt_id), "--input", "9", "--output", "1",
    )
    assert conflict.exit_code == 1
    assert "refusing to replace" in payload(conflict)["error"]

    cached = invoke(
        "usage", "record", "--json", "--attempt", str(bare_id),
        "--input", "4", "--output", "2", "--cached", "40", "--accuracy", "estimated",
    )
    assert cached.exit_code == 1
    assert "no run association" in payload(cached)["error"]

    negative = invoke(
        "usage", "record", "--json",
        "--attempt", str(attempt_id), "--input=-1", "--output", "1",
    )
    assert negative.exit_code == 1
    assert "nonnegative" in payload(negative)["error"]

    shown = invoke("usage", "--json")
    assert shown.exit_code == 0, shown.stdout
    usage_body = payload(shown)
    reports = [row for row in usage_body["observations"] if row["source"] == "host_report"]
    assert len(reports) == 1
    report = reports[0]
    assert report["input_tokens"] == 3
    assert report["output_tokens"] == 1
    assert report["cached_input_tokens"] is None
    assert report["accuracy"] == "exact"
    assert report["session_ref"] == "sess-host"
    assert report["profile"] == "host-worker"
    assert report["run_id"] == run_id
    worker = next(row for row in usage_body["profiles"] if row["profile"] == "host-worker")
    assert worker["input_tokens"] == 3
    session = next(row for row in usage_body["sessions"] if row["attempt"] == attempt_id)
    assert session["session_ref"] == "sess-host"
    run_row = next(row for row in usage_body["runs"] if row["id"] == run_id)
    assert "started_at" in run_row and "completed_at" in run_row

    human = invoke("usage")
    assert human.exit_code == 0
    assert "host_report" in human.stdout
    assert "measurement" in human.stdout
    assert "PROFILE" in human.stdout.splitlines()[0]


def test_usage_record_allows_cached_above_input(cli_project, monkeypatch):
    monkeypatch.delenv("ORX_SESSION_REF", raising=False)
    attempt_id, _bare, _run = _seed_host_attempt(session_ref=None)
    result = invoke(
        "usage", "record", "--json",
        "--attempt", str(attempt_id),
        "--input", "10", "--output", "2", "--cached", "80", "--accuracy", "estimated",
    )
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["cached_input_tokens"] == 80
    assert body["input_tokens"] == 10
    assert body["accuracy"] == "estimated"
    shown = payload(invoke("usage", "--json"))
    report = next(row for row in shown["observations"] if row["source"] == "host_report")
    assert report["cached_input_tokens"] == 80
    assert report["input_tokens"] == 10
    coverage = next(row for row in shown["coverage"] if row["profile"] == "host-worker")
    assert coverage["measurement_accuracy"] == "estimated"
    assert coverage["observed"] >= 1
