"""Add analysis_runs (0.9.0 wave 2, spec §3.4).

Deterministic analytics runs. Findings stay in result_json — immutable per
run, stable ids comparable across runs, so no separate findings table in v1.

Written GUARDED like the rest of the chain. NOTE: down_revision is the
medications migration (this wave ships 0.9.0 = series + meds + analytics
together; the spec's "0.9.1 head" was written for the original release plan).

Revision ID: d7e8f9a0b1c2
Revises: e8f9a0b1c2d3
Create Date: 2026-11-04
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "d7e8f9a0b1c2"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS analysis_runs (
            id UUID NOT NULL PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind VARCHAR(16) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'queued',
            requested_via VARCHAR(16) NOT NULL DEFAULT 'web',
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            params_json JSON NOT NULL,
            idempotency_key VARCHAR(64) NOT NULL,
            data_fingerprint VARCHAR(64),
            engine_version VARCHAR(16),
            thresholds_version VARCHAR(32),
            result_json JSON,
            narrative_text TEXT,
            narrative_status VARCHAR(16) NOT NULL DEFAULT 'none',
            job_id VARCHAR(64),
            started_at TIMESTAMP WITH TIME ZONE,
            finished_at TIMESTAMP WITH TIME ZONE,
            error TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT uq_analysis_run_idem UNIQUE (user_id, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_analysis_runs_user_created "
        "ON analysis_runs (user_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS analysis_runs")
