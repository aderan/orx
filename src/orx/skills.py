"""User-level skill installation.

Canonical copy: ``~/.agents/skills/<name>``. If ``~/.zcode/skills``,
``~/.cursor/skills``, or ``~/.codex/skills`` exist, install (or refresh) a
symlink there pointing at the canonical copy. No per-repo copies.

`skill install` with no names installs the default set (``DEFAULT_SKILLS``).
Explicit names install only those skills — the installable set is discovered
dynamically as the packaged-skill subdirectories that contain a SKILL.md, so
newly packaged skills (e.g. ``orx skill install orx-pbv``) need no code
change. Installing an already-installed skill refreshes it. Names must be
directory-safe and available; otherwise ORXError names the available skills.

`skill update` replaces the canonical copy with the packaged one and refreshes
every symlink; it only refreshes skills that are already installed in the
canonical directory and still packaged (it never installs new skills).
"""

from __future__ import annotations

import importlib.resources
import shutil
from pathlib import Path

from orx.records import ORXError

DEFAULT_SKILLS = ("orx-controller", "orx-agent")
# Backwards-compatible alias for the pre-optional-names constant.
PACKAGED_SKILLS = DEFAULT_SKILLS
SYMLINK_SOURCES = (".zcode", ".cursor", ".codex")


def packaged_skills_dir() -> Path:
    """Packaged skills inside the distribution; falls back to the repo layout
    when running from an editable/source checkout."""
    try:
        candidate = Path(importlib.resources.files("orx")) / "skills"
    except (ModuleNotFoundError, TypeError):
        candidate = None
    if candidate and candidate.is_dir():
        return candidate
    dev = Path(__file__).resolve().parents[2] / "skills"
    if dev.is_dir():
        return dev
    raise ORXError("packaged skills not found; reinstall orx-agent")


def available_skills(source_root: Path | None = None) -> list[str]:
    """Names installable from the packaged skills dir: its subdirectories
    that contain a SKILL.md, sorted."""
    root = source_root if source_root is not None else packaged_skills_dir()
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    )


def _validate_name(name: str) -> None:
    """Defensive: names become directory names under the canonical root, so
    reject anything that is not a simple directory-safe name (path
    separators, traversal, dot-prefixed) before the availability check."""
    if name.startswith(".") or "/" in name or ".." in name:
        raise ORXError(f"invalid skill name: {name!r}")


def canonical_root(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".agents" / "skills"


def _symlink_dirs(home: Path) -> list[Path]:
    return [
        home / source / "skills"
        for source in SYMLINK_SOURCES
        if (home / source / "skills").is_dir()
    ]


def install_skills(
    home: Path | None = None,
    only_update: bool = False,
    names: list[str] | None = None,
) -> dict:
    home = home or Path.home()
    source_root = packaged_skills_dir()
    canonical = canonical_root(home)

    if only_update:
        # update ignores names: refresh installed ∩ available, install nothing
        targets = [n for n in available_skills(source_root) if (canonical / n).exists()]
        # stable order: default skills first (historical order), extras sorted
        targets = [n for n in DEFAULT_SKILLS if n in targets] + [
            n for n in targets if n not in DEFAULT_SKILLS
        ]
        if not targets:
            return {
                "canonical_root": str(canonical),
                "installed": [],
                "refreshed": [],
                "symlinked_into": [],
            }
    elif names is None:
        targets = list(DEFAULT_SKILLS)
    else:
        for name in names:
            _validate_name(name)
        available = available_skills(source_root)
        unknown = [n for n in names if n not in available]
        if unknown:
            raise ORXError(
                f"unknown skill: {', '.join(unknown)} "
                f"(available: {', '.join(available) if available else 'none'})"
            )
        targets = list(dict.fromkeys(names))  # dedupe, keep order

    canonical.mkdir(parents=True, exist_ok=True)

    installed: list[str] = []
    refreshed: list[str] = []
    for name in targets:
        src = source_root / name
        if not src.is_dir():
            raise ORXError(f"packaged skill missing: {src}")
        dst = canonical / name
        existed = dst.exists()
        if only_update and not existed:
            continue  # update refreshes installed skills only
        if dst.is_symlink():
            dst.unlink()
        elif dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        (installed if not existed else refreshed).append(name)

        for link_dir in _symlink_dirs(home):
            link = link_dir / name
            if link.is_symlink() or link.exists():
                if link.is_symlink() or link.is_file():
                    link.unlink()
                else:
                    shutil.rmtree(link)
            link_dir.mkdir(parents=True, exist_ok=True)
            link.symlink_to(dst)

    return {
        "canonical_root": str(canonical),
        "installed": installed,
        "refreshed": refreshed,
        "symlinked_into": [str(d.parent) for d in _symlink_dirs(home)],
    }


def update_skills(home: Path | None = None) -> dict:
    return install_skills(home=home, only_update=True)
