"""Phase B: the host subagent execution contract (docs/zcode-subagent-analysis.md §7).

Five acceptance criteria:
- duplicate dispatch does not execute twice;
- routing changes after dispatch do not move result attribution;
- stale submissions are rejected;
- model mismatch is visible (requested vs reported);
- legacy host/CLI/external behavior keeps working.
"""

from __future__ import annotations

import pytest

from orx import dispatch
from orx.config import ConfigError, load_profiles
from orx.records import ConflictError, NotFoundError
from orx.state import Store

from conftest import ir_for, make_project, task_spec, write_evidence

SUB_PROFILES_TOML = """\
schema_version = 1

[profiles.host-planner]
driver = "host"
harness = "zcode"
model = "account:bigmodel-individual-coding-plan/GLM-5.3"
class = "strong"
effort = "high"
capabilities = ["coding"]

[profiles.sub-worker]
driver = "host"
harness = "zcode"
model = "account:bigmodel-individual-coding-plan/GLM-5.3"
class = "strong"
effort = "max"
host_mode = "subagent"
agent_ref = "orx-worker"
capabilities = ["coding"]

[profiles.sub-verifier-flash]
driver = "host"
harness = "zcode"
model = "account:bigmodel-individual-coding-plan/GLM-5.3-Flash"
class = "economy"
effort = "max"
host_mode = "subagent"
agent_ref = "orx-verifier"
capabilities = ["coding", "vision"]

[profiles.sub-verifier-strong]
driver = "host"
harness = "zcode"
model = "account:bigmodel-individual-coding-plan/GLM-5.3"
class = "strong"
effort = "max"
host_mode = "subagent"
agent_ref = "orx-verifier-strong"
capabilities = ["coding", "vision"]
"""

SUB_CONFIG_TOML = """\
schema_version = 1

[controller]
profile = "host-planner"

[plan]
depth = "auto"

[plan.light]
profiles = ["host-planner"]

[plan.standard]
profiles = ["host-planner"]

[plan.deep]
profiles = ["host-planner"]

[worker]
profiles = ["sub-worker"]

[verify]
profiles = ["sub-verifier-flash", "sub-verifier-strong"]

[runtime]
max_parallel = 1
command_timeout_sec = 30
"""


@pytest.fixture
def sub_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    proj = make_project(tmp_path, SUB_CONFIG_TOML, SUB_PROFILES_TOML)
    yield proj
    proj.close()


@pytest.fixture
def sub_planned(sub_project, tmp_path):
    goal = dispatch.create_goal(
        sub_project,
        objective="Ship the marker",
        acceptance=["marker file exists", "summary reads well"],
        constraints=[],
        context="",
    )[0]
    dispatch.submit_plan(sub_project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance[:1], verification=["test -f t1.marker"]),
        task_spec("T002", acceptance=goal.acceptance[1:], verification=["agent: result reads well"]),
    ]))
    dispatch.run_slice(sub_project)
    return sub_project


# ---------------------------------------------------------------------------
# Configuration: host_mode / agent_ref


def test_profile_host_mode_fields_parse_and_roundtrip(tmp_path):
    path = tmp_path / "profiles.toml"
    path.write_text(SUB_PROFILES_TOML)
    profiles = load_profiles(path)
    worker = profiles["sub-worker"]
    assert worker.host_mode == "subagent"
    assert worker.agent_ref == "orx-worker"
    as_dict = worker.to_dict()
    assert as_dict["host_mode"] == "subagent"
    assert as_dict["agent_ref"] == "orx-worker"
    # A plain host profile defaults to self execution with no agent_ref.
    assert profiles["host-planner"].host_mode == "self"
    assert profiles["host-planner"].agent_ref is None


