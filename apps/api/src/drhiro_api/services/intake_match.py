"""Meal-item -> canonical food matcher (0.12.0 Part B, spec B2).

match_item_to_food: name similarity >= 0.85 (SequenceMatcher on normalised
names) and kcal per 100 g within ±25% when both sides know it.
"""
from __future__ import annotations
import re
import unicodedata
from difflib import SequenceMatcher

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()

def _food_kcal_per100(db, food):
    from drhiro_api.models import FoodNutrient, Nutrient
    row = (db.query(FoodNutrient).join(Nutrient, Nutrient.id == FoodNutrient.nutrient_id)
           .filter(FoodNutrient.food_id == food.id, Nutrient.nutrient_code == "energy").first())
    return row.amount_per_100g if row else None

def _item_kcal_per100(item):
    n = item.nutrients_json or {}
    if isinstance(n, dict) and isinstance(n.get("kcal"), (int, float)):
        basis = item.grams or item.volume_ml
        if basis:
            return float(n["kcal"]) * 100.0 / basis
    return None

def match_item_to_food(db, display_name: str, item=None, limit: int = 200):
    """Return (food, confidence) or (None, score)."""
    from drhiro_api.models import Food
    foods = db.query(Food).limit(limit).all()
    item_kcal = _item_kcal_per100(item) if item is not None else None
    best, best_score = None, 0.0
    for f in foods:
        score = similarity(display_name, f.display_name)
        if score <= best_score:
            continue
        if score < 0.85:
            continue
        if item_kcal is not None:
            fk = _food_kcal_per100(db, f)
            if fk is not None and fk > 0:
                if abs(item_kcal - fk) / fk > 0.25:
                    continue
        best, best_score = f, score
    return best, best_score

def run_matcher(db, days: int = 365, limit: int = 500) -> dict:
    """Backfill micro_food_id on confirmed meal items (365d window)."""
    from datetime import date, timedelta
    from drhiro_api.models import Meal, MealItem
    from drhiro_api.services.day_boundary import nutrition_date
    cutoff = date.today() - timedelta(days=days)
    matched = skipped = 0
    items = (db.query(MealItem).join(Meal, Meal.id == MealItem.meal_id)
             .filter(Meal.status == "confirmed", MealItem.micro_food_id.is_(None)).all())
    for it in items[:limit]:
        meal = db.get(Meal, it.meal_id)
        if meal is None:
            continue
        # nutrition_date needs user tz; approximate with meal's user via eaten_at date
        if meal.eaten_at.date() < cutoff:
            skipped += 1; continue
        food, score = match_item_to_food(db, it.display_name, item=it)
        if food is not None:
            it.micro_food_id = food.id
            it.micro_match_confidence = round(score, 4)
            matched += 1
    db.commit()
    return {"matched": matched, "skipped_old": skipped}
