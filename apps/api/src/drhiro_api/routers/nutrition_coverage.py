"""Nutrition Coverage view (0.10.3 wave 4).

GET /nutrition-coverage?date=YYYY-MM-DD — daily table, one row per
nutrient: RDA/AI vs supplements (taken doses) vs food (meal items).
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import User
from drhiro_api.services.day_boundary import resolve_tz
from drhiro_api.services.nutrition_rda import coverage_rows

router = APIRouter(prefix="/nutrition-coverage", tags=["nutrition-coverage"])


@router.get("")
def nutrition_coverage(
    day: date | None = Query(default=None, alias="date"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    d = day or date.today()
    tz = resolve_tz(user.timezone)
    return {"date": d.isoformat(), "rows": coverage_rows(db, user, d, tz)}
