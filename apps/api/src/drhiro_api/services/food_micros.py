"""Food micronutrient backfill (0.12.0 Part B, spec §4.3.2).

backfill_food_micros(batch=200): foods referenced by confirmed meal items
with no food_micro_backfill row (or failed >7d). Runs the B2 matcher first.
USDA: FDC POST /v1/foods {fdcIds<=20, nutrients: all catalogue ids}.
OFF: product by barcode. Throttle 1 req/s, one 429 retry.
"""
from __future__ import annotations
import os, time
from datetime import datetime, timedelta, timezone
from drhiro_api.models import Food, FoodMicroBackfill, FoodNutrient, Meal, MealItem, Nutrient
from drhiro_nutrition.micros import NUTRIENT_CATALOGUE, SUM_IDS, by_code, convert_value

def _nutrient_map(db):
    return {n.nutrient_code: n for n in db.query(Nutrient).all()}

def _upsert(db, food_id, values: dict, nmap):
    found = 0
    for code, val in values.items():
        nut = nmap.get(code)
        if nut is None or val is None:
            continue
        row = (db.query(FoodNutrient).filter(FoodNutrient.food_id == food_id,
               FoodNutrient.nutrient_id == nut.id).first())
        if row is None:
            db.add(FoodNutrient(food_id=food_id, nutrient_id=nut.id, amount_per_100g=float(val)))
        else:
            row.amount_per_100g = float(val)
        found += 1
    return found

def _fdc_fetch(fdc_ids, fetcher=None, sleep=time.sleep):
    """Return {fdc_id: {nutrient_id: amount}} with 429 retry."""
    ids = list(fdc_ids)
    if fetcher is not None:
        return fetcher(ids)
    import httpx
    key = os.environ.get("USDA_API_KEY", "")
    # NOTE (prod 2026-11-08): FDC POST /v1/foods rejects >25 nutrient ids
    # (400 "nutrients size exceeded upper limit of 25") and a `nutrients` filter
    # in the body returns empty foodNutrients. Fetch full records instead
    # (only fdcIds in the body); the caller picks catalogue ids out of the
    # response by nutrient id.
    for attempt in (1, 2):
        r = httpx.post("https://api.nal.usda.gov/fdc/v1/foods",
                       params={"api_key": key} if key else {}, json={"fdcIds": ids}, timeout=30)
        if r.status_code == 429 and attempt == 1:
            sleep(5); continue
        r.raise_for_status()
        out = {}
        for f in r.json():
            out[int(f["fdcId"])] = {n["nutrient"]["id"]: n.get("amount") for n in f.get("foodNutrients", []) if "amount" in n}
        sleep(1)
        return out
    return {}

def _off_fetch(barcode, fetcher=None):
    if fetcher is not None:
        return fetcher(barcode)
    import httpx
    r = httpx.get(f"https://world.openfoodfacts.org/api/v3/product/{barcode}.json",
                  params={"fields": "code,product_name,nutriments"}, timeout=30)
    if r.status_code != 200:
        return None
    data = r.json()
    return (data.get("product") or {}) if data.get("status") else None

