# Review round 8 — focused corrections

Response to the independent review of `review7_focused_bundle.tar.gz`. **No new
candidate archive is built. Production untouched.** Candidate `7e2cf69` and the frozen
release archive remain unchanged.

Base for this delta: the reviewed head. See `BASE_AND_HEAD.md` in the bundle for the
exact full commit ids.

## What the review got right

The review found defects the previous checkpoint's evidence did not expose. Two are
conceded plainly:

1. **`cur.execute(...).fetchone()` on a psycopg2 cursor.** psycopg2's `execute()`
   returns `None`, so the chained call raised `AttributeError` before any status was
   read. Independently, the row mapping was wrong: the SELECT returned eight fields
   while the code read `text=row[7]` (actually `kind`) and `kind=row[8]` (out of
   range). **Every existing-receipt case raised.** The count-based test passed anyway
   because counts stayed unchanged. This was a real, user-visible bug and the
   reviewer's reproduction was correct.

2. **The resend completion ignored its UPDATE's rowcount**, so a superseded attempt
   reported `to_state=resolved_resent` and wrote a transition audit row for a
   transition that never happened.

3. **`count(action='resend')` counted audit ROWS.** Each attempt writes a start row
   and a completion row, so every resend was counted twice.

4. **`bool(payload.get('duplicate_risk_ack', False))`** accepted the string `"false"`
   as consent, because a non-empty string is truthy.

5. **The migration validator accepted four non-equivalent schemas** (modified default,
   partial index, cross-schema FK, `timestamp without time zone`).

6. **`recover_receipts()` was the only place enforcing the attempt budget**;
   `claim_receipt()` did not, and `attempts` incremented on handled failure rather
   than at claim, so repeated crashes were unbounded.

## Corrections made

### B1 — duplicate receipt handling

- Cursor contract fixed: `execute()` and `fetchone()` are separate statements, using
  `RealDictCursor` with **named** access, so column order cannot silently mis-map.
  The same named-access fix is applied to the shared `process_update` pipeline.
- The unfinished branch re-drives from the **stored `raw_text`**; the redelivered text
  is never used.
- `exhausted` and row-missing states are handled explicitly.
- **Retry budget enforced AT CLAIM and consumed on acquisition.** `attempts` now
  counts every claim taken, including one abandoned by a hard crash that never reached
  `fail_receipt`, so no entry path (redelivery, lease recovery) can drive an update
  forever. `fail_receipt` no longer double-counts.
- **Exhaustion is operator-visible**: status becomes `exhausted` with a recorded
  reason. Two holes were found while testing this and closed:
  - the claim-denied path now distinguishes "budget spent" from "another worker holds
    it" and records the former;
  - `recover_receipts()` scans with `attempts < cap`, which **silently excluded**
    at-cap receipts — so a spent-budget receipt would have stalled in `received`
    forever, neither redriven nor reported. It is now swept into `exhausted` and
    counted.
- Observability added: the main loop **catches** consume exceptions, so a broken
  duplicate path could leave the suite green while the queue never drained. Counters
  (`consume_error`, `receipt_duplicate_*`, `consume_failed_claim_released`,
  `receipt_attempt_budget_exhausted`) are exposed on `/admin/status` so tests assert
  successful handling rather than merely unchanged numbers.

### B1/B7 — real polling-offset contract

While building the "queue drains" assertion, a further defect surfaced that the review
had not identified: **acknowledgement never worked at all.** The ingress posted to
`/bot<token>/ack`, but the fake only served `/_control/ack`, so every ack 404'd
silently (`_safe_ack` swallowed the exception) and `getUpdates` re-served the same
updates forever — the ingress was reprocessing the same messages in a loop
(`receipt_duplicate_completed` reached 660 in observation).

This is fixed properly rather than papered over:

- The bespoke `/ack` route is **removed**.
- The fake implements the **real Bot API offset semantics**: `getUpdates(offset=N)`
  confirms everything below `N` and returns `N` onwards.
- The ingress persists its offset **durably** in `telegram_consumer_offset` (the table
  already existed for this purpose and was unused).
- The offset is written **after** processing, so a crash re-fetches rather than skips;
  the durable receipt remains the correctness boundary, and the offset is an
  efficiency measure only.

B7's polling-offset item is therefore **implemented**, not merely documented. Sender
identity and edit/content-conflict handling remain **unimplemented** and B7 stays open.

### B4 — attempt fencing

- The resend completion **captures `rowcount`**. If zero rows were affected, the
  result reports `applied=false`, `stale=true`, the **truthful current state** and the
  superseding attempt id — never a fabricated `resolved_resent`.
- `reply_audit` gains `attempt_id` and `applied`, so an **observation** from a
  superseded attempt is distinguishable from an **applied transition**. A stale row
  records `applied=false` and states it was superseded.
- Attempt numbering counts **distinct attempt ids**, not audit events.
- `/admin/audit` now exposes `attempt_id`/`applied`, and `/admin/status` exposes
  `current_attempt_id` — the fencing evidence is no longer unobservable.
- `/admin/recover` now drives **both** recovery paths, so tests and operators can
  exercise receipt recovery deterministically instead of waiting for the interval.

### B6 — conservative definition equivalence

`diff_activities` now rejects each case the review reproduced:

- **Defaults compared as exact canonical expressions**, not substrings, so
  `now() + interval '1 day'` is fatal.
- **Indexes compared as full definitions**: partial indexes (`WHERE false`), unique
  indexes and non-btree access methods are fatal even when the name and columns match.
  Read from the catalog, because SQLAlchemy's `get_indexes()` does not expose the
  predicate.
- **FK target schema resolved**: `unrelated.users(id)` is fatal; a same-named table in
  another schema is a different constraint.
- **Type attributes preserved**: types are read from the catalog
  (`format_type`) and `_canon_type` consults real attributes before `str()`, because
  SQLAlchemy renders `TIMESTAMP(timezone=True)` as plain `"TIMESTAMP"`. `timestamp
  without time zone` is now fatal where the declared shape is `with time zone`.

A **baseline test** asserts the unaltered production shape is *not* flagged, so these
tests cannot pass by rejecting everything.

### Strict resend confirmation

`duplicate_risk_ack` must be the **literal JSON boolean `true`** (`is not True`).
`false`, `null`, missing, numbers, strings (`"true"` included), lists and objects are
all rejected, and a rejected request neither sends nor claims the outbox row.

## Evidence

`evidence/review8_test_results.txt` records the commands, environment, and the
migration's **failing-before / passing-after** output, plus the ingress regression
results. Source paths and hashes actually imported are recorded.

### Rerun discipline

Host commit labels are insufficient — `drhiro_api` is an **editable install** pointing
at the main tree, so running inside a worktree of the base silently imports the FIXED
source and a "failing-before" run would pass. Every before/after run therefore forces
`PYTHONPATH` to the intended tree and **prints the resolving module path** plus
fix-marker attributes as proof.

## Still open

- **B7**: sender identity and edit/content-conflict handling remain unimplemented.
- Production container split, rotation cutover, OpenClaw spool wiring, R1 partial, R4
  production-mirror preservation, R6 cutover/rollback.
- Attempt-fencing coverage now includes a late result **while a newer attempt is in
  flight**, for both ordinary delivery and the explicit resend — the gap reported in
  the previous round. This runs against a disposable stack and is **not** production
  acceptance.
