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


# -- G009 T007: execution-capability preflight and permission fingerprint ---
#
# The 2026-10-07 incident: the controller's preflight could not see Cursor's
# INTERNAL execution permissions. The real config was approvalMode=allowlist
# with shell allowlist Shell(ls) only, sandbox disabled, argv had --trust but
# no --force — `ls` ran, uv/orx/pytest were denied inside the agent. These
# tests pin the preflight that reads the real config and fingerprints the
# axes WITHOUT ever claiming internal shell execution.


INCIDENT_CURSOR_CONFIG = {
    "approvalMode": "allowlist",
    "sandbox": {"mode": "disabled", "networkAccess": "user_config_with_defaults"},
    "permissions": {"allow": ["Shell(ls)"], "deny": []},
    # noise that must NEVER reach a stored preflight: account identifiers
    # and everything else the config file happens to carry
    "authInfo": {"authId": "auth-secret-1", "displayName": "Alice Example",
                 "email": "alice@example.com", "userId": "user-42"},
    "autoReviewAvailabilityCache": {"authCacheKey": "cache-secret-9",
                                    "available": True},
    "modelParameters": {"gpt-5.3-codex": {"effort": "high"}},
    "version": 3,
}


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def write_cursor_cli_config(home: Path, payload: dict) -> Path:
    directory = home / ".cursor"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "cli-config.json"
    path.write_text(json.dumps(payload))
    return path


def cursor_cli_profile(force: bool = False, model: str = "gpt-5.3-codex",
                       effort: str = "high"):
    from orx.config import Profile
    from orx.records import Driver, Effort, Harness, ModelClass

    return Profile(
        name="cli-worker", driver=Driver.CLI, harness=Harness.CURSOR,
        model=model, model_class=ModelClass.STRONG,
        effort=Effort(effort), capabilities=("coding",), force=force,
    )


def codex_cli_profile(model: str = "gpt-5.1", effort: str = "high"):
    from orx.config import Profile
    from orx.records import Driver, Effort, Harness, ModelClass

    return Profile(
        name="codex-worker", driver=Driver.CLI, harness=Harness.CODEX,
        model=model, model_class=ModelClass.STRONG,
        effort=Effort(effort), capabilities=("coding",),
    )


def test_preflight_fingerprints_incident_axes_and_no_execution_claim(
        bindir, fake_home, tmp_path):
    install_fake_agent(bindir)
    write_cursor_cli_config(fake_home, INCIDENT_CURSOR_CONFIG)
    record = probes.build_permission_preflight(
        "cursor",
        profile=cursor_cli_profile(),
        workspace=tmp_path,
        check_commands=[".venv/bin/python -m pytest -q tests/test_probes.py"],
    )
    axes = record["axes"]
    # trust and approval are DIFFERENT axes: --trust is on, approval is a
    # restrictive allowlist — exactly the incident's combination
    assert axes["trust"]["value"] is True
    assert "argv" in axes["trust"]["source"]
    assert axes["force_yolo"]["value"] is False
    assert "profile" in axes["force_yolo"]["source"]
    assert axes["approval_mode"]["value"] == "allowlist"
    assert axes["approval_mode"]["shell_allow"] == ["Shell(ls)"]
    assert axes["approval_mode"]["shell_deny"] == []
    assert "cli-config.json" in axes["approval_mode"]["source"]
    assert axes["sandbox"]["value"] == "disabled"
    assert "cli-config.json" in axes["sandbox"]["source"]
    assert axes["project_hooks"]["value"] == "unknown"
    # the preflight states its provable scope explicitly
    assert "internal shell" in record["cannot_prove"]
    assert "axes" in record["proves"]
    serialized = json.dumps(record)
    assert "executable" not in serialized
    assert "checks_runnable" not in serialized
    # identity binding: CLI / profile / model / effort / workspace /
    # toolchain / time all ride on the record
    assert record["cli"]["version"] == "agent 2026.10.01"
    assert record["cli"]["binary"].endswith("agent")
    assert record["profile"] == "cli-worker"
    assert record["model"] == "gpt-5.3-codex"
    assert record["effort"] == "high"
    assert record["workspace"] == str(tmp_path.resolve())
    assert record["toolchain"][0]["first_token"] == ".venv/bin/python"
    assert record["probed_at"]
    assert record["fingerprint"].startswith("sha256:")


