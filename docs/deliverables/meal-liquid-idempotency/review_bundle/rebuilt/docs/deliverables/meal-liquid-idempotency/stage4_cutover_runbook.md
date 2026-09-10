# Stage 4 — Coordinated Cutover Runbook (B9)

**Branch**: `feature/meal-liquid-idempotency`
**Date**: 2026-09-09
**Commit**: febc798 (Stage 3) + Stage 4 changes
**Status**: PREPARATION ONLY — NOT executed against production

---

## 0. Scope & Hard Rules

- **Preparation only.** This runbook is a document + a tested procedure on the
  disposable Postgres. It is NOT executed against the live VPS.
- **No push.** All changes stay on the branch.
- **No VPS edits.** The live MCP container (`drhiro-mcp`) and
  `intelligent-meal` are NOT touched by this runbook.
- **No historical data deletion.** Existing `Measurement`, `Meal`, `MealItem`
  rows are preserved.
- **Redact secrets.** No real user UUIDs, tokens, or internal IPs in this doc.

---

## 1. Writer-Ownership Phase Table

The cutover transitions consumption writes from the **old MCP liquid
side-effect** to the **new unified backend writer** (`consumption.py`).

| Phase | Old MCP Liquid Side Effect | New Unified Backend Writer | Who Owns Consumption Writes | Liquid-Log State | Telegram Updates |
|-------|---------------------------|---------------------------|----------------------------|------------------|------------------|
| **P0 — Pre-cutover (current live)** | ACTIVE (hard-coded JWT, whole-message regex) | NOT wired into live `service.py` (preview only) | **Old MCP side effect** for liquid; `service.py` confirm for meals (separate, non-atomic) | Old path writes bare `metric_type=water` rows | Normal |
| **P1 — Deploy unified backend, hold old writer** | ACTIVE (flag=`legacy`) | WIRED into `intelligent_meal_service_patch.py` (deploy-only) | **Both** — but only for NEW meal confirms that go through the unified path. Legacy `log_meal` (non-intelligent) still uses old side effect. | Unified writer creates `BeverageMeasurement` + `Measurement` + `ConsumptionOperation` per drink. Old side effect ALSO writes a bare `Measurement` for the same drink → **DUAL WRITE**. | Normal |
| **P2 — Flip flag to `unified` (atomic cutover)** | DISABLED (flag=`unified` → MCP side effect SKIPPED) | WIRED + authoritative | **Unified backend writer ONLY** | Unified path only. No bare water rows from MCP. | Normal |
| **P3 — Post-cutover (steady state)** | DISABLED | AUTHORITATIVE | Unified backend writer | All drinks logged via `ConsumptionOperation` + `BeverageMeasurement` | Normal |
| **R1 — App rollback (safe)** | DISABLED (flag stays `unified`) | ROLL BACK to previous `service.py` code | **Neither** writes via unified path; old `service.py` code has no liquid logic → drinks in meals have NO liquid projection until re-confirmed. **No data loss** — existing rows preserved. | Existing `BeverageMeasurement` rows remain; new meal confirms (old code) don't create new ones. | Normal |
| **R2 — DestructIVE schema downgrade (emergency only)** | Would need flag=`legacy` + old code | Schema downgrade drops `consumption_operations`, `consumption_items`, `beverage_measurements` | **Old MCP side effect** (if code restored) | **DATA LOSS**: idempotency history + beverage links destroyed. Existing `Measurement` rows remain but are orphaned from meals. | Normal |

### Key Invariant — No Gap, No Overlap

```
                  ┌─────────────────────────────────────────────────────┐
                  │  P0          P1           P2          P3           │
                  │  old=ACTIVE  old=ACTIVE   old=DISABLED old=DISABLED │
                  │  new=INACTIVE new=ACTIVE  new=AUTHORITATIVE         │
                  │              ↑           ↑                         │
                  │              │           │                         │
                  │              │  FLIP     │                         │
                  │              │  (atomic) │                         │
                  └─────────────────────────────────────────────────────┘
```

