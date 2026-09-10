# Review #7 — blocker fixes (isolated branch, disposable stack)

Focused fixes for the seven independent-review blockers. **Production untouched.**
Candidate `7e2cf69` and the frozen release archive remain unchanged; this is an
incremental fix, not a new release candidate. No push, deployment, credential
rotation, gate activation, or volume deletion.

Each blocker is fixed on the isolated branch with regression tests. Where a
failing-before run was possible without tearing down an already-fixed tree it is
recorded explicitly; every test encodes the previously-broken behaviour the review
cited, so a regression re-fails.

---

## B1. Unfinished receipts were discarded on redelivery

**Fix.** `telegram_receipts` now retains the trusted input (`raw_text`). A redelivered
update whose receipt already exists but is **not completed** is re-driven from the
stored payload instead of discarded. A new `recover_receipts()` pass re-drives
unfinished receipts **from PostgreSQL without Telegram redelivery** (stale-lease
fencing, attempts cap), at startup and periodically. The consume path and recovery
share one `process_update()` pipeline, so both follow the same claim → persist →
complete → reply contract. Deterministic test hooks (marker files in the trusted spool
volume) crash after the receipt commit and fail before consumption persistence.

**Tests (TestReceiptRecovery, 2):** crash after receipt commit (receipt left
`processing`, payload retained) then Telegram queue wiped → recovery from Postgres
produces exactly one 330-kcal meal and a `completed` receipt; failure before
consumption records an attempt, releases the claim, and recovery produces exactly one
consumption (never two).

## B2. The model network could reach unauthenticated admin routes

**Fix.** The admin HTTP server binds to the ingress **loopback** (`127.0.0.1`), so it
is off the turn-facing interface entirely — the model containers cannot reach it at
the network level, not merely at the auth level. Defence in depth: **every** admin
route (GET and POST, including `/admin/recover` and the data-disclosing GETs) now
requires the Bearer token.

**Tests (TestAdminSurfaceIsNotModelReachable + probe):** from the model container the
ingress admin surface is **unreachable** (measured, `probe["ingress_admin_reachable"]
== False` — access to the ingress itself, not just Postgres/Redis/fake-Telegram);
`GET /admin/status`, `/admin/real-output`, `/admin/audit`, `/admin/recover` all return
`401` unauthenticated and `200` with the token.

## B3. Reply ownership and audit identity were caller-controlled

**Fix.** The principal is now derived from **verified credentials**: `_authenticate()`
returns the fixed `ADMIN_IDENTITY` bound to the token; the request JSON can no longer
supply an `actor`. This is an **administrator-only** surface (documented in code and
here): there is one trusted admin identity, and the audit trail records **that
principal**, never a caller string. `claim_chat_id` remains a *selection scope* (which
reply), not an identity claim; the reply must still belong to a bound `reply_owners`
chat (`403 not_owner`).

**Tests (updated resolution tests):** a `spoofed-actor` string is ignored — the audit
records `admin`; the resend audit records `admin`.

## B4. Delivery completion lacked attempt fencing

**Fix.** `reply_outbox` gains `current_attempt_id`. Every send (`_mark_in_flight`) and
every resend mints a NEW attempt id and stores it; completion (`_finish`, resend
completion) is conditioned on `current_attempt_id` still being that id. Recovery
(in_flight → unknown) and acknowledgment clear it. A delayed result from an old
attempt therefore cannot overwrite a newer send or resolution.

**Tests (TestAttemptFencing, 2):** `old send → unknown → new resolution → late old
result` — the stale completion returns `applied=False`, the terminal state is
unchanged, no extra delivery, consumption untouched. Same for a stale result landing
on `unknown`.

## B5. HTTP errors were incorrectly treated as universally safe to retry

