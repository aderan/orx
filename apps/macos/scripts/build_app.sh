#!/bin/sh
# Build the ORX Analytics macOS app for ONE distribution channel
# (design §9 / task T012; channel gates G011 T005).
#
# Bundles a PINNED python-build-standalone runtime plus this repo's
# analytics code into the .app Resources, so a clean Mac needs no
# preinstalled Python/uv/Homebrew. The Swift app launches and manages
# the coordination engine through that bundled runtime.
#
# Channels (never blurred; gate logic in scripts/app_distribution.py):
#   internal-preview (mode `dev`)
#     "ORX Analytics (Internal Preview).app", bundle id
#     org.orxanalytics.macos.dev (separate defaults domain), ad-hoc
#     signature, zip archive. Clearly labeled internal preview —
#     NOT a formal installer.
#   official (mode `dist`)
#     "ORX Analytics.app", org.orxanalytics.macos, DMG. Requires the
#     FULL gate chain — Developer ID Application signing with hardened
#     runtime (bundled Python and every nested Mach-O included),
#     notarytool notarization, stapler, and a Gatekeeper install
#     assessment (spctl). Missing credentials or any failed check
#     fails CLOSED with a machine-readable JSON reason. There is NO
#     fallback to ad-hoc: an official failure never silently becomes
#     an internal-preview artifact.
#
# The channel + basis are fixed BEFORE the build in the distribution
# decision file (ORX repo: docs/diagnostics/g011/distribution.json):
#   scripts/build_app.sh dev  --decision /path/to/distribution.json
# or via ORX_DISTRIBUTION_DECISION. Required for official; validated
# (never contradicted) for internal-preview.
#
# Requirements: macOS 13+ on Apple Silicon with the Swift 5.9 toolchain
# (swift build), python3 (channel gates), and for the official channel
# a Developer ID Application identity plus stored notarytool
# credentials. The Python runtime is downloaded once into
# var/runtime-cache/ and its SHA256 is verified on EVERY use.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
  echo "usage: $0 [dev|dist|internal-preview|official]" \
       "[--decision FILE] [--identity NAME] [--notary-profile NAME]" >&2
}

MODE_ARG=""
DECISION="${ORX_DISTRIBUTION_DECISION:-}"
IDENTITY_NAME=""
NOTARY_PROFILE="orx-analytics-notary"
while [ $# -gt 0 ]; do
  case "$1" in
    --decision) DECISION="${2:-}"; shift 2 ;;
    --identity) IDENTITY_NAME="${2:-}"; shift 2 ;;
    --notary-profile) NOTARY_PROFILE="${2:-}"; shift 2 ;;
    -*) usage; exit 2 ;;
    *) if [ -z "$MODE_ARG" ]; then MODE_ARG="$1"; shift; else usage; exit 2; fi ;;
  esac
done
[ -n "$MODE_ARG" ] || { usage; exit 2; }
case "$MODE_ARG" in
  dev|dist|internal-preview|official) ;;
  *) usage; exit 2 ;;
esac

command -v python3 >/dev/null 2>&1 \
  || { echo "build_app.sh: python3 is required (channel gates in scripts/app_distribution.py)" >&2; exit 1; }
PY="$(command -v python3)"
GATE="$ROOT/scripts/app_distribution.py"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/orx-build-app.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# Run a channel gate; on refusal surface its machine-readable JSON and
# stop. Gates NEVER fall back to another channel.
GATE_OUT=""
run_gate() {
  set +e
  GATE_OUT="$("$PY" "$GATE" "$@" 2>"$WORK/gate.err")"
  GATE_RC=$?
  set -e
  if [ "$GATE_RC" -ne 0 ]; then
    [ -n "$GATE_OUT" ] && printf '%s\n' "$GATE_OUT" >&2
    sed -n '1,20p' "$WORK/gate.err" >&2
    exit "$GATE_RC"
  fi
}

# ---- channel identity (single source: scripts/app_distribution.py) --------
run_gate channel "$MODE_ARG" >/dev/null   # unknown modes refuse here
eval "$("$PY" "$GATE" emit-env "$MODE_ARG")"

# ---- app identity ---------------------------------------------------------
# The software version has a single source of truth:
# src/orxanalytics/__init__.py __version__ (CONTRIBUTING 发布检查).
VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$ROOT/src/orxanalytics/__init__.py")
[ -n "$VERSION" ] || { echo "build_app.sh: cannot read __version__" >&2; exit 1; }
COMMIT=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)
BUILD=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo "nogit")
FLAVOR="$MODE"

