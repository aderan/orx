"""config.toml and profiles.toml loading and validation.

Profiles are *definitions* and live only in TOML. Runtime resource/quota
state lives in SQLite and is never written back into these files.

M1 layers (high -> low): CLI argument > environment > project `.orx/` >
user `~/.config/orx/` > built-in defaults. Scalars take the highest layer
that sets them; profiles merge by name with complete same-name replacement
(project wins); role routing lists (plan.*/worker/verify) use the project
section when the project config sets it, else the user's.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path

from orx import records
from orx.records import ConfigError

SCHEMA_VERSION = 1

VALID_DRIVERS = "host | cli | external"
VALID_HARNESSES = "zcode | codex | cursor | shell"
VALID_CLASSES = "frontier | strong | economy"
VALID_EFFORTS = "quick | standard | deep | max"
VALID_TRANSPORTS = "stdin | argument | file"
VALID_DEPTHS = "light | standard | deep"

LAYER_CLI = "cli"
LAYER_ENV = "env"
LAYER_PROJECT = "project"
LAYER_USER = "user"
LAYER_DEFAULT = "default"

# The environment layer is a small, documented allowlist — never a generic
# config-in-env free-for-all. Path/discovery env vars (ORX_PROJECT,
# ORX_CONFIG_DIR, ORX_DATA_DIR) live in their own functions below.
ENV_OVERRIDES: dict[str, str] = {
    "runtime.max_parallel": "ORX_RUNTIME_MAX_PARALLEL",
    "runtime.command_timeout_sec": "ORX_RUNTIME_COMMAND_TIMEOUT_SEC",
}

BUILTIN_DEFAULTS: dict = {
    "controller.profile": "orx-host",
    "plan.depth": "auto",
    "plan.allow_class_downgrade": False,
    "plan.light.profiles": [],
    "plan.standard.profiles": [],
    "plan.deep.profiles": [],
    "worker.profiles": [],
    "verify.profiles": [],
    "runtime.max_parallel": 1,
    "runtime.command_timeout_sec": 1800,
    "inbox.github_labels": [],
    "inbox.auto_accept": False,
}

# Effective keys `orx config list/get` report, in stable order. Every one of
# these is writable by `orx config set`. `schema_version` is file metadata,
# not an effective value, and is never writable.
WRITABLE_SPEC: dict[str, str] = {
    "controller.profile": "non-empty profile name defined in profiles.toml",
    "plan.depth": "auto | light | standard | deep",
    "plan.allow_class_downgrade": "true | false",
    "plan.light.profiles": "profile names (comma-separated or a JSON array)",
    "plan.standard.profiles": "profile names (comma-separated or a JSON array)",
    "plan.deep.profiles": "profile names (comma-separated or a JSON array)",
    "worker.profiles": "profile names (comma-separated or a JSON array)",
    "verify.profiles": "profile names (comma-separated or a JSON array)",
    "runtime.max_parallel": "integer >= 1",
    "runtime.command_timeout_sec": "integer > 0",
}
RECOGNIZED_KEYS: tuple[str, ...] = tuple(WRITABLE_SPEC)
PROFILE_KEYS = frozenset({
    "controller.profile",
    "plan.light.profiles",
    "plan.standard.profiles",
    "plan.deep.profiles",
    "worker.profiles",
    "verify.profiles",
})
_DEPTH_VALUES = ("auto", "light", "standard", "deep")
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


# ---------------------------------------------------------------------------
# User-layer paths


def user_config_dir() -> Path:
    """ORX_CONFIG_DIR > XDG_CONFIG_HOME/orx > ~/.config/orx."""
    env = os.environ.get("ORX_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "orx"


def user_data_dir() -> Path:
    """ORX_DATA_DIR > XDG_DATA_HOME/orx > ~/.local/share/orx."""
    env = os.environ.get("ORX_DATA_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "orx"


def user_config_path() -> Path:
    return user_config_dir() / "config.toml"


def user_profiles_path() -> Path:
    return user_config_dir() / "profiles.toml"


@dataclass(frozen=True)
class Profile:
    name: str
    driver: records.Driver
    harness: records.Harness
    model: str
    model_class: records.ModelClass
    effort: records.Effort
    capabilities: tuple[str, ...] = ()
    executable: str | None = None
    args: tuple[str, ...] = ()
    prompt_transport: str = "file"
    force: bool = False  # cursor harness only: allow --force/--yolo launch flags

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "driver": self.driver.value,
            "harness": self.harness.value,
            "model": self.model,
            "class": self.model_class.value,
            "effort": self.effort.value,
            "capabilities": list(self.capabilities),
            "executable": self.executable,
            "args": list(self.args),
            "prompt_transport": self.prompt_transport,
            "force": self.force,
        }


@dataclass(frozen=True)
class Config:
    controller_profile: str
    plan_depth_default: str  # "auto" or a PlanDepth value
    allow_class_downgrade: bool
    depth_profiles: dict[str, list[str]] = field(default_factory=dict)
    worker_profiles: list[str] = field(default_factory=list)
    verify_profiles: list[str] = field(default_factory=list)
    max_parallel: int = 1
    command_timeout_sec: int = 1800
    inbox_github_labels: list[str] = field(default_factory=list)
    inbox_auto_accept: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def effective_parallelism(self) -> int:
        """M0 shares one working tree, so effective CLI parallelism is 1."""
        return 1

    def candidate_profiles(self, role: records.Role, depth: records.PlanDepth | None = None) -> list[str]:
        if role is records.Role.CONTROLLER:
            return [self.controller_profile]
        if role is records.Role.PLANNER:
            return list(self.depth_profiles.get(depth.value if depth else "", []))
        if role is records.Role.WORKER:
            return list(self.worker_profiles)
        if role is records.Role.VERIFIER:
            return list(self.verify_profiles)
        return []


def _read_toml(path: Path, errors: list[str]) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        errors.append(f"{path.name}: file not found")
    except tomllib.TOMLDecodeError as exc:
        errors.append(f"{path.name}: TOML parse error: {exc}")
    return {}


def _check_schema_version(data: dict, name: str, errors: list[str]) -> None:
    if "schema_version" not in data:
        errors.append(f"{name}: missing schema_version (refusing to guess)")
        return
    version = data["schema_version"]
    if not isinstance(version, int) or version != SCHEMA_VERSION:
        errors.append(f"{name}: unsupported schema_version {version!r} (expected {SCHEMA_VERSION})")


def load_config(path: Path) -> Config:
    errors: list[str] = []
    warnings: list[str] = []
    data = _read_toml(path, errors)
    if errors:
        raise ConfigError(errors)

    _check_schema_version(data, "config.toml", errors)

    controller = data.get("controller", {})
    if not isinstance(controller, dict) or not isinstance(controller.get("profile"), str):
        errors.append("config.toml: [controller] profile must be a string")

    plan = data.get("plan", {})
    if not isinstance(plan, dict):
        errors.append("config.toml: [plan] must be a table")
        plan = {}

    depth_default = plan.get("depth", "auto")
    if depth_default not in ("auto", *VALID_DEPTHS.split(" | ")):
        errors.append(
            f"config.toml: [plan] depth {depth_default!r} invalid (expected auto | {VALID_DEPTHS})"
        )

    allow_downgrade = plan.get("allow_class_downgrade", False)
    if not isinstance(allow_downgrade, bool):
        errors.append("config.toml: [plan] allow_class_downgrade must be a boolean")
        allow_downgrade = False

    depth_profiles: dict[str, list[str]] = {}
    for depth_name in ("light", "standard", "deep"):
        section = plan.get(depth_name)
        if section is None:
            depth_profiles[depth_name] = []
            continue
        if not isinstance(section, dict) or not isinstance(section.get("profiles"), list):
            errors.append(f"config.toml: [plan.{depth_name}] profiles must be a list of strings")
            continue
        names = section["profiles"]
        if not all(isinstance(n, str) and n for n in names):
            errors.append(f"config.toml: [plan.{depth_name}] profiles must be a list of strings")
            continue
        depth_profiles[depth_name] = list(names)

    worker = data.get("worker", {})
    if not isinstance(worker, dict) or not isinstance(worker.get("profiles", []), list):
        errors.append("config.toml: [worker] profiles must be a list of strings")
        worker_profiles: list[str] = []
    else:
        worker_profiles = list(worker.get("profiles", []))
        if not all(isinstance(n, str) and n for n in worker_profiles):
            errors.append("config.toml: [worker] profiles must be a list of strings")
            worker_profiles = []

    verify = data.get("verify", {})
    if not isinstance(verify, dict) or not isinstance(verify.get("profiles", []), list):
        errors.append("config.toml: [verify] profiles must be a list of strings")
        verify_profiles: list[str] = []
    else:
        verify_profiles = list(verify.get("profiles", []))
        if not all(isinstance(n, str) and n for n in verify_profiles):
            errors.append("config.toml: [verify] profiles must be a list of strings")
            verify_profiles = []

    runtime_cfg = data.get("runtime", {})
    if not isinstance(runtime_cfg, dict):
        errors.append("config.toml: [runtime] must be a table")
        runtime_cfg = {}

    max_parallel = runtime_cfg.get("max_parallel", 1)
    if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or max_parallel < 1:
        errors.append("config.toml: [runtime] max_parallel must be an integer >= 1")
        max_parallel = 1
    elif max_parallel != 1:
        warnings.append(
            f"config.toml: runtime.max_parallel = {max_parallel} is not supported in M0; "
            "effective CLI parallelism is forced to 1 (one shared working tree)"
        )

    timeout = runtime_cfg.get("command_timeout_sec", 1800)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        errors.append("config.toml: [runtime] command_timeout_sec must be a positive integer")
        timeout = 1800

    if errors:
        raise ConfigError(errors)

    controller_profile = controller["profile"] if isinstance(controller, dict) else ""
    return Config(
        controller_profile=controller_profile,
        plan_depth_default=depth_default,
        allow_class_downgrade=allow_downgrade,
        depth_profiles=depth_profiles,
        worker_profiles=worker_profiles,
        verify_profiles=verify_profiles,
        max_parallel=max_parallel,
        command_timeout_sec=timeout,
        warnings=tuple(warnings),
    )


def load_profiles(path: Path) -> dict[str, Profile]:
    errors: list[str] = []
    data = _read_toml(path, errors)
    if errors:
        raise ConfigError(errors)

    _check_schema_version(data, "profiles.toml", errors)
    raw_profiles = data.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        errors.append("profiles.toml: [profiles] must be a table of profile tables")
        raw_profiles = {}

    profiles: dict[str, Profile] = {}
    for name, raw in raw_profiles.items():
        if not isinstance(raw, dict):
            errors.append(f"profiles.toml: [profiles.{name}] must be a table")
            continue
        errors.extend(_validate_profile(name, raw))

    if errors:
        raise ConfigError(errors)

    for name, raw in raw_profiles.items():
        if isinstance(raw, dict):
            profiles[name] = _build_profile(name, raw)
    return profiles


def _validate_profile(name: str, raw: dict) -> list[str]:
    errors: list[str] = []
    p = f"profiles.toml: [profiles.{name}]"

    driver = raw.get("driver")
    if records.parse_enum(records.Driver, driver) is None:
        errors.append(f"{p} driver {driver!r} invalid ({VALID_DRIVERS})")

    harness = raw.get("harness")
    if records.parse_enum(records.Harness, harness) is None:
        errors.append(f"{p} harness {harness!r} invalid ({VALID_HARNESSES})")

    klass = raw.get("class")
    if records.parse_enum(records.ModelClass, klass) is None:
        errors.append(f"{p} class {klass!r} invalid ({VALID_CLASSES})")

    effort = raw.get("effort")
    if records.parse_enum(records.Effort, effort) is None:
        errors.append(f"{p} effort {effort!r} invalid ({VALID_EFFORTS})")

    if not isinstance(raw.get("model"), str) or not raw.get("model"):
        errors.append(f"{p} model must be a non-empty string")

    caps = raw.get("capabilities", [])
    if not isinstance(caps, list) or not all(isinstance(c, str) and c.strip() for c in caps):
        errors.append(f"{p} capabilities must be a list of non-empty strings")

    transport = raw.get("prompt_transport", "file")
    if transport not in VALID_TRANSPORTS.split(" | "):
        errors.append(f"{p} prompt_transport {transport!r} invalid ({VALID_TRANSPORTS})")

    if harness == "shell" and not isinstance(raw.get("executable"), str) or (
        harness == "shell" and not raw.get("executable")
    ):
        errors.append(f"{p} executable is required for harness = 'shell'")

    # Harness -> adapter mapping: only shell/codex/cursor have launch adapters.
    # `zcode` is a host harness; a cli-driver profile on it can never dispatch.
    if driver == "cli" and harness == "zcode":
        errors.append(
            f"{p} driver = 'cli' has no adapter for harness = 'zcode'"
            " (zcode work is done by the host; use driver = 'host')"
        )

    args = raw.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        errors.append(f"{p} args must be a list of strings")

    force = raw.get("force", False)
    if not isinstance(force, bool):
        errors.append(f"{p} force must be a boolean (cursor harness only)")

    return errors


def _build_profile(name: str, raw: dict) -> Profile:
    return Profile(
        name=name,
        driver=records.Driver(raw["driver"]),
        harness=records.Harness(raw["harness"]),
        model=raw["model"],
        model_class=records.ModelClass(raw["class"]),
        effort=records.Effort(raw["effort"]),
        capabilities=tuple(raw.get("capabilities", [])),
        executable=raw.get("executable"),
        args=tuple(raw.get("args", [])),
        prompt_transport=raw.get("prompt_transport", "file"),
        force=bool(raw.get("force", False)),
    )


def validate_references(config: Config, profiles: dict[str, Profile]) -> list[str]:
    """Every configured profile name must exist in profiles.toml."""
    errors: list[str] = []
    if config.controller_profile not in profiles:
        errors.append(
            f"config.toml: [controller] profile {config.controller_profile!r} is not defined in profiles.toml"
        )
    for depth, names in config.depth_profiles.items():
        for n in names:
            if n not in profiles:
                errors.append(f"config.toml: [plan.{depth}] profile {n!r} is not defined in profiles.toml")
    for section, names in (("worker", config.worker_profiles), ("verify", config.verify_profiles)):
        for n in names:
            if n not in profiles:
                errors.append(f"config.toml: [{section}] profile {n!r} is not defined in profiles.toml")
    return errors


def load_project_config(config_path: Path, profiles_path: Path) -> tuple[Config, dict[str, Profile]]:
    """Load both files and cross-validate references. Raises ConfigError.

    M1: layered — the project layer merges over the user layer
    (~/.config/orx/, or ORX_CONFIG_DIR/XDG_CONFIG_HOME). Existing callers
    keep their signature; the merge is the only behavioral change."""
    effective = load_effective(config_path, profiles_path)
    return effective.config, effective.profiles


def _read_layer(path: Path, layer: str, errors: list[str]) -> dict:
    """Read one layer's TOML. A missing file is an empty layer; a present
    but unreadable/malformed file is a load error attributed to the layer."""
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        errors.append(f"{path.name} [{layer}]: TOML parse error: {exc}")
        return {}


@dataclass(frozen=True)
class EffectiveConfig:
    """Layered result: the effective Config + where every recognized value
    came from. Origins map dotted config keys to cli|env|project|user|
    default; profile_origins maps profile names to project|user."""

    config: Config
    profiles: dict[str, Profile]
    origins: dict[str, str]
    profile_origins: dict[str, str]
    user_config: Path
    user_profiles: Path
    # Raw validated docs (for `orx config` writes; never rewritten on read).
    project_raw: dict = field(default_factory=dict)
    user_raw: dict = field(default_factory=dict)


def _layer_scalar(*sources: tuple[str, object], default) -> tuple[object, str]:
    """First non-None value wins; sources are (layer, value) high -> low."""
    for layer, value in sources:
        if value is not None:
            return value, layer
    return default, LAYER_DEFAULT


def load_effective(
    config_path: Path,
    profiles_path: Path,
    user_config: Path | None = None,
    user_profiles: Path | None = None,
    check_references: bool = True,
) -> EffectiveConfig:
    """Assemble the effective configuration across layers.

    Precedence for recognized values: env > project > user > builtin
    defaults (the CLI-argument layer is applied by command flags on top of
    this result, e.g. --depth/--profile). Section lists (plan.*.profiles,
    worker, verify) are taken whole from the highest layer whose raw doc
    contains that section. Profiles merge by name with complete same-name
    replacement (project wins). Reading never creates or modifies files.
    ``check_references`` is set for commands that run a project; config
    inspection can report values before every profile name is defined."""
    errors: list[str] = []
    warnings: list[str] = []
    user_config = user_config or user_config_path()
    user_profiles = user_profiles or user_profiles_path()

    project_raw = _read_layer(config_path, LAYER_PROJECT, errors)
    user_raw = _read_layer(user_config, LAYER_USER, errors)
    if errors:
        raise ConfigError(errors)
    if project_raw:
        _check_schema_version(project_raw, "config.toml", errors)
    if user_raw:
        _check_schema_version(user_raw, "config.toml [user]", errors)
    if errors:
        raise ConfigError(errors)

    def section(raw: dict, dotted: str) -> tuple[object, bool]:
        node: object = raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return None, False
            node = node[part]
        return node, True

    origins: dict[str, str] = {}

    # --- environment layer (validated allowlist) ---
    env_values: dict[str, int] = {}
    for key, var in ENV_OVERRIDES.items():
        raw_value = os.environ.get(var)
        if raw_value is None:
            continue
        try:
            env_values[key] = int(raw_value)
        except ValueError:
            errors.append(f"env {var}={raw_value!r} must be an integer")
    if errors:
        raise ConfigError(errors)

    # --- scalars with precedence ---
    def layered(dotted: str):
        env_val = env_values.get(dotted)
        proj_val, proj_present = section(project_raw, dotted)
        user_val, user_present = section(user_raw, dotted)
        value, layer = _layer_scalar(
            (LAYER_ENV, env_val if dotted in env_values else None),
            (LAYER_PROJECT, proj_val if proj_present else None),
            (LAYER_USER, user_val if user_present else None),
            default=BUILTIN_DEFAULTS.get(dotted),
        )
        origins[dotted] = layer
        return value

    controller_profile = layered("controller.profile")
    depth_default = layered("plan.depth")
    allow_downgrade = layered("plan.allow_class_downgrade")
    max_parallel = layered("runtime.max_parallel")
    timeout = layered("runtime.command_timeout_sec")

    depth_profiles: dict[str, list[str]] = {}
    for depth_name in ("light", "standard", "deep"):
        dotted = f"plan.{depth_name}.profiles"
        depth_profiles[depth_name] = layered(dotted) or []
    worker_profiles = layered("worker.profiles") or []
    verify_profiles = layered("verify.profiles") or []
    inbox_labels = layered("inbox.github_labels") or []
    inbox_auto_accept = layered("inbox.auto_accept")

    # --- validation of effective values (M0 rules, unchanged) ---
    if not isinstance(controller_profile, str) or not controller_profile:
        errors.append("config.toml: [controller] profile must be a string")
    if depth_default not in ("auto", *VALID_DEPTHS.split(" | ")):
        errors.append(
            f"config.toml: [plan] depth {depth_default!r} invalid (expected auto | {VALID_DEPTHS})"
        )
    if not isinstance(allow_downgrade, bool):
        errors.append("config.toml: [plan] allow_class_downgrade must be a boolean")
        allow_downgrade = False
    for label, names in (
        ("plan.light.profiles", depth_profiles["light"]),
        ("plan.standard.profiles", depth_profiles["standard"]),
        ("plan.deep.profiles", depth_profiles["deep"]),
        ("worker.profiles", worker_profiles),
        ("verify.profiles", verify_profiles),
        ("inbox.github_labels", inbox_labels),
    ):
        if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
            errors.append(f"config.toml: {label} must be a list of strings")
    if not isinstance(inbox_auto_accept, bool):
        errors.append("config.toml: [inbox] auto_accept must be a boolean")
        inbox_auto_accept = False
    if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or max_parallel < 1:
        errors.append("config.toml: [runtime] max_parallel must be an integer >= 1")
        max_parallel = 1
    elif max_parallel != 1:
        warnings.append(
            f"config.toml: runtime.max_parallel = {max_parallel} is not supported in M0; "
            "effective CLI parallelism is forced to 1 (one shared working tree)"
        )
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        errors.append("config.toml: [runtime] command_timeout_sec must be a positive integer")
        timeout = 1800
    if errors:
        raise ConfigError(errors)

    # --- profiles: per-layer validation, merge by name (project wins) ---
    profiles: dict[str, Profile] = {}
    profile_origins: dict[str, str] = {}
    for layer, path in ((LAYER_USER, user_profiles), (LAYER_PROJECT, profiles_path)):
        raw = _read_layer(path, layer, errors)
        if errors:
            raise ConfigError(errors)
        if not raw:
            continue
        _check_schema_version(raw, f"profiles.toml [{layer}]", errors)
        raw_profiles = raw.get("profiles", {})
        if not isinstance(raw_profiles, dict):
            errors.append(f"profiles.toml [{layer}]: [profiles] must be a table")
            continue
        if errors:
            raise ConfigError(errors)
        for name, entry in raw_profiles.items():
            if not isinstance(entry, dict):
                errors.append(f"profiles.toml [{layer}]: [profiles.{name}] must be a table")
                continue
            errs = _validate_profile(name, entry)
            errors.extend(f"profiles.toml [{layer}]: {e.split(': ', 1)[-1]}" for e in errs)
        if errors:
            raise ConfigError(errors)
        for name, entry in raw_profiles.items():
            profiles[name] = _build_profile(name, entry)
            profile_origins[name] = layer

    config = Config(
        controller_profile=controller_profile,
        plan_depth_default=depth_default,
        allow_class_downgrade=allow_downgrade,
        depth_profiles=depth_profiles,
        worker_profiles=worker_profiles,
        verify_profiles=verify_profiles,
        max_parallel=max_parallel,
        command_timeout_sec=timeout,
        inbox_github_labels=list(inbox_labels),
        inbox_auto_accept=inbox_auto_accept,
        warnings=tuple(warnings),
    )
    if check_references:
        errors.extend(validate_references(config, profiles))
    if errors:
        raise ConfigError(errors)
    return EffectiveConfig(
        config=config,
        profiles=profiles,
        origins=origins,
        profile_origins=profile_origins,
        user_config=user_config,
        user_profiles=user_profiles,
        project_raw=project_raw,
        user_raw=user_raw,
    )


# ---------------------------------------------------------------------------
# orx config path / list / get / set


def resolved_paths(project_root: Path | None) -> dict:
    """Absolute paths the config commands report.

    ``project_config`` and ``project_profiles`` are null when no project
    applies. Paths are the locations that would be read or written; the
    files do not have to exist.
    """
    def absolute(path: Path) -> str:
        return str(path.expanduser().resolve())

    project_config = None
    project_profiles = None
    if project_root is not None:
        root = project_root.expanduser().resolve()
        project_config = str(root / ".orx" / "config.toml")
        project_profiles = str(root / ".orx" / "profiles.toml")
    return {
        "user_config": absolute(user_config_path()),
        "user_profiles": absolute(user_profiles_path()),
        "user_data": absolute(user_data_dir()),
        "project_config": project_config,
        "project_profiles": project_profiles,
    }


def config_entries(effective: EffectiveConfig) -> list[dict]:
    """Recognized effective values and the layer that won for each."""
    cfg = effective.config
    values = {
        "controller.profile": cfg.controller_profile,
        "plan.depth": cfg.plan_depth_default,
        "plan.allow_class_downgrade": cfg.allow_class_downgrade,
        "plan.light.profiles": list(cfg.depth_profiles.get("light") or []),
        "plan.standard.profiles": list(cfg.depth_profiles.get("standard") or []),
        "plan.deep.profiles": list(cfg.depth_profiles.get("deep") or []),
        "worker.profiles": list(cfg.worker_profiles),
        "verify.profiles": list(cfg.verify_profiles),
        "runtime.max_parallel": cfg.max_parallel,
        "runtime.command_timeout_sec": cfg.command_timeout_sec,
    }
    return [
        {"key": key, "value": values[key], "origin": effective.origins[key]}
        for key in RECOGNIZED_KEYS
    ]


def profile_names_in(path: Path) -> set[str]:
    """Profile names defined in one profiles.toml. A missing file contributes
    nothing. A present but unreadable file is an error (the caller must not
    write a profile-valued key it cannot check)."""
    if not path.exists():
        return set()
    if not path.is_file():
        raise ConfigError([f"{path.name}: not a file"])
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"{path.name}: TOML parse error: {exc}"]) from exc
    errors: list[str] = []
    _check_schema_version(raw, path.name, errors)
    table = raw.get("profiles", {})
    if not isinstance(table, dict):
        errors.append(f"{path.name}: [profiles] must be a table")
    if errors:
        raise ConfigError(errors)
    return {name for name in table if isinstance(name, str)}


def set_config_value(
    path: Path,
    key: str,
    raw: str,
    *,
    create: bool,
    profile_names: set[str] | None = None,
) -> dict:
    """Parse and validate ``raw``, then write ``key`` into ``path``.

    The target is created only when ``create`` is true and the value is
    valid. A created file is ``schema_version = 1`` plus that one key —
    callers must not pass a document pre-filled with other layers.
    On every failure the target bytes are left untouched.
    """
    parsed = parse_config_value(key, raw, profile_names=profile_names)
    doc, existed = _load_config_document(path, create=create)
    doc = copy.deepcopy(doc)
    schema = doc.get("schema_version")
    _assign_dotted(doc, key, parsed)
    if doc.get("schema_version") != schema:
        raise ConfigError(["schema_version cannot be changed"])
    text = _dump_toml(doc)
    try:
        rewritten = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"refusing to write: generated TOML is invalid: {exc}"]) from exc
    if _canonical(rewritten) != _canonical(doc):
        raise ConfigError([
            "refusing to write: rewriting the file would drop or change existing values"
        ])
    try:
        _atomic_write_text(path, text)
    except OSError as exc:
        raise ConfigError([f"{path}: {exc}"]) from exc
    return {
        "key": key,
        "value": parsed,
        "path": str(path.expanduser().resolve()),
        "created": not existed,
    }


def parse_config_value(key: str, raw: str, *, profile_names: set[str] | None = None):
    """Turn one CLI value into a typed TOML value, or raise ConfigError.

    ``profile_names`` is required for profile-valued keys: every name must
    already be defined. Pass None for keys that do not name profiles.
    """
    if key == "schema_version":
        raise ConfigError(["schema_version cannot be changed"])
    if key not in WRITABLE_SPEC:
        known = ", ".join(WRITABLE_SPEC)
        raise ConfigError([
            f"unsupported config key {key!r}",
            f"writable keys: {known}",
        ])
    if key == "controller.profile":
        value = _parse_profile_name(key, raw)
        _require_known_profiles(key, [value], profile_names)
        return value
    if key == "plan.depth":
        value = raw.strip()
        if value not in _DEPTH_VALUES:
            expect = " | ".join(_DEPTH_VALUES)
            raise ConfigError([f"{key} {raw!r} invalid (expected {expect})"])
        return value
    if key == "plan.allow_class_downgrade":
        return _parse_bool(key, raw)
    if key in PROFILE_KEYS:
        names = _parse_name_list(key, raw)
        _require_known_profiles(key, names, profile_names)
        return names
    if key == "runtime.max_parallel":
        number = _parse_int(key, raw)
        if number < 1:
            raise ConfigError([f"{key} must be an integer >= 1"])
        return number
    if key == "runtime.command_timeout_sec":
        number = _parse_int(key, raw)
        if number <= 0:
            raise ConfigError([f"{key} must be a positive integer"])
        return number
    raise ConfigError([f"unsupported config key {key!r}"])


def _parse_bool(key: str, raw: str) -> bool:
    text = raw.strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    raise ConfigError([f"{key} must be true or false"])


def _parse_int(key: str, raw: str) -> int:
    text = raw.strip()
    if not re.fullmatch(r"-?[0-9]+", text):
        raise ConfigError([f"{key} must be an integer"])
    return int(text)


def _parse_profile_name(key: str, raw: str) -> str:
    name = raw.strip()
    if not name:
        raise ConfigError([f"{key} must be a non-empty string"])
    return name


def _parse_name_list(key: str, raw: str) -> list[str]:
    text = raw.strip()
    items: list | None = None
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            items = parsed
        elif text.endswith("]"):
            inner = text[1:-1].strip()
            items = [] if inner == "" else [_unquote(part.strip()) for part in inner.split(",")]
        else:
            raise ConfigError([
                f"{key} must be a comma-separated list of names or a JSON array"
            ])
    elif text == "":
        raise ConfigError([
            f"{key} must be a comma-separated list of names or a JSON array"
        ])
    else:
        items = [_unquote(part.strip()) for part in text.split(",")]
    if not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items):
        raise ConfigError([f"{key} must be a list of non-empty strings"])
    return [item.strip() for item in items]


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _require_known_profiles(key: str, names: list[str], profile_names: set[str] | None) -> None:
    known = profile_names or set()
    missing = [name for name in names if name not in known]
    if not missing:
        return
    errors = [
        f"{key}: profile {name!r} is not defined in profiles.toml" for name in missing
    ]
    raise ConfigError(errors)


def _load_config_document(path: Path, *, create: bool) -> tuple[dict, bool]:
    """Return ``(document, existed)``. A missing file becomes a schema-only
    document when ``create`` is set; it is not written yet."""
    if not path.exists():
        if not create:
            raise ConfigError([f"{path.name} does not exist"])
        return {"schema_version": SCHEMA_VERSION}, False
    if not path.is_file():
        raise ConfigError([f"{path} is not a file"])
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"{path.name}: TOML parse error: {exc}"]) from exc
    errors: list[str] = []
    _check_schema_version(raw, path.name, errors)
    if errors:
        raise ConfigError(errors)
    return raw, True


def _assign_dotted(doc: dict, key: str, value) -> None:
    parts = key.split(".")
    if not parts or any(not part for part in parts):
        raise ConfigError([f"unsupported config key {key!r}"])
    node = doc
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        elif not isinstance(child, dict):
            raise ConfigError([f"{key}: {part!r} is not a table"])
        node = child
    node[parts[-1]] = value


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _canonical(value):
    """Compare parsed TOML by value, ignoring timezone spelling."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
        return ("datetime", value.isoformat())
    if isinstance(value, date):
        return ("date", value.isoformat())
    if isinstance(value, time):
        return ("time", value.isoformat())
    if isinstance(value, dict):
        items = tuple(sorted((key, _canonical(item)) for key, item in value.items()))
        return ("dict", items)
    if isinstance(value, list):
        return ("list", tuple(_canonical(item) for item in value))
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, float):
        return ("float", value)
    return ("scalar", value)


