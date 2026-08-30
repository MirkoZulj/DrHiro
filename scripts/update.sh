#!/usr/bin/env bash
# drHiro on TrueForge — update: pull latest images, rebuild drHiro services,
# run health checks. TrueForge source is refreshed if it is a git checkout.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."
ENV_FILE="$(pwd)/.env"
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

info() { echo "[update] $*"; }
info "Refreshing TrueForge source (if a git checkout)..."
if [[ -d trueforge-src/.git ]]; then
  ( cd trueforge-src && git pull --ff-only 2>/dev/null ) || info "TrueForge source refresh skipped (no upstream or local edits)."
fi

info "Pulling latest base images and rebuilding drHiro services..."
docker compose --env-file "$ENV_FILE" pull 2>/dev/null || true
docker compose --env-file "$ENV_FILE" up -d --build --remove-orphans

info "Running health checks..."
sleep 8
exec bash "$SCRIPT_DIR/health-check.sh"
