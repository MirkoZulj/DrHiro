"""Add ORM-declared columns missing from the Alembic-built schema.

Revision ID: 9a1b2c3d4e5f
Revises: c9d0e1f2a3b4
Create Date: 2026-09-09

The Alembic migrations that create `users` and `food_catalog_items` are stale
relative to the ORM (and to the production schema, which was built by ORM
create_all). A database built purely from the Alembic chain was therefore
missing two ORM-declared columns:

  - users.basal_metabolism_kcal            (double precision, nullable) — Katch-McArdle BMR
  - food_catalog_items.food_id             (uuid, nullable, FK -> foods.id)

This is exactly the class of gap the R4/R6 acceptance testing is meant to catch
(an Alembic-built DB must support the real ORM reads/writes). The column types
are transcribed from the authoritative production schema.

Guarded (inspect before alter) because this project has two schema lineages:
databases built by the Alembic chain and databases built by ORM create_all
(which already contain these columns). Both upgrade and downgrade no-op safely
if the column is already present/absent.

Downgrade drops both columns (additive reverse — preserves unrelated data).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '9a1b2c3d4e5f'
down_revision: Union[str, None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

TABLE_USERS = 'users'
TABLE_FOOD_CATALOG = 'food_catalog_items'
COL_BASAL = 'basal_metabolism_kcal'
COL_FOOD_ID = 'food_id'


def _columns(bind, table: str) -> set:
    """Column names of `table`, or empty set if the table is absent."""
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {c['name'] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    # users.basal_metabolism_kcal
    if TABLE_USERS in sa.inspect(bind).get_table_names():
        if COL_BASAL not in _columns(bind, TABLE_USERS):
            op.add_column(TABLE_USERS, sa.Column(COL_BASAL, sa.Float(), nullable=True))
    # food_catalog_items.food_id
    if TABLE_FOOD_CATALOG in sa.inspect(bind).get_table_names():
        if COL_FOOD_ID not in _columns(bind, TABLE_FOOD_CATALOG):
            op.add_column(TABLE_FOOD_CATALOG, sa.Column(
                COL_FOOD_ID, postgresql.UUID(as_uuid=True),
                sa.ForeignKey('foods.id', name='fk_food_catalog_items_food_id'), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    # food_catalog_items.food_id (drop FK first, then column)
    if COL_FOOD_ID in _columns(bind, TABLE_FOOD_CATALOG):
        op.drop_constraint('fk_food_catalog_items_food_id', TABLE_FOOD_CATALOG, type_='foreignkey')
        op.drop_column(TABLE_FOOD_CATALOG, COL_FOOD_ID)
    # users.basal_metabolism_kcal
    if COL_BASAL in _columns(bind, TABLE_USERS):
        op.drop_column(TABLE_USERS, COL_BASAL)