def _dump_toml(data: dict) -> str:
    lines: list[str] = []
    _emit_container(None, data, lines)
    return "\n".join(lines).rstrip() + "\n"


def _emit_container(prefix: str | None, data: dict, lines: list[str]) -> None:
    scalars: list[tuple[str, object]] = []
    arrays: list[tuple[str, list]] = []
    tables: list[tuple[str, dict]] = []
    for key, value in data.items():
        if isinstance(value, dict):
            tables.append((key, value))
        elif _is_array_of_tables(value):
            arrays.append((key, value))
        else:
            scalars.append((key, value))
    if prefix is None:
        for key, value in scalars:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        if scalars and (tables or arrays):
            lines.append("")
    elif scalars or (not tables and not arrays):
        lines.append(f"[{_toml_dotted(prefix)}]")
        for key, value in scalars:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        lines.append("")
    for key, value in arrays:
        name = key if prefix is None else f"{prefix}.{key}"
        for item in value:
            if not isinstance(item, dict):
                raise ConfigError(["refusing to write: unsupported array of tables"])
            _emit_array_item(name, item, lines)
    for key, value in tables:
        name = key if prefix is None else f"{prefix}.{key}"
        _emit_container(name, value, lines)


def _emit_array_item(prefix: str, item: dict, lines: list[str]) -> None:
    lines.append(f"[[{_toml_dotted(prefix)}]]")
    scalars = [(k, v) for k, v in item.items() if not isinstance(v, dict) and not _is_array_of_tables(v)]
    for key, value in scalars:
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    lines.append("")
    for key, value in item.items():
        name = f"{prefix}.{key}"
        if _is_array_of_tables(value):
            for child in value:
                _emit_array_item(name, child, lines)
        elif isinstance(value, dict):
            _emit_container(name, value, lines)


