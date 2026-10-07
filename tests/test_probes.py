"""Capability snapshots and shared probes (M1 P2; catalog G008 T002). Fakes
on PATH; no network, no real codex/agent process, zero completions."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from orx import model_catalog, probes


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


def install_fake_agent(bindir: Path, logged_in: bool = True, models_ok: bool = True) -> None:
    status = "Logged in as user@example.com" if logged_in else "Not logged in"
    models = 'echo "auto - Auto"; exit 0' if models_ok else "exit 1"
    make_bin(bindir, "agent", f"""
if [ "$1" = "--version" ]; then echo "agent 2026.10.01"; exit 0; fi
if [ "$1" = "--help" ]; then cat <<'H'
  -p, --print   --output-format <format>   --workspace <path>   --trust
  --model <model>  'model[effort=high]'  --resume [chatId]
H
exit 0; fi
if [ "$1" = "status" ]; then echo "{status}"; exit 0; fi
if [ "$1" = "--list-models" ]; then {models}; fi
exit 1
""")


def install_real_shaped_fake_agent(bindir: Path) -> None:
    """The live CLI's --list-models / --help shape, shrunk but faithful:
    header, blank lines, (current)/(default) markers, zero-width noise,
    and the parameterized bracket tip."""
    make_bin(bindir, "agent", """
if [ "$1" = "--version" ]; then echo "agent 2026.10.01"; exit 0; fi
if [ "$1" = "--help" ]; then cat <<'H'
  -p, --print   --output-format <format>   --workspace <path>   --trust
  --model <model>  Model to use (e.g., gpt-5, sonnet-4-thinking).
                   Parameterized models accept quoted bracket overrides,
                   e.g. 'claude-opus-4-8[context=1m,effort=high,fast=false]'
  --resume [chatId]
H
exit 0; fi
if [ "$1" = "status" ]; then echo "Logged in as user@example.com"; exit 0; fi
if [ "$1" = "--list-models" ]; then printf 'Available models\\n\\nauto - Auto (default)\\ngrok-4.7-high - Grok 4.7 High (current)\\ngrok-4.7-high-fast - Grok 4.7 High Fast\\342\\200\\213\\342\\200\\213\\n\\nTip: use --model <id> to switch. Parameterized models also accept quoted overrides, e.g. --model '\\''claude-opus-4-8[context=1m,effort=high,fast=false]'\\''.\\n'; exit 0; fi
exit 1
""")


def test_snapshot_shape_matches_frozen_contract(bindir, tmp_path):
    install_fake_codex(bindir)
    snap = probes.capability_snapshot("codex")
    assert set(snap) == {"harness", "binary", "version", "probed_at",
                         "features", "auth", "models_discoverable",
                         "model_catalog", "catalog_error"}
    assert snap["harness"] == "codex"
    assert snap["binary"] and snap["binary"].endswith("codex")
    assert snap["version"] == "codex-cli 0.160.0"
    assert snap["features"] == {"headless": True, "json_output": True,
                                "model_selection": True,
                                "effort_selection": True, "resume": True}
    # effort via catalog (codex -c key never shows in --help)
    assert snap["auth"] == "logged_in"
    assert snap["models_discoverable"] is True
    # G008: the structured catalog rides inside the same snapshot
    catalog = snap["model_catalog"]
    assert catalog["harness"] == "codex"
    assert catalog["source"] == "codex debug models"
    assert catalog["generation"] == 1
    assert catalog["generated_at"] == snap["probed_at"]
    assert [m["slug"] for m in catalog["models"]] == ["m"]
    assert catalog["models"][0]["efforts"] == ["low"]
    assert snap["catalog_error"] is None
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
    # missing binary is a catalog discovery failure, honestly recorded
    assert snap["model_catalog"] is None
    assert "not found" in snap["catalog_error"]
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
    # non-zero exit is a recorded discovery failure, not a silent none
    assert snap["model_catalog"] is None
    assert snap["catalog_error"] == "models probe exited 1"


def test_models_probe_invalid_output_is_discovery_failure(bindir):
    make_bin(bindir, "codex", """
