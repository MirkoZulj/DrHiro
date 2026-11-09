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

def _fdc_search(query, fetcher=None, sleep=time.sleep, page_size=10):
    """Live USDA FDC name search. Returns [{fdc_id, description}]."""
    if fetcher is not None:
        return fetcher(query)
    import httpx
    key = os.environ.get("USDA_API_KEY", "")
    try:
        r = httpx.get("https://api.nal.usda.gov/fdc/v1/foods/search",
                      params={"query": query, "pageSize": page_size,
                              "dataType": ["Foundation", "SR Legacy", "Survey (FNDDS)"],
                              **({"api_key": key} if key else {})}, timeout=30)
        if r.status_code != 200:
            return []
        out = [{"fdc_id": int(f["fdcId"]), "description": f.get("description", "")}
               for f in r.json().get("foods", []) if f.get("fdcId") and f.get("description")]
        sleep(1)
        return out
    except Exception:
        return []

def _values_from_raw(raw):
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
    return values

def _best_fdc_match(display_name, item=None, fdc_search=None, fdc_fetcher=None,
                    sleep=time.sleep, threshold=0.70):
    """Best FDC candidate by name similarity, kcal-gated when item kcal known.

    Returns (fdc_id, description, score) or None.
    """
    from drhiro_api.services.intake_match import similarity, _item_kcal_per100
    cands = _fdc_search(display_name, fetcher=fdc_search, sleep=sleep)
    scored = sorted(((similarity(display_name, c["description"]), c) for c in cands),
                    key=lambda t: t[0], reverse=True)
    item_kcal = _item_kcal_per100(item) if item is not None else None
    for score, cand in scored:
        if score < threshold:
            continue
        if item_kcal is not None:
            fetched = _fdc_fetch([cand["fdc_id"]], fetcher=fdc_fetcher, sleep=sleep)
            raw = fetched.get(cand["fdc_id"]) or {}
            fk = raw.get(1008)  # Energy kcal
            if fk is not None and fk > 0 and abs(item_kcal - fk) / fk > 0.25:
                continue
            return cand["fdc_id"], cand["description"], score
        return cand["fdc_id"], cand["description"], score
    return None

def resolve_via_fdc_search(db, display_name, item=None, fdc_search=None,
                           fdc_fetcher=None, sleep=time.sleep):
    """Search FDC live, create/reuse the usda Food row, upsert its micros.

    Returns (food, score, nutrients_found) or (None, score, 0).
    Caller owns the transaction (flush only).
    """
    from drhiro_api.models import DataSource
    best = _best_fdc_match(display_name, item=item, fdc_search=fdc_search,
                           fdc_fetcher=fdc_fetcher, sleep=sleep)
    if best is None:
        return None, 0.0, 0
    fdc_id, description, score = best
    ds = db.query(DataSource).filter(DataSource.source_key == "usda").first()
    if ds is None:
        ds = DataSource(source_key="usda", source_label="USDA")
        db.add(ds); db.flush()
    food = (db.query(Food).filter(Food.data_source_id == ds.id,
                                  Food.external_id == str(fdc_id)).first())
    if food is None:
        food = Food(data_source_id=ds.id, external_id=str(fdc_id),
                    display_name=description)
        db.add(food); db.flush()
    fetched = _fdc_fetch([fdc_id], fetcher=fdc_fetcher, sleep=sleep)
    raw = fetched.get(fdc_id)
    if not raw:
        return food, score, 0
    found = _upsert(db, food.id, _values_from_raw(raw), _nutrient_map(db))
    _record(db, food, "usda_search", "done", found, None)
    return food, score, found

def attach_micro_food(db, mi) -> None:
    """Best-effort micro_food_id attach at meal-log time (never fails the log).

    1. Local catalogue match over the FULL foods table (name token search).
    2. If no local match and FDC search is enabled (USDA_API_KEY present or
       DRHIRO_FDC_SEARCH_AT_LOG=1), search FDC live, create the usda Food
       row + micros, and point the item at it.
    All failures are swallowed — logging must never break on micro matching.
    Caller owns the transaction.
    """
    import logging, os
    log = logging.getLogger("drhiro.food_micros")
    try:
        from drhiro_api.services.intake_match import match_item_to_food
        food, score = match_item_to_food(db, mi.display_name, item=mi)
        if food is not None:
            mi.micro_food_id = food.id
            mi.micro_match_confidence = round(score, 4)
            return
    except Exception:
        log.exception("micro local match failed for %s", mi.display_name)
    if not (os.environ.get("USDA_API_KEY")
            or os.environ.get("DRHIRO_FDC_SEARCH_AT_LOG") == "1"):
        return
    try:
        with db.begin_nested():
            food, score, _found = resolve_via_fdc_search(db, mi.display_name, item=mi)
            if food is not None:
                mi.micro_food_id = food.id
                mi.micro_match_confidence = round(score, 4)
    except Exception:
        log.exception("micro FDC search failed for %s", mi.display_name)

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

def backfill_food_micros(db, batch: int = 200, fdc_fetcher=None, off_fetcher=None, sleep=time.sleep, run_matcher_first: bool = True, fdc_search=None) -> dict:
    if run_matcher_first:
        from drhiro_api.services.intake_match import run_matcher
        run_matcher(db)
    nmap = _nutrient_map(db)
    rows = {b.food_id: b for b in db.query(FoodMicroBackfill).all()}
    cutoff7 = datetime.now(timezone.utc) - timedelta(days=7)

    def _retryable(row):
        if row is None:
            return True
        if row.status not in ("failed", "no_source"):
            return False
        # Legacy no_source rows (source="none") predate the live FDC search
        # fallback — retry them once under the new code path.
        if row.source == "none":
            return True
        return row.attempted_at is None or row.attempted_at < cutoff7

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
        if f is not None and _retryable(rows.get(f.id)):
            foods[f.id] = f
    pending = list(foods.values())[:batch]
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
            values = _values_from_raw(raw)
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
        # No usda/off identity: search FDC live by name and upsert the
        # matched food's micros onto this row (identity unchanged).
        report["attempted"] += 1
        try:
            best = _best_fdc_match(f.display_name, fdc_search=fdc_search,
                                   fdc_fetcher=fdc_fetcher, sleep=sleep)
            if best is None:
                _record(db, f, "fdc_search", "no_source", 0, "FDC search no match")
                report["no_source"] += 1; continue
            fdc_id, _desc, _score = best
            fetched = _fdc_fetch([fdc_id], fetcher=fdc_fetcher, sleep=sleep)
            raw = fetched.get(fdc_id)
            if not raw:
                _record(db, f, "fdc_search", "failed", 0, "no FDC response")
                report["failed"] += 1; continue
            found = _upsert(db, f.id, _values_from_raw(raw), nmap)
            _record(db, f, "fdc_search", "done", found, None); report["done"] += 1
        except Exception as e:
            _record(db, f, "fdc_search", "failed", 0, str(e)); report["failed"] += 1
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
