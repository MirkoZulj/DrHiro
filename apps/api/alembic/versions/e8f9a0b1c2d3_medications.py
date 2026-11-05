"""Add medication_items, medication_item_events, dose_events (0.9.0, spec §5.2).

Written GUARDED (IF NOT EXISTS / DO $$ ... $$ everywhere) in the same style as
the rest of the chain, chaining from c5d6e7f8a9b0.

Reminder design note (owner decision D1, 2026-11-03): ONE Reminder per time
slot using the existing `_compute_next_due` semantics. medication_items carries
``reminder_ids_json`` (JSON list of reminder ids) rather than the single FK
``reminder_id``, because a multi-time schedule legitimately owns one reminder
per slot and the ids must be addressable to pause/resume/stop them.

Revision ID: e8f9a0b1c2d3
Revises: c5d6e7f8a9b0
Create Date: 2026-11-04
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "e8f9a0b1c2d3"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS medication_items (
            id UUID NOT NULL PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind VARCHAR(16) NOT NULL,
            name VARCHAR(255) NOT NULL,
            brand VARCHAR(255),
            form VARCHAR(24),
            ingredients_json JSON,
            dose_amount FLOAT,
            dose_unit VARCHAR(16),
            schedule_json JSON,
            timezone VARCHAR(64),
            start_date DATE,
            end_date DATE,
            status VARCHAR(16) NOT NULL DEFAULT 'active',
            reason TEXT,
            prescribed_by VARCHAR(255),
            medical_record_id UUID REFERENCES medical_records(id) ON DELETE SET NULL,
            source VARCHAR(24) NOT NULL DEFAULT 'manual',
            label_image_path VARCHAR(512),
            reminder_ids_json JSON,
            stock_quantity FLOAT,
            stock_updated_at TIMESTAMP WITH TIME ZONE,
            notes TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT ck_med_kind CHECK (kind IN ('supplement','medication')),
            CONSTRAINT ck_med_status CHECK (status IN ('draft','active','paused','stopped'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_med_items_user_status ON medication_items (user_id, status)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS medication_item_events (
            id UUID NOT NULL PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            item_id UUID NOT NULL REFERENCES medication_items(id) ON DELETE CASCADE,
            kind VARCHAR(16) NOT NULL,
            effective_date DATE NOT NULL,
            before_json JSON,
            after_json JSON,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_med_item_events_user_day "
        "ON medication_item_events (user_id, effective_date)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dose_events (
            id UUID NOT NULL PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            item_id UUID NOT NULL REFERENCES medication_items(id) ON DELETE CASCADE,
            scheduled_for TIMESTAMP WITH TIME ZONE,
            taken_at TIMESTAMP WITH TIME ZONE NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'taken',
            amount FLOAT,
            unit VARCHAR(16),
            source VARCHAR(24) NOT NULL DEFAULT 'telegram',
            note TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT ck_dose_status CHECK (status IN ('taken','skipped'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dose_events_user_time ON dose_events (user_id, taken_at)"
    )
    # Partial unique index — identical to the ORM Index(postgresql_where=...).
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_dose_slot ON dose_events (item_id, scheduled_for) "
        "WHERE scheduled_for IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dose_events")
    op.execute("DROP TABLE IF EXISTS medication_item_events")
    op.execute("DROP TABLE IF EXISTS medication_items")
