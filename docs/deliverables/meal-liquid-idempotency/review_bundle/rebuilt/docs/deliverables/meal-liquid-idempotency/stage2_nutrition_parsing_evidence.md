# Stage 2 Evidence — Nutrition Resolution (B1) + Parser (B8)

**Branch**: `feature/meal-liquid-idempotency`
**Date**: 2026-09-09
**Commit**: {commit_hash}

---

## B1 — Nutrient Resolution in the Real Draft→Confirm Path

### Affected Path(s)
- `apps/api/src/drhiro_api/services/consumption.py` — `resolve_item_nutrition()`, `_external_nutrition_search()`, `_usda_search()`, `_ddg_nutrition_search()`
- `apps/api/src/drhiro_api/services/food_search.py` — fixed `literal(0)` ORDER BY bug
- `apps/api/src/drhiro_api/models.py` — added `nutrient_basis`, `resolution_source`, `food_catalog_item_id`, `nutrition_complete` to `ConsumptionItem`
- `apps/api/src/drhiro_api/services/consumption.py` — `ParsedItem` dataclass extended with provenance fields

### Reproducer (Failing-Before)
The draft handler called `parse_consumption_text` then stored EMPTY nutrients to Redis. Confirm persisted zeros. No food search/candidate resolution/scaling occurred. A lookup failure became a successful '0 kcal' meal.

```python
# BEFORE: items had empty nutrients_per_100 and nutrients_scaled
items = parse_consumption_text("1 cup coffee")
# items[0].nutrients_per_100 == {}  ← empty!
# items[0].nutrients_scaled == {}  ← empty!
```

### Fix
1. **`resolve_item_nutrition(db, item)`** — the REAL resolution path:
   - Searches local DB via `resolve_food` (tiered ranking from `food_search.py`)
   - Falls back to `_external_nutrition_search()` (USDA API → DDG) when DB has no match
   - Scales per-100 nutrients by `grams/100` or `ml/100`
   - Sets `nutrient_basis` (`per_100_g` vs `per_100_ml`) based on item type
   - Sets `nutrition_complete=False` when no resolution found (UNKNOWN ≠ KNOWN-ZERO)

2. **Provenance persistence** — `ConsumptionItem` now stores:
   - `nutrient_basis`: `per_100_g` | `per_100_ml`
   - `resolution_source`: `db` | `external` | `unmatched`
   - `food_catalog_item_id`: link to the resolved food
   - `nutrition_complete`: `False` when lookup failed

3. **Mockable boundary** — `_external_nutrition_search()` is the single boundary for external lookups. Tests mock this function (not pre-enrich items).

4. **Bug fix** — `food_search.py`: `literal(0)` in ORDER BY caused PostgreSQL `InvalidColumnReference` error. Fixed by always returning a `case()` expression.

### Verification Evidence

#### 5 Original Failing Examples (all now pass)
| Example | Items | Volume | kcal |
|---------|-------|--------|------|
| `1 cup coffee` | 1 | 240 ml | 4.8 |
| `a cup of coffee` | 1 | 240 ml | 4.8 |
| `1 glass wine` | 1 | 250 ml | 212.5 |
| `0,5 l beer` | 1 | 500 ml | 215.0 |
| `200 g steak and 250 ml water` | 2 | — | 542.0 |

#### All-6-Nutrient Totals (through real handler + DB persistence)
```
200g steak + 250ml water:
  kcal: 542.0, protein_g: 52.0, carbs_g: 0.0, fat_g: 36.0, fiber_g: 0.0, sodium_mg: 110.0
```

