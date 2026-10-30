"""Read-only food-resolution acceptance harness (spec Task 1).

Prints, for a fixed phrase list, what `resolve_food` returns: the resolved
name, its data source, and its energy (kcal) per 100 g. Run this before and
after any ranking change so the two are directly comparable and verifiable by
a third party.

Usage:
    docker exec drhiro-api-1 python tests/resolution_report.py

The live user is Mirko (id below). NEVER use db.query(User).first(): that
returns the seed user 'Alice' and has already caused a phantom-bug hunt.
"""

from __future__ import annotations

import sys

from drhiro_api.db import SessionLocal
from drhiro_api.food_search import resolve_food
from drhiro_api.models import DataSource, Food, Nutrient

# Mirko, timezone Europe/Zagreb. Hard-coded on purpose (see docstring).
LIVE_USER_ID = "0bfad360-9938-4216-8abd-b44d69e2003f"

# Bare words the user actually types, plus the phrases he logs.
ACCEPTANCE = [
    "banana", "bacon", "water", "beef", "egg", "chicken breast",
    "ice cream", "espresso coffee", "peach", "cola", "coke", "salt", "honey",
]

LOGGED = [
    "Egg, whole, raw, fresh", "Corn bread (store-bought)", "beer",
    "Croissants, cheese", "Kefir, lowfat, plain, LIFEWAY", "turkey ham",
    "Peppers, bell, green, raw", "Pepper, banana, raw",
    "Water, bottled, non-carbonated", "sourdough bread", "kefir",
    "Cheese, feta", "Cream, heavy", "Peach, raw",
    "Alcoholic beverage, wine, table, red", "Lettuce, iceberg, raw", "beef",
    "Beef, tenderloin steak, raw", "Carrots, raw", "Ham, minced",
    "Cheese, cream", "chicken breast", "Emmental cheese", "white rice",
    "WHITE BREAD", "pancetta", "cottage cheese",
]


def _kcal_by_food(db):
    """{food_id: energy_per_100g} for every food with an energy nutrient."""
    energy_id = (
        db.query(Nutrient.id).filter(Nutrient.nutrient_code == "energy").scalar()
    )
    from drhiro_api.models import FoodNutrient

    out = {}
    if energy_id is None:
        return out
    for fid, amt in db.query(FoodNutrient.food_id, FoodNutrient.amount_per_100g).filter(
        FoodNutrient.nutrient_id == energy_id
    ):
        out[str(fid)] = amt
    return out


def main() -> int:
    db = SessionLocal()
    try:
        src = {d.id: d.source_key for d in db.query(DataSource).all()}
        kcal = _kcal_by_food(db)

        phrases = ACCEPTANCE + [p for p in LOGGED if p not in ACCEPTANCE]
        print(f"{'query':38} {'#':>1} {'resolved name':52} {'source':14} {'kcal/100g':>9}")
        print("-" * 120)
        for q in phrases:
            res = resolve_food(db, q, limit=3, user_id=LIVE_USER_ID)
            if not res.matches:
                print(f"{q:38} {'-':>1} {'<no match>':52} {'':14} {'':>9}")
                continue
            for i, m in enumerate(res.matches):
                f = m.food
                sk = src.get(f.data_source_id, "?")
                k = kcal.get(str(f.id))
                kk = f"{k:.1f}" if k is not None else "-"
                tag = " *AMB*" if (i == 0 and res.ambiguous) else ""
                print(f"{q if i == 0 else '':38} {i+1:>1} {f.display_name[:52]:52} {sk:14} {kk:>9}{tag}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())