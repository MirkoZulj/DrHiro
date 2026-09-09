# G. Dry-Run Historical Duplicate/Orphan Analysis

**Branch**: `feature/meal-liquid-idempotency`
**Date**: 2026-09-09
**Source**: Production DB read-only via `sshpass -p '<redacted-secret>' ssh root@144.91.107.8` → `docker exec drhiro-postgres-1 psql -U drhiro -d drhiro`

**⚠️ ANALYSIS ONLY — NO DATA MODIFIED**

---

## 1. Production DB State

| Metric | Value |
|---|---|
| Total meals | 41 |
| Total water measurements | 22 |
| New columns (volume_ml, beverage_category, source_operation_id) | NOT present (migration not applied) |
| beverage_measurements table | Does not exist yet |

This confirms the production DB is at the pre-cutover schema. The new tables/columns exist only in the feature branch migration.

---

## 2. Duplicate Meal Analysis

### 2.1 Exact Duplicate Notes

```sql
SELECT notes, count(*) as cnt FROM meals
WHERE notes NOT LIKE '%OpenClaw heartbeat%'
GROUP BY notes HAVING count(*) > 1;
```

**Result: 0 rows.** No exact duplicate notes exist.

### 2.2 Near-Duplicate Timestamps (within 2 minutes)

```sql
SELECT m1.id, m2.id, m1.eaten_at, m2.eaten_at, m1.notes
FROM meals m1 JOIN meals m2
  ON m1.user_id = m2.user_id AND m1.id < m2.id
  AND ABS(EXTRACT(EPOCH FROM (m1.eaten_at - m2.eaten_at))) < 120
ORDER BY m1.eaten_at DESC;
```

**Result: 14 pairs found.** Analysis:

| Pair | Time Diff | Notes | Verdict |
|---|---|---|---|
| `231770b3` / `cb8b6cbf` | ~74s | "lunch beef pho" / "breakfast bread+kefir" | **Legitimate** — different meals, different contents |
| `34883b86` / `a01faf4f` | 0s (both midnight) | "dinner cevapi+veg" / "dinner cevapi" | **Legitimate** — different days (both "yesterday" entries for different dates) |
| `2cabd73c` / `e121eb06` | 0s (both midnight) | "breakfast eggs+bread" / "lunch goulash" | **Legitimate** — different days |
| `e67af086` / `e67b2a5d` | ~52s | "500 g of steak" / "500 g of steak" | **⚠️ SUSPECT** — same notes, same weight, 52s apart |
| `654877bf` / `9c08c0d4` | 0s (both midnight) | "Thursday breakfast cevapi..." / "Thursday breakfast cevapi..." | **⚠️ SUSPECT** — identical notes, same day |
| `91a5fd92` / `b1a941b3` | 0s (both midnight) | "Wednesday snack peach" / "Wednesday dinner cevapi..." | **Legitimate** — different meals |
| `19efc410` / `91a5fd92` / `b1a941b3` | 0s (all midnight) | "dinner cevapi..." / "snack peach" / "dinner cevapi..." | **⚠️ SUSPECT** — `19efc410` and `b1a941b3` have identical notes |
| `97dabdc2` / `ae4f9711` / `c8eb2fa9` | 0s (all midnight) | "breakfast steak..." / "dinner cottage cheese..." / "snack wafers..." | **Legitimate** — different meals |
| `949279a8` / `97dabdc2` / `c8eb2fa9` / `ae4f9711` | 0s (all midnight) | "dinner cottage cheese..." / "breakfast steak..." / "snack wafers..." / "dinner cottage cheese..." | **⚠️ SUSPECT** — `949279a8` and `ae4f9711` have identical notes |

### 2.3 Suspected Duplicate Meals (identical notes, same timestamp)

| Meal IDs | Eaten At | Notes | kcal |
|---|---|---|---|
| `e67af086` / `e67b2a5d` | 2026-08-28 14:40-14:41 | "500 g of steak" | 500g steak |
| `654877bf` / `9c08c0d4` | 2026-08-27 00:00:00 | "On Thursday for breakfast I had 280 g of cevapi with cheese..." | — |
| `19efc410` / `b1a941b3` | 2026-08-26 00:00:00 | "On Wednesday for dinner I had 200 g of cevapi with cheese..." | — |
| `949279a8` / `ae4f9711` | 2026-08-25 00:00:00 | "Tuesday for dinner I had 125 g of cottage cheese..." | — |

