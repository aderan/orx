"""G011 T004: the joint frozen-build + acceptance driver, judged at the
behavior level.

What these tests pin (mirroring the Goal acceptance verbatim):

- the driver provides the self-test / freeze / build / exercise / verify
  / verify-remote stages, and the manifest binds BOTH repos' full source
  commits, versions, the channel, the dependency runtime, the final
  artifact SHA256s and the per-stage log references;
- one-shot verification/build checkouts come from deterministic git
  commits (``git archive``): uncommitted workspace files NEVER enter a
  build, and versions are read from the commit, not the worktree. The
  real historical tags stay where the existing package-acceptance chain
  needs them;
- the old ORX sample is the existing traceable release-0.3.0 fixture and
  the analytics store-v2 sample is produced by a REAL old analytics
  commit (proven by regenerating with that code and comparing normalized
  dumps — never by editing a current store's version number). The old
  code also refuses a newer ORX schema (source rollback import), and the
  current code upgrades the v2 sample to v3 additively;
- the rejection paths are behavioral: bad/wrong artifacts, commit
  mismatch, hash drift, missing step evidence, and missing App / GUI /
  signature evidence all FAIL the stages with stable machine codes — a
  skipped stage can never pass, and verify-remote fails closed without a
  published channel root;
- the engine-connection machinery (the app's own serve entry + socket
  protocol + read-only, session-isolated collection) is exercised for
  real against a stand-in delivery, without claiming product acceptance;
- automation isolates real sessions (scratch HOME/ORX_* layer,
  notifications off) and never launches the GUI.

The full product run (build with the final App artifacts, exercise
against the real .app bundle, operator GUI/signature evidence) is G011
T006/T007's job; until those artifacts exist the stages fail closed and
these tests pin exactly that. The run book is
docs/diagnostics/g011/joint-acceptance-runbook.md.

G012 (analytics-only 0.4.3 on the published ORX v0.4.0 pin) adds a
focused group at the bottom: the --goal-id g012 profiles, the ORX
product-input pin (and its moved-tag refusal at freeze/candidate time),
the G012-labeled candidate document, the goal-scoped generated-directory
exemption in the uncommitted-inputs guard (everything else — and the
whole G011 guard — unchanged), and the exact prescribed dist/g012
invocation shapes (fail-closed before a freeze exists). The G012 run
book is docs/diagnostics/g012/joint-acceptance-runbook.md.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

MODULE_PATH = REPO_ROOT / "scripts" / "joint_release.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


joint_release = _load("joint_release", MODULE_PATH)
check_release = _load("check_release", REPO_ROOT / "scripts" / "check_release.py")


@pytest.fixture(scope="module")
def frozen(tmp_path_factory):
    """One real freeze (both repos' HEADs, sample verification with
    regeneration) shared by the module."""
    work = tmp_path_factory.mktemp("jr-frozen")
    log = joint_release.StageLog(work, "freeze")
    manifest = joint_release.stage_freeze(work, "internal-preview", log)
    return work, manifest


@pytest.fixture(scope="module")
def scratch_git_repo(tmp_path_factory):
    """A scratch git repo with one committed version file, a modified
    worktree copy, and an untracked file — the build-exclusion probe."""
    repo = tmp_path_factory.mktemp("jr-scratch-repo")
    (repo / "src").mkdir()
    (repo / "src" / "__init__.py").write_text('__version__ = "1.0.0"\n')
    joint_release._git(repo, "init", "-q")
    joint_release._git(repo, "add", "-A")
    joint_release._git(
        repo, "-c", "user.name=joint", "-c", "user.email=joint@example.com",
        "commit", "-q", "-m", "v1", "--no-gpg-sign",
    )
    # uncommitted worktree changes + an untracked file
    (repo / "src" / "__init__.py").write_text('__version__ = "9.9.9"\n')
    (repo / "UNCOMMITTED-REPORT.md").write_text("never enters a build\n")
    return repo


# ---------------------------------------------------------------------------
# The driver's stage surface and the manifest binding
# ---------------------------------------------------------------------------


def test_driver_provides_all_stages():
    assert joint_release.STAGES == (
        "self-test", "freeze", "battery", "build", "candidate",
        "exercise", "verify", "verify-remote",
    )
    # every stage is a real CLI entry (argparse-level)
    for stage in joint_release.STAGES:
        proc = subprocess.run(
            ["uv", "run", "python", str(MODULE_PATH), stage, "--help"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, (stage, proc.stderr)
    # an unknown stage is a usage error (exit 2), never a silent pass
    bad = subprocess.run(
        ["uv", "run", "python", str(MODULE_PATH), "not-a-stage"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert bad.returncode == 2


def test_freeze_binds_both_repos_versions_channel_runtime_logs(frozen):
    work, manifest = frozen
    binding = manifest["binding"]
    assert binding["channel"] == "internal-preview"
    # both FULL source commits, matching the repos' actual HEADs
    assert binding["orx"]["commit"] == \
        joint_release.head_commit(joint_release.REPO_ROOT)
    assert binding["analytics"]["commit"] == \
        joint_release.head_commit(joint_release.ANALYTICS_REPO)
    # versions come FROM THE COMMITS (never the worktree)
    assert binding["orx"]["version"] == joint_release.orx_version_at(
        joint_release.REPO_ROOT, binding["orx"]["commit"])
    assert binding["analytics"]["version"] == \
        joint_release.analytics_version_at(
            joint_release.ANALYTICS_REPO, binding["analytics"]["commit"])
    # the dependency/runtime binding
    assert binding["runtime"]["harness_python"]
    assert binding["runtime"]["uv"].startswith("uv ")
    assert binding["runtime"]["harness_sqlite"]
    assert binding["analytics"]["app_pinned_runtime"]["pbs_sha256"]
    # samples are bound with digests that match the committed files
    samples = binding["samples"]
    assert samples["orx_old"]["sha256"] == joint_release.sha256_file(
        REPO_ROOT / joint_release.V030_FIXTURE_DIR / "state.db")
    assert samples["analytics_store_v2"]["sha256"] == \
        joint_release.sha256_file(
            REPO_ROOT / joint_release.STORE_V2_SAMPLE_DIR / "analytics.db")
    assert samples["orx_old"]["tag"] == joint_release.V030_TAG
    # the freeze stage recorded itself with a log that exists
    record = manifest["stages"]["freeze"]
    assert record["status"] == "passed"
    assert (work / record["log"]).is_file()
    assert (work / "EVIDENCE-INDEX.md").is_file()


def test_freeze_binding_is_deterministic_across_runs(frozen, tmp_path):
    _, first = frozen
    work = tmp_path / "freeze-again"
    log = joint_release.StageLog(work, "freeze")
    second = joint_release.stage_freeze(work, "internal-preview", log)
    binding_fields = ("channel", "orx", "analytics", "samples", "runtime")
    for field in binding_fields:
        assert first["binding"][field] == second["binding"][field], field


def test_freeze_keeps_historical_tags_for_package_acceptance(frozen):
    _, manifest = frozen
    tags = joint_release._git(joint_release.REPO_ROOT, "tag", "--list")
    for tag in joint_release.REQUIRED_ORX_TAGS:
        assert tag in tags
    assert joint_release.V030_TAG in tags
    # the constants are kept in lockstep with the existing chain
    assert joint_release.V030_COMMIT == check_release.V030_COMMIT
    assert joint_release.V030_TAG == check_release.V030_TAG


# ---------------------------------------------------------------------------
# One-shot checkouts from deterministic commits
# ---------------------------------------------------------------------------


def test_archive_checkouts_exclude_uncommitted_worktree_content(
        scratch_git_repo, tmp_path):
    repo = scratch_git_repo
    commit = joint_release.head_commit(repo)
    # versions are read FROM THE COMMIT: the worktree edit to 9.9.9 is
    # invisible to the frozen binding (the version reader parses the
    # committed file content, exactly what the binding records)
    text = joint_release._git(repo, "show", f"{commit}:src/__init__.py")
    assert joint_release._version_in_source(text, "scratch") == "1.0.0"
    assert (repo / "src" / "__init__.py").read_text() != text
    status = joint_release.worktree_status(repo)
    assert status["dirty_files"] == 2  # modified + untracked
    # a one-shot archive checkout carries neither
    out = tmp_path / "checkout"
    joint_release.archive_commit(repo, commit, out)
    assert (out / "src" / "__init__.py").read_text() == \
        '__version__ = "1.0.0"\n'
    assert not (out / "UNCOMMITTED-REPORT.md").exists()
    assert not (out / ".git").exists()
    # the original repo is untouched by the checkout
    assert (repo / "UNCOMMITTED-REPORT.md").exists()


def test_freeze_records_worktree_dirtiness_honestly(frozen):
    _, manifest = frozen
    # the ORX worktree carries uncommitted G011 work during development;
    # freeze records it — and the archive-based build excludes it
    assert manifest["binding"]["orx"]["worktree"]["dirty_files"] > 0


# ---------------------------------------------------------------------------
# Samples: genuine old-version sources
# ---------------------------------------------------------------------------


def test_store_v2_sample_is_genuine_and_traceable(tmp_path):
    report, problems = joint_release.verify_samples(
        REPO_ROOT, joint_release.ANALYTICS_REPO, workroot=tmp_path)
    assert problems == []
    sample = report["analytics_store_v2"]
    assert sample["store_version"] == "2"
    assert sample["matches_regeneration"] is True  # real old code regen
    assert sample["commit"] == joint_release.ANALYTICS_STORE_V2_COMMIT
    provenance = (REPO_ROOT / joint_release.STORE_V2_SAMPLE_DIR
                  / "PROVENANCE.md").read_text()
    assert "generate-samples" in provenance        # the generation command
    assert sample["sha256"] in provenance          # and its digest
    assert sample["tables"]["orx_attempts"] > 0    # non-trivial content
    # the old ORX sample is the existing fixture, referenced not copied
    assert report["orx_old_sample"]["schema_version"] == "8"
    assert not (REPO_ROOT / joint_release.JOINT_FIXTURE_DIR
                / "release-0.3.0").exists()


def test_old_analytics_refuses_newer_orx_schema(tmp_path):
    """源码回退导入: rolling the analytics source back to the store-v2
    era and importing a CURRENT (schema v15) ORX project is refused by
    the gate — honestly, without touching the source."""
    facts = joint_release.old_analytics_refuses_newer_orx(
        REPO_ROOT, joint_release.ANALYTICS_REPO, tmp_path / "refusal")
    assert facts["old_analytics_version"] == "0.4.1"
    assert any("schema gate refuses version 15" in gap
               for gap in facts["gaps"])
    assert facts["source_untouched"] is True


def test_current_analytics_upgrades_store_v2_additively(tmp_path):
    facts = joint_release.current_analytics_upgrades_store_v2(
        joint_release.ANALYTICS_REPO,
        REPO_ROOT / joint_release.STORE_V2_SAMPLE_DIR / "analytics.db",
        tmp_path / "upgrade")
    assert facts["current_analytics_version"] == "0.4.3"
    assert facts["upgraded_store_version"] == "4"
    assert facts["receipts_mirror"] is True
    assert facts["zc_request_token_columns"] is True
    assert facts["snapshots_kept"] == 1
    assert facts["future_version_refused"] is True
    assert facts["source_unchanged"] is True  # the committed sample is read-only


# ---------------------------------------------------------------------------
# Fail-closed stages: no App / GUI / signature evidence is ever a skip
# ---------------------------------------------------------------------------


def test_build_fails_closed_without_the_final_app(frozen):
    work, _ = frozen
    with pytest.raises(joint_release.StageFailure) as excinfo:
        joint_release.stage_build(work, joint_release.StageLog(work, "build"))
    assert excinfo.value.code == "app-artifact-missing"
    assert "T006" in excinfo.value.message  # names the deliverable's owner
    # the honest failure is recorded in the manifest
    recorded = joint_release.load_manifest(work)["stages"]["build"]
    assert recorded["status"] == "failed"
    assert recorded["code"] == "app-artifact-missing"


def test_exercise_fails_closed_without_the_final_app(frozen):
    work, _ = frozen
    with pytest.raises(joint_release.StageFailure) as excinfo:
        joint_release.stage_exercise(
            work, joint_release.StageLog(work, "exercise"))
    # ordering: build has not passed (it failed closed above)
    assert excinfo.value.code == "missing-step-evidence"
    # even with a fabricated passed build record, an unbound app fails
    manifest = joint_release.load_manifest(work)
    (work / "logs").mkdir(exist_ok=True)
    (work / "logs" / "02-build.log").write_text("stand-in log\n")
    joint_release.record_stage(
        manifest, "build", "passed", "logs/02-build.log")
    joint_release.save_manifest(work, manifest)
    with pytest.raises(joint_release.StageFailure) as excinfo:
        joint_release.stage_exercise(
            work, joint_release.StageLog(work, "exercise"))
    assert excinfo.value.code == "app-artifact-missing"
    assert "final app" in excinfo.value.message


def test_verify_rejection_paths_are_behavioral(frozen, tmp_path):
    """错误产物 / 提交不符 / 哈希变化 / 缺失步骤证据 / GUI / 签名."""
    work, manifest = frozen
    probe = joint_release.probe_rejections(tmp_path / "rejections", manifest)
    assert sorted(probe) == [
        "artifact-hash-mismatch",
        "artifact-identity-mismatch",
        "channel-artifact-mismatch",
        "commit-mismatch",
        "gui-evidence-missing",
        "missing-step-evidence",
        "signature-evidence-missing",
    ]


def test_verify_rejects_a_skipped_stage(frozen, tmp_path):
    work, manifest = frozen
    scratch = tmp_path / "skipped"
    scratch.mkdir()
    crafted = {
        "binding": manifest["binding"],
        "artifacts": {},
        "stages": {
            "freeze": {"status": "passed", "log": "logs/01.log"},
            "battery": {"status": "passed", "log": "logs/03.log"},
            "build": {"status": "skipped", "log": "logs/02.log"},
        },
    }
    joint_release.save_manifest(scratch, crafted)
    (scratch / "logs").mkdir(exist_ok=True)
    for name in ("01.log", "02.log", "03.log"):
        (scratch / "logs" / name).write_text("stand-in log\n")
    with pytest.raises(joint_release.StageFailure) as excinfo:
        joint_release.stage_verify(
            scratch, joint_release.StageLog(scratch, "verify"))
    assert excinfo.value.code == "skipped-not-accepted"


# ---------------------------------------------------------------------------
# verify-remote: the consumer side of the distribution channel
# ---------------------------------------------------------------------------


def _full_pass_state(src_work: Path, src_manifest: dict, dest: Path) -> Path:
    """A scratch state that would pass local verify (stand-in artifacts,
    step records, GUI + signature evidence), for verify-remote tests."""
    dest.mkdir(parents=True, exist_ok=True)
    binding = json.loads(json.dumps(src_manifest["binding"]))
    wheel = dest / "dist" / \
        f"orx_agent-{binding['orx']['version']}-py3-none-any.whl"
    wheel.parent.mkdir(parents=True)
    wheel.write_bytes(b"stand-in wheel\n")
    app = dest / "dist" / "ORXAnalytics-0.0.0-dev-macos13-arm64.zip"
    app.write_bytes(b"stand-in app\n")
    manifest = {
        "binding": binding,
        "artifacts": {
            "orx_wheel": {"path": f"dist/{wheel.name}",
                          "sha256": joint_release.sha256_file(wheel),
                          "built_from": binding["orx"]["commit"],
                          "version": binding["orx"]["version"]},
            "analytics_wheel": {"path": f"dist/{wheel.name}",
                                "sha256": joint_release.sha256_file(wheel),
                                "built_from": binding["analytics"]["commit"]},
            "app_archive": {
                "path": f"dist/{app.name}",
                "sha256": joint_release.sha256_file(app),
                "built_from": binding["analytics"]["commit"],
                "mode": "dev",
                "signature": {"codesign_verify_exit": 0,
                              "signature_style": "adhoc",
                              "authorities": [], "adhoc": True}},
        },
        "stages": {
            "freeze": {"status": "passed", "log": "logs/01-freeze.log"},
            "battery": {"status": "passed", "log": "logs/02-battery.log"},
            "build": {"status": "passed", "log": "logs/03-build.log"},
            "exercise": {
                "status": "passed", "log": "logs/04-exercise.log",
                "engine": {"connected_via": "stand-in",
                           "read_only_source": True}},
            "verify": {"status": "passed", "log": "logs/05-verify.log",
                       "gui_evidence": {"operator": "stand-in",
                                        "observed_at": "2026-10-17"}},
        },
    }
    logs = dest / "logs"
    logs.mkdir(exist_ok=True)
    for name in ("01-freeze.log", "02-battery.log", "03-build.log",
                 "04-exercise.log", "05-verify.log"):
        (logs / name).write_text("stand-in stage log\n")
    (dest / "gui-evidence.json").write_text(json.dumps({
        "operator": "stand-in", "artifacts": ["logs/04-exercise.log"],
        "observed_at": "2026-10-17T00:00:00+00:00"}))
    joint_release.save_manifest(dest, manifest)
    return dest


def test_verify_remote_fails_closed_without_channel_root(frozen, tmp_path):
    work, manifest = frozen
    state = _full_pass_state(work, manifest, tmp_path / "accepted")
    # local verify green on the crafted state (stand-in artifacts)
    joint_release.stage_verify(state, joint_release.StageLog(state, "verify"))
    with pytest.raises(joint_release.StageFailure) as excinfo:
        joint_release.stage_verify_remote(
            state, joint_release.StageLog(state, "remote"), None)
    assert excinfo.value.code == "channel-root-missing"
    assert "T008" in excinfo.value.message


def test_verify_remote_accepts_a_matching_channel_and_rejects_drift(
        frozen, tmp_path):
    work, manifest = frozen
    state = _full_pass_state(work, manifest, tmp_path / "accepted")
    joint_release.stage_verify(state, joint_release.StageLog(state, "verify"))

    # publish: byte-identical artifacts + the same binding
    channel = tmp_path / "channels" / "internal-preview"
    channel.mkdir(parents=True)
    shutil.copytree(state / "dist", channel / "dist")
    shutil.copy(state / "manifest.json", channel / "manifest.json")
    result = joint_release.stage_verify_remote(
        state, joint_release.StageLog(state, "remote"), channel)
    assert result["stages"]["verify-remote"]["status"] == "passed"

    # tampered published bytes are rejected
    wheel = next((channel / "dist").glob("orx_agent-*.whl"))
    keep = wheel.read_bytes()
    wheel.write_bytes(keep + b"tampered")
    with pytest.raises(joint_release.StageFailure) as excinfo:
        joint_release.stage_verify_remote(
            state, joint_release.StageLog(state, "remote"), channel)
    assert excinfo.value.code == "remote-hash-mismatch"
    wheel.write_bytes(keep)

    # a drifted remote binding (different frozen commit) is rejected
    good_manifest = (channel / "manifest.json").read_text()
    remote = json.loads(good_manifest)
    remote["binding"]["orx"]["commit"] = "f" * 40
    (channel / "manifest.json").write_text(json.dumps(remote))
    with pytest.raises(joint_release.StageFailure) as excinfo:
        joint_release.stage_verify_remote(
            state, joint_release.StageLog(state, "remote"), channel)
    assert excinfo.value.code == "remote-binding-mismatch"
    (channel / "manifest.json").write_text(good_manifest)

    # channel separation: the internal-preview root must not carry the
    # official DMG flavor
    (channel / "dist" / "ORXAnalytics-0.0.0-dist-macos13-arm64.dmg") \
        .write_bytes(b"foreign flavor")
    with pytest.raises(joint_release.StageFailure) as excinfo:
        joint_release.stage_verify_remote(
            state, joint_release.StageLog(state, "remote"), channel)
    assert excinfo.value.code == "channel-separation-violation"


# ---------------------------------------------------------------------------
# The engine-connection machinery against a stand-in delivery
# ---------------------------------------------------------------------------


def test_stand_in_engine_connection_readonly_and_isolated(tmp_path):
    """The harness's engine machinery for real: the app's own serve entry
    (from the frozen analytics commit), the socket protocol, two
    collects of an isolated project, read-only sources, and NO
    real-session mirrors. The delivery is a declared STAND-IN (dev
    runtime, no .app bundle) — this test claims harness machinery, not
    product acceptance."""
    work = tmp_path / "engine"
    work.mkdir()
    an_commit = joint_release.head_commit(joint_release.ANALYTICS_REPO)
    checkout = joint_release.archive_commit(
        joint_release.ANALYTICS_REPO, an_commit, work / "an-checkout")
    app = joint_release.assemble_stand_in_app(work, checkout, "0.4.2")

    # an isolated ORX source project from the committed v0.3.0 fixture
    proj = joint_release.stage_orx_sample(
        REPO_ROOT / joint_release.V030_FIXTURE_DIR,
        (work / "sample-project").resolve())
    source_sha = joint_release.sha256_file(proj / ".orx" / "state.db")

    log = joint_release.StageLog(work, "engine-test")
    data_dir = work / "engine-data"
    handle = joint_release.launch_bundle_engine(app, data_dir, log)
    analytics_db = handle["data_dir"] / "analytics.db"
    # post-stop evidence location (same file unless the socket path was
    # rerouted to a short temp dir)
    evidence_db = (handle.get("orig_data_dir") or handle["data_dir"]) \
        / "analytics.db"
    try:
        assert handle["bundled_version"] == "0.4.2"
        assert handle["hello"]["op"] == "overview"
        register_env = joint_release.isolated_env(work / "reg-home")
        register_env["PYTHONPATH"] = str(app / "Contents" / "Resources")
        register = joint_release._run(
            [str(app / "Contents" / "Resources" / "python" / "bin" / "python3"),
             "-m", "orxanalytics", "--data-dir", str(handle["data_dir"]),
             "projects", "add", str(proj), "--name", "stand-in"],
            cwd=work, env=register_env)
        assert register.returncode == 0, register.stderr
        project_key = str(proj)  # the registry keys by the RESOLVED path

        # repeated collection through the socket: with an UNCHANGED
        # source the engine's change detection skips the re-import (no
        # duplicate snapshot); the self-test's exercise covers the
        # changed-source case (exactly 2 snapshots).
        joint_release.bundle_engine_refresh(
            handle, analytics_db, project_key, 1)
        joint_release.bundle_engine_refresh(
            handle, analytics_db, project_key, 1, reason="background")
        overview = joint_release.EngineClient(handle["socket"]).request(
            {"op": "overview", "protocol_version": 1})
    finally:
        joint_release.stop_bundle_engine(handle)

    ours = [item for bucket in ("active", "recent_completed")
            for item in overview.get(bucket, [])
            if item.get("project") == project_key]
    assert ours, "the engine never reported the isolated project"
    run_item = ours[0]
    assert run_item["schema_version"] == "8"  # the v0.3.0 sample, verbatim
    assert run_item["goal"]["status"]["raw"] in ("active", "done")

    conn = sqlite3.connect(f"file:{evidence_db}?mode=ro",
                           uri=True)
    try:
        snapshots = conn.execute(
            "SELECT COUNT(*) FROM orx_snapshots").fetchone()[0]
        attempts = conn.execute(
            "SELECT COUNT(*) FROM orx_attempts").fetchone()[0]
        non_orx = sum(conn.execute(
            f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("zc_sessions", "cx_usage", "cur_composers"))
    finally:
        conn.close()
    # two collects of an UNCHANGED source: still one snapshot, still the
    # same three attempts — repeated collection never double counts
    assert snapshots == 1
    assert attempts == 3
    assert non_orx == 0    # no real zcode/codex/cursor session mirrored
    # read-only: the source project database is byte-identical
    assert joint_release.sha256_file(proj / ".orx" / "state.db") == source_sha


def test_exercise_environment_isolates_sessions_and_mutes_notifications():
    env = joint_release.isolated_env(Path("/tmp/jr-env-probe"))
    assert env["ORX_ANALYTICS_NOTIFY"] == "0"      # no system notifications
    assert env["ORX_QUOTA_PREFLIGHT"] == "0"
    assert "analytics-data" in env["ORX_ANALYTICS_HOME"]  # isolated layer
    assert "PYTHONPATH" not in env                  # no inherited imports
    assert env["HOME"].startswith("/tmp/jr-env-probe")
    # the harness NEVER launches the GUI app binary: the only thing it
    # ever starts from a bundle is the engine serve wrapper
    source = MODULE_PATH.read_text()
    assert "orx_engine_serve.py" in source
    assert "Contents/MacOS" not in source


def test_channel_artifact_modes_are_separated():
    assert joint_release.CHANNEL_APP_MODE == {
        "internal-preview": "dev", "official": "dist",
    }


# ---------------------------------------------------------------------------
# The clean-checkout battery + the frozen candidate (G011 T006)
# ---------------------------------------------------------------------------


def test_battery_and_candidate_fail_closed_without_their_steps(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    joint_release.save_manifest(
        work, {"binding": None, "artifacts": {}, "stages": {}})
    with pytest.raises(joint_release.StageFailure) as excinfo:
        joint_release.stage_battery(
            work, joint_release.StageLog(work, "battery"))
    assert excinfo.value.code == "missing-step-evidence"

    manifest = joint_release.load_manifest(work)
    (work / "logs").mkdir(exist_ok=True)
    (work / "logs" / "01-freeze.log").write_text("stand-in log\n")
    joint_release.record_stage(manifest, "freeze", "passed",
                               "logs/01-freeze.log")
    joint_release.save_manifest(work, manifest)
    with pytest.raises(joint_release.StageFailure) as excinfo:
        joint_release.stage_candidate(
            work, joint_release.StageLog(work, "candidate"))
    # battery has not run: the candidate binds the frozen, battery-
    # checked, built combination or refuses
    assert excinfo.value.code == "missing-step-evidence"


def _scratch_candidate(dest: Path, binding: dict) -> Path:
    """A stand-in candidate.json with stand-in artifacts: real binding
    (real frozen commits), fake bytes — for the candidate-phase
    rejection paths only, never a pass claim."""
    dest.mkdir(parents=True, exist_ok=True)
    dist = dest / "dist"
    dist.mkdir()
    files = {
        "orx_wheel":
            dist / f"orx_agent-{binding['orx']['version']}-py3-none-any.whl",
        "analytics_wheel":
            dist / f"orx_analytics-{binding['analytics']['version']}"
                   f"-py3-none-any.whl",
        "analytics_sdist":
            dist / f"orx_analytics-{binding['analytics']['version']}.tar.gz",
        "app_archive":
            dist / "ORXAnalytics-0.0.0-dev-macos13-arm64.zip",
    }
    for path in files.values():
        path.write_bytes(b"stand-in artifact bytes\n")
    logs = dest / "logs"
    logs.mkdir()
    (logs / "01-battery.log").write_text("stand-in battery log\n")
    doc = {
        "schema": 1,
        "phase": "candidate",
        "goal": "G011",
        "goal_id": "g011",
        "published": False,
        "binding": binding,
        "artifacts": {
            "orx_wheel": {
                "path": f"dist/{files['orx_wheel'].name}",
                "sha256": joint_release.sha256_file(files["orx_wheel"]),
                "built_from": binding["orx"]["commit"],
                "version": binding["orx"]["version"]},
            "analytics_wheel": {
                "path": f"dist/{files['analytics_wheel'].name}",
                "sha256": joint_release.sha256_file(files["analytics_wheel"]),
                "built_from": binding["analytics"]["commit"]},
            "analytics_sdist": {
                "path": f"dist/{files['analytics_sdist'].name}",
                "sha256": joint_release.sha256_file(files["analytics_sdist"]),
                "built_from": binding["analytics"]["commit"]},
            "app_archive": {
                "path": f"dist/{files['app_archive'].name}",
                "sha256": joint_release.sha256_file(files["app_archive"]),
                "built_from": binding["analytics"]["commit"],
                "mode": "dev",
                "signature": {"codesign_verify_exit": 0,
                              "signature_style": "adhoc",
                              "authorities": [], "adhoc": True}},
        },
        "battery": {"checks": [
            {"name": name, "exit_code": 0, "log": "logs/01-battery.log"}
            for name in joint_release.REQUIRED_BATTERY_CHECKS]},
        "historical_tags": {},
        "decision": {},
    }
    candidate = dest / "candidate.json"
    candidate.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return candidate


def test_verify_candidate_phase_rejects_tampering(frozen, tmp_path, capsys):
    """``verify --manifest … --phase candidate`` is behavioral: usage
    errors exit 2; a byte drift fails closed (artifact-hash-mismatch); a
    non-app archive fails closed naming it; a non-candidate document is
    refused before anything is trusted."""
    _, manifest = frozen
    binding = json.loads(json.dumps(manifest["binding"]))
    candidate = _scratch_candidate(tmp_path / "candidate", binding)

    # --manifest requires --phase candidate (the acceptance verify uses
    # --work-dir); argparse usage error
    with pytest.raises(SystemExit) as excinfo:
        joint_release.main(["verify", "--manifest", str(candidate)])
    assert excinfo.value.code == 2
    capsys.readouterr()  # flush the argparse usage text

    # a byte drift in a bound artifact fails closed
    wheel = (tmp_path / "candidate" / "dist"
             / f"orx_agent-{binding['orx']['version']}-py3-none-any.whl")
    keep = wheel.read_bytes()
    wheel.write_bytes(keep + b"tampered")
    assert joint_release.main(
        ["verify", "--manifest", str(candidate),
         "--phase", "candidate"]) == 1
    assert json.loads(capsys.readouterr().err)["code"] \
        == "artifact-hash-mismatch"
    wheel.write_bytes(keep)

    # digests/identity and the battery evidence pass, but the bound app
    # archive is not an app: fail closed naming it (never a skip)
    assert joint_release.main(
        ["verify", "--manifest", str(candidate),
         "--phase", "candidate"]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "app-archive-invalid"

    # a document that is not a schema-1 candidate is refused outright
    doc = json.loads(candidate.read_text())
    doc["phase"] = "acceptance"
    candidate.write_text(json.dumps(doc))
    assert joint_release.main(
        ["verify", "--manifest", str(candidate),
         "--phase", "candidate"]) == 1
    assert json.loads(capsys.readouterr().err)["code"] \
        == "candidate-shape-invalid"


def test_candidate_uncommitted_inputs_guard_is_behavioral(frozen):
    """Only the baseline-protected user report may be dirty at freeze
    time; anything else (either repo) refuses the candidate."""
    _, manifest = frozen
    binding = json.loads(json.dumps(manifest["binding"]))
    binding["orx"]["worktree"] = {"dirty_files": 1,
                                  "entries": [" M docs/m1.2-report.md"]}
    binding["analytics"]["worktree"] = {"dirty_files": 0, "entries": []}
    assert joint_release.freeze_worktree_problems(binding) == []
    binding["orx"]["worktree"]["entries"].append("?? stray.txt")
    binding["analytics"]["worktree"]["entries"].append(" M src/x.py")
    problems = joint_release.freeze_worktree_problems(binding)
    assert len(problems) == 2
    assert "stray.txt" in problems[0]
    assert "src/x.py" in problems[1]


# ---------------------------------------------------------------------------
# G012: analytics-only 0.4.3 on the published ORX v0.4.0 pin
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def frozen_g012(tmp_path_factory):
    """One real G012 freeze: the ORX product input pinned to the published
    v0.4.0 tag commit, analytics at the release clone's HEAD."""
    work = tmp_path_factory.mktemp("jr-frozen-g012")
    log = joint_release.StageLog(work, "freeze")
    manifest = joint_release.stage_freeze(work, "internal-preview", log,
                                          goal_id="g012")
    return work, manifest


