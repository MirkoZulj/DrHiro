# F. Safe Deployment / Rollback Instructions

**Branch**: `feature/meal-liquid-idempotency`
**Date**: 2026-09-09

---

## CRITICAL: Coordinated Cutover

The old MCP liquid side effect (`sse_server.py` lines 1636–1698) and the new backend writer **MUST NOT** both record the same drink. The cutover must be atomic from the user's perspective.

### Pre-Deployment Checklist

1. **Database migration** (run FIRST, backward-compatible):
   ```bash
   # On the VPS production Postgres:
   psql $DATABASE_URL -f docs/deliverables/meal-liquid-idempotency/B_schema_migration_up.sql
   ```
   - All new columns are NULLABLE → existing rows are unaffected
   - New tables are empty → no performance impact
   - This can be applied days before the code cutover

2. **Verify migration**:
   ```bash
   psql $DATABASE_URL -c "\dt consumption_*"
   psql $DATABASE_URL -c "\d beverage_measurements"
   ```

3. **Deploy the new intelligent-meal service** (the patched `service.py`):
   - The new `confirm_meal` writes meal + beverage in ONE transaction
   - The new `write_consumption` function handles the unified path
   - Verify the service starts: `curl http://localhost:8090/healthz`

4. **Deploy the patched MCP server** (`sse_server.py`):
   - The liquid auto-detection block (lines 1636–1698) is **REMOVED**
   - `log_meal_intelligent` now relies on the backend to write beverages
   - No hard-coded user UUID or URL remains

5. **Verify no double-writing**:
   - Send a test message: `"250ml milk and 200g steak"`
   - Check: 1 meal row, 2 meal_items, 1 measurement (for milk), 1 beverage_measurement link
   - Check: 1 consumption_operation row with status='completed'

### Deployment Order

```
1. Apply DB migration (backward-compatible, no downtime)
2. Deploy new intelligent-meal service (atomic write path)
3. Deploy new MCP server (liquid block removed)
4. Monitor for 5 minutes
5. Verify idempotency: replay the same Telegram message → returns same meal_id
```

### Rollback Procedure

If anything goes wrong:

1. **Revert the MCP server** to the previous version (restore the liquid block)
2. **Revert the intelligent-meal service** to the previous version
3. **Rollback the DB migration**:
   ```bash
   psql $DATABASE_URL -f docs/deliverables/meal-liquid-idempotency/B_schema_migration_rollback.sql
   ```
4. **Verify**: existing functionality works as before

### Rollback Safety

- The rollback SQL drops all new tables and columns
- Existing data in `meals`, `meal_items`, `measurements` is UNTOUCHED
- The old MCP liquid block will resume writing measurements (with fresh UUIDs) — these are additive and don't conflict with the new tables
- After rollback, you can re-attempt the migration later

### Feature Flag (Recommended)

To make the cutover reversible without a full rollback, consider a feature flag:

```python
# In sse_server.py or config:
ENABLE_UNIFIED_CONSUMPTION = os.environ.get("ENABLE_UNIFIED_CONSUMPTION", "false").lower() == "true"

# In log_meal_intelligent:
if not ENABLE_UNIFIED_CONSUMPTION:
    # Run the old liquid auto-detection block
    ...
```

This allows:
- Deploy with `ENABLE_UNIFIED_CONSUMPTION=false` (old behavior)
- Toggle to `true` when ready
- Toggle back to `false` for instant rollback

### Monitoring

After deployment, watch:
- `SELECT COUNT(*) FROM consumption_operations WHERE status = 'failed'` → should be 0
- `SELECT COUNT(*) FROM beverage_measurements` → should match liquid measurement count
- `SELECT COUNT(*) FROM measurements WHERE source_provider = 'consumption'` → should equal beverage_measurement count
- Error logs for `IntegrityError` on `uq_consumption_op_telegram` (indicates retry working correctly)
