# Stage 3 — Entry-Point Coverage Matrix

**Branch**: `feature/meal-liquid-idempotency`
**Date**: 2026-09-09
**Commit**: cbc01e0 (Stage 2)

---

## Matrix Legend

- **✓ wired** = routes through shared-domain function
- **✗ bypass** = writes directly without going through shared domain
- **N/A** = not a consumption path (e.g., recipe storage, food catalog)
- **PARTIAL** = some shared rules applied but not all projections updated atomically

---

## 1. Creation Paths

| # | Entry Point | Route | File | Wires Into | Status |
|---|---|---|---|---|---|
| 1 | Manual meal create | `POST /meals` | routers/meals.py `create_meal` | ✗ direct MealItem + _lookup_nutrients | BYPASS — food_only, no beverage_link; meals without beverages unaffected but no ConsumptionOperation/ConservationItem identity |
| 2 | NL meal create | `POST /meals/from-text` | routers/meals.py `create_meal_from_text` | ✗ calls create_meal | BYPASS (same as #1) |
| 3 | Intelligent draft | `POST /meals/from-text-intelligent` | services/intelligent_meal_service_patch.py | N/A — preview/un-deployed | DEPLOY-ONLY (not in repo runtime) |
| 4 | Intelligent confirm | `POST /meals/from-text-intelligent/confirm` | services/intelligent_meal_service_patch.py `confirm_meal` | ✓ `confirm_consumption` | WIRED (preview) |
| 5 | Photo draft | `POST /meals/from-photo` | routers/meals.py `create_meal_from_photo` | N/A — needs_review draft only | No consumption yet (async vision fill) |
| 6 | Barcode meal | `POST /meals/from-barcode` | routers/meals.py `create_meal_from_barcode` | ✗ direct Meal + MealItem, no totals recompute, no operation | BYPASS — no beverage support but also no ConsumptionOperation |
| 7 | Recipe storage | `POST /meals/recipes` | routers/meals.py `create_recipe` | N/A — creates FoodCatalogItem, not a Meal | Not a consumption path |
| 8 | Recipe consume | (missing) | — | ✗ not implemented | GAP: consuming a recipe should call write_consumption |
| 9 | Add item to meal | `POST /meals/{meal_id}/items` | routers/meals.py `add_meal_item` | ✗ direct MealItem + _sync_totals, no ConsumptionItem / BeverageMeasurement | BYPASS — beverages added this way have no liquid projection |
| 10 | Copy meal | `POST /meals/{meal_id}/copy` | routers/meals.py `copy_meal` | ✗ direct Meal + MealItem copies, no new operation, no BeverageMeasurement links | BYPASS — copied beverages lose liquid linkage |
| 11 | MCP meal from text | `POST /tools/create_meal_from_text` | routers/openclaw_tools.py `tool_meal_from_text` | ✗ calls meals.create_meal | BYPASS (same as #1) |

## 2. Meal Item Mutation Paths

| # | Entry Point | Route | File | Wires Into | Status |
|---|---|---|---|---|---|
| 12 | Patch meal metadata | `PATCH /meals/{meal_id}` | routers/meals.py `patch_meal` | ✗ direct field update, no ConsumptionItem/totals recomputation from canonical | BYPASS — but only updates meal_type/notes/eaten_at (not items) |
| 13 | Patch meal item | `PATCH /meals/{meal_id}/items/{item_id}` | routers/meals.py `patch_meal_item` | ✗ _apply_explicit_food / _resolve_item_nutrition + _sync_totals, no ConsumptionItem / Measurement update | BYPASS — beverage volume/name/category changes don't propagate to Measurement |
| 14 | Remove meal item | `DELETE /meals/{meal_id}/items/{item_id}` | routers/meals.py `remove_meal_item` | ✗ db.delete(item) + _sync_totals, no BeverageMeasurement/Measurement cleanup | BYPASS — beverage deletion orphans liquid measurement |
| 15 | Confirm meal | `POST /meals/{meal_id}/confirm` | routers/meals.py `confirm_meal` | ✗ status flip only | N/A — status transition, no nutrition |
| 16 | Delete meal | `DELETE /meals/{meal_id}` | routers/meals.py `delete_meal` | ✗ `meal.status = "deleted"`, no BeverageMeasurement/Measurement cleanup, no ConsumptionOperation | BYPASS — beverage measurements orphaned |

## 3. Manual Liquid / Water Paths

| # | Entry Point | Route | File | Wires Into | Status |
|---|---|---|---|---|---|
| 17 | Manual water | `POST /ingest/manual/water` | routers/ingest.py `manual_water` | ✗ `_manual_measurement` direct write | BYPASS — bare water row, no ConsumptionOperation |
| 18 | Manual liquid | `POST /ingest/manual/liquid` | routers/ingest.py `manual_liquid` | ✓ `log_manual_liquid` | WIRED |
| 19 | Manual text (water) | `POST /ingest/manual/text` | routers/ingest.py `manual_text` | ✗ `_manual_measurement` for water metric | BYPASS — bare water row |
| 20 | MCP log_water | MCP tool | sse_server.py | ✓ → `/ingest/manual/liquid` | WIRED (via #18) |
| 21 | MCP log_liquid | MCP tool | sse_server.py | ✓ → `/ingest/manual/liquid` | WIRED (via #18) |

## 4. Generic Measurement (datapoint) Paths

| # | Entry Point | Route | File | Wires Into | Status |
|---|---|---|---|---|---|
| 22 | Log any measurement | `POST /data-points` | routers/datapoints.py `log_data_point` | ✗ direct Measurement write | BYPASS — but creates a new row, not a mutation of existing |
| 23 | Update measurement | `PATCH /data-points/{mid}` | routers/datapoints.py `update_data_point` | ✗ direct field write | BYPASS — updating a beverage measurement orphans the MealItem/BeverageMeasurement projections |
| 24 | Delete measurement | `DELETE /data-points/{mid}` | routers/datapoints.py `delete_data_point` | ✗ `db.delete(m)` | BYPASS — deleting a beverage measurement orphans the MealItem + BeverageMeasurement link + leaves meal totals stale |
| 25 | Health Connect batch | `POST /ingest/health-connect/batch` | routers/ingest.py | ✗ direct Measurement writes | SEPARATE — these are source Provider readings, not consumption. Intentionally separate path. |

---

## 5. Shared-Domain Function Inventory

| Function | Line | Purpose | Currently Called By |
|---|---|---|---|
| `parse_consumption_text` | ~325 | Free-text → ParsedItem list | intelligent_meal_service_patch |
| `confirm_consumption` | ~1107 | Confirm entry point (operation resolution + write) | intelligent_meal_service_patch |
| `write_consumption` | ~903 | Atomic meal + beverage write | confirm_consumption, log_manual_liquid |
| `get_or_create_operation` | ~716 | Idempotency key resolution | confirm_consumption, log_manual_liquid |
| `update_item_quantity` | ~1175 | Rescale grams + nutrition + linked volume | NOT WIRED to any router |
| `replace_beverage` | ~1228 | Beverage → beverage or beverage → solid | NOT WIRED to any router |
| `delete_beverage` | ~1282 | Remove meal_item + liquid measurement | NOT WIRED to any router |
| `delete_meal` | ~1310 | Cascade delete meal + beverage measurements | NOT WIRED to any router |
| `_recompute_meal_totals` | ~1340 | Recompute meal totals from items | update_item_quantity, replace_beverage, delete_beverage |
| `log_manual_liquid` | ~1408 | Reconciliation-aware liquid log | ingest.py manual_liquid |
| `_reconcile_liquid` | ~1528 | Link to existing item | log_manual_liquid |
| `_write_new_liquid_consumption` | ~1644 | New caloric drink write | log_manual_liquid |
| `resolve_item_nutrition` | ~621 | DB → external nutrition resolution | test_stage2_b1_b8_regression |

---

## 6. Aggregation Paths (Read-Side)

| Aggregation | File | Local-Day TZ | Incomplete-Nutrition | Fan-Out Risk |
|---|---|---|---|---|
| `dashboard/today` water tile | routers/dashboard.py `_water_today` | ✓ `_local_day_start` | N/A (volume only) | **RISK**: sums all `metric_type=water` rows — does NOT exclude non-consumption sources |
| `dashboard/today` meals tile | routers/dashboard.py | ✓ `_local_day_start` | ✗ no `nutrition_complete` flag check | **RISK**: meals with `nutrition_complete=False` items summed silently as 0 |
| Daily aggregate recomputation | (not implemented) | — | — | GAP |

---

## 7. Stage 3 Fix Plan (B4 + B7)

### B4 — Wire Entry Points + Fix Aggregation

**Router wiring targets:**
- `routers/meals.py`: `delete_meal` → `consumption.delete_meal` (cascade)
- `routers/meals.py`: `patch_meal_item` → update ConsumptionItem + propagate to Measurement
- `routers/meals.py`: `remove_meal_item` → `consumption.delete_beverage` (if beverage) else recompute
- `routers/datapoints.py`: `update_data_point` → delegate to `consumption.update_measurement_value` for beverages
- `routers/datapoints.py`: `delete_data_point` → delegate to `consumption.delete_measurement` for beverages

**Aggregation fixes:**
- Dashboard water tile: only count water measurements that are linked to a consumption (via BeverageMeasurement)
- Meal totals: when summing for dashboard, treat `nutrition_complete=False` items as incomplete (don't silently sum 0)
- Fan-out: ensure meal×measurement join does not multiply contributions

### B7 — Canonical Atomic Mutations

**New mutation functions needed in consumption.py:**
- `update_meal_timestamp(db, user_id, meal_id, new_eaten_at)` — update Meal + all Measurements + ConsumptionItems
- `update_meal_group(db, user_id, meal_id, new_meal_type)` — update Meal + all ConsumptionItems
- `update_measurement_value(db, user_id, measurement_id, new_value_json)` — generic datapoint update that delegates to beverage logic when the measurement is a beverage
- `delete_measurement(db, user_id, measurement_id)` — generic datapoint delete that cascades to BeverageMeasurement

**Test coverage required:**
- Ownership checks
- Repeated mutations (idempotent)
- Concurrent edits
- Transaction rollback (mid-mutation error → no partial state)
- All 6 nutrients recomputed
- Replay-after-mutation semantics
