#!/usr/bin/env bash
# drHiro on TrueForge — provision TrueForge: model provider, MCP tools server,
# and the drhiro agent (with approval gate on save_visit_brief).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

ENV_FILE="$SCRIPT_DIR/../.env"
[[ -f "$ENV_FILE" ]] || { echo "[configure] .env missing — run install.sh first." >&2; exit 1; }
set -a; source "$ENV_FILE"; set +a

TF="${TRUEFORGE_URL:-http://localhost:${TRUEFORGE_PORT:-8790}}/api/v1"
TF_BASE="${TRUEFORGE_URL:-http://localhost:${TRUEFORGE_PORT:-8790}}"

info() { echo "[configure] $*"; }
fail() { echo "[configure] ERROR: $*" >&2; exit 1; }

# 1. Wait for TrueForge
for i in $(seq 1 30); do
  if curl -sf -m 5 "$TF_BASE/healthz" >/dev/null 2>&1; then break; fi
  [[ $i -eq 30 ]] && fail "TrueForge not reachable at $TF_BASE after 30 tries."
  sleep 4
done
info "TrueForge reachable."

# 2. Register the OpenAI-compatible model provider
# TrueForge settings/model-providers: POST with {name, provider, config}
info "Registering model provider..."
curl -sf -m 30 -X POST "$TF/settings/model-providers" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"ai-backend\",\"provider\":\"openai-completions\",\"config\":{\"baseUrl\":\"${AI_BACKEND_BASE_URL%/}\",\"apiKey\":\"${AI_API_KEY}\"}}" \
  >/dev/null 2>&1 || warn "Model provider registration returned non-2xx (may already exist)."

# 3. Register the drHiro tools MCP server (SSE URL inside the compose network)
info "Registering drhiro-tools MCP server..."
curl -sf -m 30 -X POST "$TF/settings/mcp-servers" \
  -H 'Content-Type: application/json' \
  -d '{"name":"drhiro-tools","type":"sse","url":"http://drhiro-tools:3100/sse","authType":"none"}' \
  >/dev/null 2>&1 || warn "MCP server registration returned non-2xx (may already exist)."

# 4. Create / update the drhiro agent from agent/drhiro.agent.json
info "Creating drhiro agent..."
SPEC_FILE="$SCRIPT_DIR/../agent/drhiro.agent.json"
# Substitute the model name into the spec.
SPEC="$(AI_MODEL_SAFE="$AI_MODEL" python3 -c 'import os,sys;print(open(sys.argv[1]).read().replace("${AI_MODEL}",os.environ["AI_MODEL_SAFE"]))' "$SPEC_FILE")"

# Try create; fall back to update on conflict.
CODE="$(curl -s -o /tmp/tf_agent_resp.json -w '%{http_code}' -m 30 -X POST "$TF/agents" \
  -H 'Content-Type: application/json' -d "$SPEC" 2>/dev/null || echo 000)"
if [[ "$CODE" == "409" ]]; then
  info "Agent already exists — updating..."
  AGENT_ID="$(curl -sf -m 20 "$TF/agents" | python3 -c 'import sys,json;d=json.load(sys.stdin);a=[x for x in d.get("data",[]) if x.get("name")=="drhiro"];print(a[0]["id"] if a else "")' 2>/dev/null || true)"
  [[ -n "$AGENT_ID" ]] && curl -sf -m 30 -X PUT "$TF/agents/$AGENT_ID" -H 'Content-Type: application/json' -d "$SPEC" >/dev/null 2>&1 || warn "agent update failed"
elif [[ "$CODE" != "200" && "$CODE" != "201" ]]; then
  warn "Agent create returned HTTP $CODE (see /tmp/tf_agent_resp.json)."
fi
info "Provisioning complete. Agent 'drhiro' is ready (approval gate: save_visit_brief)."
