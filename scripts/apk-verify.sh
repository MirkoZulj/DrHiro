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
warn() { echo "[apk-verify] WARNING: $*"; }
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

# Qodo #7: the recorded checksum is REQUIRED. A missing/malformed sha256 is
# refused — an artifact with no expected checksum must not pass as "verified".
if [[ -z "$EXPECTED" || "${#EXPECTED}" -ne 64 ]]; then
  fail "apk.json is missing a valid sha256 — refusing to serve an unverified APK."
fi
if [[ "${ACTUAL,,}" != "${EXPECTED,,}" ]]; then
  fail "SHA-256 mismatch: recorded=${EXPECTED} actual=${ACTUAL}. Refusing to serve a tampered artifact."
fi

# Qodo #7: verify the Android signing certificate against the trusted signer
# fingerprint recorded in apk.json, when apksigner is available.
TRUSTED_SIGNER="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("signer_sha256",""))' "$META_PATH")"
if [[ -n "$TRUSTED_SIGNER" ]]; then
  APKSIGNER_BIN="${APKSIGNER:-}"
  if [[ -z "$APKSIGNER_BIN" ]]; then
    for c in /usr/bin/apksigner /usr/lib/android-sdk/build-tools/current/apksigner; do
      [[ -x "$c" ]] && APKSIGNER_BIN="$c" && break
    done
  fi
  if [[ -n "$APKSIGNER_BIN" ]]; then
    CERT="$( "$APKSIGNER_BIN" verify --print-certs "$APK_PATH" 2>/dev/null \
      | grep -i 'certificate SHA-256 digest' | head -1 | awk '{print $NF}' )"
    if [[ -z "$CERT" ]]; then
      fail "Could not extract the APK signing certificate — refusing to serve an unverified artifact."
    fi
    if [[ "${CERT,,}" != "${TRUSTED_SIGNER,,}" ]]; then
      fail "APK signing certificate does not match the trusted signer — refusing to serve a substituted artifact."
    fi
    info "  signer:     $CERT (matches trusted signer)"
  else
    warn "apksigner not found — skipping signing-certificate verification (sha256 still enforced)."
  fi
fi

info "APK verified:"
info "  version:    $VERSION"
info "  size:       $SIZE bytes ($(awk "BEGIN{printf \"%.2f\", $SIZE/1048576}") MB)"
info "  sha256:     $ACTUAL"
info "  OK"
