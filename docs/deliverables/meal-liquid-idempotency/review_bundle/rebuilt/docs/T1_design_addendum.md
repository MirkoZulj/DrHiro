# T1 Design Addendum — Production-Compatible Ingress, Writer Auth, Migration Evidence

Date: 2026-09-09 (addendum revision: packaging corrected)
Branch: `feature/meal-liquid-idempotency` (isolated)
Release candidate under review: **`7e2cf6915dbd7478e8a558817d4d51aa63879e60`**
Superseded checkpoint (NOT the candidate): **`ec6b538`**
T1 slice base: `dfc976e760554837ff71bfa63756507c73956e8b`
Full remediation base (merge-base with main): `40ff524ee820ea5507e06fca36890ca917381608`
Status: **design addendum; the OpenClaw ingress patch and scoped-model credentials
are PROPOSALS, NOT implemented. No implementation, no production change, push,
deployment, gate activation, or historical cleanup.**

This addendum answers the three review items. It supersedes the earlier bridge-as-
minting-site framing: the reconciliation (committed) showed production uses
OpenClaw's own Telegram channel, so the minting site must be there.

---

## 1. The real OpenClaw ingress change

### Where the raw identity actually is

Production's Telegram ingress is **`telegram-ingress-spool-Dd3cDhXe.js`** in the
OpenClaw `dist` (the same module the earlier R2 trace inspected). Verified
read-only against `drhiro-openclaw-gateway-1`:

- It calls `getUpdates` (the single polling consumer).
- It owns the **durable spool**: `writeTelegramSpooledUpdate`,
  `claimNextTelegramSpooledUpdate` (with `TELEGRAM_SPOOLED_UPDATE_PROCESS_ID`
  ownership + leases), `runWithTelegramSpooledReplayUpdate`,
  `completeTelegramSpooledUpdateWithRetry`, `failTelegramSpooledUpdateClaim`.
- It owns **`createTelegramBot`** — the only place with the **verified** bot
  identity (getMe), because it holds the bot token and deps.

### Why plugin hooks are insufficient (reconciled)

Earlier finding: plugin context exposes only the `accountId` **label**, not the
verified `getMe.id`, and OpenClaw drops identity before the shim
(`buildOpenAICompletionsParams` sets no `user`/`metadata`). This is **not
contradicted** — it stands. It is precisely why a plugin hook cannot mint the
envelope: a plugin lacks the verified bot id and cannot see the raw update at the
poll boundary.

### The exact change (production-compatible)

The minting component must be a **small, additive patch to
`telegram-ingress-spool-Dd3cDhXe.js`**, not a plugin hook. At the point where an
update is spooled (`writeTelegramSpooledUpdate`) the module already has, in one
place:

- the raw update dict: `conversationId` (`msg.chat.id`), `messageId`
  (`msg.message_id`), `update_id`;
- the verified bot id from `createTelegramBot` (getMe).

So the change is: **when spooling an update, extract those four fields and sign
them** (HMAC with the shared `TELEGRAM_INGRESS_SECRET`) into an envelope carried
**with the spooled update** (a sidecar field), and forward that envelope to the
trusted worker instead of (or before) the model path. Because the spool is
durable and single-consumer, this:

- **retains the single existing consumer** (the same process still owns
  `getUpdates` and the spool; no second poller, no webhook);
- **retains event ownership** (the envelope is bound at spool-write, before any
  model or shim sees it);
- **binds events to trusted processing** (the API ingress worker verifies the
  signature and fails closed, exactly as the vertical slice proves);
- **survives restarts and replay** (the spool's durable write + replay path
  carries the envelope, so a replayed update re-delivers the SAME signed event —
  the same operation, not a new one).

### Concrete seam

```
getUpdates ──▶ telegram-ingress-spool-Dd3cDhXe.js
                │  at writeTelegramSpooledUpdate(update):
                │    botId   = createTelegramBot(...).me.id      (verified)
                │    chatId  = update.message.chat.id            (conversationId)
                │    msgId   = update.message.message_id
                │    updateId= update.update_id
                │    env = sign(service, botId, chatId, msgId, digest, kind)
                │    spool(update + env_sidecar)                 (durable)
                ▼
              claimNextTelegramSpooledUpdate(ownerId=PROCESS_ID)
                │  runWithTelegramSpooledReplayUpdate(update+env)
                ▼
              trusted worker (signature verified, fail closed)
                ▼
              model (only if no_consumption) / write path
```

