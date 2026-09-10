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

---

# REVISION 2 — approved corrections (supersedes conflicting text above)

Development approved: isolated branch + disposable stack only. The five corrections
below amend §1–§4. Where this revision conflicts with earlier text, **this revision
wins**.

## R1. Trusted ingress must be separated from model-accessible execution

**The boundary does not exist today — measured, not assumed.** The spool lives inside
the model-accessible container:

```
drhiro-openclaw-gateway-1 mounts:
  /var/lib/docker/volumes/drhiro_openclaw_state/_data -> /home/node/.openclaw  (rw)
```

The same container holds `TELEGRAM_BOT_TOKEN`, `DRHIRO_TELEGRAM_BOT_TOKEN` and
`DRHIRO_JWT_SECRET`. Therefore the earlier §1.2 proposal — *mint inside the spool
module* — is **withdrawn**: HMAC signed by a key the untrusted runtime can read
establishes nothing, and a spool record written by model-accessible code is not a
trusted record.

**Revised design (isolation, not cryptography):**

1. A **trusted ingress container** (`drhiro-ingress`) is the **only** component with
   the bot token and the **only** holder of the spool volume; it performs `getUpdates`
   (single consumer), `getMe` (verified bot id), spool write, claim, resolve, persist,
   reply.
2. The **OpenClaw gateway keeps neither** the bot token nor the spool mount nor any
   Telegram credential. It becomes a pure conversational engine that receives an
   authenticated turn over an internal channel and returns text. Model-accessible
   code therefore has nothing to read and no trusted record to modify.
3. **Key material placement:** with HMAC, the ingress signing/verifying secret is
   present only in `drhiro-ingress` and `drhiro-api` — never in the gateway or MCP.
   With asymmetric signing, the **private key exists only in `drhiro-ingress`** and
   verifiers hold the **public key only** (verifiers must never hold signing
   capability). The plan adopts **Ed25519 asymmetric** as the target so that the API
   verifies without holding a signing-capable secret.
4. **Enforcement is a deployment assertion, not a convention:** a test that inspects
   the running containers' environments/mounts and fails if the bot token, spool
   mount, or private signing key appears in a model-accessible container.

If the isolation cannot be built, the honest fallback is stated plainly: the trusted
path cannot be claimed, and consumption writes stay closed.

## R2. Credential removal is a rotation cutover, not an env-var deletion

Deleting `DRHIRO_JWT_SECRET` invalidates nothing. The separately-approved cutover must
cover: (a) generate a new signing key; (b) **reject** anything signed with the retired
key (key-id / `kid` in the header, old kid denylisted — no dual-accept window);
(c) reissue credentials for legitimate callers (web, Android bridge, manual tooling);
(d) remove the old key from model-accessible env, **mounts, config files, and
tooling**; (e) verify by inventory that it is gone.
Tests: token signed with retired key → rejected; wrong scope/audience → rejected; a
valid-key token's *minting origin* is **not** claimed to be distinguishable — the API
cannot tell who minted a token under its current trusted key, and the plan says so
explicitly.
Secret handling: no secret values in logs, tests, or review artifacts. R2 is
remediation of an **exposure**; presence is not evidence of abuse.

## R3. Reply delivery — corrected guarantee (no exactly-once reply claim)

A transactional outbox durably records intent alongside the consumption commit; it
**cannot** guarantee exactly one Telegram reply. Verified spool semantics:
`completeTelegramSpooledUpdateWithRetry` requires `claim.claimToken` (else
`TelegramSpooledUpdateCompletionOwnershipError`), lease
`TELEGRAM_SPOOLED_UPDATE_CLAIM_LEASE_MS = 1800s`, stale recovery default `staleMs =
6h`, `TELEGRAM_SPOOLED_UPDATE_PROCESS_ID = <pid>:<uuid>`, reply fence lane =
`accountId\0sequentialKey`, completed/failed TTL 720h (max 1000 entries). Completion
means **"turn handled"**, not "reply delivered".

Documented semantics (superseding any stronger claim):

- **Consumption effects:** exactly once (operation key + `FOR UPDATE` + completed
  replay). This is the guarantee we actually hold.
- **Reply delivery:** we claim exactly what is true — **durable reply intent**,
  **retry of known-safe failures**, and **explicitly unresolved ambiguous sends**.
  Successful delivery is NOT guaranteed. A send whose outcome is unknown is neither
  assumed delivered nor blindly retried.

State machine (persisted alongside the consumption commit, i.e. transactional intent):

```
reply_state:
  pending   intent durably recorded, no send attempted   -> safe to send
  sent      a response was received AND durably stored   -> terminal, do not resend
  failed    a send failed with a KNOWN-SAFE failure       -> safe to retry
            (connection refused, DNS failure, 429, 5xx before acceptance,
             request never left the process)
  unknown   a send was ATTEMPTED and its outcome was not recorded
                                                          -> never auto-resent
```

- **Known-safe retry** applies only to `failed`. Retrying a request that provably
  never reached Telegram cannot duplicate a reply.
