"""Shared ORX runtime paths without a dependency on the ORX package."""

import os
from pathlib import Path


def orx_data_directory() -> Path:
    if override := os.environ.get("ORX_DATA_DIR"):
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "orx"


def analytics_data_directory(override: Path | str | None = None) -> Path:
    path = override or os.environ.get("ORX_ANALYTICS_HOME")
    return Path(path).expanduser() if path else orx_data_directory() / "analytics"
