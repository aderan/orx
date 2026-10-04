"""Doctor: environment and project checks. No paid model calls, no agent
sessions. A binary being found is never reported as "works"."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from orx import __version__
from orx.config import load_config, load_effective, load_profiles, validate_references
from orx.records import ConfigError
from orx.state import CODE_SCHEMA_VERSION

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    state: str
    detail: str = ""


def run_doctor(root: Path | None) -> dict:
    checks: list[Check] = []

    if sys.version_info >= (3, 12):
        checks.append(Check("python", OK, f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"))
    else:
        checks.append(Check("python", FAIL, f"3.12 required, running {sys.version_info.major}.{sys.version_info.minor}"))

    checks.append(Check("uv", OK if shutil.which("uv") else WARN,
                        "on PATH" if shutil.which("uv") else "not on PATH (required for `orx update`)"))
    checks.append(Check("git", OK if shutil.which("git") else WARN,
                        "on PATH" if shutil.which("git") else "not on PATH"))

    inside_repo = any(
        (p / ".git").exists() for p in ((root or Path.cwd()), *(root or Path.cwd()).parents)
    )
    checks.append(Check("git_repo", OK if inside_repo else WARN,
                        "inside a git repository" if inside_repo else "not inside a git repository"))

    # Required flags mirror the adapter contracts exactly, so doctor never
    # reports "ok" for a binary the adapter would reject with capability_mismatch.
    # Since M1 P2 the probing lives in orx.probes (shared with
    # `orx agent probe`); outputs here are byte-compatible with M0.
    from orx.probes import doctor_harness_checks
    checks.extend(Check(*triplet) for triplet in doctor_harness_checks())

    if root is None:
        checks.append(Check("project", FAIL, "no .orx/ found; run `orx init` here"))
        checks.extend(_skill_checks())
        return _summary(checks)

    orx_dir = root / ".orx"
    config_path = orx_dir / "config.toml"
    profiles_path = orx_dir / "profiles.toml"
    db_path = orx_dir / "state.db"

    checks.append(Check("orx_dir", OK if orx_dir.is_dir() else FAIL,
                        ".orx/ present" if orx_dir.is_dir() else ".orx/ missing"))

    from orx.config import user_preset_installed
    preset_installed = user_preset_installed()

    config = None
    profiles: dict = {}
    if config_path.exists():
        try:
            config = load_config(config_path)
            warnings = list(config.warnings)
            if config.max_parallel != 1:
                warnings.append(f"max_parallel = {config.max_parallel}; M0 forces effective parallelism 1")
            checks.append(Check("config", OK, "; ".join(warnings) or "valid"))
        except ConfigError as exc:
            checks.append(Check("config", FAIL, "; ".join(exc.messages)))
    elif preset_installed:
        # Init inheritance (phase C): an installed user preset owns routing;
        # the project intentionally has no config.toml.
        checks.append(Check("config", OK, "inherited from user preset (no project layer)"))
    else:
        checks.append(Check("config", FAIL, f"{config_path} missing"))

    if profiles_path.exists():
        try:
            profiles = load_profiles(profiles_path)
            checks.append(Check("profiles", OK, f"{len(profiles)} profile(s) defined"))
        except ConfigError as exc:
            checks.append(Check("profiles", FAIL, "; ".join(exc.messages)))
    elif preset_installed:
        checks.append(Check("profiles", OK, "inherited from user preset (no project layer)"))
    else:
        checks.append(Check("profiles", FAIL, f"{profiles_path} missing"))

    if config is not None and profiles is not None:
        # M1: routing references resolve across layers — validate against the
        # effective (merged) profile set, falling back to the project layer
        # alone when the merge itself is broken (effective_config reports it).
        try:
            effective = load_effective(config_path, profiles_path)
            ref_config, ref_profiles = effective.config, effective.profiles
        except ConfigError:
            ref_config, ref_profiles = config, profiles
        ref_errors = validate_references(ref_config, ref_profiles)
        checks.append(Check("profile_references", OK if not ref_errors else FAIL,
                            "all configured profiles exist" if not ref_errors else "; ".join(ref_errors)))
        for name, profile in ref_profiles.items():
            if profile.harness.value == "shell":
                found = bool(profile.executable and shutil.which(profile.executable))
                checks.append(Check(f"shell:{name}", OK if found else WARN,
                                    f"executable {profile.executable!r} found" if found
                                    else f"executable {profile.executable!r} not found on PATH"))

    # M1 layered configuration: the user layer is informational (absent is
    # fine); the effective merge is what actually runs, so it gets its own
    # check, and shell-profile executable checks cover the merged set.
    from orx.config import user_config_path, user_profiles_path
    uc, up = user_config_path(), user_profiles_path()
    present = [p.name for p in (uc, up) if p.exists()]
    checks.append(Check("user_layer", OK,
                        f"{', '.join(present)} at {uc.parent}" if present
                        else "absent (project layer + defaults only)"))
    try:
        effective = load_effective(config_path, profiles_path)
        layers = {v for v in effective.profile_origins.values()}
        checks.append(Check("effective_config", OK,
                            f"{len(effective.profiles)} effective profile(s)"
                            f" (layers: {', '.join(sorted(layers)) or 'project'});"
                            f" controller from {effective.origins.get('controller.profile', 'default')}"))
        for name, profile in effective.profiles.items():
            if profile.harness.value == "shell" and name not in profiles:
                found = bool(profile.executable and shutil.which(profile.executable))
                checks.append(Check(f"shell:{name}", OK if found else WARN,
                                    f"executable {profile.executable!r} found" if found
                                    else f"executable {profile.executable!r} not found on PATH"
                                         f" (user-layer profile)"))
    except ConfigError as exc:
        checks.append(Check("effective_config", FAIL, "; ".join(exc.messages)))
    else:
        checks.extend(_subagent_definition_checks(effective.profiles))

    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            finally:
                conn.close()
            if row is None:
                checks.append(Check("state_db", FAIL, "no schema_version in meta"))
            else:
                version = int(row[0])
                if version > CODE_SCHEMA_VERSION:
                    checks.append(Check("state_db", FAIL,
                                        f"schema version {version} newer than supported {CODE_SCHEMA_VERSION}"))
                else:
                    checks.append(Check("state_db", OK, f"schema_version {version}"))
        except sqlite3.Error as exc:
            checks.append(Check("state_db", FAIL, f"cannot open: {exc}"))
    else:
        checks.append(Check("state_db", FAIL, f"{db_path} missing"))

    checks.extend(_skill_checks())

    return _summary(checks)


def _skill_checks() -> list[Check]:
    home = Path.home()
    checks: list[Check] = []
    for skill in ("orx-controller", "orx-agent"):
        target = home / ".agents" / "skills" / skill
        checks.append(Check(f"skill:{skill}", OK,
                            "installed" if target.exists() else "not installed (`orx skill install`)"))
    return checks


def _definition_field(text: str, field: str) -> str | None:
    """One frontmatter field from a ZCode agent definition file."""
    import re
    match = re.search(rf"^{field}:\s*[\"']?([^\"'\s]+)[\"']?\s*$", text, re.M)
    return match.group(1) if match else None


def _subagent_definition_checks(profiles: dict) -> list[Check]:
    """Role definitions vs profiles consistency (phase C). A subagent profile
    names a native agent (agent_ref); its definition file is what the host
    actually loads. A model mismatch means attribution will drift on every
    run — that is a failure, not a warning. A missing definition is a warning:
    the host may simply not have it installed (or needs a new session)."""
    from orx.presets import zcode_agents_dir

    checks: list[Check] = []
    directory = zcode_agents_dir()
    for name in sorted(profiles):
        profile = profiles[name]
        if getattr(profile, "host_mode", "self") != "subagent" or not profile.agent_ref:
            continue
        ref = profile.agent_ref
        definition = directory / f"{ref}.md"
        if not definition.is_file():
            checks.append(Check(
                f"agent_def:{ref}", WARN,
                f"definition not found at {definition} (install it, or restart the"
                " host session to load new definitions)",
            ))
            continue
        text = definition.read_text()
        defined_model = _definition_field(text, "model")
        thought = _definition_field(text, "thoughtLevel") or "-"
        if defined_model is None:
            checks.append(Check(
                f"agent_def:{ref}", WARN,
                f"{definition} has no parsable model field (thoughtLevel {thought})",
            ))
            continue
        requested = profile.model.rsplit("/", 1)[-1]
        actual = defined_model.rsplit("/", 1)[-1]
        if actual != requested:
            checks.append(Check(
                f"agent_def:{ref}", FAIL,
                f"profile {name} requests {profile.model} but the definition says"
                f" {defined_model} — fix one side; two drifting facts attribute"
                f" every run wrongly (thoughtLevel {thought})",
            ))
        else:
            checks.append(Check(
                f"agent_def:{ref}", OK,
                f"model {actual} matches profile {name} (thoughtLevel {thought})",
            ))
    return checks


def _summary(checks: list[Check]) -> dict:
    # Warnings (missing optional CLIs, no git repo) do not fail doctor;
    # only failures (invalid config/state) do.
    failures = sum(1 for c in checks if c.state == FAIL)
    return {
        "version": __version__,
        "checks": [{"name": c.name, "state": c.state, "detail": c.detail} for c in checks],
        "ok": failures == 0,
        "failures": failures,
        "warnings": sum(1 for c in checks if c.state == WARN),
    }
