# Review round 9 — focused corrections

Response to the independent review of `review8_focused_bundle.tar.gz`. **No new
candidate archive.** Candidate `7e2cf69` and the frozen release archive remain
unchanged. Production untouched. Exact base/head commit ids are in `BASE_AND_HEAD.md`
in the bundle.

## Finding 1 — exhaustion sweep revoked an unexpired final claim (FIXED)

The sweep marked `status NOT IN ('completed','exhausted') AND attempts >= cap` as
exhausted regardless of lease ownership. The FINAL permitted claim increments attempts
to the cap AND acquires a valid lease; a concurrent recovery pass could therefore set a
genuinely-in-progress receipt to exhausted and clear its claim token before the final
attempt finished.

**Fix.** The sweep's predicate now uses the SAME ownership condition as claim
acquisition:

```
AND (claim_token IS NULL OR lease_expires_at IS NULL OR lease_expires_at < now())
```

An in-flight final attempt is left alone; only a spent budget whose lease is expired or
abandoned is surfaced as `exhausted`.

**Regressions (3), against a disposable stack, with a genuinely concurrent recovery
actor** (a separate process importing `ingress.recover_receipts()`, not the same
process whose HTTP admin the paused worker would otherwise block):

1. **Live final claim survives concurrent recovery.** The worker is paused after
   acquiring its final permitted claim (attempts -> cap, lease held). A concurrent
   recovery pass returns `exhausted: 0` and the receipt's status, claim token and lease
   are byte-identical afterwards. The worker is then released, persists EXACTLY one
   consumption, and completes.
2. **Expired final claim is exhausted and refuses redelivery.** An at-cap receipt with
   an expired/abandoned lease is swept to `exhausted`, and a subsequent redelivery is
   refused with no new consumption.
3. A `pause_after_claim` test hook (trusted spool only) makes the interleaving
   deterministic instead of racy.

## Finding 2 — schema=None catalog reads combined tables across schemas (FIXED)

Every catalog query filtered `(:s IS NULL OR nspname = :s)` and keyed results by
column/index name only, so with the default `schema=None` they read EVERY `activities`
table in EVERY schema and could mix column types, indexes or CHECKs from one relation
with columns and FKs from another.

**Fix.** `_relation_oid(conn, name, schema)` resolves the ONE intended relation by OID,
using the SAME name-resolution policy as the migration's own unqualified SQL
(`to_regclass` through search_path when schema is None, schema-qualified otherwise).
Every catalog read (`column_types`, `index_definitions`, `not_null_columns`,
`_check_constraints`, `table_exists`) anchors to that OID. The FOREIGN KEY is now
validated directly from `pg_constraint`: its `confrelid` must equal the resolved
intended `users` relation's OID, and `confdeltype` must be `c` — identity, not name
strings, and independent of reflection's `referred_schema`.

**Regressions (3), real PostgreSQL through the DEFAULT `schema=None` entry point, with
a second schema holding a same-named `activities` and `users`:**

- Misleading metadata in the OTHER schema (wrong `created_at` type, a VALID `>= 0`
  CHECK, a partial index) neither contaminates the intended relation's types nor masks
  a genuine defect (a wrong CHECK in the intended relation stays FATAL).
- A faulty target relation (`unrelated.users`) is FATAL by identity, not name.
- `REFERENCES users(id)` (schema omitted, search_path-visible) resolves to the intended
  users relation and is accepted — the search_path-sensitive reflection case.

Also corrected en route: `to_regclass` returns the regclass NAME (psycopg2 text
protocol), so `_relation_oid` casts `::oid` to get the numeric identity the queries
anchor on.

## Finding 3 — type normalisation still discarded length, precision and casts (FIXED)

`_canon_type` collapsed every VARCHAR to `VARCHAR` and every TIMESTAMP zone form to a
precisionless string, so `title VARCHAR(1)` read as equivalent to `VARCHAR(255)`;
`_canon_default` stripped casts, so `now()::date` read as equivalent to `now()`.

**Fix.**

- `_canon_type` preserves VARCHAR length (`VARCHAR(255)` vs `VARCHAR(1)`) and timestamp
  precision (`TIMESTAMP(3) WITH TIME ZONE` vs the declared no-precision shape). The
  declared `title` is now `VARCHAR(255)`. Any length/precision is a difference and is
  rejected.
- `_canon_default` erases ONLY formatting (whitespace, case, trailing semicolon, a
  redundant outer parenthesis pair) — never a cast. `now()::date` is not equivalent to
  `now()`. PostgreSQL's exact stored rendering (`pg_get_expr`) is the comparison basis;
  the test asserts what the server actually stored.

**Regressions (5), real PostgreSQL:** `VARCHAR(1)` FATAL; `VARCHAR(255)` baseline
accepted; `timestamp(3) with time zone` FATAL; a cast-changed default
(`SET DEFAULT (now()::date)`) FATAL; and the declared `DEFAULT now()` accepted with the
server's stored `pg_get_expr` rendering asserted to be exactly `now()`.

Clean failing-before detectors (fail pre-fix / pass post-fix): `VARCHAR(1)`,
`timestamp(3)`, multi-schema contamination, and search_path FK resolution. The
cast-changed-default test PASSES pre-fix for a different reason than post-fix (the
pre-fix normaliser leaves a residual outer parenthesis that still mismatches), so it
guards the invariant but does not by itself demonstrate the cast-preservation change;
that is demonstrated at the unit level (`_canon_default` no longer strips casts) and by
the real `pg_get_expr` assertion.

## Evidence qualifications

- **Runtime source identity.** The evidence records the source path and SHA-256 hash of
  `ingress.py` ACTUALLY LOADED BY THE RUNNING INGRESS CONTAINER (`sha256sum` of the file
  inside the container), in addition to the host/worktree hashes — host labels alone do
  not establish what a running image executed.
- **Unfinished-duplicate coverage.** The test now asserts `receipt_duplicate_unfinished`
  advances, so the suite cannot be satisfied by a recovery pass that never exercised the
  duplicate branch.

## Still open

- B7: sender identity and edit/content-conflict handling remain UNIMPLEMENTED (B7 open).
- Production container split, rotation cutover, OpenClaw spool wiring, R1 partial, R4
  production-mirror preservation, R6 cutover/rollback.
- Disposable-stack evidence is not production acceptance. Container uptimes unchanged
  support "no observed restart" only; they do not independently prove configuration,
  credentials or data were unchanged.