@pytest.mark.parametrize("snippet, match", [
    (
        'driver = "cli"\nharness = "shell"\nexecutable = "true"\nmodel = "m"\n'
        'class = "strong"\neffort = "high"\nhost_mode = "subagent"\nagent_ref = "w"\n',
        "host_mode requires driver = 'host'",
    ),
    (
        'driver = "host"\nharness = "zcode"\nmodel = "m"\nclass = "strong"\n'
        'effort = "high"\nhost_mode = "subagent"\n',
        "agent_ref is required",
    ),
    (
        'driver = "host"\nharness = "zcode"\nmodel = "m"\nclass = "strong"\n'
        'effort = "high"\nagent_ref = "w"\n',
        "agent_ref requires host_mode = 'subagent'",
    ),
    (
        'driver = "host"\nharness = "zcode"\nmodel = "m"\nclass = "strong"\n'
        'effort = "high"\nhost_mode = "process"\nagent_ref = "w"\n',
        "host_mode.*invalid",
    ),
])
def test_profile_host_mode_invalid_combinations(tmp_path, snippet, match):
    path = tmp_path / "profiles.toml"
    path.write_text(
        "schema_version = 1\n\n[profiles.bad]\n" + snippet
    )
    with pytest.raises(ConfigError, match=match):
        load_profiles(path)


# ---------------------------------------------------------------------------
# Execution spec in dispatch payloads


def test_host_park_carries_full_execution_spec(sub_planned):
    out = dispatch.run_slice(sub_planned)
    entry = out["host_required"][0]
    assert entry["task"] == "T001"
    spec = entry["execution"]
    assert spec["mode"] == "subagent"
    assert spec["agent_ref"] == "orx-worker"
    assert spec["model"] == "account:bigmodel-individual-coding-plan/GLM-5.3"
    assert spec["effort"] == "max"
    assert spec["harness"] == "zcode"
    assert spec["workdir"] == str(sub_planned.root)
    assert isinstance(spec["attempt"], int)
    assert spec["submit"] == "controller"


def test_planner_assignment_carries_execution_spec_self(sub_project):
    goal = dispatch.create_goal(
        sub_project, objective="plan something", acceptance=["a criterion"],
        constraints=[], context="",
    )[0]
    out = dispatch.plan_route(sub_project)
    assert out["mode"] == "host_required"
    spec = out["assignment"]["execution"]
    assert spec["mode"] == "self"
    assert spec["agent_ref"] is None
    assert isinstance(spec["attempt"], int)


def test_task_claim_returns_execution_spec(sub_planned):
    claimed = dispatch.task_claim(sub_planned, "T001")
    spec = claimed["execution"]
    assert spec["mode"] == "subagent"
    assert spec["agent_ref"] == "orx-worker"
    assert spec["attempt"] == claimed["attempt"]


# ---------------------------------------------------------------------------
# Verifier: persistent attempt at dispatch + dedup


def _finish_with_agent_pending(project, tmp_path):
    dispatch.task_claim(project, "T002")
    result = dispatch.task_complete(project, "T002", str(write_evidence(tmp_path)))
    assert result["status"] == "verifying"


