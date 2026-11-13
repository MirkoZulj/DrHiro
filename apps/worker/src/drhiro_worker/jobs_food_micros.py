"""Worker job: food micronutrient backfill (0.12.0 Part B, spec §4.3.2)."""
from __future__ import annotations
from drhiro_api.db import SessionLocal
from drhiro_api.services.food_micros import backfill_food_micros as _run

def backfill_food_micros(batch: int = 200) -> dict:
    db = SessionLocal()
    try:
        return _run(db, batch=batch)
    finally:
        db.close()