**Fix.** `_api()` now validates Telegram's application-level `ok` and raises
`TelegramAPIError` on `ok=false`, so a success is only recorded when Telegram actually
confirmed it. Delivery classification is conservative:
  - **known-safe retryable (`failed`)** — connection refused / DNS (request never left
    the process), and HTTP **429** (a documented pre-acceptance rate-limit rejection).
  - **ambiguous (`unknown`, never auto-resent)** — any other HTTP response (notably
    5xx), application-level `ok=false`, connection reset after connect, or timeout. A
    5xx alone does **not** prove nothing was delivered.

**Tests (TestHttpClassification, 2):** "accepted then 5xx" → `unknown`, the message
was delivered, and it is **not** auto-retried (no extra delivery after a settling
period); "accepted then `ok=false`" → `unknown`, **not** marked `sent`.

## B6. The migration's adoption validator accepted invalid schemas

**Fix.** `schema_activities.py` now validates **definitions**, not names:
  - CHECK is tested **semantically**: the canonical expression must equal
    `calories_burned >= 0` (PostgreSQL's re-rendered `(- (100)::double precision)`
    etc. is normalised). A same-named check with `>= -100` is rejected.
  - Indexes are matched on their columns (`column_names`), not just their names; a
    right name with wrong columns is fatal, a genuinely missing index is reconcilable.
  - Server defaults (`gen_random_uuid()`, `now()`), the PRIMARY KEY
    (`activities_pkey` on `id`), and the FOREIGN KEY (`user_id → users(id) ON DELETE
    CASCADE`) are now validated; any mismatch is fatal with a precise diagnostic.

**Tests (TestAdoptionRejectsInvalidSchemas, 7).** All 7 failed against the previous
validator and now pass — a genuine failing-before/passing-after demonstration: wrong
lower bound, wrong index columns, missing PK, wrong PK columns, missing FK, wrong FK
target, missing server defaults. Full R4 suite: 18 passed.

## B7. Disposable ingress omitted trusted-ingress behaviour — NOT IMPLEMENTED

