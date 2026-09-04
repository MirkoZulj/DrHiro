#!/usr/bin/env python3
"""Import USDA FoodData Central JSON exports into drHiro's normalized schema.

Reads the JSON files from ~/usda/ and populates:
  - data_sources (ensures the 'usda' row exists)
  - nutrients (upserts all unique nutrient types from both files)
  - foods (one row per USDA food, linked to the 'usda' data source)
  - food_nutrients (per-food nutrient amounts)

Usage:
    # Against dev Postgres (default from config.py or localhost:5435):
    python apps/api/scripts/import_usda.py

    # Against a specific database:
    DRHIRO_DATABASE_URL="postgresql+psycopg://user:pass@host/db" python apps/api/scripts/import_usda.py

    # As a module:
    python -m drhiro_api.scripts.import_usda

Requires: DRHIRO_DATABASE_URL env var (or uses default from config.py).
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
API_SRC = SCRIPT_DIR.parent / "src"
if str(API_SRC) not in sys.path:
    sys.path.insert(0, str(API_SRC))

# ── USDA data source key ──────────────────────────────────────────────────────
USDA_SOURCE_KEY = "usda"

# ── USDA JSON file paths ───────────────────────────────────────────────────────
DATA_DIR = Path("/home/mirko/usda")
FOUNDATION_FILE = DATA_DIR / "FoodData_Central_foundation_food_json_2026-04-30.json"
SR_LEGACY_FILE = DATA_DIR / "FoodData_Central_sr_legacy_food_json_2018-04.json"

# ── USDA nutrient id → drHiro nutrient_code ───────────────────────────────────
USDA_NUTRIENT_MAP: dict[float, str] = {
    1008: "energy",                  # Energy (kcal)
    1003: "protein",                 # Protein (g)
    1005: "carbs",                   # Carbohydrate, by difference (g)
    1004: "fat",                     # Total lipid (fat) (g)
    1079: "fiber",                   # Fiber, total dietary (g)
    1093: "sodium",                  # Sodium, Na (mg)
    269: "sugars",                   # Total Sugars (g)
    957: "energy_atwater_general",   # Energy (Atwater General Factors, kcal)
    958: "energy_atwater_specific",  # Energy (Atwater Specific Factors, kcal)
    268: "energy_kj",                # Energy (kJ)
    298: "fat_nlea",                 # Total fat (NLEA) (g)
    205: "carbs_by_diff",            # Carbohydrate, by difference (alt)
    205.2: "carbs_by_summation",     # Carbohydrate, by summation
    207: "ash",                      # Ash (g)
    202: "nitrogen",                 # Nitrogen (g)
    291: "fiber_total_dietary",      # Fiber, total dietary (alt)
    269.3: "sugars_total",           # Sugars, Total (alt)
    1087: "calcium",                 # Calcium, Ca (mg)
    1089: "iron",                    # Iron, Fe (mg)
    1090: "magnesium",               # Magnesium, Mg (mg)
    1091: "phosphorus",              # Phosphorus, P (mg)
    1092: "potassium",               # Potassium, K (mg)
    1095: "zinc",                    # Zinc, Zn (mg)
    1098: "copper",                  # Copper, Cu (mg)
    1101: "manganese",               # Manganese, Mn (mg)
    1103: "selenium",                # Selenium, Se (µg)
    1100: "iodine",                  # Iodine, I (µg)
    1162: "vitamin_c",              # Vitamin C, total ascorbic acid (mg)
    1165: "thiamin",                 # Thiamin (mg)
    1166: "riboflavin",              # Riboflavin (mg)
    1167: "niacin",                  # Niacin (mg)
    1170: "pantothenic_acid",       # Pantothenic acid (mg)
    1175: "vitamin_b6",             # Vitamin B-6 (mg)
    1177: "vitamin_b12",            # Vitamin B-12 (µg)
    1178: "vitamin_a",              # Vitamin A, IU
    1109: "vitamin_e",              # Vitamin E (alpha-tocopherol) (mg)
    1185: "vitamin_k",              # Vitamin K (phylloquinone) (µg)
    1180: "choline",                 # Choline, total (mg)
    1114: "vitamin_d",              # Vitamin D (D2 + D3) (µg)
    1018: "alcohol",                 # Alcohol, ethyl (g)
    210: "sucrose",                  # Sucrose (g)
    211: "glucose",                  # Glucose (dextrose) (g)
    212: "fructose",                 # Fructose (g)
    213: "lactose",                  # Lactose (g)
    214: "maltose",                  # Maltose (g)
    287: "galactose",                # Galactose (g)
    209: "starch",                   # Starch (g)
    1050: "carbs_summation_alt",     # Carbohydrate, by summation (alt)
    1051: "water",                   # Water (g)
    293: "fiber_aoac",               # Total dietary fiber (AOAC 2011.25) (g)
    293.3: "fiber_hmwdf",            # High Molecular Weight Dietary Fiber (g)
    293.4: "fiber_lmwdf",            # Low Molecular Weight Dietary Fiber (g)
    295: "fiber_soluble",            # Fiber, soluble (g)
    297: "fiber_insoluble",          # Fiber, insoluble (g)
}


def _categorize_nutrient(name: str, unit: str, rank: int) -> str:
    """Best-effort category for a USDA nutrient."""
    name_lower = name.lower()
    if rank is not None and rank <= 400 or "energy" in name_lower:
        return "energy"
    if any(kw in name_lower for kw in ["protein", "nitrogen"]):
        return "macronutrient"
    if any(kw in name_lower for kw in ["carb", "sugar", "fiber", "starch", "sucrose",
                                        "glucose", "fructose", "lactose", "maltose",
                                        "galactose", "alcohol", "water", "ash"]):
        return "macronutrient"
    if any(kw in name_lower for kw in ["fat", "lipid"]):
        return "macronutrient"
    if unit == "mg" and any(kw in name_lower for kw in ["calcium", "iron", "magnesium",
                                                          "phosphorus", "potassium",
                                                          "sodium", "zinc", "copper",
                                                          "manganese", "selenium", "iodine",
                                                          "sulfur", "fluoride", "chromium"]):
        return "mineral"
    if unit == "µg" and any(kw in name_lower for kw in ["selenium", "iodine", "fluoride",
                                                          "chromium"]):
        return "mineral"
    if "vitamin" in name_lower:
        return "vitamin"
    if unit == "mg":
        return "mineral"
    return "other"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_branded_food(description: str) -> bool:
    """Heuristic: detect branded/restaurant foods from the USDA description.

    SR Legacy embeds the brand in the description text ("BRAND, product",
    "DENNY'S, top sirloin steak"); the brandName/brandOwner JSON fields are
    null there. Foundation foods use Title Case ("Butter, salted"), while
    branded rows start with an ALL-CAPS brand name before the first comma.
    """
    import re

    desc = (description or "").strip()
    if not desc:
        return False
    # ALL-CAPS word(s) before the first comma: "KRAFT FOODS, Shake N Bake"
    if re.match(r"^[A-Z][A-Z\s&'.,-]*,", desc):
        return True
    # Known restaurant / chain prefixes (mixed-case variants)
    chain_prefixes = (
        "CRACKER BARREL", "T.G.I. FRIDAY", "DENNY'S", "APPLEBEE'S",
        "CHILI'S", "OUTBACK", "OLIVE GARDEN", "RED LOBSTER", "IHOP",
        "WENDY'S", "MCDONALD", "BURGER KING", "SUBWAY", "TACO BELL",
        "PILLSBURY", "SHAKE N BAKE", "KRAFT", "GENERAL MILLS",
        "CAMPBELL'S", "HEINZ", "BETTY CROCKER", "CONAGRA", "BIRDSEYE",
        "GREEN GIANT", "STOUFFER", "SWANSON", "HUNTS", "JELL-O",
        "OSCAR MAYER", "LEAN CUISINE", "SMART ONES", "HORMEL",
    )
    upper = desc.upper()
    if any(upper.startswith(p) for p in chain_prefixes):
        return True
    # Comma-separated leading token that is a possessive/branded proper noun:
    # a token ending in 'S before a comma where everything before it is caps-ish.
    head = desc.split(",")[0].strip()
    if head and head == head.upper() and len(head) > 2 and any(c.isalpha() for c in head):
        return True
    return False


def _is_liquid_food(description: str, category: str | None) -> bool:
    """Heuristic: detect beverages/liquids from category and description."""
    cat_lower = (category or "").lower()
    desc_lower = description.lower()
    # Category-level: only "Beverages" is a clear liquid signal
    if cat_lower == "beverages":
        return True
    # Description-level: must start with or be primarily a liquid
    liquid_starts = ["beverage", "drink", "juice", "smoothie", "shake",
                     "soda", "cocktail", "formula", "milk", "coffee", "tea",
                     "water ", "lemonade", "punch", "nectar"]
    return any(desc_lower.startswith(lw) for lw in liquid_starts)


def _make_engine(database_url: str | None = None) -> Engine:
    """Create a SQLAlchemy engine from env or config."""
    if database_url:
        return create_engine(database_url, pool_pre_ping=True)
    try:
        from drhiro_api.db import make_engine
        return make_engine()
    except Exception:
        return create_engine(
            "postgresql+psycopg://drhiro:drhiro@localhost:5435/drhiro",
            pool_pre_ping=True,
        )


def load_json(path: Path, key: str) -> list[dict[str, Any]]:
    """Load a USDA JSON file and return the food list."""
    print(f"  Loading {path.name} ...")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    foods = data.get(key, [])
    print(f"    Found {len(foods)} food entries")
    return foods


def import_usda(database_url: str | None = None) -> None:
    """Import USDA Foundation + SR Legacy foods into drHiro's normalized schema."""
    engine = _make_engine(database_url)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    try:
        _do_import(db)
    except Exception as db_err:
        db.rollback()
        raise db_err
    finally:
        db.close()


