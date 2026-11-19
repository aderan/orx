"""Distribution-channel identity and release gates for the ORX
Analytics macOS app (G011 T005).

Two channels, never blurred:

- ``internal-preview`` (build mode ``dev``): ad-hoc signed, NOT
  notarized. The channel, its audience and its Gatekeeper limits are
  stated in the app-visible Info.plist, the in-app manifest, the
  archive/build-record names and the install docs. An internal preview
  is explicitly NOT a formal installer.
- ``official`` (build mode ``dist``): Developer ID Application signing
  with hardened runtime — including the bundled Python runtime and every
  nested Mach-O executable — notarytool notarization, stapler, and a
  Gatekeeper install assessment (``spctl --assess``). Missing
  credentials or any failed check fail CLOSED with a machine-readable
  JSON reason (exit 3). There is no code path that silently downgrades
  an official build to ad-hoc.

The channel decision and its basis are fixed BEFORE the build in a
decision file (``docs/diagnostics/g011/distribution.json`` in the ORX
repository, passed to ``scripts/build_app.sh`` via ``--decision`` or
``ORX_DISTRIBUTION_DECISION``). Outcome records for the two channels are
kept in SEPARATE entries (``apply-outcome``); an official failure never
becomes an internal-preview pass.

Credentials and private keys never enter logs, records, or the
repository: probes record only tool outcomes and public identity
metadata (the same facts every distributed binary shows via
``codesign -dv``), never keychain contents.

CLI (JSON on stdout; refusals print machine-readable JSON and exit 3,
usage errors exit 2):

  channel          MODE -> channel facts
  emit-env         MODE -> shell assignments (quoted) for build_app.sh
  validate-decision FILE CHANNEL -> decision gate
  preflight-official -> Developer ID + notarytool credential gate
  entitlements     CHANNEL SCOPE -> entitlements plist text
  channel-info     CHANNEL VERSION COMMIT -> app-visible metadata
  record           ... -> per-archive build record (hash AFTER gates)
  apply-outcome    DECISION CHANNEL STATUS -> separate result slots
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

CHANNELS = ("internal-preview", "official")
MODE_TO_CHANNEL = {
    "dev": "internal-preview",
    "internal-preview": "internal-preview",
    "dist": "official",
    "official": "official",
}
CHANNEL_TO_MODE = {"internal-preview": "dev", "official": "dist"}

# Identity facts per channel (app name / bundle id mirror the pre-T005
# dev/dist split so existing installs keep distinct defaults domains).
CHANNEL_FACTS = {
    "internal-preview": {
        "mode": "dev",
        "app_name": "ORX Analytics (Internal Preview)",
        "bundle_id": "org.orxanalytics.macos.dev",
        "archive_suffix": "zip",
        "signing": "ad-hoc",
        "audience": "internal testing only (内部测试)",
        "gatekeeper_notice": (
            "Ad-hoc signed, NOT notarized: Gatekeeper blocks first "
            "launch and needs the documented manual confirmation. "
            "Internal preview — not a formal installer, do not "
            "distribute outside the internal audience."),
    },
    "official": {
        "mode": "dist",
        "app_name": "ORX Analytics",
        "bundle_id": "org.orxanalytics.macos",
        "archive_suffix": "dmg",
        "signing": "Developer ID Application + hardened runtime",
        "audience": "general users (正式分发)",
        "gatekeeper_notice": (
            "Developer ID Application signed, notarized and stapled; "
            "Gatekeeper accepts it without bypass steps."),
    },
}

# A full git commit (the app metadata carries the COMPLETE source
# commit, never a truncated one).
FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

# Refusal exit code (distinct from usage errors): the build script
# surfaces our JSON and stops without producing artifacts.
REFUSED = 3

# Secret-looking gate/record keys: distribution records carry tool
# outcomes only; anything credential-like is a leak and is refused.
FORBIDDEN_KEY_FRAGMENTS = (
    "password", "credential", "secret", "private_key", "p12", "keychain",
)


class Refused(Exception):
    """A fail-closed gate refusal with a machine-readable code."""

    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Channel resolution
# ---------------------------------------------------------------------------

def channel_for_mode(mode):
    """Map a build mode (or channel alias) to its channel, or refuse."""
    channel = MODE_TO_CHANNEL.get(mode)
    if channel is None:
        raise Refused("unknown-mode", f"unknown mode {mode!r}; expected one of dev/dist")
    return channel


def emit_env(mode):
    """Shell-quoted assignments describing the channel for build_app.sh."""
    channel = channel_for_mode(mode)
    facts = CHANNEL_FACTS[channel]
    pairs = {
        "CHANNEL": channel,
        "MODE": facts["mode"],
        "APP_NAME": facts["app_name"],
        "BUNDLE_ID": facts["bundle_id"],
        "ARCHIVE_SUFFIX": facts["archive_suffix"],
        "AUDIENCE": facts["audience"],
        "GATEKEEPER_NOTICE": facts["gatekeeper_notice"],
    }
    return "\n".join(f"{k}={shlex.quote(v)}" for k, v in pairs.items())


# ---------------------------------------------------------------------------
# Decision file (channel + basis fixed BEFORE the build)
# ---------------------------------------------------------------------------

def _load_decision(path):
    p = Path(path)
    if not p.is_file():
        raise Refused("decision-missing", f"distribution decision file not found at {path}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused("decision-invalid-json", f"decision file {path} is not valid JSON: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("channels"), dict):
        raise Refused("decision-invalid-shape",
                      "decision file must be a JSON object with a 'channels' object")
    return data


def validate_decision(path, channel):
    """The build-time gate: the channel must be pre-allowed with basis.

    Refuses (never warns) when the decision file is missing/unreadable,
    the channel has no entry, the entry does not allow the channel, or
    the basis is empty. Returns the binding facts on success.
    """
    if channel not in CHANNELS:
        raise Refused("unknown-channel", f"unknown channel {channel!r}")
    data = _load_decision(path)
    entry = data["channels"].get(channel)
    if not isinstance(entry, dict):
        raise Refused("decision-channel-absent",
                      f"decision file {path} has no entry for channel {channel!r}")
    decision = entry.get("decision")
    if decision != "allowed":
        raise Refused("channel-not-allowed",
                      f"decision file {path} records channel {channel!r} as "
                      f"{decision!r}; an explicit NEW decision entry is required "
                      "before building it (no silent downgrade/upgrade)")
    basis = entry.get("basis")
    if not isinstance(basis, list) or not basis or \
            not all(isinstance(b, str) and b.strip() for b in basis):
        raise Refused("decision-basis-empty",
                      f"decision file {path} allows {channel!r} without a non-empty basis list")
    return {
        "channel": channel,
        "mode": CHANNEL_TO_MODE[channel],
        "decision_path": str(path),
        "decision_sha256": sha256_file(Path(path)),
        "basis": basis,
    }


def apply_outcome(path, channel, status, summary, evidence=None):
    """Record a channel outcome in its OWN slot, never another's.

    Refuses a 'passed' outcome for a channel the decision file does not
    currently allow (a passed build cannot precede its own decision),
    and refuses unknown channels/statuses. The other channel's slot is
    never touched — an official failure never rewrites the
    internal-preview slot or vice versa.
    """
    if channel not in CHANNELS:
        raise Refused("unknown-channel", f"unknown channel {channel!r}")
    if status not in ("passed", "blocked", "failed"):
        raise Refused("unknown-status", f"unknown status {status!r}")
    data = _load_decision(path)
    entry = data["channels"].get(channel)
    if status == "passed" and (not isinstance(entry, dict)
                               or entry.get("decision") != "allowed"):
        raise Refused("outcome-contradicts-decision",
                      f"cannot record {channel!r} as passed while its decision "
                      f"entry is not 'allowed'")
    results = data.setdefault("results", {})
    results[channel] = {
        "status": status,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "summary": summary,
        "evidence": evidence,
    }
    Path(path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8")
    return {"channel": channel, "status": status,
            "slots_present": sorted(k for k, v in results.items() if v is not None)}


# ---------------------------------------------------------------------------
# Official credential preflight (fail closed, no secrets recorded)
# ---------------------------------------------------------------------------

def _real_runner(command, cwd=None):
    """Run a probe command, capturing text output (never passwords)."""
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True)


def find_developer_id_identity(runner, preferred_name=None):
    """Find a Developer ID Application signing identity.

    Parses `security find-identity -v -p codesigning`; Apple Development
    / Apple Distribution / Mac Developer identities do NOT qualify for
    distribution outside the App Store and are refused.
    """
    result = runner(["security", "find-identity", "-v", "-p", "codesigning"])
    lines = (result.stdout + result.stderr).splitlines()
    candidates = []
    for line in lines:
        match = re.search(r'"(Developer ID Application: [^"]+)"', line)
        if not match:
            continue
        hash_match = re.search(r"\b([0-9A-Fa-f]{40})\b", line)
        if hash_match:
            candidates.append((hash_match.group(1), match.group(1)))
    if not candidates:
        found = [line.strip() for line in lines
                 if re.search(r"\b[0-9A-Fa-f]{40}\b", line)]
        raise Refused(
            "no-developer-id-identity",
            "no 'Developer ID Application' identity in the keychain; "
            "identities seen: " + ("; ".join(found) if found else "none"))
    if preferred_name:
        for identity_hash, name in candidates:
            if preferred_name in name:
                return {"identity": identity_hash, "identity_name": name}
        raise Refused("identity-name-not-found",
                      f"no Developer ID Application identity matching "
                      f"{preferred_name!r}; candidates: "
                      + "; ".join(name for _, name in candidates))
    identity_hash, name = candidates[0]
    return {"identity": identity_hash, "identity_name": name}


def notarytool_credentials_present(runner, profile):
    """True when `notarytool history` works with the stored profile.

    Uses only the profile NAME (user configuration, not a secret); the
    stored password itself is never read, printed, or recorded.
    """
    result = runner(["xcrun", "notarytool", "history",
                     "--keychain-profile", profile])
    output = result.stdout + result.stderr
    if result.returncode == 0 and "No Keychain password item found" not in output:
        return True
    return False


def preflight_official(runner=_real_runner, preferred_identity=None,
                       notary_profile="orx-analytics-notary"):
    """Every credential the official channel needs, checked up front.

    Returns the public signing facts on success; refuses with a single
    machine-readable code per missing piece. The caller stops BEFORE
    building anything when this refuses.
    """
    for tool in ("codesign",):
        probe = runner(["xcrun", "--find", tool])
        if probe.returncode != 0:
            raise Refused("tool-missing", f"{tool} not available via xcrun")
    identity = find_developer_id_identity(runner, preferred_identity)
    if not notarytool_credentials_present(runner, notary_profile):
        raise Refused(
            "no-notarytool-credentials",
            f"no stored notarytool credentials for keychain profile "
            f"{notary_profile!r}; run 'xcrun notarytool store-credentials "
            f"{notary_profile}' with an App Store Connect API key first")
    return {
        "identity": identity["identity"],
        "identity_name": identity["identity_name"],
        "notarization": "keychain-profile (name and contents not recorded)",
    }


# ---------------------------------------------------------------------------
# Entitlements (hardened runtime configuration)
# ---------------------------------------------------------------------------

def entitlements_plist(channel, scope):
    """The entitlements each code level needs under hardened runtime.

    CPython (the bundled python-build-standalone runtime) may allocate
    writable+executable memory and JIT; without these entitlements a
    hardened-runtime notarized app can fail at launch. The outer app
    binary (Swift, no JIT) needs no entitlement beyond the runtime flag.
    The exact set is recorded in the build record so reviewers see what
    was applied.
    """
    if channel == "official" and scope == "python":
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            '<dict>\n'
            '\t<key>com.apple.security.cs.allow-unsigned-executable-memory</key>\n'
            '\t<true/>\n'
            '\t<key>com.apple.security.cs.allow-jit</key>\n'
            '\t<true/>\n'
            '</dict>\n'
            '</plist>\n')
    if channel == "official" and scope == "app":
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            '<dict>\n'
            '\t<key>com.apple.security.cs.allow-unsigned-executable-memory</key>\n'
            '\t<true/>\n'
            '</dict>\n'
            '</plist>\n')
    if channel == "internal-preview" and scope in ("app", "python"):
        # Ad-hoc preview: no hardened runtime, no entitlements.
        return ""
    raise Refused("unknown-entitlements-scope",
                  f"no entitlements defined for channel {channel!r} scope {scope!r}")


# ---------------------------------------------------------------------------
# App-visible channel metadata
# ---------------------------------------------------------------------------

def channel_info(channel, version, commit):
    """Info.plist channel keys + the in-app distribution manifest.

    The metadata carries the Analytics version and the COMPLETE source
    commit (40 hex); a truncated/absent commit is refused rather than
    recorded as if it were complete.
    """
    if channel not in CHANNELS:
        raise Refused("unknown-channel", f"unknown channel {channel!r}")
    if not FULL_COMMIT_RE.match(commit or ""):
        raise Refused("commit-not-full",
                      f"source commit must be the full 40-hex sha, got {commit!r}")
    facts = CHANNEL_FACTS[channel]
    return {
        "channel": channel,
        "version": version,
        "source_commit": commit,
        "info_plist": {
            "ORXAnalyticsChannel": channel,
            "ORXAnalyticsChannelAudience": facts["audience"],
            "ORXAnalyticsGatekeeperNotice": facts["gatekeeper_notice"],
            "ORXAnalyticsSourceCommit": commit,
        },
        "in_app_manifest": {
            "channel": channel,
            "mode": facts["mode"],
            "version": version,
            "source_commit": commit,
            "audience": facts["audience"],
            "gatekeeper_limit": facts["gatekeeper_notice"],
            "signing": facts["signing"],
        },
    }


# ---------------------------------------------------------------------------
# Build record (archive hash computed AFTER the final gates)
# ---------------------------------------------------------------------------

def _contains_secret_key(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            if any(fragment in str(key).lower() for fragment in FORBIDDEN_KEY_FRAGMENTS):
                return here
            found = _contains_secret_key(item, here)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _contains_secret_key(item, path)
            if found:
                return found
    return None


def _gate(gates, name):
    value = (gates or {}).get(name)
    return value if isinstance(value, dict) else {}


def build_record(channel, mode, version, commit, archive_path, gates,
                 decision=None, out_path=None):
    """Write the per-archive build record, enforcing gate ordering.

    The archive sha256 is computed HERE, only after the channel's full
    gate evidence is present (official: Developer ID signing verified,
    hardened runtime, notarization accepted, staple validated, install
    assessment accepted; internal-preview: ad-hoc signature verified and
    NOT any claim the channel cannot honestly make). A record that
    would leak credential-like keys is refused outright.
    """
    if channel not in CHANNELS:
        raise Refused("unknown-channel", f"unknown channel {channel!r}")
    if not FULL_COMMIT_RE.match(commit or ""):
        raise Refused("commit-not-full",
                      f"source commit must be the full 40-hex sha, got {commit!r}")
    signing = _gate(gates, "signing")
    notarization = _gate(gates, "notarization")
    staple = _gate(gates, "staple")
    install = _gate(gates, "install_assessment")
    if not Path(archive_path).is_file():
        raise Refused("record-archive-missing",
                      f"archive not found at {archive_path}; nothing to record")

    if channel == "official":
        missing = []
        if not (signing.get("verified") and signing.get("style") == "developer-id"):
            missing.append("signing: Developer ID signature not verified")
        if not signing.get("hardened_runtime"):
            missing.append("signing: hardened runtime not confirmed")
        if not signing.get("identity"):
            missing.append("signing: signing identity not recorded")
        if notarization.get("status") != "accepted":
            missing.append("notarization: notarytool status is "
                           f"{notarization.get('status')!r}, not 'accepted'")
        if not (staple.get("status") == "stapled" and staple.get("validated")):
            missing.append("staple: ticket not stapled and validated")
        if install.get("status") != "accepted":
            missing.append("install_assessment: Gatekeeper assessment not accepted")
        if missing:
            raise Refused(
                "record-before-gates",
                "official archive hash may only be recorded after the full "
                "gate chain; incomplete: " + "; ".join(missing))
    else:  # internal-preview
        if not (signing.get("verified") and signing.get("style") == "adhoc"):
            raise Refused("record-before-gates",
                          "internal-preview record requires a verified ad-hoc "
                          f"signature; got style={signing.get('style')!r} "
                          f"verified={signing.get('verified')!r}")
        # Honesty: the preview channel must NOT claim verification it
        # cannot have. Claims beyond ad-hoc are refused, not edited.
        if notarization.get("status") not in (None, "not-performed"):
            raise Refused("record-claims-unavailable-gate",
                          "internal-preview must not claim notarization status "
                          f"{notarization.get('status')!r}")
        if staple.get("status") not in (None, "not-performed"):
            raise Refused("record-claims-unavailable-gate",
                          "internal-preview must not claim staple status "
                          f"{staple.get('status')!r}")
        if install.get("status") not in (None, "not-performed"):
            raise Refused("record-claims-unavailable-gate",
                          "internal-preview must not claim install assessment "
                          f"status {install.get('status')!r}")

    record = {
        "schema": 1,
        "channel": channel,
        "mode": mode,
        "version": version,
        "source_commit": commit,
        "app_name": CHANNEL_FACTS[channel]["app_name"],
        "bundle_id": CHANNEL_FACTS[channel]["bundle_id"],
        "audience": CHANNEL_FACTS[channel]["audience"],
        "gatekeeper_limit": CHANNEL_FACTS[channel]["gatekeeper_notice"],
        "archive": Path(archive_path).name,
        "sha256": sha256_file(Path(archive_path)),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "gates": {
            "signing": signing,
            "notarization": notarization,
            "staple": staple,
            "install_assessment": install,
        },
        "decision": decision,
        "secrets_policy": "credentials and private keys are never recorded",
    }
    leak = _contains_secret_key({"gates": gates, "decision": decision})
    if leak:
        raise Refused("record-would-leak-secrets",
                      f"gate/record data contains credential-like key {leak!r}")
    if out_path is not None:
        Path(out_path).write_text(
            json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8")
    return record


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_json(payload):
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="app_distribution.py",
                                     description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("channel", help="mode -> channel facts")
    p.add_argument("mode")

    p = sub.add_parser("emit-env", help="mode -> shell assignments")
    p.add_argument("mode")

    p = sub.add_parser("validate-decision", help="pre-build channel decision gate")
    p.add_argument("--decision", required=True)
    p.add_argument("--channel", required=True)

    p = sub.add_parser("preflight-official", help="official credential gate")
    p.add_argument("--identity", default=None)
    p.add_argument("--notary-profile", default="orx-analytics-notary")

    p = sub.add_parser("entitlements", help="entitlements plist for a code scope")
    p.add_argument("--channel", required=True)
    p.add_argument("--scope", required=True, choices=("app", "python"))

    p = sub.add_parser("channel-info", help="app-visible channel metadata")
    p.add_argument("--channel", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--commit", required=True)

    p = sub.add_parser("record", help="write the per-archive build record")
    p.add_argument("--channel", required=True)
    p.add_argument("--mode", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--archive", required=True)
    p.add_argument("--gates", required=True,
                   help="path to the gates evidence JSON collected by build_app.sh")
    p.add_argument("--decision", default=None,
                   help="path to the distribution decision file (recorded by sha256)")
    p.add_argument("--out", default=None)

    p = sub.add_parser("apply-outcome", help="record a channel outcome in its own slot")
    p.add_argument("--decision", required=True)
    p.add_argument("--channel", required=True)
    p.add_argument("--status", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--evidence", default=None)

    args = parser.parse_args(argv)
    try:
        if args.command == "channel":
            channel = channel_for_mode(args.mode)
            facts = dict(CHANNEL_FACTS[channel])
            facts["channel"] = channel
            _print_json(facts)
        elif args.command == "emit-env":
            print(emit_env(args.mode))
        elif args.command == "validate-decision":
            _print_json(validate_decision(args.decision, args.channel))
        elif args.command == "preflight-official":
            _print_json(preflight_official(
                preferred_identity=args.identity,
                notary_profile=args.notary_profile))
        elif args.command == "entitlements":
            sys.stdout.write(entitlements_plist(args.channel, args.scope))
        elif args.command == "channel-info":
            _print_json(channel_info(args.channel, args.version, args.commit))
        elif args.command == "record":
            gates = json.loads(Path(args.gates).read_text(encoding="utf-8"))
            decision = None
            if args.decision:
                decision = {
                    "path": str(args.decision),
                    "sha256": sha256_file(Path(args.decision)),
                }
            record = build_record(args.channel, args.mode, args.version,
                                  args.commit, args.archive, gates,
                                  decision=decision, out_path=args.out)
            _print_json(record)
        elif args.command == "apply-outcome":
            _print_json(apply_outcome(args.decision, args.channel, args.status,
                                      args.summary, evidence=args.evidence))
    except Refused as exc:
        # Machine-readable refusal (the build stops; no artifact is
        # produced and no fallback channel is attempted).
        _print_json({"refused": True, "code": exc.code, "channel":
                     getattr(args, "channel", None), "detail": exc.detail})
        print(f"app_distribution: refused: {exc.code}: {exc.detail}",
              file=sys.stderr)
        return REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main())
