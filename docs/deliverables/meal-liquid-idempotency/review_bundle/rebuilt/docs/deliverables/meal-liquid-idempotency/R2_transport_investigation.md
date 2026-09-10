# R2 Transport Investigation — Telegram → MCP → API chain (read-only)

Date: 2026-09-09
Branch: `feature/meal-liquid-idempotency`
Status: **investigation only.** No production files edited, no restarts, no deploy, no push.

## 1. The live chain (traced, verified read-only)

```
Telegram
  └─> openclaw-gateway (OpenClaw, owns the bot token; receives the update)
        └─> tf-shim :3200/v1   (OpenAI-compatible; provider "trueforge")
              └─> TrueForge    (agent spec "drhiro", runs the tool loop)
                    └─> drhiro-mcp (tool server; calls the drHiro API)
                          └─> drHiro API (consumption domain)
```

Verified from live config/env (non-secret values only):
- OpenClaw config sets `models.providers.trueforge.baseUrl = http://tf-shim:3200/v1`,
  model `trueforge/trueforge-drhiro`; the in-gateway `mcp.servers.drhiro` and
  `skills.entries.skill-drhiro` entries are **`enabled: false`** — so tool calls are
  executed by **TrueForge's** agent, not inside OpenClaw.
- MCP container env: `DRHIRO_API_URL`, `DRHIRO_SERVICE_TOKEN`,
  **`DRHIRO_TELEGRAM_ID=984523234` (static)**, `REDIS_URL=redis://drhiro-redis-1:6379/3`.

## 2. Where trusted identities originate

| Identity | Originates | Trusted? | Reaches the writer? |
|---|---|---|---|
| **User (Telegram id)** | OpenClaw verifies the sender; MCP reads a **static env** `DRHIRO_TELEGRAM_ID` and sends `x-telegram-id` to the API | trusted (config), but **not per-event** | yes — user only |
| **Bot id** | Not carried anywhere in the chain | — | **no** |
| **Chat id** | Present in the Telegram update inside OpenClaw; **not forwarded** | — | **no** |
| **Message id** | Present in the Telegram update inside OpenClaw; **not forwarded** | — | **no** |
| **Update id** | Present in the Telegram update inside OpenClaw; **not forwarded** | — | **no** |

## 3. Which fields survive each hop

- **Telegram → OpenClaw:** full update available (chat_id, message_id, update_id, from.id).
- **OpenClaw → tf-shim:** OpenAI-style body. The shim derives a conversation key from
  `body.user` → `metadata.{chat_id,session_id,conversation_id,thread_id}` → **hash of the
  first user message**.
- **Live evidence (decisive):** *every* key in production Redis is of the form
  `tfshim:session:first:<hash>` — i.e. OpenClaw forwards **no** `user` and **no**
  `metadata`. Conversation identity today is a hash of the first message text.
- **tf-shim → TrueForge:** `{"input":[{"type":"user.message","content": text}]}` — text only.
- **TrueForge → MCP:** model-generated tool arguments. Any identity placed in tool arguments
  is **model-generated and therefore untrusted**.
- **MCP → API:** `x-service-token` + the **static** `x-telegram-id`.
- **MCP side channel already in use:** the shim stashes `tfshim:last_user_text` (TTL 900 s) in
  Redis and the MCP **reads it** to recover the date phrase the model paraphrases away. This is
  the existing precedent for trusted-context-via-Redis.

## 4. Confirmation callbacks / retries

There is **no** confirmation callback that refers to a Telegram message. The MCP
auto-confirms immediately (it calls `/meals/from-text` then
`/meals/from-text-intelligent/confirm` in the same tool invocation). The only
"retry" is the MCP re-calling confirm after a failure — with the **same** draft_id
but no event identity, and the live service's fallback dedup is a **2-minute
text-match heuristic** in the deployed `service.py` (not event identity).

## 5. Multiple drinks / multiple tool calls from one message

- Multiple items in one message are handled **within** one confirm call (the draft
  holds an item list; `selections=[0]*n`).
- Multiple **tool calls** from one message (e.g. meal tool + manual liquid tool) are
  currently distinguished only by tool name and by text/time heuristics. There is no
  shared per-message key, so cross-tool overlap cannot be reliably deduped today.

## 6. Non-Telegram callers

`confirm_consumption` already accepts a caller-supplied `idempotency_key`
(`source="api"`). This is the supported path for non-Telegram callers. Its
uniqueness constraint (`uq_consumption_op_idempotency`) enforces replay safety.

## 7. The blocking conclusion

**A trusted per-event identity does not exist anywhere in the current transport.**
OpenClaw owns the Telegram update but forwards neither `user` nor `metadata`; the
shim therefore falls back to a first-message hash; the MCP knows only a static
user id. Event identity cannot be obtained from tool arguments (model-generated),
and a freshly generated key per call would provide no idempotency.

## 8. Proposed transport contract (DECISION REQUIRED — not yet implemented)

### Option A — OpenClaw forwards a stable conversation key; shim mints turn identity
- OpenClaw sends the OpenAI `user` field (or `metadata.chat_id`) = Telegram chat id.
- The shim derives `conversation_key = user:<chat_id>` and, per incoming turn, writes a
  trusted per-turn record to Redis:
  `tfshim:last_turn:<conversation_key>` = `{turn_id, text_sha256, created_at}`.
- The MCP reads that record (same pattern as `tfshim:last_user_text`) and passes
  `source_chat_id=<chat_id>` plus `source_message_id=<turn_id>` (or an explicit
  `idempotency_key`) to the API.
- **Fidelity:** retries of the same turn reuse the same `turn_id` → dedup works.
  **Limitation:** the shim sees text, not Telegram `message_id`, so a user
  *deliberately* sending identical text twice may collide within the turn window.
- **Requires:** an OpenClaw config change (or a plugin/skill) to forward `user`/`metadata`;
  possibly OpenClaw version support. No OpenClaw source fork if the field is configurable.

### Option B — True per-message idempotency via the update owner
- The component that owns the Telegram update (OpenClaw) forwards `chat_id`, `message_id`,
  `update_id`, `bot_id` as request metadata to the shim; the shim records them in Redis; the
  MCP reads them and supplies them verbatim as `source_*` to the API.
- **Fidelity:** highest — true per-message identity; deliberately repeated identical texts
  are distinct events and both count; retries collapse correctly.
- **Requires:** a capability in OpenClaw to pass Telegram context through to the model
  provider request (config or plugin). **This is the open question** — it must be confirmed
  against OpenClaw's supported configuration before it can be relied on.

### Option C — Narrower fallback if neither A nor B is available
- Keep the static user identity, and use the existing `tfshim:last_user_text`-style Redis
  record to supply a **caller idempotency key** the MCP derives as
  `sha256(conversation_key + text + coarse time-bucket)`. Explicitly documented as
  **weaker** (cannot distinguish deliberate repeats; bucket-bound), not full idempotency.

### Affected files for any of A/B/C
- `services/tf-shim/shim.py` (conversation key + per-turn trusted record) — isolated branch copy.
- `packages/drhiro-mcp/src/drhiro_mcp/sse_server.py` (read the trusted record; send
  `source_chat_id/source_message_id/source_bot_id` or `idempotency_key`) — isolated branch copy.
- `apps/api/src/drhiro_api/services/intelligent_meal_service_patch.py` (accept + propagate
  the event context into `confirm_consumption`, separate from user auth).
- Possibly `openclaw.json` (config only) if Option A/B needs OpenClaw to forward context.

**Nothing above is implemented. Awaiting the transport-design decision (A, B, or C).**
