# Credential & Auth Refactor — Follow-Up Backlog

This document lists remaining hard-coded / derived credential issues OUTSIDE the
touched paths of the meal+liquid idempotency refactor. No secret values are
printed; all are shown as `<REDACTED>`.

## Touched Paths (DONE in Phase C)

- `packages/drhiro-mcp/src/drhiro_mcp/sse_server.py`:
  - `log_water`, `log_liquid` — hard-coded user JWT minting + hard-coded
    `http://172.20.0.1:8010/api/v1` REMOVED. Now routes through `call_api()` using
    configured `DRHIRO_MCP_TOKEN` + `DRHIRO_API_URL`.
  - `log_activity`, `list_activities`, `update_activity`, `list_data_points`,
    `update_data_point`, `delete_data_point`, `delete_activity` — same cleanup:
    removed inline JWT minting, now route through `call_api()`.
  - `learn_food`, `analyze_food_photo`, `correct_meal_item`, `ddg_nutrition_lookup`,
    `search_food`, `log_meal_intelligent`, `confirm_intelligent_meal`,
    `build_recipe`, `log_recipe_meal`, `list_recipes`, `delete_meal`,
    `set_custom_nutrition`, `get_steps`, `get_daily_metrics`, `get_weight`,
    `get_blood_pressure`, `get_device_code`, `get_reminders`, `create_reminder`,
    `get_trends`, `log_weight`, `log_blood_pressure`, `log_meal` — already used
    `call_api()`; no changes needed.

## Remaining Credential Issues (Follow-Up)

### 1. `apps/api/src/drhiro_api/services/intelligent_meal_service_patch.py`
- **Issue**: Hard-coded `DB_URL` placeholder with `<DB_USER>:<DB_PASS>@<DB_HOST>/<DB_NAME>`.
  The actual connection string is env-resolved at runtime, but the literal
  placeholder pattern contains angle brackets that could confuse tooling.
- **Severity**: Low (env-resolved at runtime).
- **Action**: Sanitize placeholder format; verify env loading.

### 2. `apps/api/src/drhiro_api/main.py` (or `app.py`)
- **Issue**: CORS `allow_origins=["*"]` is open. In production this should be
  restricted to the drHiro frontend origin(s).
- **Severity**: Medium (depends on deployment topology).
- **Action**: Restrict to configured origin via env.

### 3. `packages/drhiro-mcp/src/drhiro_mcp/sse_server.py` — JWT secret fallback
- **Issue**: `DRHIRO_JWT_SECRET` env var is read in several (now-removed) places;
  if the env var is empty, jwt.encode uses empty string as key. The removed
  code paths no longer mint JWTs, but any future code must not reintroduce this
  pattern.
- **Severity**: Low (no remaining usage).
- **Action**: Add linting rule: flag `jwt.encode` with string literal secrets.

### 4. `VISION_BASE_URL`, `VISION_MODEL` in sse_server.py
- **Issue**: Hard-coded Windows path `E:\\Models\\Qwen3.8-27B-UD-Q4_K_M.gguf` in
  default value. Not a credential but machine-specific.
- **Severity**: Low.
- **Action**: Ensure env override in production.

### 5. `SERVICE_TOKEN` usage pattern
- **Issue**: `_headers()` sends `x-service-token` for `/meals/` and `/tools/`
  paths. If `SERVICE_TOKEN` is empty, the header is empty — the backend must
  reject empty tokens.
- **Severity**: Medium (depends on backend validation).
- **Action**: Verify backend rejects empty service tokens.

### 6. Test database URL in tests
- **Issue**: Tests use `postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_test`.
  This is a test credential, not production, but should be env-resolved for CI.
- **Severity**: Low.
- **Action**: Move to env var.

## Reconciliation Note

The new `/manual/liquid` endpoint (`apps/api/src/drhiro_api/routers/ingest.py`) routes through `consumption.log_manual_liquid` which enforces three-intent reconciliation:
- Same-event idempotent replay (source identity)
- Explicit-reference reconciliation (`existing_item_id`)
- Genuinely new drink (`intent: "new"`) + CLARIFY for ambiguous intent

This endpoint does NOT mint JWTs or use hard-coded credentials. It uses the standard `get_current_user` dependency (same as all other `/manual/*` endpoints).

## Verification

- Grep for `0bfad360-9938-4216-8abd-b44d69e2003f` in sse_server.py: only a
  historical comment remains (line ~1608, documenting the removed block).
- Grep for `172.20.0.1` in sse_server.py: zero hits.
- Grep for `<redacted-secret>` in repo: zero hits (never in repo).
- Grep for `change-me-in-production`: zero hits.
- Grep for `drhiro:drhiro@`: only in test DB URL defaults (low severity).

---
*Prepared during Phase C-E of the meal+liquid idempotency refactor. Branch: feature/meal-liquid-idempotency.*
