# H. Concurrency Prevention Statement

**Branch**: `feature/meal-liquid-idempotency`
**Date**: 2026-09-09

---

## Overview

The unified consumption domain (`consumption.py`) prevents duplicate drink RECORDS and duplicate drink CALORIES through a combination of application-level idempotency keys, DB-level unique constraints, atomic transactions, and durable result replay.

---

## (a) Concurrent Identical Requests

**Scenario**: Two identical requests arrive simultaneously (e.g., Telegram delivers the same message twice, or the user double-taps).

**Prevention mechanism**:

1. **Idempotency key lookup** (`get_or_create_operation`):
   - For Telegram: natural key is `(user_id, source_bot_id, source_chat_id, source_message_id)`.
   - For non-Telegram: caller supplies `idempotency_key`.
   - The first request creates a `ConsumptionOperation` row with status `pending`.
   - The second request finds the existing row and waits for it to complete.

2. **DB-level unique constraint** on `consumption_operations`:
   ```sql
   UNIQUE (user_id, source_bot_id, source_chat_id, source_message_id)
   WHERE source = 'telegram'
   ```
   ```sql
   UNIQUE (user_id, idempotency_key)
   WHERE idempotency_key IS NOT NULL AND source != 'telegram'
   ```
   If two transactions try to insert the same key simultaneously, one succeeds and the other gets a unique violation. The failing transaction retries the lookup and finds the existing operation.

3. **Atomic write** (`write_consumption`):
   - The entire meal + beverage write happens in a single DB transaction (`db.commit()` at the end).
   - If any part fails, the whole transaction rolls back — no partial writes.

**Test coverage**: `TestIdempotency::test_concurrent_submit_one_set` — verifies that two concurrent submissions with the same idempotency key produce only one set of meal items and one measurement.

---

## (b) Retries After Lost Response

**Scenario**: The meal confirm succeeds, but the response is lost (network error, MCP timeout). The caller retries with the same Telegram message ID.

**Prevention mechanism**:

1. **Durable result storage**:
   - After a successful write, the operation's `result_json` is stored in the `consumption_operations` table.
   - The result includes the full confirm response (meal_id, items, totals).

2. **Replay on retry** (`write_consumption`):
   ```python
   if op.status == "completed" and op.result_json:
       return op.result_json
   ```
   When a retry arrives with the same operation ID, the function returns the stored result immediately — no new meal is created, no new measurement is written.

3. **Idempotency via Telegram key**:
   - The retry uses the same `(bot_id, chat_id, message_id)` → finds the existing operation → returns the stored result.

**Test coverage**: `TestIdempotency::test_response_lost_retry_returns_saved` — verifies that a retry after a lost response returns the original meal_id and items.

---

## (c) Edits

**Scenario**: The user edits a meal item's weight or replaces a beverage.

**Prevention mechanism**:

1. **Update in place** (`update_item_quantity`):
   - Finds the target meal item by fragment match.
   - Updates `grams` and `nutrients_json` in place — no new row created.
   - If the item has a linked beverage measurement, updates `value_json.amount_ml` in the same `Measurement` row.
   - Recomputes meal totals from all items.

2. **Replace beverage** (`replace_beverage`):
   - Deletes the old beverage measurement link.
   - Creates a new `Measurement` row with the new category/volume.
   - Updates the `BeverageMeasurement` link.
   - All in one transaction.

3. **Delete beverage** (`delete_beverage`):
   - Deletes the `MealItem`, `Measurement`, and `BeverageMeasurement` rows.
   - Recomputes meal totals.

4. **Delete meal** (`delete_meal`):
   - Cascading delete: `Meal` → `MealItem` → `BeverageMeasurement` → `Measurement`.
   - The `consumption_items` and `consumption_operations` rows are preserved for audit.

