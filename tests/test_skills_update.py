"""Skills installation and update/self-upgrade (fake uv; never upgrades pytest)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from orx import skills as skills_mod
from orx import update as update_mod
from orx.records import ORXError


# ---------------------------------------------------------------------------
# Skills


def test_package_contains_both_skills():
    root = skills_mod.packaged_skills_dir()
    for name in skills_mod.PACKAGED_SKILLS:
        skill = root / name / "SKILL.md"
        assert skill.is_file(), skill
        text = skill.read_text()
        assert text.startswith("---")
        assert f"name: {name}" in text


def test_install_writes_canonical_and_symlinks(tmp_path):
    home = tmp_path / "home"
    (home / ".zcode" / "skills").mkdir(parents=True)   # exists -> symlink
    # ~/.cursor and ~/.codex do not exist -> no symlinks there

    result = skills_mod.install_skills(home=home)

    canonical = home / ".agents" / "skills"
    for name in skills_mod.PACKAGED_SKILLS:
        assert (canonical / name / "SKILL.md").is_file()
        link = home / ".zcode" / "skills" / name
        assert link.is_symlink()
        assert link.resolve() == (canonical / name).resolve()
    assert not (home / ".cursor").exists()
    assert result["installed"] == list(skills_mod.PACKAGED_SKILLS)


def test_skill_update_replaces_canonical_and_refreshes_symlinks(tmp_path):
    home = tmp_path / "home"
    (home / ".zcode" / "skills").mkdir(parents=True)
    skills_mod.install_skills(home=home)

    canonical_skill = home / ".agents" / "skills" / "orx-agent" / "SKILL.md"
    canonical_skill.write_text("stale local edit")
    result = skills_mod.update_skills(home=home)

    assert result["refreshed"] == list(skills_mod.PACKAGED_SKILLS)
    assert "stale local edit" not in canonical_skill.read_text()
    assert (home / ".zcode" / "skills" / "orx-agent").is_symlink()


def test_update_without_install_is_a_no_op(tmp_path):
    result = skills_mod.update_skills(home=tmp_path / "home")
    assert result["installed"] == [] and result["refreshed"] == []
    assert not (tmp_path / "home" / ".agents").exists()


# ---------------------------------------------------------------------------
# Update / install source detection


def make_fake_uv(bindir: Path, tool_dir: Path, log: Path) -> None:
    script = f"""#!/bin/sh
echo "$@" >> "{log}"
if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then
  echo "{tool_dir}"
  exit 0
fi
if [ "$1" = "tool" ] && [ "$2" = "upgrade" ]; then
  echo "upgraded orx-agent"
  exit 0
fi
exit 1
"""
    path = bindir / "uv"
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def fake_uv(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tool_dir = tmp_path / "uv-tools"
    log = tmp_path / "uv.log"
    make_fake_uv(bindir, tool_dir, log)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return {"tool_dir": tool_dir, "log": log, "prefix": str(tool_dir / "orx-agent")}


def _write_receipt(tool_env: Path, editable: bool = False) -> None:
    # Mirrors uv's own receipt format.
    tool_env.mkdir(parents=True, exist_ok=True)
    requirement = (
        'requirements = [{ name = "orx-agent", editable = "/src/orx" }]'
        if editable else 'requirements = [{ name = "orx-agent" }]'
    )
    (tool_env / "uv-receipt.toml").write_text(
        f'[tool]\n{requirement}\nentrypoints = []\n'
    )


def test_detect_uv_tool_install(fake_uv):
    _write_receipt(fake_uv["tool_dir"] / "orx-agent")
    detected = update_mod.detect_install_source(prefix=fake_uv["prefix"])
    assert detected.source == "uv-tool"
    assert detected.can_upgrade


def test_detect_editable_uv_tool_install(fake_uv):
    _write_receipt(fake_uv["tool_dir"] / "orx-agent", editable=True)
    detected = update_mod.detect_install_source(prefix=fake_uv["prefix"])
    assert detected.source == "editable"
    assert not detected.can_upgrade


def test_detect_unknown_when_prefix_differs(fake_uv):
    _write_receipt(fake_uv["tool_dir"] / "orx-agent")
    detected = update_mod.detect_install_source(prefix=sys.prefix)
    assert detected.source == "unknown"


def test_detect_unknown_without_receipt(fake_uv):
    detected = update_mod.detect_install_source(prefix=fake_uv["prefix"])
    assert detected.source == "unknown"


def test_update_check_reports_command_without_running(fake_uv):
    _write_receipt(fake_uv["tool_dir"] / "orx-agent")
    result = update_mod.check_update(prefix=fake_uv["prefix"])
    assert result["source"] == "uv-tool"
    assert result["command"] == "uv tool upgrade orx-agent"
    assert fake_uv["log"].read_text() == "tool dir\n"  # only the probe ran


def test_update_runs_upgrade_for_uv_tool(fake_uv):
    _write_receipt(fake_uv["tool_dir"] / "orx-agent")
    result = update_mod.run_update(prefix=fake_uv["prefix"])
    assert result["ok"] is True
    assert fake_uv["log"].read_text() == "tool dir\ntool upgrade orx-agent\n"


def test_update_refuses_editable_install(fake_uv):
    _write_receipt(fake_uv["tool_dir"] / "orx-agent", editable=True)
    with pytest.raises(ORXError) as excinfo:
        update_mod.run_update()
    assert "editable" in str(excinfo.value)
    assert "upgrade" not in fake_uv["log"].read_text().replace("tool dir", "")


def test_update_refuses_unknown_source(fake_uv):
    with pytest.raises(ORXError):
        update_mod.run_update()