def test_preflight_stores_no_secrets_or_account_ids(bindir, fake_home, tmp_path):
    install_fake_agent(bindir)
    write_cursor_cli_config(fake_home, INCIDENT_CURSOR_CONFIG)
    record = probes.build_permission_preflight(
        "cursor", profile=cursor_cli_profile(), workspace=tmp_path)
    path = probes.persist_preflight(record)
    stored = path.read_text()
    for leak in ("alice@example.com", "user-42", "auth-secret-1",
                 "cache-secret-9", "authInfo", "modelParameters",
                 "networkAccess", "displayName"):
        assert leak not in stored, leak
    # only the fingerprint, axis values, and sources persist
    assert json.loads(stored) == record


def test_preflight_axes_move_independently(bindir, fake_home, tmp_path):
    install_fake_agent(bindir)
    write_cursor_cli_config(fake_home, INCIDENT_CURSOR_CONFIG)
    base = probes.build_permission_preflight(
        "cursor", profile=cursor_cli_profile(force=False), workspace=tmp_path)
    forced = probes.build_permission_preflight(
        "cursor", profile=cursor_cli_profile(force=True), workspace=tmp_path)
    assert forced["axes"]["force_yolo"]["value"] is True
    # flipping force touches NOTHING else: trust, approval, sandbox, and
    # hooks are separate axes with separate sources
    for axis in ("trust", "approval_mode", "sandbox", "project_hooks"):
        assert forced["axes"][axis] == base["axes"][axis]
    assert forced["fingerprint"] != base["fingerprint"]


def test_missing_config_is_unknown_not_defaulted(bindir, fake_home, tmp_path):
    install_fake_agent(bindir)
    record = probes.build_permission_preflight(
        "cursor", profile=cursor_cli_profile(), workspace=tmp_path)
    for axis in ("approval_mode", "sandbox"):
        assert record["axes"][axis]["value"] == "unknown"
        assert "absent" in record["axes"][axis]["source"]
    # a present file with the keys absent is unknown too — never a default
    # allow and never a default deny
    write_cursor_cli_config(fake_home, {"version": 1})
    present = probes.build_permission_preflight(
        "cursor", profile=cursor_cli_profile(), workspace=tmp_path)
    assert present["axes"]["approval_mode"]["value"] == "unknown"
    assert present["axes"]["sandbox"]["value"] == "unknown"
    assert "key absent" in present["axes"]["approval_mode"]["source"]


def test_controller_path_hit_is_not_internal_shell_success(
        bindir, fake_home, tmp_path):
    install_fake_agent(bindir)
    write_cursor_cli_config(fake_home, INCIDENT_CURSOR_CONFIG)
    record = probes.build_permission_preflight(
        "cursor", profile=cursor_cli_profile(), workspace=tmp_path,
        check_commands=["ls -la"],
    )
    entry = record["toolchain"][0]
    assert entry["first_token"] == "ls"
    assert entry["controller_path"]  # the controller can find it...
    # ...and the record says that proves nothing inside the agent
    assert "controller" in entry["note"].lower()
    assert "not" in entry["note"].lower()
    assert "controller" in record["cannot_prove"]
    # the permissive-looking PATH hit softened no permission axis
    assert record["axes"]["approval_mode"]["value"] == "allowlist"


def test_help_output_and_probe_success_are_not_permission(
        bindir, fake_home, tmp_path, monkeypatch):
    install_fake_agent(bindir)
    write_cursor_cli_config(fake_home, INCIDENT_CURSOR_CONFIG)
    with_agent = probes.build_permission_preflight(
        "cursor", profile=cursor_cli_profile(), workspace=tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))  # hide the agent binary
    without_agent = probes.build_permission_preflight(
        "cursor", profile=cursor_cli_profile(), workspace=tmp_path)
    # probe success changes only the CLI identity facts...
    assert with_agent["cli"]["binary"] and without_agent["cli"]["binary"] is None
    # ...never a permission axis: config still speaks, help never did
    assert without_agent["axes"]["approval_mode"] == with_agent["axes"]["approval_mode"]
    assert without_agent["axes"]["sandbox"] == with_agent["axes"]["sandbox"]
    assert without_agent["axes"]["trust"] == with_agent["axes"]["trust"]
    assert "argv" in with_agent["axes"]["trust"]["source"]
    assert "--help" not in json.dumps(with_agent)