def test_verify_dispatch_creates_persistent_verifier_attempt(sub_planned, tmp_path):
    _finish_with_agent_pending(sub_planned, tmp_path)
    out = dispatch.verify_dispatch(sub_planned)
    entries = [e for e in out["agent_required"] if e["task"] == "T002"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["execution"]["mode"] == "subagent"
    assert entry["execution"]["agent_ref"] == "orx-verifier"
    attempt_id = entry["attempt"]
    attempt = sub_planned.store.attempt_get(attempt_id)
    assert attempt.role == "verifier"
    assert attempt.profile == "sub-verifier-flash"
    assert attempt.driver == "host"
    assert attempt.verify_entry == "agent: result reads well"
    assert attempt.ended_at is None
    assert attempt.session_ref is None  # the Controller binds it at submit


def test_verify_dispatch_is_idempotent_one_attempt_per_entry(sub_planned, tmp_path):
    _finish_with_agent_pending(sub_planned, tmp_path)
    first = dispatch.verify_dispatch(sub_planned)
    second = dispatch.verify_dispatch(sub_planned)
    id_one = [e for e in first["agent_required"] if e["task"] == "T002"][0]["attempt"]
    id_two = [e for e in second["agent_required"] if e["task"] == "T002"][0]["attempt"]
    assert id_one == id_two  # duplicate dispatch does not open a second execution

    goal = sub_planned.store.goal_active()
    run = sub_planned.store.run_for_goal(goal.id)
    revision = sub_planned.store.revision_active(run.id)
    verifier_attempts = [
        a for a in sub_planned.store.attempts_all()
        if a.role == "verifier" and a.revision_id == revision.id
    ]
    assert len(verifier_attempts) == 1


def test_submit_binds_dispatch_attempt_despite_routing_change(sub_planned, tmp_path):
    """The verdict closes the attempt that was fixed at dispatch — a routing
    edit between dispatch and submit cannot move the attribution."""
    _finish_with_agent_pending(sub_planned, tmp_path)
    first = dispatch.verify_dispatch(sub_planned)
    attempt_id = [e for e in first["agent_required"] if e["task"] == "T002"][0]["attempt"]

    # Flip the verify order so the strong profile would now win a fresh route.
    sub_planned.close()
    swapped = SUB_CONFIG_TOML.replace(
        'profiles = ["sub-verifier-flash", "sub-verifier-strong"]',
        'profiles = ["sub-verifier-strong", "sub-verifier-flash"]',
    )
    (sub_planned.root / ".orx" / "config.toml").write_text(swapped)
    project = dispatch.open_project()
    try:
        result = dispatch.verify_submit(
            project, "T002", "pass", "agent: result reads well", None,
            attempt_id=attempt_id,
        )
        assert result["status"] == "passed"
        attempt = project.store.attempt_get(attempt_id)
        assert attempt.profile == "sub-verifier-flash"  # dispatch-time identity
        assert attempt.result == "pass"
        assert attempt.ended_at is not None
        rows = project.store.verifications_for(
            project.store.revision_active(
                project.store.run_for_goal(project.store.goal_active().id).id
            ).id,
            "T002",
        )
        assert any(v.attempt_id == attempt_id and v.passed for v in rows)
    finally:
        project.close()


def test_submit_without_attempt_still_prefers_open_dispatch_attempt(sub_planned, tmp_path):
    """Even without --attempt, an open dispatch-time attempt is reused instead
    of routing a new verifier (attribution stays where dispatch put it)."""
    _finish_with_agent_pending(sub_planned, tmp_path)
    first = dispatch.verify_dispatch(sub_planned)
    attempt_id = [e for e in first["agent_required"] if e["task"] == "T002"][0]["attempt"]

    result = dispatch.verify_submit(sub_planned, "T002", "pass", "agent: result reads well", None)
    assert result["status"] == "passed"
    attempt = sub_planned.store.attempt_get(attempt_id)
    assert attempt.result == "pass"
    verifier_attempts = [a for a in sub_planned.store.attempts_all() if a.role == "verifier"]
    assert len(verifier_attempts) == 1  # no second attempt was routed


# ---------------------------------------------------------------------------
# Stale rejection


def test_submit_closed_attempt_is_stale(sub_planned, tmp_path):
    """A verifier attempt closed by a failed first round must not carry the
    verdict after a retry: the retry dispatches a fresh attempt for the same
    entry, and quoting the old one is a stale submission."""
    _finish_with_agent_pending(sub_planned, tmp_path)
    first = dispatch.verify_dispatch(sub_planned)
    attempt_id = [e for e in first["agent_required"] if e["task"] == "T002"][0]["attempt"]
    dispatch.verify_submit(
        sub_planned, "T002", "fail", "agent: result reads well", None,
        attempt_id=attempt_id, reason="not convincing",
    )
    assert next(t for t in dispatch.task_list(sub_planned) if t["id"] == "T002")["status"] == "failed"

    # Retry the task; a fresh round parks, completes, and re-dispatches.
    dispatch.task_retry(sub_planned, "T002")
    dispatch.run_slice(sub_planned)
    dispatch.task_claim(sub_planned, "T002")
    dispatch.task_complete(sub_planned, "T002", str(write_evidence(tmp_path, "e2.json")))
    second = dispatch.verify_dispatch(sub_planned)
    fresh = [e for e in second["agent_required"] if e["task"] == "T002"][0]["attempt"]
    assert fresh != attempt_id

    with pytest.raises(ConflictError, match="stale"):
        dispatch.verify_submit(
            sub_planned, "T002", "pass", "agent: result reads well", None,
            attempt_id=attempt_id,
        )


def test_submit_attempt_from_wrong_entry_rejected(sub_planned, tmp_path):
    # Two agent entries on one task: an attempt bound to entry A must not
    # carry the verdict for entry B.
    goal = sub_planned.store.goal_active()
    dispatch.submit_plan(sub_planned, ir_for(goal, [
        task_spec("T003", acceptance=goal.acceptance,
                  verification=["agent: alpha reads well", "agent: beta reads well"]),
    ]))
    dispatch.run_slice(sub_planned)
    dispatch.task_claim(sub_planned, "T003")
    dispatch.task_complete(sub_planned, "T003", str(write_evidence(tmp_path, "e3.json")))
    out = dispatch.verify_dispatch(sub_planned)
    entries = sorted(
        (e for e in out["agent_required"] if e["task"] == "T003"),
        key=lambda e: e["entry"],
    )
    alpha, beta = entries
    with pytest.raises(ConflictError, match="different verification entry"):
        dispatch.verify_submit(
            sub_planned, "T003", "pass", beta["entry"], None, attempt_id=alpha["attempt"],
        )


def test_task_complete_stale_attempt_rejected_after_retry(sub_planned, tmp_path):
    out = dispatch.run_slice(sub_planned)
    entry = out["host_required"][0]
    old_attempt = entry["execution"]["attempt"]
    dispatch.task_claim(sub_planned, "T001")

    # The first round fails; retry routes a NEW attempt for the same task.
    dispatch.task_fail(sub_planned, "T001", "marker not written")
    dispatch.task_retry(sub_planned, "T001")
    rerun = dispatch.run_slice(sub_planned)
    new_attempt = rerun["host_required"][0]["execution"]["attempt"]
    assert new_attempt != old_attempt

    # The new round is claimed (running). The late completion from the old
    # attempt must not close the new one.
    dispatch.task_claim(sub_planned, "T001")
    with pytest.raises(ConflictError, match="stale"):
        dispatch.task_complete(
            sub_planned, "T001", str(write_evidence(tmp_path, "late.json")),
            attempt_id=old_attempt,
        )


def test_task_complete_with_bound_attempt_ok(sub_planned, tmp_path):
    out = dispatch.run_slice(sub_planned)
    attempt_id = out["host_required"][0]["execution"]["attempt"]
    dispatch.task_claim(sub_planned, "T001")
    (sub_planned.root / "t1.marker").write_text("ok")
    result = dispatch.task_complete(
        sub_planned, "T001", str(write_evidence(tmp_path)),
        attempt_id=attempt_id,
    )
    assert result["status"] == "passed"
    attempt = sub_planned.store.attempt_get(attempt_id)
    assert attempt.result == "completed"


# ---------------------------------------------------------------------------
# Actual model visibility


def test_submit_actual_model_match_no_mismatch(sub_planned, tmp_path):
    _finish_with_agent_pending(sub_planned, tmp_path)
    first = dispatch.verify_dispatch(sub_planned)
    attempt_id = [e for e in first["agent_required"] if e["task"] == "T002"][0]["attempt"]
    result = dispatch.verify_submit(
        sub_planned, "T002", "pass", "agent: result reads well", None,
        attempt_id=attempt_id, actual_model="GLM-5.3-Flash",
    )
    assert "model_mismatch" not in result
    attempt = sub_planned.store.attempt_get(attempt_id)
    assert attempt.actual_model == "GLM-5.3-Flash"
    assert attempt.model_source == "reported"


def test_submit_actual_model_mismatch_visible(sub_planned, tmp_path):
    """The strong model ran where Flash was requested: the mismatch is
    recorded on the attempt and surfaced in the submit result."""
    _finish_with_agent_pending(sub_planned, tmp_path)
    first = dispatch.verify_dispatch(sub_planned)
    attempt_id = [e for e in first["agent_required"] if e["task"] == "T002"][0]["attempt"]
    result = dispatch.verify_submit(
        sub_planned, "T002", "pass", "agent: result reads well", None,
        attempt_id=attempt_id, actual_model="GLM-5.3",
    )
    assert result["status"] == "passed"  # the verdict still counts
    mismatch = result["model_mismatch"]
    assert mismatch["requested"] == "account:bigmodel-individual-coding-plan/GLM-5.3-Flash"
    assert mismatch["reported"] == "GLM-5.3"
    attempt = sub_planned.store.attempt_get(attempt_id)
    assert attempt.actual_model == "GLM-5.3"
    assert attempt.model_source == "reported"


def test_task_complete_actual_model_mismatch_visible(sub_planned, tmp_path):
    out = dispatch.run_slice(sub_planned)
    attempt_id = out["host_required"][0]["execution"]["attempt"]
    dispatch.task_claim(sub_planned, "T001")
    # The delivery must be green for the reported model to be recorded: the
    # gate refuses a red completion outright.
    (sub_planned.root / "t1.marker").write_text("ok")
    result = dispatch.task_complete(
        sub_planned, "T001", str(write_evidence(tmp_path)),
        attempt_id=attempt_id, actual_model="GLM-5.3-Flash",
    )
    assert result["model_mismatch"]["reported"] == "GLM-5.3-Flash"
    attempt = sub_planned.store.attempt_get(attempt_id)
    assert attempt.actual_model == "GLM-5.3-Flash"
    assert attempt.model_source == "reported"


# ---------------------------------------------------------------------------
# Back-compat: legacy flows unchanged


def test_legacy_self_host_flow_still_works(project, tmp_path):
    """Plain self-mode host profiles (no host_mode keys) run the whole
    park -> claim -> complete -> submit loop exactly as before."""
    goal = dispatch.create_goal(
        project, objective="legacy flow", acceptance=["a criterion"],
        constraints=[], context="",
    )[0]
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=[]),
    ]))
    out = dispatch.run_slice(project)
    entry = out["host_required"][0]
    assert entry["execution"]["mode"] == "self"
    assert entry["execution"]["agent_ref"] is None
    claimed = dispatch.task_claim(project, entry["task"])
    assert claimed["execution"]["mode"] == "self"
    dispatch.task_complete(project, entry["task"], str(write_evidence(tmp_path)))


