"""config.toml and profiles.toml loading and validation.

Profiles are *definitions* and live only in TOML. Runtime resource/quota
state lives in SQLite and is never written back into these files.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
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
    """Load both files and cross-validate references. Raises ConfigError."""
    errors: list[str] = []
    config: Config | None = None
    profiles: dict[str, Profile] = {}
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        errors.extend(exc.messages)
    try:
        profiles = load_profiles(profiles_path)
    except ConfigError as exc:
        errors.extend(exc.messages)
    if config is not None:
        errors.extend(validate_references(config, profiles))
    if errors:
        raise ConfigError(errors)
    return config, profiles