def test_fingerprint_invalidates_on_every_bound_change(bindir, fake_home, tmp_path):
    install_fake_agent(bindir)
    config = write_cursor_cli_config(fake_home, INCIDENT_CURSOR_CONFIG)
    profile = cursor_cli_profile()
    kwargs = {"profile": profile, "workspace": tmp_path,
              "check_commands": [".venv/bin/python -m pytest -q"]}
    base = probes.build_permission_preflight("cursor", **kwargs)
    same = probes.build_permission_preflight("cursor", **kwargs)
    assert same["fingerprint"] == base["fingerprint"]
    assert probes.preflight_invalid_reasons(base, same) == []

    # config axis value change: new fingerprint, prior result invalid
    config.write_text(json.dumps(
        dict(INCIDENT_CURSOR_CONFIG, approvalMode="unrestricted")))
    changed = probes.build_permission_preflight("cursor", **kwargs)
    assert changed["axes"]["approval_mode"]["value"] == "unrestricted"
    assert changed["fingerprint"] != base["fingerprint"]
    assert probes.preflight_invalid_reasons(base, changed)

    # same content, bumped mtime: fingerprint holds, the mtime tripwire fires
    config.write_text(json.dumps(INCIDENT_CURSOR_CONFIG))
    stat = os.stat(config)
    os.utime(config, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    touched = probes.build_permission_preflight("cursor", **kwargs)
    assert touched["fingerprint"] == base["fingerprint"]
    reasons = probes.preflight_invalid_reasons(base, touched)
    assert reasons and "mtime" in reasons[0]

    # profile identity change (model carries effort with it here)
    other_profile = probes.build_permission_preflight(
        "cursor", profile=cursor_cli_profile(model="composer-2.5"),
        workspace=tmp_path)
    assert other_profile["fingerprint"] != base["fingerprint"]
    other_effort = probes.build_permission_preflight(
        "cursor", profile=cursor_cli_profile(effort="low"), workspace=tmp_path)
    assert other_effort["fingerprint"] != base["fingerprint"]

    # workspace change
    other_ws = probes.build_permission_preflight(
        "cursor", profile=profile, workspace=tmp_path / "ws2")
    assert other_ws["fingerprint"] != base["fingerprint"]

    # toolchain change
    other_tool = probes.build_permission_preflight(
        "cursor", profile=profile, workspace=tmp_path,
        check_commands=["uv run pytest -q"])
    assert other_tool["fingerprint"] != base["fingerprint"]

    # CLI version change
    make_bin(bindir, "agent", """
if [ "$1" = "--version" ]; then echo "agent 2026.11.01"; exit 0; fi
if [ "$1" = "--help" ]; then echo "--print --output-format --workspace --trust --model"; exit 0; fi
if [ "$1" = "status" ]; then echo "Logged in as user@example.com"; exit 0; fi
exit 1
""")
    bumped_cli = probes.build_permission_preflight("cursor", **kwargs)
    assert bumped_cli["fingerprint"] != base["fingerprint"]

    # persistence roundtrip under the user data dir
    path = probes.persist_preflight(base)
    assert probes.load_preflight("cursor", "cli-worker") == base
    data_root = Path(os.environ["ORX_DATA_DIR"])
    assert data_root in path.parents
    assert probes.load_preflight("cursor", "nope") is None


def test_preflight_never_clears_quota_cooldown_or_override(
        bindir, fake_home, tmp_path, monkeypatch):
    from conftest import make_project
    from orx.records import ResourceStatus

    install_fake_agent(bindir)
    write_cursor_cli_config(fake_home, INCIDENT_CURSOR_CONFIG)
    monkeypatch.chdir(tmp_path)
    project = make_project(tmp_path)
    try:
        store = project.store
        store.resource_learn("cli-worker", status="exhausted",
                             quota_reset_at="2026-10-07 04:24:23")
        store.resource_set("cli-worker", ResourceStatus.EXHAUSTED,
                           note="operator hold")
        before = store.resource_row("cli-worker")
        probes.build_permission_preflight(
            "cursor", profile=cursor_cli_profile(), workspace=project.root)
        after = store.resource_row("cli-worker")
        assert after.status == before.status == "exhausted"
        assert after.quota_reset_at == "2026-10-07 04:24:23"
        assert after.override == 1
    finally:
        project.close()


def test_preflight_rejects_non_probeable_harnesses(bindir, fake_home):
    with pytest.raises(ValueError, match="host-only|no permission preflight"):
        probes.build_permission_preflight("zcode")
    with pytest.raises(ValueError, match="no permission preflight"):
        probes.build_permission_preflight("shell")
    with pytest.raises(KeyError):
        probes.build_permission_preflight("nope")


def test_codex_preflight_reads_real_axes(bindir, fake_home, tmp_path, monkeypatch):
    install_fake_codex(bindir)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    (codex_home / "config.toml").write_text(
        'approval_policy = "on-request"\n\n'
        f'[projects."{tmp_path}"]\ntrust_level = "trusted"\n'
    )
    (codex_home / "hooks.json").write_text(json.dumps(
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command",
                                                 "command": "secret-cmd"}]}]}}))
    record = probes.build_permission_preflight(
        "codex", profile=codex_cli_profile(), role="worker",
        workspace=tmp_path, check_commands=[".venv/bin/python -m pytest -q"])
    axes = record["axes"]
    # sandbox is an argv fact for codex (-s), approval/trust are config facts
    assert axes["sandbox"]["value"] == "workspace-write"
    assert "argv" in axes["sandbox"]["source"]
    assert axes["approval_mode"]["value"] == "on-request"
    assert axes["trust"]["value"] == "trusted"
    assert "projects" in axes["trust"]["source"]
    assert axes["force_yolo"]["value"] is False
    # hooks count by event name; the hook commands never reach the record
    assert axes["project_hooks"]["value"] == {"configured": True,
                                              "events": ["SessionStart"]}
    assert "secret-cmd" not in json.dumps(record)
    # verifier role flips only the argv sandbox axis
    verifier = probes.build_permission_preflight(
        "codex", profile=codex_cli_profile(), role="verifier", workspace=tmp_path)
    assert verifier["axes"]["sandbox"]["value"] == "read-only"
    assert verifier["fingerprint"] != record["fingerprint"]
    # a CODEX_HOME without config.toml: config axes unknown, argv facts hold
    (codex_home / "config.toml").unlink()
    empty = probes.build_permission_preflight(
        "codex", profile=codex_cli_profile(), workspace=tmp_path)
    assert empty["axes"]["approval_mode"]["value"] == "unknown"
    assert empty["axes"]["trust"]["value"] == "unknown"
    assert empty["axes"]["sandbox"]["value"] == "workspace-write"