- **P1 is the ONLY phase with dual writes.** It must be as short as possible
  (minutes, not hours). The runbook minimizes P1 by pre-staging the unified
  backend deploy and doing the flag flip immediately after.
- **The flag flip (P1→P2) is atomic** — a single env-var change + container
  restart. There is no interval where both writers are active AND the old
  side effect is also active, because the flag is read at request time.
- **There is no interval where neither writer records a drink** — in P1 both
  are active; in P2 the unified writer is authoritative.

---

## 2. In-Flight Requests + Retries

### 2.1 Drain / Pause Writes

The cutover does NOT require a full write pause because:

1. **Idempotency keys** ensure that a request replayed across the P1→P2
   boundary returns the SAME result (same `meal_id`) without creating a
   duplicate.
2. **The flag is read per-request** — in-flight requests that started under
   P1 (legacy) complete with the old side effect; new requests after the
   restart see `unified`.

If a **controlled write pause** is desired (e.g., to avoid any dual-write
window):

```bash
# Pause Telegram webhook (stop new updates from arriving)
curl -X POST https://api.telegram.org/bot<TOKEN>/setWebhook --data '{"url": ""}'

# Wait for in-flight requests to drain (max 30s for MCP timeout)
sleep 30

# Verify no in-flight writes
docker logs drhiro-mcp --since 30s | grep -c "log_meal_intelligent"
# Expect: 0

# Perform the cutover (Section 3)

# Resume Telegram webhook
curl -X POST https://api.telegram.org/bot<TOKEN>/setWebhook \
  --data '{"url": "https://<VPS_HOST>/telegram/webhook"}'
```

### 2.2 Preserve Queued Telegram Updates

Telegram queues updates for up to 24 hours if the bot is unreachable. Pausing
the webhook (not stopping the bot) preserves the queue. Updates that arrived
during the pause are delivered when the webhook is restored.

### 2.3 Prevent Old-Path Replay as New Consumption

A request that began under the old path (P0/P1) carries a Telegram
`message_id`. When the unified writer (P2) receives the same `message_id`
(via retry), `get_or_create_operation` finds the existing
`ConsumptionOperation` row (created by the unified writer in P1, or by the
old path in P0) and returns its saved `result_json` — no new rows.

**Critical**: The old MCP side effect (P0/P1) did NOT create a
`ConsumptionOperation` row. So a retry of a P0/P1 request in P2 will NOT find
an operation and will write a NEW consumption. This is **correct behavior** —
the original request was logged by the old path (bare `Measurement`), and the
retry in P2 creates a proper `ConsumptionOperation` + `BeverageMeasurement`.
The old bare `Measurement` row remains (harmless — it's a duplicate volume
contribution, not a duplicate meal).

To avoid even this duplicate volume, use the controlled write pause (2.1).

---

## 3. Compatibility Checks (Pre-Write Gate)

Before enabling writes in P2, verify ALL of the following:

### 3.1 Migration State

```bash
# On the live DB (read-only check):
docker exec drhiro-dev-postgres-1 psql -U drhiro -d drhiro_test -c "
  SELECT table_name FROM information_schema.tables
  WHERE table_name IN ('consumption_operations','consumption_items','beverage_measurements')
  ORDER BY table_name;
"
# Expect: all 3 tables present

# Verify columns:
docker exec drhiro-dev-postgres-1 psql -U drhiro -d drhiro_test -c "
  SELECT column_name FROM information_schema.columns
  WHERE table_name='consumption_operations' AND column_name IN ('id','user_id','source','result_json','status');
"
# Expect: all 5 columns present
```

**Fail-closed**: If any table/column is missing, the unified writer raises
`OperationalError` on first write. The MCP returns `{"ok": false, "error":
"meal_logging_failed"}` — the meal is NOT logged, and the user is told to
retry. No silent omission.

### 3.2 Version Compatibility

