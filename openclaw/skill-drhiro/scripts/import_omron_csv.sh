#!/usr/bin/env bash
# drHiro OMRON CSV import from a Telegram file attachment.
#
# Usage:
#   import_omron_csv.sh <telegram_id> <file_id>
#
# The user sends the OMRON Connect CSV export as a document to the bot.
# The agent extracts the file_id from the message and calls this script.
#
# Steps:
#   1. Telegram Bot API getFile -> file_path
#   2. Download the file bytes
#   3. POST to drHiro /import/omron-csv with X-Service-Token + X-Telegram-Id
#   4. Print the import result (accepted / duplicates / rejected rows)

set -euo pipefail

TELEGRAM_ID="${1:?telegram_id required}"
FILE_ID="${2:?file_id required}"

API_BASE="${OPENCLAW_DRHIRO_API:-http://api:8000/api/v1}"
SERVICE_TOKEN="${DRHIRO_OPENCLAW_SERVICE_TOKEN:-}"
BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"

if [ -z "$SERVICE_TOKEN" ]; then
  echo "ERROR: DRHIRO_OPENCLAW_SERVICE_TOKEN not set" >&2
  exit 2
fi
if [ -z "$BOT_TOKEN" ]; then
  echo "ERROR: TELEGRAM_BOT_TOKEN not set" >&2
  exit 2
fi

TMP_FILE="/tmp/drhiro_import_${FILE_ID}.csv"
trap 'rm -f "$TMP_FILE"' EXIT

# 1. Resolve the file path from Telegram.
RESP="$(curl -sS "https://api.telegram.org/bot${BOT_TOKEN}/getFile?file_id=${FILE_ID}")"
FILE_PATH="$(printf '%s' "$RESP" | grep -o '"file_path":"[^"]*"' | cut -d'"' -f4 || true)"
if [ -z "$FILE_PATH" ]; then
  echo "ERROR: could not resolve Telegram file (bad file_id?): $RESP" >&2
  exit 2
fi

# 2. Download the file.
curl -sS -o "$TMP_FILE" "https://api.telegram.org/file/bot${BOT_TOKEN}/${FILE_PATH}"
if [ ! -s "$TMP_FILE" ]; then
  echo "ERROR: downloaded file is empty" >&2
  exit 2
fi

# 3. Upload to drHiro.
curl -sS -X POST "$API_BASE/import/omron-csv" \
  -H "X-Service-Token: $SERVICE_TOKEN" \
  -H "X-Telegram-Id: $TELEGRAM_ID" \
  -F "file=@${TMP_FILE};filename=omron.csv;type=text/csv"
echo
