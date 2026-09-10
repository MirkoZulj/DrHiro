# R2 / T1 — Production Ingress Reconciliation (read-only)

Date: 2026-09-09
Branch: `feature/meal-liquid-idempotency` (isolated)
Status: **read-only confirmation, reconciled with the earlier OpenClaw trace.**
No production change, push, deployment, or historical cleanup.

## Finding

**The actual production trusted ingress is OpenClaw's own Telegram channel —
NOT the repo's `telegram-bridge`.**

Verified read-only against the deployed containers:

| Evidence | Value | Source |
|---|---|---|
| No `telegram-bridge` container running | only `drhiro-openclaw-gateway-1`, `drhiro-api-1`, `drhiro-mcp`, `drhiro-worker-1`, … | `docker ps` |
| OpenClaw version | `2026.7.1-2` (`ghcr.io/openclaw/openclaw`) | container image |
| Telegram channel **enabled** | `channels.telegram.enabled: true`, `botToken: <redacted>`, `dmPolicy: pairing`, `groupPolicy: disabled` | `/home/node/.openclaw/openclaw.json` |
| Provider | `trueforge` → `http://tf-shim:3200/v1` | `openclaw.json` models.providers |
| MCP drhiro server | `enabled: false` | `openclaw.json` mcp.servers.drhiro |
| skill-drhiro | `enabled: false` | `openclaw.json` skills.entries |

### The offset store carries a VERIFIED bot identity

```
/home/node/.openclaw/telegram/update-offset-default.json.migrated
{
  "version": 2,
  "lastUpdateId": 836547722,
  "botId": "8677922871"
}
```

This is decisive for the trust boundary:
- OpenClaw's Telegram channel is the **single polling owner** of the production
  bot (long polling, `getUpdates`, offset persisted durably to this store).
- The store records the **verified `getMe.id`** (`botId: 8677922871`) and the
  **durable `lastUpdateId`** — both written by OpenClaw's own runtime.
- The Telegram extension exposes `readTelegramUpdateOffset` /
  `writeTelegramUpdateOffset` / `deleteTelegramUpdateOffset` and
  `inspectTelegramAccount` in its API surface.

## Reconciliation with the earlier OpenClaw trace

The earlier R2 investigation (commit `74abd4d`, then the capability verification)
traced the same component and concluded:
- `threadId` = the topic (`message_thread_id`), **not** the chat id.
- `conversationId` = `msg.chat.id` (the chat), possibly with a `:topic:<id>` suffix.
- `messageId` = `msg.message_id` (chat-scoped).
- `update_id` is **not exposed to plugin hooks** → excluded from consumption
  identity; `runId` recorded for attempt tracing only.
- OpenClaw's plugin context exposes only the `accountId` **label** — so a plugin
  cannot independently prove the verified bot id at call time.

**Reconciled conclusion:** the earlier trace is correct, and it described the
production ingress exactly (the OpenClaw Telegram channel). The gap that sent us
to T1 — OpenClaw drops identity before the shim and the plugin context exposes
only a label — is unchanged. What T1 adds is a component that captures identity
**before** OpenClaw discards it.

## Implication for T1

The T1 trusted-ingress worker and writer-ownership design are correct, but the
**minting site must be reconciled with production reality**:

- The repo's `telegram-bridge` is the intended *repo* ingress, but it is **not
  deployed**. Extending it (as done in the vertical-slice prototype) is a valid
  isolated-branch demonstration, but it is **not** the production path.
- In production, the minting component must sit on **OpenClaw's Telegram channel**:
  either a custom OpenClaw channel plugin that reads the durable offset store /
  `inspectTelegramAccount` for the verified `botId` and mints the envelope at
  ingress, or an OpenClaw-side gateway shim that owns the raw update.
- The durable offset store (`botId` + `lastUpdateId`) is the authoritative source
  for the verified bot identity in production, and it already persists across
  restarts (good for the recovery workstream).

## Not performed

No production configuration change, no container edit, no restart, no push, no
deployment. This is a read-only confirmation for the checkpoint.