#### Mass vs Volume
- Beverages use `per_100_ml` basis (e.g. `250 ml milk`)
- Foods use `per_100_g` basis (e.g. `200 g steak`)
- `250 g milk` preserves beverage identity (mass doesn't erase it)

#### Unknown ≠ Known-Zero
```
"dragonfruit with unicorn sauce" → nutrition_complete=False
```

#### External Provider Mocked at Boundary
```python
with patch("drhiro_api.services.consumption._external_nutrition_search",
           return_value=[{...}]):
    resolve_item_nutrition(db, item)
# item.nutrients_per_100["kcal"] == 60  ← from mock
# item.resolution_source == "mock_external"
```

---

## B8 — Parser Conversions/Classification

### Affected Path(s)
- `apps/api/src/drhiro_api/services/consumption.py` — `parse_consumption_text()`, `_parse_single_item()`, `_build_item()`, `_strip_nl_prefix()`

### Reproduducer (Failing-Before)
| Input | Expected | Actual (Before) |
|-------|----------|-----------------|
| `2 dl beer` | 200 ml | 2 ml (unit not recognized) |
| `2 dcl wine` | 200 ml | 2 ml |
| `2 mugs coffee` | 600 ml | not parsed (plural not recognized) |
| `2 espressos coffee` | 60 ml | not parsed |
| `1 cup rice` | FOOD | BEVERAGE (container implied drink) |
| `250ml milk,330ml beer` | 2 items | 1 item (comma not split) |
| `I drank 250 ml milk` | 250 ml | not parsed (prefix not stripped) |

### Fix
1. **Unit conversions** — added `dl`, `dcl`, `cl` and plurals (`mugs`, `espressos`) to `_VOLUME_UNITS`
2. **Regex** — `_NUM_UNIT_RE` now matches `cl`, `dl`, `dcl`, `deciliter`, etc.
3. **Container classification** — volume unit + non-beverage food name → FOOD (not beverage). Only beverage food names get `is_beverage=True`.
4. **Bare word beverage** — `coffee`, `one coffee` preserve beverage identity via `_classify_beverage()`
5. **Mass preserves beverage** — `250 g milk` keeps `is_beverage=True`
6. **Comma split** — `_ITEM_SPLIT_RE` now splits on comma when NOT preceded by a digit (allows `milk,330ml` to split)
7. **NL prefix stripping** — `_strip_nl_prefix()` removes `I drank`, `Yesterday I drank`, etc.

### Verification Evidence
| Input | Result |
|-------|--------|
| `2 dl beer` | 1 item, 200 ml, beverage |
| `2 dcl wine` | 1 item, 200 ml, beverage |
| `2 mugs coffee` | 1 item, 600 ml, beverage |
| `2 espressos coffee` | 1 item, 60 ml, beverage |
| `1 cup rice` | 1 item, FOOD (not beverage) |
| `a cup of rice` | 1 item, FOOD (agrees with above) |
| `coffee` | 1 item, beverage |
| `one coffee` | 1 item, beverage |
| `250 g milk` | 1 item, beverage, 250g |
| `250ml milk,330ml beer` | 2 items (250ml + 330ml) |
| `I drank 250 ml milk` | 1 item, 250 ml |
| `Yesterday I drank 250 ml milk` | 1 item, 250 ml |
| `0,5 l beer` | 1 item, 500 ml (preserved) |
| `tea` inside `steak` | NOT classified (preserved) |

---

## Test Results

### Stage 2 Regression Tests (NEW)
```
tests/test_stage2_b1_b8_regression.py — 30 passed
```

### Full Suite (Stage 1 + Stage 2)
```
184 passed in 25.64s
```

Breakdown:
- 154 original tests (identity, concurrency, durable replay, migration, aggregation)
- 30 new Stage-2 tests (B1 nutrient resolution + B8 parser)

### No Regressions
All Stage-1 tests for identity/concurrency/durable replay (B2/B3/B5) and migration (B6) continue to pass.

---

## Remaining Limitations

1. **External provider not tested live** — `_usda_search()` and `_ddg_nutrition_search()` are mocked at boundary. Live USDA/DDG calls require network + credentials.
2. **Density conversion** — mass↔volume conversion uses 1g≈1ml for beverages. Documented density table not yet implemented for non-water-like beverages (e.g. oil, honey).
3. **Ambiguous matches** — low-confidence candidates below threshold are marked `unmatched` rather than requesting clarification. Full clarification flow requires UI integration.
4. **NL prefix** — handles common patterns but not all possible phrasings.

---

## Open Stages

- **B4** — Aggregation correctness (liquid volume vs calorie totals across legacy+new paths)
- **B7** — Historical data migration (backfill `consumption_items` for existing meals)
- **B9** — Deployment cutover (wire `service.py` to call `consumption.confirm_consumption`)
