# R2 / T1 — Trusted Ingress Design and Writer-Ownership Plan

Date: 2026-09-09
Branch: `feature/meal-liquid-idempotency` (isolated)
Status: **design for review.** Development authorized; not a production cutover.
No production change, push, or deployment.

---

## 1. Where trusted identity actually enters

**The trusted ingress component already exists in this repository:
`services/telegram-bridge`.** It is not a new plugin and not a new gateway.

Verified facts:

| Requirement | Where telegram-bridge satisfies it |
|---|---|
| Receives the authentic Telegram event | `_poll_loop()` → `tg.get_updates(offset=…)`; the raw update dict is in hand |
| Verified bot identity | holds `TELEGRAM_BOT_TOKEN`; `POST getMe` returns the authoritative `id` |
| Authenticates the user | `_authorized(message, cfg)` before any processing |
| Retains event ownership | `update_id`, `message.message_id`, `message.chat.id`, `message.from.id` all present |
| Single owner of the bot | it is the **only** polling consumer; `install.sh` refuses to run polling while a webhook is set and warns *"Never set a Telegram webhook on this token."* |

### Decision: extend telegram-bridge. Do **not** add a channel plugin or a second gateway.

Rationale, and the answer to "whether this is a channel plugin or pre-OpenClaw
gateway":

- A **pre-OpenClaw gateway** exists already — telegram-bridge *is* it. It sits
  before OpenClaw/TrueForge and owns the raw update.
- An **OpenClaw channel plugin** cannot serve here: the plugin context does not
  expose a verified `getMe.id`, and OpenClaw drops identity before the shim
  (verified previously: `buildOpenAICompletionsParams` sets no `user`/`metadata`).
- Adding either a second polling consumer **or** a webhook receiver would violate
  the single-owner rule you set: two independent receivers must not own the same
  bot. Telegram forbids polling and webhook simultaneously, and two pollers would
  race and split updates.

So: **one owner (telegram-bridge), extended with a trusted ingestion worker.**

### Exact handoff

```
Telegram  ──getUpdates(offset)──▶  telegram-bridge  (SINGLE OWNER)
                                      │
                                      ├─ 1. authorize sender (existing _authorized)
                                      ├─ 2. derive canonical event key from
                                      │     (bot_id=getMe.id, chat_id, message_id)
                                      ├─ 3. DURABLE RECEIPT in Postgres
                                      │     consumption_operations (status=received)
                                      │     unique (user_id, bot, chat, message)
                                      ├─ 4. persist poll offset in Postgres
                                      ├─ 5. TRUSTED WORKER (owns the operation):
                                      │     · model call BOUND to operation_id;
                                      │       reply treated as UNTRUSTED PROPOSALS
                                      │     · validate proposals
                                      │     · nutrient resolution (server-side)
                                      │     · clarify when ambiguous
                                      ├─ 6. ATOMIC WRITE: meal + items + beverage
                                      │     link in one transaction, then
                                      │     status=completed + result_json
                                      └─ 7. reply to chat
                                            (reply failure does NOT roll back;
                                             result is durable → replay)
```

### Acknowledgement, durable receipt, retries, forwarding

- **Ack = advancing the poll offset.** Telegram considers an update delivered once
  the offset moves past it. Therefore the **durable receipt (step 3) and the
  persisted offset (step 4) are written before the offset advances.** A crash
  before step 3/4 means Telegram redelivers; a crash after step 3/4 means the
  operation already exists and redelivery **replays** it.
- **Retries/redelivery:** the unique constraint on
  `(user_id, source_bot_id, source_chat_id, source_message_id)` is the arbiter.
  A redelivered update finds its existing operation and returns its durable
  `result_json` — no new consumption. This is why identity, not text, is the key.
- **Forwarding to the conversational system:** the worker calls the model through
  the existing TrueForge surface, but the **binding is the operation the worker
  owns**, not a session the model chooses. The model's tool-call arguments are
  never the write path.

---

## 2. Identity separated from content

- **Event key:** versioned, canonical encoding of `(bot_id, chat_id, message_id)`
  → `v1|bot=<id>|chat=<id>|msg=<id>`, then hashed. Canonical and versioned so the
  representation is unambiguous and can be migrated deliberately.
- **Content digest stored separately** (`payload_hash`) for conflict detection:
  - same event + same digest → **replay** the existing operation/result;
  - same event + different digest → **reject as an unexplained conflict**, unless
    it arrives as an **authenticated edit** of the same message, in which case it
    is recorded as an explicit **revision** of the original operation. Never a
    silent second consumption.
- **Distinct messages with identical text → distinct events** (different
  `message_id`). Content is not part of identity, so identical text cannot collide.