def test_g012_goal_profiles_pin_and_defaults():
    g011 = joint_release.GOAL_CONFIGS["g011"]
    assert g011["label"] == "G011"
    # the G011 default surface is unchanged
    assert g011["default_work_dir"] == joint_release.DEFAULT_WORK_DIR
    assert g011["decision_file"] == joint_release.DECISION_FILE
    assert g011["orx_input"] == "head"
    g012 = joint_release.GOAL_CONFIGS["g012"]
    assert g012["label"] == "G012"
    assert g012["default_work_dir"] == Path("dist") / "g012"
    assert g012["decision_file"] == (
        joint_release.REPO_ROOT / "docs" / "diagnostics" / "g012"
        / "distribution.json")
    assert g012["orx_input"] == "tag:v0.4.0"
    # the published pin is live, real, and reads the published version
    live = joint_release._git(joint_release.REPO_ROOT, "rev-parse",
                              "v0.4.0^{commit}")
    assert live == joint_release.ORX_V040_COMMIT
    assert joint_release.orx_version_at(
        joint_release.REPO_ROOT, joint_release.ORX_V040_COMMIT) == "0.4.0"


def test_g012_freeze_pins_orx_product_input_to_published_v040(frozen_g012):
    work, manifest = frozen_g012
    binding = manifest["binding"]
    assert binding["goal_id"] == "g012"
    assert binding["goal_label"] == "G012"
    # ORX product input: the PUBLISHED v0.4.0 tag commit (never HEAD)
    assert binding["orx"]["input"] == "tag:v0.4.0"
    assert binding["orx"]["pinned_tag"] == {
        "tag": "v0.4.0",
        "commit": joint_release.ORX_V040_COMMIT,
        "published_commit": joint_release.ORX_V040_COMMIT,
    }
    assert binding["orx"]["commit"] == joint_release.ORX_V040_COMMIT
    assert binding["orx"]["version"] == "0.4.0"
    assert joint_release.orx_version_at(
        joint_release.REPO_ROOT,
        binding["orx"]["commit"]) == binding["orx"]["version"]
    # the analytics input is still the release clone's HEAD
    assert binding["analytics"]["commit"] == joint_release.head_commit(
        joint_release.ANALYTICS_REPO)
    # the G012 acceptance driver commit is recorded SEPARATELY from the
    # product inputs (provenance only)
    assert binding["acceptance_driver"]["commit"] == \
        joint_release.head_commit(joint_release.REPO_ROOT)
    record = manifest["stages"]["freeze"]
    assert record["status"] == "passed"
    assert record["goal_id"] == "g012"
    assert (work / record["log"]).is_file()