**Status: UNRESOLVED except for wording.** The earlier text ("real OpenClaw spool
wiring pending") was misleading, because it implied the behaviour existed but awaited
wiring. It does not exist. This checkpoint corrects the **claim**; the **behaviour**
remains unimplemented and unproven:

  - **Real polling-offset — UNIMPLEMENTED.** The slice acknowledges via the fake
    Telegram `/ack` endpoint. There is no real `last_update_id` offset contract, and
    no durable poll-offset persistence. The durable receipt is the redelivery arbiter
    here; a real offset is not implemented.
  - **Sender identity — UNIMPLEMENTED.** A verified bot identity authenticates the
    BOT, not the SENDER. Every incoming message is mapped to one fixed disposable user
    (`USER_UUID` derived from the bot id). The Telegram sender id is never resolved to
    an internal user, and no sender check is performed. A legitimate bot identity does
    NOT authenticate the sender.
  - **Edits / content conflicts — UNIMPLEMENTED.** An edited message sharing the same
    `message_id` is treated as a duplicate receipt, not as a revision; conflicting
    payload reuse is not reconciled.

No code path implements any of the three. They are listed so the evidence is not
over-read, and they remain open findings. **B7 is not closed by this checkpoint.**

---

## Packaging vs implementation separation

The earlier oversized patch was largely previous review exports and patches embedded
in the tree. For this checkpoint the functional change set is kept **separate** from
packaging artifacts:

**Functional diff, base `8921c305bce543f38653a5a948018a6ba33ba997` → head
`8ea0bcbced78b0f2c1a210047873779cc9008577`** (commits `6b0c39b` then `8ea0bcb`).
Ten files, complete and exact:

| file | commit |
|---|---|
| `apps/api/src/drhiro_api/schema_activities.py` | `6b0c39b` |
| `tests/test_r4_activities_migration.py` | `6b0c39b` |
| `deploy/disposable/app/ingress.py` | `8ea0bcb` |
| `deploy/disposable/app/stack_ctl.py` | `8ea0bcb` |
| `deploy/disposable/app/fake_telegram.py` | `8ea0bcb` |
| `deploy/disposable/app/probe_isolation.py` | `8ea0bcb` |
| `deploy/disposable/app/concurrent_probe.py` | `8ea0bcb` |
| `deploy/disposable/sql/001_trusted.sql` | `8ea0bcb` |
| `deploy/disposable/docker-compose.isolated.yml` | `8ea0bcb` |
| `tests/test_t1_isolated_ingress_stack.py` | `8ea0bcb` |

An earlier revision of this document omitted `concurrent_probe.py` from the functional
inventory; that was an error in the listing, and the inventory above is the corrected,
authoritative one. `concurrent_probe.py` is functional: it was changed to seed its
receipt as `completed` so the live ingress's `recover_receipts()` cannot race the
probe's own writers on the identity under test.

**Documentation / evidence (NOT functional), commit
`42fe5ba3255de74cc846a8a5aa4ac164cda6cb96`:** `REVIEW7_BLOCKERS.md`, plan REVISION 5 in
`T1_implementation_plan.md`, `evidence/review7_test_results.txt`. Identified separately
so packaging is not mistaken for new functional code.

A new candidate archive is **not** built at this checkpoint, per instruction ("submit
focused fixes and evidence before another candidate archive").

## Test state

| Suite | Gate | Result |
|---|---|---|
| `tests/test_r4_activities_migration.py` | `DRHIRO_ACTIVITIES_MIGRATION_DB=1` | **18 passed** (incl. 7 new negative) |
| `tests/test_t1_isolated_ingress_stack.py` | `DRHIRO_ISOLATED_STACK=1` | **37 passed** |
| default suite (production inspection excluded) | — | **363 passed / 101 skipped / 0 errors** |
| R1/R2 gated (`r4_orm_alembic_parity`, `r4_migration_chain`, `r1_nutrition_resolution`, `r2_trusted_key_set`, `r1_stack_isolation`) | `DRHIRO_R1R2_ALEMBIC_DB=1 DRHIRO_R4_ALEMBIC_DB=1` | **49 passed** |

Full commands, environment/versions, and the migration failing-before/passing-after
output are in `evidence/review7_test_results.txt`.

## What the production observation does and does not show

Container **uptimes were unchanged** across the whole session (every `drhiro-*`
service at the same uptime before and after). That observation supports **"no observed
restart"**. It does **not** independently prove that configuration, credentials, or
data were unchanged: uptime says nothing about a `docker exec` edit inside a running
container, an out-of-band database write, a config reload, or a credential read. No
such change was made — but the uptime figure is not the evidence for that, and it is
not offered as such. The evidence for "no production change" is that every action in
this checkpoint targeted the disposable stack (`drhiro-iso`) or a disposable test
database, and production was consulted read-only.

## Attempt-fencing test coverage — precise scope

**What the fencing tests DO cover** (`TestAttemptFencing`, 2 tests), both of which
inject a delayed completion from an old attempt id via `stack_ctl late-result`:

  1. A late old result arriving **after an acknowledgement** (row is
     `resolved_acknowledged`; `current_attempt_id` is NULL).
  2. A late old result arriving **after the transition to `unknown`** (row is
     `unknown`; `current_attempt_id` is NULL).

In both, `_finish` returns `applied=False` and the terminal state is unchanged.

**What the fencing tests do NOT cover:** a late old result arriving **while a newer
attempt is actively in flight** — i.e. row state `in_flight` with
`current_attempt_id` set to a *different, newer* attempt id, with the delayed result
carrying the superseded id. That interleaving is **not exercised by any test**.

The implementation guards it by construction (`_finish` and the resend completion both
filter on `WHERE ... AND current_attempt_id = <the attempt that is completing>`, so a
superseded attempt matches no row), but "guarded by construction" is a code-reading
claim, not a verified one. **Untested; treat as a coverage gap.** This is stated rather
than papered over because a green suite must not be read as proving it.

Full suite runs are recorded in `evidence/review7_test_results.txt`.
