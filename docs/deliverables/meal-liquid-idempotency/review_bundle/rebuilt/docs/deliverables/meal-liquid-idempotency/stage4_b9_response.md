# B9 — Coordinated Deployment Cutover (Response Matrix)

**Branch**: `feature/meal-liquid-idempotency`
**Date**: 2026-09-09
**Commit**: (this commit)

---

## B9 Response Matrix

B9 is not a code defect — it is the **coordinated cutover** from the old
(dual-write, non-idempotent) state to the new (single-writer, idempotent)
state. It closes the last remaining gate before production deployment.

| # | Finding | Affected Path | Fix | Verification | Status |
|---|---------|---------------|-----|--------------|--------|
| B9-0 | **All-or-nothing cutover** — removing the old MCP side effect had no env toggle, making the cutover binary (deploy-all-or-deploy-none). | `packages/drhiro-mcp/src/drhiro_mcp/sse_server.py` | Added `DRHIRO_LIQUID_WRITER=legacy\|unified` flag (fail-closed). Default `legacy` preserves old behavior; `unified` disables the side effect atomically. | `tests/test_stage4_writer_flag.py` — 5 tests proving mode control + fail-closed on unknown value. | **CLOSED** |
| B9-1 | **Dual-write window undefined** — the period between deploying the unified backend and disabling the old side effect had no defined writer ownership. | Cutover runbook | Phase table in `stage4_cutover_runbook.md` identifies P1 as the ONLY dual-write phase; minimizes it to minutes; provides atomic flip via env var. | Rehearsal `test_legacy_then_unified_both_succeed` proves both paths work; `test_no_gap_no_overlap_invariant` proves unified-mode invariant. | **CLOSED** |
| B9-2 | **No in-flight handling spec** — requests in-flight during the cutover could replay incorrectly. | Ingest + confirm pipeline | Section 2 of the runbook: drain/pause via Telegram webhook pause; idempotency keys ensure no replay creates duplicate. | `test_retry_across_transition_returns_saved` — same `message_id` returns saved result, 1 meal, 1 measurement. | **CLOSED** |
| B9-3 | **No compatibility gates** — if the unified backend is missing (e.g., old API container), the MCP would silently omit liquid logging. | Pre-write gate | Section 3 of the runbook: check migration state + version + dependency before writes. Fail-closed: missing table → `OperationalError` → MCP returns `{"ok": false, "error": "meal_logging_failed"}`. | `test_unified_write_rolls_back_on_error` — FK violation rolls back, no partial state. | **CLOSED** |
| B9-4 | **No rollback procedure** — if the unified writer has bugs, reverting was ad-hoc. | Rollback | Section 4.1: **app rollback** is safe and additive (existing rows preserved). Section 4.2: **destructive schema downgrade** requires data-preservation plan (back up new tables first). | `test_app_rollback_preserves_data` — flag flip does not delete existing `ConsumptionOperation` / `BeverageMeasurement` rows. | **CLOSED** |
| B9-5 | **No rehearsal** — no evidence the cutover works on real Postgres. | Entire cutover flow | `tests/test_stage4_cutover_rehearsal.py` — 5 tests on disposable Postgres covering legacy→unified, failure-midway, retries, rollback, invariant. | All 5 tests pass (real execution output in `test_results.txt`). | **CLOSED** |

### Stage 1-3 Blockers (Carried Forward, All Closed)

| Finding | Status | Closed In |
|---------|--------|-----------|
| B1 — Nutrient resolution in draft→confirm path | **CLOSED** | Stage 2 (cbc01e0) |
| B2 — Durable replay without Redis draft | **CLOSED** | Stage 1 |
| B3 — Trusted identity propagation | **CLOSED** | Stage 1 |
| B4 — Entry-point coverage | **CLOSED** | Stage 3 (76c6e4f) |
| B5 — Concurrency-safe first creation | **CLOSED** | Stage 1 |
| B6 — Migration single authority | **CLOSED** | Stage 1 |
| B7 — Canonical mutations | **CLOSED** | Stage 3 (febc798) |
| B8 — Parser conversions/classification | **CLOSED** | Stage 2 |
| B9 — Coordinated deployment cutover | **CLOSED** | Stage 4 (this commit) |

---

## Evidence

- **Runbook**: `docs/deliverables/meal-liquid-idempotency/stage4_cutover_runbook.md`
- **Flag tests**: `tests/test_stage4_writer_flag.py` (5 passed)
- **Rehearsal tests**: `tests/test_stage4_cutover_rehearsal.py` (5 passed)
- **Full suite**: `test_results.txt` (243 passed)

---

## Test Count After B9 Closeout

| Stage | Tests | Cumulative |
|-------|-------|------------|
| Stage 1 (B1, B2, B3, B5, B6, B8) | 154 | 154 |
| Stage 2 (B1 deep-dive, B8 parser) | 30 | 184 |
| Stage 3 (B4, B7) | 49 | 233 |
| Stage 4 (B9 flag + rehearsal) | 10 | **243** |

All 243 tests pass.
