-- B. Schema Migration: Operation Identity, Item Identity, Durable Result, Liquid-Meal Linkage
-- Branch: feature/meal-liquid-idempotency
-- Target: apps/api/src/drhiro_api/models.py + Alembic migration
-- Single authority: Alembic migration f1a2b3c4d5e6_consumption_idempotency.py
-- This raw SQL is regenerated to be EXACTLY equivalent to the Alembic migration.
--
-- New tables:
--   1. consumption_operations  — idempotency key + durable result (Telegram update_id scope)
--   2. consumption_items       — stable item identity under an operation (meal item OR liquid)
--   3. beverage_measurements   — links a liquid Measurement to its source meal_item (1:1)
--
-- Modified tables:
--   4. meal_items              — add source_operation_id, source_item_id (nullable, for traceability)
--   5. measurements            — add source_operation_id, source_item_id (nullable)
--   6. meals                   — add source_operation_id (nullable)
--
-- All new columns are NULLABLE to avoid breaking existing rows. Backfill is out of scope
-- for the migration; the application populates them on new writes.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. consumption_operations
-- ---------------------------------------------------------------------------
-- One row per logical "user did this" event. For Telegram, the natural key is
-- (bot_id, chat_id, message_id) — message_id alone is NOT unique across chats.
-- For non-Telegram entry points, the caller supplies an idempotency_key.
CREATE TABLE consumption_operations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id),
    -- Source identity
    source              VARCHAR(32) NOT NULL DEFAULT 'telegram',  -- telegram | mcp_direct | api | recipe | import
    -- For Telegram: bot_id + chat_id + message_id
    source_chat_id      VARCHAR(64),
    source_message_id   VARCHAR(64),
    source_bot_id       VARCHAR(128),
    -- For non-Telegram: caller-supplied stable key
    idempotency_key     VARCHAR(255),
    -- What was requested (for audit / debugging)
    raw_text            TEXT,
    -- Durable result: the full confirm response, stored so replays return it
    result_json         JSONB NOT NULL DEFAULT '{}',
    -- Status
    status              VARCHAR(32) NOT NULL DEFAULT 'pending',  -- pending | completed | failed
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Uniqueness: one operation per Telegram message (unconditional, matches ORM)
CREATE UNIQUE INDEX uq_consumption_op_telegram
    ON consumption_operations (user_id, source_bot_id, source_chat_id, source_message_id);

-- Uniqueness: one operation per caller-supplied idempotency key (unconditional, matches ORM)
CREATE UNIQUE INDEX uq_consumption_op_idempotency
    ON consumption_operations (user_id, idempotency_key);

CREATE INDEX ix_consumption_ops_user_created
    ON consumption_operations (user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 2. consumption_items
-- ---------------------------------------------------------------------------
-- Each parsed item (food or beverage) under an operation gets a stable identity.
-- The item_id is deterministic: hash of (operation_id, item_index) or caller-supplied.
-- This lets meal_items and measurements reference the SAME logical drink.
CREATE TABLE consumption_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_id        UUID NOT NULL REFERENCES consumption_operations(id) ON DELETE CASCADE,
    user_id             UUID NOT NULL REFERENCES users(id),
    -- Stable identity within the operation
    item_key            VARCHAR(64) NOT NULL,
    -- Classification
    item_kind           VARCHAR(16) NOT NULL DEFAULT 'food',  -- food | beverage
    -- Parsed canonical data
    display_name        VARCHAR(255) NOT NULL,
    quantity            DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    unit                VARCHAR(32),
    grams               DOUBLE PRECISION,
    volume_ml           DOUBLE PRECISION,
    -- Nutrition per 100g / per 100ml (canonical basis)
    nutrients_per_100   JSONB NOT NULL DEFAULT '{}',
    -- Scaled nutrition (what was actually logged)
    nutrients_scaled    JSONB NOT NULL DEFAULT '{}',
    -- Beverage category (for liquids): water, non_alcoholic, beer, wine, spirits, other_alcohol
    beverage_category   VARCHAR(32),
    -- Meal group this item belongs to
    meal_type           VARCHAR(32),
    -- Provenance / confidence
    source              VARCHAR(32) NOT NULL DEFAULT 'manual',
    confidence          DOUBLE PRECISION,
    -- Links to the actual written rows (populated after write)
    meal_item_id        UUID,  -- references meal_items.id (set after write)
    measurement_id      UUID,  -- references measurements.id (set after write for liquids)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX uq_consumption_item_op_key
    ON consumption_items (operation_id, item_key);

CREATE INDEX ix_consumption_items_user_op
    ON consumption_items (user_id, operation_id);

CREATE INDEX ix_consumption_items_meal_item
    ON consumption_items (meal_item_id) WHERE meal_item_id IS NOT NULL;

CREATE INDEX ix_consumption_items_measurement
    ON consumption_items (measurement_id) WHERE measurement_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. beverage_measurements
-- ---------------------------------------------------------------------------
-- 1:1 link between a liquid Measurement row and the meal_item that represents
-- the same drink. Deleting either side cascades. Updating volume on one side
-- can find the other via this table.
CREATE TABLE beverage_measurements (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id),
    meal_item_id        UUID NOT NULL UNIQUE,  -- the meal_items row
    measurement_id      UUID NOT NULL UNIQUE,  -- the measurements row
    consumption_item_id UUID REFERENCES consumption_items(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_bev_meal_item ON beverage_measurements (meal_item_id);
CREATE INDEX ix_bev_measurement ON beverage_measurements (measurement_id);
CREATE INDEX ix_bev_user ON beverage_measurements (user_id);

-- ---------------------------------------------------------------------------
-- 4. meal_items — add traceability columns
-- ---------------------------------------------------------------------------
ALTER TABLE meal_items
    ADD COLUMN source_operation_id UUID REFERENCES consumption_operations(id),
    ADD COLUMN source_item_id UUID REFERENCES consumption_items(id),
    ADD COLUMN volume_ml DOUBLE PRECISION,
    ADD COLUMN beverage_category VARCHAR(32);

CREATE INDEX ix_meal_items_source_op ON meal_items (source_operation_id) WHERE source_operation_id IS NOT NULL;
CREATE INDEX ix_meal_items_source_item ON meal_items (source_item_id) WHERE source_item_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 5. measurements — add traceability columns
-- ---------------------------------------------------------------------------
ALTER TABLE measurements
    ADD COLUMN source_operation_id UUID REFERENCES consumption_operations(id),
    ADD COLUMN source_item_id UUID REFERENCES consumption_items(id),
    ADD COLUMN meal_item_id UUID REFERENCES meal_items(id);

CREATE INDEX ix_measurements_source_op ON measurements (source_operation_id) WHERE source_operation_id IS NOT NULL;
CREATE INDEX ix_measurements_source_item ON measurements (source_item_id) WHERE source_item_id IS NOT NULL;
CREATE INDEX ix_measurements_meal_item ON measurements (meal_item_id) WHERE meal_item_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 6. meals — add traceability column
-- ---------------------------------------------------------------------------
ALTER TABLE meals
    ADD COLUMN source_operation_id UUID REFERENCES consumption_operations(id);

CREATE INDEX ix_meals_source_op ON meals (source_operation_id) WHERE source_operation_id IS NOT NULL;

COMMIT;
