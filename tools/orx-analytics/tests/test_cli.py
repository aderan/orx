"""The module entry behaves like the installed console command."""

from __future__ import annotations

import os
import subprocess
import sys

from orxanalytics import __version__


def test_module_version_needs_no_source_or_data(tmp_path):
    env = {**os.environ, "ORX_ANALYTICS_HOME": str(tmp_path / "unused")}
    result = subprocess.run(
        [sys.executable, "-m", "orxanalytics", "--version"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == __version__
    assert not (tmp_path / "unused").exists()
