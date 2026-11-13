"""Questionnarium (0.12.0): patient health Q&A history.

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
"""
from alembic import op

revision = "f3a4b5c6d7e8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS questionnarium_questions (
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            question TEXT NOT NULL,
            answer TEXT,
            model VARCHAR(16) NOT NULL,
            profile VARCHAR(64),
            sources_json JSON DEFAULT '[]'::json,
            context_json JSON,
            latency_ms INTEGER,
            status VARCHAR(16) NOT NULL DEFAULT 'ok',
            error TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            PRIMARY KEY (id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_questionnarium_user_created "
        "ON questionnarium_questions (user_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_questionnarium_user_created")
    op.execute("DROP TABLE IF EXISTS questionnarium_questions")
