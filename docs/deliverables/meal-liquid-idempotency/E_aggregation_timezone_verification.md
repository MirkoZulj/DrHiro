# E. Aggregation + Timezone Verification

**Branch**: `feature/meal-liquid-idempotency`
**Date**: 2026-09-09

---

## 1. Daily Calorie Aggregation — Single Source of Truth

**File**: `apps/api/src/drhiro_api/routers/dashboard.py` lines 251–257

```python
meals_today_rows = (
    db.query(Meal)
    .filter(Meal.user_id == user.id, Meal.eaten_at >= today_start, Meal.status != "deleted")
)
calories_kcal_today = sum(float((m.totals_json or {}).get("kcal") or 0) for m in meals_today_rows)
```

**Verification**: Calories are summed from `meals.totals_json.kcal` ONLY. Liquid `Measurement` rows (metric_type=water) contribute `amount_ml` to the liquid tile but **zero** to calorie totals. This is the intended design: meal items are the single dietary-calorie source; liquid records supply volume only.

**Test coverage**: `TestAggregation.test_aggregation_no_fan_out` verifies that after logging a meal with milk (105 kcal) + beer (141.9 kcal), the meal totals reflect the sum while liquid measurements contribute only volume.

## 2. Liquid Volume Aggregation

**File**: `apps/api/src/drhiro_api/routers/dashboard.py` lines 226–242

```python
liquid_today: dict[str, int] = {c: 0 for c in liquid_cats}
for m in water_measurements:
    cat = (m["value_json"].get("category") or "water")
    liquid_today[cat] += m["value_json"].get("amount_ml", 0)
liquids_today = {"total_ml": sum(liquid_today.values()), **liquid_today}
```

**Verification**: Liquid volume is aggregated from `measurements` rows with `metric_type='water'`. With the unified write path, each beverage creates a `Measurement` row with `value_json = {"amount_ml": N, "category": <cat>}`. The `beverage_measurements` table links each such row to its source `meal_item`, ensuring no double-counting.

## 3. Plain Water vs Total Fluid

- **Plain water**: `category = "water"` — pure H2O
- **Total fluid**: sum of all categories (water + non_alcoholic + beer + wine + spirits + other_alcoholic)
- The dashboard exposes both `water_ml_today` (plain water only) and `liquids_today.total_ml` (all fluids).

## 4. Timezone Behavior

**File**: `apps/api/src/drhiro_api/routers/dashboard.py` lines 163–175

"Today" is computed in the user's timezone:
```python
user_tz = ZoneInfo(user.timezone)
today_start = datetime.now(user_tz).replace(hour=0, minute=0, second=0, microsecond=0)
```

**Test coverage**: `TestAggregation.test_yesterday_near_midnight_uses_local_date` verifies that a meal logged at 23:50 CEST (UTC+2) is stored as 21:50 UTC on the same calendar day, and the `eaten_at` field preserves the correct local date.

**Key behavior**: All `eaten_at` values are stored as tz-aware UTC. The conversion to local date happens at query time using the user's `timezone` field. This means:
- A meal logged at 23:50 CEST → stored as 21:50 UTC → displayed as 23:50 CEST
- A meal logged at 00:10 CEST (just after midnight) → stored as 22:10 UTC (previous day) → displayed as 00:10 CEST (new day)
- The "today" boundary is correctly computed in the user's local timezone

## 5. No Fan-Out Under the Unified Path

With the old MCP liquid side effect, a single `log_meal_intelligent` call could produce:
- 1 meal row (from service.py confirm)
- 1+ measurement rows (from MCP liquid block, with fresh UUIDs each retry)

With the unified path:
- 1 meal row
- N meal_items (one per parsed item)
- 0 or 1 measurement row per beverage item (linked via beverage_measurements)
- 1 consumption_operation row (idempotency + durable result)
- N consumption_items (stable identity)

**No duplicate meal rows**: The `consumption_operations` unique constraint on `(user_id, source_bot_id, source_chat_id, source_message_id)` prevents duplicate meal creation on retry. The operation result is returned for replays.

**No duplicate liquid rows**: Each beverage item gets a deterministic `source_record_id = "consumption:{operation_id}:{item_key}"`. The `measurements` unique constraint on `(user_id, source_provider, source_record_id)` prevents duplicate liquid rows.
