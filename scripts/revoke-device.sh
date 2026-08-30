#!/usr/bin/env bash
# drHiro on TrueForge — revoke a paired device.
#
# Usage: revoke-device.sh <device-id> <telegram-user-id>
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ENV_FILE="$(pwd)/.env"; [[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

DEVICE_ID="${1:?usage: revoke-device.sh <device-id> <telegram-user-id>}"
USER_ID="${2:?usage: revoke-device.sh <device-id> <telegram-user-id>}"
PYTHONPATH="services/telegram-bridge/src" python3 - "$DEVICE_ID" "$USER_ID" <<'PY'
import sys, os
from drhiro_bridge.pairing import PairingManager
dev, uid = sys.argv[1], sys.argv[2]
state = os.environ.get("PAIRING_STATE_DIR", "data/pairing")
ok = PairingManager(state).revoke_device(dev, uid)
if ok:
    print(f"[pairing] Device {dev[:8]}… revoked.")
else:
    print(f"[pairing] ERROR: device not found or not owned by this user.", file=sys.stderr); sys.exit(1)
PY
