"""User-level skill installation.

Canonical copy: ``~/.agents/skills/<name>``. If ``~/.zcode/skills``,
``~/.cursor/skills``, or ``~/.codex/skills`` exist, install (or refresh) a
symlink there pointing at the canonical copy. No per-repo copies.

`skill update` replaces the canonical copy with the packaged one and refreshes
every symlink.
"""

from __future__ import annotations

import importlib.resources
import shutil
from pathlib import Path

from orx.records import ORXError

PACKAGED_SKILLS = ("orx-controller", "orx-agent")
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


def canonical_root(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".agents" / "skills"


def _symlink_dirs(home: Path) -> list[Path]:
    return [
        home / source / "skills"
        for source in SYMLINK_SOURCES
        if (home / source / "skills").is_dir()
    ]


def install_skills(home: Path | None = None, only_update: bool = False) -> dict:
    home = home or Path.home()
    source_root = packaged_skills_dir()
    canonical = canonical_root(home)

    if only_update:
        already = [n for n in PACKAGED_SKILLS if (canonical / n).exists()]
        if not already:
            return {
                "canonical_root": str(canonical),
                "installed": [],
                "refreshed": [],
                "symlinked_into": [],
            }
    canonical.mkdir(parents=True, exist_ok=True)

    installed: list[str] = []
    refreshed: list[str] = []
    for name in PACKAGED_SKILLS:
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
