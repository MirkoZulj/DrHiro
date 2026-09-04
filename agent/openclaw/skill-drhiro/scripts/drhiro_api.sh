#!/usr/bin/env bash
# drHiro API helper — the ONLY way the agent calls the drHiro Core API.
#
# Usage:
#   drhiro_api.sh <telegram_id> <method> <path> [json-body]
#
# Examples:
#   drhiro_api.sh <TELEGRAM_ID> GET /tools/get_my_today_summary
#   drhiro_api.sh <TELEGRAM_ID> POST /tools/create_manual_weight '{"value": 82.4}'
#   drhiro_api.sh <TELEGRAM_ID> POST /tools/create_manual_bp '{"systolic":128,"diastolic":78,"pulse":64}'
#
# Auth:
#   - X-Service-Token: signed gateway identity (DRHIRO_OPENCLAW_SERVICE_TOKEN env)
#   - X-Telegram-Id:   the sender's Telegram id, passed as arg 1 (never invented)
#
# The drHiro API resolves the drHiro user from the Telegram id server-side.
# The agent must NEVER invent a telegram id; it reads it from the session.

set -euo pipefail

TELEGRAM_ID="${1:?telegram_id required}"
METHOD="${2:?method required}"
PATH_SPEC="${3:?path required}"
BODY="${4:-}"

API_BASE="${OPENCLAW_DRHIRO_API:-http://api:8000/api/v1}"
SERVICE_TOKEN="${DRHIRO_OPENCLAW_SERVICE_TOKEN:-}"

if [ -z "$SERVICE_TOKEN" ]; then
  echo "ERROR: DRHIRO_OPENCLAW_SERVICE_TOKEN not set" >&2
  exit 2
fi

ARGS=(-sS -X "$METHOD" "$API_BASE$PATH_SPEC"
      -H "X-Service-Token: $SERVICE_TOKEN"
      -H "X-Telegram-Id: $TELEGRAM_ID"
      -H "Content-Type: application/json")

if [ -n "$BODY" ]; then
  ARGS+=(-d "$BODY")
fi

# shellcheck disable=SC2068
curl "${ARGS[@]}"
echo
