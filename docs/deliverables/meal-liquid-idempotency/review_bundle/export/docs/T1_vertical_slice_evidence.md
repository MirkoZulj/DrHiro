# T1 — Trusted Ingress Vertical Slice: Evidence

Date: 2026-09-09
Branch: `feature/meal-liquid-idempotency` (isolated)
Status: **vertical slice implemented and proven on an Alembic-built database.**
No production change, push, deployment, or historical cleanup.

Design: `T1_ingress_design_and_writer_ownership.md` (committed first).

---

## 1. The ingress component

`services/telegram-bridge` — extended, **not** replaced. It already owned the
single polling consumer of the bot token, saw the raw update, held the token, and
authorized the sender. Added:

| File | Role |
|---|---|
| `services/telegram-bridge/src/drhiro_bridge/ingress.py` | Extracts trusted identity from the authentic update, signs it (HMAC), delivers it. `extract_event` / `sign_event` / `verify_event` / `IngressClient`. |
| `apps/api/src/drhiro_api/services/telegram_ingress.py` | The trusted worker: canonical identity, `accept_signed_event` (verify, fail closed), durable receipt, binding, validation, nutrient resolution, atomic write, revisions, ownership-checked callbacks. |
| `apps/api/src/drhiro_api/routers/telegram_ingress.py` | `POST /api/v1/ingest/telegram/event` + `require_model_writer_allowed` gate. |
| `services/telegram-bridge/src/drhiro_bridge/main.py` | Poll-loop wiring + verified `getMe.id` (`_verify_bot_id`). |

Identity is `blake2b(canonical JSON v1 {bot, chat, msg})`. Content is a separate
`sha256` digest. The signature covers `service + bot + chat + msg + user +
digest + kind`.

## 2. Decisive test results

### Default suite (unchanged behaviour)

```
294 passed, 39 skipped
```
(Pre-T1 baseline was 284 passed / 11 skipped. +10 always-on bridge-wiring tests;
28 new gated tests are skipped by default: 18 vertical-slice + 10 endpoint.)

### T1 vertical slice — **Alembic-built DB** (`drhiro_t1_alembic`, head `9a1b2c3d4e5f`)

```
18 passed
```
The schema is **not** created by `create_all`; the suite asserts
`alembic_version` is populated before running.

| Requirement | Test | Result |
|---|---|---|
| Raw update → ingress → resolution → persistence | `test_raw_update_to_persisted_meal` | 300 g steak → **813 kcal / 78 g protein** persisted, `resolution_source='db'` |
| Unknown ≠ known-zero | `test_unknown_food_is_not_reported_as_known_zero` | `nutrition_complete=False`, `resolution_source='unmatched'` |
| **Concurrent identical messages** | `test_two_identical_messages_are_two_consumptions` | 2 consumptions, 2 operations, distinct message ids |
| Concurrent redelivery of one message | `test_concurrent_redelivery_of_one_message_yields_one_consumption` | 4 threads → **1** meal |
| **Redelivery / lost response** | `test_redelivery_replays_without_second_consumption` | status `replayed`, same `meal_id`, 1 meal |
| Same event, different content, not an edit | `test_same_identity_different_content_without_edit_is_rejected` | `IngressConflict` raised, still 1 meal |
| **Multiple drinks + same-item overlap** | `test_drinks_and_meal_contribute_exactly_once` | 2 drinks → exactly 2 measurements (150, 330 ml), 2 beverage links, 3 items, totals = 542 + 124.5 + 141.9 kcal |
| Repeated drink under same event | `test_repeated_logging_of_same_drink_converges` | 1 measurement, 1 meal |
| **Edits** | `test_edit_is_a_revision_not_a_second_consumption` | status `revised`, revision 1, still 1 operation / 1 meal, totals updated to 500 g |
| **Ownership-checked callbacks** | `test_callback_confirm_replays_and_checks_ownership` | owner → `replayed`; other user → `rejected: callback_not_owner` |
| Callback for unknown operation | `test_callback_for_unknown_operation_is_ignored` | `ignored`, no meal |
| **Complete Redis loss + worker restart** | `test_complete_redis_loss_replays_from_postgres` | `FLUSHALL` executed, `dbsize()==0`, fresh session → `replayed` from PostgreSQL, meal count still 1 |
| Forged signature | `test_forged_signature_is_rejected` | `invalid_signature` |
| Missing envelope | `test_missing_envelope_is_rejected` | rejected |
| Content swapped after signing | `test_content_swapped_after_signing_is_rejected` | `content_digest_mismatch` |
| **Cross-account substitution** | `test_cross_account_substitution_is_rejected` | `cross_account_bot_mismatch` |
| **Model cannot supply identity/nutrition** | `test_model_output_cannot_supply_identity_or_nutrition` | hostile `user_id`/`bot_id`/`kcal=99999` ignored; identity from transport, nutrition server-resolved 813 kcal |
| Malformed proposals | `test_malformed_proposals_are_rejected_not_repaired` | 5 rejections, `needs_clarification`, no meal |

