"""System food-micros admin API (0.12.0 Part B, spec §4.3.2)."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import Meal, MealItem, User
from drhiro_api.routers.backups import _require_admin
from drhiro_api.services.food_micros import backfill_food_micros, status_summary
from drhiro_api.services.intake_match import match_item_to_food
from drhiro_api.services.task_queue import enqueue

router = APIRouter(prefix="/system/food-micros", tags=["system-food-micros"])

@router.post("/backfill")
def trigger_backfill(batch: int = 200, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(db, user)
    job_id = enqueue("drhiro", "drhiro_worker.jobs_food_micros.backfill_food_micros")
    if job_id is None:
        # No worker reachable (local/test): run synchronously.
        report = backfill_food_micros(db, batch=batch)
        return {"job_id": None, "report": report}
    return {"job_id": job_id, "queued": True}

@router.get("/status")
def status(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(db, user)
    s = status_summary(db)
    # % of last-90-days confirmed kcal whose food has >=1 micronutrient
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    meals = db.query(Meal).filter(Meal.status == "confirmed", Meal.eaten_at >= cutoff).all()
    kt = known = 0.0
    for m in meals:
        for it in m.items:
            n = it.nutrients_json or {}
            kcal = float(n["kcal"]) if isinstance(n, dict) and isinstance(n.get("kcal"), (int, float)) else 0.0
            kt += kcal
            from drhiro_api.services.intake import item_micros
            micros, _ = item_micros(db, it)
            if micros:
                known += kcal
    s["kcal_with_micros_pct_90d"] = round(known / kt * 100, 1) if kt else None
    return s

@router.patch("/meals/items/{item_id}/micro-food")
def set_micro_food(item_id: str, food_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from drhiro_api.models import Food
    import uuid as _uuid
    item = db.query(MealItem).join(Meal, Meal.id == MealItem.meal_id).filter(MealItem.id == _uuid.UUID(item_id), Meal.user_id == user.id).first()
    if item is None:
        raise HTTPException(404, "meal item not found")
    if food_id:
        food = db.get(Food, _uuid.UUID(food_id))
        if food is None:
            raise HTTPException(404, "food not found")
        item.micro_food_id = food.id; item.micro_match_confidence = 1.0
    else:
        item.micro_food_id = None; item.micro_match_confidence = None
    db.commit()
    return {"item_id": item_id, "micro_food_id": str(item.micro_food_id) if item.micro_food_id else None}

@router.post("/meals/{meal_id}/match")
def match_meal(meal_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    import uuid as _uuid
    meal = db.get(Meal, _uuid.UUID(meal_id))
    if meal is None or meal.user_id != user.id:
        raise HTTPException(404, "meal not found")
    matched = 0
    for it in meal.items:
        food, score = match_item_to_food(db, it.display_name, item=it)
        if food is not None:
            it.micro_food_id = food.id; it.micro_match_confidence = round(score, 4); matched += 1
    db.commit()
    return {"meal_id": meal_id, "matched": matched}
