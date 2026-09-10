# R2 — Hook-Field Semantics Verification and Architecture Decision

Date: 2026-09-09
Branch: `feature/meal-liquid-idempotency` (isolated)
Status: **VERIFIED against OpenClaw 2026.7.1 deployed build; architecture DECIDED.**
No production config change, restart, push, or deploy.

---

## 1. What the hook fields actually mean (verified from `telegram-ingress-spool-*.js`, not assumed)

| Field in plugin hook | Actual meaning | Provenance (verified) |
|---|---|---|
| `threadId` | **Telegram `message_thread_id` (the topic)** — NOT the chat id | `resolveTelegramInbound...`: `const threadId = isGroup ? resolveTelegramForumThreadId({isForum, messageThreadId}) : messageThreadId;` |
| `messageId` | Telegram `msg.message_id`, stringified | `const messageId = typeof msg.message_id === "number" ? String(msg.message_id) : void 0;` and set into the hook context as `messageId` |
| `conversationId` | Telegram `msg.chat.id` (with `:topic:<threadId>` appended for topic messages) | `conversation.id = String(chatId)`; `conversationKey = threadId != null ? "${chatId}:topic:${threadId}" : String(chatId)`; hook `conversationId` = `stripChannelPrefix(to/originatingTo)` |
| `senderId` | Telegram `msg.from.id` | `senderId = msg.from?.id != null ? String(msg.from.id) : void 0` |
| `channelId` | `"telegram"` | `ctx.OriginatingChannel ?? ctx.Surface ?? ctx.Provider` |
| `accountId` | **configured account LABEL** (e.g. "main") — NOT a bot id | `ctx.AccountId` |
| bot Telegram id (`me.id`) | **available in the channel runtime only** as `primaryCtx.me?.id` (from `getMe`) — **NOT exposed to plugin hook context** | `const botId = primaryCtx.me?.id;` (line 4498); plugin context exposes only `accountId` |

### Consequences

1. **`threadId` is the topic, not the chat.** In a group/supergroup with topics, two messages in different topics share the same `chat_id` but have different `message_thread_id`; `message_id` is chat-scoped and unique regardless of topic. For consumption identity, `(chat_id, message_id)` is **unambiguous** across topics without needing `threadId`. This is sound: **`event_id` must be derived from the chat (`conversationId` stripped to `chat_id`) + `message_id`**, not from `threadId`.

2. **A standard plugin hook cannot supply a verified bot id.** The plugin `message_received` event/context carries only `accountId` (a label). The verified Telegram bot id (`me.id`, from `getMe`) exists only inside the Telegram channel runtime. Therefore, to satisfy "bot_id = actual Telegram bot ID, mapped from the channel account to a verified getMe.id, stored in trusted config, fail closed if missing/ambiguous," the plugin path alone is **insufficient**; a channel-level adapter (or provisioning that captures `me.id` at startup and stores it per `accountId` in trusted config) is required.

3. **`update_id` is not exposed** to plugin hooks (confirmed in the mapper and ingress). Per instruction, it is **not required** for consumption identity; delivery tracing uses `(bot_id, chat_id, message_id)` with `runId` recorded separately for attempt tracing and **excluded from the consumption key** (it can change on retries).

---

## 2. Architecture decision — adapter required

The user's conditions demand guarantees the standard plugin hook path cannot provide:

- **Verified bot id** (`me.id`) — only `accountId` label is in the plugin context.
- **Guaranteed per-turn binding + extraction** — the plugin injects context *into the prompt*; only a process on the request path can strictly extract-and-remove the envelope before it reaches the model, and bind it to the specific turn/run with an input digest.
- **Run-scoped trusted transport to MCP/API** — the tool loop runs inside TrueForge; the standard plugin path cannot carry event context to the MCP without the model seeing it.

**Decision: use an adapter on the request path** (the shim, which already sits between OpenClaw and TrueForge and already processes the request body). Concretely:

1. **Mint point (identity capture):** a channel-level mechanism captures `me.id` (verified getMe) and stores `accountId → me.id` in trusted config at provisioning/startup; on each inbound message it derives `event_id` from `(bot_id, chat_id, message_id)` and mints a signed envelope.
2. **Extraction + binding (adapter = the shim):** the shim reads ONLY the designated envelope location in the request (the OpenClaw runtime-context block for the immediately preceding user message — not arbitrary user text, not history), verifies the HMAC **and the request binding** (service, account, event, input-digest), removes the envelope **before** forwarding the turn to TrueForge, and never persists it to conversational history or logs.
3. **Run-scoped transport:** the shim writes a per-event (NOT shared-"latest") trusted record that the MCP resolves on the request path, and the API verifies it — identity never travels as model-generated tool arguments.
4. **Fail closed** on missing, duplicate, conflicting, unsigned, expired, or wrong-service/account/input-digest envelopes.

This is the fallback the instruction explicitly anticipated ("If supported hooks cannot guarantee this binding and extraction, use the adapter").

---

## 3. Contract refinements locked by this verification

- **`event_id = BLAKE2b/256(canonical_versioned("v1", bot_id, chat_id, message_id))`** — a versioned, canonical serialization (JSON with sorted keys and explicit field tags), NOT ambiguous delimiter concatenation (`"|"` is not relied on; group/topic suffixes are excluded so topic boundaries do not fragment identity).
- **`chat_id`** = `conversationId` stripped of the `:topic:<threadId>` suffix (verified parse), i.e. `msg.chat.id`.
- **`bot_id`** = verified `me.id` from trusted config (fail closed if absent/ambiguous).
- **`threadId`** retained separately for delivery/audit, excluded from the consumption key.
- **`runId`** recorded separately for attempt tracing, excluded from the consumption key.
- **Request binding** = `HMAC over canonical(service, account, event_id, input_digest)` where `input_digest` is the SHA-256 of the *relevant current input* (defined below), so a valid envelope copied from another request fails.
- **Envelope expiry** (`issued_at` + TTL) governs acceptance of a *new* write; **durable replay retention** is independent (the saved result is returned for a fresh authenticated retry of an old event even after the envelope's own credentials expired).

### What exactly is hashed for `input_digest`

The shim computes the digest over the **current user-authored input only** (the text of the newest user-role message *excluding* the OpenClaw runtime-context block and any envelope), normalized to the same canonical form used at mint time (e.g. UTF-8 NFC, trimmed). This binds the envelope to the specific turn so a copied envelope for a different input is rejected, while a redelivery of the *same* message (same input text) verifies.

---

## 4. Open / not done

The refined envelope module, shim adapter changes, MCP/API propagation, and the real integration harness (restart + Redis-loss) are **to be implemented next** on the isolated branch. R1 remains partial; remaining R4 evidence items remain open.
