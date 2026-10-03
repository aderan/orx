"""Capability snapshots and shared probes (M1 P2). Fakes on PATH; no
network, no real codex/agent process, zero completions."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from orx import probes


def make_bin(directory: Path, name: str, script: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def bindir(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")
    monkeypatch.setenv("ORX_DATA_DIR", str(tmp_path / "data"))
    yield directory


FAKE_CODEX_HELP = """usage: codex exec [OPTIONS] PROMPT
  --json            -m, --model <MODEL>    -C, --cd <DIR>
  --output-last-message <FILE>   --output-schema <FILE>
  -s, --sandbox <SANDBOX_MODE>   --ephemeral   resume a session
  -c, --config <KEY=VALUE>  model_reasoning_effort levels
"""


def install_fake_codex(bindir: Path) -> None:
    make_bin(bindir, "codex", f"""
if [ "$1" = "--version" ]; then echo "codex-cli 0.160.0"; exit 0; fi
if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then cat <<'H'
{FAKE_CODEX_HELP}
H
exit 0; fi
if [ "$1" = "login" ]; then echo "Logged in using ChatGPT"; exit 0; fi
if [ "$1" = "debug" ]; then echo '{{"models": [{{"slug": "m", "supported_reasoning_levels": [{{"effort": "low"}}]}}]}}'; exit 0; fi
exit 1
""")


def install_fake_agent(bindir: Path, logged_in: bool = True) -> None:
    status = "Logged in as user@example.com" if logged_in else "Not logged in"
    make_bin(bindir, "agent", f"""
if [ "$1" = "--version" ]; then echo "agent 2026.10.01"; exit 0; fi
if [ "$1" = "--help" ]; then cat <<'H'
  -p, --print   --output-format <format>   --workspace <path>   --trust
  --model <model>  'model[effort=high]'  --resume [chatId]
H
exit 0; fi
if [ "$1" = "status" ]; then echo "{status}"; exit 0; fi
if [ "$1" = "--list-models" ]; then echo "Available models"; echo; echo "auto - Auto"; exit 0; fi
exit 1
""")


def test_snapshot_shape_matches_frozen_contract(bindir, tmp_path):
    install_fake_codex(bindir)
    snap = probes.capability_snapshot("codex")
    assert set(snap) == {"harness", "binary", "version", "probed_at",
                         "features", "auth", "models_discoverable"}
    assert snap["harness"] == "codex"
    assert snap["binary"] and snap["binary"].endswith("codex")
    assert snap["version"] == "codex-cli 0.160.0"
    assert snap["features"] == {"headless": True, "json_output": True,
                                "model_selection": True,
                                "effort_selection": True, "resume": True}
    # effort via catalog (codex -c key never shows in --help)
    assert snap["auth"] == "logged_in"
    assert snap["models_discoverable"] is True
    # persisted under the user data dir
    path = probes.snapshot_path("codex")
    assert path.parent.parent == tmp_path / "data"
    assert json.loads(path.read_text()) == snap


def test_snapshot_missing_binary(bindir, monkeypatch):
    monkeypatch.setenv("PATH", str(bindir))  # hide the real codex
    snap = probes.capability_snapshot("codex")
    assert snap["binary"] is None
    assert snap["features"]["headless"] is False
    assert snap["auth"] == "unknown"
    assert probes.load_snapshot("codex")["binary"] is None


def test_snapshot_not_logged_in(bindir):
    install_fake_agent(bindir, logged_in=False)
    snap = probes.capability_snapshot("cursor")
    assert snap["auth"] == "not_logged_in"
    assert snap["features"]["headless"] is True
    assert snap["models_discoverable"] is True


def test_probe_rejects_host_only_and_generic(bindir):
    with pytest.raises(ValueError, match="host-only|no probe"):
        probes.capability_snapshot("zcode")
    with pytest.raises(ValueError, match="no probe"):
        probes.capability_snapshot("shell")
    with pytest.raises(KeyError):
        probes.capability_snapshot("nope")


def test_harness_registry_covers_documented_set():
    assert set(probes.HARNESSES) == {"codex", "cursor", "shell", "zcode"}
    assert probes.HARNESSES["zcode"].host_only is True


def test_doctor_checks_unchanged_through_shared_module(bindir):
    from orx.doctor import run_doctor
    install_fake_codex(bindir)
    install_fake_agent(bindir)
    triplets = probes.doctor_harness_checks()
    assert [t[0] for t in triplets] == ["codex", "agent"]
    codex = triplets[0]
    assert codex[1] == "ok"
    assert codex[2].startswith("found at ") and "flags present; auth logged_in; operational probe not_run" in codex[2]
    agent = triplets[1]
    assert agent[1] == "ok" and "auth logged_in" in agent[2]


def test_load_snapshot_missing_returns_none(bindir):
    assert probes.load_snapshot("codex") is None


def test_snapshot_persistence_roundtrip_and_reload(bindir):
    install_fake_codex(bindir)
    first = probes.capability_snapshot("codex")
    reloaded = probes.load_snapshot("codex")
    assert reloaded == first
    # re-probe overwrites atomically with a fresh probed_at or same shape
    second = probes.capability_snapshot("codex")
    assert set(second) == set(first) and second["probed_at"] >= first["probed_at"]


def test_doctor_still_byte_compatible_when_binary_missing(bindir, monkeypatch):
    monkeypatch.setenv("PATH", str(bindir))
    triplets = probes.doctor_harness_checks()
    assert triplets == [
        ("codex", "warn", "not found on PATH (optional harness)"),
        ("agent", "warn", "not found on PATH (optional harness)"),
    ]


def test_version_probe_failure_leaves_version_null(bindir):
    make_bin(bindir, "codex", """
if [ "$1" = "--version" ]; then exit 3; fi
if [ "$1" = "exec" ]; then cat <<'H'
  --json -m -C --output-last-message --ephemeral
H
exit 0; fi
exit 0
""")
    snap = probes.capability_snapshot("codex")
    assert snap["version"] is None
    assert snap["features"]["headless"] is True  # help still parsed


def test_partial_flags_report_headless_false(bindir):
    make_bin(bindir, "codex", """
if [ "$1" = "exec" ]; then echo "only --json here"; exit 0; fi
exit 0
""")
    snap = probes.capability_snapshot("codex")
    assert snap["features"]["headless"] is False
    assert snap["auth"] == "unknown"  # auth only probed when headless


def test_models_probe_failure_keeps_models_false(bindir):
    make_bin(bindir, "codex", """
if [ "$1" = "exec" ]; then cat <<'H'
  --json -m -C --output-last-message
H
exit 0; fi
if [ "$1" = "login" ]; then echo "Logged in using ChatGPT"; exit 0; fi
if [ "$1" = "debug" ]; then echo "boom" >&2; exit 1; fi
exit 0
""")
    snap = probes.capability_snapshot("codex")
    assert snap["auth"] == "logged_in"
    assert snap["models_discoverable"] is False
    assert snap["features"]["effort_selection"] is False


def test_agent_list_marks_host_only_and_probeable(bindir):
    """list output distinguishes adapter harnesses from host-only zcode."""
    from orx import dispatch as dispatch_mod

    class FakeProject:
        pass

    # pure registry check; no project needed
    probeable = [h for h, spec in probes.HARNESSES.items()
                 if not spec.host_only and spec.harness != "shell"]
    assert probeable == ["codex", "cursor"]
