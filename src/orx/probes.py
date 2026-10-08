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

import hashlib
import json
import os
import shutil
import subprocess
import tomllib
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


# ---------------------------------------------------------------------------
# G009 T007: execution-capability preflight and permission fingerprint
#
# The 2026-10-07 incident: controller-side PATH/denylist checks and help
# probes could not see the CLI agents' INTERNAL execution permissions. The
# real config had approvalMode=allowlist with shell allowlist Shell(ls)
# only, sandbox disabled, argv --trust but no --force — `ls` ran while
# uv/orx/pytest were denied inside the agent. --trust (workspace trust),
# --force/--yolo, approval mode, sandbox, and project hooks are SEPARATE
# axes; none implies any other.
#
# This preflight reads the harness's REAL current config (read-only; the
# same files the CLI itself reads) and fingerprints the effective axes.
# It states exactly what it can prove — the CLI identity and the axis
# values with their sources — and explicitly what it can never prove:
# internal shell execution. It never writes the harness config, never
# launches a completion, and never clears quota cooldowns, quota
# exhaustions, or manual overrides (probe success is not quota
# sufficiency; T005 semantics).

PERMISSION_AXES: tuple[str, ...] = (
    "trust",          # workspace trust (--trust for cursor; per-project config for codex)
    "force_yolo",     # --force/--yolo (cursor) / the never-passed bypass flag (codex)
    "approval_mode",  # the harness's own approval policy
    "sandbox",        # sandbox mode (argv for codex; config for cursor)
    "project_hooks",  # harness-level hooks that intercept agent actions
)

PREFLIGHT_PROVES = (
    "the harness CLI identity and version, and the effective permission "
    "axes above, each with the source it was read from"
)
PREFLIGHT_CANNOT_PROVE = (
    "internal shell execution inside the agent session: controller-side "
    "PATH lookups, denylist checks, and help output are not in-agent "
    "approval. This preflight also never clears quota cooldowns, quota "
    "exhaustions, or manual overrides (probe success is not quota "
    "sufficiency)"
)
PREFLIGHT_INVALIDATED_BY = (
    "any bound axis value or source change, a config file mtime change, "
    "a CLI binary/version change, a profile/model/effort change, a "
    "workspace change, or a toolchain change"
)


def cursor_config_path() -> Path:
    """The config file the `agent` CLI itself reads (located read-only;
    never written by ORX)."""
    return Path.home() / ".cursor" / "cli-config.json"


def codex_home_dir() -> Path:
    """CODEX_HOME > ~/.codex — the same discovery orx.quota uses."""
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def _unknown_axis(source: str) -> dict:
    return {"value": "unknown", "source": source}


def _cursor_config_axes(config_path: Path) -> tuple[dict, dict, list[dict]]:
    """(axes, axes_payload_for_digest, source_meta) from the real cursor
    CLI config. Only the permission-relevant keys are ever read:
    authInfo and the account caches are not opened at all, so no account
    identifier can reach a record. A missing file or key is ``unknown`` —
    never a default-allow and never a default-deny."""
    axes: dict[str, dict] = {}
    meta: dict = {"path": str(config_path)}
    try:
        raw = json.loads(config_path.read_text())
    except FileNotFoundError:
        meta["state"] = "absent"
        raw = None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        meta["state"] = "unparseable"
        raw = None
    else:
        meta["state"] = "read"
        meta["mtime"] = config_path.stat().st_mtime

    def finish() -> tuple[dict, dict, list[dict]]:
        payload = {
            "approval_mode": {
                key: axes["approval_mode"].get(key)
                for key in ("value", "shell_allow", "shell_deny")
            },
            "sandbox": axes["sandbox"].get("value"),
        }
        return axes, payload, [meta]

    if not isinstance(raw, dict):
        where = f"{meta['state']} config {config_path}"
        for name in ("approval_mode", "sandbox"):
            axes[name] = _unknown_axis(
                f"{where}: no readable value (unknown, not defaulted)")
        return finish()

    approval = raw.get("approvalMode")
    if isinstance(approval, str) and approval:
        entry: dict = {"value": approval,
                       "source": f"config {config_path} approvalMode"}
        permissions = raw.get("permissions")
        if isinstance(permissions, dict):
            allow = permissions.get("allow")
            deny = permissions.get("deny")
            entry["shell_allow"] = allow if isinstance(allow, list) else []
            entry["shell_deny"] = deny if isinstance(deny, list) else []
        else:
            entry["shell_allow"] = []
            entry["shell_deny"] = []
        axes["approval_mode"] = entry
    else:
        axes["approval_mode"] = _unknown_axis(
            f"config {config_path} present, approvalMode key absent "
            "(unknown, not defaulted)")

    sandbox = raw.get("sandbox")
    mode = sandbox.get("mode") if isinstance(sandbox, dict) else None
    if isinstance(mode, str) and mode:
        axes["sandbox"] = {
            "value": mode,
            "source": f"config {config_path} sandbox.mode "
                      "(no --sandbox argv override)",
        }
    else:
        axes["sandbox"] = _unknown_axis(
            f"config {config_path} present, sandbox.mode key absent "
            "(unknown, not defaulted)")
    return finish()