**Test coverage**:
- `TestMutations::test_drink_volume_corrected_once_each` — verifies volume update doesn't create duplicates.
- `TestMutations::test_drink_deleted_removes_both` — verifies deletion removes both meal_item and measurement.
- `TestMutations::test_beverage_to_solid_removes_liquid` — verifies beverage→solid swap removes the liquid record.
- `TestB7CanonicalMutations::test_quantity_change_updates_all_projections` — verifies all projections (meal_item, measurement, totals) update atomically.
- `TestB7CanonicalMutations::test_all_six_nutrients_recomputed_on_mutation` — verifies all 6 nutrients (kcal/protein/carbs/fat/fiber/sodium) recomputed.
- `TestB7CanonicalMutations::test_timestamp_mutation_updates_all_projections` — verifies Meal.eaten_at + Measurement.start_at/end_at updated.
- `TestB7CanonicalMutations::test_meal_group_mutation_updates_all_projections` — verifies Meal.meal_type + ConsumptionItem.meal_type updated.

---

## (d) Deletion

**Scenario**: The user deletes a meal or a specific beverage.

**Prevention mechanism**:

1. **Cascading delete** (`delete_meal`):
   - Deletes the `Meal` row.
   - All `MealItem` rows with `meal_id` are deleted (SQLAlchemy cascade).
   - All `BeverageMeasurement` rows linked to those meal_items are deleted.
   - All `Measurement` rows linked to those beverage_measurements are deleted.
   - The operation row is preserved (for audit).

2. **Beverage-only delete** (`delete_beverage`):
   - Deletes the `MealItem` row.
   - Deletes the `BeverageMeasurement` link.
   - Deletes the `Measurement` row.
   - Recomputes meal totals.

3. **No orphan records**:
   - The `beverage_measurements` table has `UniqueConstraint("meal_item_id")` and `UniqueConstraint("measurement_id")` — ensuring 1:1 linkage.
   - When a meal_item is deleted, the beverage_measurement and measurement are always deleted with it.

**Test coverage**:
- `TestMutations::test_delete_meal_cascades_to_beverage_measurements` — verifies cascade delete.
- `TestMutations::test_drink_deleted_removes_both` — verifies beverage-only delete.

## (e) Generic Datapoint CRUD (B4/B7)

**Scenario**: A beverage measurement is updated or deleted via the generic `/data-points` CRUD endpoints.

**Prevention mechanism**:

1. **update_measurement_value()** — detects if the measurement is linked to a beverage (via `BeverageMeasurement`). If yes, it updates:
   - `Measurement.value_json`
   - `MealItem.volume_ml`, `MealItem.nutrients_json` (rescaled by volume ratio)
   - `Meal.totals_json` (via `_recompute_meal_totals`)
   All in one transaction. If the measurement is not a beverage, only `Measurement.value_json` is updated.

2. **delete_measurement()** — detects if the measurement is linked to a beverage. If yes, it cascades:
   - Deletes `BeverageMeasurement` link
   - Deletes `MealItem`
   - Deletes `Measurement`
   - Recomputes `Meal.totals_json`
   If not a beverage, just deletes the `Measurement`.

3. **Orphan handling** — if a `BeverageMeasurement` row exists but the `MealItem` is missing, the orphan is cleaned up and the measurement is updated/deleted normally.

**Test coverage**:
- `TestB4EntryPointCoverage::test_generic_datapoint_update_delegates_for_beverage` — verifies all projections update.
- `TestB4EntryPointCoverage::test_generic_datapoint_delete_delegates_for_beverage` — verifies cascade.


## B7 — Full Closeout (delete_meal cascade + beverage→solid rename)

### Blocker B7-1: delete_meal did not cascade to liquid projections
**Status**: **FIXED** — `routers/meals.py:delete_meal` now eagerly loads meal items, queries linked BeverageMeasurement rows, deletes their Measurement rows, deletes the BeverageMeasurement rows, then hard-deletes the meal. No orphaned liquid remains.

**Test coverage**: `TestDeleteMealCascade::test_delete_meal_cascades_to_liquid_projections` — verifies BeverageMeasurement, Measurement, and MealItem all gone after delete.

### Blocker B7-2: beverage→solid rename kept obsolete liquid projection
**Status**: **FIXED** — `propagate_beverage_patch` uses `_classify_beverage(new_name)` as the source of truth. When the new name is not a beverage, it clears `beverage_category`, sets `volume_ml=None`, and removes the BeverageMeasurement + Measurement. Meal totals are recomputed from the solid food's nutrients.

