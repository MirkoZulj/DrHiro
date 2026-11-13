"""0.12.0 Part B: meal_items micro match columns + ensure catalogue seeded.

Revision ID: e2f3a4b5c6d7
Revises: d1e0f2a3b4c5
"""
from alembic import op

revision = "e2f3a4b5c6d7"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("ALTER TABLE meal_items ADD COLUMN IF NOT EXISTS micro_food_id UUID REFERENCES foods(id) ON DELETE SET NULL")
    op.execute("ALTER TABLE meal_items ADD COLUMN IF NOT EXISTS micro_match_confidence DOUBLE PRECISION")
    op.execute("CREATE INDEX IF NOT EXISTS ix_meal_items_micro_food ON meal_items (micro_food_id)")

def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_meal_items_micro_food")
    op.execute("ALTER TABLE meal_items DROP COLUMN IF EXISTS micro_match_confidence")
    op.execute("ALTER TABLE meal_items DROP COLUMN IF EXISTS micro_food_id")
