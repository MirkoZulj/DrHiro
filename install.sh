#!/usr/bin/env bash
#
# drHiro on TrueForge — Ubuntu installer (22.04 / 24.04)
#
# Prompts for EXACTLY five inputs:
#   1. Telegram bot token
#   2. Authorized Telegram username
#   3. OpenAI-compatible AI backend base URL
#   4. AI backend API key (or a local placeholder key)
#   5. Model name
#
# Creates a protected .env (mode 600), validates the Telegram token WITHOUT
# exposing it, detects webhook/polling conflicts, validates the AI backend,
# builds and starts the stack, runs health checks, and prints safe commands.
#
# Safety: this script never prints the bot token or API key, never creates or
# deletes a Telegram webhook without explicit confirmation, and never runs long
# polling while a webhook is set.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[install]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC} $*"; }
fail()  { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Validate OS + privileges
# ---------------------------------------------------------------------------
info "Validating OS and privileges..."
command -v lsb_release >/dev/null 2>&1 || fail "lsb_release not found."
DISTRO="$(lsb_release -si 2>/dev/null || echo unknown)"
VER="$(lsb_release -sr 2>/dev/null || echo unknown)"
if [[ "$DISTRO" != "Ubuntu" ]]; then
  fail "This installer targets Ubuntu 22.04/24.04 (detected: $DISTRO $VER)."
fi
case "$VER" in
  22.04|24.04) info "Ubuntu $VER detected." ;;
  *) warn "Ubuntu $VER detected — installer targets 22.04/24.04; proceeding with caution." ;;
esac
if [[ "$(id -u)" -ne 0 ]]; then
  # Allow sudo re-exec.
  if command -v sudo >/dev/null 2>&1; then
    exec sudo bash "$0" "$@"
  fi
  fail "Please run as root (or with sudo)."
fi

# ---------------------------------------------------------------------------
# 2. Docker Engine + Compose
# ---------------------------------------------------------------------------
ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    info "Docker not found — installing Docker Engine (official convenience script)."
    curl -fsSL https://get.docker.com | sh
  fi
  docker --version >/dev/null 2>&1 || fail "Docker install failed."
  if ! docker compose version >/dev/null 2>&1; then
    if ! docker-compose --version >/dev/null 2>&1; then
      fail "Docker Compose plugin not available. Install docker-compose-plugin (apt install docker-compose-plugin)."
    fi
    warn "Only legacy docker-compose found; modern 'docker compose' is recommended."
  fi
  info "Docker ready: $(docker --version)"
  info "Compose ready: $(docker compose version 2>/dev/null || docker-compose --version)"
}
ensure_docker

# ---------------------------------------------------------------------------
# 3. Protected .env
# ---------------------------------------------------------------------------
ENV_FILE="$SCRIPT_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
  warn ".env already exists. Re-running will NOT overwrite existing values."
fi
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

# ---------------------------------------------------------------------------
# 4. Prompt for the five inputs
# ---------------------------------------------------------------------------
read_secret() { # prompt -> var
  local prompt="$1" var="$2" val
  read -rsp "$prompt" val; echo
  printf -v "$var" '%s' "$val"
}

echo
info "Configuration. Enter the five values (input is not echoed for secrets)."

# 4.1 Telegram bot token
BOT_TOKEN=""
if grep -q '^TELEGRAM_BOT_TOKEN=.\+' "$ENV_FILE"; then
  BOT_TOKEN="$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
fi
if [[ -z "$BOT_TOKEN" ]]; then
  read_secret "Telegram bot token (from @BotFather): " BOT_TOKEN
  [[ -n "$BOT_TOKEN" ]] || fail "Telegram bot token is required."
fi

# 4.2 Authorized username
ALLOWED_USER=""
if grep -q '^TELEGRAM_ALLOWED_USERNAME=.\+' "$ENV_FILE"; then
  ALLOWED_USER="$(grep '^TELEGRAM_ALLOWED_USERNAME=' "$ENV_FILE" | cut -d= -f2-)"
fi
if [[ -z "$ALLOWED_USER" ]]; then
  read -rp "Authorized Telegram username (no @): " ALLOWED_USER
  ALLOWED_USER="${ALLOWED_USER#@}"
  [[ -n "$ALLOWED_USER" ]] || fail "Authorized username is required."
fi

# 4.3 AI backend base URL
AI_BASE_URL=""
if grep -q '^AI_BACKEND_BASE_URL=.\+' "$ENV_FILE"; then
  AI_BASE_URL="$(grep '^AI_BACKEND_BASE_URL=' "$ENV_FILE" | cut -d= -f2-)"
fi
if [[ -z "$AI_BASE_URL" ]]; then
  read -rp "OpenAI-compatible AI backend base URL (e.g. https://api.openai.com/v1): " AI_BASE_URL
  [[ -n "$AI_BASE_URL" ]] || fail "AI backend base URL is required."
fi

