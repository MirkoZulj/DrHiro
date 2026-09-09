"""Consumption idempotency + item identity + beverage linkage.

Revision ID: f1a2b3c4d5e6
Revises: e7f8a9b0c1d2
Create Date: 2026-09-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'e7f8a9b0c1d2'
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    # ---------------------------------------------------------------------------
    # 1. consumption_operations
    # ---------------------------------------------------------------------------
    op.create_table(
        'consumption_operations',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('source', sa.String(32), nullable=False, server_default='telegram'),
        sa.Column('source_chat_id', sa.String(64), nullable=True),
        sa.Column('source_message_id', sa.String(64), nullable=True),
        sa.Column('source_bot_id', sa.String(128), nullable=True),
        sa.Column('idempotency_key', sa.String(255), nullable=True),
        sa.Column('raw_text', sa.Text, nullable=True),
        sa.Column('result_json', sa.JSON, nullable=False, server_default='{}'),
        sa.Column('status', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()')),
    )
    op.create_index('uq_consumption_op_telegram', 'consumption_operations',
                    ['user_id', 'source_bot_id', 'source_chat_id', 'source_message_id'],
                    unique=True,
                    postgresql_where=sa.text("source = 'telegram'"))
    op.create_index('uq_consumption_op_idempotency', 'consumption_operations',
                    ['user_id', 'idempotency_key'],
                    unique=True,
                    postgresql_where=sa.text("idempotency_key IS NOT NULL AND source != 'telegram'"))
    op.create_index('ix_consumption_ops_user_created', 'consumption_operations',
                    ['user_id', sa.text('created_at DESC')])

    # ---------------------------------------------------------------------------
    # 2. consumption_items
    # ---------------------------------------------------------------------------
    op.create_table(
        'consumption_items',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('operation_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('consumption_operations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('item_key', sa.String(64), nullable=False),
        sa.Column('item_kind', sa.String(16), nullable=False, server_default='food'),
        sa.Column('display_name', sa.String(255), nullable=False),
        sa.Column('quantity', sa.Float, nullable=False, server_default='1.0'),
        sa.Column('unit', sa.String(32), nullable=True),
        sa.Column('grams', sa.Float, nullable=True),
        sa.Column('volume_ml', sa.Float, nullable=True),
        sa.Column('nutrients_per_100', sa.JSON, nullable=False, server_default='{}'),
        sa.Column('nutrients_scaled', sa.JSON, nullable=False, server_default='{}'),
        sa.Column('beverage_category', sa.String(32), nullable=True),
        sa.Column('meal_type', sa.String(32), nullable=True),
        sa.Column('source', sa.String(32), nullable=False, server_default='manual'),
        sa.Column('confidence', sa.Float, nullable=True),
        sa.Column('meal_item_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('measurement_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()')),
    )
    op.create_index('uq_consumption_item_op_key', 'consumption_items',
                    ['operation_id', 'item_key'], unique=True)
    op.create_index('ix_consumption_items_user_op', 'consumption_items',
                    ['user_id', 'operation_id'])
    op.create_index('ix_consumption_items_meal_item', 'consumption_items',
                    ['meal_item_id'])
    op.create_index('ix_consumption_items_measurement', 'consumption_items',
                    ['measurement_id'])

    # ---------------------------------------------------------------------------
    # 3. beverage_measurements
    # ---------------------------------------------------------------------------
    op.create_table(
        'beverage_measurements',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('meal_item_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('measurement_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('consumption_item_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()')),
    )
    op.create_index('uq_bev_meal_item', 'beverage_measurements',
                    ['meal_item_id'], unique=True)
    op.create_index('uq_bev_measurement', 'beverage_measurements',
                    ['measurement_id'], unique=True)
    op.create_index('ix_bev_user', 'beverage_measurements', ['user_id'])

    # ---------------------------------------------------------------------------
    # 4. meal_items — add traceability columns
    # ---------------------------------------------------------------------------
    op.add_column('meal_items', sa.Column('source_operation_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('meal_items', sa.Column('source_item_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('meal_items', sa.Column('volume_ml', sa.Float, nullable=True))
    op.add_column('meal_items', sa.Column('beverage_category', sa.String(32), nullable=True))
    op.create_index('ix_meal_items_source_op', 'meal_items', ['source_operation_id'])
    op.create_index('ix_meal_items_source_item', 'meal_items', ['source_item_id'])

    # ---------------------------------------------------------------------------
    # 5. measurements — add traceability columns
    # ---------------------------------------------------------------------------
    op.add_column('measurements', sa.Column('source_operation_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('measurements', sa.Column('source_item_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('measurements', sa.Column('meal_item_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index('ix_measurements_source_op', 'measurements', ['source_operation_id'])
    op.create_index('ix_measurements_source_item', 'measurements', ['source_item_id'])
    op.create_index('ix_measurements_meal_item', 'measurements', ['meal_item_id'])

    # ---------------------------------------------------------------------------
    # 6. meals — add traceability column
    # ---------------------------------------------------------------------------
    op.add_column('meals', sa.Column('source_operation_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index('ix_meals_source_op', 'meals', ['source_operation_id'])


def downgrade() -> None:
    # Reverse order
    op.drop_index('ix_meals_source_op', table_name='meals')
    op.drop_column('meals', 'source_operation_id')

    op.drop_index('ix_measurements_meal_item', table_name='measurements')
    op.drop_index('ix_measurements_source_item', table_name='measurements')
    op.drop_index('ix_measurements_source_op', table_name='measurements')
    op.drop_column('measurements', 'meal_item_id')
    op.drop_column('measurements', 'source_item_id')
    op.drop_column('measurements', 'source_operation_id')

    op.drop_index('ix_meal_items_source_item', table_name='meal_items')
    op.drop_index('ix_meal_items_source_op', table_name='meal_items')
    op.drop_column('meal_items', 'beverage_category')
    op.drop_column('meal_items', 'volume_ml')
    op.drop_column('meal_items', 'source_item_id')
    op.drop_column('meal_items', 'source_operation_id')

    op.drop_table('beverage_measurements')
    op.drop_table('consumption_items')
    op.drop_table('consumption_operations')
