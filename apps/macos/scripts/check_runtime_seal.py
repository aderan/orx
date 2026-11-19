"""Post-install runtime seal check for the ORX Analytics app (G012 T006).

The disclosed v0.4.2 defect: the first operator launch of the installed
app started the bundled engine with the parent environment verbatim, and
the bundled CPython (3.12.8) writes ``__pycache__``/*.pyc for imported
modules when ``PYTHONDONTWRITEBYTECODE`` is absent, ``"0"`` or empty.
That dropped 73 bytecode files INTO the signed ``.app`` and flipped
``codesign --verify --strict`` from 0 to 1 (evidence: ORX repository
``dist/g011/gui-acceptance/seal-break-codesign.log`` and
``seal-break-pyc-files.txt``). A harness-started engine stayed sealed
only because the harness environment already carried the flag.

This check verifies the FIXED app on an installed copy, end to end:

1. snapshot the bundle's complete content (file set + SHA-256 digests,
   symlink targets, directories) BEFORE any launch;
2. strict-verify the code seal BEFORE launch (``codesign --verify
   --deep --strict``);
3. launch the APP ITSELF through the production path — its own
   executable, which must pull up the BUNDLED engine via the app's
   makeChild — then stop the app and the engine, then relaunch the app
   so the engine is started a SECOND time (first launch + restart);
4. snapshot the bundle again and re-run the strict verification AFTER;
5. fail closed on: any bundle content change (added bytecode is called
   out explicitly but ANY mutation fails), either strict verification
   failing, missing REAL app-launch evidence (live socket + engine
   process attributable to the launched app + engine launch log), or an
   engine that cannot be stopped between rounds.

Honesty constraints (what this check must NEVER do to look green):

- it never injects a bytecode-disabling value to mask a defect. The
  child environment is deliberately given the ADVERSARIAL value
  ``PYTHONDONTWRITEBYTECODE=0`` — on the bundled runtime that value
  still ENABLES bytecode writing, so only the app's own
  ``makeChild`` forcing can keep the bundle sealed;
- it never runs the engine directly with a Python interpreter: only
  the app executable is launched (the production operator path);
- it never (re)signs anything;
- all runtime data, engine logs and evidence files live OUTSIDE the app
  bundle (the data dir defaults to ``<evidence-dir>/runtime-data``);
- dev-override variables (``ORX_ANALYTICS_ENGINE_PYTHON`` etc.) are
  stripped from the child environment so the check always exercises the
  BUNDLED runtime, never a developer checkout redirect.

Usage::

    python3 scripts/check_runtime_seal.py --app <App.app> --evidence-dir <DIR>

Exit codes: 0 = sealed (pass), 1 = seal broken / evidence missing,
2 = usage error. A machine-readable ``seal-check-report.json`` plus all
logs are written into the evidence dir.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import socket
import subprocess
import sys
import time

# The dev-override env vars that would redirect the app away from the
# bundled runtime; a seal check that exercised a checkout Python would
# prove nothing about the shipped bundle.
DEV_OVERRIDE_VARS = (
    "ORX_ANALYTICS_ENGINE_PYTHON",
    "ORX_ANALYTICS_ENGINE_SERVE",
    "ORX_ANALYTICS_ENGINE_PYTHONPATH",
)

# Adversarial operator condition. On the bundled CPython 3.12.8 this
# value still writes bytecode (empirically: absent / "0" / "" all write;
# only "1" does not), so it can never mask a missing fix — it makes the
# check reproduce the acceptance condition "父环境设置为 0".
HOSTILE_BYTECODE_VALUE = "0"

STOP_GRACE_SECONDS = 10.0
PS_COLUMNS = "pid=,ppid=,command="


class SealCheckFailure(Exception):
    """A verdict-relevant failure with a machine-readable code."""

    def __init__(self, code: str, detail: str,
                 evidence: dict | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.evidence = evidence


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_snapshot(app: Path) -> dict:
    """The COMPLETE bundle content: every file (size + sha256), symlink
    (target) and directory (presence), keyed by bundle-relative POSIX
    path. Codesign seals resources; our snapshot is at least as strict —
    ANY added, removed or changed entry fails the check."""
    entries: dict[str, dict] = {}
    for current, dirnames, filenames in os.walk(app):
        dirnames.sort()
        # Never descend into stray metadata the Finder may attach; the
        # walk itself must not follow symlinked directories (codesign
        # does not follow them either).
        dirnames[:] = sorted(
            d for d in dirnames
            if not (Path(current) / d).is_symlink()
        )
        for name in sorted(dirnames + filenames):
            path = Path(current) / name
            rel = path.relative_to(app).as_posix()
            if path.is_symlink():
                entries[rel] = {"type": "symlink",
                                "target": os.readlink(path)}
            elif path.is_dir():
                entries[rel] = {"type": "dir"}
            else:
                entries[rel] = {"type": "file",
                                "size": path.stat().st_size,
                                "sha256": sha256_of(path)}
    return entries


def snapshot_diff(before: dict, after: dict) -> dict:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(
        rel for rel in set(before) & set(after)
        if before[rel] != after[rel]
    )
    bytecode = sorted(
        rel for rel in added
        if "/__pycache__/" in f"/{rel}" or rel.endswith(".pyc")
    )
    return {"added": added, "removed": removed, "changed": changed,
            "added_bytecode": bytecode}


def codesign_verify(app: Path, log_path: Path) -> dict:
    # codesign is flag-driven: `--verify` (never a bare `verify`
    # subcommand — that would be parsed as the target path).
    result = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2",
         str(app)],
        capture_output=True, text=True, timeout=120)
    output = (result.stdout + result.stderr).strip()
    log_path.write_text(output + "\n", encoding="utf-8")
    return {"exit_code": result.returncode,
            "log": log_path.name,
            "first_line": output.splitlines()[0] if output else ""}


def ps_entries() -> list[dict]:
    result = subprocess.run(["ps", "-axo", PS_COLUMNS],
                            capture_output=True, text=True, timeout=30)
    entries = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            entries.append({"pid": int(parts[0]), "ppid": int(parts[1]),
                            "command": parts[2]})
    return entries


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def socket_connects(path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1.0)
            probe.connect(str(path))
        return True
    except OSError:
        return False


def stop_app(process: "subprocess.Popen[bytes]", what: str) -> None:
    """Stop the app the checker launched itself. Unlike ``terminate``
    (for non-child processes found via ps), the app is OUR child: after
    the signal it must be reaped with waitpid, or it lingers as a
    zombie that still answers signal 0 and would falsely fail the check
    as "survived SIGKILL"."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=STOP_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    process.kill()
    try:
        process.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        raise SealCheckFailure(
            "engine-cleanup-failed",
            f"{what} pid {process.pid} survived SIGKILL; cannot guarantee "
            f"a clean second round")