def test_legacy_submit_without_dispatch_still_routes(sub_planned, tmp_path):
    """A verdict submitted with no prior dispatch (no open attempt) falls back
    to routing a verifier at submit time — the M1 behavior."""
    _finish_with_agent_pending(sub_planned, tmp_path)
    result = dispatch.verify_submit(sub_planned, "T002", "pass", "agent: result reads well", None)
    assert result["status"] == "passed"
    verifiers = [a for a in sub_planned.store.attempts_all() if a.role == "verifier"]
    assert len(verifiers) == 1
    assert verifiers[0].profile == "sub-verifier-flash"


def test_submit_unknown_attempt_rejected(sub_planned, tmp_path):
    _finish_with_agent_pending(sub_planned, tmp_path)
    with pytest.raises(NotFoundError):
        dispatch.verify_submit(
            sub_planned, "T002", "pass", "agent: result reads well", None, attempt_id=999999,
        )


# ---------------------------------------------------------------------------
# Schema v8


def test_v7_to_v8_migration_adds_attempt_columns(tmp_path):
    """A v7 database (attempts without the subagent contract columns) upgrades
    on reopen through v8 to v9; existing rows survive with NULLs (never
    invented) and the v9 replan tables come along additively."""
    import sqlite3

    db = tmp_path / "v7.db"
    store = Store.open(db)  # code is v9; build a v7 db by hand
    store.conn.execute("ALTER TABLE attempts DROP COLUMN verify_entry")
    store.conn.execute("ALTER TABLE attempts DROP COLUMN actual_model")
    store.conn.execute("ALTER TABLE attempts DROP COLUMN model_source")
    store.conn.execute(
        "INSERT INTO attempts(role, profile, driver, harness, model, requested_effort)"
        " VALUES('worker', 'legacy', 'host', 'zcode', 'm', 'high')"
    )
    store.conn.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
    store.close()

    reopened = Store.open(db)
    try:
        assert reopened.schema_version() == 9
        columns = {r["name"] for r in reopened.conn.execute("PRAGMA table_info(attempts)")}
        assert {"verify_entry", "actual_model", "model_source"} <= columns
        legacy = reopened.attempts_all()[0]
        assert legacy.verify_entry is None
        assert legacy.actual_model is None
        assert legacy.model_source is None
        # v9 replan tables arrive empty on the upgraded file (G004).
        tables = {
            r["name"]
            for r in reopened.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "replan_mappings" in tables and "replan_artifact_sources" in tables
        assert reopened.replan_mappings_for_run("R001") == []
    finally:
        reopened.close()
    check = sqlite3.connect(db)
    try:
        assert check.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "9"
    finally:
        check.close()
    assert not list(tmp_path.glob("v7.db.migrate-*")), "stale migration backups"
