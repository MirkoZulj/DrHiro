#!/usr/bin/env bash
# drHiro on TrueForge — register the Android Bridge APK with Telegram.
#
# Uploads drhiro-bridge.apk as a Telegram document on first server setup,
# stores Telegram's returned file_id in apk.json (mode 600), and resends by
# file_id on later runs. A failed upload never persists a file_id.
#
# This makes a REAL upload to Telegram — requires explicit human approval and
# a valid TELEGRAM_BOT_TOKEN in .env. Never prints the token.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

ENV_FILE="$(pwd)/.env"
[[ -f "$ENV_FILE" ]] || { echo "[apk-register] .env missing — run install.sh first." >&2; exit 1; }
set -a; source "$ENV_FILE"; set +a

APK_DIR="${APK_DIR:-./apk}"
APK_PATH="$APK_DIR/drhiro-bridge.apk"
META_PATH="$APK_DIR/apk.json"

info() { echo "[apk-register] $*"; }
fail() { echo "[apk-register] ERROR: $*" >&2; exit 1; }

[[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] || fail "TELEGRAM_BOT_TOKEN not set."
[[ -f "$APK_PATH" ]] || fail "APK not found at $APK_PATH."
[[ -f "$META_PATH" ]] || fail "apk.json not found — run apk-verify.sh first."

# 1. Verify checksum + size before anything else.
bash "$SCRIPT_DIR/apk-verify.sh" || fail "APK verification failed."

# 2. Compute the caption (version + sha256) without secrets.
VERSION="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("version","unknown"))' "$META_PATH")"
SHA="$(sha256sum "$APK_PATH" | awk '{print $1}')"
CAPTION="drHiro Bridge v${VERSION}
This is the official signed Android companion APK from your self-hosted drHiro server.
SHA-256: ${SHA}
Install:
Download the attached APK.
Open it from Telegram or your Downloads folder.
If Android asks, allow installation from this source.
Open drHiro Bridge.
Only install APK files sent by your own authorized drHiro bot."

# 3. Resend by existing file_id ONLY if it is bound to the current hash
#    (Qodo #9); an upgrade invalidates the stale id and forces a re-upload.
SHA="$(sha256sum "$APK_PATH" | awk '{print $1}')"
FID="$(python3 -c 'import json,sys
try:
    m=json.load(open(sys.argv[1]))
    if str(m.get("sha256","")).lower() == sys.argv[2].lower():
        print(m.get("file_id",""))
    else:
        print("")
except Exception: print("")' "$META_PATH" "$SHA")"

if [[ -n "$FID" ]]; then
  info "Reusing stored file_id for current APK hash (no re-upload)."
  RESP="$(curl -sS -m 60 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getFile" \
    -F "file_id=$FID" 2>&1 || true)"
  if python3 -c 'import sys,json
try: assert json.load(sys.stdin).get("ok")
except Exception: sys.exit(1)' <<<"$RESP" 2>/dev/null; then
    info "file_id still valid on Telegram."
  else
    info "Stored file_id stale; re-uploading."
    FID=""
  fi
fi

if [[ -z "$FID" ]]; then
  info "Uploading APK to Telegram (registration)..."
  RESP="$(curl -sS -m 180 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendDocument" \
    -F "document=@${APK_PATH}" -F "caption=${CAPTION}" 2>&1 || true)"
  if ! python3 -c 'import sys,json
try: assert json.load(sys.stdin).get("ok")
except Exception: sys.exit(1)' <<<"$RESP" 2>/dev/null; then
    fail "Upload failed. No file_id was stored. (Token not printed.)"
  fi
  FID="$(python3 -c 'import sys,json
try: print(json.load(sys.stdin)["result"]["document"]["file_id"])
except Exception: print("")' <<<"$RESP")"
  [[ -n "$FID" ]] || fail "Telegram returned no file_id."
  # Persist the file_id bound to the current APK hash (mode 600).
  python3 - "$META_PATH" "$FID" "$SHA" <<'PY'
import json, sys, time
meta_path, fid, sha = sys.argv[1], sys.argv[2], sys.argv[3]
meta = json.load(open(meta_path))
meta["file_id"] = fid
meta["sha256"] = sha
meta["file_id_set_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
open(meta_path, "w").write(json.dumps(meta, indent=2, sort_keys=True))
PY
  chmod 600 "$META_PATH"
  info "Uploaded and persisted file_id."
fi

info "APK registered. file_id stored in $META_PATH (mode 600)."
