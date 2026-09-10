# R4 — Remaining Migration Evidence (this checkpoint)

Date: 2026-09-09
Branch: `feature/meal-liquid-idempotency` (isolated)
Status: **new evidence; several previously-open R4 items now addressed.**

## 1. Revision graph (exact)

```
<base> -> b2f3c4d5e6f7  Canonical food-domain baseline (data_sources, nutrients,
                        foods, food_nutrients, food_brands, food_ingredients)
b2f3c4d5e6f7 -> 3c00321778bc  initial canonical schema (users, meals, ...)
3c00321778bc -> c4d8e2f6a9b1  food_resolution_rules
c4d8e2f6a9b1 -> d5e6f7a8b9c0  recipes
d5e6f7a8b9c0 -> e7f8a9b0c1d2  app_settings
e7f8a9b0c1d2 -> f1a2b3c4d5e6  consumption idempotency + items + beverage linkage
f1a2b3c4d5e6 -> a1b2c3d4e5f7  payload_hash
a1b2c3d4e5f7 -> c9d0e1f2a3b4  nutrient-resolution provenance columns
c9d0e1f2a3b4 -> 9a1b2c3d4e5f (head)  ORM-declared columns missing from schema
```

`b2f3c4d5e6f7` is the new root (down_revision=None); `3c00321778bc` was re-based
to run after it. `alembic history` confirms the linear, single-head chain above.

## 2. Production existing-table validation (read-only)

Production (`drhiro-postgres-1`, `drhiro` DB):
- `alembic_version` = **`d5e6f7a8b9c0`** — the recipes revision, **before** the
  consumption-idempotency migration.
- **27** public tables.
- `activities` → **exists** (created out-of-band, NOT by the Alembic chain).
- `consumption_operations` → **does not exist** (production is pre-feature).

**Upgrade safety:** production sits on `d5e6f7a8b9c0`, which IS a revision in the
chain. `alembic upgrade head` from `d5e6` runs only the four later feature
migrations (`e7f8a9b0c1d2`, `f1a2b3c4d5e6`, `a1b2c3d4e5f7`, `c9d0e1f2a3b4`,
`9a1b2c3d4e5f`) — the inserted baseline `b2f3c4d5e6f7` and the re-based
`3c00321778bc` are **not** replayed for an already-provisioned database. This
addresses R4 item 2 (production at a supported revision upgrades safely without
replaying the new baseline).

## 3. `activities` provisioning (R4 item 5 — now concretely characterised)

- `activities` is ORM-declared (models.py) but **not created by any migration**.
- The food-domain baseline docstring claims it is "provided by a separate
  migration that follows 3c003" — **no such migration exists**.
- ORM table diff: 28 ORM tables, 28 migration-created names, with **only
  `activities` missing** from the chain.
- Production already has `activities` (created out-of-band). A **fresh
  Alembic-built DB does NOT**, so a fresh build is missing `activities`.

**Status:** the fresh-database claim is **narrowed** to the 27 chain-owned tables
and **excludes `activities`**. This is a genuine, unresolved gap: a fresh DB
lacking `activities` is not a complete application bootstrap if current handlers
reference it. A follow-on migration must create `activities` (with its FK to
`users.id`, respecting the dependency on `users` from `3c003`). **Open.**

## 4. Downgrade safety qualification (R4 item 4)

Dropping the four nutrient-resolution columns (`nutrient_basis`,
`resolution_source`, `food_catalog_item_id`, `nutrition_complete`) destroys their
contents; this is **not** lossless rollback. No test will call downgrade+upgrade
"proof of idempotency." The T1 cutover/rollback plan treats schema downgrade as
emergency-only and never as a routine path. **Qualified as destructive.**

## 5. Remaining open R4 items

- **Item 3 (production-mirror value/relationship preservation):** a synthetic
  mirror with linked records (meal → items → beverage link) compared by values
  and relationships across the migration is still **outstanding** — the earlier
  mirror was schema-only, and building a faithful synthetic mirror is a
  dedicated step.
- **Item 5 (`activities`):** open, characterised above.

These are reported honestly: they are not closed by this checkpoint.
