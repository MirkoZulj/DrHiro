#!/usr/bin/env bash
# drHiro on TrueForge — uninstall. Destructive: stops and removes containers,
# volumes, and the protected .env. Requires explicit confirmation.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

echo "This will STOP and REMOVE all drHiro on TrueForge containers, networks,"
echo "volumes, and the protected .env file. This is DESTRUCTIVE and irreversible."
read -rp "Type 'UNINSTALL' to proceed: " CONFIRM
if [[ "$CONFIRM" != "UNINSTALL" ]]; then
  echo "[uninstall] Aborted."
  exit 1
fi

echo "[uninstall] Stopping and removing containers/volumes..."
docker compose down --volumes --remove-orphans

if [[ -f .env ]]; then
  echo "[uninstall] Removing protected .env..."
  rm -f .env
fi

echo "[uninstall] Done. To fully remove images, run: docker compose down --rmi all"
