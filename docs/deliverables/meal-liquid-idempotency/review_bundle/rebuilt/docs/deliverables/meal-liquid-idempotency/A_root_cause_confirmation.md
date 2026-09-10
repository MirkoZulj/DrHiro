# A. Root-Cause Confirmation — Meal+Liquid Logging Reliability

**Branch**: `feature/meal-liquid-idempotency`
**Date**: 2026-09-09
**Source of truth**: Pi repo `/home/mirko/work/DrHiro`, VPS reference `/opt/apps/intelligent-meal/service.py` (pulled read-only to `docs/reference/intelligent-meal-service.py`).

---

## 1. Separate, unlinked writes (the core bug)

**File**: `packages/drhiro-mcp/src/drhiro_mcp/sse_server.py` lines 1636–1698

After `log_meal_intelligent` confirms a meal via the intelligent-meal service, the MCP server runs a **separate** liquid auto-log block that:
- Mints its own JWT for a **hard-coded user** `0bfad360-9938-4216-8abd-b44d69e2003f` (line 1646)
- POSTs to a **hard-coded URL** `http://172.20.0.1:8010/api/v1/ingest/manual/water` (line 1651, 1684)
- Scans the **whole original sentence** with substring regex for a drink category + first-number volume

**Consequence**: The meal write (in the deploy-only `service.py` confirm path) and the liquid write are **separate, non-atomic, non-idempotent** writes. A failed liquid write leaves incomplete accounting; a retry of the whole `log_meal_intelligent` tool call produces a **duplicate meal** (the dedup window is notes+2min, but the MCP retry happens after the first confirm already committed and deleted the draft).

## 2. Duplicate prevention is insufficient

**File**: `docs/reference/intelligent-meal-service.py` lines 1132–1153

`confirm_meal` dedupes only by `notes = :notes AND created_at > NOW() - INTERVAL '2 minutes'`. After a successful confirm:
- The Redis draft is **deleted** (line 1297)
- A lost-success retry finds no draft → **404** (line 1122), no durable result returned
- No DB-level uniqueness on (user_id, source, source_record_id) for the intelligent-meal path
- Each liquid write mints a **fresh** `uuid.uuid4()` → no dedup possible

## 3. Two parsers disagree

**File**: `docs/reference/intelligent-meal-service.py` `parse_meal_text` (lines 304–429) vs `sse_server.py` liquid block (lines 1652–1697).

| Input | `parse_meal_text` (service.py) | Liquid block (sse_server.py) |
|---|---|---|
| `0,5 l beer` | Splits on `,` → two items: `0` and `5 l beer` | Regex `(\d+(?:[.,]\d+)?)` matches `0` → wrong |
| `200g steak and 250ml water` | Two items correctly | `re.search(r"tea\|...", "steak")` → **no**, but `water` matches → category ok; amount = first number `200` → **wrong item** |
| `1 cup coffee` | Container regex → 240g coffee | `cup` → 250ml (different conversion) |
| `a cup of coffee` | Container regex → 240g | `cup` → 250ml |
| `tea` inside `steak` | Token-aware, no false hit | `re.search(r"\btea\b", "steak")` → **no** (word boundary saves it here), but substring `tea` in `steak` without boundary would hit |

The MCP liquid block uses **whole-message first-category-first-number** association — it does NOT know which item the number belongs to.

## 4. Calorie double-count — NOT happening today

`apps/api/src/drhiro_api/routers/dashboard.py` lines 251–257: `calories_kcal_today` sums `totals_json.kcal` from **meals only**. Liquid `Measurement` rows contribute only `amount_ml`. So the real calorie risk is **duplicate MEAL rows from retries**, NOT the liquid writer. We will NOT add liquid-kcal handling; meal items remain the single dietary-calorie source.

## 5. Corrections/deletion — separate paths, no linkage

**File**: `docs/reference/intelligent-meal-service.py`
- `latest_meal` (line 1389): `SELECT id FROM meals ORDER BY created_at DESC LIMIT 1` — **no user_id filter**
- `delete_meal_by_fragment` (line 1365): `SELECT id, notes FROM meals WHERE notes ILIKE :pat` — **no user_id filter**
- `correct_meal_item` (line 1447): rebuilds totals with only **4 nutrients** (kcal/protein/carbs/fat) — **drops fiber_g + sodium_mg**
- Low-confidence rejection (lines 1180–1200): after the DDG fallback block, if `candidates` is truthy but the top is still below MIN_CONFIDENCE, execution **falls through** and logs the sub-threshold candidate anyway (the `if candidates and sel_idx < len(candidates)` at line 1200 is satisfied by the now-populated `candidates` list from the fallback, but the confidence floor was only checked once before the fallback — and after fallback the code does NOT re-check).
- Meal group not validated: `meal_type` defaults to `"snack"` (line 1129), accepts any string.
- `correct_meal_item` **overloads** `meal_type` field to carry a replacement food name (line 1407: `right = (req.meal_type or "").strip()`) — must NOT globally validate meal_type until corrections get a dedicated schema.

## 6. Hard-coded secrets

- `sse_server.py` line 1646: hard-coded user UUID
- `sse_server.py` line 1651: hard-coded internal API URL `http://172.20.0.1:8010`
- `docs/reference/intelligent-meal-service.py` line 37: `JWT_SECRET = os.environ.get("DRHIRO_JWT_SECRET", "change-me-in-production")` — default fallback
- `docs/reference/intelligent-meal-service.py` line 35: `DB_URL` default `postgresql+psycopg2://drhiro:drhiro@localhost:5432/drhiro` — default credentials

## 7. Where service.py lives

The 1816-line `service.py` is **deploy-only** on the VPS at `/opt/apps/intelligent-meal/service.py`. It is NOT in the git repo. The branch's versioned reference copy is at `docs/reference/intelligent-meal-service.py` (pulled read-only via sshpass). The patch targets this reference as the authoritative spec; the actual production file is on the VPS and will be updated via the normal deploy pipeline.

## 8. Models vs SQL mismatch (watcher)

- `meals` table has `eaten_at` (tz-aware), NOT `recorded_at`
- `measurements.source_record_id` is `NOT NULL` with a unique constraint on `(user_id, source_provider, source_record_id)`
- `measurements.start_at` / `end_at` are tz-aware
- `meal_items` has no `source_record_id` column — item identity must be derived from a new linkage table or a hash of (meal_id, display_name, created_at)

---

**Conclusion**: The fix must (a) propagate a stable operation ID from Telegram ingress through to the DB, (b) unify parsing into a single canonical-item extractor, (c) write meal + liquid in ONE transaction with DB-level uniqueness, (d) persist the operation result for replay, (e) unify all mutation paths through shared logic, (f) add meal-group validation AFTER giving corrections a dedicated schema, (g) fix the 4-nutrient rebuild, (h) fix the low-confidence fall-through, (i) remove hard-coded secrets.