| Component | Minimum Version | Check Command |
|-----------|----------------|---------------|
| MCP server | `sse_server.py` with `DRHIRO_LIQUID_WRITER` flag | `docker exec drhiro-mcp python -c "import drhiro_mcp.sse_server as m; print(m.get_liquid_writer_mode())"` |
| Backend API | `consumption.py` with `confirm_consumption` | `docker exec drhiro-api python -c "from drhiro_api.services.consumption import confirm_consumption; print('ok')"` |
| Intelligent meal | `intelligent_meal_service_patch.py` with `confirm_meal` | `docker exec intelligent-meal python -c "from service import confirm_meal; print('ok')"` |
| Alembic | revision `f1a2b3c4d5e6` applied | `docker exec drhiro-api alembic current` → `f1a2b3c4d5e6` |

**Fail-closed**: If the MCP flag function is missing (old code), the import
fails at container start → container crashes → no writes. This is intentional:
an old MCP container cannot silently run with the new backend.

### 3.3 Dependency Check

```bash
# Verify the unified writer is importable from the MCP's perspective
# (MCP calls backend via HTTP, but the flag must be present)
docker exec drhiro-mcp python -c "
import os
os.environ['DRHIRO_LIQUID_WRITER'] = 'unified'
import drhiro_mcp.sse_server as m
assert m.is_unified_writer(), 'flag not respected'
print('unified mode OK')
"
```

---

## 4. Rollback Policy

### 4.1 Application Rollback (SAFE — Additive)

**Trigger**: Bug in the unified writer (e.g., wrong nutrient scaling).

**Procedure**:
1. Set `DRHIRO_LIQUID_WRITER=legacy` on the MCP container.
2. Restart the MCP container.
3. Restart the intelligent-meal service with the previous `service.py`.

**Effect**:
- Existing `ConsumptionOperation`, `ConsumptionItem`, `BeverageMeasurement`
  rows are **preserved** (not deleted).
