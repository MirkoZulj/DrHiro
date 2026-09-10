# R2 — Shim Adapter Integration Evidence

Date: 2026-09-09
Branch: `feature/meal-liquid-idempotency` (isolated)
Status: **shim-adapter phase implemented + integration-proven; MCP/API propagation not yet done.**
No production config change, restart, push, or deploy.

## Scope of this phase

The review required the trusted event identity to be captured from application
transport, bound to the specific turn/run, extracted-and-removed before the
model, and never carried as model-generated tool arguments. The adapter seam is
the shim (already between OpenClaw and TrueForge). This phase implements and
proves the **shim-side** of that contract against the REAL modified shim + REAL
Redis. The MCP/API propagation (run-scoped transport into the API confirm handler
with ownership-checked callbacks) is the **next, not-yet-complete** phase.

## What the real shim now does (commits 346ed0a, 38e8efe)

1. **Extract + remove:** reads the envelope ONLY from the OpenClaw
   runtime-context block (never user text or history), verifies it, and strips it
   from the request body before anything is forwarded to TrueForge.
2. **Fail closed:** `503` if trusted config (`DRHIRO_BOT_ID`, `DRHIRO_EVENT_SECRET`)
   is missing; `422` on missing / duplicate / unsigned / expired / forged /
   copied / cross-account / no-envelope.
3. **Binding:** HMAC over `service + account(bot) + event + input_digest`. The
   shim independently knows only the trusted bot id (`getMe.id`); chat/message are
   authentic because the HMAC covers them and `event_id` is canonically
   consistent with the signed identity fields. `input_digest` binds to the actual
   current input, so a valid envelope copied from a different request fails.
4. **Durable, per-event record:** writes `tfshim:event:<event_id>` to Redis —
   NOT a shared "latest event" slot — with `EVENT_RECORD_TTL >>` envelope max-age,
   so a fresh authenticated retry of an old event resolves its durable result
   even after the envelope credentials expired.
5. **Cleanup:** the cleaned body drives `conversation_key` and
   `latest_user_message`, so the envelope never enters history or logs.

## Integration evidence (tests/test_r2_shim_integration.py — 8 passing)

Launches the REAL modified shim + a TrueForge-equivalent stub as subprocesses
against REAL Redis (`drhiro-dev-redis-1`, db 15) and drives the real HTTP
endpoint:

| Case | Result |
|---|---|
| Valid envelope | `200`; stub proves the envelope NEVER reached the model side (`has_envelope=false`) |
| Missing / junk envelope | `422`, model NOT called |
| Envelope copied from another request (different input) | `422` (input-digest binding) |
| Forged (wrong secret) | `422` |
| Cross-account (other bot id) | `422` |
| Concurrent events (two message ids) | two independent durable records, no context exchange |
| **Restart** | real shim process killed + restarted: records + session mapping survive; a valid envelope still processes |
| **Redis-loss** | transient record deletion re-established by a fresh valid envelope; a stale copied envelope still rejected |

Envelope unit tests (`tests/test_r2_event_envelope.py`, 14) additionally prove:
envelope quoted in user text / in history is **never scanned or accepted**;
duplicate envelopes rejected as ambiguous; expiry-vs-durable-retention
distinction; canonical (not concatenated) event id; topic non-fragmentation;
two identical messages = two consumptions; redelivery = one; item-discriminator
stability.

Full suite: `284 passed, 11 skipped`.

## Signing-key and rotation notes

- `DRHIRO_EVENT_SECRET` is the HMAC key shared only with the minting adapter; it
  is read from the environment, never in source control or prompts. `getMe`
  provisioning maps `accountId -> DRHIRO_BOT_ID` at startup. Rotation = change
  the secret in secret management and redeploy; envelopes minted under the old
  key are rejected (fail closed). Expiry is enforced by `issued_at` + max-age.

## Honest residual scope (NOT done in this phase)

- **MCP/API propagation:** the API confirm handler must accept + verify the event
  context (source_bot/chat/message + signature or a run-scoped token) and enforce
  ownership-checked callbacks. The run-scoped transport from the shim's durable
  record through the TrueForge tool loop to the MCP is not yet wired or proven.
- **Identical-text concurrent disambiguation:** prompt-channel carriage alone
  cannot independently distinguish two concurrent messages with IDENTICAL text
  (their `input_digest` matches); full disambiguation needs the run-scoped
  store resolved at the MCP/API boundary by the message identity the adapter
  captures. This is part of the MCP/API phase, not resolved here.

These are not claimed as closed. R1 remains partial; remaining R4 evidence items
(migration-history graph, production existing-table validation, production-mirror
value/relationship preservation, downgrade qualification, `activities`
provisioning) remain open.
