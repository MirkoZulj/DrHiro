"""app_settings table — singleton instance-global runtime settings store.

Revision ID: e7f8a9b0c1d2
Revises: d5e6f7a8b9c0
Create Date: 2026-09-04 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "e7f8a9b0c1d2"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "app_settings",
        sa.Column("id", sa.String(16), primary_key=True, server_default=sa.text("'singleton'")),
        sa.Column("ai_backend_url", sa.String(512), nullable=True),
        sa.Column("model_name", sa.String(255), nullable=True),
        sa.Column("ai_api_key", sa.Text(), nullable=True),
        sa.Column("telegram_bot_token", sa.Text(), nullable=True),
        sa.Column("telegram_allowed_username", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    # Seed the singleton row so reads/writes always find it.
    op.execute(
        "INSERT INTO app_settings (id, created_at, updated_at) "
        "VALUES ('singleton', NOW(), NOW()) "
        "ON CONFLICT (id) DO NOTHING"
    )


def downgrade():
    op.drop_table("app_settings")