This is the **architectural blocker closure** the review named: a production-
compatible ingress seam now exists (the spool module), and the minting site is
concrete. It is **not implemented** — it is a design change to OpenClaw's runtime
module, which is out of the repo's patch scope and needs a separately-gated step.
The repo-side bridge remains a valid prototype of the trusted worker semantics.

---

## 2. Writer authorization — model-originated writes at the API

### Inventory of every route + credential the model runtime can reach

The model runtime (TrueForge via the shim → OpenClaw → MCP `drhiro-mcp` → drHiro
API) holds:

| Surface | Route(s) | Credential the MCP presents |
|---|---|---|
| `openclaw_tools` | `/tools/*` (incl. `create_meal_from_text`, `update_meal_item`, `confirm_meal`) | `x-service-token` (signed service JWT, `service=openclaw`) + `x-telegram-id` |
| `meals` | `/meals`, `/meals/from-text`, `/meals/{id}/*`, `/meals/{id}/confirm`, etc. | `Authorization: Bearer <user JWT>` (the MCP's `DRHIRO_MCP_TOKEN`) |
| `ingest` | `/ingest/manual/water|liquid|text`, `/ingest/manual/weight|bp` | `Authorization: Bearer <user JWT>` |
| `auth`, `dashboard`, `reminders`, `datapoints` | reads + some writes | `Bearer <user JWT>` |

### The problem (verified)

The model reaches `/meals`, `/ingest`, and most of `/tools` with an **ordinary
user JWT** (`DRHIRO_MCP_TOKEN`) or, on `/tools`, a **service token that the API
must trust without a per-call user link**. An API layer cannot reliably tell "a
model holding the user's token" from "the user holding their token". This is why
the checkpoint correctly moved the decisive control to the MCP tool layer — but
the review asks for an API-side enforceable boundary too.

### Proposal: separate caller credentials/scopes

The enforceable boundary is a **distinct model-issued credential**, not an
unsigned header:

1. **A dedicated `scope=model` service credential.** Replace the user JWT the MCP
   holds (`DRHIRO_MCP_TOKEN`) with a short-lived, signed credential that encodes
   `service=model` + the resolved `user_id` (server-resolved from the trusted
   ingress, never from the model). The API validates:
   - `scope == "model"` → the caller is the model acting for that user;
   - the writer gate then **denies consumption writes** when the trusted path is
     active (`403 model_writer_disabled`), regardless of route;
   - a plain user JWT (`scope == "user"`) is a real user and is **not** blocked.
   This makes model-vs-user distinguishable **at the API** by **credential scope,
   not by provenance inference**.

2. **`/tools` service token tightened.** The `x-service-token` must carry
   `scope=model` and be verified against `jwt_secret` with an explicit scope check
   (currently `validate_service_token` checks only `type=service` +
   `service=openclaw`). Add a scope claim so `/tools` consumption writers can be
   scoped-gated like the rest.

3. **MCP blocking retained as an additional control** (defense-in-depth): keep
   `DRHIRO_TRUSTED_INGRESS_WRITERS_DISABLED` disabling the model's consumption
   tools. Even if the API credential were somehow bypassed, the model cannot call
   `log_meal`/`log_water` etc.

4. **Explicit idempotency contract for legitimate manual callers.** The web/Android
   bridge and manual tools keep `idempotency_key` semantics on the user-JWT
   surface — never model-chosen identity.

This is a **design**, not yet implemented: it changes how the MCP authenticates
and how the API scopes service tokens. It is the enforceable boundary the review
asks for; MCP blocking stays as a second, independent control.

### 2a. The two proposals are LABELLED as proposals — and what implementation must satisfy

Both the OpenClaw ingress patch (§1) and the scoped-model credential (§2) are
**proposals, not implemented**. Before implementation, three open design points
must be resolved (all documented here; none is satisfied by a signed envelope
alone):

**A. How the spooled identity reaches the trusted T1 worker — not merely how an
envelope is signed.**
Signing the envelope at `writeTelegramSpooledUpdate` is necessary but not
sufficient. The open requirement is the *transport* of that signed identity from
the OpenClaw process to the trusted worker without the model choosing or
altering it. Design options, in preference order:
1. **Sidecar on the spooled update, consumed by a dedicated trusted sink.** The
   patched spool module writes the envelope as a field on the spooled record;
   a trusted process (the T1 ingress worker) claims that record directly from
   the spool (`claimNextTelegramSpooledUpdate`), verifies the signature, and only
   then forwards an already-bound event to the model. The model never sees the
   raw identity; it sees only a verified `operation_id`.
2. **Separate durable queue with an HMAC-bound reference.** The spool writes the
   envelope to a PostgreSQL/Redis queue keyed by `(bot_id, chat_id, msg_id)`; the
   T1 worker polls that queue. This decouples from OpenClaw internals but adds a
   second durable store.
Both options require that the worker and the spool share the HMAC secret and the
same canonical field encoding (versioned `v1` serialization, not delimiter
concatenation). The decisive property to test: **a model cannot forge, copy, or
re-order the identity** — it arrives bound to the operation, and the operation
key is derived from transport identity, never from model text.

**B. How the OpenClaw modification survives upgrades and fails safely on an
unsupported version.**
Because the change is to OpenClaw's *runtime module* (`telegram-ingress-spool-*`,
whose filename is hashed per build), a naive patch breaks on upgrade. Required:
- **Version detection + fail-closed.** Before patching the spool, detect the
  deployed OpenClaw version (or the exact module hash) against a whitelist. On an
  unsupported version, **do not enable the trusted path** — the ingress falls back
  to the current (non-minting) behaviour and the model-writer gate stays closed so
  no consumption write bypasses identity.
