# Review round 11 — creation-path same-schema policy

Response to the independent review of `review10_focused_bundle.tar.gz`. **No new
candidate archive.** Candidate `7e2cf69` and the frozen release archive are unchanged.
Production untouched. Exact base/head commit ids are in `BASE_AND_HEAD.md`.

## Blocking gap — default-schema creation was still unqualified (FIXED)

The reviewer's reading was exactly right. The round-10 `{q}users(id)` qualification
only took effect **when a schema was passed explicitly**: `_qualify(None)` returns an
empty string, so `create_activities(conn)` still emitted

```sql
CREATE TABLE activities (...
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
```

Both names were therefore resolved independently by `search_path`, and in the
reviewer's counterexample they resolve to **different schemas**: with
`search_path = first, second`, where `first` is writable and has no `users`, and
`second` has `users` but no `activities`, the unqualified `CREATE TABLE` lands in
`first` while the unqualified FK lookup finds `second.users`. Creation **succeeded**
with precisely the cross-schema relationship the validator rejects. Reproduced against
the round-10 code:

```
RESULT: create_activities SUCCEEDED  <-- policy violation
STATE: created first.activities referencing second.users
STATE: activities tables in first/second = 1; indexes = 3
```

### Fix

`_creation_namespace_oid()` resolves the namespace an unqualified CREATE would
**actually** land in — the caller's explicit schema when given, otherwise the **first
schema in the effective search path in which the current user may CREATE**
(`current_schemas(false)` + `has_schema_privilege(..., 'CREATE')`), which is
PostgreSQL's own rule for unqualified object creation. The destination is resolved from
that rule, **not** inferred from whichever `users` happens to resolve.

Both the new table **and** its `users` target are then emitted schema-qualified against
that namespace, so table placement and FK target can no longer be decided by two
different lookups.

If the destination namespace has no `users` relation, creation raises the new
`MissingUsersRelation` with a clear message. It **never** silently falls through to
another schema's users — deriving the target from whichever `users` resolves is the bug
being prevented.

After the fix the same counterexample refuses, leaving **0** activities tables and
**0** indexes.

## Regressions (real PostgreSQL)

- `test_default_schema_creation_refuses_cross_schema_users` — the counterexample;
  asserts a clear failure and that no activities table or index survives.
- `test_default_schema_creation_binds_same_schema_users` — positive: with
  `schema=None` the created FK points at the **destination schema's** users, asserted
  by FK target OID and referenced columns `= id`, and the resulting table validates
  through the `schema=None` entry point.
- `test_explicit_schema_creation_ignores_misleading_search_path` — positive: an
  explicit schema is created with **its own** users even when another schema's users
  leads `search_path`.

Added `deploy/disposable/creation_path_probe.py`, which runs the counterexample against
a chosen source tree and reports the outcome plus the surviving tables/indexes, so the
before/after is reproducible from the evidence alone rather than only through the test
harness.

## Before/after

**Partially an AttributeError, and I am labelling it as such.** Run against the
round-10 tree the new negative fails on `MissingUsersRelation` not existing — that
proves the class is new, not that the behaviour differed. The **behavioural** evidence
is therefore the probe, printed in the capture for both trees:

- round-10: creation **succeeded**, `first.activities` → `second.users`; 1 table, 3
  indexes
- this commit: **refused** with `MissingUsersRelation`; 0 tables, 0 indexes

R4: **38 passed**. The unchanged positives from earlier rounds still pass.

## Minor handoff corrections

- **`BASE_AND_HEAD.md`** no longer labels the functional diff "review round 9" and no
  longer calls `afa4103` the "round-8 state"; it now states **review round 11** and the
  **fully-reviewed round-10 documentation state**, matching the ids and inventory.
- **"Exact commands"** now prints every command **beside the result it produced**,
  including the round-9-validator failing-before command and the default-suite command,
  each preceded by a print of the source path that run actually imported.

## Evidence

- Migration: R4 **38 passed**; round-9-validator negatives **2 failed / 1 passed**
  against round-10, passing after; round-8-era negatives still fail against the base.
- Stack suite: **55 passed** — **not re-run this round**. `ingress.py` is byte-identical
  across the base and this head (Git blob `d183a1463f14402f0f511c5597eb4c84f28525a9`,
  printed in the capture), so the total carries forward on **blob identity**, not on a
  commit label. A differing blob would invalidate that; identical blobs mean the same
  source was exercised.
- Default suite: **363 passed, 139 skipped, 0 errors**.
- Container `ingress.py` hash `a5d33b2…` from the round-10 capture still describes the
  submitted head for the same reason. This remains submitted capture evidence.

## Still open

- **B7**: sender identity and edit/content-conflict handling remain UNIMPLEMENTED.
- Production container split, rotation cutover, OpenClaw spool wiring, R1 partial, R4
  production-mirror preservation, R6 cutover/rollback.
- Disposable-stack results are **evidence, not production acceptance**.
