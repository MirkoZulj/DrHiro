# R2 (Option B) — Capability Verification, Transport Contract, and Test Plan

Date: 2026-09-09
Branch: `feature/meal-liquid-idempotency` (isolated)
Status: **DESIGN — awaiting review. Nothing implemented against production.** No production config change, restart, push, or deploy.

---

## 1. Capability verification (what OpenClaw 2026.7.1 can actually forward)

Verified by inspecting the deployed OpenClaw build (`drhiro-openclaw-gateway-1`,
`openclaw@2026.7.1`) — its request-construction code, config schema, and bundled
plugin docs — not by inferring from Redis key patterns.

### 1.1 The outbound provider request carries NO identity

`buildOpenAICompletionsParams()` (in `openai-transport-stream-*.js`) constructs
the request sent to the provider (our `tf-shim`). It sets exactly:

`model`, `messages`, `stream`, `stream_options`, `store`, `prompt_cache_key`,
`prompt_cache_retention`, `temperature`, `top_p`, `response_format`,
`frequency_penalty`, `presence_penalty`, `seed`, `stop`, `tools`, `tool_choice`,
and max-token fields.

**It never sets `user` and never sets `metadata`.** This is the code-level
explanation for the observed production Redis keys all being
`tfshim:session:first:<hash>` (the shim's last-resort "hash of first message
text" fallback): OpenClaw supplies neither `user` nor `metadata`, so the shim's
preferred branches never match.

### 1.2 There is no supported plugin hook for the provider payload

OpenClaw's internal agent core does have `before_provider_payload` /
`before_provider_request` seams (`emitBeforeProviderPayload`,
`emitBeforeProviderRequest`), but **they are internal**: their handler signature
is `{type, model, payload}` (and `{type, model, sessionId, streamOptions}`), and
neither name appears in the **supported plugin hook union**, which is:

```
after_compaction, after_tool_call, agent_end, agent_turn_prepare,
before_agent_finalize, before_agent_reply, before_agent_run, before_agent_start,
before_compaction, before_dispatch, before_install, before_message_write,
before_model_resolve, before_prompt_build, before_reset, before_tool_call,
channel_pairing_requested, cron_changed, gateway_start, gateway_stop,
heartbeat_prompt_contribution, inbound_claim, llm_input, llm_output,
message_received, message_sending, message_sent, model_call_ended,
model_call_started, reply_dispatch, reply_payload_sending, resolve_exec_env,
session_end, session_start, subagent_delivery_target, subagent_ended,
subagent_spawned, subagent_spawning, tool_result_persist
```

(extracted from `hook-runner-global-*.js`; matches `docs/plugins/hooks.md`).
So a plugin **cannot** inject `user`/`metadata` onto the wire.

### 1.3 The tool loop is NOT in OpenClaw

OpenClaw's own `mcp.servers.drhiro` and `skills.entries.skill-drhiro` entries are
**`enabled: false`**; the agent spec runs inside **TrueForge**, reached because
OpenClaw treats `tf-shim` as an OpenAI-compatible *model*. Tool execution
therefore happens in TrueForge, and OpenClaw's `before_tool_call` never fires for
the meal/liquid tools.

### 1.4 What IS available: trusted inbound identity + turn context

Two supported hooks give us what we need:

- **`message_received`** — delivers `threadId` (= chat), **`messageId`**,
  `senderId`, `sessionKey`, **`runId`**, `from`, `content`, `timestamp`, and
  `metadata` (`to`, `provider`, `surface`, `originatingChannel`, `originatingTo`,
  `messageId`, `senderId`, `senderName`, …). The internal variant additionally
  carries `channelId`, `accountId`, `conversationId`. → **This is the trusted
  application transport origin for event identity.**
- **`agent_turn_prepare` / `before_prompt_build`** — can inject
  `prependContext` / `appendContext` (and system-prompt text) into the turn.
  → **This is the only plugin-controllable channel into the provider request.**

### 1.5 Verdict

Option B **is achievable**, but **not** by "provider metadata" (impossible here).
It is achieved by: a plugin capturing trusted identity at `message_received`,
persisting it durably, and injecting an **opaque, HMAC-signed event envelope**
into the turn context that the shim reads from the **request body**. Because the
envelope carries only a signed opaque id — not the identity — the model can
neither read nor forge identity.

**No OpenClaw fork or gateway rewrite is required.** If review rejects
prompt-channel carriage, the fallback is a gateway/adapter change (see §7).

---

## 2. Identity contract (required by review)

| Requirement | Design |
|---|---|
| Authenticated user context separate from event identity | User identity stays where it is today (`x-telegram-id` → user lookup). Event identity is a distinct envelope → `source_chat_id`/`source_message_id`/`source_bot_id` on the operation. Never conflated. |
| Preserve bot_id, chat_id, message_id | Captured at `message_received` (`accountId`/surface→bot, `threadId`/`conversationId`→chat, `messageId`) and stored **durably** in the event record. Not carried in clear text through the model. |
| Retain `update_id` for delivery tracing | OpenClaw does **not** expose Telegram `update_id` to plugin hooks. **Gap — see §6.** Delivery tracing uses `(bot_id, chat_id, message_id)` plus the per-run `runId`; `update_id` is recorded when/if the ingress can supply it. |
| Not every update is a consumption | The envelope is only attached to turns that reach an ingestion tool. A non-consumption update produces no operation; the plugin does not mint an operation on delivery alone. |
| Callbacks reference the original operation | Confirmation callbacks carry the **same event id**, resolving to the existing operation (prototype test 6). A callback never mints a second consumption. |
| Stable per-item discriminator | Item key = `f"{event_id}:item:{discriminator}"`, where the discriminator is derived from the item's position/canonical identity. Two drinks in one message → two keys. A liquid-tool call for a drink already written by the meal tool resolves to the **same** key (prototype test 4). |
| Edited messages invoke corrections | An edit is an update carrying `edited_message` for an existing `message_id`: the derived event id is unchanged, so it resolves to the existing operation and is applied as a **correction mutation**, not a new consumption. A genuinely new message id is a new consumption. |
| No shared "latest event" record | Binding is keyed by `event_id`; no global "latest" slot exists (prototype test 3 demonstrates concurrent turns keeping separate identities). |
| Durable beyond transient state | Event record + operation result live in the database; Redis is at most a cache. Retry after cache loss returns the durable result (prototype test 5). |
| Trust boundary | Identity never comes from model-generated tool arguments. The envelope is minted by the host plugin from the channel event and is **HMAC-signed**; unsigned/tampered/expired envelopes are rejected (prototype trust-boundary tests). Clients/tools cannot set or override it: the MCP only *echoes* a signed id, and the API verifies the signature. |

---

## 3. Exact transport contract

**Envelope block** (injected into the turn's context text):
```
<<<BEGIN_DRHIRO_EVENT_CONTEXT>>>
[DRHIRO_EVENT_CONTEXT] {"v":1,"event_id":"<32-hex>","chat_id_h":"<16-hex>","message_id_h":"<16-hex>","bot_id_h":"<16-hex>","issued_at":<unix>,"nonce":"<short>","sig":"<64-hex hmac-sha256>"}
<<<END_DRHIRO_EVENT_CONTEXT>>>
```
- `sig = HMAC-SHA256(secret, canonical_json(all fields except sig))`, secret shared **only** between the OpenClaw plugin and the shim.
- `event_id = sha256(bot_id + "|" + chat_id + "|" + message_id)[:32]` — deterministic, so redelivery derives the identical id.
- `chat_id_h`/`message_id_h`/`bot_id_h` are truncated hashes; raw identifiers never enter the prompt.

**Hop contract**

| Hop | Carries | Notes |
|---|---|---|
| Telegram → OpenClaw | full update (bot, chat, message, update_id, sender) | unchanged |
| OpenClaw plugin `message_received` | captures bot/chat/message (+sessionKey, runId) | mints `event_id`; persists durable event record |
| OpenClaw plugin `agent_turn_prepare`/`before_prompt_build` | injects the signed envelope into turn context | per-turn binding; no shared slot |
| OpenClaw → tf-shim | OpenAI chat-completions request whose `messages` contain the envelope | **transport read**: the shim parses the request body, never model output |
| tf-shim | verifies HMAC + expiry; resolves `event_id` → raw identity from the durable record | strips prior-turn envelopes so history replays cannot be mistaken for the current event |
| tf-shim → TrueForge | per-turn context keyed by `event_id` | tool calls in this run are bound to that event |
| MCP → API | `source_chat_id`/`source_message_id`/`source_bot_id` (or `idempotency_key=event_id`) **+ signed proof** | API verifies the signature and the registry; rejects unsigned/unknown |
| API | `consumption_operations` uniqueness on `(user, bot, chat, message)` and on `idempotency_key` | existing constraints; durable replay via `result_json` |

**Failure mode:** a missing/invalid envelope → the API **fails closed** with an
explicit error; it never silently writes without identity (matching the existing
`telegram_source_requires_identity` behaviour).

---

## 4. Demonstrated with a sanitized fixture

`tests/fixtures/r2_provider_request_sanitized.json` — a synthetic
provider-request body (placeholder ids only), plus
`tests/r2_prototype_event_envelope.py` (mint/verify/extract, durable store) and
`tests/test_r2_optionb_envelope.py` (**13 tests, all passing**; full suite
`262 passed, 11 skipped`).

The prototype shows: envelope extracted and verified from the request body; raw
identifiers absent from the prompt; baseline (no envelope — today's real traffic)
fails explicitly rather than guessing; forged/unsigned/tampered/expired/wrong-secret
envelopes rejected; and the six acceptance scenarios below pass at prototype level.

---

## 5. Acceptance test plan (end-to-end, after implementation)

All via the **real authenticated path**, on an Alembic-built DB
(`DRHIRO_R1R2_ALEMBIC_DB=1`), providers mocked at their boundary:

1. **Two separate messages, identical text** → two events, two consumptions, two distinct `operation_id`s, both counted in daily totals.
2. **Redelivery / retry of one message** → one event, one consumption; retry returns the saved `result_json`.
3. **Concurrent messages in the same chat** → separate event ids; both persist; no cross-assignment of items.
4. **Multiple drinks + cross-tool overlap** → distinct item keys per drink; a meal-tool + liquid-tool call for the same drink share one item key and contribute volume/calories once.
5. **Confirmation retry after Redis deletion** → durable saved result returned (no 404, no duplicate).
6. **Message edit / confirmation callback** → targets the existing operation as a correction; no second consumption.
7. **Trust boundary** → unsigned/forged/expired/wrong-secret envelope → explicit failure, no write; user text containing a look-alike block cannot forge identity.
8. **Non-Telegram callers** → caller-supplied `idempotency_key`, stable across retries.

---

## 6. Known gaps (explicit)

- **`update_id` is not exposed** by OpenClaw's plugin hooks. Requirement says
  "retain for delivery tracing" — we cannot retain what the transport does not
  provide. Options: (a) accept `(bot_id, chat_id, message_id)` + `runId` as the
  delivery-tracing tuple (Telegram guarantees message-id stability); (b) add a
  gateway/adapter that captures `update_id` earlier (see §7). **Decision needed.**
- **Prompt-channel carriage** relies on the plugin injecting context text; OpenClaw
  escapes its own internal-context delimiters in some paths but not
  universally, so the design deliberately does **not** rely on delimiter
  uniqueness — security rests on the HMAC.
- **`bot_id`** is derived from the channel account (`accountId`/surface) rather
  than a Telegram `getMe` id; if an exact Telegram bot id is required, the
  adapter must supply it.

---

## 7. Fallback if review rejects prompt-channel carriage

A gateway/adapter change that captures the identity **before it is discarded**:

- **Option B′ (adapter in front of the shim):** keep OpenClaw as-is; place an
  adapter that owns the Telegram ingress (or wrap `update-offset-runtime`) so it
  sees the raw update, mints the same `event_id`, persists the durable record, and
  attaches the signed envelope — identical contract, different injection point.
- **Option B″ (custom channel plugin):** implement the Telegram channel as a
  plugin using the documented channel SDK (`sdk-channel-ingress`,
  `sdk-channel-turn`) so the channel itself owns the event identity end to end.

Both preserve the §2 contract and §3 envelope format; only the mint point moves.

---

## 8. What is NOT done

Nothing is implemented: no plugin, no shim change, no MCP/API change, no config
change, no deployment. R1 remains **partial**; the remaining R4 evidence items
(migration-history graph, production existing-table validation, production-mirror
value/relationship preservation, downgrade qualification, `activities`
provisioning) remain **open**.
