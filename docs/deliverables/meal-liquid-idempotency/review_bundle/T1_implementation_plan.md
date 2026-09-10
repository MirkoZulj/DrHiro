# T1 Implementation Plan — Remaining Blockers (PRE-IMPLEMENTATION)

Date: 2026-09-09
Branch: `feature/meal-liquid-idempotency`
**Frozen candidate: `7e2cf6915dbd7478e8a558817d4d51aa63879e60`** — unchanged by this
document. This is a plan, not an implementation. No repackaging.
Status: **proposal scope + acceptance tests. No production change, push,
deployment, gate activation, or historical cleanup.**

This document returns the proposed change scope and acceptance tests **before**
implementation, and separates:
- **EXECUTED** — tests already run and passing (candidate or earlier commits).
- **PENDING** — designs and tests not yet written or run.

---

## 0. Evidence base (read-only, production)

Inspected `drhiro-openclaw-gateway-1`, `drhiro-mcp`, `drhiro-api-1`,
`drhiro-postgres-1` read-only. No writes, no restarts, no config changes.

### 0.1 Two decisive findings that change the earlier design

**FINDING A — the model runtime holds the JWT *signing* secret.**
Container environment (keys and value lengths only; values not read):

```
drhiro-mcp:                DRHIRO_JWT_SECRET (96)   DRHIRO_SERVICE_TOKEN (179)
                           DRHIRO_TELEGRAM_ID (9)   DRHIRO_API_URL (29)
drhiro-openclaw-gateway-1: DRHIRO_JWT_SECRET (96)   DRHIRO_OPENCLAW_SERVICE_TOKEN (179)
                           TELEGRAM_BOT_TOKEN (46)  DRHIRO_TELEGRAM_BOT_TOKEN (46)
```

`DRHIRO_JWT_SECRET` in the model runtime means the model runtime can **mint an
arbitrary user JWT**, not merely replay one. Consequences for the earlier addendum:
- The proposed `scope=model` credential is **unenforceable as written**: a holder of
  the signing key can mint `scope=user` and bypass the scope check. Adding a
  narrower credential does **not** close the boundary while the broader key remains.
- Envelope HMAC verification is only meaningful if the verifying key is **absent
  from the model runtime**. The ingress secret is a separate setting
  (`telegram_ingress_secret`, distinct from `jwt_secret`) — this separation must be
  preserved and enforced in deployment, not just in config defaults.
- `DRHIRO_TELEGRAM_ID` (hard-coded, 9 chars) in the MCP is a user-identity constant
  in the model runtime — identity must be transport-derived, never a constant.

**FINDING B — the trusted worker has no reply/delivery record.**
`services/telegram_ingress.py` returns a result dict (`status`, `operation_id`,
`result`) and persists operation state, but there is **no persisted reply/delivery
state and no outbox**. "Committed but the reply never arrived" is therefore
**unmodelled**, and recovery from a crash between DB-commit and reply-delivery is
currently untestable. This is a required design addition, not a test-only gap.

These two findings supersede parts of `T1_design_addendum.md` §1/§2 as written; the
addendum's proposal framing remains correct, but its `scope=model` proposal is
**insufficient without key removal** (§2 below).

---

## 1. Production OpenClaw ingress — the complete path

### 1.1 The verified current path (read-only, from the deployed build)

```
grammY runner: monitor-polling.runtime-B0O0lKow.js
  │  imports from telegram-ingress-spool-Dd3cDhXe.js:
  │    p createTelegramBot, f writeTelegramSpooledUpdate,
  │    n claimNextTelegramSpooledUpdate, x runWithTelegramSpooledReplayUpdate,
  │    c recoverStaleTelegramSpooledUpdateClaims, r completeTelegramSpooledUpdateWithRetry,
  │    v buildTelegramReplyFenceLaneKey, u releaseTelegramSpooledUpdateClaim
  │
  1. single getUpdates consumer; bot built by createTelegramBot(token)
  │     → getMe gives the VERIFIED bot identity (the only such place)
  2. writeTelegramSpooledUpdate(update)  → durable spool
  3. claimNextTelegramSpooledUpdate({ownerId: TELEGRAM_SPOOLED_UPDATE_PROCESS_ID})
  4. runWithTelegramSpooledReplayUpdate(update, …)   ← raw update in scope
  5. turn built → OpenClaw model path → tf-shim:3200 → TrueForge → MCP → API
```

