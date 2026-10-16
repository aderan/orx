"""G009 closeout: automatic zcode_db usage backfill at attempt closure.

Every test is deterministic: a fake zcode database plus ORX's own dispatch
paths — no paid model calls. The incident's 21-of-26 host gap closed at the
moment the books can first be written (attempt terminal), and every
non-recordable outcome is STATED (missing reason), never zero-filled.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from conftest import HOST_CONFIG_TOML, HOST_PROFILES_TOML, ir_for, make_project, task_spec  # noqa: E402
from test_host_progress_e2e import make_zcode_usage_db  # noqa: E402
from orx import dispatch  # noqa: E402
from orx.records import ConflictError  # noqa: E402


def _usage_rows(project, attempt_id):
    return [r for r in project.store.usage_rows() if r["attempt_id"] == attempt_id]


def _host_goal(project, *, verification=("true",)):
    goal = dispatch.create_goal(
        project, objective="auto backfill", acceptance=["done"],
        constraints=[], context="",
    )[0]
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance,
                  verification=list(verification)),
    ]))
    return goal


def _revision_for(project, goal):
    return project.store.revision_active(
        project.store.run_for_goal(goal.id).id)


def test_complete_closes_the_books_automatically(tmp_path, monkeypatch):
    """A host worker attempt with a bound session gets its zcode_db usage
    row the moment it completes — no manual `orx usage zcode` call."""
    zdb = tmp_path / "zcode.sqlite"
    make_zcode_usage_db(
        zdb, str(tmp_path),
        sessions=[("sess-worker", None, None)],
        usages=[("r1", "lr1", 0, "sess-worker", "completed", 1000, 50, 980, 1050)],
    )
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    try:
        goal = _host_goal(project)
        dispatch.run_slice(project)  # parks the host assignment
        revision = _revision_for(project, goal)
        attempt = project.store.attempt_latest_for_task(revision.id, "T001")
        assert (attempt.driver, attempt.harness) == ("host", "zcode")
        # the real flow: the parked worker CLAIMS (waiting_host -> running,
        # session bound) before completing
        claimed = dispatch.task_claim(project, "T001", session="sess-worker")
        assert claimed["attempt"] == attempt.id

        import json
        evidence = tmp_path / "evidence.json"
        evidence.write_text(json.dumps({
            "status": "passed", "summary": "done",
            "checks": [{"command": "true", "exit_code": 0, "log": None}],
            "artifacts": [],
        }))
        dispatch.task_complete(project, "T001", str(evidence),
                                attempt_id=attempt.id)
        rows = _usage_rows(project, attempt.id)
        assert len(rows) == 1 and rows[0]["source"] == "zcode_db"
        assert (rows[0]["input_tokens"], rows[0]["output_tokens"],
                rows[0]["cached_input_tokens"]) == (1000, 50, 980)
        assert project.store.attempt_get(attempt.id).usage_missing_reason is None
    finally:
        project.close()


def test_unbound_host_attempt_states_missing_on_close(tmp_path, monkeypatch):
    """No bound session and nothing discoverable: the closure records the
    host_session_unbound missing reason instead of staying silent."""
    zdb = tmp_path / "zcode.sqlite"
    make_zcode_usage_db(zdb, str(tmp_path), sessions=[], usages=[])
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    try:
        goal = _host_goal(project)
        dispatch.run_slice(project)
        revision = _revision_for(project, goal)
        attempt = project.store.attempt_latest_for_task(revision.id, "T001")
        # claim without a session: running but unbound (no discovery hit)
        dispatch.task_claim(project, "T001")
        dispatch.task_fail(project, "T001", reason="operator closed it")
        assert project.store.attempt_get(attempt.id).usage_missing_reason == \
            "host_session_unbound"
        assert _usage_rows(project, attempt.id) == []
    finally:
        project.close()


def test_subtree_conflict_is_stated_not_raised(tmp_path, monkeypatch):
    """When the session subtree already belongs to another attempt's book,
    closure records zcode_subtree_conflict — the guard that prevents double
    counting, expressed as a missing reason instead of a crash."""
    zdb = tmp_path / "zcode.sqlite"
    make_zcode_usage_db(
        zdb, str(tmp_path),
        sessions=[("sess-a", None, None), ("sess-b", "sess-a", None)],
        usages=[("r1", "lr1", 0, "sess-b", "completed", 100, 5, 90, 105)],
    )
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    try:
        _host_goal(project)
        goal = project.store.goal_active()
        revision = project.store.revision_active(
            project.store.run_for_goal(goal.id).id)
        owner = project.store.attempt_create(
            revision_row_id=revision.id, role="worker", profile="p",
            driver="host", harness="zcode", model_id="m",
            requested_effort="high", task_id="T001",
        )
        project.store.attempt_update(owner.id, session_ref="sess-a")
        project.store.usage_record_zcode(owner.id, 100, 5, 90, "exact")

        inner = project.store.attempt_create(
            revision_row_id=revision.id, role="verifier", profile="p",
            driver="host", harness="zcode", model_id="m",
            requested_effort="medium", task_id="T001",
        )
        project.store.attempt_update(inner.id, session_ref="sess-b")
        result = dispatch._usage_autobill(project, inner.id)
        assert result["status"] == "missing"
        assert result["reason"] == "zcode_subtree_conflict"
        assert project.store.attempt_get(inner.id).usage_missing_reason == \
            "zcode_subtree_conflict"
        # the manual path still raises for the same overlap
        with pytest.raises(ConflictError):
            dispatch.usage_zcode_record(project, inner.id)
    finally:
        project.close()


def test_disabled_config_and_absent_db_skip_quietly(tmp_path, monkeypatch):
    """usage.auto_backfill_zcode=false, or no zcode database at all: no rows,
    no missing reasons — ORX cannot know, so the attempt stays unassessed
    for the usage views to count honestly."""
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML  # default: backfill on, but no zcode db file
    project = make_project(tmp_path, config, HOST_PROFILES_TOML)
    try:
        _host_goal(project)
        dispatch.run_slice(project)
        goal = project.store.goal_active()
        revision = project.store.revision_active(
            project.store.run_for_goal(goal.id).id)
        attempt = project.store.attempt_latest_for_task(revision.id, "T001")
        # ORX_ZCODE_DB unset in this test AND the default path exists on this
        # machine — point it at a nonexistent file for determinism.
        monkeypatch.setenv("ORX_ZCODE_DB", str(tmp_path / "absent.sqlite"))
        result = dispatch._usage_autobill(project, attempt.id)
        assert result["status"] == "skipped" and result["reason"] == "zcode_db_absent"
        assert _usage_rows(project, attempt.id) == []
        assert project.store.attempt_get(attempt.id).usage_missing_reason is None
    finally:
        project.close()


def test_cli_attempts_are_never_autobilled(tmp_path, monkeypatch):
    """CLI attempts carry their own native usage capture; the auto-backfill
    pre-check skips them rather than raising."""
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML.replace(
        'profiles = ["host-worker", "host-external"]', 'profiles = ["cli-fake"]')
    profiles = HOST_PROFILES_TOML.replace('harness = "shell"', 'harness = "shell"')
    project = make_project(tmp_path, config, profiles)
    try:
        goal = _host_goal(project)
        dispatch.run_slice(project)  # CLI worker runs `true` and passes
        revision = _revision_for(project, goal)
        attempt = project.store.attempt_latest_for_task(revision.id, "T001")
        assert attempt.driver == "cli"
        result = dispatch._usage_autobill(project, attempt.id)
        assert result["status"] == "skipped"
        assert result["reason"] == "not a host zcode attempt"
    finally:
        project.close()


def test_project_config_can_disable_backfill(tmp_path, monkeypatch):
    zdb = tmp_path / "zcode.sqlite"
    make_zcode_usage_db(
        zdb, str(tmp_path),
        sessions=[("sess-worker", None, None)],
        usages=[("r1", "lr1", 0, "sess-worker", "completed", 10, 1, 9, 11)],
    )
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    monkeypatch.chdir(tmp_path)
    config = HOST_CONFIG_TOML + "\n[usage]\nauto_backfill_zcode = false\n"
    project = make_project(tmp_path, config, HOST_PROFILES_TOML)
    try:
        assert project.config.usage_auto_backfill_zcode is False
        _host_goal(project)
        dispatch.run_slice(project)
        goal = project.store.goal_active()
        revision = project.store.revision_active(
            project.store.run_for_goal(goal.id).id)
        attempt = project.store.attempt_latest_for_task(revision.id, "T001")
        project.store.attempt_update(attempt.id, session_ref="sess-worker")
        result = dispatch._usage_autobill(project, attempt.id)
        assert result["status"] == "skipped"
        assert result["reason"] == "usage.auto_backfill_zcode disabled"
        assert _usage_rows(project, attempt.id) == []
        # the manual path stays available regardless
        manual = dispatch.usage_zcode_record(project, attempt.id)
        assert manual["status"] == "recorded"
    finally:
        project.close()


def test_cli_complete_forwards_late_session_binding(tmp_path, monkeypatch):
    """`orx task complete --session` must reach dispatch.task_complete: the
    G009 run had an attempt whose late identity binding was silently dropped
    by the CLI layer (session accepted, never forwarded)."""
    from orx.cli import task_complete  # noqa: F401  (import proves the signature)
    import inspect
    sig = inspect.signature(task_complete)
    assert "session" in sig.parameters
    source = inspect.getsource(task_complete)
    assert "session=session" in source, (
        "the CLI accepts --session but does not forward it to dispatch"
    )


# ---------------------------------------------------------------------------
# G010 T001: closure-time reads that must not write a wrong book.


def _parked_host_attempt(project, goal):
    revision = project.store.revision_active(
        project.store.run_for_goal(goal.id).id)
    return project.store.attempt_latest_for_task(revision.id, "T001")


def test_unsettled_usage_states_missing_then_records_when_settled(
        tmp_path, monkeypatch):
    """用量尚未稳定: a closure-time read that catches a model_usage row still
    mid-flight (status outside the terminal vocabulary) must NOT record its
    provisional sums — the singleton guard would then make the later true
    number unrecordable. It states zcode_usage_unsettled instead, and a later
    re-run records the settled numbers and clears the reason."""
    zdb = tmp_path / "zcode.sqlite"
    make_zcode_usage_db(
        zdb, str(tmp_path),
        sessions=[("sess-worker", None, None)],
        usages=[("r1", "lr1", 0, "sess-worker", "streaming", 1000, 50, 980, 1050)],
    )
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    try:
        goal = _host_goal(project)
        dispatch.run_slice(project)
        attempt = _parked_host_attempt(project, goal)
        project.store.attempt_update(attempt.id, session_ref="sess-worker")
        result = dispatch._usage_autobill(project, attempt.id)
        assert result["status"] == "missing"
        assert result["reason"] == "zcode_usage_unsettled"
        assert result["settling_request_rows"] == 1
        assert _usage_rows(project, attempt.id) == []
        assert project.store.attempt_get(attempt.id).usage_missing_reason == \
            "zcode_usage_unsettled"
        # the stream settles: the same rows now carry a terminal status
        make_zcode_usage_db(
            zdb, str(tmp_path),
            sessions=[("sess-worker", None, None)],
            usages=[("r1", "lr1", 0, "sess-worker", "completed", 1000, 50, 980, 1050)],
        )
        settled = dispatch._usage_autobill(project, attempt.id)
        assert settled["status"] == "recorded"
        rows = _usage_rows(project, attempt.id)
        assert len(rows) == 1 and rows[0]["source"] == "zcode_db"
        assert (rows[0]["input_tokens"], rows[0]["output_tokens"]) == (1000, 50)
        assert project.store.attempt_get(attempt.id).usage_missing_reason is None
    finally:
        project.close()


def test_duplicate_backfill_is_distinguished_from_subtree_conflict(
        tmp_path, monkeypatch):
    """重复补录: the attempt ALREADY has a zcode_db observation and a fresh
    closure-time read returned different numbers. The stored row stays
    authoritative (never overwritten, never inflated), no second row is
    written, the attempt is NOT marked missing (it is observed), and the
    stated reason names the duplicate backfill — not the subtree conflict
    that the old undifferentiated ConflictError handler blamed."""
    zdb = tmp_path / "zcode.sqlite"
    make_zcode_usage_db(
        zdb, str(tmp_path),
        sessions=[("sess-worker", None, None)],
        usages=[("r1", "lr1", 0, "sess-worker", "completed", 150, 8, 140, 158)],
    )
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    try:
        goal = _host_goal(project)
        dispatch.run_slice(project)
        attempt = _parked_host_attempt(project, goal)
        project.store.attempt_update(attempt.id, session_ref="sess-worker")
        # an earlier closure recorded the then-current numbers
        project.store.usage_record_zcode(attempt.id, 100, 5, 90, "exact")
        result = dispatch._usage_autobill(project, attempt.id)
        assert result["status"] == "already_recorded"
        assert result["reason"] == "zcode_duplicate_backfill"
        assert project.store.attempt_get(attempt.id).usage_missing_reason is None
        rows = _usage_rows(project, attempt.id)
        assert len(rows) == 1
        assert (rows[0]["input_tokens"], rows[0]["output_tokens"]) == (100, 5)
    finally:
        project.close()


def test_cli_usage_zcode_unsettled_is_a_stated_domain_error(tmp_path, monkeypatch):
    """The manual re-sync path states unsettled the same way the auto-backfill
    does: exit 1 with the mid-flight row count, nothing recorded, missing
    reason stated on the attempt."""
    from typer.testing import CliRunner
    from orx.cli import app

    runner = CliRunner()
    zdb = tmp_path / "zcode.sqlite"
    make_zcode_usage_db(
        zdb, str(tmp_path),
        sessions=[("sess-worker", None, None)],
        usages=[("r1", "lr1", 0, "sess-worker", "pending", 1000, 50, 980, 1050)],
    )
    monkeypatch.setenv("ORX_ZCODE_DB", str(zdb))
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    try:
        goal = _host_goal(project)
        dispatch.run_slice(project)
        attempt = _parked_host_attempt(project, goal)
        project.store.attempt_update(attempt.id, session_ref="sess-worker")
        project.close()
        r = runner.invoke(app, ["usage", "zcode", "--attempt", str(attempt.id)])
        assert r.exit_code == 1
        assert "mid-flight model_usage request" in r.output
    finally:
        project.close()