def _codex_config_axes(codex_home: Path,
                       workspace: Path | None) -> tuple[dict, dict, list[dict]]:
    """(axes, axes_payload_for_digest, source_meta) from the real codex
    config.toml plus hooks.json. The hooks file contributes event NAMES
    only — hook commands are never stored."""
    config_path = codex_home / "config.toml"
    hooks_path = codex_home / "hooks.json"
    axes: dict[str, dict] = {}
    meta: dict = {"path": str(config_path)}
    data = None
    try:
        with open(config_path, "rb") as fh:
            data = tomllib.load(fh)
        meta["state"] = "read"
        meta["mtime"] = config_path.stat().st_mtime
    except FileNotFoundError:
        meta["state"] = "absent"
    except (tomllib.TOMLDecodeError, OSError):
        meta["state"] = "unparseable"

    if not isinstance(data, dict):
        where = f"{meta['state']} codex config {config_path}"
        axes["approval_mode"] = _unknown_axis(
            f"{where}: no readable value (unknown, not defaulted)")
        axes["trust"] = _unknown_axis(
            f"{where}: workspace trust unreadable (unknown, not defaulted)")
    else:
        policy = data.get("approval_policy")
        if isinstance(policy, str) and policy:
            axes["approval_mode"] = {
                "value": policy,
                "source": f"config {config_path} approval_policy",
            }
        else:
            axes["approval_mode"] = _unknown_axis(
                f"config {config_path} present, approval_policy key absent "
                "(unknown, not defaulted)")
        trust = None
        if workspace is not None:
            projects = data.get("projects")
            entry = (projects.get(str(workspace))
                     if isinstance(projects, dict) else None)
            if isinstance(entry, dict):
                trust = entry.get("trust_level")
        if isinstance(trust, str) and trust:
            axes["trust"] = {
                "value": trust,
                "source": f"config {config_path} projects.{workspace} trust_level",
            }
        else:
            axes["trust"] = _unknown_axis(
                f"codex config {config_path} has no projects trust_level for "
                f"{workspace or '(no workspace)'} (unknown, not defaulted)")

    hooks_meta: dict = {"path": str(hooks_path)}
    hooks_value: object = {"configured": False, "events": []}
    hooks_source = f"{hooks_path} absent (no hooks configured)"
    try:
        raw_hooks = json.loads(hooks_path.read_text())
    except FileNotFoundError:
        hooks_meta["state"] = "absent"
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        hooks_meta["state"] = "unparseable"
        hooks_value = "unknown"
        hooks_source = f"{hooks_path} present but unparseable (unknown)"
    else:
        hooks_meta["state"] = "read"
        hooks_meta["mtime"] = hooks_path.stat().st_mtime
        events = raw_hooks.get("hooks") if isinstance(raw_hooks, dict) else None
        if isinstance(events, dict) and events:
            hooks_value = {"configured": True, "events": sorted(events)}
            hooks_source = (f"{hooks_path} hooks event names "
                            "(hook commands never stored)")
    axes["project_hooks"] = {"value": hooks_value, "source": hooks_source}

    payload = {
        "approval_mode": axes["approval_mode"].get("value"),
        "trust": axes["trust"].get("value"),
        "project_hooks": hooks_value,
    }
    return axes, payload, [meta, hooks_meta]


def _argv_axis_entries(harness: str, profile, role: str) -> dict:
    """The argv-owned axes via the adapter mirrors (the adapters are the
    source of truth; test_adapters pins the mirrors to real launch argv)."""
    from orx.adapters import codex as codex_module
    from orx.adapters import cursor as cursor_module

    entries: dict[str, dict] = {}
    if harness == "cursor":
        mirror = cursor_module.permission_argv_axes(profile)
        entries["trust"] = {
            "value": True,
            "source": "argv: the cursor adapter always passes --trust "
                      "(workspace trust axis, not an approval mode)",
        }
        if profile is None:
            entries["force_yolo"] = _unknown_axis(
                "argv: no profile selected — --force/--yolo is "
                "profile-driven (unknown, not defaulted)")
        else:
            entries["force_yolo"] = {
                "value": mirror["force_yolo"],
                "source": f"argv: --force/--yolo only when profile "
                          f"{profile.name} sets force = {mirror['force_yolo']}",
            }
    else:
        mirror = codex_module.permission_argv_axes(profile, role)
        entries["force_yolo"] = {
            "value": mirror["force_yolo"],
            "source": "argv: the codex adapter never passes "
                      "--dangerously-bypass-approvals-and-sandbox",
        }
        entries["sandbox"] = {
            "value": mirror["sandbox"],
            "source": f"argv: the codex adapter always passes "
                      f"-s {mirror['sandbox']} for role {role}",
        }
    return entries


