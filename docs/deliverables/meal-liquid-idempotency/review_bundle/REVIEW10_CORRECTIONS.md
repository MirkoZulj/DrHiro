# Review round 10 — focused FK mapping + schema policy

Response to the independent review of `review9_focused_bundle.tar.gz`. **No new
candidate archive.** Candidate `7e2cf69` and the frozen release archive remain
unchanged. Production untouched. Exact base/head commit ids are in `BASE_AND_HEAD.md`.

## Blocking finding — referenced FK columns were no longer validated (FIXED)

The round-9 OID rewrite unnested `c.confkey` but **never joined those attribute numbers
to the referenced relation's `pg_attribute`**, never aggregated their names, and never
compared them to `['id']`. It validated the constraint name, the local columns, the
referred table OID and `ON DELETE`, but not the referred column list. A constraint such
as

```
FOREIGN KEY (user_id) REFERENCES users(alternate_id) ON DELETE CASCADE
```

could therefore pass whenever `users.alternate_id` was a compatible unique UUID column.
That was a **regression I introduced in round 9**: the round-8 validator did compare
`referred_columns` (via reflection) and I dropped the check when I replaced reflection
with the catalog. The reviewer's reproduction was correct and the query inspection was
correct — the referenced attribute names were not selected at all, so PostgreSQL
execution provided no target-column comparison either.

**Fix.** The query now joins `f.attnum` to `pg_attribute` on `c.confrelid` and
aggregates the referenced column names in **ordinal order**, alongside the constrained
columns in the same ordinal order. Both sides of the mapping are compared, so the
pairing is what is validated — not merely the table identity:

```
JOIN pg_attribute fa ON fa.attrelid = c.confrelid AND fa.attnum = f.attnum
string_agg(fa.attname, ',' ORDER BY f.ord) AS ref_cols
```

`ref_cols != ['id']` is now FATAL with a precise diagnostic naming the actual
referenced columns and relation.

## Same change — schema-policy inconsistency resolved

The round-9 code resolved the intended `users` relation with a **separate** unqualified
lookup (`_relation_oid(conn, "users", None)`) while the comment and diagnostic claimed
the target must be the users relation **in the activities schema**. Those are different
rules, and the reviewer's counterexample is exact: with `search_path = first, second`,
where `first` holds `users` but no `activities` and `second` holds both, `activities`
resolves to `second.activities` while `users` resolves to `first.users`. OID comparison
alone cannot enforce same-schema ownership.

**Policy chosen and documented: same-schema ownership.** The intended users relation is
derived from the **resolved activities relation's namespace**
(`_namespace_of` → `_relation_in_namespace`), so the namespace that was actually
selected for `activities` governs the target.

Two consequences handled in the same change:

- **Creation is now consistent with validation.** `create_activities` previously
  emitted an unqualified `REFERENCES users(id)`, which resolves through `search_path`
  at CREATE time and can bind a different schema's `users` than validation (and the
  operator) expects. It now emits the FK **schema-qualified** against the same
  namespace as the table being created (`REFERENCES {q}users(id)`), so a fresh
  database satisfies the same rule the validator enforces.
- The diagnostic no longer says "the activities schema" while behaving differently; it
  reports the expected users OID in the **same namespace as activities**.

### The users-only-leading-schema case is now tested

With `search_path = first, second` (`first` holds only `users`), `second.activities`
referencing `second.users` is **accepted** through the default `schema=None` entry
point, and repointing the FK at `first.users` — the schema an independent unqualified
lookup would have found — is **FATAL**. The previous tests placed the intended schema
first and so never exercised this.

## Before/after evidence

Both new regressions fail against the **submitted round-9 validator** (commit
`9076937`) and pass after the correction:

- `test_referenced_column_alternate_id_is_fatal` — round 9 returned `fatal=[]` for
  `REFERENCES users(alternate_id)`.
- `test_users_only_leading_schema_is_not_used` — round 9 resolved `users` to
  `first.users` (oid 118616) and then **rejected the correct `second.users`** as
  cross-schema, i.e. the policy was not merely undefended but inverted in this case.

The unchanged `users(id)` baseline passes in both.

## Test correction — lease assertion (reviewer's coverage note)

`test_concurrent_recovery_does_not_revoke_live_final_claim` asserted status and claim
token preservation but **not** the lease, despite the round-9 report saying it did. That
was an accurate criticism and the claim was overstated. The test now asserts the lease
is **unexpired at the checkpoint**, that its timestamp is **unchanged** after the
concurrent recovery pass, and that it remains unexpired afterwards.

## Evidence/provenance clarifications

- **Capture-time revision.** `review9_test_results.txt` recorded docs head
  `0104940…` while `BASE_AND_HEAD.md` named `afa4103…`. Both are correct but they are
  **different revisions**: the evidence records its own commit, so committing it
  necessarily creates a newer docs head. The capture now labels that line explicitly as
  the **capture-time revision** and states the relationship. No historical capture
  output was rewritten.
- **Default suite.** Its total is now **produced inside the capture** (command and
  output in `review10_test_results.txt`), not reported separately.
- **Runtime provenance.** The capture continues to record the SHA-256 of
  `ingress.py` actually loaded by the running container. It repeats the round-9 value
  (`a5d33b2…`) because `ingress.py` is unchanged in this round — the round-10 changes
  are confined to the validator and the tests. That remains submitted capture evidence,
  not a live inspection by the reviewer.

## Still open

- **B7**: sender identity and edit/content-conflict handling remain UNIMPLEMENTED.
- Production container split, rotation cutover, OpenClaw spool wiring, R1 partial, R4
  production-mirror preservation, R6 cutover/rollback.
- The stack suite passed but exercises a disposable stack: **evidence, not production
  acceptance**. Unchanged uptimes support "no observed restart" only.
