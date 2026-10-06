"""Phase C: reliability and delivery (docs/zcode-subagent-analysis.md §7).

Acceptance criteria:
- init respects an installed user preset (does not shadow it with project defaults);
- existing project overrides are preserved (preset writes only the user layer);
- role definitions (ZCode agent files) vs profiles consistency is checkable (doctor);
- recovery never creates a second writer for a running host task.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orx import dispatch
from orx.config import ConfigError, load_effective
from orx.presets import install_preset, list_presets, preset_dir, source_agents_dir
from orx.records import ConflictError

from conftest import (
    HOST_CONFIG_TOML,
    active_task,
    ir_for,
    make_project,
    task_spec,
    write_evidence,
)


@pytest.fixture
def user_layer(tmp_path):
    """An isolated user layer directory wired via ORX_CONFIG_DIR."""
    import os
    directory = tmp_path / "userlayer"
    directory.mkdir()
    os.environ["ORX_CONFIG_DIR"] = str(directory)
    yield directory
    del os.environ["ORX_CONFIG_DIR"]


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    directory = tmp_path / "zcode-agents"
    directory.mkdir()
    monkeypatch.setenv("ORX_ZCODE_AGENTS_DIR", str(directory))
    return directory


# ---------------------------------------------------------------------------
# Packaged preset


def test_zcode_preset_packaged_files_are_valid():
    directory = preset_dir("zcode")
    assert (directory / "config.toml").exists()
    assert (directory / "profiles.toml").exists()
    from orx.config import load_config, load_profiles
    config = load_config(directory / "config.toml")
    profiles = load_profiles(directory / "profiles.toml")
    assert set(profiles) == {
        "zcode-controller", "zcode-worker",
        "zcode-verifier-flash", "zcode-verifier-strong",
    }
    worker = profiles["zcode-worker"]
    assert worker.host_mode == "subagent"
    assert worker.agent_ref == "orx-worker"
    # Every routing reference resolves inside the preset itself.
    assert config.controller_profile in profiles
    assert set(config.worker_profiles + config.verify_profiles) <= set(profiles)
    # The agent definitions the preset's agent_refs point at ship with ORX.
    source = source_agents_dir()
    for ref in ("orx-worker", "orx-verifier", "orx-verifier-strong"):
        assert (source / f"{ref}.md").exists(), ref


def test_list_presets_includes_zcode():
    presets = list_presets()
    assert any(p["name"] == "zcode" for p in presets)


# ---------------------------------------------------------------------------
# Preset install: user layer only, no silent overwrites


def test_install_into_empty_user_layer_writes_preset(user_layer):
    report = install_preset("zcode")
    assert report["profiles_added"] == [
        "zcode-controller", "zcode-verifier-flash",
        "zcode-verifier-strong", "zcode-worker",
    ] or set(report["profiles_added"]) == {
        "zcode-controller", "zcode-verifier-flash",
        "zcode-verifier-strong", "zcode-worker",
    }
    assert (user_layer / "profiles.toml").exists()
    assert (user_layer / "config.toml").exists()
    # The preset is effective for a project with no layers of its own.
    effective = load_effective(user_layer / "empty.toml", user_layer / "empty.toml")
    assert effective.config.controller_profile == "zcode-controller"
    assert effective.config.worker_profiles == ["zcode-worker"]
    assert effective.config.verify_profiles == ["zcode-verifier-flash", "zcode-verifier-strong"]


def test_install_refuses_conflicting_profile_and_touches_nothing(user_layer):
    (user_layer / "profiles.toml").write_text(
        "schema_version = 1\n\n"
        '[profiles.zcode-worker]\ndriver = "host"\nharness = "zcode"\n'
        'model = "other/model"\nclass = "strong"\neffort = "low"\n'
        'host_mode = "subagent"\nagent_ref = "someone-else"\ncapabilities = ["coding"]\n'
    )
    before = (user_layer / "profiles.toml").read_text()
    with pytest.raises(ConfigError, match="zcode-worker"):
        install_preset("zcode")
    assert (user_layer / "profiles.toml").read_text() == before


def test_install_preserves_existing_user_config_keys(user_layer):
    # The user already routes the controller to their own profile…
    (user_layer / "profiles.toml").write_text(
        "schema_version = 1\n\n"
        '[profiles.my-controller]\ndriver = "host"\nharness = "zcode"\n'
        'model = "m"\nclass = "strong"\neffort = "high"\ncapabilities = ["coding"]\n'
    )
    (user_layer / "config.toml").write_text(
        "schema_version = 1\n\n[controller]\nprofile = \"my-controller\"\n"
    )
    report = install_preset("zcode")
    assert "controller.profile" in report["config_preserved"]
    assert "worker.profiles" in report["config_added"]
    effective = load_effective(user_layer / "empty.toml", user_layer / "empty.toml")
    assert effective.config.controller_profile == "my-controller"  # user wins
    assert effective.config.worker_profiles == ["zcode-worker"]  # preset fills gaps


def test_install_never_touches_project_layers(user_layer, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    install_preset("zcode")
    project = make_project(tmp_path)  # project writes its own layers after init
    effective = load_effective(project.config_path, project.profiles_path)
    assert effective.config.controller_profile == "host-planner"  # project wins
    project.close()


def test_install_is_idempotent(user_layer):
    install_preset("zcode")
    second = install_preset("zcode")
    assert second["profiles_added"] == []
    assert not second.get("conflicts")


def test_install_copies_missing_agent_definitions(user_layer, agents_dir):
    report = install_preset("zcode")
    installed = {a["name"]: a for a in report["agents"]}
    assert installed["orx-worker"]["installed"] is True
    assert (agents_dir / "orx-worker.md").exists()
    assert (agents_dir / "orx-verifier.md").exists()
    assert (agents_dir / "orx-verifier-strong.md").exists()


def test_install_preserves_existing_agent_definitions(user_layer, agents_dir):
    (agents_dir / "orx-worker.md").write_text("custom live definition\n")
    report = install_preset("zcode")
    installed = {a["name"]: a for a in report["agents"]}
    assert installed["orx-worker"]["installed"] is False
    assert installed["orx-worker"]["preserved_existing"] is True
    assert (agents_dir / "orx-worker.md").read_text() == "custom live definition\n"
    assert installed["orx-verifier"]["installed"] is True


# ---------------------------------------------------------------------------
# init respects an installed user preset


def test_init_inherits_when_user_preset_installed(user_layer, tmp_path, monkeypatch):
    install_preset("zcode")
    monkeypatch.chdir(tmp_path)
    report = dispatch.init_project(tmp_path)
    orx = tmp_path / ".orx"
    # No project config/profiles shadowing the preset…
    assert not (orx / "config.toml").exists() or (orx / "config.toml").read_text().strip() == ""
    assert not (orx / "profiles.toml").exists() or (orx / "profiles.toml").read_text().strip() == ""
    assert ".orx/state.db" in report["created"]
    # …and the effective project IS the preset.
    project = dispatch.open_project()
    try:
        assert project.config.controller_profile == "zcode-controller"
        assert project.config.worker_profiles == ["zcode-worker"]
        seeded = {row["profile"] for row in dispatch.resource_list(project)}
        assert "zcode-worker" in seeded
    finally:
        project.close()


def test_init_partial_user_layer_still_writes_defaults(tmp_path, monkeypatch, user_layer):
    """A user layer with only profiles.toml is NOT a preset: the old behavior
    (project defaults) must not change under it."""
    (user_layer / "profiles.toml").write_text(
        "schema_version = 1\n\n[profiles.extra]\ndriver = \"host\"\nharness = \"zcode\"\n"
        "model = \"x\"\nclass = \"strong\"\neffort = \"high\"\ncapabilities = [\"coding\"]\n"
    )
    monkeypatch.chdir(tmp_path)
    dispatch.init_project(tmp_path)
    assert "orx-host" in (tmp_path / ".orx" / "profiles.toml").read_text()


def test_existing_project_override_survives_preset_install(user_layer, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    project.close()
    install_preset("zcode")
    effective = load_effective(tmp_path / ".orx" / "config.toml",
                               tmp_path / ".orx" / "profiles.toml")
    assert effective.config.controller_profile == "host-planner"
    assert "host-worker" in effective.config.worker_profiles


# ---------------------------------------------------------------------------
# Doctor: role definitions vs profiles consistency


def _agent_definition(model: str, thought: str = "max") -> str:
    return (
        "---\n"
        f"description: probe agent\ncustom: true\nmodel: {model}\n"
        f"thoughtLevel: {thought}\ntools: [Read, Bash]\n"
        "---\n\nprobe\n"
    )


def test_doctor_flags_subagent_model_mismatch(tmp_path, monkeypatch, user_layer, agents_dir):
    from orx.doctor import run_doctor
    monkeypatch.chdir(tmp_path)
    install_preset("zcode")
    (agents_dir / "orx-worker.md").write_text(_agent_definition("account:x/GLM-4.5"))
    dispatch.init_project(tmp_path)
    result = run_doctor(tmp_path)
    checks = {c["name"]: c for c in result["checks"]}
    worker = checks["agent_def:orx-worker"]
    assert worker["state"] == "fail"
    assert "GLM-5.3" in worker["detail"] and "GLM-4.5" in worker["detail"]
    assert not result["ok"]


def test_doctor_warns_on_missing_agent_definition(tmp_path, monkeypatch, user_layer, agents_dir):
    from orx.doctor import run_doctor
    monkeypatch.chdir(tmp_path)
    install_preset("zcode")
    for existing in agents_dir.glob("*.md"):
        existing.unlink()  # simulate definitions never installed
    dispatch.init_project(tmp_path)  # inherit mode: no project files
    result = run_doctor(tmp_path)
    checks = {c["name"]: c for c in result["checks"]}
    # Inherited project: the config check must not fail on the missing file.
    assert checks["config"]["state"] == "ok"
    assert "inherited" in checks["config"]["detail"]
    assert checks["profiles"]["state"] == "ok"
    worker = checks.get("agent_def:orx-worker")
    assert worker is not None and worker["state"] == "warn"
    assert "not found" in worker["detail"]


def test_doctor_passes_consistent_definitions(tmp_path, monkeypatch, user_layer, agents_dir):
    from orx.doctor import run_doctor
    monkeypatch.chdir(tmp_path)
    install_preset("zcode")
    (agents_dir / "orx-worker.md").write_text(
        _agent_definition("account:bigmodel-individual-coding-plan/GLM-5.3")
    )
    (agents_dir / "orx-verifier.md").write_text(
        _agent_definition("account:bigmodel-individual-coding-plan/GLM-5.3-Flash")
    )
    (agents_dir / "orx-verifier-strong.md").write_text(
        _agent_definition("account:bigmodel-individual-coding-plan/GLM-5.3")
    )
    dispatch.init_project(tmp_path)
    result = run_doctor(tmp_path)
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["agent_def:orx-worker"]["state"] == "ok"
    assert checks["agent_def:orx-verifier"]["state"] == "ok"
    assert checks["agent_def:orx-verifier-strong"]["state"] == "ok"
    assert result["ok"]


# ---------------------------------------------------------------------------
# Recovery: never a second writer


@pytest.fixture
def running_host_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    goal = dispatch.create_goal(
        project, objective="recovery probe", acceptance=["a criterion"],
        constraints=[], context="",
    )[0]
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=[]),
    ]))
    dispatch.run_slice(project)
    claimed = dispatch.task_claim(project, "T001")
    yield project, claimed
    project.close()


def test_running_host_task_surfaces_as_recovery_not_redispatch(running_host_task):
    project, claimed = running_host_task
    out = dispatch.run_slice(project)
    # The running task is reported for recovery with its identity…
    assert [e["task"] for e in out["recovery"]] == ["T001"]
    entry = out["recovery"][0]
    assert entry["attempt"] == claimed["attempt"]
    assert entry["execution"]["attempt"] == claimed["attempt"]
    assert "task complete" in entry["contract"] and "task fail" in entry["contract"]
    # …never re-parked, never a second attempt, still running.
    assert all(e["task"] != "T001" for e in out["host_required"])
    goal = project.store.goal_active()
    run = project.store.run_for_goal(goal.id)
    revision = project.store.revision_active(run.id)
    attempts = [a for a in project.store.attempts_all() if a.task_id == "T001"]
    assert len(attempts) == 1
    assert project.store.task_get(revision.id, "T001").status == "running"


def test_late_completion_binds_original_attempt_after_recovery(running_host_task, tmp_path):
    project, claimed = running_host_task
    dispatch.run_slice(project)  # recovery pass in between changes nothing
    result = dispatch.task_complete(
        project, "T001", str(write_evidence(tmp_path)),
        attempt_id=claimed["attempt"],
    )
    assert result["status"] == "passed"


def test_second_writer_only_after_explicit_fail_and_retry(running_host_task, tmp_path):
    project, claimed = running_host_task
    dispatch.task_fail(project, "T001", "subagent died")
    dispatch.task_retry(project, "T001")
    out = dispatch.run_slice(project)
    parked = [e for e in out["host_required"] if e["task"] == "T001"]
    assert len(parked) == 1
    assert parked[0]["execution"]["attempt"] != claimed["attempt"]
    # The new round is claimed; the old attempt's late completion is stale.
    dispatch.task_claim(project, "T001")
    with pytest.raises(ConflictError, match="stale"):
        dispatch.task_complete(
            project, "T001", str(write_evidence(tmp_path, "late.json")),
            attempt_id=claimed["attempt"],
        )


# ---------------------------------------------------------------------------
# G006 T004: current-window progress on the observation faces
# (docs/host-progress-contract.md §7-§8)
#
# status / task list / run recovery show the CURRENT attempt's latest
# report with its received time and age, `unknown` when there is none, and
# an overdue CHECK HINT once age >= the configured threshold (closed
# boundary; exactly equal IS overdue). The hint only suggests checking the
# original session: no fail, no retry, no second worker, and the attempt's
# legal late delivery stays acceptable. Clock anomalies never read as
# overdue. All clocks below are the injectable ORX seam — no real sleep.


PROGRESS_TIMEOUT_ONE_MIN = HOST_CONFIG_TOML.replace(
    '[worker]\nprofiles = ["host-worker", "host-external"]\n',
    '[worker]\nprofiles = ["host-worker", "host-external"]\n'
    "progress_timeout_min = 1\n",
)


def _freeze_progress_clock(monkeypatch):
    """Freeze both ORX clock seams (write and read side) at one instant."""
    holder = {"now": datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)}

    def now():
        return holder["now"].isoformat(timespec="microseconds")

    monkeypatch.setattr("orx.state.now", now)
    monkeypatch.setattr("orx.dispatch.db_now", now)
    return holder


def _progress_window(project, task_id="T001"):
    """The current-window progress block the observation faces publish."""
    row = next(t for t in dispatch.task_list(project) if t["id"] == task_id)
    return row["progress"]


@pytest.fixture
def progress_project(tmp_path, monkeypatch):
    """A claimed host task (session-bound) in a project with a 1-minute
    report timeout, under a frozen ORX clock. Yields (project, claimed,
    clock holder) — advance `clock["now"]` instead of sleeping."""
    monkeypatch.chdir(tmp_path)
    clock = _freeze_progress_clock(monkeypatch)
    project = make_project(tmp_path, config_toml=PROGRESS_TIMEOUT_ONE_MIN)
    goal = dispatch.create_goal(
        project, objective="progress probe", acceptance=["a criterion"],
        constraints=[], context="",
    )[0]
    dispatch.submit_plan(project, ir_for(goal, [
        task_spec("T001", acceptance=goal.acceptance, verification=[]),
    ]))
    dispatch.run_slice(project)
    claimed = dispatch.task_claim(project, "T001", session="sess-worker-1")
    yield project, claimed, clock
    project.close()


def test_recovery_face_shows_unknown_before_any_report(running_host_task):
    project, claimed = running_host_task
    out = dispatch.run_slice(project)
    progress = out["recovery"][0]["progress"]
    assert progress == {
        "attempt": claimed["attempt"],
        "state": "unknown",
        "phase": None,
        "message": None,
        "received_at": None,
        "age_sec": None,
        "timeout_sec": 3600,  # builtin default 60 minutes, compared in seconds
        "hint": None,
        "note": None,
    }
    # The same block rides `orx status` (which reuses task list).
    assert dispatch.status_data(project)["tasks"][0]["progress"] == progress
    assert _progress_window(project) == progress


def test_progress_boundaries_under_equal_and_past_default_threshold(
        running_host_task, monkeypatch):
    project, claimed = running_host_task
    clock = _freeze_progress_clock(monkeypatch)
    attempt = claimed["attempt"]
    dispatch.task_heartbeat(project, "T001", attempt, "checking")
    base = clock["now"]

    clock["now"] = base + timedelta(seconds=3599)  # one second under
    under = dispatch.run_slice(project)["recovery"][0]["progress"]
    assert under["state"] == "reported"
    assert under["phase"] == "checking"
    assert under["received_at"] == base.isoformat(timespec="microseconds")
    assert under["age_sec"] == 3599
    assert under["timeout_sec"] == 3600
    assert under["hint"] is None and under["note"] is None

    clock["now"] = base + timedelta(seconds=3600)  # exactly the threshold
    at = dispatch.run_slice(project)["recovery"][0]["progress"]
    assert at["state"] == "overdue"  # closed boundary: == means overdue
    assert at["age_sec"] == 3600
    assert ">= 60m threshold" in at["hint"]
    assert "check the original worker session" in at["hint"]

    clock["now"] = base + timedelta(seconds=3601)  # past it
    past = dispatch.run_slice(project)["recovery"][0]["progress"]
    assert past["state"] == "overdue" and past["age_sec"] == 3601


def test_configured_threshold_and_session_handle_ride_the_hint(progress_project):
    project, claimed, clock = progress_project
    dispatch.task_heartbeat(
        project, "T001", claimed["attempt"], "implementing", "round 1/3 red"
    )
    base = clock["now"]

    clock["now"] = base + timedelta(seconds=30)
    fresh = _progress_window(project)
    assert fresh["timeout_sec"] == 60  # configured 1 minute, in seconds
    assert fresh["state"] == "reported" and fresh["age_sec"] == 30

    clock["now"] = base + timedelta(seconds=90)
    stale = _progress_window(project)
    assert stale["state"] == "overdue"
    assert ">= 1m threshold" in stale["hint"]
    assert "handle: sess-worker-1" in stale["hint"]
    assert "before any fail/retry" in stale["hint"]


def test_overdue_is_a_hint_only_and_late_delivery_still_accepted(
        progress_project, tmp_path):
    project, claimed, clock = progress_project
    attempt = claimed["attempt"]
    dispatch.task_heartbeat(project, "T001", attempt, "checking")
    clock["now"] = clock["now"] + timedelta(seconds=4000)  # far past 1 minute

    attempts_before = len(project.store.attempts_all())
    events_before = len(project.store.task_events_all())
    progress = dispatch.run_slice(project)["recovery"][0]["progress"]
    assert progress["state"] == "overdue"
    # The hint changed nothing observable: still running, no new attempt,
    # no extra task event, no fail/retry/replan, no second worker.
    assert active_task(project, "T001").status == "running"
    assert len(project.store.attempts_all()) == attempts_before
    assert len(project.store.task_events_all()) == events_before
    # And the overdue attempt's legal late delivery is still accepted.
    result = dispatch.task_complete(
        project, "T001", str(write_evidence(tmp_path, "late.json")),
        attempt_id=attempt,
    )
    assert result["status"] == "passed"


def test_retry_resets_the_current_window_to_unknown(progress_project):
    project, claimed, clock = progress_project
    old_attempt = claimed["attempt"]
    dispatch.task_heartbeat(
        project, "T001", old_attempt, "implementing", "still working"
    )
    assert _progress_window(project)["state"] == "reported"

    dispatch.task_fail(project, "T001", "worker lost")
    dispatch.task_retry(project, "T001")
    dispatch.run_slice(project)
    dispatch.task_claim(project, "T001")

    progress = _progress_window(project)
    assert progress["attempt"] != old_attempt  # a fresh attempt owns the window
    assert progress["state"] == "unknown"      # and it starts unknown…
    assert progress["phase"] is None
    assert progress["received_at"] is None
    assert progress["hint"] is None
    # …the old attempt's report never fills it, and the closed attempt can
    # no longer report into this task at all.
    with pytest.raises(ConflictError):
        dispatch.task_heartbeat(project, "T001", old_attempt, "checking")
    assert _progress_window(project)["state"] == "unknown"


def test_progress_clock_anomalies_never_read_as_overdue(progress_project):
    project, claimed, clock = progress_project
    attempt = claimed["attempt"]
    dispatch.task_heartbeat(project, "T001", attempt, "checking")
    base = clock["now"]

    # Read clock earlier than received_at (rollback): the report is real,
    # the age is not honestly computable — reported + note, never overdue.
    clock["now"] = base - timedelta(seconds=30)
    rolled = _progress_window(project)
    assert rolled["state"] == "reported"
    assert rolled["age_sec"] is None
    assert rolled["note"] == "clock anomaly: received_at after read clock"
    assert rolled["hint"] is None

    # An unparseable stored timestamp is the same honesty rule: no fake
    # age (0 or negative), no overdue verdict from broken data.
    project.store.conn.execute(
        "UPDATE attempt_progress SET received_at = 'not-a-timestamp'"
    )
    project.store.conn.commit()
    clock["now"] = base + timedelta(seconds=999999)
    broken = _progress_window(project)
    assert broken["state"] == "reported"
    assert broken["age_sec"] is None
    assert broken["note"] == "clock anomaly: received_at missing or unparseable"
    assert broken["hint"] is None
