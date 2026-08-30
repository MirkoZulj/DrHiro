#!/usr/bin/env bash
# drHiro on TrueForge — verify the Android Bridge APK artifact.
#
# Recomputes the SHA-256 of drhiro-bridge.apk and compares it against the
# recorded checksum in apk.json, reports version + size, and enforces the size
# limit. Never prints the bot token or any secret.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

ENV_FILE="$(pwd)/.env"
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

APK_DIR="${APK_DIR:-./apk}"
APK_PATH="$APK_DIR/drhiro-bridge.apk"
META_PATH="$APK_DIR/apk.json"
MAX_MB="${APK_MAX_SIZE_MB:-45}"

info() { echo "[apk-verify] $*"; }
fail() { echo "[apk-verify] ERROR: $*" >&2; exit 1; }

[[ -f "$APK_PATH" ]] || fail "APK not found at $APK_PATH"
[[ -f "$META_PATH" ]] || fail "apk.json not found at $META_PATH"

SIZE="$(stat -c %s "$APK_PATH")"
MAX_BYTES=$(( MAX_MB * 1024 * 1024 ))
if [[ "$SIZE" -gt "$MAX_BYTES" ]]; then
  fail "APK is $SIZE bytes (> ${MAX_MB} MB limit). Refusing to serve."
fi
if [[ "$SIZE" -gt $(( 50 * 1024 * 1024 )) ]]; then
  fail "APK exceeds the Telegram Bot API 50 MB hard cap. Refusing to serve."
fi

ACTUAL="$(sha256sum "$APK_PATH" | awk '{print $1}')"
EXPECTED="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("sha256",""))' "$META_PATH")"
VERSION="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("version","unknown"))' "$META_PATH")"

if [[ -n "$EXPECTED" && "${ACTUAL,,}" != "${EXPECTED,,}" ]]; then
  fail "SHA-256 mismatch: recorded=${EXPECTED} actual=${ACTUAL}. Refusing to serve a tampered artifact."
fi

info "APK verified:"
info "  version:    $VERSION"
info "  size:       $SIZE bytes ($(awk "BEGIN{printf \"%.2f\", $SIZE/1048576}") MB)"
info "  sha256:     $ACTUAL"
info "  OK"