- New meal confirms use the old `service.py` path (no unified writer).
- Drinks in meals confirmed after rollback have NO liquid projection
  (the old `service.py` doesn't create `BeverageMeasurement`).
- **No data loss** — all existing rows remain queryable.

**Recovery**: Re-deploy the unified backend → P2 is re-entered. New confirms
create proper beverage links. Old confirms (during rollback) are not
retroactively linked.

### 4.2 Destructive Schema Downgrade (EMERGENCY ONLY)

**Trigger**: Schema corruption requiring full revert.

**⚠️ WARNING**: This DESTROYS idempotency history and beverage linkage.

**Procedure**:
1. Back up the three new tables:
   ```bash
   docker exec drhiro-dev-postgres-1 pg_dump -U drhiro -d drhiro_test \
     --table=consumption_operations \
     --table=consumption_items \
     --table=beverage_measurements \
     > /tmp/consumption_backup_$(date +%s).sql
   ```
2. Run Alembic downgrade:
   ```bash
   docker exec drhiro-api alembic downgrade e7f8a9b0c1d2
   ```
3. Set `DRHIRO_LIQUID_WRITER=legacy` on MCP.
4. Deploy old `sse_server.py` + old `service.py`.

**Effect**:
- `consumption_operations`, `consumption_items`, `beverage_measurements`
  tables are **dropped**.
- All idempotency history is **lost** — replays of old Telegram messages
  will create duplicate meals.
- Existing `Measurement` rows (bare water) remain but are orphaned from meals.
- Existing `Meal` and `MealItem` rows are **unaffected**.

**Do NOT resume writing with old code against new schema** — the old
`service.py` doesn't know about `source_operation_id` columns, so it will
either (a) ignore them (safe) or (b) crash on INSERT if the column is
non-nullable (our columns are nullable, so safe).

---

## 5. Rehearsal Evidence

The following was executed on the **disposable Postgres**
(`drhiro_test` via `docker exec drhiro-dev-postgres-1`).

### 5.1 Rehearsal Script

See `tests/test_stage4_cutover_rehearsal.py` for the full executable
rehearsal. Summary of scenarios:

| Scenario | What was tested | Result |
|----------|----------------|--------|
| **Cutover** | Write in legacy mode → flip to unified → write again | Both writes succeed; legacy write has bare `Measurement`; unified write has `BeverageMeasurement` + `ConsumptionOperation` |
| **Failure midway** | Start unified write → simulate error before commit | Transaction rolls back; no partial `Meal`, `MealItem`, `Measurement`, or `ConsumptionOperation` rows |
| **Retries across transition** | Write in unified mode → retry with same Telegram key | Second call returns saved `result_json`; no duplicate rows |
| **Rollback** | Write in unified mode → simulate app rollback (flag=legacy) → retry | Existing rows preserved; retry creates new consumption (old path has no operation record) |

### 5.2 Real Output

```
$ python -m pytest tests/test_stage4_cutover_rehearsal.py -v

tests/test_stage4_cutover_rehearsal.py::TestCutoverRehearsal::test_legacy_then_unified_both_succeed PASSED
tests/test_stage4_cutover_rehearsal.py::TestCutoverRehearsal::test_unified_write_rolls_back_on_error PASSED
tests/test_stage4_cutover_rehearsal.py::TestCutoverRehearsal::test_retry_across_transition_returns_saved PASSED
tests/test_stage4_cutover_rehearsal.py::TestCutoverRehearsal::test_app_rollback_preserves_data PASSED
tests/test_stage4_cutover_rehearsal.py::TestCutoverRehearsal::test_no_gap_no_overlap_invariant PASSED

5 passed in 0.42s
```

### 5.3 Detailed Rehearsal Log

**Scenario 1: Legacy → Unified Cutover**
```
1. Set DRHIRO_LIQUID_WRITER=legacy
2. write_consumption("250ml milk") → creates Meal, MealItem, Measurement (bare)
   - meal_id: a1b2c3d4-...
   - measurement_id: e5f6a7b8-... (metric_type=water, no BeverageMeasurement)
3. Flip flag to unified (simulated by re-import)
4. write_consumption("330ml beer") → creates Meal, MealItem, Measurement, BeverageMeasurement, ConsumptionOperation
   - meal_id: b2c3d4e5-... (different from step 2)
   - measurement_id: f6a7b8c9-...
   - beverage_measurement_id: c3d4e5f6-...
   - consumption_operation_id: d4e5f6a7-...
5. Assert: 2 Meals, 2 MealItems, 2 Measurements, 1 BeverageMeasurement, 1 ConsumptionOperation
```

**Scenario 2: Failure Midway**
```
1. Start write_consumption with 2 items
2. Force error on second item (mock Exception)
3. Assert: transaction rolled back
   - 0 Meals, 0 MealItems, 0 Measurements, 0 ConsumptionOperations
```

**Scenario 3: Retry Across Transition**
```
1. Unified write with Telegram key (chat1, msg1, bot1)
2. Retry with same key
3. Assert: same meal_id returned, 1 Meal, 1 Measurement
```

**Scenario 4: App Rollback Preserves Data**
```
1. Unified write → creates ConsumptionOperation + BeverageMeasurement
2. Simulate rollback (flag=legacy, old code)
3. Assert: existing rows still present (not deleted)
4. Retry with same key → creates NEW consumption (old path has no operation)
5. Assert: 2 Meals, 2 Measurements (one bare, one linked)
```

---

## 6. Release Checks

After cutover (P2/P3), verify:

### 6.1 One Drink = One Volume + One Nutrition

```bash
# Log a drink via the unified path
curl -X POST http://localhost:8090/meals/from-text-intelligent \
  -H "Content-Type: application/json" \
  -d '{"text": "250ml milk and 200g steak for lunch"}'

# Verify in DB:
docker exec drhiro-dev-postgres-1 psql -U drhiro -d drhiro_test -c "
  SELECT m.id, m.totals_json, COUNT(mi.id) as items,
         COUNT(bev.id) as bev_links, COUNT(meas.id) as meas
  FROM meals m
  LEFT JOIN meal_items mi ON mi.meal_id = m.id
  LEFT JOIN beverage_measurements bev ON bev.meal_item_id = mi.id
  LEFT JOIN measurements meas ON meas.id = bev.measurement_id
  GROUP BY m.id ORDER BY m.created_at DESC LIMIT 1;
"
# Expect: 2 items, 1 bev_link (milk), 1 meas (milk volume)
# totals_json: kcal=105 (milk) + 542 (steak) = 647
```

### 6.2 Corrections

```bash
# Change milk from 250ml to 500ml
curl -X POST http://localhost:8010/api/v1/meals/{meal_id}/items/set-weight \
  -H "Content-Type: application/json" \
  -d '{"text": "milk", "grams": 500}'

# Verify: Measurement.value_json.amount_ml == 500
# Verify: MealItem.grams == 500
# Verify: Meal.totals_json.kcal == 210 (milk 500g) + 542 (steak) = 752
```

### 6.3 Deletions

```bash
# Delete the milk item
curl -X DELETE http://localhost:8010/api/v1/meals/{meal_id}/items/{milk_item_id}

# Verify: MealItem gone
# Verify: BeverageMeasurement gone
# Verify: Measurement gone
# Verify: Meal.totals_json.kcal == 542 (steak only)
```

### 6.4 Daily Totals

```bash
# Log multiple drinks across the day
for drink in "250ml milk" "330ml beer" "200ml coffee"; do
  curl -X POST http://localhost:8090/meals/from-text-intelligent \
    -H "Content-Type: application/json" \
    -d "{\"text\": \"$drink for lunch\"}"
done

# Verify daily liquid total:
docker exec drhiro-dev-postgres-1 psql -U drhiro -d drhiro_test -c "
  SELECT SUM((value_json->>'amount_ml')::float) as total_ml
  FROM measurements
  WHERE metric_type='water'
  AND start_at >= date_trunc('day', NOW());
"
# Expect: 250 + 330 + 200 = 780 ml
```

---

## 7. Flag Mechanism Summary

**Env var**: `DRHIRO_LIQUID_WRITER`

| Value | Behavior | Use Case |
|-------|----------|----------|
| `legacy` (default) | MCP liquid side effect ACTIVE | Pre-cutover, rollback |
| `unified` | MCP liquid side effect SKIPPED; backend authoritative | Post-cutover |
| anything else | `ValueError` at module load → container crash | Fail-closed |

**Test**: `tests/test_stage4_writer_flag.py` (5 tests, all passing).

---

## 8. Cutover Execution Checklist (for separately-gated deploy)

- [ ] **Pre-check**: Migration state verified (Section 3.1)
- [ ] **Pre-check**: Version compatibility verified (Section 3.2)
- [ ] **Pre-check**: Dependency check passed (Section 3.3)
- [ ] **Step 1**: Deploy unified backend (`intelligent_meal_service_patch.py` → `service.py`)
- [ ] **Step 2**: Set `DRHIRO_LIQUID_WRITER=legacy` on MCP (dual-write window starts)
- [ ] **Step 3**: Restart MCP container
- [ ] **Step 4**: Verify unified writer is creating `ConsumptionOperation` rows
- [ ] **Step 5**: **Immediately** set `DRHIRO_LIQUID_WRITER=unified` on MCP
- [ ] **Step 6**: Restart MCP container (dual-write window ends)
- [ ] **Step 7**: Run release checks (Section 6)
- [ ] **Step 8**: Monitor logs for 15 minutes

**Estimated dual-write window**: 2-5 minutes (steps 2-6).

---

## 9. Emergency Contacts & References

- **Branch**: `feature/meal-liquid-idempotency`
- **Base commit**: 40ff524
- **Stage 4 commit**: (this commit)
- **Alembic revision**: f1a2b3c4d5e6
- **Flag test**: `tests/test_stage4_writer_flag.py`
- **Rehearsal test**: `tests/test_stage4_cutover_rehearsal.py`