def backfill_food_micros(db, batch: int = 200, fdc_fetcher=None, off_fetcher=None, sleep=time.sleep, run_matcher_first: bool = True) -> dict:
    if run_matcher_first:
        from drhiro_api.services.intake_match import run_matcher
        run_matcher(db)
    nmap = _nutrient_map(db)
    done_ids = {b.food_id for b in db.query(FoodMicroBackfill).all()}
    failed_old = {b.food_id for b in db.query(FoodMicroBackfill).filter(FoodMicroBackfill.status == "failed").all()
                  if b.attempted_at and b.attempted_at < datetime.now(timezone.utc) - timedelta(days=7)}
    todo_ids = (done_ids - failed_old)
    # foods referenced by any confirmed meal item
    foods = {}
    for it in db.query(MealItem).join(Meal, Meal.id == MealItem.meal_id).filter(Meal.status == "confirmed").all():
        ref = getattr(it, "micro_food_id", None) or it.food_catalog_item_id
        if ref is None:
            continue
        try:
            f = db.get(Food, __import__("uuid").UUID(str(ref)))
        except Exception:
            f = None
        if f is not None and f.id not in done_ids:
            foods[f.id] = f
    pending = [f for f in foods.values() if f.id not in todo_ids or f.id in failed_old][:batch]
    report = {"attempted": 0, "done": 0, "no_source": 0, "failed": 0}
    usda_foods = [f for f in pending if (f.data_source.source_key if f.data_source else "") == "usda" and str(f.external_id).isdigit()]
    off_foods = [f for f in pending if (f.data_source.source_key if f.data_source else "") in ("off", "open_food_facts") and f.barcode]
    others = [f for f in pending if f not in usda_foods and f not in off_foods]
    # USDA in chunks of 20
    for i in range(0, len(usda_foods), 20):
        chunk = usda_foods[i:i+20]
        try:
            fetched = _fdc_fetch([int(f.external_id) for f in chunk], fetcher=fdc_fetcher, sleep=sleep)
        except Exception as e:
            for f in chunk:
                _record(db, f, "usda", "failed", 0, str(e)); report["attempted"] += 1; report["failed"] += 1
            continue
        for f in chunk:
            report["attempted"] += 1
            raw = fetched.get(int(f.external_id))
            if not raw:
                _record(db, f, "usda", "failed", 0, "no FDC response"); report["failed"] += 1; continue
            values = {}
            for c in NUTRIENT_CATALOGUE:
                if c.code in SUM_IDS:
                    total_g = sum(raw.get(i, 0) or 0 for i in c.fdc_ids)
                    if any(raw.get(i) is not None for i in c.fdc_ids):
                        values[c.code] = convert_value(total_g, c.fdc_unit, c.code)
                else:
                    for fid in c.fdc_ids:
                        if raw.get(fid) is not None:
                            values[c.code] = convert_value(raw[fid], c.fdc_unit, c.code)
                            break
            found = _upsert(db, f.id, values, nmap)
            _record(db, f, "usda", "done", found, None); report["done"] += 1
    for f in off_foods:
        report["attempted"] += 1
        try:
            product = _off_fetch(f.barcode, fetcher=off_fetcher)
        except Exception as e:
            _record(db, f, "off", "failed", 0, str(e)); report["failed"] += 1; continue
        if not product:
            _record(db, f, "off", "no_source", 0, None); report["no_source"] += 1; continue
        nutr = product.get("nutriments") or {}
        values = {}
        for c in NUTRIENT_CATALOGUE:
            if c.off_key:
                v = nutr.get(f"{c.off_key}_100g", nutr.get(c.off_key))
                if v is not None:
                    values[c.code] = convert_value(float(v), "g", c.code)
        found = _upsert(db, f.id, values, nmap)
        _record(db, f, "off", "done", found, None); report["done"] += 1
    for f in others:
        report["attempted"] += 1
        _record(db, f, "none", "no_source", 0, None); report["no_source"] += 1
    db.commit()
    return report

def _record(db, food, source, status, found, error):
    row = db.query(FoodMicroBackfill).filter(FoodMicroBackfill.food_id == food.id).first()
    if row is None:
        db.add(FoodMicroBackfill(food_id=food.id, source=source, status=status, nutrients_found=found,
                                 attempted_at=datetime.now(timezone.utc), error=error))
    else:
        row.source, row.status, row.nutrients_found = source, status, found
        row.attempted_at = datetime.now(timezone.utc); row.error = error
    db.flush()

def status_summary(db) -> dict:
    from sqlalchemy import func
    rows = db.query(FoodMicroBackfill.status, func.count()).group_by(FoodMicroBackfill.status).all()
    return {"counts": {s: n for s, n in rows}, "total": sum(n for _, n in rows)}
