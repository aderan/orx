"""Distribution-channel identity and release gates (G011 T005).

Two channels, never blurred:

- internal-preview: ad-hoc, honestly labeled (channel / audience /
  Gatekeeper limits in app metadata, names, manifests, docs) — NOT a
  formal installer;
- official: real Developer ID signing (nested Python included),
  hardened runtime, notarytool, stapler, Gatekeeper assessment —
  fail-closed on missing credentials or failed checks, with NO silent
  downgrade to preview.

These tests exercise the gate logic with synthetic runners and fake
archives; they never claim official-channel passes (test doubles are
not official evidence — that is the point of the design).
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import app_distribution as ad

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_APP = REPO_ROOT / "scripts" / "build_app.sh"

FULL_COMMIT = "07ab676e3f6499f8b636354c670de7b11ac15e69"
DEVID_HASH = "A1B2C3D4E5F60718293A4B5C6D7E8F9012345678"

# The exact keychain state probed live on this machine (baseline.json):
# Apple Development + Apple Distribution, NO Developer ID Application.
SEC_NO_DEVID = (
    '  1) F99C084833F368FE85CDAF89DD6565E71CCEC7C8 '
    '"Apple Development: libin feng (QCKV9T523J)"\n'
    '  2) 3C224E0C33535D9B381BCD633E95CBA0A70C5DED '
    '"Apple Distribution: libin feng (CJK4RPXHR4)"\n'
    '     2 valid identities found\n'
)
SEC_WITH_DEVID = (
    f'  1) {DEVID_HASH} '
    '"Developer ID Application: libin feng (ABCDEFGHIJ)"\n'
    '     1 valid identities found\n'
)


def refuse(code):
    return pytest.raises(ad.Refused, match=code)


def fake_runner(responses):
    """Runner routing by (command prefix); records every call."""
    calls = []

    def runner(command, cwd=None):
        calls.append(list(command))
        for prefix, payload in responses:
            length = len(prefix)
            if list(command[:length]) == list(prefix):
                if isinstance(payload, Exception):
                    raise payload
                rc, out, err = payload
                return subprocess.CompletedProcess(command, rc, out, err)
        return subprocess.CompletedProcess(command, 0, "", "")

    runner.calls = calls
    return runner


def write_decision(path, preview="allowed", official="blocked", results=None):
    data = {
        "goal": "G011",
        "decision_file_schema": 1,
        "channels": {
            "internal-preview": {
                "mode": "dev",
                "decision": preview,
                "basis": ["no Developer ID Application certificate on the build host",
                          "no stored notarytool credentials"],
            },
            "official": {
                "mode": "dist",
                "decision": official,
                "basis": ["Developer ID Application certificate present",
                          "notarytool keychain profile stored"],
                "fail_closed": True,
            },
        },
        "results": results if results is not None else {},
    }
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


# ---------------------------------------------------------------- channel --

def test_channel_mapping_and_aliases():
    assert ad.channel_for_mode("dev") == "internal-preview"
    assert ad.channel_for_mode("internal-preview") == "internal-preview"
    assert ad.channel_for_mode("dist") == "official"
    assert ad.channel_for_mode("official") == "official"
    with refuse("unknown-mode"):
        ad.channel_for_mode("beta")


def test_emit_env_is_eval_safe_shell():
    emitted = ad.emit_env("dev")
    assignments = {}
    for line in emitted.splitlines():
        key, _, value = line.partition("=")
        assert key.isidentifier() and key.isupper()
        assignments[key] = shlex.split(value)[0] if value else ""
    assert assignments["CHANNEL"] == "internal-preview"
    assert assignments["APP_NAME"] == "ORX Analytics (Internal Preview)"
    assert assignments["BUNDLE_ID"] == "org.orxanalytics.macos.dev"
    # Names contain spaces/parens: quoting must round-trip through a
    # real shell word-split.
    assert " ".join(shlex.split(
        emitted.splitlines()[2].partition("=")[2])) == "ORX Analytics (Internal Preview)"


def test_channel_facts_differ_in_every_visible_dimension():
    preview, official = ad.CHANNEL_FACTS["internal-preview"], ad.CHANNEL_FACTS["official"]
    # App-visible identity, archive kind, signing story and Gatekeeper
    # text all differ: no dimension where preview masquerades as formal.
    assert preview["app_name"] != official["app_name"]
    assert preview["bundle_id"] != official["bundle_id"]
    assert preview["archive_suffix"] == "zip" != official["archive_suffix"]
    assert "NOT notarized" in preview["gatekeeper_notice"]
    assert "notarized" in official["gatekeeper_notice"]
    assert "internal" in preview["audience"].lower()
    assert "general users" in official["audience"]


# --------------------------------------------------------------- decision --

def test_decision_gate_requires_pre_fixed_allowed_channel(tmp_path):
    decision = tmp_path / "distribution.json"
    write_decision(decision)
    binding = ad.validate_decision(decision, "internal-preview")
    assert binding["channel"] == "internal-preview"
    assert binding["basis"] and all(binding["basis"])
    assert binding["decision_sha256"] == hashlib.sha256(
        decision.read_bytes()).hexdigest()


def test_decision_gate_refusals_are_machine_readable(tmp_path):
    decision = tmp_path / "distribution.json"
    write_decision(decision, official="blocked")
    # Missing file, invalid JSON, wrong shape.
    with refuse("decision-missing"):
        ad.validate_decision(tmp_path / "nope.json", "official")
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    with refuse("decision-invalid-json"):
        ad.validate_decision(tmp_path / "bad.json", "official")
    (tmp_path / "shape.json").write_text('{"channels": 3}', encoding="utf-8")
    with refuse("decision-invalid-shape"):
        ad.validate_decision(tmp_path / "shape.json", "official")
    # Channel without an entry.
    partial = tmp_path / "partial.json"
    data = write_decision(partial)
    del data["channels"]["official"]
    partial.write_text(json.dumps(data), encoding="utf-8")
    with refuse("decision-channel-absent"):
        ad.validate_decision(partial, "official")
    # Entry present but not allowed (this machine's official state).
    decision2 = tmp_path / "d2.json"
    write_decision(decision2, official="blocked")
    channels_only = json.loads(decision2.read_text())
    channels_only["channels"]["official"]["decision"] = "blocked"
    decision2.write_text(json.dumps(channels_only), encoding="utf-8")
    with refuse("channel-not-allowed"):
        ad.validate_decision(decision2, "official")
    # Allowed but no basis.
    decision3 = tmp_path / "d3.json"
    write_decision(decision3, preview="allowed")
    data = json.loads(decision3.read_text())
    data["channels"]["internal-preview"]["basis"] = []
    decision3.write_text(json.dumps(data), encoding="utf-8")
    with refuse("decision-basis-empty"):
        ad.validate_decision(decision3, "internal-preview")


# -------------------------------------------------------- outcome records --

def test_outcomes_record_in_separate_slots_no_cross_channel_writes(tmp_path):
    decision = tmp_path / "distribution.json"
    write_decision(decision)
    # Official failure lands in the official slot only.
    ad.apply_outcome(decision, "official", "blocked",
                     "no Developer ID / notarytool credentials",
                     evidence="docs/diagnostics/g011/baseline.json")
    data = json.loads(decision.read_text())
    assert data["results"]["official"]["status"] == "blocked"
    assert "internal-preview" not in data["results"]
    # Preview pass lands in the preview slot only; official untouched.
    ad.apply_outcome(decision, "internal-preview", "passed",
                     "labeled ad-hoc zip built and recorded")
    data = json.loads(decision.read_text())
    assert data["results"]["internal-preview"]["status"] == "passed"
    assert data["results"]["official"]["status"] == "blocked"


def test_outcome_refuses_pass_without_allowed_decision_and_unknown_inputs(tmp_path):
    decision = tmp_path / "distribution.json"
    write_decision(decision)  # official blocked
    with refuse("outcome-contradicts-decision"):
        ad.apply_outcome(decision, "official", "passed", "cannot pass")
    with refuse("unknown-channel"):
        ad.apply_outcome(decision, "beta", "passed", "?")
    with refuse("unknown-status"):
        ad.apply_outcome(decision, "internal-preview", "shipped", "?")


# ------------------------------------------------------ credential gates --

def test_preflight_refuses_without_developer_id_identity():
    runner = fake_runner([
        (["security", "find-identity"], (0, SEC_NO_DEVID, "")),
    ])
    with refuse("no-developer-id-identity") as excinfo:
        ad.preflight_official(runner=runner)
    # The refusal names what WAS found (Apple Development/Distribution
    # do not qualify) — honest diagnostics, no fix without facts.
    assert "Apple Development" in excinfo.value.detail
    assert "Apple Distribution" in excinfo.value.detail


def test_preflight_refuses_without_notarytool_credentials():
    runner = fake_runner([
        (["security", "find-identity"], (0, SEC_WITH_DEVID, "")),
        (["xcrun", "notarytool", "history"],
         (1, "", "Error: No Keychain password item found for profile: "
                 "orx-analytics-notary\n")),
    ])
    with refuse("no-notarytool-credentials"):
        ad.preflight_official(runner=runner)


def test_preflight_passes_only_with_both_credentials_and_records_no_secrets():
    runner = fake_runner([
        (["security", "find-identity"], (0, SEC_WITH_DEVID, "")),
        (["xcrun", "notarytool", "history"], (0, "history: no items\n", "")),
    ])
    result = ad.preflight_official(runner=runner)
    assert result["identity"] == DEVID_HASH
    assert result["identity_name"].startswith("Developer ID Application")
    # Only the method is recorded — never keychain contents.
    assert "password" not in json.dumps(result).lower()
    assert "keychain-profile" in result["notarization"]
    assert "not recorded" in result["notarization"]
    # The probe never passed secret material on a command line.
    for call in runner.calls:
        assert not any("password" in part.lower() for part in call)


def test_preflight_identity_selection():
    runner = fake_runner([
        (["security", "find-identity"], (0, SEC_WITH_DEVID, "")),
    ])
    with refuse("identity-name-not-found"):
        ad.preflight_official(runner=runner, preferred_identity="someone else")
    ok = ad.preflight_official(runner=runner, preferred_identity="libin feng")
    assert ok["identity"] == DEVID_HASH


# ----------------------------------------------------------- entitlements --

def test_official_entitlements_pin_hardened_runtime_needs():
    python_plist = ad.entitlements_plist("official", "python")
    assert "com.apple.security.cs.allow-unsigned-executable-memory" in python_plist
    assert "com.apple.security.cs.allow-jit" in python_plist
    app_plist = ad.entitlements_plist("official", "app")
    assert "allow-unsigned-executable-memory" in app_plist
    # Preview: no hardened runtime, therefore no entitlements to pin.
    assert ad.entitlements_plist("internal-preview", "app") == ""
    assert ad.entitlements_plist("internal-preview", "python") == ""
    with refuse("unknown-entitlements-scope"):
        ad.entitlements_plist("official", "installer")


# ------------------------------------------------------- channel metadata --

def test_channel_metadata_carries_version_full_commit_and_limits():
    info = ad.channel_info("internal-preview", "0.4.2", FULL_COMMIT)
    assert info["info_plist"]["ORXAnalyticsChannel"] == "internal-preview"
    assert info["info_plist"]["ORXAnalyticsSourceCommit"] == FULL_COMMIT
    assert "internal" in info["info_plist"]["ORXAnalyticsChannelAudience"].lower()
    assert "NOT notarized" in info["info_plist"]["ORXAnalyticsGatekeeperNotice"]
    manifest = info["in_app_manifest"]
    assert manifest["version"] == "0.4.2"
    assert manifest["source_commit"] == FULL_COMMIT
    assert manifest["gatekeeper_limit"]


def test_channel_metadata_refuses_truncated_commit_and_unknown_channel():
    with refuse("commit-not-full"):
        ad.channel_info("official", "0.4.2", "07ab676")
    with refuse("commit-not-full"):
        ad.channel_info("official", "0.4.2", "")
    with refuse("unknown-channel"):
        ad.channel_info("beta", "0.4.2", FULL_COMMIT)


# ----------------------------------------------------------- build record --

def make_archive(tmp_path, content=b"fake archive bytes"):
    archive = tmp_path / "ORXAnalytics-0.4.2-internal-preview-dev-macos13-arm64.zip"
    archive.write_bytes(content)
    return archive


def official_gates():
    return {
        "signing": {"style": "developer-id", "verified": True,
                    "hardened_runtime": True, "identity": DEVID_HASH,
                    "identity_name": "Developer ID Application: libin feng (ABCDEFGHIJ)",
                    "team_identifier": "ABCDEFGHIJ"},
        "notarization": {"status": "accepted", "submission_id": "fffffff-0000"},
        "staple": {"status": "stapled", "validated": True},
        "install_assessment": {"status": "accepted",
                               "tool": "spctl --assess --type execute",
                               "detail": "accepted source=Developer ID"},
    }


def preview_gates():
    return {
        "signing": {"style": "adhoc", "verified": True,
                    "hardened_runtime": False, "identity": None},
        "notarization": {"status": "not-performed"},
        "staple": {"status": "not-performed"},
        "install_assessment": {"status": "not-performed"},
    }


def test_official_record_hashes_archive_only_after_full_gate_chain(tmp_path):
    archive = make_archive(tmp_path)
    record = ad.build_record("official", "dist", "0.4.2", FULL_COMMIT,
                             archive, official_gates())
    assert record["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert record["channel"] == "official"
    assert record["source_commit"] == FULL_COMMIT
    assert record["gates"]["notarization"]["status"] == "accepted"
    assert record["gates"]["install_assessment"]["status"] == "accepted"


@pytest.mark.parametrize("mangle,fragment", [
    (lambda g: g["signing"].update(verified=False), "signing"),
    (lambda g: g["signing"].update(style="adhoc"), "Developer ID"),
    (lambda g: g["signing"].update(hardened_runtime=False), "hardened"),
    (lambda g: g["signing"].pop("identity"), "identity"),
    (lambda g: g["notarization"].update(status="in-progress"), "notarization"),
    (lambda g: g["staple"].update(status="not-performed"), "staple"),
    (lambda g: g["staple"].update(validated=False), "staple"),
    (lambda g: g["install_assessment"].update(status="rejected"),
     "install_assessment"),
])
def test_official_record_fails_closed_on_any_incomplete_gate(
        tmp_path, mangle, fragment):
    archive = make_archive(tmp_path)
    gates = official_gates()
    mangle(gates)
    with refuse("record-before-gates") as excinfo:
        ad.build_record("official", "dist", "0.4.2", FULL_COMMIT,
                        archive, gates)
    assert fragment in excinfo.value.detail


def test_preview_record_allows_adhoc_and_refuses_formal_claims(tmp_path):
    archive = make_archive(tmp_path)
    record = ad.build_record("internal-preview", "dev", "0.4.2", FULL_COMMIT,
                             archive, preview_gates())
    assert record["gates"]["signing"]["style"] == "adhoc"
    assert record["gatekeeper_limit"]
    assert "internal" in record["audience"].lower()
    # A preview claiming notarization/assessment is refused, not edited.
    forged = json.loads(json.dumps(preview_gates()))
    forged["notarization"]["status"] = "accepted"
    with refuse("record-claims-unavailable-gate"):
        ad.build_record("internal-preview", "dev", "0.4.2", FULL_COMMIT,
                        archive, forged)
    forged2 = json.loads(json.dumps(preview_gates()))
    forged2["install_assessment"]["status"] = "accepted"
    with refuse("record-claims-unavailable-gate"):
        ad.build_record("internal-preview", "dev", "0.4.2", FULL_COMMIT,
                        archive, forged2)
    # Preview without a verified ad-hoc signature is not recordable.
    broken = json.loads(json.dumps(preview_gates()))
    broken["signing"]["verified"] = False
    with refuse("record-before-gates"):
        ad.build_record("internal-preview", "dev", "0.4.2", FULL_COMMIT,
                        archive, broken)


def test_record_refuses_truncated_commit_missing_archive_and_secret_keys(
        tmp_path):
    archive = make_archive(tmp_path)
    with refuse("commit-not-full"):
        ad.build_record("official", "dist", "0.4.2", "07ab676",
                        archive, official_gates())
    with refuse("record-archive-missing"):
        ad.build_record("official", "dist", "0.4.2", FULL_COMMIT,
                        tmp_path / "absent.dmg", official_gates())
    leaky = official_gates()
    leaky["signing"]["keychain_password"] = "hunter2"
    with refuse("record-would-leak-secrets"):
        ad.build_record("official", "dist", "0.4.2", FULL_COMMIT,
                        archive, leaky)


def test_record_writes_file_and_decision_binding(tmp_path):
    archive = make_archive(tmp_path)
    decision = tmp_path / "distribution.json"
    write_decision(decision)
    out = tmp_path / "record.json"
    ad.build_record("internal-preview", "dev", "0.4.2", FULL_COMMIT,
                    archive, preview_gates(),
                    decision={"path": str(decision),
                              "sha256": hashlib.sha256(
                                  decision.read_bytes()).hexdigest()},
                    out_path=out)
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["channel"] == "internal-preview"
    assert written["decision"]["sha256"] == hashlib.sha256(
        decision.read_bytes()).hexdigest()


# -------------------------------------------------------------------- CLI --

def test_cli_refusal_prints_machine_json_and_exits_three(capsys):
    rc = ad.main(["preflight-official"])
    assert rc == ad.REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["refused"] is True
    assert payload["code"] == "no-developer-id-identity"
    assert payload["channel"] is None


def test_cli_channel_and_record_roundtrip(tmp_path, capsys):
    ad.main(["channel", "dist"])
    facts = json.loads(capsys.readouterr().out)
    assert facts["channel"] == "official"
    archive = make_archive(tmp_path)
    gates = tmp_path / "gates.json"
    gates.write_text(json.dumps(preview_gates()), encoding="utf-8")
    out = tmp_path / "rec.json"
    rc = ad.main(["record", "--channel", "internal-preview",
                  "--mode", "dev", "--version", "0.4.2",
                  "--commit", FULL_COMMIT, "--archive", str(archive),
                  "--gates", str(gates), "--out", str(out)])
    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["channel"] == \
        "internal-preview"


# ----------------------------------------------- build_app.sh contract ----

def test_build_script_implements_the_official_gate_chain_for_real():
    script = BUILD_APP.read_text(encoding="utf-8")
    # The official channel must run the REAL tools, not stubs.
    assert "codesign --force --sign" in script
    assert "--options runtime" in script
    assert "notarytool submit" in script
    assert "--keychain-profile" in script
    assert "--wait" in script
    assert "stapler staple" in script
    assert "stapler validate" in script
    assert "spctl --assess" in script
    # Nested signing covers the bundled Python runtime.
    assert "Resources/python" in script
    assert "Mach-O" in script
    # The decision file gates the official build before any work.
    assert "validate-decision" in script
    assert "decision-missing" in script
    assert "preflight-official" in script
    # Quarantine removal is NEVER a method (not even present).
    assert "xattr" not in script
    # No self-recursion: an official failure cannot re-run as preview.
    assert 'sh "$0"' not in script
    assert '"$0" dev' not in script
    assert '"$0" dist' not in script
    # The preview channel is labeled in the script's own output.
    assert "NOT a formal installer" in script


def test_build_script_names_and_records_carry_the_channel():
    script = BUILD_APP.read_text(encoding="utf-8")
    # Archive names carry channel + mode tokens (joint-release glob
    # contract: ORXAnalytics-*-<mode>-macos13-arm64.<suffix>).
    assert "ORXAnalytics-${VERSION}-${CHANNEL}-${MODE}-macos13-arm64" in script
    # App-visible Info.plist channel keys and in-app manifest.
    assert "ORXAnalyticsChannel" in script
    assert "ORXAnalyticsSourceCommit" in script
    assert "distribution.json" in script
    # A per-archive build record is always written.
    assert '"$ARCHIVE.json"' in script
    # Full commit required before heavy work.
    assert "channel-info" in script
