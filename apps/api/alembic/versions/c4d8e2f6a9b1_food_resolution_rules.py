"""food_resolution_rules table

Revision ID: c4d8e2f6a9b1
Revises: a1b2c3d4e5f6
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c4d8e2f6a9b1"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "food_resolution_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("original_pattern", sa.Text(), nullable=False),
        sa.Column("resolved_food_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("foods.id"), nullable=True),
        sa.Column("rule_text", sa.Text(), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False,
                  server_default="user"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("scope IN ('user', 'global')", name="ck_food_rules_scope"),
    )
    op.create_index("ix_food_rules_user", "food_resolution_rules",
                    ["user_id", "active"])


def downgrade() -> None:
    op.drop_index("ix_food_rules_user", table_name="food_resolution_rules")
    op.drop_table("food_resolution_rules")
