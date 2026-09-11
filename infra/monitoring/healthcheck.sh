# drHiro uptime + disk alerting (systemd timer runs this on the VPS).
# Delivers to Telegram via a simple curl webhook. Adjust to taste.

DRHIRO_TG_CHAT="${DRHIRO_TG_CHAT_ID:-}"
DRHIRO_TG_TOKEN="${TELEGRAM_BOT_TOKEN:-}"

send() {
  [ -n "$DRHIRO_TG_TOKEN" ] && [ -n "$DRHIRO_TG_CHAT" ] || return 0
  curl -s -X POST "https://api.telegram.org/bot${DRHIRO_TG_TOKEN}/sendMessage" \
    -d chat_id="$DRHIRO_TG_CHAT" -d text="$1" > /dev/null
}

DISK_PCT=$(df -h / | awk 'NR==2 {print $5}' | tr -d '%')
if [ "$DISK_PCT" -gt 85 ]; then
  send "drHiro: disk at ${DISK_PCT}% on $(hostname)"
fi

# Postgres reachable?
if ! pg_isready -q -U "${POSTGRES_USER:-drhiro}" -h localhost; then
  send "drHiro: Postgres is DOWN on $(hostname)"
fi

# API healthy?
if ! curl -sf http://localhost:8000/health > /dev/null 2>&1; then
  send "drHiro: API health check FAILED on $(hostname)"
fi
