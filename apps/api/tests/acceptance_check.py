"""Focused acceptance + stability check (evidence for the D1-D3 report).

Run: docker exec drhiro-api-1 python tests/acceptance_check.py
"""

from __future__ import annotations

from drhiro_api.db import SessionLocal
from drhiro_api.food_search import resolve_food
from drhiro_api.models import DataSource, Food, FoodNutrient, Nutrient

LIVE_USER_ID = "0bfad360-9938-4216-8abd-b44d69e2003f"

ACCEPTANCE = ["banana", "bacon", "water", "beef", "egg", "chicken breast",
              "ice cream", "espresso coffee", "peach", "cola", "coke", "salt", "honey"]

EXACT = ["Pepper, banana, raw", "Corn bread (store-bought)",
         "Kefir, lowfat, plain, LIFEWAY", "Whole grain sourdough bread",
         "egg, whole, raw", "Egg, whole, raw, fresh", "Peppers, bell, green, raw",
         "Cheese, feta", "Cream, heavy", "Alcoholic beverage, wine, table, red",
         "Lettuce, iceberg, raw", "Beef, tenderloin steak, raw", "Carrots, raw",
         "Ham, minced", "Cheese, cream", "Croissants, cheese"]

PINNED = ["beer", "pepper", "wine", "sourdough bread", "prosciutto", "kefir",
          "special dark, dark chocolate", "broccoli", "beer mustard"]


def main() -> int:
    db = SessionLocal()
    try:
        src = {d.id: d.source_key for d in db.query(DataSource).all()}
        energy_id = db.query(Nutrient.id).filter(Nutrient.nutrient_code == "energy").scalar()
        kcal = {}
        if energy_id is not None:
            for fid, amt in db.query(FoodNutrient.food_id, FoodNutrient.amount_per_100g).filter(
                    FoodNutrient.nutrient_id == energy_id):
                kcal[str(fid)] = amt

        print(f"{'query':38} {'resolved name':52} {'source':14} {'kcal':>7} {'stable5':>8} {'tier':>4} amb")
        print("-" * 130)
        bad = 0
        for q in ACCEPTANCE + EXACT + PINNED:
            names = []
            first = None
            for _ in range(5):
                r = resolve_food(db, q, limit=3, user_id=LIVE_USER_ID)
                n = r.best.display_name if r.best else "<none>"
                names.append(n)
                first = r
            stable = "YES" if len(set(names)) == 1 else "NO"
            if stable == "NO":
                bad += 1
            f = first.best
            sk = src.get(f.data_source_id, "?") if f else ""
            k = kcal.get(str(f.id)) if f else None
            kk = f"{k:.1f}" if k is not None else "-"
            print(f"{q:38} {(f.display_name if f else '<none>')[:52]:52} {sk:14} {kk:>7} {stable:>8} "
                  f"{first.matches[0].tier if first.matches else -1:>4} {first.ambiguous}")
        print("-" * 130)
        print(f"unstable queries: {bad}")
        return 1 if bad else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())