def test_g012_freeze_refuses_a_moved_v040_pin(frozen_g012, monkeypatch):
    work, _ = frozen_g012
    monkeypatch.setattr(joint_release, "ORX_V040_COMMIT", "0" * 40)
    moved = work / "moved-pin"
    with pytest.raises(joint_release.StageFailure) as excinfo:
        joint_release.stage_freeze(
            moved, "internal-preview",
            joint_release.StageLog(moved, "freeze"), goal_id="g012")
    assert excinfo.value.code == "orx-pin-moved"


def test_g012_orx_pin_rechecked_at_candidate_time(frozen_g012):
    _, manifest = frozen_g012
    binding = json.loads(json.dumps(manifest["binding"]))
    assert joint_release.orx_pin_problems(binding) == []
    moved = json.loads(json.dumps(binding))
    moved["orx"]["pinned_tag"]["published_commit"] = "0" * 40
    problems = joint_release.orx_pin_problems(moved)
    assert problems and "moved" in problems[0]
    # a G011 binding (no pin) is untouched by the pin check
    assert joint_release.orx_pin_problems({"orx": {}}) == []


def test_candidate_document_labels_the_goal(frozen, frozen_g012):
    """The candidate record is labeled from the binding: G012 is G012,
    never G011 (and never carries a G011 task id or note)."""
    _, g011_manifest = frozen
    _, g012_manifest = frozen_g012

    def doc_for(manifest):
        return joint_release.candidate_document(
            manifest["binding"], decision={"channel": "stub"},
            snapshot={"orx": [], "analytics": []},
            battery_checks=[], missing=["orx_pytest"],
            battery_record={"checkouts": {}, "log": "logs/01.log"},
            artifacts={}, facts={}, meta={})

    g011_doc = doc_for(g011_manifest)
    assert g011_doc["goal"] == "G011"
    assert g011_doc["task"] == "T006"
    g012_doc = doc_for(g012_manifest)
    assert g012_doc["goal"] == "G012"
    assert g012_doc["goal_id"] == "g012"
    assert g012_doc["task"] is None
    assert g012_doc["published"] is False
    assert "G011" not in json.dumps(g012_doc)  # no G011 mislabel anywhere
    # a legacy G011 binding (no goal keys) still defaults to G011
    legacy = json.loads(json.dumps(g011_manifest["binding"]))
    legacy.pop("goal_id", None)
    legacy.pop("goal_label", None)
    doc = joint_release.candidate_document(
        legacy, decision={}, snapshot={}, battery_checks=[], missing=[],
        battery_record={}, artifacts={}, facts={}, meta={})
    assert doc["goal"] == "G011"


