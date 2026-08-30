#!/usr/bin/env bash
# drHiro on TrueForge — create a pairing token for an authorized user.
#
# Usage: create-pairing-token.sh <telegram-user-id> [server-url] [ttl-seconds]
# Prints the drhiro://pair deep link (single-use, time-limited).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ENV_FILE="$(pwd)/.env"; [[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

PY=python3
USER_ID="${1:?usage: create-pairing-token.sh <telegram-user-id> [server-url] [ttl]}"
SERVER="${2:-${DRHIRO_PUBLIC_URL:-http://localhost:${PAIRING_HTTP_PORT:-8091}}}"
TTL="${3:-${PAIRING_TTL_SECONDS:-600}}"

# Run in the bridge's python env. Here we import the module directly.
PYTHONPATH="services/telegram-bridge/src" $PY - "$USER_ID" "$SERVER" "$TTL" <<'PY'
import sys, os
from drhiro_bridge.pairing import PairingManager
uid, server, ttl = sys.argv[1], sys.argv[2], int(sys.argv[3])
state = os.environ.get("PAIRING_STATE_DIR", "data/pairing")
m = PairingManager(state, token_ttl=ttl)
try:
    r = m.create_token(uid, server)
except Exception as e:
    print(f"[pairing] ERROR: {e}", file=sys.stderr); sys.exit(1)
print("[pairing] Created single-use pairing token.")
print("  server:", r["server_url"])
print("  expires:", r["expiration_iso"], f"(in {ttl}s)")
if r["insecure"]: print("  WARNING:", r["warning"])
print("  link:", r["link"])
PY
