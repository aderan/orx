#!/bin/sh
# Xcode-free smoke test for the ORX Analytics app skeleton (task T008).
#
# Builds the executable (scripts/build_macos_app.sh) and runs it with
# --smoke-test: the app verifies its protocol client (version
# negotiation + incompatibility reporting), cache-first layer and frozen
# formatting rules against a local synthetic engine, then exits with a
# status — no GUI session, no live engine, no real projects.
set -eu

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"

"$APP_DIR/scripts/build_macos_app.sh" debug

BIN="$APP_DIR/.build/debug/ORXAnalyticsApp"
"$BIN" --smoke-test
