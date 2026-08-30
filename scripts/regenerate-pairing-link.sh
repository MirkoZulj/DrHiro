#!/usr/bin/env bash
# drHiro on TrueForge — regenerate a fresh pairing link (without resending APK).
#
# Usage: regenerate-pairing-link.sh <telegram-user-id> [server-url] [ttl-seconds]
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ENV_FILE="$(pwd)/.env"; [[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

USER_ID="${1:?usage: regenerate-pairing-link.sh <telegram-user-id> [server-url] [ttl]}"
SERVER="${2:-${DRHIRO_PUBLIC_URL:-http://localhost:${PAIRING_HTTP_PORT:-8091}}}"
TTL="${3:-${PAIRING_TTL_SECONDS:-600}}"
exec "$(dirname "${BASH_SOURCE[0]}")/create-pairing-token.sh" "$USER_ID" "$SERVER" "$TTL"
