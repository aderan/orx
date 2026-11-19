"""Runtime seal check regression (G012 T006).

The app must not break its own code seal when IT launches its bundled
engine — first launch and engine restart. The stand-in apps below model
the production launch path: a real ad-hoc-signed ``.app`` bundle whose
executable spawns the bundled python with the bundled serve script as a
child (the engine is the app's child and outlives the app), with the
engine's output redirected to the app's launch log under the DATA dir.

The checker must:

- pass the launch path that disables bytecode writing itself — even
  though the checker deliberately hands the app the adversarial value
  ``PYTHONDONTWRITEBYTECODE=0`` (on CPython 3.12 that value still
  ENABLES bytecode writing; only ``1`` disables it);
- reject bytecode landing in the bundle (the disclosed v0.4.2 defect);
- reject a bundle whose strict verification already fails;
- reject a run whose REAL app-launch evidence is missing, even when the
  bundle content and the signature are perfectly fine.

Everything runs in temporary directories; no real engine, no real
projects, and never the user's installed app (CONTRIBUTING: 测试用合成来源).
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_runtime_seal.py"

# The prescribed check runs pytest from the ORX repository root, where
# neither the clone root nor its scripts/ package is importable (and the
# ORX root has its own scripts/ that must not shadow the clone's).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytestmark = pytest.mark.skipif(
    shutil.which("codesign") is None,
    reason="codesign is required to seal and verify stand-in bundles")

# The engine's stand-in serve script: imports the package next to the
# runtime (the import whose bytecode once broke the seal), then serves
# the data dir's socket until terminated.
SERVE_SCRIPT = textwrap.dedent("""\
    import os
    import signal
    import socket
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import orxanalytics

    data_dir = sys.argv[1]
    socket_path = os.path.join(data_dir, "engine.sock")
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    server.listen(4)
    server.settimeout(0.5)
    stopped = []
    signal.signal(signal.SIGTERM, lambda *_: stopped.append(True))
    print("stand-in engine serving; package marker:",
          orxanalytics.MARKER, flush=True)
    while not stopped:
        try:
            connection, _ = server.accept()
            connection.close()
        except socket.timeout:
            continue
    """)

# The stand-in app executable. The fixed variant models the FIXED
# makeChild (forces PYTHONDONTWRITEBYTECODE=1 over any inherited
# conflict); the defect variant models v0.4.2 (passes the parent
# environment through verbatim). Both keep the app alive until
# terminated and leave the engine child independent of the app.
APP_WRAPPER = textwrap.dedent("""\
    #!/bin/sh
    set -eu
    APP="$(cd "$(dirname "$0")/../.." && pwd)"
    DATA="${{ORX_ANALYTICS_HOME:?seal check must set ORX_ANALYTICS_HOME}}"
    mkdir -p "$DATA/logs"
    {force_lines}
    "$APP/Contents/Resources/python/bin/python3" \\
        "$APP/Contents/Resources/orx_engine_serve.py" "$DATA" \\
        >>"$DATA/logs/engine-launch.log" 2>&1 &
    exec sleep 2147483647
    """)
FORCE_FLAG_LINES = "PYTHONDONTWRITEBYTECODE=1\nexport PYTHONDONTWRITEBYTECODE"

NO_ENGINE_WRAPPER = "#!/bin/sh\nexit 0\n"


def _writes_bytecode(python: str) -> bool:
    """A stand-in engine that cannot write bytecode could never
    reproduce the defect; require an interpreter that can."""
    with tempfile.TemporaryDirectory(prefix="orx-sealcap-") as tmp:
        module_dir = Path(tmp) / "mod"
        module_dir.mkdir()
        (module_dir / "m.py").write_text("MARKER = 1\n", encoding="utf-8")
        result = subprocess.run(
            [python, "-c", "import m"],
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(module_dir)},
            capture_output=True, timeout=60)
        if result.returncode != 0:
            return False
        return any(module_dir.rglob("*.pyc"))


@pytest.fixture(scope="module")
def capable_python() -> str:
    candidates = [os.environ.get("ORX_ANALYTICS_TESTS_PYTHON", ""),
                  sys.executable,
                  shutil.which("python3") or "",
                  "/opt/homebrew/bin/python3",
                  "/usr/bin/python3"]
    for candidate in candidates:
        if candidate and Path(candidate).exists() and _writes_bytecode(candidate):
            return candidate
    pytest.skip("no bytecode-writing python3 available to build a stand-in "
                "engine (Apple's /usr/bin/python3 cannot produce the defect)")


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def build_stand_in_app(root: Path, python: str, *, force_flag: bool,
                       no_engine: bool = False) -> Path:
    """A minimal but real .app: Info.plist, an executable, a bundled
    python (wrapper around a bytecode-writing interpreter) and the
    orxanalytics package whose import is the seal hazard."""
    app = root / "Seal Stand-in.app"
    resources = app / "Contents" / "Resources"
    resources.mkdir(parents=True)
    if no_engine:
        wrapper = NO_ENGINE_WRAPPER
    else:
        wrapper = APP_WRAPPER.format(
            force_lines=FORCE_FLAG_LINES if force_flag else ":")
    _write_executable(app / "Contents" / "MacOS" / "ORXAnalyticsApp", wrapper)
    _write_executable(resources / "python" / "bin" / "python3",
                      f'#!/bin/sh\nexec "{python}" "$@"\n')
    (resources / "orx_engine_serve.py").write_text(SERVE_SCRIPT,
                                                   encoding="utf-8")
    package = resources / "orxanalytics"
    package.mkdir()
    (package / "__init__.py").write_text(
        'MARKER = "stand-in-package"\n'
        'COMPUTED = sum(i * i for i in range(64))\n', encoding="utf-8")
    (app / "Contents" / "PkgInfo").write_text("APPL????", encoding="utf-8")
    (app / "Contents" / "Info.plist").write_text(textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
            "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0"><dict>
            <key>CFBundleExecutable</key><string>ORXAnalyticsApp</string>
            <key>CFBundleIdentifier</key><string>org.orxanalytics.standin</string>
            <key>CFBundlePackageType</key><string>APPL</string>
            <key>CFBundleShortVersionString</key><string>0.0.0-standin</string>
        </dict></plist>
        """), encoding="utf-8")
    return app