def test_g012_uncommitted_inputs_guard_exempts_only_generated_dirs(frozen_g012):
    """G012: the analytics clone's untracked GENERATED build output
    (apps/macos/.build) is the ONE exemption; tracked modifications,
    untracked non-generated paths, the ORX protected-report rule and the
    whole G011 guard still refuse."""
    _, manifest = frozen_g012
    binding = json.loads(json.dumps(manifest["binding"]))
    binding["orx"]["worktree"] = {"dirty_files": 1,
                                  "entries": [" M docs/m1.2-report.md"]}
    binding["analytics"]["worktree"] = {
        "dirty_files": 1, "entries": ["?? apps/macos/.build/"]}
    assert joint_release.freeze_worktree_problems(binding) == []
    # a tracked modification inside the same directory is NOT exempt
    binding["analytics"]["worktree"]["entries"] = [
        " M apps/macos/.build/debug.yaml"]
    problems = joint_release.freeze_worktree_problems(binding)
    assert len(problems) == 1 and "debug.yaml" in problems[0]
    # an untracked non-generated path is NOT exempt
    binding["analytics"]["worktree"]["entries"] = ["?? stray.txt"]
    problems = joint_release.freeze_worktree_problems(binding)
    assert len(problems) == 1 and "stray.txt" in problems[0]
    # the exemption is goal-scoped: the same generated dir under G011
    # still refuses (the G011 gate is unchanged)
    g011_binding = json.loads(json.dumps(binding))
    g011_binding["goal_id"] = "g011"
    g011_binding["analytics"]["worktree"]["entries"] = [
        "?? apps/macos/.build/"]
    problems = joint_release.freeze_worktree_problems(g011_binding)
    assert len(problems) == 1 and ".build" in problems[0]
    # the ORX protected-report rule is unchanged for G012
    binding["analytics"]["worktree"]["entries"] = ["?? apps/macos/.build/"]
    binding["orx"]["worktree"]["entries"].append("?? stray.txt")
    problems = joint_release.freeze_worktree_problems(binding)
    assert len(problems) == 1 and "ORX" in problems[0]


