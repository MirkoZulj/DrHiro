# R2 — Transport Rebound: TrueForge cannot carry trusted per-run context

Date: 2026-09-09
Branch: `feature/meal-liquid-idempotency` (isolated)
Status: **STOPPED at a consequential design decision — transport change required.**
No production change, push, or deploy.

## 1. Where trusted identity enters the adapter — honest correction

My earlier "adapter" framing was wrong, and you were right to challenge it.

- The **shim signs nothing and receives no identity.** OpenClaw drops the
  Telegram identity before the shim (`buildOpenAICompletionsParams` never sets
  `user`/`metadata`; plugin context exposes only the `accountId` label).
- My R2 integration tests **supplied the envelope from a fixture**. They prove
  the *shim's downstream handling* (extract, verify, remove, durable-record
  write) works — they do **not** exercise live ingress. There is **no minting
  component** in the repo today.
- The component that must capture and sign `(bot_id, conversationId, messageId)`
  is one that **owns the raw Telegram update and a verified `getMe.id`**. Two
  candidates, neither built:
  - a **custom OpenClaw channel plugin** (runs inside the OpenClaw Telegram
    extension, which has `primaryCtx.me.id`, `msg.chat.id`, `msg.message_id`);
    or
  - a **pre-OpenClaw gateway** that owns the bot webhook/polling and `getMe`
    and injects the signed envelope into the request OpenClaw forwards.

## 2. Run binding cannot survive the TrueForge tool loop — verified

The requirement: a trusted invocation context must select the correct event
independently of text/ordering/latest-lookup, and survive TrueForge → MCP.

Verified against the deployed stack:
- **TrueForge is a third-party Node agent** (`@truefoundry/trueforge@0.1.4`).
  Its `sessionId` is internal DB identity (`session_id`/`turn_id` rows); there is
  **no per-run token attached to MCP `tools/call` requests**.
- **The MCP itself documents it:** `# Session is advisory, not enforced —
  TrueForge may operate statelessly.` The MCP reads only `mcp-session-id` (a
  UUID the MCP mints at `initialize`) and model-generated tool arguments.
- **The shim creates one TrueForge session per `conversation_key`**, and since
  OpenClaw forwards no `user`/`metadata`, that key is `first:<first-message-hash>`
  — a **chat/thread-scoped** session shared by every message in the thread.
- Therefore **two identical-text messages in the same chat are indistinguishable
  at the MCP**: same session, same conversation key, identical model-chosen tool
  arguments, no trusted per-run identity.

**Verdict: TrueForge cannot carry trusted per-run context to the MCP.** Per the
instruction, I am stopping to propose a transport change. Model-generated tool
arguments and text matching are rejected as identity carriers.

## 3. Proposed transport change

All viable options converge on one principle: **consumption identity and
operation creation must never depend on the model's tool loop.**

### Option T1 (recommended) — deterministic ingestion out of the model path
A trusted ingestion component that owns the raw update + verified `getMe.id`
parses the message deterministically and calls the drHiro API directly with:
- full trusted event identity `(bot_id, chat_id, message_id)`,
- a signed envelope / HMAC for authenticity,
- a durable idempotency key `= canonical(event)`,
- `input_digest` for the exact text ingested.

The LLM (TrueForge) becomes **assistive only**: it may propose/disambiguate
food candidates, but those candidates are fed **back through the trusted
ingestion component** to create the operation. The model never creates
operations and never supplies identity. This is the only architecture that
guarantees per-event binding, identical-text disambiguation, and no double-count.

### Option T2 — per-event session + out-of-band logging worker
The shim creates a **separate TrueForge session per event** and records
`event → trueforge_session` durably; a trusted worker (or the shim) performs the
logging using the event binding it owns, rather than the MCP's model-driven
tool calls. Effect equals T1 with more moving parts.

### Option T3 — deterministic worker + LLM only for non-consumption turns
Consumption messages are routed to the deterministic worker; the model path is
used only for other turns. Same principle as T1.

### What T1 removes
- The MCP `tools/call` path for meal/liquid logging (or it becomes advisory).
- The double-count hazard from `manual/water` vs meal-tool overlap.
- The impossible requirement of binding model tool calls to a trusted event.

## 4. Expiry and durable-store corrections

- **Durable records must live in PostgreSQL, not Redis.** A Redis-restart test
  does **not** establish survival of Redis **data loss**. The `consumption_operations`
  table (result_json + idempotency key) is the durable store; Redis is at most a
  cache and must never be the source of truth for identity or results.
- **Expiry semantics:** an expired or invalid envelope must **not authorize any
  read or write**. Keeping results longer than the credentials is correct;
  **accepting expired credentials is not**. A *fresh authenticated* request for
  the same event may retrieve its durable result, because the idempotency key is
  `canonical(event)` and the durable result is keyed by it in Postgres — never
  by an accepted-expired envelope.

## 5. Honest status

- **No live-ingress minting component exists** (see §1). My shim integration
  tests are downstream-only evidence.
- **Run binding through TrueForge → MCP is not achievable** without a transport
  change (§2, §3). This is a consequential design decision requiring your call.
- R1 stays **partial** until a real Telegram confirmation produces correct
  persisted nutrition; R4 evidence items remain **open**.
- Skipped-test coverage relevant to these findings: `DRHIRO_R1R2_ALEMBIC_DB=1`
  (11 skipped in the default suite) gates the R1/R4 decisive tests that require
  the Alembic-built DB; those do not cover live ingress or TrueForge tool
  execution and so are not evidence for this transport question.

**Awaiting your decision on T1 / T2 / T3 (or a different transport direction).**
