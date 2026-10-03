"""Install-source detection and `orx update`.

Source detection uses uv's own receipt (``<uv tool dir>/orx-agent/uv-receipt.toml``)
plus ``sys.prefix``; parsing `uv tool list` text would break on format changes.

`orx update` runs ``uv tool upgrade orx-agent`` only for a non-editable
uv-tool install. Editable installs exit 1 with the reason. Tests inject a
fake ``uv`` executable and never upgrade the interpreter running pytest.
"""

from __future__ import annotations

import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from orx import __version__, runtime
from orx.records import ORXError

UPGRADE_COMMAND = ["uv", "tool", "upgrade", "orx-agent"]


@dataclass(frozen=True)
class InstallSource:
    source: str  # "uv-tool" | "editable" | "unknown"
    detail: str
    tool_env: Path | None = None

    @property
    def can_upgrade(self) -> bool:
        return self.source == "uv-tool"


def _uv_tool_dir() -> Path | None:
    if not shutil.which("uv"):
        return None
    result = runtime.run_argv(["uv", "tool", "dir"], cwd=Path.cwd(), timeout=30)
    if not result.ok:
        return None
    first = result.stdout.strip().splitlines()
    if not first:
        return None
    return Path(first[0].strip())


def detect_install_source(prefix: str | None = None) -> InstallSource:
    prefix = prefix if prefix is not None else sys.prefix
    tool_dir = _uv_tool_dir()
    if tool_dir is None:
        return InstallSource("unknown", "uv not found or `uv tool dir` failed")
    tool_env = tool_dir / "orx-agent"
    receipt = tool_env / "uv-receipt.toml"
    if not receipt.exists():
        return InstallSource("unknown", f"no uv receipt at {receipt}")
    if Path(prefix).resolve() != tool_env.resolve():
        return InstallSource(
            "unknown",
            f"running interpreter is not the uv tool env ({tool_env}); "
            "this orx came from pip/venv/source",
        )
    try:
        data = tomllib.loads(receipt.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return InstallSource("unknown", f"unreadable receipt: {exc}")
    # uv writes: [tool] requirements = [{ name = "orx-agent", editable = <path> }]
    tool_section = data.get("tool")
    if isinstance(tool_section, dict):
        for requirement in tool_section.get("requirements", []):
            if (
                isinstance(requirement, dict)
                and requirement.get("name") == "orx-agent"
                and requirement.get("editable")
            ):
                return InstallSource("editable", "uv tool editable install", tool_env=tool_env)
    return InstallSource("uv-tool", "uv tool install", tool_env=tool_env)


def check_update(prefix: str | None = None) -> dict:
    detected = detect_install_source(prefix=prefix)
    return {
        "version": __version__,
        "source": detected.source,
        "detail": detected.detail,
        "command": " ".join(UPGRADE_COMMAND) if detected.can_upgrade else None,
        "can_upgrade": detected.can_upgrade,
    }


def run_update(prefix: str | None = None) -> dict:
    detected = detect_install_source(prefix=prefix)
    if detected.source == "editable":
        raise ORXError(
            "orx is an editable uv tool install; upgrade by pulling the source "
            "checkout and reinstalling, not via `uv tool upgrade`"
        )
    if not detected.can_upgrade:
        raise ORXError(
            f"orx update only supports non-editable uv tool installs "
            f"(detected: {detected.source}; {detected.detail})"
        )
    result = runtime.run_argv(UPGRADE_COMMAND, cwd=Path.cwd(), timeout=600)
    return {
        "ok": result.ok,
        "command": " ".join(UPGRADE_COMMAND),
        "exit_code": result.exit_code,
        "output": result.stdout[-4000:],
    }
