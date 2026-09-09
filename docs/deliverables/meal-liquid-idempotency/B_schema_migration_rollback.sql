-- B. Schema Migration ROLLBACK
-- Reverses B_schema_migration_up.sql in dependency order.
-- Run this to undo the migration if the cutover fails.

BEGIN;

-- 6. meals
DROP INDEX IF EXISTS ix_meals_source_op;
ALTER TABLE meals DROP COLUMN IF EXISTS source_operation_id;

-- 5. measurements
DROP INDEX IF EXISTS ix_measurements_meal_item;
DROP INDEX IF EXISTS ix_measurements_source_item;
DROP INDEX IF EXISTS ix_measurements_source_op;
ALTER TABLE measurements DROP COLUMN IF EXISTS meal_item_id;
ALTER TABLE measurements DROP COLUMN IF EXISTS source_item_id;
ALTER TABLE measurements DROP COLUMN IF EXISTS source_operation_id;

-- 4. meal_items
DROP INDEX IF EXISTS ix_meal_items_source_item;
DROP INDEX IF EXISTS ix_meal_items_source_op;
ALTER TABLE meal_items DROP COLUMN IF EXISTS beverage_category;
ALTER TABLE meal_items DROP COLUMN IF EXISTS volume_ml;
ALTER TABLE meal_items DROP COLUMN IF EXISTS source_item_id;
ALTER TABLE meal_items DROP COLUMN IF EXISTS source_operation_id;

-- 3. beverage_measurements
DROP INDEX IF EXISTS ix_bev_user;
DROP INDEX IF EXISTS ix_bev_measurement;
DROP INDEX IF EXISTS ix_bev_meal_item;
DROP TABLE IF EXISTS beverage_measurements;

-- 2. consumption_items
DROP INDEX IF EXISTS ix_consumption_items_measurement;
DROP INDEX IF EXISTS ix_consumption_items_meal_item;
DROP INDEX IF EXISTS ix_consumption_items_user_op;
DROP INDEX IF EXISTS uq_consumption_item_op_key;
DROP TABLE IF EXISTS consumption_items;

-- 1. consumption_operations
DROP INDEX IF EXISTS ix_consumption_ops_user_created;
DROP INDEX IF EXISTS uq_consumption_op_idempotency;
DROP INDEX IF EXISTS uq_consumption_op_telegram;
DROP TABLE IF EXISTS consumption_operations;

COMMIT;