def test_g012_battery_joint_layout_pins_the_v042_release_pair(
        frozen_g012, monkeypatch):
    """G012: the joint layout beside the pinned v0.4.0 ORX clone carries
    the G011 RELEASE PAIR analytics (tag v0.4.2) — the environment the
    v0.4.0 suite's own joint tests pin (analytics 0.4.2 / store v3); the
    G012 analytics freeze commit (0.4.3 / store v4) is checked in its own
    clean checkout. A moved v0.4.2 tag is a stop, never a re-pin. G011
    keeps the single version-consistent clone (the freeze commit)."""
    _, manifest = frozen_g012
    binding = manifest["binding"]
    # g011: the joint layout carries the freeze commit itself (G011 pair)
    assert joint_release.joint_layout_analytics_commit(
        "g011", "freezec0mmit") == "freezec0mmit"
    # g012: resolved from the REAL analytics repo's v0.4.2 tag
    resolved = joint_release.joint_layout_analytics_commit(
        binding["goal_id"], binding["analytics"]["commit"])
    assert resolved == joint_release.ANALYTICS_V042_COMMIT
    # the pair clone is NOT the 0.4.3 freeze commit (the pair moved on)
    assert resolved != binding["analytics"]["commit"]
    # a moved v0.4.2 tag fails closed
    monkeypatch.setattr(
        joint_release, "_git", lambda *a, **k: "0" * 40)
    with pytest.raises(joint_release.StageFailure) as excinfo:
        joint_release.joint_layout_analytics_commit("g012", "freezec0mmit")
    assert excinfo.value.code == "analytics-v042-pin-moved"


