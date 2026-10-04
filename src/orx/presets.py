"""User-layer presets: opinionated defaults for a specific host setup.

A preset is a packaged directory (``src/orx/presets/<name>/``) holding a
``config.toml`` (routing defaults) and a ``profiles.toml``. Installing one
writes ONLY the user layer (~/.config/orx, or ORX_CONFIG_DIR) and, for
subagent presets, copies the referenced native agent definitions into the
ZCode agents directory. No project directory is ever read or written here —
project layers keep winning by the normal layering rules.

Merge discipline (each regression-tested):
- profiles: same-name entries must be identical, or the whole install is
  refused with the conflicting names (no partial writes);
- config: preset keys fill only gaps — keys the user already set are
  preserved and reported, never overwritten;
- agent definitions: installed only when missing; an existing live
  definition always wins (it is the one the host actually loads) and drift
  is surfaced by `orx doctor`, not silently replaced.
"""

from __future__ import annotations

import importlib.resources
import os
import shutil
import tomllib
from pathlib import Path

from orx.config import (
    _dump_toml,
    user_config_path,
    user_profiles_path,
    load_config,
    load_profiles,
)
from orx.records import ConfigError, NotFoundError

# Keys a preset config may set, with the section list semantics noted.
_PRESET_CONFIG_KEYS = (
    "controller.profile",
    "plan.depth",
    "plan.allow_class_downgrade",
    "plan.light.profiles",
    "plan.standard.profiles",
    "plan.deep.profiles",
    "worker.profiles",
    "verify.profiles",
    "runtime.max_parallel",
    "runtime.command_timeout_sec",
)


def _presets_root() -> Path:
    return Path(importlib.resources.files("orx")) / "presets"


def preset_dir(name: str) -> Path:
    directory = _presets_root() / name
    if not (directory / "config.toml").is_file() or not (directory / "profiles.toml").is_file():
        known = ", ".join(p["name"] for p in list_presets()) or "(none)"
        raise NotFoundError(
            f"unknown preset {name!r}; available presets: {known}"
        )
    return directory


def list_presets() -> list[dict]:
    """Presets packaged with this ORX, in stable name order."""
    root = _presets_root()
    if not root.is_dir():
        return []
    rows = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (directory / "config.toml").is_file():
            continue
        profiles = load_profiles(directory / "profiles.toml")
        rows.append({
            "name": directory.name,
            "profiles": len(profiles),
            "config": str((directory / "config.toml")),
        })
    return rows


def zcode_agents_dir() -> Path:
    """Where ZCode loads custom subagent definitions from."""
    env = os.environ.get("ORX_ZCODE_AGENTS_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".zcode" / "agents"


def source_agents_dir() -> Path:
    """ORX's own agent definitions. Packaged copy first, repo layout second
    (editable installs resolve here, same fallback order as skills)."""
    packaged = Path(importlib.resources.files("orx")) / "agents"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "agents"


def _read_toml_doc(path: Path) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _section(raw: dict, dotted: str):
    node: object = raw
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def install_preset(name: str, *, agents_dir: Path | None = None) -> dict:
    """Install a preset into the user layer. Raises ConfigError on profile
    conflicts (nothing written); returns a full report otherwise."""
    directory = preset_dir(name)

    # Validate the packaged preset itself before touching the user layer —
    # a broken preset must fail loudly here, not at the next project open.
    load_config(directory / "config.toml")
    preset_profiles = load_profiles(directory / "profiles.toml")

    agents_target = agents_dir or zcode_agents_dir()
    user_profiles = user_profiles_path()
    user_config = user_config_path()
    user_profiles.parent.mkdir(parents=True, exist_ok=True)

    # --- profiles: identical-or-refuse merge ---
    preset_map = _read_toml_doc(directory / "profiles.toml")["profiles"]
    added: list[str] = []
    preserved: list[str] = []
    if user_profiles.exists() and user_profiles.stat().st_size > 0:
        user_doc = _read_toml_doc(user_profiles)
        user_map = user_doc.get("profiles", {})
        conflicts = sorted(
            n for n, entry in preset_map.items()
            if n in user_map and user_map[n] != entry
        )
        if conflicts:
            raise ConfigError([
                f"profile(s) {conflicts} differ between the user layer and preset"
                f" '{name}'; refusing to install (resolve manually or align the"
                " user definitions)"
            ])
        merged_map = dict(user_map)
        for n in preset_map:
            if n in user_map:
                preserved.append(n)
            else:
                merged_map[n] = preset_map[n]
                added.append(n)
        doc = {"schema_version": 1, "profiles": merged_map}
        user_profiles.write_text(_dump_toml(doc))
    else:
        shutil.copyfile(directory / "profiles.toml", user_profiles)
        added = sorted(preset_profiles)
    # Order-stable report.
    added, preserved = sorted(added), sorted(preserved)

    # --- config: fill gaps, preserve user values ---
    preset_doc = _read_toml_doc(directory / "config.toml")
    config_added: list[str] = []
    config_preserved: list[str] = []
    if user_config.exists() and user_config.stat().st_size > 0:
        user_doc = _read_toml_doc(user_config)
        import copy
        doc = copy.deepcopy(user_doc)
        for key in _PRESET_CONFIG_KEYS:
            value, present = _section(preset_doc, key)
            if not present:
                continue
            _, user_set = _section(user_doc, key)
            if user_set:
                config_preserved.append(key)
                continue
            node = doc
            parts = key.split(".")
            for part in parts[:-1]:
                child = node.setdefault(part, {})
                if not isinstance(child, dict):
                    raise ConfigError([f"user config key {key!r} conflicts with a non-table section"])
                node = child
            node[parts[-1]] = value
            config_added.append(key)
        user_config.write_text(_dump_toml(doc))
    else:
        shutil.copyfile(directory / "config.toml", user_config)
        config_added = list(_PRESET_CONFIG_KEYS)

    # --- agent definitions: install only what is missing ---
    agents_report = []
    refs = sorted({p.agent_ref for p in preset_profiles.values() if p.agent_ref})
    if refs:
        source = source_agents_dir()
        agents_target.mkdir(parents=True, exist_ok=True)
        for ref in refs:
            definition = agents_target / f"{ref}.md"
            shipped = source / f"{ref}.md"
            if definition.exists():
                agents_report.append({
                    "name": ref, "installed": False, "preserved_existing": True,
                    "path": str(definition),
                })
                continue
            if not shipped.is_file():
                agents_report.append({
                    "name": ref, "installed": False, "preserved_existing": False,
                    "path": None,
                    "note": "definition not packaged with this ORX (install manually)",
                })
                continue
            shutil.copyfile(shipped, definition)
            agents_report.append({
                "name": ref, "installed": True, "preserved_existing": False,
                "path": str(definition),
            })

    return {
        "preset": name,
        "user_profiles": str(user_profiles),
        "profiles_added": added,
        "profiles_preserved": preserved,
        "user_config": str(user_config),
        "config_added": config_added,
        "config_preserved": config_preserved,
        "agents": agents_report,
        "agents_dir": str(agents_target),
        "conflicts": [],
    }
