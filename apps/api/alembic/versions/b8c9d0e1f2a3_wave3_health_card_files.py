"""Wave 3 (0.10.x): health_profiles + generated_files.

health_profiles: one row per user, the self-maintained health card (spec §6).
generated_files: metadata + status for every downloadable artefact — PDF
reports, data-export bundles, system backups, restore tests (spec §2.2).

Written GUARDED like the rest of the chain. Revision ID: b8c9d0e1f2a3.

Revision ID: b8c9d0e1f2a3
Revises: d7e8f9a0b1c2
Create Date: 2026-11-04
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "b8c9d0e1f2a3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS health_profiles (
            id UUID NOT NULL PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            blood_type VARCHAR(8),
            organ_donor VARCHAR(8),
            conditions_json JSON DEFAULT '[]'::json,
            allergies_json JSON DEFAULT '[]'::json,
            intolerances_json JSON DEFAULT '[]'::json,
            implants_devices_json JSON DEFAULT '[]'::json,
            vaccinations_json JSON DEFAULT '[]'::json,
            surgeries_json JSON DEFAULT '[]'::json,
            family_history_json JSON DEFAULT '[]'::json,
            emergency_contacts_json JSON DEFAULT '[]'::json,
            care_team_json JSON DEFAULT '[]'::json,
            insurance_json JSON,
            notes TEXT,
            reviewed_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT uq_health_profile_user UNIQUE (user_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS generated_files (
            id UUID NOT NULL PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind VARCHAR(32) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'queued',
            rel_path VARCHAR(512),
            filename VARCHAR(255),
            mime VARCHAR(64),
            size_bytes INTEGER,
            sha256 VARCHAR(64),
            params_json JSON,
            analysis_run_id UUID REFERENCES analysis_runs(id) ON DELETE SET NULL,
            error TEXT,
            expires_at TIMESTAMP WITH TIME ZONE,
            job_id VARCHAR(64),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            id VARCHAR(32) NOT NULL PRIMARY KEY,
            ai_backend_url VARCHAR(512),
            model_name VARCHAR(255),
            ai_api_key TEXT,
            telegram_bot_token TEXT,
            telegram_allowed_username VARCHAR(255),
            telegram_allowed_user_id VARCHAR(32),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_generated_files_user_kind "
        "ON generated_files (user_id, kind, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app_settings")
    op.execute("DROP TABLE IF EXISTS generated_files")
    op.execute("DROP TABLE IF EXISTS health_profiles")
