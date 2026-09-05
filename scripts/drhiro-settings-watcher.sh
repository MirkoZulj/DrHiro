#!/usr/bin/env bash
# drhiro-settings-watcher — host-side apply of settings changes.
#
# Runs as ROOT on the host (NOT inside any container). No container mounts
# /var/run/docker.sock and no app code shells out to docker.
#
# Model: when the web Settings screen saves a change that a service binds at
# start (telegram-bridge, openclaw-gateway) or provisions externally
# (trueforge via configure.sh), the API writes an empty flag file named
# <service>.flag into RESTART_FLAGS_DIR. This watcher:
#   1. takes a flock so only one run executes at a time;
#   2. processes every pending flag in one run, then exits;
#   3. for each flag: reads the CURRENT settings directly from Postgres
#      (via `docker compose exec -T postgres psql` — DB stays internal-only;
#      port 5432 is never published to the host for this);
#   4. atomically regenerates .env (temp file, chmod 600, root-owned, mv);
#   5. runs configure.sh for a trueforge flag, then force-recreates the
#      affected service(s);
#   6. logs every action (timestamp, service, success/failure, exit code) to
#      a dedicated log — field NAMES only, NEVER values.
#
# A failed configure.sh or recreate leaves a .failed marker that
# health-check.sh detects, so a half-applied config is visible, not silent.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_DIR="$REPO_DIR"   # root docker-compose.yml lives here

ENV_FILE="$REPO_DIR/.env"
FLAGS_DIR="${RESTART_FLAGS_DIR:-/var/lib/drhiro/restart-flags}"
LOG_FILE="${DRHIRO_WATCHER_LOG:-/var/log/drhiro-settings-watcher.log}"
LOCK_FILE="/var/lock/drhiro-settings-watcher.lock"

# compose invocation: repo root has docker-compose.yml
COMPOSE=(docker compose --project-directory "$COMPOSE_DIR")

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >>"$LOG_FILE"; }

# Never log secret values: we only ever log service names, field NAMES, and
# exit codes — never the resolved token/key/url content.

# --- atomic .env regeneration -------------------------------------------------
# Read current editable settings from the app_settings row via postgres, then
# rewrite the editable keys in .env atomically. Keys map store column -> .env var.
read_setting() {  # $1 = column
  local col="$1"
  # shellcheck disable=SC2016
  "${COMPOSE[@]}" exec -T postgres psql -U "${POSTGRES_USER:-drhiro}" -d "${POSTGRES_DB:-drhiro}" \
    -tA -c "SELECT ${col} FROM app_settings WHERE id='singleton';" 2>>"$LOG_FILE"
}

regenerate_env() {
  [[ -f "$ENV_FILE" ]] || { log "regenerate_env: .env missing at $ENV_FILE"; return 1; }

  # Load current secrets/db creds from .env to query Postgres.
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a

  # Pull store values (may be empty if unset — then leave the existing .env key).
  local ai_url model ai_key tg_token tg_user
  ai_url="$(read_setting ai_backend_url | tr -d '\r')"
  model="$(read_setting model_name | tr -d '\r')"
  ai_key="$(read_setting ai_api_key | tr -d '\r')"
  tg_token="$(read_setting telegram_bot_token | tr -d '\r')"
  tg_user="$(read_setting telegram_allowed_username | tr -d '\r')"

  # Build a temp .env: copy existing, then override editable keys if the store
  # has a value. Atomic: write temp (600, root), then mv into place.
  local tmp
  tmp="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
  chmod 600 "$tmp"
  cp "$ENV_FILE" "$tmp"

  update_key() {  # $1 var, $2 value (may be empty -> leave as-is)
    local k="$1" v="$2"
    if [[ -n "$v" ]]; then
      if grep -qE "^${k}=" "$tmp"; then
        sed -i "s|^${k}=.*|${k}=${v}|" "$tmp"
      else
        printf '%s=%s\n' "$k" "$v" >>"$tmp"
      fi
    fi
  }
  update_key "AI_BACKEND_BASE_URL" "$ai_url"
  update_key "AI_MODEL" "$model"
  update_key "AI_API_KEY" "$ai_key"
  update_key "TELEGRAM_BOT_TOKEN" "$tg_token"
  update_key "TELEGRAM_ALLOWED_USERNAME" "$tg_user"

  chmod 600 "$tmp"   # ensure mode before mv
  chown root:root "$tmp"
  mv -f "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  log "regenerate_env: .env regenerated atomically"
}

# --- flag processing ----------------------------------------------------------
process_service() {
  local svc="$1"
  log "apply: processing $svc"
  case "$svc" in
    trueforge)
      # 1) regenerate .env from the store, 2) run configure.sh (reads .env),
      #    3) force-recreate trueforge.
      if ! regenerate_env; then
        echo "trueforge" >"$FLAGS_DIR/failed.flag"
        log "apply trueforge: .env regeneration FAILED"; return 1
      fi
      if ! bash "$REPO_DIR/scripts/configure.sh" >>"$LOG_FILE" 2>&1; then
        echo "trueforge" >"$FLAGS_DIR/failed.flag"
        log "apply trueforge: configure.sh FAILED (exit $?)"
        return 1
      fi
      "${COMPOSE[@]}" up -d --force-recreate trueforge >>"$LOG_FILE" 2>&1
      local code=$?
      if [[ $code -ne 0 ]]; then
        echo "trueforge" >"$FLAGS_DIR/failed.flag"
        log "apply trueforge: recreate FAILED (exit $code)"
        return 1
      fi
      log "apply trueforge: OK (configure.sh + recreate)"
      ;;
    telegram-bridge|openclaw-gateway)
      if ! regenerate_env; then
        echo "$svc" >"$FLAGS_DIR/failed.flag"
        log "apply $svc: .env regeneration FAILED"; return 1
      fi
      "${COMPOSE[@]}" up -d --force-recreate "$svc" >>"$LOG_FILE" 2>&1
      local code=$?
      if [[ $code -ne 0 ]]; then
        echo "$svc" >"$FLAGS_DIR/failed.flag"
        log "apply $svc: recreate FAILED (exit $code)"
        return 1
      fi
      log "apply $svc: OK (recreate)"
      ;;
    *)
      log "apply: unknown service '$svc' — ignored"
      ;;
  esac
}

main() {
  # flock: only one watcher run at a time.
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log "another watcher run in progress — exiting"
    exit 0
  fi

  mkdir -p "$FLAGS_DIR" "$(dirname "$LOG_FILE")" 2>/dev/null || true
  touch "$LOG_FILE"

  local any=0
  for flag in "$FLAGS_DIR"/*.flag; do
    [[ -e "$flag" ]] || continue
    local svc
    svc="$(basename "$flag")"
    svc="${svc%.flag}"
    # Skip our own failure marker in the pending loop.
    [[ "$svc" == "failed" ]] && { rm -f "$flag"; continue; }
    process_service "$svc"
    rm -f "$flag"
    any=1
  done
  if [[ $any -eq 1 ]]; then
    log "watcher run complete"
  fi
  exit 0
}

main "$@"