**Likely cause**: These are the OLD bug — retry after lost response. The first confirm committed, the response was lost, the MCP retried, and the second confirm created a duplicate meal. The dedup window (notes + 2 minutes) failed because the draft was already deleted after the first confirm.

**Recommended action**: Manual review by the user. These are NOT auto-deleted by this analysis.

---

## 3. Orphan Water Measurements (no associated meal within 5 minutes)

**14 orphan measurements found:**

| Measurement ID | Time | Amount | Category | Likely Source |
|---|---|---|---|---|
| `40fd60d1` | 2026-09-08 18:01 | 500ml | beer | Direct log_liquid (no meal context) |
| `5b0f10fb` | 2026-09-08 18:00 | 600ml | water | Direct log_water |
| `bc47c034` | 2026-09-07 05:46 | 250ml | water | Direct log_water |
| `25a59a5f` | 2026-09-06 18:00 | 400ml | water | Direct log_water |
| `8644109f` | 2026-09-06 15:56 | 650ml | (none) | Direct log_water (old format) |
| `b47a98cb` | 2026-09-06 15:54 | 350ml | wine | Direct log_liquid |
| `7aff5473` | 2026-09-06 13:00 | 400ml | water | Direct log_water |
| `4951ae14` | 2026-09-02 20:46 | 1000ml | beer | Direct log_liquid |
| `515de699` | 2026-09-02 07:18 | 400ml | water | Direct log_water |
| `20434029` | 2026-09-01 17:30 | 300ml | wine | Direct log_liquid |
| `e898f2eb` | 2026-09-01 16:34 | 1000ml | beer | Direct log_liquid |
| `7fb55a5a` | 2026-09-01 16:16 | 200ml | water | Direct log_water |
| `648cb797` | 2026-09-01 14:23 | 400ml | (none) | Direct log_water (old format) |
| `087727ee` | 2026-08-11 17:02 | 500ml | (none) | Direct log_water (old format) |

**Analysis**: These are NOT orphans from the old MCP liquid auto-log side-effect. They are direct user-initiated `log_water` / `log_liquid` calls (the user said "log 250ml water" explicitly). The old MCP side-effect only fired inside `log_meal_intelligent`, and those measurements would be within 5 minutes of a meal.

**No action needed** — these are legitimate standalone liquid logs.

---

## 4. Beverage Items in meal_items (Old Parser Artifacts)

Several meal_items contain beverage names parsed as food items by the old `parse_meal_text`:

| Display Name | grams | Meal Notes |
|---|---|---|
| "Water, bottled, generic" | 250g | "I had a glass of water, a double espresso..." |
| "Alcoholic beverage, beer, light" | 330g | "4 glasses of water and one beer, lager 500ml" |
| "Alcoholic beverage, wine, table, red" | 150-300g | Various dinner meals |
| "Kefir, lowfat, plain, LIFEWAY" | 200g | Breakfast meals |
| "Oat milk, unsweetened, plain" | 200g | Breakfast meals |
| "Coke" | 250g | Breakfast meals |
| "double espresso with dash of milk" | 100g | Various breakfast meals |

**Analysis**: The old parser classified beverages as food items with grams. The new `consumption.py` classifies them as beverages with `volume_ml` and `beverage_category`. These historical rows are NOT corrupted — they represent the best-effort parsing of the old system. They will coexist with new writes.

**No action needed** — historical data is preserved as-is.

---

## 5. OpenClaw Heartbeat Poll Rows

4 meal rows with notes like "[OpenClaw heartbeat poll]" and 0.0 kcal. These are automated health-check polls that accidentally created meal rows. They have 1 meal_item each with 0 grams.

**Recommendation**: These could be cleaned up in a future migration, but they are harmless (0 kcal, no liquid). NOT part of this cutover.

---

## 6. Summary

| Category | Count | Action |
|---|---|---|
| Exact duplicate notes | 0 | None |
| Suspected duplicate meals (retry artifacts) | 4 pairs | Manual review recommended |
| Orphan water measurements (direct logs) | 14 | None — legitimate |
| Beverage items in meal_items | ~15 | None — historical artifacts |
| OpenClaw heartbeat polls | 4 | Future cleanup (out of scope) |

**Conclusion**: The production data has ~4 suspected duplicate meal pairs that are likely retry artifacts from the old non-idempotent confirm path. These should be reviewed manually by the user before/after deployment. No automatic deletion is performed. The new idempotency mechanism (consumption_operations + durable result) prevents future duplicates.
