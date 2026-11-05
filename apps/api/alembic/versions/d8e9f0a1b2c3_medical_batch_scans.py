"""Add source_image_paths_json to medical_records (multi-photo batch scans).

A medical photo batch is logged as ONE record. The legacy single column
``source_image_path`` holds the first scan; this JSON list holds ALL scans of
the batch in upload order so no photo is lost when a batch has >1 image.

Written GUARDED (IF NOT EXISTS everywhere) so it can be applied by hand with
the direct Operations(MigrationContext) pattern, or re-run safely after a
create_all-built database.

Revision ID: d8e9f0a1b2c3
Revises: b8c9d0e1f2a3
Create Date: 2026-11-05
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "d8e9f0a1b2c3"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'medical_records' AND column_name = 'source_image_paths_json'
            ) THEN
                ALTER TABLE medical_records ADD COLUMN source_image_paths_json JSON;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE medical_records DROP COLUMN IF EXISTS source_image_paths_json")
