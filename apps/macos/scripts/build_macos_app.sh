#!/bin/sh
# Build the ORX Analytics menu-bar app skeleton with the SwiftPM
# command-line toolchain — no Xcode required (design §9 / task T008).
#
# Usage: scripts/build_macos_app.sh [debug|release]
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${1:-debug}"
APP_DIR="$ROOT/apps/macos"
BIN="$APP_DIR/.build/$CONFIG/ORXAnalyticsApp"

cd "$APP_DIR"
swift build -c "$CONFIG"

if [ ! -x "$BIN" ]; then
    echo "build_macos_app.sh: expected executable at $BIN" >&2
    exit 1
fi

# Verify the LSUIElement Info.plist really made it into the binary
# (SwiftPM executable: embedded into __TEXT,__info_plist).
if command -v otool >/dev/null 2>&1; then
    if otool -s __TEXT __info_plist "$BIN" | grep -q __info_plist; then
        echo "info-plist: embedded (__TEXT,__info_plist)"
    else
        echo "build_macos_app.sh: __TEXT,__info_plist section missing" >&2
        exit 1
    fi
fi

echo "built: $BIN"