DIST_DIR="$ROOT/var/dist"
STAGE="$DIST_DIR/$MODE"
APP="$STAGE/$APP_NAME.app"
# Channel token precedes the mode token so both stay machine-greppable
# while remaining compatible with the joint-release archive contract
# (ORXAnalytics-*-<mode>-macos13-arm64.<suffix>).
ARCHIVE="$DIST_DIR/ORXAnalytics-${VERSION}-${CHANNEL}-${MODE}-macos13-arm64.${ARCHIVE_SUFFIX}"

# ---- 0. pre-build gates: decision fixed BEFORE building --------------------
# Official builds REQUIRE the pre-fixed decision (channel + basis) and
# every credential, checked BEFORE any expensive work. Internal-preview
# validates the decision when one is provided (a contradicting decision
# refuses; no silent downgrade either way).
if [ -n "$DECISION" ]; then
  run_gate validate-decision --decision "$DECISION" --channel "$CHANNEL"
  echo "decision: $CHANNEL allowed by $DECISION"
elif [ "$CHANNEL" = official ]; then
  echo '{"refused": true, "code": "decision-missing", "channel": "official",
  "detail": "official builds require the pre-fixed distribution decision file (--decision / ORX_DISTRIBUTION_DECISION)"}' >&2
  exit 3
else
  echo "decision: none supplied; internal-preview proceeds as the labeled ad-hoc channel"
  echo "          (the ORX joint-release manifest binds the channel itself)"
fi
if [ "$CHANNEL" = official ]; then
  run_gate preflight-official --notary-profile "$NOTARY_PROFILE" \
    ${IDENTITY_NAME:+--identity "$IDENTITY_NAME"}
  IDENTITY=$(printf '%s\n' "$GATE_OUT" | sed -n 's/^  "identity": "\(.*\)",$/\1/p')
  echo "credentials: Developer ID identity present; notarytool profile stored"
fi
# App metadata needs the COMPLETE source commit; a truncated/absent one
# refuses rather than being recorded as if complete.
run_gate channel-info --channel "$CHANNEL" --version "$VERSION" --commit "$COMMIT"
printf '%s\n' "$GATE_OUT" > "$WORK/channel-info.json"

# ---- pinned standalone Python runtime (version + checksum frozen) --------
PBS_TAG="20250115"
PBS_PYTHON="cpython-3.12.8"
PBS_NAME="${PBS_PYTHON}+${PBS_TAG}-aarch64-apple-darwin-install_only"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_NAME}.tar.gz"
PBS_SHA256="e29003b69465c33692830032d9d237d84ea43a2e8461db9134641640fb49f040"
CACHE_DIR="$ROOT/var/runtime-cache"
TARBALL="$CACHE_DIR/$PBS_NAME.tar.gz"

mkdir -p "$CACHE_DIR" "$STAGE"
if [ ! -f "$TARBALL" ]; then
  echo "build_app.sh: downloading pinned runtime $PBS_NAME ..."
  curl -fL --retry 3 -C - -o "$TARBALL" "$PBS_URL" \
    || { echo "build_app.sh: download failed; pre-seed $TARBALL to build offline" >&2; exit 1; }
fi
echo "$PBS_SHA256  $TARBALL" | shasum -a 256 -c - >/dev/null \
  || { echo "build_app.sh: SHA256 mismatch for $TARBALL" >&2; exit 1; }
echo "runtime: $PBS_NAME (sha256 verified)"

# ---- 1. Swift release binary (SwiftPM, Xcode-free) ------------------------
"$ROOT/scripts/build_macos_app.sh" release
BIN="$ROOT/apps/macos/.build/release/ORXAnalyticsApp"

ARCH=$(lipo -info "$BIN" 2>/dev/null || file "$BIN")
case "$ARCH" in
  *arm64*) ;;
  *) echo "build_app.sh: $BIN is not an arm64 build: $ARCH" >&2; exit 1 ;;
esac
MIN_OS=$(otool -l "$BIN" | awk '/LC_BUILD_VERSION/{f=1} f && /minos/{print $2; exit}')
case "$MIN_OS" in
  13.*|1[4-9].*) ;;
  *) echo "build_app.sh: unexpected deployment target '$MIN_OS' (want >= 13.0)" >&2; exit 1 ;;
esac

# ---- 2. assemble the .app bundle ------------------------------------------
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$BIN" "$APP/Contents/MacOS/ORXAnalyticsApp"

