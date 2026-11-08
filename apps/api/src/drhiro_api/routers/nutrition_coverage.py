"""Nutrition Coverage (legacy alias, spec §4.6).

GET /nutrition-coverage?date=YYYY-MM-DD — reshapes /intake/day to the old
row format. Delete in 0.12.
"""
from __future__ import annotations
from datetime import date
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import User
from drhiro_api.routers.intake import day as intake_day

router = APIRouter(prefix="/nutrition-coverage", tags=["nutrition-coverage"])

@router.get("")
def nutrition_coverage(day: date | None = Query(default=None, alias="date"),
                       user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    d = day or date.today()
    body = intake_day(date=d, user=user, db=db)
    _LEGACY = {"thiamin": "vitamin_b1", "riboflavin": "vitamin_b2", "niacin": "vitamin_b3", "epa_dha": "omega3_epa_dha"}
    rows = []
    for n in body["nutrients"]:
        nd = n["days"][0] if n["days"] else {}
        rows.append({"key": _LEGACY.get(n["code"], n["code"]), "name": n["label"], "rda": n["ref"], "unit": n["unit"],
                     "from_supplements": nd.get("supp", 0.0), "from_food": nd.get("food"),
                     "coverage_pct": nd.get("pct"), "source": "nasem-2024.1", "age": None})
    return {"date": d.isoformat(), "rows": rows, "alias_of": "/intake/day"}
