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

## B7. Disposable ingress omitted trusted-ingress behaviour

**Fix.** No behaviour change is claimed. The module docstring, the plan, and this
document now state **explicitly** that the following are OUT OF PROVEN SCOPE:
  - **Real polling-offset** — the slice acknowledges via the fake `/ack`, not a real
    `last_update_id` offset contract.
  - **Sender identity** — a verified bot identity authenticates the BOT, not the
    SENDER; every message maps to one fixed disposable user. Sender resolution is not
    implemented or proven.
  - **Edits / content conflicts** — an edited message sharing a `message_id` is
    treated as a duplicate receipt, not a revision; conflicts are not reconciled.

These are listed so the evidence is not over-read.

---

## Packaging vs implementation separation

The earlier oversized patch was largely previous review exports and patches embedded
in the tree. For this checkpoint the functional change set is kept **separate** from
packaging artifacts:

- **Implementation (functional code + tests):** `deploy/disposable/app/ingress.py`,
  `app/stack_ctl.py`, `app/fake_telegram.py`, `app/probe_isolation.py`,
  `deploy/disposable/sql/001_trusted.sql`, `deploy/disposable/docker-compose.isolated.yml`,
  `apps/api/src/drhiro_api/schema_activities.py`, and the two test files.
- **Packaging / documentation (not functional):** plan revisions, this review
  document, evidence captures, and any future archive index. These are listed
  separately in the git log and are not to be mistaken for new functional code.

A new candidate archive is **not** built at this checkpoint, per instruction ("submit
focused fixes and evidence before another candidate archive").

## Test state

| Suite | Result |
|---|---|
| `tests/test_r4_activities_migration.py` (gated) | **18 passed** (incl. 7 new negative) |
| `tests/test_t1_isolated_ingress_stack.py` (gated, disposable stack) | stack suite |
| default suite | see the evidence run |

Full suite runs are recorded in the evidence directory. Production services verified
untouched (uptimes unchanged) after testing.
