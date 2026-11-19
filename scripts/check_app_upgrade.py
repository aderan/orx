"""Rehearse a real preview App and CLI replacement in disposable directories."""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

import joint_release as release


def cli_install(wheel: Path, destination: Path) -> Path:
    subprocess.run(["uv", "venv", "--python", sys.executable, str(destination)],
                   check=True, capture_output=True)
    subprocess.run(
        ["uv", "pip", "install", "--no-index", "--no-deps", "--python",
         str(destination / "bin/python"), str(wheel)], check=True, capture_output=True)
    return destination / "bin/orx-analytics"


def consistent_copy(source: Path, target: Path) -> None:
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live, \
            closing(sqlite3.connect(target)) as backup:
        live.backup(backup)
        if backup.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("SQLite backup failed integrity_check")


def query(path: Path, op: str, **fields) -> dict:
    client = release.EngineClient(path, timeout=2)
    try:
        answer = client.request({"op": op, "protocol_version": 1, **fields})
    finally:
        client.close()
    if not answer.get("ok"):
        raise RuntimeError(f"{op} failed: {answer}")
    return answer


def checked_bundle(archive: Path, destination: Path) -> tuple[Path, dict]:
    app = release.extract_app_archive(archive, destination)
    if release.signature_evidence(app)["codesign_verify_exit"] != 0:
        raise RuntimeError(f"archive {archive.name} has an invalid code signature")
    return app, release.gather_app_facts(app)


@contextmanager
def installed_app(app: Path, home: Path, data: Path, logfile: Path):
    """Launch the original signed executable. Own only its test engine child."""
    env = release.isolated_env(home)
    env["CFFIXED_USER_HOME"] = str(home)
    env["ORX_ANALYTICS_HOME"] = str(data)
    env["PATH"] = "/usr/bin:/bin"
    executable = app / "Contents/MacOS/ORXAnalyticsApp"
    socket = data / "engine.sock"
    if socket.exists():
        raise RuntimeError("a previous engine still holds the rehearsal directory")
    with logfile.open("wb") as log:
        process = subprocess.Popen(
            [str(executable), "-data.directory.override", str(data), "-notifications.enabled", "NO"],
            env=env, cwd=home, stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
    pid = None
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"App quit with code {process.returncode} before serving, see {logfile}")
            try:
                status = query(socket, "health")
            except OSError:
                time.sleep(0.2)
                continue
            pid = status["engine"]["pid"]
            parent = subprocess.check_output(["ps", "-p", str(pid), "-o", "ppid="],
                                             text=True).strip()
            if parent != str(process.pid):
                raise RuntimeError("engine is not the installed App's child")
            yield process, pid
            return
        raise RuntimeError(f"installed App did not start its engine, see {logfile}")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        if pid is None and (data / "engine.lock").is_file():
            pid = json.loads((data / "engine.lock").read_text()).get("pid")
        if pid is not None:
            running = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="],
                                     capture_output=True, text=True)
            if (running.returncode == 0 and str(app) in running.stdout
                    and str(data) in running.stdout):
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 10
                while socket.exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                if socket.exists():
                    raise RuntimeError("the prior engine has not released its socket")


@contextmanager
def bundled_engine(app: Path, home: Path, data: Path, logfile: Path):
    log = release.StageLog(logfile.parent, logfile.stem)
    try:
        handle = release.launch_bundle_engine(app, data, log)
        try:
            yield None, handle["proc"].pid
        finally:
            release.stop_bundle_engine(handle)
    finally:
        log.close()