def terminate(pid: int, what: str) -> None:
    """TERM, wait, KILL, wait — for processes that are NOT our children
    (the engine, found via ps; its exit is reaped by its own parent).
    A process that survives SIGKILL fails the check (cleanup is part of
    repeatability)."""
    if not process_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    while time.monotonic() < deadline and process_alive(pid):
        time.sleep(0.1)
    if process_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while time.monotonic() < deadline and process_alive(pid):
            time.sleep(0.1)
    if process_alive(pid):
        raise SealCheckFailure(
            "engine-cleanup-failed",
            f"{what} pid {pid} survived SIGKILL; cannot guarantee a clean "
            f"second round")


def find_bundle_processes(app: Path) -> list[dict]:
    """Processes whose command line references this bundle — the only
    processes the check is ever allowed to signal. The checker itself is
    excluded: its own command line carries --app <path>."""
    needle = str(app)
    mine = os.getpid()
    return [entry for entry in ps_entries()
            if entry["pid"] != mine and needle in entry["command"]]


def read_bundle_executable(app: Path) -> str:
    plist = app / "Contents" / "Info.plist"
    if not plist.is_file():
        raise SealCheckFailure("not-a-bundle",
                               f"{app} has no Contents/Info.plist")
    try:
        with open(plist, "rb") as handle:
            info = plistlib.load(handle)
    except Exception as error:  # noqa: BLE001 - report, never crash
        raise SealCheckFailure("not-a-bundle",
                               f"cannot parse {plist}: {error}") from error
    return info.get("CFBundleExecutable", "ORXAnalyticsApp")


