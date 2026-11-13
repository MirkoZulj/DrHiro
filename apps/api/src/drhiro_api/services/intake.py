"""Intake aggregation (0.12.0 Part B, spec §4.3-4.5).

Food micros come from food_nutrients via the meal item's food link, computed
at query time. Unknown = absent, never 0. Confirmed meals only, bucketed by
nutrition_date in the user's tz. Beverages count via their meal items.
"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from drhiro_api.services.day_boundary import nutrition_date
from drhiro_api.services.schemas_meds import normalise_ingredient
from drhiro_api.models import DoseEvent, Food, FoodCatalogItem, FoodNutrient, Meal, MealItem, MedicationItem, Nutrient

try:
    from drhiro_nutrition.micros import NUTRIENT_CATALOGUE, UnmappedUnit, to_canonical
    from drhiro_nutrition.dri import reference_for, age_from_dob, DRI_VERSION
except Exception:  # pragma: no cover
    NUTRIENT_CATALOGUE = (); UnmappedUnit = ValueError; to_canonical = None
    reference_for = None; age_from_dob = None; DRI_VERSION = "nasem-2024.1"

CAT_CODES = [c.code for c in NUTRIENT_CATALOGUE]

@dataclass
class NutrientDay:
    date: date
    food: float | None = None
    supp: float = 0.0
    total: float | None = None
    pct: float | None = None
    completeness: float | None = None
    above_ul: bool = False
    status: str = "no_data"
    contributors: list = field(default_factory=list)

def _resolve_food(db, item):
    """Return (food, per100: dict|None) for a meal item, or (None, None)."""
    ref = getattr(item, "micro_food_id", None) or item.food_catalog_item_id
    if not ref:
        return None, None
    food = None
    try:
        food = db.get(Food, uuid.UUID(str(ref)))
    except Exception:
        food = None
    per100 = None
    if food is None:
        # maybe a FoodCatalogItem id, or an external_id
        try:
            ci = db.get(FoodCatalogItem, uuid.UUID(str(ref)))
        except Exception:
            ci = None
        if ci is not None:
            if ci.food_id:
                food = db.get(Food, ci.food_id)
            if not food and ci.nutrients_per_100g_json:
                per100 = {k: v for k, v in ci.nutrients_per_100g_json.items() if k in CAT_CODES}
        if food is None and not per100:
            food = db.query(Food).filter(Food.external_id == str(ref)).first()
    return food, per100

def _food_per100(db, food, per100_override, nutrient_rows):
    if per100_override:
        return per100_override
    return {code: val for code, val in nutrient_rows.get(food.id, {}).items()}

def item_micros(db, item, nutrient_rows=None):
    """{code: amount} for one meal item; absent code = unknown. Also returns
    contributor name. nutrient_rows: {food_id: {code: per100}} preloaded."""
    food, per100_override = _resolve_food(db, item)
    if food is None and not per100_override:
        return {}, item.display_name
    basis = item.grams if item.grams else (item.volume_ml if item.volume_ml else None)
    if basis is None:
        return {}, item.display_name
    per100 = _food_per100(db, food, per100_override, nutrient_rows or {})
    return {code: val * basis / 100.0 for code, val in per100.items() if val is not None}, item.display_name

def _item_kcal(item, food_kcal_per100=None):
    n = item.nutrients_json or {}
    if isinstance(n, dict) and isinstance(n.get("kcal"), (int, float)):
        return float(n["kcal"])
    if food_kcal_per100 is not None:
        basis = item.grams or item.volume_ml
        return food_kcal_per100 * basis / 100.0 if basis else None
    return None

def supplement_micros(db, start: date, end: date, user, tz):
    """{(day, code): amount} + unknowns list, from taken doses in range."""
    doses = (db.query(DoseEvent).filter(DoseEvent.user_id == user.id, DoseEvent.status == "taken").all())
    items = {i.id: i for i in db.query(MedicationItem).filter(MedicationItem.user_id == user.id).all()}
    totals = {}
    unknowns = []
    for d in doses:
        day = nutrition_date(d.taken_at, tz)
        if not (start <= day <= end):
            continue
        item = items.get(d.item_id)
        if item is None or not item.ingredients_json:
            continue
        amount = float(d.amount if d.amount is not None else (item.dose_amount or 1.0))
        for raw in item.ingredients_json:
            try:
                e = normalise_ingredient(raw)
            except ValueError:
                unknowns.append({"code": None, "text": str(raw.get("name") if isinstance(raw, dict) else raw), "detail": "unmapped ingredient"})
                continue
            if e.nutrient_code is None or e.mapping == "none" or e.amount is None:
                continue
            if e.form == "compound":
                unknowns.append({"code": e.nutrient_code, "text": e.name, "detail": "mineral_compound_amount"})
                continue
            per_unit = e.amount / (e.serving_units if e.per == "serving" else 1)
            try:
                canon, assumed = to_canonical(per_unit, e.unit, e.nutrient_code, name=e.name, form=e.form)
            except UnmappedUnit as ex:
                unknowns.append({"code": e.nutrient_code, "text": e.name, "detail": str(ex)})
                continue
            key = (day, e.nutrient_code)
            totals[key] = totals.get(key, 0.0) + canon * amount
            if e.mapping == "suggested":
                unknowns.append({"code": e.nutrient_code, "text": e.name, "detail": "suggested mapping — confirm"})
    return totals, unknowns

def _load_food_rows(db, food_ids):
    rows = {}
    if not food_ids:
        return rows
    q = (db.query(FoodNutrient, Nutrient).join(Nutrient, Nutrient.id == FoodNutrient.nutrient_id)
         .filter(FoodNutrient.food_id.in_(food_ids)).all())
    for fn, nut in q:
        if nut.nutrient_code in CAT_CODES:
            rows.setdefault(fn.food_id, {})[nut.nutrient_code] = fn.amount_per_100g
    return rows

def intake_range(db, user, start: date, end: date, codes=None):
    """Full aggregation for [start, end]. Returns dict with days, per-code
    nutrient day lists, unknowns, reference profile, completeness_overall."""
    codes = codes or list(CAT_CODES)
    tz_name = user.timezone
    tz = tz_name  # nutrition_date resolves names itself
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    # --- meals (confirmed only)
    meals = (db.query(Meal).filter(Meal.user_id == user.id, Meal.status == "confirmed").all())
    meal_day = {}
    all_items = []
    for m in meals:
        d = nutrition_date(m.eaten_at, tz)
        if start <= d <= end:
            meal_day.setdefault(d, []).extend(m.items)
            all_items.extend(m.items)
    food_ids = set()
    for it in all_items:
        f, _ = _resolve_food(db, it)
        if f is not None:
            food_ids.add(f.id)
    nutrient_rows = _load_food_rows(db, food_ids)
    # per-day per-code food totals + kcal-known per code
    food_tot = {}; kcal_known = {}; kcal_total = {}
    unknowns = []; contributors = {}
    for d, items in meal_day.items():
        for it in items:
            micros, name = item_micros(db, it, nutrient_rows)
            food, _ov = _resolve_food(db, it)
            kcal = _item_kcal(it)
            if kcal is not None:
                kcal_total[d] = kcal_total.get(d, 0.0) + kcal
            if food is None and not micros:
                if kcal:
                    unknowns.append({"code": None, "text": name, "detail": "food without micro data"})
                continue
            for code in codes:
                if code in micros:
                    food_tot[(d, code)] = food_tot.get((d, code), 0.0) + micros[code]
                    if kcal is not None:
                        kcal_known[(d, code)] = kcal_known.get((d, code), 0.0) + kcal
                    contributors.setdefault((d, code), []).append({"source": "food", "name": name, "amount": round(micros[code], 3)})
    supp_tot, supp_unknowns = supplement_micros(db, start, end, user, tz)
    unknowns.extend(supp_unknowns)
    # contributors for supplements
    # (re-walk doses cheaply for names)
    age = age_from_dob(getattr(user, "date_of_birth", None))
    sex = getattr(user, "sex_for_health_calculations", None)
    ref0 = reference_for(codes[0], sex, age) if codes else {"assumed": True, "band": "31-50", "sex_used": "m", "version": DRI_VERSION}
    nutrients = []
    for code in codes:
        ref = reference_for(code, sex, age)
        ndays = []
        for d in days:
            has_meals = d in meal_day
            has_supp = any(k[0] == d and k[1] == code for k in supp_tot)
            food = food_tot.get((d, code)) if has_meals else None
            supp = supp_tot.get((d, code), 0.0)
            if not has_meals and supp == 0.0:
                ndays.append({"date": d.isoformat(), "food": None, "supp": 0.0, "total": None, "pct": None,
                              "completeness": None, "above_ul": False, "status": "no_data"})
                continue
            total = (food or 0.0) + supp if (food is not None or supp) else None
            if food is None and supp:
                status = "food_missing"
            else:
                comp = None
                if has_meals:
                    kt = kcal_total.get(d, 0.0)
                    comp = round(kcal_known.get((d, code), 0.0) / kt, 4) if kt else None
                status = "partial" if (comp is not None and comp < 0.8) else "ok"
            pct = round(total / ref["value"] * 100, 1) if (total is not None and ref["value"]) else None
            entry = {"date": d.isoformat(), "food": round(food, 3) if food is not None else None,
                     "supp": round(supp, 3), "total": round(total, 3) if total is not None else None,
                     "pct": pct, "completeness": (round(kcal_known.get((d, code), 0.0) / kcal_total[d], 4)
                                                  if has_meals and kcal_total.get(d) else None),
                     "above_ul": False, "status": status}
            # UL scope
            if ref["ul"] is not None:
                scope = ref["ul_scope"] or ""
                cmp_val = supp if "supplemental" in scope else total
                if cmp_val is not None and cmp_val > ref["ul"]:
                    entry["above_ul"] = True
            ndays.append(entry)
        nutrients.append({"code": code, "days": ndays, "ref": ref})
    # completeness overall: kcal share with >=1 micro known (items resolved to a food)
    comp_all = 0.0
    kt_all = sum(kcal_total.values())
    if kt_all:
        known_kcal = 0.0
        for d, items in meal_day.items():
            for it in items:
                micros, _n = item_micros(db, it, nutrient_rows)
                if micros:
                    k = _item_kcal(it)
                    if k:
                        known_kcal += k
        comp_all = round(known_kcal / kt_all, 4)
    return {"start": start.isoformat(), "end": end.isoformat(), "tz": tz_name,
            "dri_version": DRI_VERSION,
            "reference_profile": {"sex": sex, "band": ref0["band"], "assumed": ref0["assumed"], "sex_used": ref0["sex_used"]},
            "days": [d.isoformat() for d in days], "nutrients_raw": nutrients,
            "unknowns": unknowns[:10], "completeness_overall": comp_all,
            "contributors": {f"{d}|{c}": v for (d, c), v in contributors.items()}}