# 4.4 AI API key (or placeholder)
AI_API_KEY=""
if grep -q '^AI_API_KEY=.\+' "$ENV_FILE"; then
  AI_API_KEY="$(grep '^AI_API_KEY=' "$ENV_FILE" | cut -d= -f2-)"
fi
if [[ -z "$AI_API_KEY" ]]; then
  read_secret "AI backend API key (or 'local' placeholder): " AI_API_KEY
  [[ -n "$AI_API_KEY" ]] || fail "AI API key is required (use 'local' for a local model)."
fi

# 4.5 Model name
AI_MODEL=""
if grep -q '^AI_MODEL=.\+' "$ENV_FILE"; then
  AI_MODEL="$(grep '^AI_MODEL=' "$ENV_FILE" | cut -d= -f2-)"
fi
if [[ -z "$AI_MODEL" ]]; then
  read -rp "Model name (as advertised by the backend): " AI_MODEL
  [[ -n "$AI_MODEL" ]] || fail "Model name is required."
fi

# 4.6 (OPTIONAL) Pairing public URL — reachable by the Android Bridge.
# The Android device reaches the server for pairing over this address. It must
# be an HTTPS (or trusted-LAN) URL the phone can reach, fronted by a reverse
# proxy that forwards to the internal pairing port. If left blank, pairing
# commands (/pair, /apk) will refuse to mint an unreachable link.
DRHIRO_PUBLIC_URL=""
if grep -q '^DRHIRO_PUBLIC_URL=.\\+' "$ENV_FILE"; then
  DRHIRO_PUBLIC_URL="$(grep '^DRHIRO_PUBLIC_URL=' "$ENV_FILE" | cut -d= -f2-)"
fi
if [[ -z "$DRHIRO_PUBLIC_URL" ]]; then
  read -rp "Pairing public URL the Android device reaches the server at (e.g. https://bridge.example.com; ENTER to skip): " DRHIRO_PUBLIC_URL
  if [[ -z "$DRHIRO_PUBLIC_URL" ]]; then
    warn "DRHIRO_PUBLIC_URL not set — Android Bridge pairing will be unavailable until it is configured in .env."
  fi
fi

# ---------------------------------------------------------------------------
# 5. Validate Telegram token WITHOUT exposing it
# ---------------------------------------------------------------------------
info "Validating Telegram bot token..."
VALIDATE_TOKEN="${VALIDATE_TOKEN:-1}"
if [[ "$VALIDATE_TOKEN" == "1" ]]; then
  BOT_USER="$(curl -sS -m 20 -X POST "https://api.telegram.org/bot${BOT_TOKEN}/getMe" \
    | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    print(d["result"]["username"] if d.get("ok") else "")
except Exception: print("")')"
  if [[ -z "$BOT_USER" ]]; then
    fail "Telegram token rejected. Check it with @BotFather. (Value not printed.)"
  fi
  info "Bot @$BOT_USER authenticated. (Token kept private.)"
fi

# ---------------------------------------------------------------------------
# 6. Webhook / polling conflict detection
# ---------------------------------------------------------------------------
info "Checking for an existing Telegram webhook..."
WEBHOOK_URL="$(curl -sS -m 20 -X POST "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo" \
  | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    print(d["result"].get("url","") if d.get("ok") else "")
except Exception: print("")')"
if [[ -n "$WEBHOOK_URL" ]]; then
  echo
  warn "A Telegram webhook is currently set to: $WEBHOOK_URL"
  warn "Long polling and a webhook CANNOT run at the same time."
  read -rp "Type 'CONFIRM' to DELETE the webhook and switch to long polling: " CONFIRM
  if [[ "$CONFIRM" != "CONFIRM" ]]; then
    fail "Webhook not removed. Aborting install — please remove it manually or rerun."
  fi
  curl -sS -m 20 -X POST "https://api.telegram.org/bot${BOT_TOKEN}/deleteWebhook" \
    | python3 -c 'import sys,json
d=json.load(sys.stdin); print("[install] Webhook deleted:", d.get("ok"))'
else
  info "No webhook configured — safe to use long polling."
fi

# ---------------------------------------------------------------------------
# 7. Validate AI backend connectivity + model availability
# ---------------------------------------------------------------------------
info "Validating AI backend connectivity and model availability..."
MODELS_JSON="$(curl -sS -m 30 "${AI_BASE_URL%/}/models" -H "Authorization: Bearer ${AI_API_KEY}" 2>/dev/null || true)"
# Qodo #10: parse the /models JSON and compare AI_MODEL against returned IDs —
# never execute the response as a Python program.
MODEL_MATCH="$(AI_MODEL_SAFE="$AI_MODEL" python3 -c 'import sys,json,os
model=os.environ["AI_MODEL_SAFE"]
try:
    d=json.load(sys.stdin)
    ids=[m.get("id","") for m in d.get("data",[])]
    sys.exit(0 if any(model==i or model in i for i in ids) else 1)
except Exception:
    sys.exit(2)' <<<"$MODELS_JSON" 2>/dev/null; echo $?)"
