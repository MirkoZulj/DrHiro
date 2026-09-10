# R4 ORM↔Alembic Parity — Evidence

Date: 2026-09-09
Branch: `feature/meal-liquid-idempotency`
Status: **parity demonstrated for the comparison dimensions listed below; migration-history and data-preservation checks remain OPEN (see §5).**

Scope: preparation only. No deploy, no push, no production write.

## 1. What was found

Running the integration tests against an **Alembic-built** database (schema from
`alembic upgrade head`, no `Base.metadata.create_all`) exposed two ORM-declared
columns that the Alembic chain never created:

| Table | Column | ORM type | Present in production? | Present in Alembic chain? |
|---|---|---|---|---|
| `users` | `basal_metabolism_kcal` | float (nullable) | yes (create_all) | **no** (before fix) |
| `food_catalog_items` | `food_id` | uuid (nullable, FK→foods.id) | yes (create_all) | **no** (before fix) |

Consequence: a database built purely from the migration chain could not satisfy
the ORM, so `alembic`-built environments failed on the first `User` insert. This
is exactly the divergence the review required be surfaced by testing against a
migration-built database.

## 2. Fix

`apps/api/alembic/versions/9a1b2c3d4e5f_orm_column_gaps.py` — new head revision
(chains after `c9d0e1f2a3b4`), adds both columns with types transcribed from the
authoritative production schema:

- `users.basal_metabolism_kcal` — `FLOAT`, nullable
- `food_catalog_items.food_id` — `UUID`, nullable, FK `food_catalog_items_food_id` → `foods.id`

Downgrade drops both columns and the FK constraint. Additive-only; no other
table or row is touched.

## 3. Exact comparison scope ("full parity" qualified)

Demonstrated on `drhiro_r4test_alembic` (Alembic-built, head `9a1b2c3d4e5f`),
by `tests/test_r4_orm_alembic_parity.py` (8 tests, GREEN).

**Compared — 28 ORM tables the chain creates, 291 columns:**

| Dimension | Method | Result |
|---|---|---|
| Table presence | every chain-owned ORM table exists | 0 missing |
| Column presence | every ORM column exists (291) | 0 missing |
| Column TYPE | compared after canonicalizing equivalent spellings (`VARCHAR(n)`~`VARCHAR`, `DOUBLE PRECISION`~`FLOAT`, `TIMESTAMP`~`DATETIME`, `CHARACTER VARYING`~`VARCHAR`) | 0 mismatches |
| Column NULLABILITY | ORM `nullable` vs DB `nullable` | 0 mismatches |
| INDEX names | every ORM index name exists in DB | 0 missing |
| UNIQUE constraints | compared by covered column-set (unnamed UCs matched by columns) | 0 missing |
| Server defaults | every ORM column with `server_default` has a DB-side default | 0 missing |

**NOT compared (explicitly out of scope for this claim):**

- FK `ondelete` behaviour (carried by the migration DDL; not asserted here)
- CHECK constraint expressions (not asserted here)
- Index operator class / type (only index *names* compared)
- Column order (irrelevant to ORM reads/writes)
- `activities` — pre-existing ORM table **not created by any migration**
  (documented gap; see §5)

Numerically: **0 type mismatches, 0 nullability mismatches, 0 missing indexes,
0 missing unique constraints, 0 missing server defaults** across the 28
chain-owned tables / 291 columns.

## 4. Failing-before / passing-after

| Phase | Command | Result |
|---|---|---|
| RED (downgrade to pre-parity head `c9d0e1f2a3b4`) | parity suite | **3 failed, 1 passed** (missing-column + regression + nullability) |
| GREEN (upgrade to head `9a1b2c3d4e5f`) | parity suite | **8 passed** |

Repro:
```
alembic downgrade c9d0e1f2a3b4   # RED
DRHIRO_TEST_DB_URL=...drhiro_r4test_alembic pytest tests/test_r4_orm_alembic_parity.py   # 3 failed
alembic upgrade head              # GREEN
DRHIRO_TEST_DB_URL=...drhiro_r4test_alembic pytest tests/test_r4_orm_alembic_parity.py   # 8 passed
```

## 5. OPEN — not yet demonstrated (do NOT treat as closed)

The following were requested and are **not** substantiated by this commit:

1. **Migration-history change explanation and revision graph.** Making
   `b2f3c4d5e6f7` a new root and re-parenting `3c00321778bc` alters
   previously-applied history. A written revision-graph explanation and a
   proof that databases already at supported revisions upgrade safely is
   **outstanding**.
2. **Production existing-table validation.** Explicit confirmation that
   production's existing tables are unaffected by the inserted baseline is
   **outstanding**.
3. **Production-mirror data preservation with representative linked records.**
   The earlier mirror was schema-only; preservation of *values and
   relationships* (not just row counts) using synthetic linked data is
   **outstanding**.
4. **Downgrade safety qualification.** Dropping the four nutrient columns
   destroys their contents. This is **not** lossless rollback; unrelated-data
   preservation must be demonstrated, and downgrade+upgrade must not be called
   proof of idempotency. **Outstanding.**
5. **`activities` provisioning.** `activities` is ORM-declared but not created
   by any migration. Either its supported provisioning is included or the
   fresh-database claim is narrowed explicitly. Currently: **narrowed** — the
   fresh-build claim covers the 28 chain-owned tables, NOT `activities`.

## 6. Test-environment note

The shared test suite still builds schema via `Base.metadata.create_all`, which
adds new tables but not columns to existing Alembic tables. The decisive R4
checks therefore run against a **dedicated Alembic-built database**
(`drhiro_r4test_alembic`). Closing the create_all divergence for the whole
integration suite remains a follow-up.
