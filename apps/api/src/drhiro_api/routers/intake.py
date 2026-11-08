"""Intake analysis API (0.12.0 Part B, spec §4.6)."""
from __future__ import annotations
from datetime import date, timedelta
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import Meal, MealItem, DoseEvent, MedicationItem, User
from drhiro_api.services.day_boundary import nutrition_date, resolve_tz
from drhiro_api.services.intake import intake_range
from drhiro_api.services.schemas_meds import NUTRIENT_CODES

router = APIRouter(prefix="/intake", tags=["intake"])

def _window(view: str, offset: int, anchor: date | None, tz_name):
    today = nutrition_date(__import__("datetime").datetime.now(__import__("datetime").timezone.utc), tz_name) if anchor is None else anchor
    if view == "day":
        d0 = today + timedelta(days=offset)
        return d0, d0
    if view == "month":
        first = date(today.year, today.month, 1)
        # shift by offset months
        y = first.year + (first.month - 1 + offset) // 12
        m = (first.month - 1 + offset) % 12 + 1
        start = date(y, m, 1)
        end = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    else:
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        start, end = monday, monday + timedelta(days=6)
    return start, end

def _shape(result, codes, source="all"):
    from drhiro_nutrition.micros import by_code
    out_nutrients = []
    for n in result["nutrients_raw"]:
        c = by_code(n["code"])
        days = []
        for d in n["days"]:
            dd = dict(d)
            if source == "food":
                dd["supp"] = 0.0
                dd["total"] = dd["food"]
            elif source == "supplements":
                dd["food"] = None
                dd["total"] = dd["supp"] if dd["supp"] else dd["total"]
            days.append(dd)
        vals = [d["pct"] for d in days if d["pct"] is not None]
        summary = {"days_with_data": len(vals), "avg_pct": round(sum(vals)/len(vals), 1) if vals else None,
                   "min_pct": min(vals) if vals else None, "max_pct": max(vals) if vals else None,
                   "days_ge_100": sum(1 for v in vals if v >= 100),
                   "avg_amount": None}
        out_nutrients.append({"code": n["code"], "label": c.label_en if c else n["code"],
                              "label_hr": c.label_hr if c else n["code"], "unit": c.unit if c else None,
                              "group": c.group if c else None, "ref": n["ref"]["value"], "ref_kind": n["ref"]["kind"],
                              "ul": n["ref"]["ul"], "ul_scope": n["ref"]["ul_scope"], "days": days, "summary": summary})
    return {"view": None, "start": result["start"], "end": result["end"], "tz": result["tz"],
            "dri_version": result["dri_version"], "reference_profile": result["reference_profile"],
            "days": result["days"], "nutrients": out_nutrients, "unknowns": result["unknowns"],
            "completeness_overall": result["completeness_overall"]}

@router.get("/overview")
def overview(view: str = "week", offset: int = 0, codes: str | None = None, source: str = "all",
             anchor: date | None = Query(default=None, alias="date"),
             user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    code_list = [c.strip() for c in codes.split(",")] if codes else None
    if code_list:
        code_list = [c for c in code_list if c in NUTRIENT_CODES]
    start, end = _window(view, offset, anchor, user.timezone)
    result = intake_range(db, user, start, end, code_list)
    body = _shape(result, code_list, source)
    body["view"] = view
    return body

@router.get("/day")
def day(date: date = Query(...), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    result = intake_range(db, user, date, date)
    body = _shape(result, None, "all")
    body["view"] = "day"; body["date"] = date.isoformat()
    # contributors + meals/doses that day
    contrib = {}
    for n in body["nutrients"]:
        if n["days"]:
            n.update(n["days"][0])  # flat day fields: food/supp/total/pct/status/...
        key = f"{date.isoformat()}|{n['code']}"
        contrib[n["code"]] = result["contributors"].get(key, [])[:5]
        n["contributors"] = contrib[n["code"]]
    meals = []
    for m in db.query(Meal).filter(Meal.user_id == user.id, Meal.status == "confirmed").all():
        if nutrition_date(m.eaten_at, user.timezone) == date:
            meals.append({"id": str(m.id), "eaten_at": m.eaten_at.isoformat(), "meal_type": m.meal_type,
                          "items": [{"id": str(i.id), "display_name": i.display_name, "grams": i.grams,
                                     "volume_ml": i.volume_ml} for i in m.items]})
    items_map = {i.id: i for i in db.query(MedicationItem).filter(MedicationItem.user_id == user.id).all()}
    doses = []
    for d in db.query(DoseEvent).filter(DoseEvent.user_id == user.id, DoseEvent.status == "taken").all():
        if nutrition_date(d.taken_at, user.timezone) == date:
            it = items_map.get(d.item_id)
            doses.append({"id": str(d.id), "item_id": str(d.item_id), "name": it.name if it else None,
                          "amount": d.amount, "taken_at": d.taken_at.isoformat()})
    body["meals"] = meals; body["doses"] = doses
    return body

@router.get("/nutrients")
def nutrients(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from drhiro_nutrition.micros import NUTRIENT_CATALOGUE
    return {"codes": [{"code": c.code, "label_en": c.label_en, "label_hr": c.label_hr, "unit": c.unit, "group": c.group}
                      for c in NUTRIENT_CATALOGUE], "count": len(NUTRIENT_CATALOGUE)}
