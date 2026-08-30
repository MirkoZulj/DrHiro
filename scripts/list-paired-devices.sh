#!/usr/bin/env bash
# drHiro on TrueForge — list paired devices for a user.
#
# Usage: list-paired-devices.sh <telegram-user-id>
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ENV_FILE="$(pwd)/.env"; [[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

USER_ID="${1:?usage: list-paired-devices.sh <telegram-user-id>}"
PYTHONPATH="services/telegram-bridge/src" python3 - "$USER_ID" <<'PY'
import sys, os, json
from drhiro_bridge.pairing import PairingManager
uid = sys.argv[1]
state = os.environ.get("PAIRING_STATE_DIR", "data/pairing")
devices = PairingManager(state).list_devices(uid)
if not devices:
    print("[pairing] No linked devices for this user."); sys.exit(0)
print(f"[pairing] {len(devices)} linked device(s):")
for d in devices:
    print(f"  {d['device_id']}  {d['device_name']}  {'revoked' if d['revoked'] else 'active'}  {d['server_url']}")
PY
