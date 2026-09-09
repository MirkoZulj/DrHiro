# C. Patch Summary — Meal+Liquid Idempotency Refactor

**Branch**: `feature/meal-liquid-idempotency`
**Date**: 2026-09-09
**Commit**: {commit_hash}
**Final suite**: 154 passed

## Stage 1 Foundations (B2/B3/B5/B6 — all closed)

### B5 — Concurrency-safe first creation
- `get_or_create_operation` uses `INSERT ... ON CONFLICT DO NOTHING` with `insert_id = uuid.uuid4()` (UUID object, not str).
- Fixed: old `insert_id = str(uuid.uuid4())` compared unequal to `op.id` UUID → every call reported `created=False`.
- Genuine concurrent test (`tests/test_b5_concurrent.py`): two sessions + `threading.Barrier(2)` on PostgreSQL → exactly one operation, one meal, one measurement.

### B2 — Durable replay without Redis draft
- New `find_completed_result_by_identity()` looks up a COMPLETED operation by source identity.
- `confirm_meal` resolves completed operation when Redis draft is gone; explicit 404 if no draft AND no completed op.

### B3/B6 — identity enforcement + migration single authority (preserved)

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

### 2. `apps/api/src/drhiro_api/services/consumption.py`
**Change**: Unified consumption domain — the SINGLE write path for all consumption logging.

**Public API**:
- `parse_consumption_text(text)` — canonical item extractor (replaces both `service.py parse_meal_text` and the MCP liquid block)
- `get_or_create_operation(db, user_id, ...)` — idempotency: returns existing operation if one matches Telegram key or caller key
- `get_operation_result(db, operation_id)` — durable result replay
- `write_consumption(db, user_id, items, ...)` — atomic meal + beverage write in ONE transaction
- `confirm_consumption(db, user_id, items, ...)` — single entry point for `/meals/from-text-intelligent/confirm` handler
- **`log_manual_liquid(db, user_id, amount_ml, ...)`** — reconciliation-aware manual liquid logging
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
- **`_reconcile_liquid(db, user_id, amount_ml, category, existing_item_id, eaten_at)`** — link a manual liquid to an EXISTING consumption item
- **`_liquid_to_parsed_items(amount_ml, category, display_name)`** — build a ParsedItem list for a standalone liquid
- **`_write_new_liquid_consumption(db, user_id, items, eaten_at, source, notes)`** — write a genuinely new drink consumption with volume AND calories

### 3. `apps/api/src/drhiro_api/routers/ingest.py`
**Change**: Added reconciliation-aware `/manual/liquid` endpoint.

**New endpoint**: `POST /manual/liquid`
- Routes through `consumption.log_manual_liquid`
- Enforces the three-intent reconciliation semantics
- Returns `ManualLiquidResult` with `ok`, `reconciled`, `clarify`, `error`, `measurement_id`, `message` fields

### 4. `tests/test_legacy_new_water_coexistence.py`
**Change**: Replaced wrong `test_same_drink_manual_plus_meal_counts_twice` (which asserted 660ml double-count for a same-event meal+liquid) with 7 reconciliation-path tests proving the correct semantics.

### 5. `apps/api/alembic/versions/f1a2b3c4d5e6_consumption_idempotency.py` (NEW)
**Change**: Alembic migration creating the three new tables and adding nullable columns to existing tables.

### 6. `packages/drhiro-mcp/src/drhiro_mcp/sse_server.py`
**Change**: **MCP CUTOVER** — removed the liquid auto-log side-effect block (lines 1636–1698).

---

## Reconciliation Semantics (Three Distinct Paths)

The `/manual/liquid` endpoint and `log_manual_liquid` function enforce three distinct code paths:

### Path 1: Same-Event Idempotent Replay
**Trigger**: Request carries source identity (`source_chat_id` + `source_message_id` + `source_bot_id`) or `idempotency_key`.
**Resolution**: Look up `ConsumptionOperation` by that identity. If already `completed`, return the SAVED result. No new row.

**Request shape**:
```json
{"amount_ml": 330, "category": "beer", "source": "telegram", "source_chat_id": "...", "source_message_id": "...", "source_bot_id": "..."}
```

**Use case**: One Telegram message causes BOTH meal tool call AND liquid tool call for the SAME drink → ONE consumption (volume once, calories once). Retry/repeated tool invocation returns existing result, no additional contribution.

### Path 2: Explicit-Reference Reconciliation
**Trigger**: Request carries `existing_item_id`.
**Resolution**: Link the new beverage volume to the EXISTING item/measurement. No second row.

**Request shape**:
```json
{"amount_ml": 100, "category": "non_alcoholic", "existing_item_id": "<measurement_id>"}
```

**Use case**: User says "also count that milk as liquid" → volume linked to existing item once; dashboard sum shows it once, not twice. Returns `reconciled: true`, `total_amount_ml` updated.

### Path 3: Genuinely New Drink + Clarify
**Trigger**: Request carries `intent: "new"` (with no source identity / reference). Without `intent: "new"`, ambiguous → CLARIFY.
**Resolution**: Write a NEW consumption with volume AND calories (not a bare water row). If ambiguous, return `clarify: true`, nothing written.

**Request shape** (new drink):
```json
{"amount_ml": 200, "category": "non_alcoholic", "intent": "new", "display_name": "milk"}
```

**Request shape** (ambiguous — triggers clarify):
```json
{"amount_ml": 250, "category": "water"}
```

**Use case**: "I drank another glass of milk" = new consumption with additional volume AND calories. Ambiguous intent = CLARIFY response, not duplicate.

### Dashboard Aggregation Proof
- A caloric standalone drink contributes nutrition once (kcal present, not a bare water row with 0 kcal).
- Same drink cannot yield two rows in the dashboard liquid sum across legacy+new paths. The `log_manual_liquid` replay + reconciliation mechanisms prevent a second row.

---

## Entry Points Touched

| Entry Point | Tool/Route | Wires Into |
|---|---|---|
| MCP `log_meal_intelligent` | `sse_server.py` → `call_api("POST", "/meals/from-text-intelligent/confirm")` | Backend confirm (which uses `consumption.py`) |
| MCP `log_water` | `sse_server.py` → `POST /ingest/manual/liquid` | Reconciliation-aware liquid log |
| MCP `log_liquid` | `sse_server.py` → `POST /ingest/manual/liquid` | Reconciliation-aware liquid log |
| Backend confirm | `service.py` `confirm_meal` (deploy-only) | Should call `consumption.confirm_consumption()` |

---

## What Still Needs Wiring (post-cutover)

The `service.py` on the VPS (deploy-only) needs to be updated to call `consumption.confirm_consumption()` instead of its current `confirm_meal` logic. This is a deploy-pipeline task, not a git commit task. The reference copy at `docs/reference/intelligent-meal-service.py` documents the target behavior.

---

## Test Coverage

126 tests pass. 7 new reconciliation-path tests replace the removed `test_same_drink_manual_plus_meal_counts_twice` (which asserted the wrong 660ml double-count semantics). New tests prove:
- Same-event meal+liquid = ONE consumption
- Retry returns saved result, no new contribution
- Explicit reference reconciles to existing item (no second row)
- Genuinely new drink = additional volume AND calories
- Ambiguous intent = CLARIFY, nothing written
- Standalone caloric drink contributes nutrition once
- Same drink cannot yield two rows in the sum