if [ "$1" = "exec" ]; then cat <<'H'
  --json -m -C --output-last-message
H
exit 0; fi
if [ "$1" = "login" ]; then echo "Logged in using ChatGPT"; exit 0; fi
if [ "$1" = "debug" ]; then echo "looks fine but is not JSON"; exit 0; fi
exit 0
""")
    snap = probes.capability_snapshot("codex")
    # exit 0 with unparseable output must not produce a successful catalog
    assert snap["models_discoverable"] is False
    assert snap["model_catalog"] is None
    assert "not valid JSON" in snap["catalog_error"]


def test_models_probe_timeout_is_recorded_failure(bindir, monkeypatch):
    monkeypatch.setattr(probes, "MODELS_PROBE_TIMEOUT", 0.2)
    make_bin(bindir, "codex", """
if [ "$1" = "exec" ]; then cat <<'H'
  --json -m -C --output-last-message
H
exit 0; fi
if [ "$1" = "login" ]; then echo "Logged in using ChatGPT"; exit 0; fi
if [ "$1" = "debug" ]; then sleep 1; echo '{"models": []}'; exit 0; fi
exit 0
""")
    snap = probes.capability_snapshot("codex")
    assert snap["models_discoverable"] is False
    assert snap["model_catalog"] is None
    assert "timed out" in snap["catalog_error"]


def test_valid_empty_catalog_is_discoverable_and_distinct_from_failure(bindir):
    make_bin(bindir, "codex", """
if [ "$1" = "exec" ]; then cat <<'H'
  --json -m -C --output-last-message
H
exit 0; fi
if [ "$1" = "login" ]; then echo "Logged in using ChatGPT"; exit 0; fi
if [ "$1" = "debug" ]; then echo '{"models": []}'; exit 0; fi
exit 0
""")
    snap = probes.capability_snapshot("codex")
    # a valid empty catalog is a success with zero models — not a failure
    assert snap["models_discoverable"] is True
    assert snap["catalog_error"] is None
    assert snap["model_catalog"]["models"] == []
    assert snap["model_catalog"]["generation"] == 1


def test_auth_failure_does_not_fabricate_a_catalog(bindir):
    """Auth and the catalog are orthogonal: whatever the models probe
    returned is parsed; an auth failure never invents a catalog."""
    install_fake_agent(bindir, logged_in=False, models_ok=False)
    snap = probes.capability_snapshot("cursor")
    assert snap["auth"] == "not_logged_in"
    assert snap["models_discoverable"] is False
    assert snap["model_catalog"] is None
    assert snap["catalog_error"] == "models probe exited 1"


def test_cursor_catalog_marks_current_default_and_parameterized(bindir):
    install_real_shaped_fake_agent(bindir)
    snap = probes.capability_snapshot("cursor")
    catalog = snap["model_catalog"]
    slugs = [m["slug"] for m in catalog["models"]]
    assert slugs == ["auto", "grok-4.7-high", "grok-4.7-high-fast"]
    by_slug = {m["slug"]: m for m in catalog["models"]}
    assert by_slug["grok-4.7-high"]["is_current"] is True
    assert by_slug["auto"]["is_default"] is True
    assert by_slug["grok-4.7-high-fast"]["family"] == "grok-4.7"
    assert by_slug["grok-4.7-high-fast"]["speeds"] == ["fast"]
    # parameterized syntax evidence is separate from enumerated models
    parameterized = catalog["parameterized"]
    assert parameterized["available"] is True
    assert set(parameterized["param_keys"]) == {"context", "effort", "fast"}
    assert "claude-opus-4-8" in parameterized["example_families"]
    assert "claude-opus-4-8" not in slugs
    assert snap["catalog_error"] is None


def test_catalog_generation_advances_on_successful_refresh(bindir):
    install_fake_codex(bindir)
    first = probes.capability_snapshot("codex")
    assert first["model_catalog"]["generation"] == 1
    second = probes.capability_snapshot("codex")
    assert second["model_catalog"]["generation"] == 2
    # the same-process cache follows the new generation
    cached = model_catalog.cached_catalog("codex")
    assert cached is not None and cached.generation == 2


def test_refresh_failure_never_relabels_old_catalog_as_fresh(bindir):
    install_fake_codex(bindir)
    good = probes.capability_snapshot("codex")
    assert good["model_catalog"] is not None
    old_generated_at = good["model_catalog"]["generated_at"]
    # the catalog breaks on the next refresh
    make_bin(bindir, "codex", """