- **Per-item identities** are stable within an operation:
  `item_key = f(op:<operation_id>:item:<discriminator>)`, where the discriminator
  is derived deterministically from the item's position/canonical descriptor, so
  retries and repeated tool calls converge on the same row
  (`uq_consumption_item_op_key`).

### Edits

Telegram edits arrive as `edited_message`. The edit is processed as a **revision
of the existing operation** identified by `(bot, chat, message_id)` — the same
message id — and recorded as such. It is never treated as a new consumption and
never as an ordinary retry.

---

## 3. Model assistance preserved, but untrusted

Deterministic parsing is **not** a precondition for trustworthy identity. The
model may propose quantities, food matches, and interpretations; the trusted
worker:

1. binds the model request and response to the `operation_id` it owns;
2. treats all model output as **untrusted data**;
3. validates it (quantities sane, candidates must exist, no invented ids);
4. performs **nutrient resolution server-side** (`resolve_item_nutrition`), never
   trusting model-supplied nutrition for the write;
5. requests **clarification** where ambiguity remains, via a draft bound to the
   operation.

Model output must never choose: the user, the event identity, operation
ownership, or authorization.

---

## 4. Writer-ownership plan (bypass elimination)

### Inventory of consumption-writing paths today

| Writer (MCP tool) | Legacy API endpoint | Action under T1 |
|---|---|---|
| `log_meal` | `POST /meals/from-text`, `POST /meals/from-text-intelligent/confirm` | **disable / redirect** |
| `log_meal_intelligent` | `POST /meals/from-text-intelligent` | **disable / redirect** |
| `confirm_intelligent_meal` | `POST /meals/from-text-intelligent/confirm` | **disable / redirect** |
| `log_liquid` | `POST /ingest/manual/water` | **disable / redirect** |
| `log_water` | `POST /ingest/manual/water` | **disable / redirect** |
| `log_recipe_meal`, `build_recipe` | meal/recipe writers | **disable / redirect** |
| `delete_meal` | `POST /meals/delete-by-fragment` | **redirect** (as a revision of the operation) |
| `learn_food`, `analyze_food_photo` | `POST /meals/learn` | review (catalog write, not consumption) |
| meal-item edits (`update_data_point` grams, `correct_meal_item`) | meal item writers | **redirect** |

### Ownership rules when T1 is activated

1. **The trusted worker is the only writer for Telegram-originated consumption.**
2. Legacy endpoints are **gated by authenticated caller identity, not by model
   arguments.** A model-originated call (no trusted context) is **rejected**, so
   the conversational model cannot independently log the same drink twice.
3. **Manual / non-Telegram logging remains supported** through authenticated
   callers that supply an **explicit idempotency contract** — a caller-minted
   `idempotency_key` with the same replay semantics (`uq_consumption_op_idempotency`).
   This is what keeps the Android bridge, web UI, and manual tools working.
4. Gating is configuration-controlled so it can be activated per environment;
   default remains today's behaviour until the vertical slice is proven.

This directly removes the R5 hazard: `log_water`/`log_liquid` double-counting when
a drink is already accounted for by the meal path.

---

## 5. PostgreSQL as durable authority

Durable in **PostgreSQL** (already modelled by the prior phases):

- receipt + operation state → `consumption_operations` (`status`, `result_json`,
  `payload_hash`, `raw_text`, `idempotency_key`, identity columns);
- item identity → `consumption_items` (`item_key`, provenance, links);
- beverage linkage → `beverage_measurements`;
- poll offset → persisted alongside so restart resumes correctly.

**Redis is an accelerator only.** Recovery of ownership and replay must never
require Redis. A Redis restart is not evidence of Redis data-loss survival; the
recovery tests must therefore drop and recreate Redis entirely and show that the
durable Postgres record still replays.

---

## 6. Vertical slice to be proven (evidence to follow)

One raw Telegram event → trusted ingress → nutrition resolution → confirmation →
atomic meal/liquid persistence → response. Decisive tests:

1. concurrent **identical** messages → two consumptions, correctly attributed;
2. redelivery and lost responses → no second consumption, durable replay;
3. multiple drinks and **same-item overlap** (meal + liquid tools, one volume and
   one nutrition contribution);
4. edits and **ownership-checked** callbacks;
5. worker restart and **complete Redis loss**;
6. failures between each stage: receipt → interpretation → confirmation →
   commit → reply delivery.

Run on an **Alembic-built database**, with the currently skipped R1/R4 cases
enabled, and report those results **separately** from the default suite.

## 7. Honest status

T1 is the selected approach; selection is not proof. R1/R2, the outstanding R4
checks, and the other review findings remain open until demonstrated.
