"""Create log_write_fingerprints (server-side free-text write dedupe).

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-10-02

Backs the content-fingerprint guard on the free-text logging path. The MCP
bridge forwards no per-message Telegram identity, so every model-initiated
create_meal_from_text call writes an ``anon-<uuid>`` order and a looping agent
double-wrote the same line (3000 ml logged where the truth was 750 ml). This
table remembers the fingerprint of each anon write so a repeat inside a short
window is suppressed.

Guarded (inspect before create) because this project has two schema lineages:
databases built by the Alembic chain and databases built by ORM create_all.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "a0b1c2d3e4f5"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

TABLE = "log_write_fingerprints"


def _table_exists(bind) -> bool:
    return TABLE in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind):
        return
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_log_fingerprints_user_source_fp", TABLE,
                    ["user_id", "source", "fingerprint"])
    op.create_index("ix_log_fingerprints_user_seen", TABLE,
                    ["user_id", "seen_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind):
        return
    op.drop_index("ix_log_fingerprints_user_seen", table_name=TABLE)
    op.drop_index("ix_log_fingerprints_user_source_fp", table_name=TABLE)
    op.drop_table(TABLE)