def _do_import(db) -> None:
    now = _utcnow()

    # ── Step 1: Get or create the USDA data source ─────────────────────────
    row = db.execute(
        text("SELECT id FROM data_sources WHERE source_key = :key"),
        {"key": USDA_SOURCE_KEY},
    ).fetchone()

    if row:
        data_source_id: uuid.UUID = row[0]
        print(f"  USDA data_source found: {data_source_id}")
    else:
        data_source_id = uuid.uuid4()
        db.execute(
            text(
                """INSERT INTO data_sources
                   (id, source_key, source_label, source_url, source_version,
                    license, description, is_active, created_at, updated_at)
                   VALUES (:id, :key, :label, :url, :ver, :lic, :desc, true, :now, :now)"""
            ),
            {
                "id": data_source_id,
                "key": USDA_SOURCE_KEY,
                "label": "USDA FoodData Central",
                "url": "https://fdc.nal.usda.gov",
                "ver": "fdc-v1",
                "lic": "Public Domain",
                "desc": "USDA Foundation, SR Legacy foods",
                "now": now,
            },
        )
        db.flush()
        print(f"  USDA data_source created: {data_source_id}")

    # ── Step 2: Collect all unique nutrients + food data ───────────────────
    all_nutrients: dict[float, dict[str, Any]] = {}  # USDA nutrient id → info
    all_foods: dict[int, dict[str, Any]] = {}       # fdc_id → food data

    datasets = [
        (FOUNDATION_FILE, "FoundationFoods", "Foundation"),
        (SR_LEGACY_FILE, "SRLegacyFoods", "SR Legacy"),
    ]

    for filepath, key, label in datasets:
        foods = load_json(filepath, key)
        t0 = time.time()
        for food in foods:
            if food is None:
                continue
            fdc_id = food.get("fdcId")
            if fdc_id is None:
                continue
            description = food.get("description", "")
            if not description:
                continue

            # Collect nutrients
            for fn in food.get("foodNutrients") or []:
                nut_obj = fn.get("nutrient")
                if not isinstance(nut_obj, dict):
                    continue
                nid = nut_obj.get("id")
                if nid is not None and nid not in all_nutrients:
                    name = nut_obj.get("name", "")
                    number = nut_obj.get("number", "")
                    unit = nut_obj.get("unitName", "")
                    rank = nut_obj.get("rank", 0)
                    code = USDA_NUTRIENT_MAP.get(nid, number or f"usda_{nid}")
                    category = _categorize_nutrient(name, unit, rank)
                    all_nutrients[nid] = {
                        "code": code,
                        "label": name,
                        "unit": unit,
                        "rank": rank,
                        "category": category,
                    }

            # Collect food
            food_category = None
            fc = food.get("foodCategory")
            if isinstance(fc, dict):
                food_category = fc.get("description")

            is_liquid = _is_liquid_food(description, food_category)

            all_foods[fdc_id] = {
                "external_id": str(fdc_id),
                "display_name": description,
                "category": food_category,
                "is_generic": not is_branded_food(description),
                "is_liquid": is_liquid,
                "data_type": label,
                "nutrients": {},
                "food_portions": [],
            }

            # Collect food_nutrients for this food
            for fn in food.get("foodNutrients") or []:
                nut_obj = fn.get("nutrient")
                if not isinstance(nut_obj, dict):
                    continue
                nid = nut_obj.get("id")
                amount = fn.get("amount")
                if nid is not None and amount is not None:
                    all_foods[fdc_id]["nutrients"][nid] = amount

            # Collect food portions
            for p in food.get("foodPortions") or []:
                mu = p.get("measureUnit") or {}
                all_foods[fdc_id]["food_portions"].append({
                    "amount": p.get("amount"),
                    "measure_unit": mu.get("name", ""),
                    "modifier": p.get("modifier", ""),
                    "gram_weight": p.get("gramWeight"),
                    "sequence_number": p.get("sequenceNumber"),
                })

        elapsed = time.time() - t0
        print(f"  {label}: {len(foods)} foods processed in {elapsed:.1f}s")

    print(f"\n  Total unique nutrients: {len(all_nutrients)}")
    print(f"  Total unique foods: {len(all_foods)}")

    # ── Step 3: Upsert nutrients ───────────────────────────────────────────
    print("\n  Upserting nutrients ...")
    existing_codes: dict[str, uuid.UUID] = {}
    for r in db.execute(text("SELECT id, nutrient_code FROM nutrients")).fetchall():
        existing_codes[r[1]] = r[0]

    nutrient_id_by_code: dict[str, uuid.UUID] = {}
    t0 = time.time()
    for nid, info in all_nutrients.items():
        code = info["code"]
        if code in existing_codes:
            nutrient_id_by_code[code] = existing_codes[code]
        elif code in nutrient_id_by_code:
            pass
        else:
            new_id = uuid.uuid4()
            db.execute(
                text(
                    """INSERT INTO nutrients
                       (id, nutrient_code, nutrient_label, unit, category, created_at)
                       VALUES (:id, :code, :label, :unit, :cat, :now)
                       ON CONFLICT (nutrient_code) DO UPDATE SET
                           nutrient_label = EXCLUDED.nutrient_label,
                           unit = EXCLUDED.unit,
                           category = EXCLUDED.category"""
                ),
                {
                    "id": new_id,
                    "code": code,
                    "label": info["label"],
                    "unit": info["unit"],
                    "cat": info["category"],
                    "now": now,
                },
            )
            nutrient_id_by_code[code] = new_id
            existing_codes[code] = new_id
    db.flush()

    # Build USDA nid → drHiro nutrient id map
    usda_nid_to_drhio_nut_id: dict[float, uuid.UUID] = {}
    for r in db.execute(text("SELECT id, nutrient_code FROM nutrients")).fetchall():
        for nid, info in all_nutrients.items():
            if info["code"] == r[1]:
                usda_nid_to_drhio_nut_id[nid] = r[0]
                break

    elapsed = time.time() - t0
    print(f"  Nutrients upserted in {elapsed:.1f}s")

    # ── Step 4: Upsert foods ───────────────────────────────────────────────
    print("\n  Upserting foods ...")
    existing_foods: set[str] = set()
    for r in db.execute(
        text("SELECT external_id FROM foods WHERE data_source_id = :ds_id"),
        {"ds_id": data_source_id},
    ).fetchall():
        existing_foods.add(r[0])

    foods_inserted = 0
    foods_skipped = 0
    t0 = time.time()

    food_values: list[dict[str, Any]] = []
    for fdc_id, info in all_foods.items():
        ext_id = info["external_id"]
        if ext_id in existing_foods:
            foods_skipped += 1
            continue
        food_values.append({
            "id": uuid.uuid4(),
            "data_source_id": data_source_id,
            "external_id": ext_id,
            "display_name": info["display_name"],
            "category": info["category"],
            "is_generic": info["is_generic"],
            "is_liquid": info["is_liquid"],
            "created_at": now,
            "updated_at": now,
        })

    CHUNK = 500
    for i in range(0, len(food_values), CHUNK):
        chunk = food_values[i : i + CHUNK]
        db.execute(
            text(
                """INSERT INTO foods
                   (id, data_source_id, external_id, display_name, category,
                    is_generic, is_liquid, created_at, updated_at)
                   VALUES (:id, :data_source_id, :external_id, :display_name,
                           :category, :is_generic, :is_liquid, :created_at, :updated_at)
                   ON CONFLICT (data_source_id, external_id) DO NOTHING"""
            ),
            chunk,
        )
        foods_inserted += len(chunk)
        if foods_inserted % 2000 == 0:
            print(f"    ... {foods_inserted} foods inserted")
            db.flush()

    db.flush()
    elapsed = time.time() - t0
    print(f"  Foods inserted: {foods_inserted}, skipped (already exist): {foods_skipped} in {elapsed:.1f}s")

    # Re-fetch all food IDs
    print("  Re-fetching food IDs ...")
    food_id_by_ext: dict[str, uuid.UUID] = {}
    for r in db.execute(
        text("SELECT id, external_id FROM foods WHERE data_source_id = :ds_id"),
        {"ds_id": data_source_id},
    ).fetchall():
        food_id_by_ext[r[1]] = r[0]
    print(f"    Found {len(food_id_by_ext)} food IDs")

    # ── Step 5: Upsert food_nutrients ──────────────────────────────────────
    print("\n  Upserting food_nutrients ...")
    existing_fn: set[tuple[str, str]] = set()
    for r in db.execute(
        text(
            """SELECT f.external_id, n.nutrient_code
               FROM food_nutrients fn
               JOIN foods f ON f.id = fn.food_id
               JOIN nutrients n ON n.id = fn.nutrient_id"""
        )
    ).fetchall():
        existing_fn.add((r[0], r[1]))

    fn_inserted = 0
    fn_skipped = 0
    t0 = time.time()

    fn_values: list[dict[str, Any]] = []
    for fdc_id, info in all_foods.items():
        ext_id = info["external_id"]
        food_uuid = food_id_by_ext.get(ext_id)
        if not food_uuid:
            continue
        for usda_nid, amount in info["nutrients"].items():
            nut_uuid = usda_nid_to_drhio_nut_id.get(usda_nid)
            if not nut_uuid:
                continue
            code = all_nutrients[usda_nid]["code"]
            if (ext_id, code) in existing_fn:
                fn_skipped += 1
                continue
            fn_values.append({
                "id": uuid.uuid4(),
                "food_id": food_uuid,
                "nutrient_id": nut_uuid,
                "amount_per_100g": amount,
            })
            existing_fn.add((ext_id, code))

    for i in range(0, len(fn_values), CHUNK):
        chunk = fn_values[i : i + CHUNK]
        db.execute(
            text(
                """INSERT INTO food_nutrients
                   (id, food_id, nutrient_id, amount_per_100g)
                   VALUES (:id, :food_id, :nutrient_id, :amount_per_100g)
                   ON CONFLICT (food_id, nutrient_id) DO UPDATE SET
                       amount_per_100g = EXCLUDED.amount_per_100g"""
            ),
            chunk,
        )
        fn_inserted += len(chunk)
        if fn_inserted % 10000 == 0:
            print(f"    ... {fn_inserted} food_nutrients inserted")
            db.flush()

    db.flush()
    elapsed = time.time() - t0
    print(f"  food_nutrients inserted: {fn_inserted}, skipped (existing): {fn_skipped} in {elapsed:.1f}s")

    # ── Commit and summary ─────────────────────────────────────────────────
    db.commit()
    _print_summary(db, data_source_id)