**Where identity dies:** between 4 and 5 — `buildOpenAICompletionsParams()` sets no
`user`/`metadata`, and plugin context exposes only an account label, not the verified
`getMe.id`. That is why the minting point must be 2–4 (inside the spool module), not
a plugin hook.

### 1.2 Proposed change scope (PROPOSAL — not implemented)

Signing an envelope alone does **not** complete the path. The missing pieces are
*transport* and *ownership*, not cryptography:

- **Mint at spool-write (step 2).** Extend `writeTelegramSpooledUpdate` (or wrap it
  via a trusted adapter) to attach a signed envelope sidecar:
  `{v:1, service, bot_id (verified), chat_id, message_id, update_id, content_digest,
  kind: created|edited}`, HMAC with `telegram_ingress_secret`.
- **Trusted claim path (steps 3–4).** The **T1 worker becomes the primary consumer**
  of the same spool: it claims the same record through the same
  `claimNextTelegramSpooledUpdate` / lease API (no second poller, no webhook), derives
  the operation key from the transport identity, runs resolution + persistence, and
  produces the reply. Only if the deterministic path yields `no_consumption` is the
  conversational model consulted.
- **The model turn carries no identity.** The turn forwarded onward contains only
  `operation_id` + user text; the envelope is extracted and removed before the model
  sees anything, and is never persisted in conversational history or prompt logs.
- **Reply returns through the spool's own completion API**
  (`completeTelegramSpooledUpdateWithRetry`, fenced by
  `buildTelegramReplyFenceLaneKey`) so the reply is bound to the same claim/lane.
- **Upgrade survival (required, not optional).** Because the module filename is
  build-hashed (`-Dd3cDhXe`), patch via a **wrapper** that extends the spool's public
  exports at load time, guarded by a **version/module-hash whitelist**; on an
  unsupported version the trusted path is **disabled (fail closed)** and the
  model-writer gate stays closed. Re-verify the spool export surface on every
  OpenClaw upgrade before enabling.

### 1.3 Acceptance tests

**PENDING (to be written, on an isolated local stack — never production):**
- Full-path test: real spool directory + real claim/lease + T1 worker → exactly one
  consumption; operation key derived from `(bot_id, chat_id, message_id)`; **assert
  the forwarded model turn contains no envelope and no identity**.
- Fail-closed: missing / duplicate / conflicting envelope; envelope copied from
  another event; envelope quoted in user text.
- Lease expiry + concurrent claim (`recoverStaleTelegramSpooledUpdateClaims`) → no
  double consumption; replay after claim loss → same operation.
- Version gate: unknown/unsupported spool module hash → trusted path disabled.
- Thread/topic semantics: assert the actual `chat_id` semantics used by the spool
  (do not assume `threadId` is the chat id).

**EXECUTED (candidate, not live ingress — fixtures only):**
`test_t1_bridge_wiring.py` (10, bridge-side extraction + `getMe` mismatch refusal),
`test_t1_ingress_endpoint.py` (10, real ASGI app: 401/503 fail-closed, signed event
persists, HTTP redelivery replays), signature/forgery/cross-account tests in
`test_t1_trusted_ingress_vertical_slice.py`. **These do not exercise the OpenClaw
spool**; they validate downstream handling of a fixture-supplied envelope.

---

## 2. Writer authorization — enforceable boundary + credential removal

### 2.1 Proposed change scope (PROPOSAL — not implemented)

Ordered; (1) is a precondition for (2) and (3) to mean anything:

1. **Remove `DRHIRO_JWT_SECRET` from the model runtime** (`drhiro-mcp` and
   `drhiro-openclaw-gateway-1`). The MCP must not be able to sign. Where the API needs
   to verify user JWTs, the API alone holds the secret (or move to asymmetric keys:
   trusted components hold only the public key). **This is the actual boundary** —
   scope claims are theatre while the signing key is in the model runtime.
2. **Distinct model credential** minted by a trusted component (not the MCP), with an
   audience/scope claim and a **server-resolved** `user_id`; verified by the API
   against a key the model runtime does not hold. Remove the hard-coded
   `DRHIRO_TELEGRAM_ID` identity constant.
3. **Deny consumption writes from any `scope=model`/service caller** when the trusted
   path is active (`403 model_writer_disabled`), on every consumption-writing route.
   A **real user JWT** keeps manual/non-Telegram logging with an explicit
   `idempotency_key` contract.
4. **Bound the model runtime's egress** to the intended MCP/API surface so a broader
   credential cannot be used against other routes.