- **Patch via a wrapper, not in-place.** Prefer a separate trusted adapter that
  wraps/extends the spool's public exports at load time, so a rebuild of the
  module surfaces a version mismatch instead of silently breaking.
- **Upgrade regression gate.** Re-verify the spool exports (`writeTelegramSpooledUpdate`,
  `claimNextTelegramSpooledUpdate`, `runWithTelegramSpooledReplayUpdate`,
  `createTelegramBot`) on every OpenClaw upgrade; the acceptance suite must run
  against the upgraded runtime before the trusted path is enabled.
This is not yet designed to implementation depth — it is the upgrade-survival
requirement.

**C. How existing user JWTs are removed from the model runtime.**
Adding a restricted `scope=model` credential is **insufficient on its own**: while
the broader user JWT (`DRHIRO_MCP_TOKEN`) remains in the model's environment, the
model can bypass the restricted credential by simply using the broader one. The
implementation must therefore **remove or revoke the broader credential from the
model runtime**, not merely add a narrower one. Required:
- **Rotate/revoke** the user JWT the MCP currently holds (`DRHIRO_MCP_TOKEN`) so it
  no longer authorizes the model.
- **Bound the model's network egress** so it can reach only the intended
  MCP/API endpoints (no arbitrary calls to `/meals`, `/ingest`, `/auth` with any
  user credential).
- **Verify in the MCP** that the presented credential carries `scope=model` and a
  server-resolved `user_id`, rejecting anything broader.
The review's objection — "adding a restricted credential does not prevent bypass
while broader credentials remain accessible" — is correct and is a hard gate for
implementation: the broader credential must be gone, and that must be tested (an
attempt to present the old user JWT to a consumption route must fail).

---

## 3. Remaining migration evidence

### 3.1 `activities` — correction of the false claim

**The prior doc's claim is corrected.** `R4_remaining_evidence.md` stated that the
food-domain baseline's docstring claims `activities` is "provided by a separate
migration that follows 3c003" — **that follow-on migration does not exist.** This
addendum makes the correction explicit and precise:

- `activities` is ORM-declared (models.py) but **not created by any Alembic
  migration**. Verified by diff: 28 ORM tables, 28 migration-created names, with
  **only `activities` missing**.
- Production already has `activities` (created out-of-band, confirmed read-only).
- **A fresh Alembic-built application bootstrap therefore lacks `activities`.**

**Decision (explicit retention of the limitation):** rather than assert a
supported migration path that does not exist, this is retained as an
**incomplete fresh-application bootstrap limitation**. The fresh-database claim
covers the 27 chain-owned tables and **excludes `activities`**. A follow-on
migration to create `activities` (with its FK to `users.id`, respecting that
`users` is created by `3c003`) is required before a fresh-bootstrap claim can be
complete. **This item remains OPEN.**

### 3.2 Production-mirror value/relationship preservation

The earlier mirror was schema-only. This addendum specifies the required check
(not yet run): build a disposable synthetic mirror at a supported pre-feature
revision, seed representative **linked** records (user → meals → items →
beverage `Measurement` + `beverage_measurements` link), run `alembic upgrade head`,
and assert:

- **values** preserved: `Meal.totals_json`, `MealItem.nutrients_json`,
  `Measurement.value_json.amount_ml` unchanged before/after;