# Python runtime: install_only tarball ships a relocatable python/ tree.
tar -xzf "$TARBALL" -C "$APP/Contents/Resources"
mv "$APP/Contents/Resources/python" "$APP/Contents/Resources/python.tmp"
mv "$APP/Contents/Resources/python.tmp" "$APP/Contents/Resources/python"

# Analytics code: stdlib-only package copied beside the runtime; the app
# runs `<bundled python> -m orxanalytics.engine` with PYTHONPATH here.
rm -rf "$APP/Contents/Resources/orxanalytics"
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
  "$ROOT/src/orxanalytics/" "$APP/Contents/Resources/orxanalytics/"
[ -f "$APP/Contents/Resources/orxanalytics/__init__.py" ] \
  || { echo "build_app.sh: analytics package missing from staging" >&2; exit 1; }
# The app's upgrade guard reads this to decide backup-before-migrate.
printf '%s\n' "$VERSION" > "$APP/Contents/Resources/orxanalytics-version.txt"

# Engine serve entry the app launches: the checked-in wrapper around
# the engine's public serve API (acquire + start_server + supervisor).
# `python -m orxanalytics.engine` currently never binds the socket —
# see the wrapper's docstring and docs/app-installation.md.
cp "$ROOT/apps/macos/Resources/orx_engine_serve.py" \
   "$APP/Contents/Resources/orx_engine_serve.py"

# In-app distribution manifest: channel, audience, Gatekeeper limit,
# version and the COMPLETE source commit travel inside the artifact.
"$PY" - "$WORK/channel-info.json" "$APP/Contents/Resources/distribution.json" <<'PYEOF'
import json, sys
info = json.loads(open(sys.argv[1], encoding="utf-8").read())
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(info["in_app_manifest"], fh, indent=2, ensure_ascii=False,
              sort_keys=True)
    fh.write("\n")
PYEOF

printf 'APPL????' > "$APP/Contents/PkgInfo"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key>
	<string>$APP_NAME</string>
	<key>CFBundleDisplayName</key>
	<string>$APP_NAME</string>
	<key>CFBundleIdentifier</key>
	<string>$BUNDLE_ID</string>
	<key>CFBundleShortVersionString</key>
	<string>$VERSION</string>
	<key>CFBundleVersion</key>
	<string>$VERSION+$FLAVOR+$BUILD</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleExecutable</key>
	<string>ORXAnalyticsApp</string>
	<key>LSMinimumSystemVersion</key>
	<string>13.0</string>
	<!-- Menu-bar-only app: no Dock icon, no main window (design §7). -->
	<key>LSUIElement</key>
	<true/>
	<key>NSHumanReadableCopyright</key>
	<string>ORX Analytics contributors</string>
	<key>NSSupportsAutomaticTermination</key>
	<false/>
	<key>NSSupportsSuddenTermination</key>
	<false/>
	<!-- Build flavor marker so the app itself can display it honestly. -->
	<key>ORXAnalyticsBuildFlavor</key>
	<string>$FLAVOR</string>
	<!-- Channel identity (G011 T005): the distribution channel, its
	     audience, its Gatekeeper limits and the COMPLETE source commit
	     are visible IN the app, not only in build logs. -->
	<key>ORXAnalyticsChannel</key>
	<string>$CHANNEL</string>
	<key>ORXAnalyticsChannelAudience</key>
	<string>$AUDIENCE</string>
	<key>ORXAnalyticsGatekeeperNotice</key>
	<string>$GATEKEEPER_NOTICE</string>
	<key>ORXAnalyticsSourceCommit</key>
	<string>$COMMIT</string>
	<key>ORXAnalyticsBundledPython</key>
	<string>$PBS_NAME</string>
</dict>
</plist>
PLIST

# ---- 3. bounded self-test: the bundled runtime really runs the engine ----
# Fully synthetic: an empty ORX_DATA_DIR means discovery finds no projects,
# so the self-test never reads the developer's real sessions (CONTRIBUTING).
# G011 T006 fix: this runs BEFORE the signature gates and with bytecode
# writing disabled — running the engine must never mutate the bundle that
# is about to be sealed (the previous order let __pycache__/*.pyc land
# inside the signed .app, breaking the code-seal of the final archive).
SELFTMP=$(mktemp -d "${TMPDIR:-/tmp}/orx-app-selftest.XXXXXX")
mkdir -p "$SELFTMP/empty-orx"
PYBIN="$APP/Contents/Resources/python/bin/python3"
ORX_DATA_DIR="$SELFTMP/empty-orx" PYTHONDONTWRITEBYTECODE=1 "$PYBIN" -B \
  "$APP/Contents/Resources/orx_engine_serve.py" "$SELFTMP/data" \
  >"$SELFTMP/engine.out" 2>"$SELFTMP/engine.err" &
