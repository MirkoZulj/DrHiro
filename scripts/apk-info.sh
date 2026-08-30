#!/usr/bin/env bash
# drHiro on TrueForge — report APK delivery status without revealing secrets.
#
# Prints version, size, sha256, whether a file_id is registered, and delivery
# readiness. Never prints the bot token or the file_id value itself.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

ENV_FILE="$(pwd)/.env"
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

APK_DIR="${APK_DIR:-./apk}"
APK_PATH="$APK_DIR/drhiro-bridge.apk"
META_PATH="$APK_DIR/apk.json"

info() { echo "[apk-info] $*"; }

if [[ ! -f "$APK_PATH" ]]; then
  info "APK: MISSING (place drhiro-bridge.apk in $APK_DIR)"
  exit 1
fi

VERSION="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("version","unknown"))' "$META_PATH" 2>/dev/null || echo unknown)"
SIZE="$(stat -c %s "$APK_PATH")"
SHA="$(sha256sum "$APK_PATH" | awk '{print $1}')"
REGISTERED="$(python3 -c 'import json,sys
try:
    m=json.load(open(sys.argv[1])); print("yes" if m.get("file_id") else "no")
except Exception: print("no")' "$META_PATH")"

info "APK delivery status:"
info "  file:          $APK_PATH"
info "  version:       $VERSION"
info "  size:          $SIZE bytes ($(awk "BEGIN{printf \"%.2f\", $SIZE/1048576}") MB)"
info "  sha256:        $SHA"
info "  registered:    $REGISTERED (file_id is stored locally, not shown)"
info "  status:        $(if [[ "$REGISTERED" == "yes" ]]; then echo 'ready to serve via /apk'; else echo 'not yet registered — run apk-register.sh'; fi)"