def test_doctor_permission_checks_show_axes_without_defaults(fake_home):
    from orx.doctor import run_doctor

    triplets = probes.doctor_permission_checks()
    by_name = {name: (state, detail) for name, state, detail in triplets}
    assert set(by_name) == {"permissions:cursor", "permissions:codex"}
    # no config anywhere: unknown is a warning, never a default, never ok
    for name, (state, detail) in by_name.items():
        assert state == "warn"
        assert "unknown" in detail
    write_cursor_cli_config(fake_home, INCIDENT_CURSOR_CONFIG)
    (fake_home / ".codex").mkdir()
    (fake_home / ".codex" / "config.toml").write_text(
        'approval_policy = "on-request"\n')
    triplets = probes.doctor_permission_checks()
    by_name = {name: (state, detail) for name, state, detail in triplets}
    cursor_state, cursor_detail = by_name["permissions:cursor"]
    assert cursor_state == "ok"
    assert "allowlist" in cursor_detail and "disabled" in cursor_detail
    assert "argv" in cursor_detail  # trust/force flagged as separate axes
    codex_state, codex_detail = by_name["permissions:codex"]
    assert codex_state == "ok" and "on-request" in codex_detail
    # run_doctor carries the checks (read-only doctor invocation)
    summary = run_doctor(None)
    names = {c["name"] for c in summary["checks"]}
    assert {"permissions:cursor", "permissions:codex"} <= names