if [[ "$MODEL_MATCH" == "0" ]]; then
  info "Model '$AI_MODEL' confirmed available."
elif [[ "$MODEL_MATCH" == "1" ]]; then
  warn "Model '$AI_MODEL' not listed in /models — TrueForge will confirm at run time."
elif [[ "$MODEL_MATCH" == "2" ]]; then
  warn "Could not parse /models response (non-fatal; connectivity will be rechecked at startup)."
else
  warn "Could not list models from $AI_BASE_URL (non-fatal; connectivity will be rechecked at startup)."
fi

# ---------------------------------------------------------------------------
# 8. Write .env
# ---------------------------------------------------------------------------
cat > "$ENV_FILE" <<EOF
# drHiro on TrueForge — generated by install.sh $(date -u +%Y-%m-%dT%H:%M:%SZ)
TELEGRAM_BOT_TOKEN=${BOT_TOKEN}
TELEGRAM_ALLOWED_USERNAME=${ALLOWED_USER}
AI_BACKEND_BASE_URL=${AI_BASE_URL}
AI_API_KEY=${AI_API_KEY}
AI_MODEL=${AI_MODEL}
DRHIRO_PUBLIC_URL=${DRHIRO_PUBLIC_URL}
EOF
chmod 600 "$ENV_FILE"
info "Protected .env written (mode 600)."

# ---------------------------------------------------------------------------
# 9. Prepare TrueForge source if needed, then start the stack
# ---------------------------------------------------------------------------
if [[ ! -d "$SCRIPT_DIR/trueforge-src/.git" ]]; then
  info "Cloning TrueForge (MIT) source for hosted-mode build (one-time)..."
  git clone --depth 1 --branch "v${TRUEFORGE_VERSION:-0.1.4}" \
    https://github.com/truefoundry/trueforge.git "$SCRIPT_DIR/trueforge-src" \
    || git clone --depth 1 https://github.com/truefoundry/trueforge.git "$SCRIPT_DIR/trueforge-src"
fi

info "Building and starting the stack (first build can take several minutes)..."
docker compose --env-file "$ENV_FILE" up -d --build

# ---------------------------------------------------------------------------
# 10. Health checks
# ---------------------------------------------------------------------------
info "Running health checks..."
sleep 8
bash "$SCRIPT_DIR/scripts/health-check.sh" || fail "Health checks failed — see logs above."

# ---------------------------------------------------------------------------
# 11. Provision TrueForge (model provider + MCP server + agent)
# ---------------------------------------------------------------------------
if [[ -x "$SCRIPT_DIR/scripts/configure.sh" ]]; then
  info "Provisioning TrueForge (model provider, tools server, agent)..."
  bash "$SCRIPT_DIR/scripts/configure.sh" || warn "configure.sh reported issues — review."
fi

# ---------------------------------------------------------------------------
# 12. Android Bridge APK registration (OPTIONAL, approval-gated)
# ---------------------------------------------------------------------------
# Uploading the APK to Telegram is an external action — only proceed if the
# operator explicitly confirms AND a signed APK is present in ./apk.
if [[ -f "$SCRIPT_DIR/apk/drhiro-bridge.apk" && -x "$SCRIPT_DIR/scripts/apk-register.sh" ]]; then
  echo
  warn "A drHiro Bridge APK was found in ./apk."
  read -rp "Register it with Telegram now (upload + store file_id)? [y/N]: " REG_APK
  if [[ "${REG_APK,,}" == "y" ]]; then
    bash "$SCRIPT_DIR/scripts/apk-register.sh" || warn "APK registration failed — see output above."
  else
    info "Skipping APK registration. Send /apk to the bot later to register on demand."
  fi
fi

# ---------------------------------------------------------------------------
# 13. Safe operational commands
# ---------------------------------------------------------------------------
echo
info "Install complete. Safe operational commands:"
cat <<'CMDS'
  Status:          docker compose ps
  Logs:            docker compose logs -f --tail=100 telegram-bridge
  Health check:    ./scripts/health-check.sh
  Update:          ./scripts/update.sh
  Backup:          ./scripts/backup.sh
  Configure:       ./scripts/configure.sh
  Uninstall:       ./scripts/uninstall.sh
  APK verify:      ./scripts/apk-verify.sh
  APK info:        ./scripts/apk-info.sh
  APK register:    ./scripts/apk-register.sh   (uploads to Telegram; approval-gated)
  Pairing token:   ./scripts/create-pairing-token.sh <user-id>
  List devices:    ./scripts/list-paired-devices.sh <user-id>
  Revoke device:   ./scripts/revoke-device.sh <device-id> <user-id>
  Pairing link:    ./scripts/regenerate-pairing-link.sh <user-id>
  Stop:            docker compose stop
  Start:           docker compose start
  TrueForge admin: open http://localhost:8790 (TRUEFORGE_PORT)
CMDS
info "Done. The bot is now running with long polling. Never set a Telegram webhook on this token."