5. **Retain MCP blocking** (`DRHIRO_TRUSTED_INGRESS_WRITERS_DISABLED`) as an
   independent second control.

### 2.2 Acceptance tests

**PENDING:**
- The model runtime presenting **its own minted `scope=user` JWT** → rejected on every
  consumption route (this is the test the earlier suite is missing).
- The old user JWT (pre-removal) → rejected after rotation.
- `scope=model` credential → `403 model_writer_disabled` when the gate is active;
  → permitted for non-consumption reads.
- A real user JWT → manual logging allowed, `idempotency_key` honoured.
- Credential inventory check: assert `DRHIRO_JWT_SECRET` is **absent** from the model
  runtime's environment (a deployment assertion, not just a unit test).
- MCP flag set → consumption tools disabled.

**EXECUTED (candidate):**
`test_t1_writer_gate_routes.py` (27: route-by-route + OFF→ON transitions with valid
auth), `test_t1_mcp_writer_gate.py` (10: real MCP subprocess, `model_writer_disabled`).
**Caveat: these assume the model runtime cannot mint credentials — which FINDING A
shows it currently can.** They remain valid as regression coverage, not as proof of
the boundary.

---

## 3. Migrations

### 3.1 `activities` — proposed migration

ORM (`models.py:91`) declares `activities`: `id` uuid PK, `user_id` uuid
FK→`users.id` ON DELETE CASCADE (indexed), `activity_date` DATE not-null, `title`
VARCHAR(255) not-null, `description` TEXT null, `calories_burned` FLOAT not-null, plus
`created_at`/`updated_at`. **No migration creates it.** The baseline docstring
(`b2f3c4d5e6f7`) claims *"It is provided by a separate migration that follows
3c003"* — **that migration does not exist** (the false claim, corrected).

Chain (verified): `<base> → b2f3c4d5e6f7 → 3c00321778bc → c4d8e2f6a9b1 →
d5e6f7a8b9c0 → e7f8a9b0c1d2 → f1a2b3c4d5e6 → a1b2c3d4e5f7 → c9d0e1f2a3b4 →
9a1b2c3d4e5f (head)`. **Production sits at `d5e6f7a8b9c0`.**

Proposed scope:
- New migration at **head** (`down_revision = '9a1b2c3d4e5f'`). It must **not** be
  inserted after `3c003` as the docstring suggests: production has already applied
  up to `d5e6f7a8b9c0`, and inserting mid-chain rewrites applied history.
- **Guarded creation.** Production already has `activities` (created out-of-band,
  confirmed read-only), so a bare `op.create_table` would fail there. The migration
  must inspect for the table and skip creation when present (idempotent), creating it
  only on a database that lacks it.
- Correct the baseline docstring's false claim as part of the change.
- Downgrade drops the table — **destructive; not a lossless rollback** (already
  qualified).

### 3.2 Value/relationship preservation (isolated representative DB)

**PENDING** — run on an isolated, disposable, **Alembic-built** database (never
production, never the shared test DB, never `create_all`):
- Start at a supported pre-feature revision; seed **linked synthetic** records:
  `users → meals → meal_items → measurements` + `beverage_measurements` link +
  `food_catalog_items`/`foods` references.
- `alembic upgrade head`; then assert **field-by-field**, not row counts:
  - values preserved: `Meal.totals_json`, `MealItem.nutrients_json`,
    `Measurement.value_json`;
  - relationships preserved: `beverage_measurements` links, FK integrity,
    `source_operation_id` links, CASCADE behaviour.
- Separately assert the upgrade is **additive** (the chain's only destructive
  operations are in `downgrade()` — verified for `9a1b2c3d4e5f` and the chain).
- Explicitly document which tables the fresh-bootstrap claim covers (27 chain-owned)
  and that `activities` is excluded until §3.1 lands.

### 3.3 Acceptance tests

**PENDING:** fresh-Alembic-DB column/FK/index parity for `activities` vs ORM metadata;
production-mirror upgrade at `d5e6f7a8b9c0` (with `activities` present) succeeds as a
no-op; value/relationship preservation suite above.

**EXECUTED:** `test_r4_orm_alembic_parity.py` (8) + `test_r4_migration_chain.py` (6) +
`test_r1_nutrition_resolution.py` (3) = 17, gated on an Alembic-built DB. These do
**not** cover `activities` (which the parity suite currently asserts as absent) and do
**not** cover value preservation.

