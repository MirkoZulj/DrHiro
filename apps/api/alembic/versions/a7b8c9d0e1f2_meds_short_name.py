"""Meds edit (0.11.0): medication_items.short_name abbreviation column.

Guarded (IF NOT EXISTS), nullable — NULL means "fall back to the computed
12-char short_name". Non-destructive; downgrade leaves the column.

Revision ID: a7b8c9d0e1f2
Revises: d1e0f2a3b4c5
"""
from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "d1e0f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE medication_items ADD COLUMN IF NOT EXISTS short_name VARCHAR(32)")


def downgrade() -> None:
    # Non-destructive: column intentionally left in place.
    pass
