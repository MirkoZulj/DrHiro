"""Add payload_hash to consumption_operations for B3 identity propagation.

Revision ID: a1b2c3d4e5f7
Revises: f1a2b3c4d5e6
Create Date: 2026-09-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f7'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column('consumption_operations',
                  sa.Column('payload_hash', sa.String(64), nullable=True))
    op.create_index('ix_consumption_ops_payload_hash', 'consumption_operations',
                    ['payload_hash'])


def downgrade() -> None:
    op.drop_index('ix_consumption_ops_payload_hash', table_name='consumption_operations')
    op.drop_column('consumption_operations', 'payload_hash')