def is_inside(child: Path, parent: Path) -> bool:
    try:
        return child.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


def app_launch_environment(data_dir: Path) -> tuple[dict, list]:
    env = dict(os.environ)
    stripped = [var for var in DEV_OVERRIDE_VARS if env.pop(var, None)]
    env["ORX_ANALYTICS_HOME"] = str(data_dir)
    env["PYTHONDONTWRITEBYTECODE"] = HOSTILE_BYTECODE_VALUE
    return env, stripped


def launch_round(app: Path, executable: Path, data_dir: Path,
                 evidence_dir: Path, round_name: str,
                 launch_log: Path, timeout: float) -> dict:
    """Launch the app itself, wait for REAL engine-up evidence, then stop
    the app and the engine. Evidence is only complete when the socket
    accepts connections AND a live engine process is attributable to the
    launched app (its parent) AND the app's engine launch log exists —
    all under the app's data dir, outside the bundle."""
    env, stripped = app_launch_environment(data_dir)
    started = time.monotonic()
    try:
        with open(launch_log, "wb") as sink:
            process = subprocess.Popen(
                [str(executable)], env=env, stdout=sink,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True)
    except OSError as error:
        raise SealCheckFailure(
            "app-launch-failed",
            f"launching the app executable {executable} failed: {error}",
            evidence=None) from error
    evidence = {
        "round": round_name,
        "app_pid": process.pid,
        "app_executable": str(executable),
        "started_utc": utc_now(),
        "stripped_dev_overrides": stripped,
        "socket_path": str(data_dir / "engine.sock"),
        "socket_observed": False,
        "engine_process_observed": False,
        "engine": None,
        "engine_launch_log": None,
        "waited_seconds": None,
    }
    engine_entry = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not evidence["socket_observed"]:
            evidence["socket_observed"] = socket_connects(
                data_dir / "engine.sock")
        if not evidence["engine_process_observed"]:
            candidates = [
                entry for entry in ps_entries()
                if entry["ppid"] == process.pid and str(app) in entry["command"]
            ]
            if candidates:
                engine_entry = candidates[0]
                evidence["engine_process_observed"] = True
        if evidence["socket_observed"] and evidence["engine_process_observed"]:
            break
        time.sleep(0.25)
    evidence["waited_seconds"] = round(time.monotonic() - started, 1)
    if engine_entry is not None:
        evidence["engine"] = {
            "pid": engine_entry["pid"],
            "ppid": engine_entry["ppid"],
            "command": engine_entry["command"],
        }
    log_path = data_dir / "logs" / "engine-launch.log"
    if log_path.is_file():
        evidence["engine_launch_log"] = {
            "path": str(log_path),
            "size": log_path.stat().st_size,
        }

    missing = []
    if not evidence["socket_observed"]:
        missing.append(f"engine socket never accepted connections at "
                       f"{evidence['socket_path']}")
    if not evidence["engine_process_observed"]:
        missing.append("no engine process attributable to the launched app "
                       "(child of the app pid, command referencing the "
                       "bundle) was observed")
    if evidence["engine_launch_log"] is None:
        missing.append(f"the app's engine launch log {log_path} never "
                       f"appeared (makeChild writes it under the data dir)")

    # Stop the app first (an operator quits the app; the engine is by
    # design independent of the app's lifetime), then stop the engine.
    # The app goes through stop_app (our child — must be reaped, not
    # merely signalled); engine processes go through terminate.
    stop_app(process, f"{round_name} app")
    for entry in find_bundle_processes(app):
        terminate(entry["pid"], f"{round_name} engine")
    leftovers = find_bundle_processes(app)
    evidence["bundle_processes_after_stop"] = [e["pid"] for e in leftovers]
    if leftovers:
        raise SealCheckFailure(
            "engine-cleanup-failed",
            f"{round_name}: processes still reference the bundle after "
            f"stopping: {[e['command'] for e in leftovers]}")

    if missing:
        raise SealCheckFailure(
            "launch-evidence-missing",
            f"{round_name}: " + "; ".join(missing),
            evidence=evidence)
    return evidence


