"""Exercise the two CLI-only and App-only upgrade paths on real artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import joint_release as release


def probe_pair(work: Path, archive: Path, wheel: Path) -> dict:
    work.mkdir()
    log = release.StageLog(work, "compatibility")
    app = release.extract_app_archive(archive, work / "app")
    env = release.isolated_env(work / "cli-home")
    venv = work / "venv"
    subprocess.run(["uv", "venv", "--python", sys.executable, str(venv)],
                   env=env, cwd=work, check=True, capture_output=True, text=True)
    subprocess.run(
        ["uv", "pip", "install", "--no-index", "--no-deps",
         "--python", str(venv / "bin" / "python"), str(wheel)],
        env=env, cwd=work, check=True, capture_output=True, text=True)
    cli = venv / "bin" / "orx-analytics"
    version = subprocess.check_output([str(cli), "--version"], env=env,
                                      cwd=work, text=True).strip()
    handle = release.launch_bundle_engine(app, work / "data", log)

    def watch(action: str) -> dict:
        result = subprocess.run(
            [str(cli), "--data-dir", str(handle["data_dir"]), "watch", action],
            env=env, cwd=work, check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    try:
        pid = handle["proc"].pid
        started = watch("start")
        if not (started.get("started") and not started["engine_spawned"]
                and started["engine"]["pid"] == pid):
            raise RuntimeError(f"CLI {version} did not reuse the App engine")
        client = release.EngineClient(handle["socket"])
        try:
            rejected = client.request({"op": "overview", "protocol_version": 999})
            if rejected.get("error", {}).get("code") != "unsupported_protocol":
                raise RuntimeError("Engine accepted an unsupported protocol")
            healthy = client.request({"op": "health", "protocol_version": 1})
        finally:
            client.close()
        if not healthy["ok"] or healthy["engine"]["pid"] != pid:
            raise RuntimeError("Protocol rejection disturbed engine ownership")
        stopped = watch("stop")
        if not stopped.get("stopped") or stopped["engine"]["pid"] != pid:
            raise RuntimeError("CLI stop did not release its own demand")
        after_stop = watch("status")
        if after_stop["engine"]["pid"] != pid or not after_stop["engine"]["running"]:
            raise RuntimeError("CLI stop changed the live App engine")
        return {
            "analytics_cli": version,
            "app_engine": handle["bundled_version"],
            "archive_sha256": release.sha256_file(archive),
            "wheel_sha256": release.sha256_file(wheel),
            "protocol": healthy["protocol_version"],
            "shared_engine_confirmed": True,
            "protocol_mismatch": rejected["error"],
            "engine_survived_cli_stop": True,
        }
    finally:
        try:
            if handle["proc"].poll() is None:
                watch("stop")
        finally:
            release.stop_bundle_engine(handle)
            log.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("legacy-archive", "legacy-wheel", "current-archive", "current-wheel"):
        parser.add_argument(f"--{name}", type=lambda value: Path(value).resolve(),
                            required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="orx-pair-") as tmp:
        work = Path(tmp).resolve()
        report = {
            "app_only_upgraded": probe_pair(
                work / "app-only", args.current_archive, args.legacy_wheel),
            "cli_only_upgraded": probe_pair(
                work / "cli-only", args.legacy_archive, args.current_wheel),
        }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
