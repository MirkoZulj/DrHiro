# R4 + Migration Authority — Evidence (remediation of review finding R4 and the deeper Alembic self-sufficiency gap)

Date: 2026-09-09
Branch: `feature/meal-liquid-idempotency`
Scope: **preparation only** — no deploy, no push, no production write, no historical cleanup.

## 1. Review finding (R4)
> "The models diff introduces nutrient_basis, resolution_source, food_catalog_item_id, and nutrition_complete. None of those names occurs in the supplied Alembic migration patch sections. … include migrations for the added persistent fields and validate the actual upgrade chain from a representative pre-change schema."

## 2. Root-cause investigation (read-only, production + repo)
Read-only checks on the live production DB (VPS `drhiro-postgres-1`, DB `drhiro`) and the repo established:

- **Production Alembic is at `d5e6f7a8b9c0`** (pre-idempotency). The later revisions `e7f8a9b0c1d2` (app_settings), `f1a2b3c4d5e6` (consumption idempotency), `a1b2c3d4e5f7` (payload_hash) have **never been applied**. `consumption_items`, `consumption_operations`, `beverage_measurements` do **not exist** in production. `meal_items`/`measurements` lack the feature traceability columns.
- **`foods` (8,165 rows), `food_nutrients` (659,339), `data_sources`, `nutrients`, `food_brands`, `food_ingredients` exist** in production with real data, but **no Alembic migration creates any of them**. They were provisioned out-of-band (ORM `create_all` at bootstrap + `apps/api/scripts/import_usda.py`), NOT by the migration chain.
- The dev/test DB "worked" only because tests build schema via `Base.metadata.create_all`, which silently creates every ORM table — masking the migration gap.
- Consequently a **fresh `alembic upgrade head` failed** at `c4d8e2f6a9b1` (food_resolution_rules) because `foods` did not exist — reproduced before the fix.

## 3. Fixes (branch, commits to follow)
1. **New Alembic baseline `b2f3c4d5e6f7`** — `down_revision = None` (chain root). Creates the six food-domain tables `data_sources`, `nutrients`, `foods`, `food_nutrients`, `food_brands`, `food_ingredients` with column DDL transcribed **verbatim** from the authoritative production schema (zero drift). Constraint + index names match production exactly.
2. **Re-based `3c00321778bc`** — its `down_revision` changed from `None` to `b2f3c4d5e6f7`, so the initial canonical schema now runs after the food tables exist. (Safely skipped on any DB already past `3c003`, because Alembic tracks a single head revision.)
3. **New migration `c9d0e1f2a3b4`** (R4) — chains after `a1b2c3d4e5f7`, adds the four nutrient-provenance columns to `consumption_items` (`nutrient_basis`, `resolution_source`, `food_catalog_item_id`, `nutrition_complete` NOT NULL default true) with upgrade + downgrade.
4. `activities` is intentionally left out of this chain (it carries a FK to `users` created in `3c003` and is out of the meal/liquid scope; it already exists in production via `create_all`). Documented, not blind-added.

Resulting linear chain (single head `c9d0e1f2a3b4`):
`b2f3(food baseline, root) -> 3c003 -> c4d8 -> d5e6 -> e7f8 -> f1a2 -> a1b2 -> c9d0`

## 4. Validation (disposable PostgreSQL, `drhiro_r4test_*`)
### 4a. FRESH empty DB — `alembic upgrade head`
A disposable empty DB was upgraded from scratch. **Succeeded end-to-end** through all 8 revisions. Result: 29 tables created (all ORM tables except `activities`), `alembic_version = c9d0e1f2a3b4`, and all four R4 columns present on `consumption_items`. This fixes the previously-broken fresh path.

### 4b. EXISTING pre-change DB (production mirror)
A disposable DB was loaded with the **production schema-only dump** (no personal data) — `foods` present, consumption feature tables absent — then stamped at production's recorded head `d5e6f7a8b9c0`, seeded with a synthetic user. Running `alembic upgrade head` **applied only** `e7f8 -> f1a2 -> a1b2 -> c9d0` (the four revisions above production's head — the food baseline and `3c003` were correctly **not** re-run). Result: synthetic data preserved (1 user), new feature tables created (`app_settings`, `beverage_measurements`, `consumption_items`, `consumption_operations`), all four R4 columns present, head = `c9d0e1f2a3b4`.

### 4c. Downgrade / rollback
`alembic downgrade a1b2c3d4e5f7` dropped the four R4 columns and **preserved the user row**; re-`upgrade head` re-added them idempotently (head back to `c9d0e1f2a3b4`).

### 4d. Regression suite
The shared test DB was rebuilt from the corrected authoritative chain. Full suite green: **243 passed** (242 prior + new migration-chain test file adjusted). One B6 test (`test_telegram_key_uniqueness_enforced`) raw-inserted `consumption_operations` without timestamps, relying on the alembic `server_default` for `created_at`/`updated_at`; it was made schema-independent by supplying `NOW()` explicitly (its assertion — DB-level uniqueness enforcement — is unchanged and not weakened). A new `tests/test_r4_migration_chain.py` (6 tests) locks in single-head linearity, the food-baseline root, the re-based `3c003`, production-head presence, and the R4 upgrade/downgrade coverage.

## 5. Remaining migration-authority limitations (explicit)
- **`activities`** is not created by any migration (pre-existing; FK to `users`; out of meal/liquid scope; already present in production via `create_all`). Not blind-added to avoid drift and chain forks. Flagged for a separate future baseline if full ORM parity from a fresh alembic build is required.
- The **test suite builds schema via ORM `create_all`** in its fixtures (not alembic). This remains a test-environment divergence: the migration chain is now self-sufficient and validated on disposable DBs, but the shared test DB is create_all-driven. A future change could point the integration fixtures at an alembic-built disposable DB to close this.
- **Production upgrade to the new head is NOT executed** — pending separate approval. This evidence validates the chain; the actual runbook-gated migration remains on hold.