if [ "$1" = "--version" ]; then echo "codex-cli 0.160.0"; exit 0; fi
if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then cat <<'H'
  --json -m -C --output-last-message model_reasoning_effort resume
H
exit 0; fi
if [ "$1" = "login" ]; then echo "Logged in using ChatGPT"; exit 0; fi
if [ "$1" = "debug" ]; then echo '{"models": [}}' ; exit 0; fi
exit 1
""")
    refreshed = probes.capability_snapshot("codex")
    assert refreshed["models_discoverable"] is False
    assert refreshed["model_catalog"] is None
    assert "not valid JSON" in refreshed["catalog_error"]
    # persisted state carries no catalog that could pass as a fresh copy
    # of the old one (neither old models with a new timestamp nor an
    # ok-status): the failed refresh dropped it and kept the reason
    persisted = probes.load_snapshot("codex")
    assert persisted["model_catalog"] is None
    assert persisted["catalog_error"] == refreshed["catalog_error"]
    assert old_generated_at  # the old catalog existed and was real
    # the same-process cache no longer serves the stale catalog
    assert model_catalog.cached_catalog("codex") is None


def test_old_format_snapshot_stays_readable_and_refresh_restarts(bindir):
    # a pre-G008 snapshot (no catalog keys) on disk
    legacy = {
        "harness": "codex", "binary": "/old/codex", "version": "codex-cli 0.1.0",
        "probed_at": "2026-01-01T00:00:00+00:00",
        "features": {"headless": True, "json_output": True,
                     "model_selection": True, "effort_selection": True,
                     "resume": False},
        "auth": "logged_in", "models_discoverable": True,
    }
    path = probes.snapshot_path("codex")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy))
    assert probes.load_snapshot("codex") == legacy
    assert model_catalog.cached_catalog("codex") is None
    # a successful refresh after the legacy snapshot starts at generation 1
    install_fake_codex(bindir)
    snap = probes.capability_snapshot("codex")
    assert snap["model_catalog"]["generation"] == 1


def test_large_codex_catalog_is_not_truncated(bindir, tmp_path):
    # the real codex dump is >256 KiB; the probe must read all of it
    models = [
        {"slug": f"family-{i:04d}", "display_name": f"F {i}",
         "description": "y" * 80,
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
         "context_window": 272000}
        for i in range(1100)
    ]
    payload = json.dumps({"models": models})
    assert len(payload) > 256 * 1024
    dump = tmp_path / "big-catalog.json"
    dump.write_text(payload)
    make_bin(bindir, "codex", f"""
if [ "$1" = "exec" ]; then cat <<'H'
  --json -m -C --output-last-message model_reasoning_effort resume
H
exit 0; fi
if [ "$1" = "login" ]; then echo "Logged in using ChatGPT"; exit 0; fi
if [ "$1" = "debug" ]; then cat '{dump}'; exit 0; fi
exit 1
""")
    snap = probes.capability_snapshot("codex")
    assert snap["models_discoverable"] is True
    catalog = snap["model_catalog"]
    assert len(catalog["models"]) == 1100
    assert catalog["models"][-1]["slug"] == "family-1099"


def test_agent_list_marks_host_only_and_probeable(bindir):
    """list output distinguishes adapter harnesses from host-only zcode."""
    from orx import dispatch as dispatch_mod

    class FakeProject:
        pass

    # pure registry check; no project needed
    probeable = [h for h, spec in probes.HARNESSES.items()
                 if not spec.host_only and spec.harness != "shell"]
    assert probeable == ["codex", "cursor"]
