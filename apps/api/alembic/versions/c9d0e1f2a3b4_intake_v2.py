"""Intake v2 (0.11.0-a, spec §2): unit_label, disable med reminders, paused->active, seed nutrients, food_micro_backfill.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
"""
from alembic import op
import uuid

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None

# spec §4.1 catalogue (code, label_en, canonical unit, group)
_NUTRIENTS = [
 ("vitamin_a","Vitamin A (RAE)","mcg","vitamin"),("vitamin_c","Vitamin C","mg","vitamin"),
 ("vitamin_d","Vitamin D","mcg","vitamin"),("vitamin_e","Vitamin E","mg","vitamin"),
 ("vitamin_k","Vitamin K","mcg","vitamin"),("thiamin","B1 Thiamin","mg","vitamin"),
 ("riboflavin","B2 Riboflavin","mg","vitamin"),("niacin","B3 Niacin","mg","vitamin"),
 ("pantothenic","B5 Pantothenic acid","mg","vitamin"),("vitamin_b6","B6","mg","vitamin"),
 ("biotin","B7 Biotin","mcg","vitamin"),("folate","B9 Folate (DFE)","mcg","vitamin"),
 ("vitamin_b12","B12","mcg","vitamin"),("choline","Choline","mg","vitamin"),
 ("calcium","Calcium","mg","mineral"),("phosphorus","Phosphorus","mg","mineral"),
 ("magnesium","Magnesium","mg","mineral"),("potassium","Potassium","mg","mineral"),
 ("sodium","Sodium","mg","mineral"),("iron","Iron","mg","trace"),("zinc","Zinc","mg","trace"),
 ("copper","Copper","mcg","trace"),("manganese","Manganese","mg","trace"),
 ("selenium","Selenium","mcg","trace"),("iodine","Iodine","mcg","trace"),
 ("chromium","Chromium","mcg","trace"),("molybdenum","Molybdenum","mcg","trace"),
 ("epa_dha","Omega-3 EPA+DHA","mg","fatty_acid"),("ala","Omega-3 ALA","g","fatty_acid"),
]

def upgrade() -> None:
    op.execute("ALTER TABLE medication_items ADD COLUMN IF NOT EXISTS unit_label VARCHAR(24)")
    op.execute("UPDATE reminders SET enabled = false WHERE type = 'medication' AND id::text IN (SELECT jsonb_array_elements_text(reminder_ids_json::jsonb) FROM medication_items WHERE reminder_ids_json IS NOT NULL)")
    op.execute("UPDATE medication_items SET status = 'active' WHERE status = 'paused'")
    for code, label, unit, group in _NUTRIENTS:
        op.execute(f"INSERT INTO nutrients (id, nutrient_code, nutrient_label, unit, category, created_at) VALUES ('{uuid.uuid4()}', '{code}', '{label}', '{unit}', '{group}', now()) ON CONFLICT (nutrient_code) DO NOTHING")
    op.execute("""CREATE TABLE IF NOT EXISTS food_micro_backfill (
            id UUID NOT NULL PRIMARY KEY,
            food_id UUID NOT NULL REFERENCES foods(id) ON DELETE CASCADE,
            source VARCHAR(16) NOT NULL,
            status VARCHAR(16) NOT NULL,
            nutrients_found INTEGER NOT NULL DEFAULT 0,
            attempted_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            error TEXT,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT uq_food_micro_backfill_food UNIQUE (food_id)
        )""")

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS food_micro_backfill")
    # unit_label column intentionally left (non-destructive); no data reversal for reminders/paused