- **`unknown` is the honest state for an ambiguous send.** Exactly one window
  produces it: the request was written to the socket (or handed to the transport),
  and the worker died — or lost its lease — before the response was recorded and
  committed. The worker cannot distinguish "Telegram never got it" from "Telegram
  delivered it and I lost the confirmation". Recording `sent` would be a guess;
  retrying would risk a duplicate; so the state is recorded as `unknown` and left
  for resolution. **A crash mid-send therefore enters `unknown`, not `pending`**:
  the intent row is written and the send attempt is marked as in-flight *before* the
  network call, so recovery sees an in-flight attempt with no recorded outcome.
  A crash *before* the send attempt leaves `pending` and is safely retryable —
  that asymmetry is the whole point, and it is asserted by test.
- **Resolution of `unknown`** — two supported paths, neither automatic:
  1. **Operator resolution.** An operator inspects the operation, the reply body and
     the transport evidence, then either marks it `sent` (accepted as delivered) or
     `failed` (accepted as lost, enabling a manual resend). Recorded with actor,
     timestamp and reason.
  2. **Authenticated user resolution.** The user can query the operation and is told
     plainly that a reply may not have arrived. An explicit user action ("resend")
     is an authorised decision to send again and therefore an accepted duplicate
     risk; it moves `unknown -> failed -> send`, never a silent auto-retry.
  Until one of those happens the operation is surfaced as unresolved — never
  silently retried, never silently assumed sent.
- Explicitly **not** claimed: exactly-once replies, or guaranteed delivery. No claim
  rests on a stub, and `sent` means "a response was durably stored", not "the user
  saw it".

## R4. Migrations: validate existing state, don't skip it

Measured production `activities` (read-only `\d activities`):

```
id uuid NOT NULL DEFAULT gen_random_uuid()
user_id uuid NOT NULL            (idx_activities_user_date btree(user_id, activity_date))
activity_date date NOT NULL
title varchar(255) NOT NULL
description text NULL
calories_burned double precision NOT NULL   CHECK (calories_burned >= 0)
created_at timestamptz NOT NULL DEFAULT now()
updated_at timestamptz NOT NULL DEFAULT now()
PK activities_pkey(id) · FK activities_user_id_fkey -> users(id) ON DELETE CASCADE
```

This differs from the ORM declaration (which has no CHECK, no server defaults, and an
`index=True` on `user_id` that would name the index `ix_activities_user_id`). Adoption
therefore must **compare** columns, types, nullability, defaults, constraints and
indexes, then either **reconcile supported differences** or **fail with a precise
diagnostic** — never silently skip.
- **Ownership-aware reversal:** the migration must **not** auto-drop a pre-existing
  table. It records whether *it* created the table (e.g. an `alembic` marker/comment
  on the table). Downgrade drops only self-created tables; an adopted production
  table makes **downgrade unsupported** and it fails with that message.
- Tests: **fresh creation** (all columns/types/constraints/indexes as declared, incl.
  the CHECK and a deterministic index name) **and adoption** of the production-shaped
  table (validation passes; downgrade refuses).

## R5. Spool handoff contract (explicit)

- **Exclusive claim ownership:** one record has one claim token; completion requires
  it (`TelegramSpooledUpdateCompletionOwnershipError` otherwise). `ownerId =
  <pid>:<uuid>` identifies the claiming process.
- **Lease renewal:** 1800s lease; long turns must call `refreshTelegramSpooledUpdateClaim`
  or the claim expires mid-turn.
- **Stale-worker fencing:** `recoverStaleTelegramSpooledUpdateClaims(staleMs=6h)`
  reclaims abandoned claims; `isTelegramSpooledUpdateClaimOwnedByOtherLiveProcess`
  fences a zombie worker; a reclaimed record is re-processed and must converge via
  the consumption idempotency (not by re-writing).
- **When completion occurs:** after the turn's effects (consumption commit + reply
  attempt recorded) — completion is what stops re-delivery; crashing before it means
  the record is reclaimed after the lease and re-driven.
- **How ordinary OpenClaw processing is prevented from consuming the same record:**
  by R1 — OpenClaw has **no spool mount and no bot token**, so it cannot poll, claim,
  or complete. This is the structural answer; the claim protocol is defence in depth
  among the trusted ingress's own workers.
- **Operation binding is never model-controlled:** the write path is bound server-side
  to the authenticated ingress credential. Forwarding `operation_id` to the model is
  **non-authoritative**: it is an opaque reference; any model-supplied value is
  accepted only as a lookup that must **match** the server-bound operation, and a
  mismatch is rejected. This resolves the earlier "no identity in the turn" wording:
  no *authoritative* identity travels to the model; a non-authoritative correlation
  handle may.

## Revised sequencing

1. **R1 container split + key placement** (with the deployment assertion test).
2. **R2 rotation cutover** (separately approved).
3. **R4 `activities` migration** (fresh + adoption; independent, small).
4. **R3 delivery state** (`pending|sent|unknown|failed`) + migration.
5. **R5 trusted claim worker** against the isolated spool.
6. Gap tests in §4.1.

Tests are labelled EXECUTED or PENDING throughout; R1–R5 remain PROPOSALS until
implemented and tested on the disposable stack.