def run_rehearsal(work: Path, old_archive: Path, old_wheel: Path,
                  new_archive: Path, new_wheel: Path, *, engine_only: bool) -> dict:
    if work.exists() and any(work.iterdir()):
        raise RuntimeError("the rehearsal needs an empty work directory")
    work.mkdir(parents=True, exist_ok=True)
    app_slot = work / "Applications"
    old_app, old_facts = checked_bundle(old_archive, app_slot)
    new_app, new_facts = checked_bundle(new_archive, work / "new-package")
    if old_facts["bundle_id"] != new_facts["bundle_id"]:
        raise RuntimeError("the migration changed the channel's Bundle ID")
    old_cli = cli_install(old_wheel, work / "old-cli")
    new_cli = cli_install(new_wheel, work / "new-cli")
    data = work / "custom-data"
    data.mkdir()
    db = data / "analytics.db"
    socket = data / "engine.sock"
    project = work / "Project A"
    fixture = release.REPO_ROOT / release.V030_FIXTURE_DIR / "state.db"
    fixture_before = release.sha256_file(fixture)
    (project / ".orx").mkdir(parents=True)
    source = project / ".orx/state.db"
    shutil.copyfile(fixture, source)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as connection:
        run_id, = connection.execute("SELECT id FROM runs ORDER BY id LIMIT 1").fetchone()

    def cli(executable: Path, *args: str) -> str:
        return subprocess.check_output(
            [str(executable), "--data-dir", str(data), *args], cwd=work,
            env=release.isolated_env(work / "cli-home"), text=True)

    def version(path: Path) -> str:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            return connection.execute(
                "SELECT value FROM meta WHERE key='store_version'").fetchone()[0]

    def history() -> dict:
        result = query(socket, "runs.get", project=str(project), run_id=run_id)
        if not result.get("found"):
            raise RuntimeError("the imported run is unavailable")
        usage = result["usage"]
        return {"tokens": usage["tokens"], "coverage": usage["coverage"]}

    launch = bundled_engine if engine_only else installed_app
    with launch(old_app, work / "old-home", data, work / "old-app.log"):
        cli(old_cli, "projects", "add", str(project), "--name", "Migration project")
        release.bundle_engine_refresh({"socket": socket}, db, str(project), 1)
        original_history = history()
        registry_before = cli(old_cli, "projects", "list")
        old_version = version(db)

    pre_upgrade = work / "pre-upgrade.db"
    consistent_copy(db, pre_upgrade)
    saved_old_app = work / old_app.name
    old_app.rename(saved_old_app)
    installed_new_app = app_slot / new_app.name
    new_app.rename(installed_new_app)

    with launch(installed_new_app, work / "new-home", data,
                       work / "new-app.log") as (app_process, upgraded_pid):
        if cli(new_cli, "projects", "list") != registry_before or history() != original_history:
            raise RuntimeError("registry or usage changed on upgrade")
        cli_version = cli(new_cli, "--version").strip()
        if cli_version != new_facts["cf_bundle_version_string"]:
            raise RuntimeError("the upgraded CLI and bundled engine versions differ")
        started = json.loads(cli(new_cli, "watch", "start"))
        try:
            if not started.get("started") or started.get("engine_spawned"):
                raise RuntimeError("watch did not share the installed App's engine")
            if app_process is not None:
                app_process.terminate()
                app_process.wait(timeout=10)
                after_quit = json.loads(cli(new_cli, "watch", "status"))
                if not after_quit["demand"]["held"] or after_quit["engine"]["pid"] != upgraded_pid:
                    raise RuntimeError("App exit terminated the CLI watch engine")
        finally:
            cli(new_cli, "watch", "stop")
        new_version = version(db)
        app_backup_sha256 = None
        if not engine_only:
            app_backups = list(data.glob("backups/analytics-pre-*.db"))
            if len(app_backups) != 1 or version(app_backups[0]) != old_version:
                raise RuntimeError("the App did not leave a restorable pre-upgrade database")
            with closing(sqlite3.connect(app_backups[0].as_uri() + "?mode=ro", uri=True)) as backup:
                if backup.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise RuntimeError("the App backup failed integrity_check")
            app_backup_sha256 = release.sha256_file(app_backups[0])

    if release.signature_evidence(installed_new_app)["codesign_verify_exit"] != 0:
        raise RuntimeError("the App's signature did not survive real launch")
    upgraded = work / "after-upgrade.db"
    consistent_copy(db, upgraded)
    if any(data.glob("analytics.db-*")):
        raise RuntimeError("refusing to restore while database sidecars remain")
    restore_staged = data / "analytics.restore.db"
    shutil.copyfile(pre_upgrade, restore_staged)
    restore_staged.replace(db)
    installed_new_app.rename(work / installed_new_app.name)
    saved_old_app.rename(old_app)

    with launch(old_app, work / "old-home", data, work / "rollback-app.log"):
        restored_version = version(db)
        if (cli(old_cli, "projects", "list") != registry_before
                or history() != original_history or restored_version != old_version):
            raise RuntimeError("old App and CLI did not recover the original data")
    if release.sha256_file(fixture) != fixture_before or release.sha256_file(source) != fixture_before:
        raise RuntimeError("the rehearsed collections changed the ORX source")

    return {
        "mode": "bundled-engine-only" if engine_only else "installed-app",
        "old_app": old_facts["cf_bundle_version_string"],
        "new_app": new_facts["cf_bundle_version_string"],
        "bundle_id": new_facts["bundle_id"],
        "old_app_archive_sha256": release.sha256_file(old_archive),
        "new_app_archive_sha256": release.sha256_file(new_archive),
        "old_cli_wheel_sha256": release.sha256_file(old_wheel),
        "new_cli_wheel_sha256": release.sha256_file(new_wheel),
        "analytics_store_versions": [old_version, new_version, restored_version],
        "sqlite_backup_sha256": release.sha256_file(pre_upgrade),
        "app_backup_sha256": app_backup_sha256,
        "upgraded_db_preserved_sha256": release.sha256_file(upgraded),
        "history": original_history,
        "registry_preserved": True,
        "orx_source_unchanged": True,
        "app_exit_preserved_cli_watch": None if engine_only else True,
        "old_cli_and_bundled_engine_restored": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("work-dir", "old-archive", "old-wheel", "new-archive", "new-wheel"):
        parser.add_argument(f"--{name}", required=True, type=lambda value: Path(value).resolve())
    parser.add_argument("--engine-only", action="store_true",
                        help="exercise data with the archived engines; omit App process claims")
    args = parser.parse_args()
    result = run_rehearsal(args.work_dir, args.old_archive, args.old_wheel,
                           args.new_archive, args.new_wheel, engine_only=args.engine_only)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