def _is_array_of_tables(value: object) -> bool:
    return isinstance(value, list) and len(value) > 0 and all(isinstance(item, dict) for item in value)


def _toml_dotted(name: str) -> str:
    return ".".join(_toml_key(part) for part in name.split("."))


def _toml_key(key: str) -> str:
    if _BARE_KEY.fullmatch(key):
        return key
    return _toml_string(key)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigError(["refusing to write: non-finite float cannot be represented"])
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, datetime):
        text = value.isoformat()
        if value.tzinfo is not None and text.endswith("+00:00"):
            return text[:-6] + "Z"
        return text
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ConfigError([f"refusing to write: unsupported value type {type(value).__name__}"])


def _toml_string(value: str) -> str:
    pieces = ['"']
    for char in value:
        code = ord(char)
        if char == "\\":
            pieces.append("\\\\")
        elif char == '"':
            pieces.append('\\"')
        elif char == "\b":
            pieces.append("\\b")
        elif char == "\t":
            pieces.append("\\t")
        elif char == "\n":
            pieces.append("\\n")
        elif char == "\f":
            pieces.append("\\f")
        elif char == "\r":
            pieces.append("\\r")
        elif code < 32:
            pieces.append(f"\\u{code:04x}")
        else:
            pieces.append(char)
    pieces.append('"')
    return "".join(pieces)


