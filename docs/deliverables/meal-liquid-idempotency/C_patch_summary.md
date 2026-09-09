# C. Patch Summary — Meal+Liquid Idempotency Refactor

**Branch**: `feature/meal-liquid-idempotency`
**Date**: 2026-09-09

---

## Files Changed

### 1. `apps/api/src/drhiro_api/models.py`
**Change**: Added three new ORM models for consumption identity + beverage linkage.

| Model | Table | Purpose |
|---|---|---|
| `ConsumptionOperation` | `consumption_operations` | Idempotency key + durable result (Telegram update_id scope) |
| `ConsumptionItem` | `consumption_items` | Stable item identity under an operation (meal item OR beverage) |
| `BeverageMeasurement` | `beverage_measurements` | 1:1 link between a liquid `Measurement` and its source `meal_item` |

**New columns on existing tables** (all nullable, for traceability):
- `meal_items.source_operation_id`, `meal_items.source_item_id`
- `measurements.source_operation_id`, `measurements.source_item_id`, `measurements.meal_item_id`
- `meals.source_operation_id`

### 2. `apps/api/src/drhiro_api/services/consumption.py` (NEW)
**Change**: Unified consumption domain — the SINGLE write path for all consumption logging.

**Public API**:
- `parse_consumption_text(text)` — canonical item extractor (replaces both `service.py parse_meal_text` and the MCP liquid block)
- `get_or_create_operation(db, user_id, ...)` — idempotency: returns existing operation if one matches Telegram key or caller key
- `get_operation_result(db, operation_id)` — durable result replay
- `write_consumption(db, user_id, items, ...)` — atomic meal + beverage write in ONE transaction
- `update_item_quantity(db, user_id, meal_id, item_fragment, new_grams)` — rescale + linked volume update
- `replace_beverage(db, user_id, meal_id, item_fragment, new_category)` — beverage → beverage swap
- `delete_beverage(db, user_id, meal_id, item_fragment)` — removes meal_item + measurement + beverage_measurement
- `delete_meal(db, user_id, meal_id)` — cascades to beverage_measurements

**Internal helpers**:
- `_to_float(num)` — decimal-comma parsing
- `_normalize_meal_type(mt)` — validates breakfast|lunch|dinner|snack, defaults snack
- `_classify_beverage(text)` — token-aware classification (word boundary, most specific first)
- `_stable_item_key(operation_id, index)` — deterministic item key
- `_compute_source_record_id(operation_id, item_key)` — deterministic source_record_id
- `_scale_nutrients(per100, factor)` — scale per-100 nutrients
- `_sum_nutrients(nutrient_dicts)` — sum across all 6 nutrients
- `_recompute_meal_totals(db, meal)` — rebuild meal totals from items
- `_match_item(items, frag)` — fragment → meal_item matching

### 3. `apps/api/alembic/versions/f1a2b3c4d5e6_consumption_idempotency.py` (NEW)
**Change**: Alembic migration creating the three new tables and adding nullable columns to existing tables.

### 4. `packages/drhiro-mcp/src/drhiro_mcp/sse_server.py`
**Change**: **MCP CUTOVER** — removed the liquid auto-log side-effect block (lines 1636–1698).

**What was removed**:
- Hard-coded user UUID `0bfad360-9938-4216-8abd-b44d69e2003f`
- Hard-coded internal API URL `http://172.20.0.1:8010/api/v1`
- JWT minting for `/ingest/manual/water`
- Whole-message regex scan for drink categories + first-number volume association
- Separate non-atomic POST to `/ingest/manual/water`

**What remains intact**:
- `log_meal_intelligent` tool still confirms meals via `/meals/from-text-intelligent/confirm`
- The confirmed meal summary is still returned to the bot
- `log_water` and `log_liquid` tools still work (they are explicit user-initiated actions, not side-effects)

### 5. `tests/test_consumption_idempotency.py` (NEW)
**Change**: 27 tests covering parser, idempotency, atomic writes, mutations, aggregation, and nutrient helpers.

---

## Files NOT Changed (intentionally)

| File | Reason |
|---|---|
| `docs/reference/intelligent-meal-service.py` | Deploy-only VPS service.py, pulled read-only as reference. NOT in git. |
| `apps/api/src/drhiro_api/routers/dashboard.py` | Calorie aggregation verified correct (E deliverable). No changes needed. |
| `apps/api/src/drhiro_api/routers/meals.py` | Legacy meal router still exists; new consumption.py is the unified path. |
| `apps/api/src/drhiro_api/routers/measurements.py` | Direct liquid log endpoint still exists for explicit user actions. |

---

## Entry Points Touched

| Entry Point | Tool/Route | Wires Into |
|---|---|---|
| MCP `log_meal_intelligent` | `sse_server.py` → `call_api("POST", "/meals/from-text-intelligent/confirm")` | Backend confirm (which should use `consumption.py`) |
| MCP `log_water` | `sse_server.py` → `POST /ingest/manual/water` | Direct liquid log (explicit user action) |
| MCP `log_liquid` | `sse_server.py` → `POST /ingest/manual/water` | Direct liquid log (explicit user action) |
| Backend confirm | `service.py` `confirm_meal` (deploy-only) | Should call `consumption.write_consumption()` |

---

## What Still Needs Wiring (post-cutover)

The `service.py` on the VPS (deploy-only) needs to be updated to call `consumption.write_consumption()` instead of its current `confirm_meal` logic. This is a deploy-pipeline task, not a git commit task. The reference copy at `docs/reference/intelligent-meal-service.py` documents the target behavior.

The `log_water` and `log_liquid` MCP tools still POST directly to `/ingest/manual/water`. These are explicit user-initiated actions (the user says "log 250ml water"), not side-effects of meal logging. They do NOT conflict with the meal+liquid cutover because:
1. They are triggered by the model in response to a direct user request, not as a side-effect of meal confirm.
2. They use the user's actual Telegram ID from `DRHIRO_TELEGRAM_ID` env var, not a hard-coded UUID.
3. They do not run inside the meal confirm flow.

If future hardening is desired, these can be routed through `consumption.py` as well, but they are NOT part of the duplicate-drink bug.