ENGPID=$!
SOCK="$SELFTMP/data/engine.sock"
OK=no
i=0
while [ "$i" -lt 30 ]; do
  if [ -S "$SOCK" ]; then OK=yes; break; fi
  kill -0 "$ENGPID" 2>/dev/null || break
  sleep 0.5
  i=$((i + 1))
done
if ! kill -0 "$ENGPID" 2>/dev/null; then
  wait "$ENGPID" || true
  echo "build_app.sh: bundled engine exited early:" >&2
  tail -5 "$SELFTMP/engine.err" >&2 || true
  rm -rf "$SELFTMP"
  exit 1
fi
kill "$ENGPID" 2>/dev/null || true
wait "$ENGPID" 2>/dev/null || true
rm -rf "$SELFTMP"
[ "$OK" = yes ] \
  || { echo "build_app.sh: bundled engine did not bind its socket in 15s" >&2; exit 1; }
echo "self-test: bundled engine served on its socket"

# Seal baseline: the bytecode/cache files legitimately present BEFORE the
# signature gates (the pinned runtime ships a few precompiled stdlib
# caches; they are inside the seal from the start and are fine).
find "$APP" \( -name "__pycache__" -o -name "*.pyc" \) -print | sort \
  > "$WORK/seal-baseline.txt"

