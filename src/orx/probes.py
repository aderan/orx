"""Shared harness probes and capability snapshots (M1 P2, catalog extended G008).

doctor consumes this module; `orx agent probe` builds snapshots from it.
Probes are local-only: help text, version, login status, and model catalogs.
A probe NEVER launches a completion.

Snapshot shape (M1 P2 frozen core, G008 T002 additive catalog keys):

    {
      "harness": "codex",
      "binary": "/opt/homebrew/bin/codex",
      "version": "codex-cli 0.160.0",
      "probed_at": "2026-10-03T...Z",
      "features": {"headless": true, "json_output": true,
                    "model_selection": true, "effort_selection": true,
                    "resume": false},
      "auth": "logged_in | not_logged_in | unknown",
      "models_discoverable": true,
      "model_catalog": {...} | null,     # G008: structured catalog (see below)
      "catalog_error": "..." | null      # G008: why discovery last failed
    }

G008 catalog semantics (src/orx/model_catalog.py does the parsing):

    * "model_catalog" is present only for a genuinely fresh, successful
      discovery — parsed from the local models probe output
      (cursor: `agent --list-models`; codex: `codex debug models`),
      stamped with source/generated_at and a per-harness "generation"
      that increments on every successful refresh.
    * "catalog_error" records why discovery failed (binary missing,
      non-zero exit, timeout, unparseable output). A failed refresh
      never relabels an old catalog as a fresh success: the snapshot
      either carries a new catalog or none plus the error.
    * A valid empty catalog (exit 0, parseable, zero models) is a
      success: models_discoverable true, catalog with "models": [] —
      distinguishable from any failure.
    * Snapshots written before G008 (no catalog keys) stay readable.
    * Auth is orthogonal to the catalog: the models probe output is
      parsed whatever `auth` reports, and auth never fabricates a catalog.
    * Persisting a snapshot invalidates the same-process catalog cache
      (orx.model_catalog), so a successful refresh retires stale reads.

Snapshots persist at <user data dir>/probes/<harness>.json.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from orx import model_catalog
from orx.config import user_data_dir

# The models probe reads the full local catalog (the real codex dump is
# >256 KiB JSON; probe_argv reads it without any stream cap). The timeout
# bounds the process runtime, not the output size.
MODELS_PROBE_TIMEOUT = 30.0


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
    # How to parse the models probe output: "cursor_text" (agent
    # --list-models lines) or "codex_json" (codex debug models JSON).
    # None when there is no models probe.
    catalog_format: str | None = None
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
        catalog_format="codex_json",
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
        catalog_format="cursor_text",
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
    (there is nothing a probe would mean).

    The model catalog is discovered from the local models probe only
    (see module docstring for the failure/empty/refresh semantics): a
    parseable exit-0 output becomes a generation-stamped catalog; binary
    missing, non-zero exit, timeout, or unparseable output becomes a
    catalog_error and no catalog — never a stale catalog relabeled fresh.
    """
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
        "model_catalog": None,
        "catalog_error": None,
    }
    # The generation the next successful catalog refresh will carry,
    # read from the persisted snapshot before anything overwrites it.
    previous = load_snapshot(spec.harness)
    if binary_path is None:
        snapshot["catalog_error"] = f"binary {spec.binary!r} not found on PATH"
        if persist:
            _persist(snapshot)
            model_catalog.invalidate(spec.harness)
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
            code_m, models_text = probe_argv(
                list(spec.models_argv), timeout=MODELS_PROBE_TIMEOUT
            )
            if code_m is None:
                snapshot["catalog_error"] = (
                    f"models probe timed out after {MODELS_PROBE_TIMEOUT:g}s"
                    " or could not be launched"
                )
            elif code_m != 0:
                snapshot["catalog_error"] = f"models probe exited {code_m}"
            elif not models_text.strip():
                snapshot["catalog_error"] = "models probe returned empty output"
            else:
                try:
                    entries = _parse_catalog(spec, models_text)
                except model_catalog.CatalogDiscoveryError as exc:
                    snapshot["catalog_error"] = str(exc)
                else:
                    parameterized = None
                    if spec.catalog_format == "cursor_text":
                        # Bracket parameterized-syntax evidence is read
                        # from the help text and the list output already
                        # fetched — no additional probe invocation.
                        parameterized = model_catalog.parse_parameterized_syntax(
                            {
                                " ".join(spec.help_argv): help_text,
                                " ".join(spec.models_argv): models_text,
                            }
                        )
                    snapshot["model_catalog"] = model_catalog.build_catalog(
                        harness=spec.harness,
                        source=" ".join(spec.models_argv),
                        models=entries,
                        generation=model_catalog.next_generation(previous),
                        parameterized=parameterized,
                        generated_at=snapshot["probed_at"],
                    ).to_dict()
                    snapshot["models_discoverable"] = True
                    # Codex exposes effort as a -c config key
                    # (model_reasoning_effort) that never appears in
                    # --help; the catalog's own effort levels are the
                    # real signal.
                    if any(entry.efforts or entry.effort for entry in entries):
                        snapshot["features"]["effort_selection"] = True

    if persist:
        _persist(snapshot)
        # A persisted refresh retires any same-process cached catalog;
        # cached_catalog() would also notice via the generation check.
        model_catalog.invalidate(spec.harness)
    return snapshot


def _parse_catalog(spec: HarnessSpec, models_text: str) -> list[model_catalog.CatalogEntry]:
    """Parse models probe output with the harness's catalog format."""
    if spec.catalog_format == "codex_json":
        return model_catalog.parse_codex_models_json(models_text)
    return model_catalog.parse_cursor_list_models(models_text)


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