def _print_summary(db, data_source_id: uuid.UUID) -> None:
    """Print a summary of the import results."""
    print("\n" + "=" * 70)
    print("USDA IMPORT SUMMARY")
    print("=" * 70)

    for table in ["data_sources", "nutrients", "foods", "food_nutrients"]:
        if table == "foods":
            count = db.execute(
                text("SELECT COUNT(*) FROM foods WHERE data_source_id = :ds_id"),
                {"ds_id": data_source_id},
            ).fetchone()[0]
            total = db.execute(text("SELECT COUNT(*) FROM foods")).fetchone()[0]
            print(f"  {table:<25} {count:>10,} rows (USDA) / {total:>10,} total")
        elif table == "food_nutrients":
            count = db.execute(
                text(
                    """SELECT COUNT(*) FROM food_nutrients fn
                       JOIN foods f ON f.id = fn.food_id
                       WHERE f.data_source_id = :ds_id"""
                ),
                {"ds_id": data_source_id},
            ).fetchone()[0]
            total = db.execute(text("SELECT COUNT(*) FROM food_nutrients")).fetchone()[0]
            print(f"  {table:<25} {count:>10,} rows (USDA) / {total:>10,} total")
        else:
            count = db.execute(text(f"SELECT COUNT(*) FROM {table}")).fetchone()[0]
            print(f"  {table:<25} {count:>10,} rows")

    # Top categories
    print("\n  Top food categories:")
    for row in db.execute(
        text(
            """SELECT category, COUNT(*) FROM foods
               WHERE data_source_id = :ds_id
               GROUP BY category ORDER BY COUNT(*) DESC LIMIT 10"""
        ),
        {"ds_id": data_source_id},
    ).fetchall():
        print(f"    {row[0] or '(uncategorized)':<30} {row[1]:>8} foods")

    print("\n  Import complete.")


def main() -> None:
    """Entry point for `python apps/api/scripts/import_usda.py`."""
    database_url = os.environ.get("DRHIRO_DATABASE_URL")
    if database_url:
        print("Using DRHIRO_DATABASE_URL from environment")
    else:
        print("Using default database (config.py or localhost:5435)")

    t_start = time.time()
    import_usda(database_url)
    elapsed = time.time() - t_start
    print(f"\nTotal time: {elapsed:.1f}s")


if __name__ == "__main__":
    main()