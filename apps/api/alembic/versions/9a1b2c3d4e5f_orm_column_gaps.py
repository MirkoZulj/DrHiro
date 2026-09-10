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


def upgrade() -> None:
    op.add_column('users', sa.Column('basal_metabolism_kcal', sa.Float(), nullable=True))
    op.add_column('food_catalog_items', sa.Column(
        'food_id', postgresql.UUID(as_uuid=True),
        sa.ForeignKey('foods.id', name='fk_food_catalog_items_food_id'), nullable=True))


def downgrade() -> None:
    op.drop_constraint('fk_food_catalog_items_food_id', 'food_catalog_items', type_='foreignkey')
    op.drop_column('food_catalog_items', 'food_id')
    op.drop_column('users', 'basal_metabolism_kcal')
