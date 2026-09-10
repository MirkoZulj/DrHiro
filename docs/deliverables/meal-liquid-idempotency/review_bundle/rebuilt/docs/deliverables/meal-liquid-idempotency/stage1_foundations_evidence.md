# Stage 1 Foundations Evidence — Meal+Liquid Idempotency

**Branch**: `feature/meal-liquid-idempotency`
**Date**: 2026-09-09
**Commit**: {commit_hash}
**Final suite**: 154 passed

---

## B6 — Migration Single Authority (FULLY CLOSED)

**Affected path**: `apps/api/alembic/versions/f1a2b3c4d5e6_consumption_idempotency.py`, `B_schema_migration_up.sql`, `B_schema_migration_rollback.sql`

**Reproducer**: Previously, multiple migration scripts could independently create the same tables/columns, risking drift on the Pi's disposable Postgres.

**Fix**: One Alembic revision is the single authority. SQL rollback artifacts (up/down) are generated from Alembic and committed alongside it. Migrations are idempotent (`CREATE TABLE IF NOT EXISTS`, `DROP TABLE IF EXISTS`).

**Verification**: `python -m pytest tests/test_b6_migration_authority.py -q` → 13 passed. Alembic `upgrade`/`downgrade` validated against the disposable Postgres (13 migration-authority assertions incl. idempotent re-run, rollback completeness, column nullability for traceability).

---

## B3 — Trusted Identity Propagation (FULLY CLOSED)

**Affected path**: `apps/api/src/drhiro_api/services/consumption.py` (`get_or_create_operation`), `apps/api/src/drhiro_api/models.py` (`ConsumptionOperation.payload_hash`)

**Reproducer**: Without identity enforcement, any caller could omit `source_chat_id`/`source_message_id`/`source_bot_id` for Telegram source and still create an operation, defeating idempotency.

**Fix**:
- `get_or_create_operation` fails closed: Telegram source requires full identity; non-Telegram requires `idempotency_key`.
- `payload_hash` added to `ConsumptionOperation`. Reuse of the same identity key with a different payload → `ValueError("conflicting_payload_reuse")`.
- Multi-item discriminators (different `raw_text` for same key) tested.

**Verification**: `python -m pytest tests/test_b3_identity_propagation.py -q` → 21 passed. Covers: telegram identity required, missing identity → error, idempotency_key path, conflicting payload reuse, multi-item discrimination, idempotent second-call.

---

## B5 — Concurrency-Safe First Creation (FULLY CLOSED)

**Affected path**: `apps/api/src/drhiro_api/services/consumption.py` (`get_or_create_operation` atomic INSERT)

**Reproducer**: Two concurrent callers racing on first creation could both pass the `SELECT` check and both `INSERT`, creating duplicate operations for the same Telegram identity.

**Fix**: Replaced the SELECT-then-INSERT with `INSERT ... ON CONFLICT DO NOTHothing`. After the INSERT, re-select the winner row and compare `op.id == insert_id` to determine `created`. The type mismatch bug (`insert_id = str(uuid.uuid4())` vs `op.id` as UUID object) is fixed by using `insert_id = uuid.uuid4()`.

**Sequential test** (`test_concurrent_submit_one_set`): Updated to reflect the atomic B5 semantics — first call creates (created=True), second finds (created=False), both return the same `op.id`.

**Genuine concurrent test** (`tests/test_b5_concurrent.py::TestB5ConcurrentFirstCreation::test_concurrent_first_creation_two_sessions_barrier`):
- Two separate DB sessions (thread-local `sessionmaker`)
- `threading.Barrier(2)` to synchronize both threads right before the INSERT
- Both callers attempt first creation simultaneously on PostgreSQL
- Exactly ONE operation row + ONE meal + ONE measurement created
- Both callers receive the SAME `meal_id`
- Stable across 3 consecutive runs

**Verification**: `python -m pytest tests/test_b5_concurrent.py tests/test_consumption_idempotency.py::TestIdempotency::test_concurrent_submit_one_set -q` → 2 passed (3 runs, all green).

---

## B2 — Durable Replay Without Redis Draft (FULLY CLOSED)

**Affected path**:
- `apps/api/src/drhiro_api/services/consumption.py`: new `find_completed_result_by_identity()`
- `apps/api/src/drhiro_api/services/intelligent_meal_service_patch.py`: `confirm_meal` rewritten

**Reproducer**: The confirm handler did `get_draft(req.draft_id)` → 404 if absent. A retry after the Redis draft was deleted (response loss, TTL expiry) failed with 404, forcing the caller to re-parse and risk an empty meal.

**Fix**:
1. Added `find_completed_result_by_identity()` — looks up a COMPLETED `ConsumptionOperation` by source identity (Telegram key or idempotency_key) and returns its saved `result_json`.
2. Rewrote `confirm_meal`:
   - If draft exists → parse, confirm, delete draft (unchanged happy path).
   - If draft is gone → call `find_completed_result_by_identity()`. If found → return saved result (durable replay). If not found → raise explicit 404 with detail (NOT an empty meal).

**Tests** (all through the actual `handler_confirm` flow — not a substitute):
- `test_response_lost_after_commit_replay_returns_saved_result`: first confirm deletes draft, retry returns same `meal_id`, no duplicate.
- `test_expired_draft_replay_returns_saved_result`: explicit draft TTL deletion, retry returns saved result.
- `test_missing_draft_no_completed_op_returns_explicit_error`: no draft + no op → 404 + `ok=False` + `error` key. Asserts NO meal, NO measurement, NO operation created (empty-meal anti-pattern is prevented).

**Verification**: `python -m pytest tests/test_consumption_confirm_integration.py::TestB2DurableReplayWithoutDraft -q` → 3 passed.

---

## Stage 1 Status Summary

| Finding | Status | Tests |
|---|---|---|
| B6 Migration single authority | **FULLY CLOSED** | 13 (Alembic up/down + idempotency) |
| B3 Trusted identity propagation | **FULLY CLOSED** | 21 (identity enforcement + payload_hash) |
| B5 Concurrency-safe creation | **FULLY CLOSED** | 2 (sequential + genuine concurrent with barrier) |
| B2 Durable replay w/o draft | **FULLY CLOSED** | 3 (response-loss, TTL, missing-draft-error) |
| **Total new/updated tests** | | **39** across B2/B3/B5/B6 |
| **Full suite** | | **154 passed, 0 failed** |

## Files Changed (Stage 1)
- `apps/api/src/drhiro_api/services/consumption.py` — atomic B5 INSERT, `find_completed_result_by_identity()` for B2
- `apps/api/src/drhiro_api/services/intelligent_meal_service_patch.py` — B2 durable replay confirm handler
- `tests/test_consumption_idempotency.py` — updated `test_concurrent_submit_one_set` for B5 semantics
- `tests/test_b5_concurrent.py` (NEW) — genuine concurrent first-creation test
- `tests/test_consumption_confirm_integration.py` — B2 tests + updated handler simulation
- Debug scratch files removed (`debug_b3_full.py`, `debug_b3_test.py`, `drhiro_meal_liquid_review_bundle.tar.gz`)