**Test coverage**:
- `TestBeverageToSolidRename::test_beverage_to_solid_rename_clears_category_and_drops_liquid` — full rename (food_catalog_item_id path): asserts category=None, volume_ml=None, BeverageMeasurement=0, Measurement=0, meal totals match steak (677.5 kcal, 65g protein, 0g carbs, 45g fat, 0g fiber, 137.5mg sodium).
- `TestBeverageToSolidRename::test_beverage_to_solid_rename_by_name_only` — name-only rename (display_name path): same assertions.
- `TestBeverageToSolidRename::test_beverage_to_beverage_rename_keeps_liquid` — milk→juice: BeverageMeasurement+Measurement preserved, category updated, meal totals match juice (112.5 kcal, 1.75g protein, 25g carbs, 0.5g fat, 0.5g fiber, 2.5mg sodium).
- `TestWiredBeveragePatch::test_patch_beverage_to_solid_drops_liquid` — wired-path integration test.

### No-resurrect guarantee
After a meal with beverages is deleted, retrying the original creation does NOT resurrect the deleted meal's consumption. The new meal is a SEPARATE entity.

**Test coverage**: `TestDeleteMealCascade::test_delete_meal_retry_does_not_resurrect` — creates a meal, deletes it, recreates with same content, asserts only 1 measurement (the new one).

### Ownership isolation
- `TestDeleteMealCascade::test_delete_meal_ownership_404` — User B cannot delete User A's meal (404, no mutation).
- `TestBeverageToSolidRename::test_beverage_to_solid_ownership_404` — User B cannot rename User A's meal item (404, no mutation).

### Verdict: B7 FULLY CLOSED
Both B7 blocker conditions are now closed. Stage 3 = B4 + B7 complete.

---

## B7 — Canonical Atomic Mutations

**Stage 3 adds four mutation functions that update ALL projections atomically from one canonical record**:

1. **update_meal_timestamp(db, user_id, meal_id, new_eaten_at)**:
   - Updates `Meal.eaten_at`
   - Updates all linked `Measurement.start_at` / `end_at` (via `BeverageMeasurement` join)
   - Updates all linked `ConsumptionItem.updated_at`

2. **update_meal_group(db, user_id, meal_id, new_meal_type)**:
   - Updates `Meal.meal_type` (normalized via `_normalize_meal_type`)
   - Updates all linked `ConsumptionItem.meal_type`

3. **update_measurement_value(db, user_id, measurement_id, new_value_json)**:
   - For beverages: detects `BeverageMeasurement` link, rescales `MealItem.nutrients_json` by volume ratio, updates `MealItem.volume_ml`, updates `Meal.totals_json`
   - For non-beverages: only updates `Measurement.value_json`

4. **delete_measurement(db, user_id, measurement_id)**:
   - For beverages: cascades delete to `BeverageMeasurement`, `MealItem`, and recomputes `Meal.totals_json`
   - For non-beverages: just deletes `Measurement`

**Test coverage**:
- `TestB7CanonicalMutations::test_timestamp_mutation_updates_all_projections`
- `TestB7CanonicalMutations::test_meal_group_mutation_updates_all_projections`
- `TestB7CanonicalMutations::test_quantity_change_updates_all_projections`
- `TestB7CanonicalMutations::test_all_six_nutrients_recomputed_on_mutation`
- `TestB7CanonicalMutations::test_ownership_check_on_mutation`
- `TestB7CanonicalMutations::test_repeated_mutation_is_idempotent`
- `TestB7CanonicalMutations::test_transaction_rollback_on_error`
- `TestB7CanonicalMutations::test_replay_after_mutation_does_not_resurrect`

---

## DB Constraints Summary

| Constraint | Table | Columns | Purpose |
|---|---|---|---|
| `uq_consumption_op_telegram` | `consumption_operations` | `(user_id, source_bot_id, source_chat_id, source_message_id)` | One operation per Telegram message |
| `uq_consumption_op_idempotency` | `consumption_operations` | `(user_id, idempotency_key)` | One operation per caller key |
| `uq_consumption_item_op_key` | `consumption_items` | `(operation_id, item_key)` | Stable item identity |
| `uq_bev_meal_item` | `beverage_measurements` | `(meal_item_id)` | 1:1 meal_item ↔ beverage |
| `uq_bev_measurement` | `beverage_measurements` | `(measurement_id)` | 1:1 measurement ↔ beverage |

