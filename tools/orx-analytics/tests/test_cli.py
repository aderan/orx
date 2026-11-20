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

def test_projects_and_watch_use_the_same_orx_data_directory(tmp_path, monkeypatch, capsys):
    from orxanalytics.cli import main
    from orxanalytics.store import Store

    for name in ("ORX_ANALYTICS_HOME", "ORX_DATA_DIR", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = tmp_path / ".local/share/orx/analytics"
    assert main(["watch", "status"]) == 0
    assert str(expected / "engine.sock") in capsys.readouterr().out
    assert not expected.exists()
    assert main(["projects", "list"]) == 0
    assert (expected / "analytics.db").is_file()

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("ORX_DATA_DIR", str(tmp_path / "orx-data"))
    monkeypatch.setenv("ORX_ANALYTICS_HOME", str(tmp_path / "override"))
    store = Store(tmp_path / "explicit")
    assert store.data_dir == tmp_path / "explicit"
    store.close()
    for name, wanted in (
        ("ORX_ANALYTICS_HOME", tmp_path / "override"),
        ("ORX_DATA_DIR", tmp_path / "orx-data/analytics"),
        ("XDG_DATA_HOME", tmp_path / "xdg/orx/analytics"),
    ):
        store = Store()
        assert store.data_dir == wanted
        store.close()
        monkeypatch.delenv(name)