---

## 4. Recovery and confirmations — coverage gaps

### 4.1 Identified gaps

**(a) Concurrent edits** — only sequential coverage exists
(`test_edit_is_a_revision_not_a_second_consumption`). Missing: two concurrent edits of
one message; an edit arriving while the original is still `processing`; edit with
wrong ownership (other user/bot); edit after the operation completed; edit racing a
confirmation callback.

**(b) Callback state transitions** — only
`test_callback_confirm_replays_and_checks_ownership` and unknown-op-ignored. Missing:
confirm→cancel→confirm; callback on an already-completed operation (must replay, not
re-write); callback on a revisioned operation; **concurrent duplicate callbacks**
(exactly one confirm); callback after crash/recovery; callback for an operation in
`needs_clarification`.

**(c) Crashes around DB commit and reply delivery** — **blocked by FINDING B**: there
is no persisted reply/delivery state, so this cannot be tested. Missing once the state
exists: crash **after commit, before reply** (recovery must re-send the reply and must
**not** re-write the consumption); crash **after reply sent, before completion mark**
(must not double-reply); crash **during** recovery; recovery run repeatedly
(idempotent).

### 4.2 Proposed change scope (PROPOSAL — not implemented)

- Add a durable **delivery state** for the reply — either columns on
  `consumption_operations` (`reply_state ∈ pending|sent|failed`, `reply_payload`) or a
  small outbox table — plus its migration (same chain-head + guarded rules as §3.1).
- Extend the trusted worker to record **intent to send before send** and
  **completion after send**, and extend `recover_incomplete_operations()` to re-drive
  `pending` replies idempotently without touching completed consumptions.
- Extend the recovery selector to the new states (`pending_reply`) with the same
  stale-cutoff + `FOR UPDATE` + `populate_existing` serialization pattern.

### 4.3 Acceptance tests

**PENDING:** each gap in 4.1(a)–(c), on real PostgreSQL, asserting **exactly one
consumption and exactly one reply per event**; plus recovery idempotence under
repeated runs.

**EXECUTED (candidate):**
- `TestCrashRecovery::test_crash_between_receipt_and_completion_is_recovered`
- `TestCrashRecovery::test_recovery_does_not_touch_completed_or_clarification`
- `TestCrashRecovery::test_concurrent_recovery_no_duplicate_after_uncertain_commit`
  (6 racing workers, own sessions; serialization by `SELECT … FOR UPDATE` +
  `populate_existing()`; DB converges to one meal/op/item)
- `TestRestartAndRedisLoss::test_complete_redis_loss_replays_from_postgres`
- Callback/edit basics listed in §4.1.

DB/connection setup for the executed recovery tests: PostgreSQL
`drhiro_t1_alembic` (Alembic-built, `DRHIRO_T1_ALEMBIC_DB=1`), Redis
`redis://localhost:6382/15`.

---

## 5. Sequencing and gates

1. **§2.1(1) key removal** first — every other boundary is cosmetic until the model
   runtime cannot sign. Deployment change; requires explicit approval.
2. **§3.1 `activities` migration** (independent, small, unblocks the fresh-bootstrap
   claim).
3. **§4.2 delivery state + migration** (unblocks the crash/commit/reply tests).
4. **§1.2 OpenClaw ingress wrapper** (largest; needs the upgrade/version gate).
5. **§2.1(2)–(5) credential scoping + egress bound**, then re-run the route suite
   against a model runtime that can no longer mint.

Each step is independently reviewable; none is activated in production without
approval. Tests are stated per step above so that "EXECUTED" never stands in for
"PENDING".

---

## 6. Status of outstanding findings (all remain OPEN)

| Finding | Status |
|---|---|
| Production-compatible ingress (spool path + trusted claim) | **PROPOSAL** — architectural blocker |
| Enforceable caller boundary (key removal + scoped credential) | **PROPOSAL** — currently bypassable (FINDING A) |
| Reply-delivery durability | **NOT DESIGNED** — no delivery state exists (FINDING B) |
| `activities` migration | **PROPOSAL** — fresh bootstrap incomplete |
| Value/relationship preservation | **PENDING** — not run |
| Concurrent edits / callback transitions / commit-reply crashes | **GAPS IDENTIFIED** — tests pending |
| R1 partial · R2 open · R4/R6 outstanding evidence | **OPEN** |

Candidate `7e2cf69` remains frozen. No production change, push, deployment, gate
activation, or historical cleanup.
