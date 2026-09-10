# R6 — T1 Trusted-Ingress Cutover and Rollback Plan

Date: 2026-09-09
Branch: `feature/meal-liquid-idempotency` (isolated)
Status: **plan, NOT executed. No production change, push, deployment, or
historical cleanup.**

Replaces/extends the Stage-4 B9 writer-ownership runbook with the T1 trusted
ingress cutover. The B9 runbook (`stage4_cutover_runbook.md`) governs the
unified backend writer; this plan governs who owns Telegram consumption and how
the model's writer surface is gated.

---

## 0. Scope and hard rules

- **Preparation only.** This is a sequenced plan with a rollback path, tested on
  disposable databases. It is NOT executed against the live VPS.
- **No push, no deploy, no restart, no production config change.**
- **No historical data deletion.** Existing `Meal`, `MealItem`, `Measurement`
  rows are preserved.
- **Secrets redacted.** No tokens, user UUIDs, or internal IPs here.

---

## 1. Production reality (from the reconciliation)

Production trusted ingress is **OpenClaw's Telegram channel**, not the repo's
`telegram-bridge`:

- `channels.telegram.enabled: true`, `botToken`, `dmPolicy: pairing`,
  `groupPolicy: disabled`.
- Durable offset store carries the **verified `botId`** (`8677922871`) and
  `lastUpdateId` — OpenClaw is the single polling owner.
- OpenClaw forwards to `http://tf-shim:3200/v1` (TrueForge via the shim); the
  MCP drhiro server and skill-drhiro are both `enabled: false`.

**Consequence:** the minting component that signs the trusted event must sit on
OpenClaw's Telegram channel, not on the bridge (which is not deployed). The
repo-side bridge implementation and its tests are a valid isolated-branch
prototype of the trusted worker, but the production seam is OpenClaw's channel.

---

## 2. Cutover phases

### P0 — Pre-cutover (today)
- `DRHIRO_TELEGRAM_INGRESS_ENABLED=false`, `DRHIRO_LEGACY_CONSUMPTION_WRITERS_ENABLED=true`,
  `DRHIRO_TRUSTED_INGRESS_WRITERS_DISABLED=false`.
- Behaviour is unchanged: the model logs through the existing path.

### P1 — Minting component on OpenClaw's channel (build, no switch)
- Implement the minting seam that captures identity **before** OpenClaw
  discards it: read the durable offset store / `inspectTelegramAccount` for the
  verified `botId`, extract `conversationId` + `messageId`, sign the envelope.
- Gate: `DRHIRO_TELEGRAM_INGRESS_ENABLED=true` alone. The trusted worker runs
  but the legacy writers are still open, so nothing is blocked yet.

### P2 — Writer ownership handover
- Set `DRHIRO_LEGACY_CONSUMPTION_WRITERS_ENABLED=false` AND
  `DRHIRO_TRUSTED_INGRESS_WRITERS_DISABLED=true` together.
- The trusted worker owns Telegram consumption; the model's consumption tools
  (`log_meal`, `log_meal_intelligent`, `confirm_intelligent_meal`, `log_water`,
  `log_liquid`, `log_recipe_meal`, `build_recipe`, `delete_meal`,
  `correct_meal_item`) return `model_writer_disabled`; the `/meals` and `/tools`
  service-token writers are refused at the API.
- `DRHIRO_TELEGRAM_BOT_ID` and `DRHIRO_TELEGRAM_INGRESS_SECRET` must both be
  configured and match the verified `getMe.id`; the ingress fails closed (503)
  otherwise.

### P3 — Observe and confirm
- Watch for `model_writer_disabled` in model calls and `completed` / `revised`
  in the trusted worker. Confirm no silent double-count (R3 reconciliation).

---

## 3. Invariants enforced by the flags

| Invariant | Enforced by |
|---|---|
| No gap: trusted path ON before legacy writers close | `require_model_writer_allowed` returns 503 `legacy_writers_closed_without_trusted_path` if legacy OFF but trusted OFF |
| No overlap: model cannot write while trusted ON | `require_model_writer_allowed` 403 `model_writer_disabled` for service-token writers; MCP `consumption_writers_disabled` for model tools |
| Manual logging preserved | `/ingest/*` is NOT API-gated (user JWT surface); MCP tool gate is the model-only control |
| Fail closed on missing config | ingress endpoint 503 if secret/bot id missing |

---

## 4. Rollback

### 4.1 Fast rollback (recommended, additive)
Revert the flags to P0:
1. `DRHIRO_TRUSTED_INGRESS_WRITERS_DISABLED=false`
2. `DRHIRO_LEGACY_CONSUMPTION_WRITERS_ENABLED=true`
3. `DRHIRO_TELEGRAM_INGRESS_ENABLED=false`

No schema change is required. Operations already written by the trusted worker
are preserved; the model resumes legacy logging. Rollback is a config change
only, no data migration, no destructive step.

### 4.2 Destructive schema downgrade (EMERGENCY ONLY)
Do **not** do this as a routine rollback. If a schema downgrade were ever forced
(e.g. to a pre-`consumption_operations` revision), the four feature columns
(`consumption_items` provenance, etc.) would be dropped and their contents lost.
This is **not** lossless and is not authorised as a routine path.

### 4.3 Partial-flag rollback (gate-only)
To stop model writes while keeping the trusted worker active:
`DRHIRO_TRUSTED_INGRESS_WRITERS_DISABLED=true` + `DRHIRO_LEGACY_CONSUMPTION_WRITERS_ENABLED=false`
without the trusted path is refused (503); so keep trusted ON while closing the
model writers.

---

## 5. Recovery (see crash-recovery workstream)

- In-flight operations (`pending` / `processing` older than a threshold) are
  re-driven from PostgreSQL by `recover_incomplete_operations`, **not** by
  Telegram redelivery.
- The durable receipt (consumption_operations row) is the redelivery arbiter,
  independent of the poll offset.
- Complete Redis loss does not affect recovery or replay (PostgreSQL is the
  authority).

---

## 6. Pre-cutover evidence required (gate)

- T1 vertical slice on an Alembic-built DB (green: 24 tests).
- T1 ingress endpoint over the real ASGI app (green: 10).
- Route-by-route writer gate + transition (green: 27).
- MCP consumption-writer gate (green: 10).
- R3 reconciliation regression (`test_legacy_new_water_coexistence`, green: 10).
- Default suite (green; T1 gated tests skipped by default).
- R4 remaining evidence items (see R4 evidence doc) before production cutover.

Cutover is gated on this checklist being satisfied in full; it is not satisfied
yet (R4 items open, no live round trip, LLM proposer outside acceptance).