def test_g012_prescribed_invocation_shapes(tmp_path, capsys):
    """The exact T007/T008 invocation shapes under dist/g012 run and —
    before any G012 freeze exists — FAIL CLOSED (manifest-missing, never
    a silent pass). --goal-id is explicit: unknown ids are usage errors."""
    work = tmp_path / "g012"
    shapes = (
        ["verify", "--manifest", str(work / "candidate.json"),
         "--phase", "candidate"],
        ["verify", "--work-dir", str(work)],
        ["verify-remote", "--work-dir", str(work),
         "--channel-root", str(work / "channels" / "internal-preview")],
        ["battery", "--work-dir", str(work)],
        ["build", "--work-dir", str(work)],
        ["candidate", "--work-dir", str(work)],
        ["exercise", "--work-dir", str(work)],
    )
    for argv in shapes:
        capsys.readouterr()
        assert joint_release.main(list(argv)) == 1, argv
        reported = json.loads(capsys.readouterr().err)
        assert reported["code"] == "manifest-missing", (argv, reported)

    # the literal published-shape check (paths relative to the repo root,
    # exactly as prescribed) fails closed the same way and writes nothing;
    # once a REAL dist/g012/candidate.json exists the same command is the
    # prescribed live check and is not second-guessed here
    literal = REPO_ROOT / "dist" / "g012" / "candidate.json"
    if not literal.exists():
        proc = subprocess.run(
            ["uv", "run", "python", str(MODULE_PATH),
             "verify", "--manifest", "dist/g012/candidate.json",
             "--phase", "candidate"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 1
        assert json.loads(proc.stderr)["code"] == "manifest-missing"
        assert not literal.parent.exists()

    # --goal-id is explicit: unknown ids are usage errors (exit 2)
    with pytest.raises(SystemExit) as excinfo:
        joint_release.main(["freeze", "--goal-id", "bogus",
                            "--work-dir", str(work)])
    assert excinfo.value.code == 2
    capsys.readouterr()
