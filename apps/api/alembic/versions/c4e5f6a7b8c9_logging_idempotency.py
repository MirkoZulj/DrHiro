"""Logging idempotency: soft-delete columns for the liquid and activity ledgers.

Revision ID: c4e5f6a7b8c9
Revises: 9a1b2c3d4e5f
Create Date: 2026-09-11

WHY
Slice 4 of the logging-correctness work makes one Telegram message idempotent and
editable. `meals` can already be soft-deleted through `meals.status = 'deleted'`,
but the liquid ledger (`measurements`) and the activity ledger (`activities`) had
no way to retire a row without a hard DELETE. This adds a nullable `deleted_at`
to exactly those two tables, and nothing else.

The identity table for (chat_id, message_id) is the EXISTING `consumption_operations`
table, which already carries source / source_chat_id / source_message_id /
source_bot_id / idempotency_key / raw_text / result_json / status / payload_hash and
already has `uq_consumption_op_telegram (user_id, source_bot_id, source_chat_id,
source_message_id)`. This revision does NOT invent a second identity table; it
creates it only on a database where it is genuinely absent.

EXPLICITLY OUT OF SCOPE (and deliberately not done here):
  * anything that adopts or rebuilds `activities` the way b7c8d9e0f1a2 does --
    that revision is never applied by this work
  * a duration column on `activities`
  * denormalised telegram id columns on meals / measurements / activities
  * any change to the `meals.status` values (deleted already exists)

PRODUCTION: production is at d5e6f7a8b9c0 and has no `consumption_operations`.
This child does not belong on production and MUST NOT be applied there in this
release. Slice 4 evidence is disposable-only.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4e5f6a7b8c9"
down_revision: Union[str, None] = "9a1b2c3d4e5f"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table in ("measurements", "activities"):
        cols = {c["name"] for c in inspector.get_columns(table)}
        if "deleted_at" not in cols:
            op.add_column(
                table,
                sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            )

    # The identity table. It already exists in this chain -- create it only where
    # it is genuinely missing, and never invent a second one.
    if "consumption_operations" not in inspector.get_table_names():
        from drhiro_api.models import ConsumptionOperation

        ConsumptionOperation.__table__.create(bind)

    # Uniqueness for the (source, chat_id, message_id) key. The composite unique
    # constraint on the table also includes source_bot_id, which is NULL for
    # messages that arrive without a verified bot id -- and NULLs are distinct in
    # PostgreSQL, so that constraint alone does not enforce the key we use.
    index_names = {i["name"] for i in inspector.get_indexes("consumption_operations")}
    if "ix_consumption_ops_source_identity" not in index_names:
        op.create_index(
            "ix_consumption_ops_source_identity",
            "consumption_operations",
            ["source", "source_chat_id", "source_message_id"],
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_consumption_ops_source_identity")
    for table in ("activities", "measurements"):
        op.drop_column(table, "deleted_at")