def _toolchain_entries(check_commands) -> list[dict]:
    """First tokens of the task's prescribed check commands, looked up on
    the CONTROLLER's PATH. Explicitly framed as proof of nothing inside
    the agent — the incident's exact confusion."""
    entries = []
    for command in check_commands:
        token = command.strip().split()[0] if command.strip() else ""
        entries.append({
            "command": command,
            "first_token": token,
            "controller_path": shutil.which(token) if token else None,
            "note": "controller-side PATH lookup only; this is not "
                    "in-agent shell approval",
        })
    return entries


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")).hexdigest()


def _preflight_fingerprint(record: dict) -> str:
    """Identity hash over everything the result binds: CLI identity,
    profile/model/effort/role, the permission axes with their sources,
    the workspace, the toolchain tokens, and the per-source axes digests.
    The timestamp is deliberately outside the hash so an unchanged
    environment reproduces the fingerprint; `probed_at` rides alongside."""
    payload = {
        "harness": record["harness"],
        "cli": record["cli"],
        "profile": record["profile"],
        "model": record["model"],
        "effort": record["effort"],
        "role": record["role"],
        "workspace": record["workspace"],
        "axes": {
            name: {"value": entry.get("value"), "source": entry.get("source")}
            for name, entry in record["axes"].items()
        },
        "toolchain": [entry["first_token"] for entry in record["toolchain"]],
        "sources": [[source["path"], source.get("axes_sha256")]
                    for source in record["sources"]],
    }
    return "sha256:" + hashlib.sha256(
        _canonical_json(payload).encode("utf-8")).hexdigest()[:32]


def build_permission_preflight(
    harness: str,
    *,
    profile=None,
    role: str = "worker",
    workspace: Path | None = None,
    check_commands=(),
) -> dict:
    """Read the harness's REAL current permission config (read-only) and
    fingerprint the effective axes. Raises KeyError for unknown harness
    names; host-only/generic harnesses raise ValueError (nothing to
    preflight). The record states its provable scope explicitly and never
    claims internal shell execution, never stores secrets or account
    identifiers (only axis values, sources, and digests), and never
    touches quota/health state."""
    spec = HARNESSES[harness]
    if spec.host_only or spec.harness == "shell":
        raise ValueError(
            f"harness '{harness}' has no permission preflight ({spec.note}); "
            "preflights apply to codex and cursor"
        )
    binary_path = shutil.which(spec.binary)
    version = None
    if binary_path is not None:
        code, version_text = probe_argv(list(spec.version_argv), timeout=15)
        if code == 0 and version_text:
            version = version_text.splitlines()[0].strip()

    argv_axes = _argv_axis_entries(harness, profile, role)
    if harness == "cursor":
        config_axes, payload, metas = _cursor_config_axes(cursor_config_path())
        config_axes["project_hooks"] = _unknown_axis(
            "no cursor project-hooks config location known to this ORX "
            "version (unknown, not defaulted)")
        payload["project_hooks"] = "unknown"
        argv_owned = ("trust", "force_yolo")
    else:
        config_axes, payload, metas = _codex_config_axes(
            codex_home_dir(), workspace)
        argv_owned = ("force_yolo", "sandbox")

    axes: dict[str, dict] = {}
    for name in PERMISSION_AXES:
        axes[name] = argv_axes[name] if name in argv_owned else config_axes[name]

    for meta in metas:
        meta["axes_sha256"] = _digest(payload)
    record = {
        "kind": "execution_preflight",
        "harness": harness,
        "profile": profile.name if profile is not None else None,
        "model": profile.model if profile is not None else None,
        "effort": profile.effort.value if profile is not None else None,
        "role": role,
        "workspace": str(workspace.resolve()) if workspace is not None else None,
        "cli": {"binary": binary_path, "version": version},
        "axes": axes,
        "toolchain": _toolchain_entries(check_commands),
        "proves": PREFLIGHT_PROVES,
        "cannot_prove": PREFLIGHT_CANNOT_PROVE,
        "invalidated_by": PREFLIGHT_INVALIDATED_BY,
        "probed_at": _now(),
        "sources": metas,
    }
    record["fingerprint"] = _preflight_fingerprint(record)
    return record