def run_check(app: Path, evidence_dir: Path, data_dir: Path,
              launch_timeout: float) -> tuple[dict, list]:
    """Drive the full check; returns (report, failures). Raises only for
    usage-class problems; verdict-relevant problems land in failures."""
    app = app.resolve()
    if not app.is_dir():
        raise SealCheckFailure("app-not-found", f"{app} is not a directory")
    # Location guards come first: a refused evidence/data location is
    # never touched, whatever state the bundle itself is in.
    if is_inside(evidence_dir, app):
        raise SealCheckFailure(
            "evidence-dir-inside-app",
            f"evidence dir {evidence_dir} must live outside the app bundle")
    if is_inside(data_dir, app):
        raise SealCheckFailure(
            "data-dir-inside-app",
            f"runtime data dir {data_dir} must live outside the app bundle "
            f"(runtime data and logs may never be written into the bundle)")
    executable = app / "Contents" / "MacOS" / read_bundle_executable(app)
    if not executable.is_file():
        raise SealCheckFailure(
            "not-a-bundle", f"{executable} does not exist (no app executable)")

    evidence_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "tool": "check_runtime_seal.py",
        "started_utc": utc_now(),
        "app": str(app),
        "app_executable": str(executable),
        "data_dir": str(data_dir),
        "evidence_dir": str(evidence_dir),
        "hostile_child_env": {"PYTHONDONTWRITEBYTECODE":
                              HOSTILE_BYTECODE_VALUE},
        "hostile_rationale": (
            "0 still enables bytecode writing on the bundled CPython "
            "3.12.8 (absent/0/empty all write; only 1 does not), so this "
            "adversarial value can mask nothing; only the app's makeChild "
            "forcing keeps the bundle sealed"),
        "rounds": [],
        "failures": [],
    }

    failures: list[dict] = []

    def record_failure(code: str, detail: str) -> None:
        failures.append({"code": code, "detail": detail})

    # 1. BEFORE: full content snapshot + strict verification.
    before = bundle_snapshot(app)
    (evidence_dir / "bundle-before.json").write_text(
        json.dumps(before, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    report["bundle_before"] = {"entries": len(before)}
    verify_before = codesign_verify(app, evidence_dir / "codesign-before.log")
    report["codesign_before"] = verify_before
    if verify_before["exit_code"] != 0:
        record_failure(
            "codesign-pre-failed",
            f"strict verification failed BEFORE launch: "
            f"{verify_before['first_line']} — the installed copy is already "
            f"unsealed; see {verify_before['log']}")
        report["failures"] = failures
        report["verdict"] = "fail"
        report["finished_utc"] = utc_now()
        return report, failures

    # 2. First launch via makeChild, then the engine restart round. A
    # failed round keeps its partial evidence for diagnosis and stops
    # the sequence (round 2 would prove nothing on top of a dead one).
    try:
        for index, name in enumerate(("first-launch", "engine-restart"), 1):
            report["rounds"].append(launch_round(
                app=app, executable=executable, data_dir=data_dir,
                evidence_dir=evidence_dir, round_name=name,
                launch_log=evidence_dir / f"app-launch-{index}.log",
                timeout=launch_timeout))
    except SealCheckFailure as failure:
        if failure.evidence is not None:
            report["rounds"].append(failure.evidence)
        record_failure(failure.code, failure.detail)

    # 3. AFTER: snapshot + strict verification again, then compare.
    after = bundle_snapshot(app)
    (evidence_dir / "bundle-after.json").write_text(
        json.dumps(after, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    report["bundle_after"] = {"entries": len(after)}
    diff = snapshot_diff(before, after)
    report["bundle_diff"] = diff
    if diff["added"] or diff["removed"] or diff["changed"]:
        if diff["added_bytecode"]:
            record_failure(
                "bundle-bytecode-added",
                f"the app launch wrote bytecode INTO the bundle: "
                f"{diff['added_bytecode'][:10]}"
                f"{' …' if len(diff['added_bytecode']) > 10 else ''} "
                f"(the runtime seal fix is not in effect)")
        else:
            record_failure(
                "bundle-mutated",
                f"bundle content changed across the app launches — "
                f"added={diff['added'][:10]} removed={diff['removed'][:10]} "
                f"changed={diff['changed'][:10]}")
    verify_after = codesign_verify(app, evidence_dir / "codesign-after.log")
    report["codesign_after"] = verify_after
    if verify_after["exit_code"] != 0:
        record_failure(
            "codesign-post-failed",
            f"strict verification failed AFTER the app launches: "
            f"{verify_after['first_line']} — the running app broke its own "
            f"seal; see {verify_after['log']}")

    report["failures"] = failures
    report["verdict"] = "pass" if not failures else "fail"
    report["finished_utc"] = utc_now()
    return report, failures


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_runtime_seal.py",
        description="Verify an installed ORX Analytics app keeps its code "
                    "seal across real first-launch and engine-restart "
                    "rounds (G012 runtime seal fix).")
    parser.add_argument("--app", required=True,
                        help="path to the installed .app bundle to check")
    parser.add_argument("--evidence-dir", required=True,
                        help="directory for all evidence files (must be "
                             "outside the app bundle)")
    parser.add_argument("--data-dir", default=None,
                        help="engine data dir for the check (default: "
                             "<evidence-dir>/runtime-data; must be outside "
                             "the app bundle)")
    parser.add_argument("--launch-timeout", type=float, default=90.0,
                        help="seconds to wait per round for engine-up "
                             "evidence (default: 90)")
    args = parser.parse_args(argv)

    app = Path(args.app)
    evidence_dir = Path(args.evidence_dir)
    data_dir = Path(args.data_dir) if args.data_dir \
        else evidence_dir / "runtime-data"
    if shutil.which("codesign") is None:
        print(json.dumps({"refused": True, "code": "codesign-missing",
                          "detail": "codesign is required for the seal check"},
                         indent=2))
        return 2
    try:
        report, failures = run_check(app, evidence_dir, data_dir,
                                     args.launch_timeout)
        report_path = evidence_dir / "seal-check-report.json"
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True)
            + "\n", encoding="utf-8")
    except SealCheckFailure as failure:
        report = {"tool": "check_runtime_seal.py", "verdict": "fail",
                  "failures": [{"code": failure.code,
                                "detail": failure.detail}]}
        failures = report["failures"]
        # A refused location is never written to — not even the report.
        report_path = None
        if not is_inside(evidence_dir, app):
            evidence_dir.mkdir(parents=True, exist_ok=True)
            report_path = evidence_dir / "seal-check-report.json"
            report_path.write_text(
                json.dumps(report, indent=2, ensure_ascii=False,
                           sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    if failures:
        print(f"FAIL: runtime seal check failed ({len(failures)} failure(s)); "
              f"report: {report_path}", file=sys.stderr)
        return 1
    print(f"PASS: app kept its seal across first launch and engine restart; "
          f"report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