- **relationships** preserved: `beverage_measurements` links,
  `consumption_operations`/`consumption_items` FKs, `source_operation_id` links;
- **not just row counts**: compare the resolved objects field-by-field, not counts.

This is specified but **not yet executed** — it is a dedicated, gated step. It
remains **OPEN** until run.

---

## 4. Claims pinned to their tests

Every claim below is tied to a specific passing test in the candidate
(`7e2cf69`; `ec6b538` is the superseded checkpoint):

- **Crash recovery without Telegram redelivery:**
  `test_t1_trusted_ingress_vertical_slice.py::TestCrashRecovery::test_crash_between_receipt_and_completion_is_recovered`
  (creates an operation with `status=processing`, no network update, fresh worker
  re-drives → one 813-kcal meal, one operation).
- **Redis-loss replay from Postgres:**
  `...::TestRestartAndRedisLoss::test_complete_redis_loss_replays_from_postgres`
  (`FLUSHALL`, `dbsize()==0`, fresh session → `replayed`, one meal).
- **Production ingress reconciliation:**
  read-only evidence in `T1_production_ingress_reconciliation.md`; the seam
  (spool module) verified in this addendum.
- **Writer gate route coverage:**
  `test_t1_writer_gate_routes.py` (27) + `test_t1_mcp_writer_gate.py` (10).
- **Clarification / no silent write:**
  `...::TestTrustBoundary::test_unsupported_input_clarifies_not_guesses`,
  `test_clarification_outcome_does_not_write`.

---

## 5. Pending-operation recovery: concurrent + uncertain-commit test (IMPLEMENTED + VERIFIED)

A pending-operation recovery must additionally demonstrate **safe concurrent
recovery** and **no duplicate writes after an uncertain commit outcome**. This was
the review-required addition; it is implemented and passing.

`TestCrashRecovery::test_concurrent_recovery_no_duplicate_after_uncertain_commit`
- Seed one operation with `status=processing` and a stale `updated_at`.
- Launch 6 threads, each running `recover_incomplete_operations()` against the
  same operation (stale_minutes=0).
- It exposed a REAL bug (fixed in the candidate): `write_consumption`'s `FOR
  UPDATE` re-read returned the stale identity-map object (`status=processing`),
  so concurrent workers inserted duplicate items. Fixed with
  `.with_for_update().populate_existing()`.
- Assert: exactly **one** meal, **one** operation, **one** item, and the
  operation's final `status == "completed"` — the DB-level no-duplicate proof.

**Exact database/connection setup for this test (reported as requested):**
- **Database:** PostgreSQL `postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_t1_alembic`
  — an **Alembic-built** database (suite requires `DRHIRO_T1_ALEMBIC_DB=1`), NOT
  a `create_all` DB. The suite asserts `alembic_version` is populated.
- **Redis:** `redis://localhost:6382/15` (env `REDIS_URL`), used for the
  durable-receipt acceleration only; recovery/replay reads PostgreSQL, so
  complete Redis loss does not affect correctness.
- **Concurrency model:** 6 threads, each with its **own** SQLAlchemy
  `SessionLocal()` (no shared session), racing `recover_incomplete_operations`
  on the same row. Serialization is by Postgres row lock
  (`SELECT ... FOR UPDATE`) + `populate_existing()`.
- **Result:** 25/25 T1 vertical-slice tests pass, including this one, in both
  the main repo and a clean reconstructed worktree.

---

## 6. Summary of status after this addendum

| Item | Status |
|---|---|
| Patch exported (actual files), base stated, checksums verified | **DONE** (§ bundle) |
| Real OpenClaw ingress change identified (spool module) | **DONE** — PROPOSAL; not implemented (out-of-repo) |
| Writer authorization boundary (scope=model credential) | **DONE** — PROPOSAL; not implemented (JWT removal + egress bound still required) |
| Spooled-identity reach / upgrade fail-safe / JWT removal | **DOCUMENTED** (2a) — implementation gates, not done |
| `activities` false claim corrected; limitation retained | **DONE** — item still OPEN |
| Production-mirror value/relationship check | **SPECIFIED** — not run, OPEN |
| Concurrent/uncertain-commit recovery test + fix | **DONE** — implemented, verified (worktree + main) |
| R1 / R2 / R4 / R6 | remain partial/open per the review |
| Live Telegram round trip | acceptable at this stage (not required) |
| Production-compatible ingress implemented | **NOT DONE** — architectural blocker |

No production change, push, deployment, gate activation, or historical cleanup.
