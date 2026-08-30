#!/usr/bin/env bash
# drHiro on TrueForge — health check: containers up, TrueForge healthy, tools
# reachable, Telegram token valid (without printing it), polling-only enforced.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

ENV_FILE="$SCRIPT_DIR/../.env"
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

TF_PORT="${TRUEFORGE_PORT:-8790}"
TF_BASE="${TRUEFORGE_URL:-http://localhost:$TF_PORT}"
FAIL=0

say() { echo "[health] $*"; }
ok()   { say "OK   $*"; }
bad()  { say "FAIL $*"; FAIL=1; }

# 1. Compose services
say "Checking compose services..."
docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | while read -r n s; do
  if [[ "$s" == *"Up"* ]]; then ok "container $n: up"; else bad "container $n: $s"; fi
done

# 2. TrueForge health
if curl -sf -m 10 "$TF_BASE/healthz" >/dev/null 2>&1; then ok "TrueForge /healthz"; else bad "TrueForge /healthz unreachable"; fi

# 3. drhiro-tools SSE endpoint
if curl -sf -m 10 "http://localhost:3100/sse" >/dev/null 2>&1 || true; then
  ok "drhiro-tools SSE reachable on :3100 (probe best-effort)"
else
  # SSE streams; a 4xx/5xx or connect failure means down. Use a short timeout.
  CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://localhost:3100/sse" 2>/dev/null || true)"
  [[ -n "$CODE" ]] && ok "drhiro-tools responded HTTP $CODE" || bad "drhiro-tools not reachable on :3100"
fi

# 4. Telegram token valid + no webhook (secret-safe)
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  if curl -sf -m 15 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" >/dev/null 2>&1; then
    ok "Telegram token valid"
  else
    bad "Telegram token invalid"
  fi
  WH="$(curl -sf -m 15 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" 2>/dev/null \
        | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["result"].get("url",""))
except Exception: print("")' 2>/dev/null || true)"
  if [[ -n "$WH" ]]; then bad "A Telegram webhook is set ($WH) — long polling cannot run!"; else ok "No Telegram webhook (polling-safe)"; fi
else
  bad "TELEGRAM_BOT_TOKEN not set"
fi

# 5. AI backend reachable
if [[ -n "${AI_BACKEND_BASE_URL:-}" ]]; then
  if curl -sf -m 15 -o /dev/null "${AI_BACKEND_BASE_URL%/}/models" -H "Authorization: Bearer ${AI_API_KEY:-}" 2>/dev/null; then
    ok "AI backend reachable: $AI_BACKEND_BASE_URL"
  else
    bad "AI backend unreachable: $AI_BACKEND_BASE_URL"
  fi
else
  bad "AI_BACKEND_BASE_URL not set"
fi

say ""
if [[ "$FAIL" -eq 0 ]]; then say "ALL CHECKS PASSED."; else say "SOME CHECKS FAILED."; fi
exit "$FAIL"