def migrate_profiles_to_user(project_profiles: Path,
                             user_profiles: Path | None = None) -> dict:
    """Collision-safe host procedure: move the project layer's profile
    definitions to the user layer, leaving the project with a schema header.

    Rules (each is regression-tested):
    - absent user file: project definitions become the user layer verbatim;
    - existing user file: same-name definitions must be identical, or the
      migration refuses (no partial writes); unrelated entries preserved;
    - idempotent: re-running after a successful migration is a no-op (the
      project layer already is a bare schema header);
    - returns a report {moved, preserved, skipped, user_profiles}.

    The caller rewrites the project file only on success."""
    user_profiles = user_profiles or user_profiles_path()
    project_raw = _read_layer(project_profiles, LAYER_PROJECT, [])
    project_profiles_map = project_raw.get("profiles", {}) if project_raw else {}
    if not project_profiles_map:
        return {"moved": [], "preserved": [], "user_profiles": str(user_profiles),
                "note": "project layer has no profile definitions; nothing to do"}

    if user_profiles.exists():
        user_raw = _read_layer(user_profiles, LAYER_USER, [])
        user_map = user_raw.get("profiles", {}) if user_raw else {}
        conflicts = [n for n, entry in project_profiles_map.items()
                     if n in user_map and user_map[n] != entry]
        if conflicts:
            raise ConfigError([
                f"profile(s) {conflicts} differ between project and user layers; "
                "refusing to migrate (resolve manually or delete the project definitions)"
            ])
        target_map = {**user_map, **project_profiles_map}
        preserved = sorted(set(user_map) - set(project_profiles_map))
    else:
        target_map = dict(project_profiles_map)
        preserved = []

    user_profiles.parent.mkdir(parents=True, exist_ok=True)
    lines = ["schema_version = 1\n"]
    for name, entry in target_map.items():
        lines.append(f"\n[profiles.{name}]\n")
        for key in ("driver", "harness", "model", "class", "effort"):
            lines.append(f'{key} = "{entry[key]}"\n')
        caps = ", ".join(f'"{c}"' for c in entry.get("capabilities", []))
        lines.append(f"capabilities = [{caps}]\n")
        for key in ("executable", "prompt_transport"):
            if entry.get(key) is not None:
                lines.append(f'{key} = "{entry[key]}"\n')
        if entry.get("force"):
            lines.append("force = true\n")
        if entry.get("args"):
            args = ", ".join(json.dumps(a) for a in entry["args"])
            lines.append(f"args = [{args}]\n")
    user_profiles.write_text("".join(lines))
    return {"moved": sorted(project_profiles_map), "preserved": preserved,
            "user_profiles": str(user_profiles), "note": None}
