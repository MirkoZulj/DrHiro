"""Add provenance columns to daily_aggregates (mifitness-import).

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-10-05

``daily_aggregates`` had no provenance column, so a row's origin was only a
convention.  With the Mi Fitness gap-filling importer we must be able to say
"Health Connect leads": the export must never overwrite a day another source
already owns.  ``source_provider`` (and a deterministic ``source_record_id``)
make that enforceable in data.

Both columns are nullable and additive.  Legacy rows (there are none today,
but a DB built by ORM ``create_all`` may already have them) are backfilled to
``'computed'`` so the origin is explicit.

Guarded (inspect before add) because this project has two schema lineages:
databases built by the Alembic chain and databases built by ORM create_all.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, None] = "e6f7a8b9c0d1"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

TABLE = "daily_aggregates"
COLUMNS = ("source_provider", "source_record_id")


def _existing_columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind)
    if "source_provider" not in cols:
        op.add_column(TABLE, sa.Column("source_provider", sa.String(length=32), nullable=True))
    if "source_record_id" not in cols:
        op.add_column(TABLE, sa.Column("source_record_id", sa.String(length=255), nullable=True))
    # Backfill any pre-existing row (there are none today) so the origin is
    # explicit rather than NULL.
    op.execute(
        f"UPDATE {TABLE} SET source_provider = 'computed' "
        "WHERE source_provider IS NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind)
    if "source_record_id" in cols:
        op.drop_column(TABLE, "source_record_id")
    if "source_provider" in cols:
        op.drop_column(TABLE, "source_provider")