---

## Writer Coordination (B9 — Stage 4)

**Scenario**: During cutover, could BOTH the old MCP liquid side effect AND the new unified backend writer record the same drink?

**Answer**: No — controlled by `DRHIRO_LIQUID_WRITER`:

- `legacy` (default): old MCP side effect ACTIVE, new backend may also write (P1 dual-write window — minimized to minutes).
- `unified`: old MCP side effect SKIPPED — `if not is_unified_writer()` gate in `sse_server.py` runs the legacy block; in `unified` mode it is bypassed and a log line is emitted instead.
- Any other value → `ValueError` at module load (fail-closed → container crash → no writes).

The flag is read per-request, so the flip is atomic at container restart. There is no interval where neither writer records a drink (P1 has both; P2 has unified only).

**Test coverage**: `tests/test_stage4_writer_flag.py` (5 tests) — proves mode control + fail-closed on invalid values.

## Calorie Double-Count Prevention

**Scenario**: Could a beverage's calories be counted twice — once as a meal_item and once as a liquid measurement?

**Answer**: No. The dashboard aggregation (`dashboard.py` lines 251–257) sums `totals_json.kcal` from **meals only**. Liquid `Measurement` rows contribute `amount_ml` to the liquid tile but **zero** to calorie totals. The `consumption.py` unified write path ensures that beverage nutrition is included in the meal's `totals_json` (via the meal_item's `nutrients_json`), and the `Measurement` row only stores `amount_ml` + `category` — no calorie data.

**Test coverage**: `TestAggregation::test_aggregation_no_fan_out` — verifies that after logging a meal with milk + beer, the meal totals reflect the sum while liquid measurements contribute only volume.

---

## Manual-Liquid Reconciliation (Three-Intent Model)

**Scenario**: The same drink is referenced by a meal tool call and a manual-liquid log in the same Telegram event. How is double-counting prevented?

**Answer**: Through three distinct code paths in `consumption.log_manual_liquid`:

1. **Same-Event Idempotent Replay**: The manual-liquid log carries the same source identity (`chat_id` + `message_id` + `bot_id`) as the meal confirm. `get_or_create_operation` finds the already-completed operation and returns its saved result — no new row, no double count.

2. **Explicit-Reference Reconciliation**: The agent model sends `existing_item_id` (a reference to an existing measurement or consumption item). `_reconcile_liquid` updates the existing measurement's `amount_ml` in place — no second row.

3. **Genuinely New Drink**: The agent model sends `intent: "new"` with a new source identity. A new consumption is written with volume AND calories. Without `intent: "new"`, ambiguous intent returns a CLARIFY response — nothing is written.

**Test coverage** (in `tests/test_legacy_new_water_coexistence.py`):
- `test_same_event_meal_plus_liquid_one_consumption` — same source identity → one consumption, one measurement (330ml, not 660ml)
- `test_retry_returns_saved_result` — retry returns existing meal_id, no new contribution
- `test_reconcile_existing_item_links_once` — explicit reference updates existing item (250+100=350ml total, not 500)
- `test_genuinely_new_drink_additional_volume_and_calories` — new message + intent=new → two distinct drinks (250+200=450ml)
- `test_ambiguous_intent_clarifies` — no identity, no reference, no intent → CLARIFY response, nothing written
- `test_standalone_caloric_drink_contributes_nutrition` — caloric drink via manual path has real kcal in meal totals
- `test_same_drink_cannot_yield_two_rows_in_sum` — logging same (operation, item) twice via both paths → one measurement row

---

## Cross-User Isolation

**Scenario**: Could user A's operation affect user B's data?

**Answer**: No. Every query in `consumption.py` filters by `user_id`:
- `get_or_create_operation`: filters by `user_id` in both Telegram and idempotency key lookups.
- `write_consumption`: the `Meal` and `MealItem` rows are created with `user_id`.
- `update_item_quantity`, `replace_beverage`, `delete_beverage`, `delete_meal`: all filter by `user_id` in the initial lookup.

**Test coverage**: `TestMutations::test_cross_user_no_unauthorized_access` — verifies that user B cannot modify or delete user A's meals.
