"""Timeline read model: ordering, source coverage, filters, and the JSON envelope."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from orx import dispatch, verify
from orx.cli import app
from orx.records import NotFoundError

from conftest import ir_for, task_spec, write_evidence

runner = CliRunner()

SCHEMA_TABLES = {
    "meta",
    "goals",
    "runs",
    "plan_revisions",
    "planning_assignments",
    "tasks",
    "task_dependencies",
    "task_events",
    "attempts",
    "evidence",
    "verifications",
    "routing_decisions",
    "resource_status",
    "usage_observations",
    "external_events",
    "inbox_items",
}


def invoke(*args):
    return runner.invoke(app, list(args))


def payload(result):
    return json.loads(result.stdout)


def _install_clock(monkeypatch):
    counter = {"n": 0}
    base = datetime(2026, 10, 3, 8, 0, 0, tzinfo=timezone.utc)

    def now():
        counter["n"] += 1
        return (base + timedelta(seconds=counter["n"])).isoformat(timespec="seconds")

    monkeypatch.setattr("orx.state.now", now)
    monkeypatch.setattr("orx.dispatch.db_now", now)


def _index(entries, pred):
    return next(i for i, entry in enumerate(entries) if pred(entry))


def test_timeline_orders_filters_and_envelope(project, tmp_path, monkeypatch):
    _install_clock(monkeypatch)
    goal, run = dispatch.create_goal(
        project,
        objective="Ship the timeline",
        acceptance=["marker file exists"],
        constraints=[],
        context="",
    )
    routed = dispatch.plan_route(project)
    assert routed["mode"] == "host_required"
    dispatch.submit_plan(
        project,
        ir_for(goal, [
            task_spec(
                "T001",
                objective="write the marker",
                acceptance=["marker file exists"],
                verification=["test -f ready.txt", "agent: confirm the marker"],
            )
        ]),
    )
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    # Red-complete contract: the marker is missing, so the completion is
    # REFUSED — but the refused gate already recorded its red rows, and the
    # timeline sees them while the task stays running.
    with pytest.raises(verify.DeliveryRejected):
        dispatch.task_complete(project, "T001", str(write_evidence(tmp_path)))
    assert any(entry["event"] == "verify.fail" for entry in dispatch.timeline(project)["entries"])
    midway = dispatch.timeline(project)
    assert any(entry["event"] == "verify.fail" for entry in midway["entries"])
    (tmp_path / "ready.txt").write_text("ok\n")
    completed = dispatch.task_complete(
        project, "T001", str(write_evidence(tmp_path, "e1.json"))
    )
    assert completed["status"] == "verifying"
    # The round's agent verdict fails: the task lands failed via verify_fail.
    verdict = dispatch.verify_submit(
        project,
        "T001",
        "fail",
        "agent: confirm the marker",
        str(write_evidence(tmp_path, "v.json")),
        reason="marker unreadable",
    )
    assert verdict["status"] == "failed"
    dispatch.task_retry(project, "T001")
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")
    completed = dispatch.task_complete(
        project, "T001", str(write_evidence(tmp_path, "e2.json"))
    )
    assert completed["status"] == "verifying"
    verdict = dispatch.verify_submit(
        project,
        "T001",
        "pass",
        "agent: confirm the marker",
        str(write_evidence(tmp_path, "v2.json")),
    )
    assert verdict["status"] == "passed"

    goal2, run2 = dispatch.create_goal(
        project,
        objective="Second goal",
        acceptance=["second"],
        constraints=[],
        context="",
    )

    full = dispatch.timeline(project)
    entries = full["entries"]
    assert full["count"] == len(entries) > 0
    stamps = [entry["ts"] for entry in entries]
    assert stamps == sorted(stamps)
    assert all(stamps[i] < stamps[i + 1] for i in range(len(stamps) - 1))
    for entry in entries:
        assert set(entry) == {"ts", "actor", "event", "detail"}

    def at(pred):
        return _index(entries, pred)

    assert at(lambda e: e["event"] == "goal.created" and e["detail"].startswith(goal.id)) < at(
        lambda e: e["event"] == "run.created" and run.id in e["detail"]
    )
    # Host planning creates the waiting assignment, opens one planner attempt
    # on it, then persists the route so the decision can name that attempt.
    assert at(lambda e: e["event"] == "run.created" and run.id in e["detail"]) < at(
        lambda e: e["event"] == "plan.assign"
    )
    assert at(lambda e: e["event"] == "plan.assign") < at(
        lambda e: e["event"] == "attempt.start" and e["detail"].startswith("planner")
    )
    assert at(lambda e: e["event"] == "attempt.start" and e["detail"].startswith("planner")) < at(
        lambda e: e["event"] == "route" and e["detail"].startswith("planner")
    )
    assert at(lambda e: e["event"] == "route" and e["detail"].startswith("planner")) < at(
        lambda e: e["event"] == "plan.submit"
    )
    assert at(lambda e: e["event"] == "plan.submit") < at(
        lambda e: e["event"] == "route" and e["detail"].startswith("worker")
    )
    claims = [i for i, entry in enumerate(entries) if entry["event"] == "claim"]
    completes = [i for i, entry in enumerate(entries) if entry["event"] == "complete"]
    fail_i = at(lambda e: e["event"] == "verify_fail")
    retry_i = at(lambda e: e["event"] == "retry")
    passed_i = at(
        lambda e: e["event"] == "verify.pass" and e["detail"].startswith("T001 agent:")
    )
    assert len(claims) == 2 and len(completes) == 2
    assert at(lambda e: e["event"] == "route" and e["detail"].startswith("worker")) < claims[0]
    assert claims[0] < completes[0] < fail_i < retry_i < claims[1] < completes[1] < passed_i
    roles = [entry["detail"].split()[0] for entry in entries if entry["event"] == "attempt.start"]
    assert {"planner", "worker", "verifier"} <= set(roles)

    events = {entry["event"] for entry in entries}
    assert {
        "goal.created",
        "run.created",
        "plan.assign",
        "plan.submit",
        "route",
        "attempt.start",
        "attempt.end",
        "claim",
        "complete",
        "verify.pass",
        "verify_fail",
        "retry",
    } <= events

    by_run = dispatch.timeline(project, run_id=run.id)
    assert any(entry["event"] == "claim" for entry in by_run["entries"])
    assert all(goal2.id not in entry["detail"] for entry in by_run["entries"])
    assert all(run2.id not in entry["detail"] for entry in by_run["entries"])
    by_run2 = dispatch.timeline(project, run_id=run2.id)
    assert any(
        entry["event"] == "goal.created" and goal2.id in entry["detail"]
        for entry in by_run2["entries"]
    )
    assert all(entry["event"] != "claim" for entry in by_run2["entries"])

    by_task = dispatch.timeline(project, task_id="T001")
    assert any(entry["event"] == "claim" for entry in by_task["entries"])
    assert all(
        entry["event"] not in {"goal.created", "run.created", "plan.assign", "plan.submit"}
        for entry in by_task["entries"]
    )

    by_planner = dispatch.timeline(project, profile="host-planner")
    assert any(entry["event"] == "plan.assign" for entry in by_planner["entries"])
    assert any(
        entry["event"] == "attempt.start" and entry["detail"].startswith("planner")
        for entry in by_planner["entries"]
    )
    assert all(entry["event"] != "claim" for entry in by_planner["entries"])

    by_worker = dispatch.timeline(project, profile="host-worker")
    assert any(entry["event"] == "claim" for entry in by_worker["entries"])
    assert all(not entry["detail"].startswith("planner") for entry in by_worker["entries"])

    by_verifier = dispatch.timeline(project, profile="host-verifier")
    assert any(
        entry["event"] == "attempt.start" and entry["detail"].startswith("verifier")
        for entry in by_verifier["entries"]
    )
    assert any(entry["event"] == "verify.pass" for entry in by_verifier["entries"])
    assert all(entry["event"] != "claim" for entry in by_verifier["entries"])

    limited = dispatch.timeline(project, limit=3)
    assert limited["entries"] == entries[-3:]
    assert limited["count"] == 3

    combined = dispatch.timeline(
        project, run_id=run.id, task_id="T001", profile="host-worker", limit=2
    )
    assert combined["count"] == 2
    assert [entry["ts"] for entry in combined["entries"]] == sorted(
        entry["ts"] for entry in combined["entries"]
    )

    with pytest.raises(NotFoundError):
        dispatch.timeline(project, run_id="R999")
    with pytest.raises(NotFoundError):
        dispatch.timeline(project, task_id="T999")

    names = {
        row[0]
        for row in project.store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert names == SCHEMA_TABLES

    result = invoke("timeline", "--json", "--run", run.id, "--task", "T001", "--limit", "2")
    assert result.exit_code == 0, result.stdout
    body = payload(result)
    assert body["ok"] is True
    assert "error" not in body
    assert body["count"] == 2 == len(body["entries"])
    assert body["entries"] == dispatch.timeline(
        project, run_id=run.id, task_id="T001", limit=2
    )["entries"]
    assert [entry["ts"] for entry in body["entries"]] == sorted(
        entry["ts"] for entry in body["entries"]
    )

    human = invoke("timeline", "--limit", "1")
    assert human.exit_code == 0, human.stdout
    line = human.stdout.strip().splitlines()[-1]
    assert re.match(r"^\d{2}:\d{2}:\d{2}  \S+  \S+  .+", line)

    missing = invoke("timeline", "--json", "--run", "R999")
    assert missing.exit_code == 1
    assert payload(missing)["ok"] is False
    assert "error" in payload(missing)

    usage = invoke("timeline", "--limit", "0")
    assert usage.exit_code == 2


def test_timeline_outside_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke("timeline", "--json")
    assert result.exit_code == 1
    body = payload(result)
    assert body["ok"] is False and "error" in body
