"""Add foods.is_default (curated plain default for a bare generic word).

Revision ID: e6f7a8b9c0d1
Revises: b1c2d3e4f5a6
Create Date: 2026-10-04

Ranking cannot know which variant of a food is "plain" for a bare word: both
"Alcoholic beverage, beer, light" and "...beer, regular, all" are head matches
for "beer". This nullable flag records the human-curated default, set by
scripts/set_food_defaults.py, and preferred by resolve_food() ahead of the
variant/token tie-breaks (behind an exact-name match). NULL = not a default.

Guarded (inspect before add) because this project has two schema lineages:
databases built by the Alembic chain and databases built by ORM create_all.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, None] = "b1c2d3e4f5a6"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

TABLE = "foods"
COLUMN = "is_default"


def _column_exists(bind) -> bool:
    cols = {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}
    return COLUMN in cols


def upgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind):
        return
    op.add_column(TABLE, sa.Column(COLUMN, sa.Boolean(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind):
        return
    op.drop_column(TABLE, COLUMN)