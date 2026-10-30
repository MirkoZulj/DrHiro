"""Add nutrient-resolution provenance columns to consumption_items.

Revision ID: c9d0e1f2a3b4
Revises: a1b2c3d4e5f7
Create Date: 2026-09-09

The ORM (ConsumptionItem) declares four Stage-2 nutrient-resolution fields:
  - nutrient_basis         ('per_100_g' | 'per_100_ml')
  - resolution_source      ('db' | 'external' | 'unmatched')
  - food_catalog_item_id   (FK -> foods.id)
  - nutrition_complete     (bool; UNKNOWN != KNOWN-ZERO marker)

The consuming table was created by f1a2b3c4d5e6 WITHOUT these columns, so a
database upgraded purely through the revision chain could not support the new
ORM reads/writes. This migration adds them as an incremental upgrade so an
EXISTING (pre-change) database is compatible. nutrition_complete is NOT NULL
with server_default true so existing rows are treated as complete (they were
logged before provenance tracking; there is no basis to claim otherwise).

Rollback (downgrade) drops the columns. This is additive-only; it does NOT
touch or destroy any recorded consumption or idempotency rows.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, None] = 'a1b2c3d4e5f7'
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column('consumption_items', sa.Column('nutrient_basis', sa.String(16), nullable=True))
    op.add_column('consumption_items', sa.Column('resolution_source', sa.String(32), nullable=True))
    op.add_column('consumption_items', sa.Column(
        'food_catalog_item_id', postgresql.UUID(as_uuid=True), nullable=True
    ))
    # nutrition_complete is NOT NULL; existing rows default to true.
    op.add_column(
        'consumption_items',
        sa.Column('nutrition_complete', sa.Boolean(), nullable=False, server_default=sa.text('true')),
    )
    op.create_index('ix_consumption_items_nutrition_complete', 'consumption_items',
                    ['nutrition_complete'])
    op.create_index('ix_consumption_items_resolution_source', 'consumption_items',
                    ['resolution_source'])


def downgrade() -> None:
    op.drop_index('ix_consumption_items_resolution_source', table_name='consumption_items')
    op.drop_index('ix_consumption_items_nutrition_complete', table_name='consumption_items')
    op.drop_column('consumption_items', 'nutrition_complete')
    op.drop_column('consumption_items', 'food_catalog_item_id')
    op.drop_column('consumption_items', 'resolution_source')
    op.drop_column('consumption_items', 'nutrient_basis')
