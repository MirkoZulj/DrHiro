"""Add medical_encounters + scan columns on medical_records.

Encounter-centric medical records: a clinical encounter groups one or more
transcribed documents. medical_records gains a nullable encounter_id FK and a
nullable source_image_path for the stored scan image.

Written GUARDED (IF NOT EXISTS / IF EXISTS everywhere) so it can be applied by
hand with the direct Operations(MigrationContext) pattern, or re-run safely
after a create_all-built database.

Revision ID: c5d6e7f8a9b0
Revises: b3c4d5e6f7a8
Create Date: 2026-11-02
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "c5d6e7f8a9b0"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS medical_encounters (
            id UUID NOT NULL PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            encounter_date DATE NOT NULL,
            facility VARCHAR(255),
            title VARCHAR(255),
            notes TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_medical_encounters_user_id ON medical_encounters (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_medical_encounters_encounter_date ON medical_encounters (encounter_date)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_medical_encounters_user_date "
        "ON medical_encounters (user_id, encounter_date)"
    )
    # Additive columns on medical_records (guarded).
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'medical_records' AND column_name = 'encounter_id'
            ) THEN
                ALTER TABLE medical_records
                    ADD COLUMN encounter_id UUID REFERENCES medical_encounters(id) ON DELETE CASCADE;
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'medical_records' AND column_name = 'source_image_path'
            ) THEN
                ALTER TABLE medical_records ADD COLUMN source_image_path VARCHAR(512);
            END IF;
        END $$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_medical_records_encounter_id ON medical_records (encounter_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_medical_records_encounter_id")
    op.execute("ALTER TABLE medical_records DROP COLUMN IF EXISTS source_image_path")
    op.execute("ALTER TABLE medical_records DROP COLUMN IF EXISTS encounter_id")
    op.execute("DROP TABLE IF EXISTS medical_encounters")
