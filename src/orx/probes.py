"""Shared harness probes and capability snapshots (M1 P2).

doctor consumes this module; `orx agent probe` builds snapshots from it.
Probes are local-only: help text, version, login status, and model catalogs.
A probe NEVER launches a completion.

Snapshot shape (frozen by docs/m1-plan.md):

    {
      "harness": "codex",
      "binary": "/opt/homebrew/bin/codex",
      "version": "codex-cli 0.160.0",
      "probed_at": "2026-10-03T...Z",
      "features": {"headless": true, "json_output": true,
                    "model_selection": true, "effort_selection": true,
                    "resume": false},
      "auth": "logged_in | not_logged_in | unknown",
      "models_discoverable": true
    }

Snapshots persist at <user data dir>/probes/<harness>.json.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from orx.config import user_data_dir


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def probe_argv(argv: list[str], timeout: float = 10.0) -> tuple[int | None, str]:
    """Run a local CLI probe. Returns (exit_code, combined output)."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout + "\n" + proc.stderr).strip()
    except (subprocess.TimeoutExpired, OSError):
        return None, ""


@dataclass(frozen=True)
class HarnessSpec:
    harness: str
    binary: str
    required_flags: tuple[str, ...]
    help_argv: tuple[str, ...]
    status_argv: tuple[str, ...]
    logged_in_marker: str
    version_argv: tuple[str, ...]
    models_argv: tuple[str, ...] | None  # None -> not discoverable locally
    host_only: bool = False
    note: str = ""


HARNESSES: dict[str, HarnessSpec] = {
    "codex": HarnessSpec(
        harness="codex",
        binary="codex",
        required_flags=("--json", "-m", "-C", "--output-last-message"),
        help_argv=("codex", "exec", "--help"),
        status_argv=("codex", "login", "status"),
        logged_in_marker="logged in",
        version_argv=("codex", "--version"),
        models_argv=("codex", "debug", "models"),
    ),
    "cursor": HarnessSpec(
        harness="cursor",
        binary="agent",
        required_flags=("--print", "--output-format", "--workspace", "--trust", "--model"),
        help_argv=("agent", "--help"),
        status_argv=("agent", "status"),
        logged_in_marker="logged in",
        version_argv=("agent", "--version"),
        models_argv=("agent", "--list-models"),
    ),
    "shell": HarnessSpec(
        harness="shell",
        binary="",
        required_flags=(),
        help_argv=(),
        status_argv=(),
        logged_in_marker="",
        version_argv=(),
        models_argv=None,
        note="generic local executables; nothing to probe",
    ),
    "zcode": HarnessSpec(
        harness="zcode",
        binary="",
        required_flags=(),
        help_argv=(),
        status_argv=(),
        logged_in_marker="",
        version_argv=(),
        models_argv=None,
        host_only=True,
        note="host harness: work is done by the host, no CLI to probe",
    ),
}


def _auth_from(status_text: str, marker: str) -> str:
    text = status_text.lower()
    # Negation must win: real CLIs print "Not logged in", which contains the
    # positive marker as a substring (latent M0 doctor bug, found by the P2
    # fake-driven tests).
    if not text:
        return "unknown"
    if "not logged in" in text or "logged out" in text:
        return "not_logged_in"
    if marker.lower() in text:
        return "logged_in"
    return "not_logged_in"


def capability_snapshot(harness: str, persist: bool = True) -> dict:
    """Probe one harness and build its capability snapshot. Raises KeyError
    for unknown harness names; host-only/generic harnesses raise ValueError
    (there is nothing a probe would mean)."""
    spec = HARNESSES[harness]
    if spec.host_only or spec.harness == "shell":
        raise ValueError(
            f"harness '{harness}' has no probe ({spec.note}); "
            "probes apply to codex and cursor"
        )
    binary_path = shutil.which(spec.binary)
    snapshot = {
        "harness": spec.harness,
        "binary": binary_path,
        "version": None,
        "probed_at": _now(),
        "features": {
            "headless": False,
            "json_output": False,
            "model_selection": False,
            "effort_selection": False,
            "resume": False,
        },
        "auth": "unknown",
        "models_discoverable": False,
    }
    if binary_path is None:
        if persist:
            _persist(snapshot)
        return snapshot

    code, version_text = probe_argv(list(spec.version_argv), timeout=15)
    if code == 0 and version_text:
        snapshot["version"] = version_text.splitlines()[0].strip()

    code, help_text = probe_argv(list(spec.help_argv))
    if code is not None:
        flags = [f for f in spec.required_flags if f in help_text]
        snapshot["features"]["headless"] = len(flags) == len(spec.required_flags)
        snapshot["features"]["json_output"] = "--json" in help_text or "--output-format" in help_text
        snapshot["features"]["model_selection"] = "-m" in flags or "--model" in flags
        snapshot["features"]["effort_selection"] = (
            "model_reasoning_effort" in help_text or "effort=" in help_text
        )
        snapshot["features"]["resume"] = "resume" in help_text.lower()

        if snapshot["features"]["headless"]:
            code_s, status_text = probe_argv(list(spec.status_argv))
            snapshot["auth"] = _auth_from(status_text, spec.logged_in_marker)

        if spec.models_argv is not None:
            code_m, models_text = probe_argv(list(spec.models_argv), timeout=30)
            if code_m == 0 and models_text.strip():
                snapshot["models_discoverable"] = True
                # Codex exposes effort as a -c config key (model_reasoning_effort)
                # that never appears in --help; the local catalog's
                # supported_reasoning_levels entries are the real signal.
                if '"effort"' in models_text or "effort=" in help_text:
                    snapshot["features"]["effort_selection"] = True

    if persist:
        _persist(snapshot)
    return snapshot


def snapshot_path(harness: str) -> Path:
    return user_data_dir() / "probes" / f"{harness}.json"


def load_snapshot(harness: str) -> dict | None:
    path = snapshot_path(harness)
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _persist(snapshot: dict) -> Path:
    path = snapshot_path(snapshot["harness"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2) + "\n")
    return path


# ---------------------------------------------------------------------------
# doctor adapter: same Check outputs as the pre-P2 inline logic


def doctor_harness_checks() -> list[tuple[str, str, str]]:
    """(name, state, detail) triples for doctor's per-harness checks,
    byte-compatible with the M0 implementation."""
    from orx.doctor import OK, WARN

    out: list[tuple[str, str, str]] = []
    for harness in ("codex", "cursor"):
        spec = HARNESSES[harness]
        path = shutil.which(spec.binary)
        if not path:
            out.append((spec.binary, WARN, "not found on PATH (optional harness)"))
            continue
        code, help_text = probe_argv(list(spec.help_argv))
        missing = [f for f in spec.required_flags if f not in help_text]
        if code is None:
            out.append((spec.binary, WARN,
                        "found; help probe timed out; operational probe not_run"))
        elif missing:
            out.append((spec.binary, WARN,
                        f"found at {path}; help is missing {missing}; "
                        "adapter would refuse (capability_mismatch)"))
        else:
            code_s, status_text = probe_argv(list(spec.status_argv))
            auth = _auth_from(status_text, spec.logged_in_marker)
            out.append((spec.binary, OK,
                        f"found at {path}; flags present; auth {auth}; "
                        "operational probe not_run"))
    return out