def preflight_path(harness: str, profile_name: str | None) -> Path:
    """Where a (harness, profile) preflight persists, under the user data
    dir next to the capability snapshots."""
    slug = profile_name or "no-profile"
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in slug)
    return user_data_dir() / "probes" / "preflight" / f"{harness}-{safe}.json"


def load_preflight(harness: str, profile_name: str | None) -> dict | None:
    try:
        return json.loads(preflight_path(harness, profile_name).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def persist_preflight(record: dict) -> Path:
    path = preflight_path(record["harness"], record.get("profile"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n")
    return path


def preflight_invalid_reasons(stored: dict | None, current: dict) -> list[str]:
    """Why a stored preflight no longer binds. Empty list means the prior
    result is still valid; a missing prior result yields an empty list
    (there is nothing to invalidate — the caller reports absence itself).
    Invalidation fires on a fingerprint change (a bound axis value or
    source, the CLI identity, profile/model/effort, workspace, or
    toolchain changed) and on a config file mtime change even when the
    axes-relevant content hash is equal. Invalidation NEVER auto-clears
    anything: it only retires the prior preflight's claim."""
    if stored is None:
        return []
    reasons: list[str] = []
    if stored.get("fingerprint") != current.get("fingerprint"):
        reasons.append(
            "fingerprint differs: a bound axis/source, the CLI identity, "
            "the profile/model/effort, the workspace, or the toolchain "
            "changed")
    current_by_path = {s.get("path"): s for s in current.get("sources", [])}
    for source in stored.get("sources", []):
        match = current_by_path.get(source.get("path"))
        if match is not None and source.get("mtime") != match.get("mtime"):
            reasons.append(
                f"config mtime changed for {source.get('path')} "
                "(content hash equal; the touch still retires the prior "
                "result)")
    return reasons


def doctor_permission_checks() -> list[tuple[str, str, str]]:
    """(name, state, detail) triples for doctor: the harness permission
    axes as read from the real CLI config files. Visibility only — an
    absent or unparseable config is a warning (unknown, never defaulted,
    never a failure), and nothing here claims internal shell execution
    will succeed. Per-profile argv axes (trust/force) and fingerprints
    belong to `orx agent preflight`, not doctor."""
    from orx.doctor import OK, WARN

    out: list[tuple[str, str, str]] = []
    cursor_axes, _, cursor_metas = _cursor_config_axes(cursor_config_path())
    meta = cursor_metas[0]
    if meta["state"] == "read":
        approval = cursor_axes["approval_mode"]
        sandbox = cursor_axes["sandbox"]
        allow = ", ".join(str(item) for item in approval.get("shell_allow", [])) or "none"
        deny = ", ".join(str(item) for item in approval.get("shell_deny", [])) or "none"
        out.append((
            "permissions:cursor", OK,
            f"approval {approval['value']} (shell allow: {allow}; deny: {deny}); "
            f"sandbox {sandbox['value']}; trust/force are per-profile argv "
            "axes (`orx agent preflight cursor --profile NAME`); this "
            "proves nothing about in-agent shell execution",
        ))
    else:
        out.append((
            "permissions:cursor", WARN,
            f"{meta['state']} {cursor_config_path()}: approval/sandbox "
            "unknown (not defaulted); per-profile argv axes via "
            "`orx agent preflight cursor --profile NAME`",
        ))

    codex_axes, _, codex_metas = _codex_config_axes(codex_home_dir(), None)
    meta = codex_metas[0]
    config_path = codex_home_dir() / "config.toml"
    if meta["state"] == "read":
        approval = codex_axes["approval_mode"]["value"]
        hooks = codex_axes["project_hooks"]["value"]
        if isinstance(hooks, dict) and hooks.get("configured"):
            events = ", ".join(hooks.get("events", []))
            hooks_text = f"hooks configured ({events})"
        else:
            hooks_text = "no hooks configured"
        out.append((
            "permissions:codex", OK,
            f"approval {approval}; {hooks_text}; workspace trust and the "
            "-s sandbox axis are per-launch facts (`orx agent preflight "
            "codex --profile NAME`); this proves nothing about in-agent "
            "shell execution",
        ))
    else:
        out.append((
            "permissions:codex", WARN,
            f"{meta['state']} {config_path}: approval unknown (not "
            "defaulted); per-launch axes via `orx agent preflight codex "
            "--profile NAME`",
        ))
    return out