### T1 endpoint — HTTP through the real ASGI app, Alembic DB

```
10 passed
```
Fail-closed: missing secret → 503, missing verified bot id → 503, missing
signature → 401, forged → 401, cross-account → 401.
Happy path: signed event → 200 with 813 kcal persisted; redelivery over HTTP →
`replayed`, 1 meal.
Writer ownership: model service token → **403 `model_writer_disabled`**; manual
caller unaffected; closing legacy writers with no trusted path → **503
`legacy_writers_closed_without_trusted_path`**.

### Gated R1/R4 — **second Alembic-built DB** (`drhiro_r1r4_alembic`)

```
17 passed
```
These are the tests skipped in the default suite. Enabled with
`DRHIRO_R1R2_ALEMBIC_DB=1 DRHIRO_R4_ALEMBIC_DB=1`.

### Bridge wiring

```
10 passed
```
Consumption turn owned by ingress → **the model is never called**
(`turns_received == []`), reply carries the trusted totals. Non-consumption turn
→ `no_consumption`, handed back to the model. `getMe` mismatch → refuses to
start; missing id → fails closed. Extraction covers `message`, `edited_message`
(kind `edit`), and `callback_query` (referencing the original operation).

## 3. Failure-point coverage

- **Before receipt:** nothing is written; Telegram redelivers (offset not advanced).
- **After receipt, before interpretation:** operation exists as `processing`; a
  redelivery finds it and re-runs (no duplicate, single unique identity).
- **After interpretation, before commit:** no meal row; operation not `completed`.
- **After commit, before reply:** result is durable; the redelivered update
  returns `replayed` with the original `meal_id`.
- **Reply delivery failure:** does not roll back the write (result read from
  PostgreSQL, not from the reply path).
- **Worker restart / Redis loss:** proven above.

## 4. Honest scope — not proven here

- **No live Telegram round trip.** No deployment is authorized, so the ingress is
  proven through the real bridge extraction code and the real ASGI app against
  the real database — not against Telegram's servers.
- **The production proposer is not exercised end-to-end.** Tests inject a
  deterministic proposer; the LLM proposer (`llm_proposer`) is wired but its
  quality/parsing is not validated here.
- **The writer gate is implemented and tested but NOT activated.** Defaults keep
  `telegram_ingress_enabled=false` and `legacy_consumption_writers_enabled=true`,
  so today's behaviour is unchanged.
- **`/meals/from-text-intelligent/confirm`** is called by the MCP but has no
  matching route in the API; noted, not resolved.
- **Poll-offset persistence is designed but not implemented**; today the offset
  is in-memory, which is why the durable receipt (not the offset) is the arbiter
  of redelivery.

R1 remains **partial** (real Telegram confirmation producing correct persisted
nutrition has not been demonstrated). The outstanding R4 evidence items
(production existing-table validation, production-mirror value/relationship
preservation, downgrade qualification, `activities`) remain **open**.
