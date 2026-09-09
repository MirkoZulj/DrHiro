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

**Test coverage**:
- `TestIdempotency::test_concurrent_submit_one_set` — sequential dedup: same key → same op.id, first creates (created=True), second finds (created=False).
- `TestB5ConcurrentFirstCreation::test_concurrent_first_creation_two_sessions_barrier` — genuine concurrent first creation: two separate DB sessions + `threading.Barrier(2)` on PostgreSQL. Both callers attempt first creation simultaneously → exactly ONE operation, ONE meal, ONE measurement. Both callers receive the SAME meal_id. Stable across repeated runs.

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
