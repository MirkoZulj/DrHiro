"""recipes table

Revision ID: d5e6f7a8b9c0
Revises: c4d8e2f6a9b1
Create Date: 2026-08-26 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd5e6f7a8b9c0'
down_revision = 'c4d8e2f6a9b1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'recipes',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('total_weight_g', sa.Float(), nullable=False),
        sa.Column('ingredients_json', sa.JSON(), nullable=False),
        sa.Column('nutrition_per_100g', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    )
    op.create_index('ix_recipes_user_name', 'recipes', ['user_id', 'name'])


def downgrade():
    op.drop_index('ix_recipes_user_name')
    op.drop_table('recipes')
