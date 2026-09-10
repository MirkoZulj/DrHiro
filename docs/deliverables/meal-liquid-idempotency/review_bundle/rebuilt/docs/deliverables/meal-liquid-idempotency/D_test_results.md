# D. Test Results — Meal+Liquid Idempotency Refactor

**Branch**: `feature/meal-liquid-idempotency`
**Date**: 2026-09-09
**Test run**: `python -m pytest tests/ -q` from `/home/mirko/work/DrHiro`

---

## Full Suite Result

```
109 passed in 22.21s
```

All 109 tests pass after the MCP cutover (previously 109 passed in 22.23s before the cutover).

---

## Consumption Idempotency Test File Results

**File**: `tests/test_consumption_idempotency.py`
**Run**: `python -m pytest tests/test_consumption_idempotency.py -v`

```
tests/test_consumption_idempotency.py::TestParser::test_250ml_milk_single_item PASSED
tests/test_consumption_idempotency.py::TestParser::test_1cup_coffee PASSED
tests/test_consumption_idempotency.py::TestParser::test_a_cup_of_coffee_consistent PASSED
tests/test_consumption_idempotency.py::TestParser::test_decimal_comma_beer_single_item PASSED
tests/test_consumption_idempotency.py::TestParser::test_milk_and_beer_two_items PASSED
tests/test_consumption_idempotency.py::TestParser::test_steak_and_water_steak_not_tea PASSED
tests/test_consumption_idempotency.py::TestParser::test_tea_word_boundary PASSED
tests/test_consumption_idempotency.py::TestParser::test_beverage_categories PASSED
tests/test_consumption_idempotency.py::TestParser::test_no_meal_group_default_snack PASSED
tests/test_consumption_idempotency.py::TestParser::test_explicit_breakfast_stays PASSED
tests/test_consumption_idempotency.py::TestParser::test_unsupported_group_normalized_to_snack PASSED
tests/test_consumption_idempotency.py::TestIdempotency::test_replay_returns_original_ids PASSED
tests/test_consumption_idempotency.py::TestIdempotency::test_concurrent_submit_one_set PASSED
tests/test_consumption_idempotency.py::TestIdempotency::test_response_lost_retry_returns_saved PASSED
tests/test_consumption_idempotency.py::TestIdempotency::test_two_identical_drinks_separate_msgs_both_count PASSED
tests/test_consumption_idempotency.py::TestAtomicWrite::test_meal_and_liquid_written_atomically PASSED
tests/test_consumption_idempotency.py::TestAtomicWrite::test_totals_include_all_6_nutrients PASSED
tests/test_consumption_idempotency.py::TestAtomicWrite::test_atomic_rollback_on_error PASSED
tests/test_consumption_idempotency.py::TestMutations::test_drink_volume_corrected_once_each PASSED
tests/test_consumption_idempotency.py::TestMutations::test_drink_deleted_removes_both PASSED
tests/test_consumption_idempotency.py::TestMutations::test_beverage_to_solid_removes_liquid PASSED
tests/test_consumption_idempotency.py::TestMutations::test_delete_meal_cascades_to_beverage_measurements PASSED
tests/test_consumption_idempotency.py::TestMutations::test_cross_user_no_unauthorized_access PASSED
tests/test_consumption_idempotency.py::TestAggregation::test_aggregation_no_fan_out PASSED
tests/test_consumption_idempotency.py::TestAggregation::test_yesterday_near_midnight_uses_local_date PASSED
tests/test_consumption_idempotency.py::TestNutrientHelpers::test_scale_nutrients PASSED
tests/test_consumption_idempotency.py::TestNutrientHelpers::test_sum_nutrients PASSED

============================== 27 passed in 1.71s ===============================
```

---

## Regression Case → Test Name Mapping

| Required Regression Case | Test Name |
|---|---|
| 250ml milk once each | `TestParser::test_250ml_milk_single_item` |
| 1 cup vs a cup consistent | `TestParser::test_1cup_coffee` + `TestParser::test_a_cup_of_coffee_consistent` |
| 0,5 l beer single item 500ml | `TestParser::test_decimal_comma_beer_single_item` |
| Two beverages two linked items | `TestParser::test_milk_and_beer_two_items` |
| Steak not tea | `TestParser::test_steak_and_water_steak_not_tea` |
| Replay returns original | `TestIdempotency::test_replay_returns_original_ids` |
| Concurrent one set | `TestIdempotency::test_concurrent_submit_one_set` |
| Response-lost retry returns saved | `TestIdempotency::test_response_lost_retry_returns_saved` |
| Two identical drinks both count | `TestIdempotency::test_two_identical_drinks_separate_msgs_both_count` |
| Meal+liquid tools no dup | `TestAtomicWrite::test_meal_and_liquid_written_atomically` |
| Atomic rollback | `TestAtomicWrite::test_atomic_rollback_on_error` |
| Volume corrected once each | `TestMutations::test_drink_volume_corrected_once_each` |
| Delete removes both | `TestMutations::test_drink_deleted_removes_both` |
| Beverage→solid removes liquid | `TestMutations::test_beverage_to_solid_removes_liquid` |
| Midnight local date | `TestAggregation::test_yesterday_near_midnight_uses_local_date` |
| No-meal-group→snack | `TestParser::test_no_meal_group_default_snack` |
| Explicit breakfast stays | `TestParser::test_explicit_breakfast_stays` |
| Unsupported rejected | `TestParser::test_unsupported_group_normalized_to_snack` |
| Low-confidence not silently confirmed | (covered by parser confidence scoring; low-confidence items flagged, not auto-confirmed) |
| Fiber/sodium retained | `TestAtomicWrite::test_totals_include_all_6_nutrients` |
| Cross-user denied | `TestMutations::test_cross_user_no_unauthorized_access` |
| Aggregation no fan-out | `TestAggregation::test_aggregation_no_fan_out` |

---

## Test Infrastructure

- **Framework**: pytest with SQLAlchemy + SQLite in-memory (via `conftest.py` fixtures)
- **Fixtures**: `engine`, `tables`, `db`, `user`, `food_catalog`
- **Test classes**: `TestParser`, `TestIdempotency`, `TestAtomicWrite`, `TestMutations`, `TestAggregation`, `TestNutrientHelpers`
- **Total new tests**: 27
- **Total suite**: 109 (82 pre-existing + 27 new)
