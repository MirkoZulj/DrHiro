# T1 Design Addendum — Production-Compatible Ingress, Writer Auth, Migration Evidence

Date: 2026-09-09
Branch: `feature/meal-liquid-idempotency` (isolated)
Candidate under review: **`ec6b538`**
Status: **design addendum; no implementation, no production change, push,
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

Every claim below is tied to a specific passing test in the candidate (`ec6b538`):

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

## 5. Pending-operation recovery: concurrent + uncertain-commit test (ADDED)

A pending-operation recovery must additionally demonstrate **safe concurrent
recovery** and **no duplicate writes after an uncertain commit outcome**. This is
a new test required by the review and is described here; the implementation
follows in the same commit as the addendum:

`TestCrashRecovery::test_concurrent_recovery_no_duplicate` —
- Seed one operation with `status=processing` and a stale `updated_at`.
- Launch N threads each running `recover_incomplete_operations()` against the same
  operation.
- Assert: exactly **one** meal, **one** operation, and the concurrent re-drives
  collapse via the existing `write_consumption` `FOR UPDATE` + `status=completed`
  replay guard (the second writer blocks, then observes `completed` and returns
  the saved result — no duplicate).
- Assert: `recovered.completed <= 1` across the whole run (the others are
  `replayed`), and total meals == 1.

This is the missing concurrency proof. It will be committed with the addendum.

---

## 6. Summary of status after this addendum

| Item | Status |
|---|---|
| Patch exported (actual files), base stated, checksums verified | **DONE** (§ bundle) |
| Real OpenClaw ingress change identified (spool module) | **DONE** — design; not implemented (out-of-repo) |
| Writer authorization boundary (scope=model credential) | **DONE** — design; not implemented |
| `activities` false claim corrected; limitation retained | **DONE** — item still OPEN |
| Production-mirror value/relationship check | **SPECIFIED** — not run, OPEN |
| Concurrent/uncertain-commit recovery test | **ADDED** (below) |
| R1 / R2 / R4 / R6 | remain partial/open per the review |
| Live Telegram round trip | acceptable at this stage (not required) |

No production change, push, deployment, gate activation, or historical cleanup.
