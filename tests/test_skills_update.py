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
# Optional per-name install (fake packaged dir; no dependency on skills/orx-pbv)


def fake_packaged_skills(tmp_path, monkeypatch, names=("orx-controller", "orx-agent", "orx-pbv")):
    root = tmp_path / "packaged-skills"
    for name in names:
        skill = root / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name} skill\n")
    monkeypatch.setattr(skills_mod, "packaged_skills_dir", lambda: root)
    return root


def test_available_skills_requires_skill_md(tmp_path, monkeypatch):
    root = fake_packaged_skills(tmp_path, monkeypatch, names=("orx-agent",))
    (root / "not-a-skill").mkdir()           # no SKILL.md -> not available
    (root / "README.md").write_text("file")  # not a directory
    assert skills_mod.available_skills() == ["orx-agent"]


def test_install_no_names_installs_exactly_default_set(tmp_path, monkeypatch):
    fake_packaged_skills(tmp_path, monkeypatch)  # orx-pbv is packaged too
    home = tmp_path / "home"
    (home / ".zcode" / "skills").mkdir(parents=True)

    result = skills_mod.install_skills(home=home)

    canonical = home / ".agents" / "skills"
    assert sorted(result["installed"]) == ["orx-agent", "orx-controller"]
    assert result["refreshed"] == []
    for name in ("orx-controller", "orx-agent"):
        assert (canonical / name / "SKILL.md").is_file()
        assert (home / ".zcode" / "skills" / name).is_symlink()
    assert not (canonical / "orx-pbv").exists()  # default set only
    assert not (home / ".zcode" / "skills" / "orx-pbv").exists()


def test_install_explicit_name_installs_only_that_skill(tmp_path, monkeypatch):
    fake_packaged_skills(tmp_path, monkeypatch)
    home = tmp_path / "home"
    (home / ".zcode" / "skills").mkdir(parents=True)

    result = skills_mod.install_skills(home=home, names=["orx-pbv"])

    canonical = home / ".agents" / "skills"
    assert result["installed"] == ["orx-pbv"]
    assert (canonical / "orx-pbv" / "SKILL.md").is_file()
    link = home / ".zcode" / "skills" / "orx-pbv"
    assert link.is_symlink()
    assert link.resolve() == (canonical / "orx-pbv").resolve()
    for name in ("orx-controller", "orx-agent"):
        assert not (canonical / name).exists()


def test_install_multiple_names(tmp_path, monkeypatch):
    fake_packaged_skills(tmp_path, monkeypatch)
    home = tmp_path / "home"

    result = skills_mod.install_skills(home=home, names=["orx-pbv", "orx-agent"])

    canonical = home / ".agents" / "skills"
    assert sorted(result["installed"]) == ["orx-agent", "orx-pbv"]
    assert (canonical / "orx-pbv" / "SKILL.md").is_file()
    assert (canonical / "orx-agent" / "SKILL.md").is_file()
    assert not (canonical / "orx-controller").exists()


def test_reinstall_explicit_name_replaces_stale_content(tmp_path, monkeypatch):
    fake_packaged_skills(tmp_path, monkeypatch)
    home = tmp_path / "home"
    skills_mod.install_skills(home=home, names=["orx-pbv"])

    canonical_skill = home / ".agents" / "skills" / "orx-pbv" / "SKILL.md"
    packaged = skills_mod.packaged_skills_dir() / "orx-pbv" / "SKILL.md"
    canonical_skill.write_text("stale local edit")

    result = skills_mod.install_skills(home=home, names=["orx-pbv"])

    assert result["installed"] == [] and result["refreshed"] == ["orx-pbv"]
    assert canonical_skill.read_text() == packaged.read_text()


def test_install_unknown_name_lists_available_skills(tmp_path, monkeypatch):
    fake_packaged_skills(tmp_path, monkeypatch)
    with pytest.raises(ORXError) as excinfo:
        skills_mod.install_skills(home=tmp_path / "home", names=["nope"])
    message = str(excinfo.value)
    assert "nope" in message
    for available in ("orx-controller", "orx-agent", "orx-pbv"):
        assert available in message
    assert not (tmp_path / "home" / ".agents").exists()  # rejected before any copy


@pytest.mark.parametrize("hostile", ["../evil", "a/b", ".hidden"])
def test_install_rejects_hostile_names(tmp_path, monkeypatch, hostile):
    fake_packaged_skills(tmp_path, monkeypatch)
    with pytest.raises(ORXError) as excinfo:
        skills_mod.install_skills(home=tmp_path / "home", names=[hostile])
    assert hostile in str(excinfo.value)
    assert not (tmp_path / "home" / ".agents").exists()


def test_update_refreshes_only_installed_and_available_skills(tmp_path, monkeypatch):
    fake_packaged_skills(tmp_path, monkeypatch)
    home = tmp_path / "home"
    (home / ".zcode" / "skills").mkdir(parents=True)
    skills_mod.install_skills(home=home, names=["orx-pbv"])

    canonical_skill = home / ".agents" / "skills" / "orx-pbv" / "SKILL.md"
    packaged = skills_mod.packaged_skills_dir() / "orx-pbv" / "SKILL.md"
    canonical_skill.write_text("stale local edit")

    result = skills_mod.update_skills(home=home)

    assert result["refreshed"] == ["orx-pbv"]
    assert canonical_skill.read_text() == packaged.read_text()
    assert (home / ".zcode" / "skills" / "orx-pbv").is_symlink()
    # packaged but not installed -> update must not install them
    canonical = home / ".agents" / "skills"
    assert not (canonical / "orx-controller").exists()
    assert not (canonical / "orx-agent").exists()


def test_update_ignores_names_and_installs_nothing(tmp_path, monkeypatch):
    fake_packaged_skills(tmp_path, monkeypatch)
    home = tmp_path / "home"

    result = skills_mod.install_skills(home=home, only_update=True, names=["orx-pbv"])

    assert result["installed"] == [] and result["refreshed"] == []
    assert not (home / ".agents").exists()


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
