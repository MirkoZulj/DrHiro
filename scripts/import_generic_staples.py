"""Idempotent import of the authoritative generic staples (spec Task 2).

The ranker cannot pick a correct row that does not exist. This imports the
plain generic entries for the foods a user logs by bare name, from USDA
Foundation / SR Legacy only (NEVER Branded), into the `usda` data source.

Run inside the API container (it has USDA_API_KEY and the DB URL):

    docker cp scripts/import_generic_staples.py drhiro-api-1:/tmp/
    docker exec drhiro-api-1 python /tmp/import_generic_staples.py

Idempotent: keyed on (data_source_id='usda', external_id=<fdc id>). Re-running
updates the display_name and nutrient rows in place.

Convention choices (documented in the delivery report):
  * meat/fish staples are imported RAW and, where a cooked variant exists,
    ALSO as cooked with an unambiguous name ("... raw" / "... cooked");
  * grains/bread/potatoes are imported AS-EATEN (cooked rice, white bread,
    baked potato), matching how the user weighs them.
  * a few display names are curated to the plain culinary name (e.g.
    "Bacon, cooked", "Chicken breast, raw") because USDA's raw description
    ("Pork, cured, bacon, cooked, baked") is not a head match for the bare
    word the user types. The NUTRIENT PANEL is the authoritative one.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
import uuid

from drhiro_api.db import SessionLocal
from drhiro_api.models import DataSource, Food, FoodNutrient, Nutrient

# FDC nutrient ids -> our nutrient_code.
FDC_IDS = {
    1008: "energy",   # Energy (kcal)
    2047: "energy",   # Energy (Atwater General Factors) -- Foundation fallback
    2048: "energy",   # Energy (Atwater Specific Factors)
    1003: "protein",
    1005: "carbs",
    1004: "fat",
    1079: "fiber",
    1093: "sodium",
}

# (curated display_name, fdc_id, is_liquid)
STAPLES = [
    # fruit / veg (as eaten, raw)
    ("Bananas, raw", 173944, False),
    ("Apples, raw, with skin", 171688, False),
    ("Peaches, yellow, raw", 169928, False),
    ("Tomatoes, red, ripe, raw, year round average", 170457, False),
    # drinks (as drunk)
    ("Water, tap, drinking", 173647, True),
    ("Water, bottled, generic", 174158, True),
    ("Tea, brewed", 173227, True),
    ("Coffee, brewed", 171890, True),
    ("Espresso coffee, brewed", 171891, True),
    ("Milk, whole", 171265, True),
    # meat / fish (raw AND cooked where both exist)
    ("Bacon, cooked", 167914, False),
    ("Chicken breast, raw", 171077, False),
    ("Chicken breast, cooked", 171477, False),
    ("Beef, composite of trimmed retail cuts, separable lean and fat, raw", 174757, False),
    ("Beef, composite of trimmed retail cuts, separable lean and fat, cooked", 170206, False),
    ("Fish, salmon, Atlantic, farmed, raw", 175167, False),
    ("Fish, salmon, Atlantic, farmed, cooked", 175168, False),
    # grains / bread / potatoes (as eaten)
    ("Rice, white, cooked", 168878, False),
    ("Bread, white", 174924, False),
    ("Potatoes, baked", 170093, False),
    # dairy / fats / pantry
    ("Ice creams, vanilla", 167575, False),
    ("Cheese, cheddar", 328637, False),
    ("Yogurt, plain, whole milk", 171284, False),
    ("Olive oil", 171413, False),
    ("Butter, salted", 173410, False),
    ("Sugars, granulated", 746784, False),
    ("Wheat flour, white, all-purpose, enriched, bleached", 168894, False),
]


def _fetch(fdc_id: int, key: str) -> dict:
    url = f"https://api.nal.usda.gov/fdc/v1/food/{fdc_id}?api_key={key}"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                return json.loads(r.read() or b"{}")
        except Exception as exc:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    return {}


def _nutrients_from_detail(detail: dict) -> dict:
    """{nutrient_code: amount_per_100g}, energy preferring 1008 then Atwater."""
    raw: dict[str, float] = {}
    for x in detail.get("foodNutrients") or []:
        nid = (x.get("nutrient") or {}).get("id")
        amt = x.get("amount")
        code = FDC_IDS.get(nid)
        if code is None or amt is None:
            continue
        # Do not let a later Atwater value overwrite a real 1008 energy.
        if code == "energy" and "energy" in raw:
            continue
        raw[code] = amt
    if raw.get("energy") is None:
        p = raw.get("protein") or 0.0
        c = raw.get("carbs") or 0.0
        f = raw.get("fat") or 0.0
        if p or c or f:
            raw["energy"] = 4.0 * p + 4.0 * c + 9.0 * f
    return raw


def upsert(db, ds: DataSource, nut_ids: dict, name: str, fdc_id: int,
           is_liquid: bool, vals: dict) -> str:
    # Fold non-breaking spaces (and any doubled space they leave behind) so
    # stored names are comparable to typed ones.
    name = re.sub(r" {2,}", " ", (name or "").replace("\u00a0", " "))
    ext = str(fdc_id)
    f = (db.query(Food)
         .filter(Food.data_source_id == ds.id, Food.external_id == ext)
         .first())
    created = False
    adopted = False
    if f is None:
        # Idempotent ACROSS SOURCES: the same FDC food may already exist under
        # another source (e.g. a `drhiro_private` row persisted by the online
        # cascade). Adopt that row instead of minting a second row for the same
        # external_id -- otherwise the import creates a twin, as it did for
        # `Bananas, raw` (drhiro_private/173944 + usda/173944).
        f = (db.query(Food)
             .filter(Food.external_id == ext)
             .order_by(Food.id)
             .first())
        if f is not None:
            adopted = True
    if f is None:
        f = Food(id=uuid.uuid4(), data_source_id=ds.id, external_id=ext,
                 display_name=name, is_generic=True, is_liquid=is_liquid)
        db.add(f)
        db.flush()
        created = True
    else:
        f.display_name = name
        f.is_generic = True
        f.is_liquid = is_liquid

    existing = {fn.nutrient_id: fn for fn in
                db.query(FoodNutrient).filter(FoodNutrient.food_id == f.id).all()}
    for code, val in vals.items():
        nid = nut_ids.get(code)
        if nid is None or val is None:
            continue
        row = existing.get(nid)
        if row is None:
            db.add(FoodNutrient(food_id=f.id, nutrient_id=nid,
                                amount_per_100g=val, amount_per_serving=None))
        else:
            row.amount_per_100g = val
    db.flush()
    if created:
        return "created"
    return "adopted" if adopted else "updated"


def main() -> int:
    key = os.environ.get("USDA_API_KEY")
    if not key:
        print("USDA_API_KEY not set", file=sys.stderr)
        return 2
    db = SessionLocal()
    try:
        ds = db.query(DataSource).filter(DataSource.source_key == "usda").first()
        if ds is None:
            ds = DataSource(source_key="usda", source_label="USDA FoodData Central",
                            source_url="https://fdc.nal.usda.gov", is_active=True)
            db.add(ds)
            db.flush()
        nut_ids = {n.nutrient_code: n.id for n in db.query(Nutrient).all()}

        ok = 0
        for name, fdc_id, is_liquid in STAPLES:
            detail = _fetch(fdc_id, key)
            if not detail:
                print(f"FAIL fetch {fdc_id} {name}")
                continue
            vals = _nutrients_from_detail(detail)
            action = upsert(db, ds, nut_ids, name, fdc_id, is_liquid, vals)
            db.commit()
            ok += 1
            print(f"{action:7} {fdc_id:>8} kcal={vals.get('energy')} {name}")
            time.sleep(0.25)  # stay well inside the 1000/hour limit
        print(f"done: {ok}/{len(STAPLES)} staples present")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())