# ---- 4. signature + notarization gates (per channel) ----------------------
if [ "$CHANNEL" = official ]; then
  # Official: Developer ID Application + hardened runtime, nested code
  # first (bundled Python: every Mach-O file), then the outer app; real
  # notarytool submit --wait; real stapler; Gatekeeper assessment.
  "$PY" "$GATE" entitlements --channel official --scope python \
    > "$WORK/python.entitlements"
  "$PY" "$GATE" entitlements --channel official --scope app \
    > "$WORK/app.entitlements"

  echo "signature: signing nested code (bundled Python runtime) ..."
  find "$APP/Contents/Resources/python" -type f -print > "$WORK/nested-files.txt"
  NESTED_FAIL=0
  while IFS= read -r nested; do
    if file -b "$nested" | grep -q "Mach-O"; then
      if ! codesign --force --sign "$IDENTITY" --timestamp \
          --options runtime --entitlements "$WORK/python.entitlements" \
          "$nested" >/dev/null 2>&1; then
        echo "build_app.sh: nested signing failed for $nested" >&2
        NESTED_FAIL=1
        break
      fi
    fi
  done < "$WORK/nested-files.txt"
  [ "$NESTED_FAIL" -eq 0 ] \
    || { echo '{"refused": true, "code": "nested-signing-failed", "channel": "official"}' >&2; exit 3; }

  codesign --force --sign "$IDENTITY" --timestamp \
    --options runtime --entitlements "$WORK/app.entitlements" "$APP"
  codesign --verify --strict --deep "$APP" 2>/dev/null \
    || { echo '{"refused": true, "code": "signature-verify-failed", "channel": "official"}' >&2; exit 3; }
  codesign -dv "$APP" > "$WORK/codesign-dv.txt" 2>&1 || true
  SIGN_AUTHORITY=$(sed -n 's/^Authority=\(.*\)$/\1/p' "$WORK/codesign-dv.txt" | head -1)
  SIGN_TEAMID=$(sed -n 's/^TeamIdentifier=\(.*\)$/\1/p' "$WORK/codesign-dv.txt" | head -1)
  case "$SIGN_AUTHORITY" in
    "Developer ID Application:"*) ;;
    *) echo '{"refused": true, "code": "signature-not-developer-id", "channel": "official",
       "detail": "signed authority is not Developer ID Application"}' >&2; exit 3 ;;
  esac
  echo "signature: Developer ID Application (hardened runtime, nested code signed)"

  # Notarization: submit the zipped app and WAIT for the verdict.
  echo "notarization: xcrun notarytool submit --wait (profile: stored keychain credentials) ..."
  ditto -c -k --keepParent "$APP" "$WORK/notary.zip"
  if ! xcrun notarytool submit "$WORK/notary.zip" \
      --keychain-profile "$NOTARY_PROFILE" --wait > "$WORK/notarytool.log" 2>&1; then
    sed -n '1,20p' "$WORK/notarytool.log" >&2 || true
    echo '{"refused": true, "code": "notarization-submit-failed", "channel": "official"}' >&2
    exit 3
  fi
  grep -q "status: Accepted" "$WORK/notarytool.log" \
    || { sed -n '1,20p' "$WORK/notarytool.log" >&2 || true
         echo '{"refused": true, "code": "notarization-not-accepted", "channel": "official"}' >&2
         exit 3; }
  NOTARY_ID=$(sed -n 's/^id: \(.*\)$/\1/p' "$WORK/notarytool.log" | head -1)
  echo "notarization: accepted (submission ${NOTARY_ID:-unknown})"

  # Staple the ticket so Gatekeeper can validate offline.
  xcrun stapler staple "$APP" \
    || { echo '{"refused": true, "code": "staple-failed", "channel": "official"}' >&2; exit 3; }
  xcrun stapler validate "$APP" > "$WORK/stapler-validate.log" 2>&1 \
    || { echo '{"refused": true, "code": "staple-invalid", "channel": "official"}' >&2; exit 3; }
  echo "staple: ticket stapled and validated"

  # Install evaluation: assess a copied install the way Gatekeeper
  # would. The pass criterion is spctl ACCEPTANCE — quarantine removal
  # is NEVER used as an official-channel method.
  ASSESS_APP="$WORK/install-eval/$APP_NAME.app"
  mkdir -p "$WORK/install-eval"
  ditto "$APP" "$ASSESS_APP"
  if ! spctl --assess --type execute -vv "$ASSESS_APP" \
      > "$WORK/spctl.log" 2>&1 || ! grep -q "accepted" "$WORK/spctl.log"; then
    sed -n '1,10p' "$WORK/spctl.log" >&2 || true
    echo '{"refused": true, "code": "install-assessment-failed", "channel": "official",
           "detail": "spctl did not accept the installed copy; quarantine removal is not a method here"}' >&2
    exit 3
  fi
  xcrun stapler validate "$ASSESS_APP" >/dev/null 2>&1 \
    || { echo '{"refused": true, "code": "install-assessment-failed", "channel": "official",
               "detail": "stapled ticket missing on the installed copy"}' >&2; exit 3; }
  ASSESS_DETAIL=$(tr '\n' ' ' < "$WORK/spctl.log" | sed 's/  */ /g')
  echo "install assessment: $ASSESS_DETAIL"

  env \
    G_SIGN_STYLE="developer-id" G_SIGN_VERIFIED="true" G_HARDENED="true" \
    G_IDENTITY="$IDENTITY" G_IDENTITY_NAME="$SIGN_AUTHORITY" \
    G_TEAM_ID="$SIGN_TEAMID" \
    G_NOTARY_STATUS="accepted" G_NOTARY_ID="$NOTARY_ID" \
    G_STAPLE_STATUS="stapled" G_STAPLE_VALID="true" \
    G_ASSESS_STATUS="accepted" G_ASSESS_DETAIL="$ASSESS_DETAIL" \
    "$PY" - <<'PYEOF' > "$WORK/gates.json"
import json, os
def g(k): return os.environ.get(k, "")
gates = {
    "signing": {"style": g("G_SIGN_STYLE"),
                "verified": g("G_SIGN_VERIFIED") == "true",
                "hardened_runtime": g("G_HARDENED") == "true",
                "identity": g("G_IDENTITY") or None,
                "identity_name": g("G_IDENTITY_NAME") or None,
                "team_identifier": g("G_TEAM_ID") or None,
                "nested_code": "all Mach-O files under Resources/python signed"},
    "notarization": {"status": g("G_NOTARY_STATUS"),
                     "submission_id": g("G_NOTARY_ID") or None,
                     "tool": "xcrun notarytool submit --wait"},
    "staple": {"status": g("G_STAPLE_STATUS"),
               "validated": g("G_STAPLE_VALID") == "true"},
    "install_assessment": {"status": g("G_ASSESS_STATUS"),
                           "tool": "spctl --assess --type execute",
                           "detail": g("G_ASSESS_DETAIL")},
}
print(json.dumps(gates, indent=2))
PYEOF
else
  # Internal preview: ad-hoc signature, honestly labeled. NOT notarized,
  # NOT a formal installer; the record refuses to claim otherwise.
  codesign --force --sign - --deep "$APP" >/dev/null 2>&1
  codesign --verify --strict "$APP" 2>/dev/null \
    || codesign --verify "$APP" 2>/dev/null \
    || { echo "build_app.sh: ad-hoc signature did not verify" >&2; exit 1; }
  echo "signature: ad-hoc (internal preview; NOT Developer ID signed, NOT notarized)"
  env \
    G_SIGN_STYLE="adhoc" G_SIGN_VERIFIED="true" G_HARDENED="false" \
    G_NOTARY_STATUS="not-performed" G_STAPLE_STATUS="not-performed" \
    G_ASSESS_STATUS="not-performed" \
    "$PY" - <<'PYEOF' > "$WORK/gates.json"
