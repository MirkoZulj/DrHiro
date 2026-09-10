"""Canonical food-domain baseline (data_sources, nutrients, foods, food_nutrients,
food_brands, food_ingredients).

Revision ID: b2f3c4d5e6f7
Revises:
Create Date: 2026-09-09

This migration makes Alembic self-sufficient for a FRESH database. Previously
the six food-domain tables were provisioned OUT-OF-BAND (ORM create_all at app
bootstrap plus the USDA import script), NOT by the Alembic chain — yet later
migrations (c4d8e2f6a9b1 food_resolution_rules, and the consumption feature)
carry FOREIGN KEYs to `foods`. A fresh `alembic upgrade head` therefore failed
at c4d8e2f6a9b1 because `foods` did not exist.

This baseline (down_revision=None) creates those six tables, and 3c003
(initial_canonical_schema) is re-based to run after it. The column DDL is
transcribed VERBATIM from the authoritative production schema
(drhiro-postgres-1, drhiro DB) so there is ZERO drift from an existing
real-data database. These tables carry NO server-side defaults in production
(ids + timestamps are supplied by the ORM / import scripts), so none are added
here.

`activities` is intentionally NOT in this baseline: it carries a FK to
`users.id`, and `users` is created by 3c003 (which now runs after this
baseline). It is provided by a separate migration that follows 3c003.

Downgrade drops the six tables (reverse FK order).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b2f3c4d5e6f7'
down_revision: Union[str, None] = None
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    # ---- data_sources (no FK) ------------------------------------------------
    op.create_table(
        'data_sources',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('source_key', sa.String(32), nullable=False, unique=True),
        sa.Column('source_label', sa.String(255), nullable=False),
        sa.Column('source_url', sa.String(512), nullable=True),
        sa.Column('source_version', sa.String(32), nullable=True),
        sa.Column('license', sa.String(255), nullable=True),
        sa.Column('description', sa.Text, nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )

    # ---- nutrients (no FK) ---------------------------------------------------
    op.create_table(
        'nutrients',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('nutrient_code', sa.String(32), nullable=False, unique=True),
        sa.Column('nutrient_label', sa.String(255), nullable=False),
        sa.Column('unit', sa.String(16), nullable=False),
        sa.Column('category', sa.String(16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )

    # ---- foods (FK data_sources) --------------------------------------------
    op.create_table(
        'foods',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('data_source_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('data_sources.id'), nullable=False),
        sa.Column('external_id', sa.String(255), nullable=False),
        sa.Column('display_name', sa.String(255), nullable=False),
        sa.Column('display_name_en', sa.String(255), nullable=True),
        sa.Column('barcode', sa.String(64), nullable=True),
        sa.Column('category', sa.String(64), nullable=True),
        sa.Column('is_generic', sa.Boolean(), nullable=False),
        sa.Column('is_liquid', sa.Boolean(), nullable=False),
        sa.Column('serving_grams', sa.Float(), nullable=True),
        sa.Column('serving_unit', sa.String(32), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('data_source_id', 'external_id', name='uq_food_source_external'),
    )
    op.create_index('ix_foods_barcode', 'foods', ['barcode'])
    op.create_index('ix_foods_display_name', 'foods', ['display_name'])

    # ---- food_nutrients (FK foods, nutrients) ------------------------------
    op.create_table(
        'food_nutrients',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('food_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('foods.id'), nullable=False),
        sa.Column('nutrient_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('nutrients.id'), nullable=False),
        sa.Column('amount_per_100g', sa.Float(), nullable=True),
        sa.Column('amount_per_serving', sa.Float(), nullable=True),
        sa.UniqueConstraint('food_id', 'nutrient_id', name='uq_food_nutrient'),
    )
    op.create_index('ix_food_nutrients_nutrient', 'food_nutrients', ['nutrient_id'])

    # ---- food_brands (FK foods) ---------------------------------------------
    op.create_table(
        'food_brands',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('food_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('foods.id'), nullable=False),
        sa.Column('brand_name', sa.String(255), nullable=True),
        sa.Column('brand_owner', sa.String(255), nullable=True),
        sa.Column('packaging_text', sa.Text, nullable=True),
        sa.Column('origin_country', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )

    # ---- food_ingredients (FK foods) ----------------------------------------
    op.create_table(
        'food_ingredients',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('food_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('foods.id'), nullable=False),
        sa.Column('ingredient_text', sa.Text, nullable=False),
        sa.Column('ingredient_order', sa.Integer(), nullable=False),
        sa.Column('is_allergen', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('food_ingredients')
    op.drop_table('food_brands')
    op.drop_index('ix_food_nutrients_nutrient', table_name='food_nutrients')
    op.drop_table('food_nutrients')
    op.drop_index('ix_foods_display_name', table_name='foods')
    op.drop_index('ix_foods_barcode', table_name='foods')
    op.drop_table('foods')
    op.drop_table('nutrients')
    op.drop_table('data_sources')