def adhoc_sign(app: Path) -> None:
    subprocess.run(["codesign", "--force", "--sign", "-", "--deep", str(app)],
                   check=True, capture_output=True, timeout=120)


def run_seal_check(app: Path, evidence_dir: Path,
                   *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECK_SCRIPT), "--app", str(app),
         "--evidence-dir", str(evidence_dir), *extra],
        capture_output=True, text=True, timeout=600)


@pytest.fixture
def short_data_dir():
    """A data dir directly under $TMPDIR: macOS limits AF_UNIX socket
    paths to ~104 chars, and pytest's tmp_path nests far deeper than
    that — a stand-in engine asked to bind there dies with "AF_UNIX
    path too long" before it can serve. Using the checker's --data-dir
    also exercises that flag for real."""
    path = Path(tempfile.mkdtemp(prefix="orx-sealdata-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def load_report(evidence_dir: Path) -> dict:
    import json
    return json.loads((evidence_dir / "seal-check-report.json")
                      .read_text(encoding="utf-8"))


def failure_codes(report: dict) -> list[str]:
    return [failure["code"] for failure in report.get("failures", [])]


# --------------------------------------------------------------- unit ----

def test_app_launch_environment_is_adversarial_never_masking(monkeypatch,
                                                              tmp_path):
    """The child env handed to the app: dev redirects stripped, data dir
    steered outside the user's live dir, and the bytecode value set to
    "0" — a WRITE-ENABLE value on CPython 3.12, so a missing fix cannot
    hide behind it."""
    from scripts.check_runtime_seal import app_launch_environment

    monkeypatch.setenv("ORX_ANALYTICS_ENGINE_PYTHON", "/dev/python")
    monkeypatch.setenv("ORX_ANALYTICS_ENGINE_SERVE", "/checkout/serve.py")
    monkeypatch.setenv("ORX_ANALYTICS_ENGINE_PYTHONPATH", "/checkout/src")
    child, stripped = app_launch_environment(tmp_path / "data")
    assert child["ORX_ANALYTICS_HOME"] == str(tmp_path / "data")
    assert child["PYTHONDONTWRITEBYTECODE"] == "0"
    assert sorted(stripped) == [
        "ORX_ANALYTICS_ENGINE_PYTHON",
        "ORX_ANALYTICS_ENGINE_PYTHONPATH",
        "ORX_ANALYTICS_ENGINE_SERVE",
    ]
    for var in stripped:
        assert var not in child


def test_snapshot_diff_classifies_added_bytecode():
    from scripts.check_runtime_seal import snapshot_diff

    before = {
        "Contents/Info.plist": {"type": "file", "size": 1, "sha256": "a"},
        "Contents/Resources/pkg/__init__.py": {"type": "file", "size": 2,
                                               "sha256": "b"},
        "Contents/Resources/empty": {"type": "dir"},
        "Contents/Resources/link": {"type": "symlink", "target": "../x"},
    }
    after = {
        "Contents/Info.plist": {"type": "file", "size": 1, "sha256": "z"},
        "Contents/Resources/pkg/__init__.py": {"type": "file", "size": 2,
                                               "sha256": "b"},
        "Contents/Resources/pkg/__pycache__/__init__.cpython-312.pyc": {
            "type": "file", "size": 9, "sha256": "p"},
        "Contents/Resources/link": {"type": "symlink", "target": "../y"},
    }
    diff = snapshot_diff(before, after)
    assert diff["added"] == [
        "Contents/Resources/pkg/__pycache__/__init__.cpython-312.pyc"]
    assert diff["removed"] == ["Contents/Resources/empty"]
    assert sorted(diff["changed"]) == ["Contents/Info.plist",
                                       "Contents/Resources/link"]
    assert diff["added_bytecode"] == diff["added"]


def test_snapshot_diff_clean_when_identical():
    from scripts.check_runtime_seal import snapshot_diff

    snapshot = {"Contents/Info.plist": {"type": "file", "size": 1,
                                        "sha256": "a"}}
    diff = snapshot_diff(snapshot, dict(snapshot))
    assert diff == {"added": [], "removed": [], "changed": [],
                    "added_bytecode": []}


# ----------------------------------------------------- end-to-end runs ----

def test_passes_when_launch_path_forces_the_flag(capable_python, tmp_path,
                                                 short_data_dir):
    """First launch AND engine restart through the app's own launch path
    keep the sealed bundle byte-identical, with live engine evidence for
    both rounds — despite the adversarial inherited value."""
    app = build_stand_in_app(tmp_path, capable_python, force_flag=True)
    adhoc_sign(app)
    evidence = tmp_path / "evidence"

    result = run_seal_check(app, evidence, "--launch-timeout", "60",
                            "--data-dir", str(short_data_dir))
    assert result.returncode == 0, result.stderr

    report = load_report(evidence)
    assert report["verdict"] == "pass"
    assert report["failures"] == []
    assert report["hostile_child_env"] == {"PYTHONDONTWRITEBYTECODE": "0"}

    assert [round["round"] for round in report["rounds"]] == [
        "first-launch", "engine-restart"]
    for round_ in report["rounds"]:
        assert round_["socket_observed"] is True
        assert round_["engine_process_observed"] is True
        # The engine is a child of the launched app — the production
        # makeChild topology, not something the checker started itself.
        assert round_["engine"]["ppid"] == round_["app_pid"]
        command = round_["engine"]["command"]
        assert "orx_engine_serve.py" in command
        assert "Contents/Resources" in command
        # The app's engine launch log lives under the DATA dir, never
        # inside the bundle.
        assert round_["engine_launch_log"]["path"].startswith(
            str(report["data_dir"]))

    assert report["bundle_diff"]["added"] == []
    assert report["bundle_diff"]["removed"] == []
    assert report["bundle_diff"]["changed"] == []
    assert report["codesign_before"]["exit_code"] == 0
    assert report["codesign_after"]["exit_code"] == 0
    for name in ("bundle-before.json", "bundle-after.json",
                 "codesign-before.log", "codesign-after.log",
                 "app-launch-1.log", "app-launch-2.log",
                 "seal-check-report.json"):
        assert (evidence / name).is_file(), f"missing evidence file {name}"
    # Runtime data (socket, logs) stayed outside the app bundle.
    assert (Path(report["data_dir"]) / "engine.sock").exists()
    assert not (app / "Contents" / "Resources" / "engine.sock").exists()


def test_rejects_new_bytecode_written_into_bundle(capable_python, tmp_path,
                                                  short_data_dir):
    """The disclosed v0.4.2 defect, reproduced: a launch path that does
    NOT force the flag lets the engine's imports write __pycache__ into
    the bundle; the check must refuse, naming the bytecode."""
    app = build_stand_in_app(tmp_path, capable_python, force_flag=False)
    adhoc_sign(app)
    evidence = tmp_path / "evidence"

    result = run_seal_check(app, evidence, "--launch-timeout", "60",
                            "--data-dir", str(short_data_dir))
    assert result.returncode != 0

    report = load_report(evidence)
    assert report["verdict"] == "fail"
    assert "bundle-bytecode-added" in failure_codes(report)
    assert "codesign-post-failed" in failure_codes(report)
    added = report["bundle_diff"]["added_bytecode"]
    assert added, "the defect run must name the added bytecode"
    assert all("/__pycache__/" in f"/{path}" or path.endswith(".pyc")
               for path in added)
    assert any("orxanalytics" in path for path in added)
    # The stray engine is gone: the check never leaves bundle processes.
    assert report["rounds"], "the defect run still records its rounds"
    assert all(not round_.get("bundle_processes_after_stop")
               for round_ in report["rounds"])


def test_rejects_bundle_whose_signature_already_fails(capable_python,
                                                      tmp_path):
    """A copy that is unsealed BEFORE any launch (the pre-verification
    gate): refuse without launching, so a broken install can never be
    laundered by a green run."""
    app = build_stand_in_app(tmp_path, capable_python, force_flag=True)
    adhoc_sign(app)
    (app / "Contents" / "_CodeSignature" / "CodeResources").write_text(
        "corrupted", encoding="utf-8")
    evidence = tmp_path / "evidence"

    result = run_seal_check(app, evidence, "--launch-timeout", "10")
    assert result.returncode != 0

    report = load_report(evidence)
    assert report["verdict"] == "fail"
    assert "codesign-pre-failed" in failure_codes(report)
    assert report["rounds"] == [], "a pre-broken bundle must not be launched"
    assert report["codesign_before"]["exit_code"] != 0


def test_rejects_missing_launch_evidence_with_intact_bundle(capable_python,
                                                             tmp_path,
                                                             short_data_dir):
    """The strongest form of the evidence gate: an 'app' that exits
    without ever starting an engine leaves content and signature intact,
    and the check STILL refuses — proving green runs require real
    launch evidence, not just an unchanged bundle."""
    app = build_stand_in_app(tmp_path, capable_python, force_flag=True,
                             no_engine=True)
    adhoc_sign(app)
    evidence = tmp_path / "evidence"

    result = run_seal_check(app, evidence, "--launch-timeout", "6",
                            "--data-dir", str(short_data_dir))
    assert result.returncode != 0

    report = load_report(evidence)
    assert report["verdict"] == "fail"
    assert "launch-evidence-missing" in failure_codes(report)
    assert report["bundle_diff"]["added"] == []
    assert report["bundle_diff"]["changed"] == []
    assert report["codesign_after"]["exit_code"] == 0
    rounds = report["rounds"]
    assert len(rounds) == 1, "a failed round stops the sequence"
    assert rounds[0]["socket_observed"] is False
    assert rounds[0]["engine_process_observed"] is False


def test_refuses_evidence_dir_inside_the_app(tmp_path):
    """Evidence (and by extension runtime data) may never be written
    into the bundle the check is guarding — and the refusal itself must
    not write anything into the refused location."""
    import json

    from scripts.check_runtime_seal import is_inside

    app = tmp_path / "Guarded.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    inside = app / "Contents" / "Resources" / "seal-evidence"
    assert is_inside(inside, app)
    assert not is_inside(tmp_path / "outside", app)

    result = subprocess.run(
        [sys.executable, str(CHECK_SCRIPT), "--app", str(app),
         "--evidence-dir", str(inside)],
        capture_output=True, text=True, timeout=120)
    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert "evidence-dir-inside-app" in failure_codes(report)
    assert not inside.exists(), "the refused location must stay untouched"