import json, os
def g(k): return os.environ.get(k, "")
print(json.dumps({
    "signing": {"style": g("G_SIGN_STYLE"),
                "verified": g("G_SIGN_VERIFIED") == "true",
                "hardened_runtime": g("G_HARDENED") == "true",
                "identity": None, "identity_name": None, "team_identifier": None,
                "note": "ad-hoc; Gatekeeper limits stated in app metadata"},
    "notarization": {"status": g("G_NOTARY_STATUS"),
                     "reason": "channel policy: internal preview is not notarized"},
    "staple": {"status": g("G_STAPLE_STATUS")},
    "install_assessment": {"status": g("G_ASSESS_STATUS"),
                           "reason": "channel policy: ad-hoc apps cannot pass spctl; the preview install path uses the documented manual Gatekeeper confirmation"},
}, indent=2))
PYEOF
fi

# ---- 5. archive (.app + install material) --------------------------------
# Seal guard FIRST: the archive may only cover a bundle whose bytecode/
# cache content is byte-for-byte the set present when the signature gates
# sealed it. Anything that appeared inside the .app after signing breaks
# the code-seal for every consumer; refuse instead of hashing a mutable
# artifact.
find "$APP" \( -name "__pycache__" -o -name "*.pyc" \) -print | sort \
  > "$WORK/seal-now.txt"
if ! cmp -s "$WORK/seal-baseline.txt" "$WORK/seal-now.txt"; then
  echo '{"refused": true, "code": "bundle-mutated-after-signing", "channel": "'"$CHANNEL"'",
  "detail": "bytecode/cache files inside the .app changed across the signature gates; the archive sha256 may only be recorded for a sealed bundle"}' >&2
  diff "$WORK/seal-baseline.txt" "$WORK/seal-now.txt" >&2 || true
  exit 3
fi
# The archive is created only after every channel gate passed, so the
# recorded sha256 is provably the hash of the FULLY signed/notarized
# artifact (scripts/app_distribution.py record enforces the ordering).
rm -f "$ARCHIVE"
if [ "$CHANNEL" = official ]; then
  IMGSTAGE="$STAGE/dmg-root"
  rm -rf "$IMGSTAGE"; mkdir -p "$IMGSTAGE"
  mv "$APP" "$IMGSTAGE/$APP_NAME.app"
  ln -s /Applications "$IMGSTAGE/Applications"
  hdiutil create -volname "ORX Analytics" -format UDZO \
    -srcfolder "$IMGSTAGE" "$ARCHIVE" -ov >/dev/null
  mv "$IMGSTAGE/$APP_NAME.app" "$APP"
  rm -rf "$IMGSTAGE"
else
  ditto -c -k --keepParent "$APP" "$ARCHIVE"
fi

# ---- 6. build record: hash AFTER the final gates ---------------------------
if [ -n "$DECISION" ]; then
  run_gate record --channel "$CHANNEL" --mode "$MODE" --version "$VERSION" \
    --commit "$COMMIT" --archive "$ARCHIVE" --gates "$WORK/gates.json" \
    --decision "$DECISION" --out "$ARCHIVE.json"
else
  run_gate record --channel "$CHANNEL" --mode "$MODE" --version "$VERSION" \
    --commit "$COMMIT" --archive "$ARCHIVE" --gates "$WORK/gates.json" \
    --out "$ARCHIVE.json"
fi

echo
echo "build_app.sh: done ($CHANNEL / $MODE)"
echo "  app:        $APP"
echo "  archive:    $ARCHIVE"
echo "  record:     $ARCHIVE.json"
echo "  version:    $VERSION (source commit $COMMIT)"
echo "  audience:   $AUDIENCE"
echo "  gatekeeper: $GATEKEEPER_NOTICE"
if [ "$CHANNEL" = internal-preview ]; then
  echo "  NOTE: internal preview — ad-hoc signed, not notarized."
  echo "        NOT a formal installer; do not distribute beyond the internal audience."
fi
