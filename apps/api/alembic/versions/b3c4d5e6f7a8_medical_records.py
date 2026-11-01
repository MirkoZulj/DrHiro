"""Add medical_records (transcribed medical documents from Telegram photos).

The alembic chain was repaired on 2026-10-05, but this migration is written
GUARDED (IF NOT EXISTS everywhere) so it can also be applied by hand with the
direct Operations(MigrationContext) pattern, or re-run safely after a
create_all-built database.

Revision ID: b3c4d5e6f7a8
Revises: f910f1548389
Create Date: 2026-11-01
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "b3c4d5e6f7a8"
down_revision = "f910f1548389"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS medical_records (
            id UUID NOT NULL PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title VARCHAR(255),
            content TEXT NOT NULL,
            source_type VARCHAR(32) NOT NULL DEFAULT 'telegram_photo',
            recorded_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_medical_records_user_id ON medical_records (user_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_medical_records_user_recorded "
        "ON medical_records (user_id, recorded_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_medical_records_user_recorded")
    op.execute("DROP INDEX IF EXISTS ix_medical_records_user_id")
    op.execute("DROP TABLE IF EXISTS medical_records")
