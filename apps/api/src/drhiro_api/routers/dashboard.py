"""Dashboard, trends, and summaries endpoints. Section 8.4."""

from __future__ import annotations

import uuid
from datetime import date as date_type, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session, selectinload

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import Activity, Goal, Meal, Measurement, User
from drhiro_api.security import audit
from drhiro_rules.calculations import (
    bp_average_by_context,
    coverage_score,
    rolling_average,
    rolling_median,
    sleep_duration_consistency,
    weight_trend,
)
from drhiro_schema.metrics import MetricType

router = APIRouter(tags=["dashboard"])


def _measurements_since(db: Session, user_id: uuid.UUID, days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        db.query(Measurement)
        .filter(Measurement.user_id == user_id, Measurement.start_at >= cutoff)
        .order_by(Measurement.start_at.asc())
        .all()
    )
    return [
        {
            "id": str(m.id),
            "metric_type": m.metric_type,
            "start_at": m.start_at,
            "end_at": m.end_at,
            "value_json": m.value_json,
            "source_provider": m.source_provider,
            "recording_method": m.recording_method,
        }
        for m in rows
    ]



def _dedup_sleep(records):
    """Compute total sleep from potentially overlapping records.
    Health Connect sends full sessions + sub-stages that overlap.
    Keeps only the longest coverage per time period.
    """
    if not records:
        return 0.0
    sorted_recs = sorted(records, key=lambda r: ((r.get('end_at') or r['start_at']) - r['start_at'], r['start_at']))
    total_min = 0.0
    occupied = []
    for rec in sorted_recs:
        start = rec['start_at']
        end = rec.get('end_at') or start
        dur = (rec.get('value_json') or {}).get('duration_min', 0) or 0
        uncovered = [(start, end)]
        for occ_start, occ_end in occupied:
            new_unc = []
            for u_s, u_e in uncovered:
                if occ_end <= u_s or occ_start >= u_e:
                    new_unc.append((u_s, u_e))
                else:
                    if u_s < occ_start:
                        new_unc.append((u_s, occ_start))
                    if u_e > occ_end:
                        new_unc.append((occ_end, u_e))
            uncovered = new_unc
            if not uncovered:
                break
        if not uncovered:
            continue
        unc_sec = sum((e - s).total_seconds() for s, e in uncovered)
        total_sec = (end - start).total_seconds()
        if total_sec > 0 and unc_sec > 0:
            total_min += dur * unc_sec / total_sec
            occupied.append((start, end))
    return round(total_min, 0)


def _dedup_steps(records: list[dict]) -> int:
    """Sum step counts without double-counting overlapping intervals.

    Health Connect returns step records at multiple aggregation levels
    (30-min summaries containing per-minute + per-second detail records).
    Summing ALL records double-counts sub-intervals.  The prior heuristic
    gave every record proportional credit for its uncovered fraction, which
    HALVED real per-minute data even when there were no summaries (the user's
    band showed ~6.6k, the tile 3.4k).

    Option B (the defensible fix): granular records (< 30 min) are real,
    distinct measurements — count them fully.  A coarse record (>= 30 min
    summary) contributes ONLY for the time NOT already covered by granular
    records (gap-filling).  Never trim a granular record.
    """
    if not records:
        return 0

    GRANULAR = 30 * 60  # 30-minute summary threshold (seconds)

    granular = [
        r for r in records
        if (r.get("end_at") or r["start_at"]) - r["start_at"] < timedelta(seconds=GRANULAR)
    ]
    coarse = [
        r for r in records
        if (r.get("end_at") or r["start_at"]) - r["start_at"] >= timedelta(seconds=GRANULAR)
    ]

    # Granular records: fully counted; occupy their intervals.
    occupied: list[tuple[datetime, datetime]] = []
    total = 0
    for rec in granular:
        start = rec["start_at"]
        end = rec.get("end_at") or start
        total += (rec.get("value_json") or {}).get("count", 0)
        occupied.append((start, end))

    # Coarse summaries: only uncovered (gap) time is credited, proportionally.
    for rec in coarse:
        start = rec["start_at"]
        end = rec.get("end_at") or start
        count = (rec.get("value_json") or {}).get("count", 0)
        uncovered = [(start, end)]
        for occ_start, occ_end in occupied:
            new_uncovered: list[tuple[datetime, datetime]] = []
            for u_start, u_end in uncovered:
                if occ_end <= u_start or occ_start >= u_end:
                    new_uncovered.append((u_start, u_end))
                else:
                    if u_start < occ_start:
                        new_uncovered.append((u_start, occ_start))
                    if u_end > occ_end:
                        new_uncovered.append((occ_end, u_end))
            uncovered = new_uncovered
            if not uncovered:
                break
        if not uncovered:
            continue  # fully covered by granular records
        unc_sec = sum((e - s).total_seconds() for s, e in uncovered)
        total_sec = (end - start).total_seconds()
        if total_sec > 0 and unc_sec > 0:
            total += int(count * unc_sec / total_sec)
            occupied.extend(uncovered)

    return total


def _local_day_start(user: User) -> datetime:
    """Start of the user's current LOCAL day, as tz-aware UTC datetime.

    Measurements are stored as tz-aware UTC. 'Today' must be computed in the
    user's timezone (e.g. Europe/Zagreb), not UTC midnight — otherwise steps
    between local midnight and 02:00 (for UTC+2) fall on 'yesterday' and the
    daily total is under-counted.
    """
    tz = ZoneInfo(user.timezone or "UTC")
    local_now = datetime.now(tz)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)


@router.get("/dashboard/today")
def dashboard_today(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Today's summary: steps, weight, BP latest, water, meals, coverage."""
    today_start = _local_day_start(user)
    measurements = _measurements_since(db, user.id, 30)

    steps_today = _dedup_steps(
        [m for m in measurements
         if m["metric_type"] == MetricType.STEPS and m["start_at"] >= today_start]
    )
    def _latest_point(metric_type, value_key=None):
        """Return the newest row for a point-in-time metric, with recency metadata."""
        for m in reversed(measurements):
            if m["metric_type"] == metric_type:
                dt = m.get("start_at") or m.get("recorded_at")
                if dt is None:
                    continue
                v = m["value_json"]
                if value_key:
                    v = v.get(value_key)
                if v is None:
                    continue
                local_dt = dt.astimezone(ZoneInfo(user.timezone))
                return {
                    "value": v,
                    "measured_at": local_dt.isoformat(),
                    "days_old": (datetime.now(ZoneInfo(user.timezone)).date() - local_dt.date()).days,
                    "is_stale": (datetime.now(ZoneInfo(user.timezone)).date() - local_dt.date()).days > 2,
                    "source_provider": m.get("source_provider"),
                    "recording_method": m.get("recording_method"),
                }
        return None

    latest_bp = None
    for m in reversed(measurements):
        if m["metric_type"] == MetricType.BLOOD_PRESSURE:
            dt = m.get("start_at") or m.get("recorded_at")
            if dt is None:
                continue
            local_dt = dt.astimezone(ZoneInfo(user.timezone))
            latest_bp = {
                "systolic_mmhg": m["value_json"].get("systolic_mmhg"),
                "diastolic_mmhg": m["value_json"].get("diastolic_mmhg"),
                "pulse_bpm": m["value_json"].get("pulse_bpm"),
                "measured_at": local_dt.isoformat(),
                "days_old": (datetime.now(ZoneInfo(user.timezone)).date() - local_dt.date()).days,
                "is_stale": (datetime.now(ZoneInfo(user.timezone)).date() - local_dt.date()).days > 2,
                "source_provider": m.get("source_provider"),
                "recording_method": m.get("recording_method"),
            }
            break
    latest_weight_kg = _latest_point(MetricType.WEIGHT, "weight_kg")
    water_today = sum(
        m["value_json"].get("amount_ml", 0)
        for m in measurements
        if m["metric_type"] == MetricType.WATER and m["start_at"] >= today_start
    )
    # Liquid breakdown by category (value_json may carry a "category" key;
    # missing -> "water" for backward compat with old rows).
    liquid_cats = ["water", "non_alcoholic", "beer", "wine", "spirits", "other_alcohol"]
    liquid_today: dict[str, int] = {c: 0 for c in liquid_cats}
    for m in measurements:
        if m["metric_type"] != MetricType.WATER or m["start_at"] < today_start:
            continue
        cat = (m["value_json"].get("category") or "water")
        if cat not in liquid_today:
            cat = "water"
        liquid_today[cat] += m["value_json"].get("amount_ml", 0)
    liquids_today = {"total_ml": sum(liquid_today.values()), **liquid_today}
    # Last water measurement ANY day (for "last log" display)
    water_last = None
    for m in reversed(measurements):
        if m["metric_type"] == MetricType.WATER:
            dt = m.get("start_at") or m.get("recorded_at")
            if dt is not None:
                water_last = dt.astimezone(ZoneInfo(user.timezone)).isoformat()
            break
    meals_today_rows = (
        db.query(Meal)
        .filter(Meal.user_id == user.id, Meal.eaten_at >= today_start, Meal.status != "deleted")
        .all()
    )
    meals_today = len(meals_today_rows)
    calories_kcal_today = sum(float((m.totals_json or {}).get("kcal") or 0) for m in meals_today_rows)
    calories_measured_at = (
        max((m.eaten_at for m in meals_today_rows), default=None).astimezone(ZoneInfo(user.timezone)).isoformat()
        if meals_today_rows else None
    )
    sleep_last = None
    sleep_rows = [m for m in measurements if m["metric_type"] == MetricType.SLEEP]
    if sleep_rows:
        # Use the MOST RECENT sleep night, not the longest single record ever.
        # Sleep is stored as per-stage records (deep/light/REM) that share a
        # night; pick the latest local date that has any sleep, then dedup its
        # records into a total.  The old "max by duration_min" returned a
        # 12-day-old value (546 min from Aug 20) instead of last night.
        tz = ZoneInfo(user.timezone)
        by_night: dict[str, list] = {}
        for m in sleep_rows:
            dt = m.get("start_at") or m.get("recorded_at")
            if dt is None:
                continue
            local_d = dt.astimezone(tz).date().isoformat()
            by_night.setdefault(local_d, []).append(m)
        latest_night = max(by_night)
        night_recs = [
            {
                "start_at": m.get("start_at") or m.get("recorded_at"),
                "end_at": m.get("end_at") or (m.get("start_at") or m.get("recorded_at")),
                "value_json": m.get("value_json") or {},
            }
            for m in by_night[latest_night]
        ]
        duration_min = _dedup_sleep(night_recs)
        if duration_min > 0:
            anchor = night_recs[0]["start_at"]
            local_dt = anchor.astimezone(tz)
            sleep_last = {
                "duration_min": duration_min,
                "measured_at": local_dt.isoformat(),
                "days_old": (datetime.now(tz).date() - local_dt.date()).days,
                "is_stale": (datetime.now(tz).date() - local_dt.date()).days > 2,
                "source_provider": by_night[latest_night][0].get("source_provider"),
            }

    return {
        "steps_today": steps_today,
        "steps_coverage_7d": coverage_score(
            [m for m in measurements if m["metric_type"] == MetricType.STEPS], 7,

        ),
        "latest_bp": latest_bp,
        "latest_weight_kg": latest_weight_kg,
        "water_ml_today": water_today,
        "liquids_today": liquids_today,
        "water_measured_at": water_last,
        "meals_logged_today": meals_today,
        "calories_kcal_today": round(calories_kcal_today, 1),
        "calories_measured_at": calories_measured_at,
        "calories_is_stale": False,
        "last_sleep": sleep_last,
        "missing_data_note": "Absence of wearable data is reported as missing, never as zero.",
    }


@router.get("/trends")
def trends(
    metric: str = Query(...),
    period: Literal["7d", "14d", "30d", "90d"] = "90d",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    days = int(period[:-1])
    measurements = _measurements_since(db, user.id, days)
    if metric == "weight":
        return weight_trend(measurements, days)
    if metric == "blood_pressure":
        return {
            "overall": bp_average_by_context(measurements),
            "morning": bp_average_by_context(measurements, "sitting"),
            "raw_count": sum(1 for m in measurements if m["metric_type"] == MetricType.BLOOD_PRESSURE),
        }
    if metric == "sleep":
        return sleep_duration_consistency(measurements, min(days, 30))
    if metric == "steps":
        steps_raw = [m for m in measurements if m["metric_type"] == MetricType.STEPS]
        return {
            "avg_7d": _dedup_steps([m for m in steps_raw if m["start_at"] >= datetime.now(timezone.utc) - timedelta(days=7)]),
            "avg_14d": _dedup_steps([m for m in steps_raw if m["start_at"] >= datetime.now(timezone.utc) - timedelta(days=14)]),
            "median_30d": rolling_median(steps_raw, MetricType.STEPS, 30),
            "coverage": coverage_score([m for m in steps_raw], days),
        }
    if metric == "heart_rate":
        return {
            "avg_resting_7d": rolling_average(measurements, MetricType.RESTING_HEART_RATE, 7),
            "avg_7d": rolling_average(measurements, MetricType.HEART_RATE, 7),
        }
    if metric in ("calories", "nutrition"):
        return _nutrition_trend(db, user.id, days)
    raise HTTPException(status_code=400, detail=f"Unsupported metric: {metric}")





def _nutrition_trend(db: Session, user_id: uuid.UUID, days: int) -> dict:
    """Daily nutrition totals from meals over the trailing window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    meals = (
        db.query(Meal)
        .filter(Meal.user_id == user_id, Meal.eaten_at >= cutoff, Meal.status != "deleted")
        .order_by(Meal.eaten_at.asc())
        .all()
    )
    from collections import defaultdict
    daily: dict[str, dict] = defaultdict(lambda: {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "meals_count": 0})
    for m in meals:
        local_day = m.eaten_at.date().isoformat()
        t = m.totals_json or {}
        daily[local_day]["kcal"] += t.get("kcal") or 0
        daily[local_day]["protein_g"] += t.get("protein_g") or 0
        daily[local_day]["carbs_g"] += t.get("carbs_g") or 0
        daily[local_day]["fat_g"] += t.get("fat_g") or 0
        daily[local_day]["meals_count"] += 1
    points = [
        {
            "date": d,
            "kcal": round(v["kcal"], 1),
            "protein_g": round(v["protein_g"], 1),
            "carbs_g": round(v["carbs_g"], 1),
            "fat_g": round(v["fat_g"], 1),
            "meals_count": v["meals_count"],
        }
        for d, v in sorted(daily.items())
    ]
    return {"points": points, "days": days}


@router.get("/dashboard/meals-today")
def meals_today(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Today''s meals with full nutrition breakdown for the popup."""
    today_start = _local_day_start(user)
    meals = (
        db.query(Meal)
        .options(selectinload(Meal.items))
        .filter(Meal.user_id == user.id, Meal.eaten_at >= today_start, Meal.status != "deleted")
        .order_by(Meal.eaten_at.asc())
        .all()
    )
    total_kcal = 0.0
    total_protein = 0.0
    total_carbs = 0.0
    total_fat = 0.0
    meal_list = []
    for m in meals:
        t = m.totals_json or {}
        kcal = t.get("kcal") or 0
        protein = t.get("protein_g") or 0
        carbs = t.get("carbs_g") or 0
        fat = t.get("fat_g") or 0
        total_kcal += kcal
        total_protein += protein
        total_carbs += carbs
        total_fat += fat
        items = []
        for i in m.items:
            n = i.nutrients_json or {}
            items.append({
                "item_id": str(i.id),
                "display_name": i.display_name,
                "grams": i.grams,
                "kcal": n.get("kcal"),
                "protein_g": n.get("protein_g"),
                "carbs_g": n.get("carbs_g"),
                "fat_g": n.get("fat_g"),
            })
        meal_list.append({
            "id": str(m.id),
            "meal_type": m.meal_type,
            "eaten_at": m.eaten_at.isoformat(),
            "totals": t,
            "items": items,
        })
    return {
        "meals": meal_list,
        "totals": {
            "kcal": round(total_kcal, 1),
            "protein_g": round(total_protein, 1),
            "carbs_g": round(total_carbs, 1),
            "fat_g": round(total_fat, 1),
        },
        "count": len(meal_list),
    }


# ---- Activity & energy-balance endpoints (2026-08-29) ----

class BmrUpdate(BaseModel):
    basal_metabolism_kcal: float | None = None

class ActivityCreate(BaseModel):
    activity_date: str | None = None  # ISO date; defaults to today (user tz)
    title: str
    description: str | None = None
    calories_burned: float


@router.get("/activity/settings")
def activity_settings(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """BMR (user-set, applies to all days) + Katch-McArdle reference inputs."""
    return {
        "basal_metabolism_kcal": user.basal_metabolism_kcal,
        "has_lean_mass_data": bool(user.height_cm and user.date_of_birth and user.sex_for_health_calculations),
    }


@router.patch("/activity/settings")
def update_activity_settings(req: BmrUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if req.basal_metabolism_kcal is not None:
        if not (500 <= req.basal_metabolism_kcal <= 6000):
            raise HTTPException(status_code=422, detail="BMR must be 500-6000 kcal")
        user.basal_metabolism_kcal = req.basal_metabolism_kcal
        audit(db, "user", str(user.id), user.id, "activity.bmr_set", "user", str(user.id),
              {"basal_metabolism_kcal": req.basal_metabolism_kcal})
        db.commit()
    return {"ok": True, "basal_metabolism_kcal": user.basal_metabolism_kcal}


@router.get("/activities")
def list_activities(date: str | None = Query(None), days: int = Query(30, ge=1, le=366),
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Activities for a date (default: today, user tz) or an aggregate daily sum for N days."""
    tz = ZoneInfo(user.timezone)
    if date:
        d = date_type.fromisoformat(date)
        rows = (db.query(Activity)
                .filter(Activity.user_id == user.id, Activity.activity_date == d)
                .order_by(Activity.created_at).all())
        return {"date": d.isoformat(),
                "items": [_activity_out(a) for a in rows],
                "total_burned": sum(a.calories_burned for a in rows)}
    end = datetime.now(tz).date()
    start = end - timedelta(days=days - 1)
    rows = (db.query(Activity)
            .filter(Activity.user_id == user.id, Activity.activity_date >= start, Activity.activity_date <= end)
            .all())
    by_day: dict[str, float] = {}
    for a in rows:
        by_day[a.activity_date.isoformat()] = by_day.get(a.activity_date.isoformat(), 0.0) + a.calories_burned
    return {"days": by_day, "total_burned": sum(by_day.values())}


def _activity_out(a: Activity) -> dict:
    return {
        "id": str(a.id), "date": a.activity_date.isoformat(), "title": a.title,
        "description": a.description, "calories_burned": a.calories_burned,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@router.post("/activities")
def create_activity(req: ActivityCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tz = ZoneInfo(user.timezone)
    d = date_type.fromisoformat(req.activity_date) if req.activity_date else datetime.now(tz).date()
    a = Activity(user_id=user.id, activity_date=d, title=req.title.strip(),
                 description=req.description, calories_burned=req.calories_burned)
    db.add(a)
    audit(db, "user", str(user.id), user.id, "activity.created", "activity", str(a.id),
          {"title": a.title, "calories_burned": a.calories_burned, "date": d.isoformat()})
    db.commit()
    db.refresh(a)
    return _activity_out(a)


@router.delete("/activities/{activity_id}")
def delete_activity(activity_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    a = db.query(Activity).filter(Activity.id == activity_id, Activity.user_id == user.id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Activity not found")
    db.delete(a)
    db.commit()
    return {"ok": True}


@router.get("/energy-balance")
def energy_balance(days: int = Query(30, ge=1, le=366),
                   user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Daily calorie balance = intake (meals) - burned (BMR + walking + sport).

    Walking calories come from the steps-derived active-energy metric when the
    tracker provides it; otherwise approximated via steps (rough 0.04 kcal/step
    net of BMR contribution is NOT applied here — the tracker value is used
    as-is when present).
    """
    tz = ZoneInfo(user.timezone)
    end = datetime.now(tz).date()
    start = end - timedelta(days=days - 1)

    bmr = user.basal_metabolism_kcal or 0.0

    # intake by day
    meals = (db.query(Meal)
             .filter(Meal.user_id == user.id,
                     Meal.eaten_at >= datetime.combine(start, datetime.min.time(), tzinfo=tz),
                     Meal.eaten_at < datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=tz))
             .all())
    intake: dict[str, float] = {}
    for m in meals:
        day = m.eaten_at.astimezone(tz).date().isoformat()
        t = (m.totals_json or {})
        intake[day] = intake.get(day, 0.0) + float(t.get("kcal") or 0)

    # sport activities by day
    acts = (db.query(Activity)
            .filter(Activity.user_id == user.id,
                    Activity.activity_date >= start, Activity.activity_date <= end).all())
    sport: dict[str, float] = {}
    for a in acts:
        sport[a.activity_date.isoformat()] = sport.get(a.activity_date.isoformat(), 0.0) + a.calories_burned

    # active energy (walking) by day — prefer tracker 'active_energy', else steps*0.04
    measurements = _measurements_since(db, user.id, days + 1)
    active_start = datetime.combine(start, datetime.min.time(), tzinfo=tz)
    walking: dict[str, float] = {}
    for m in measurements:
        dt = m.get("start_at") or m.get("recorded_at")
        if dt is None or dt < active_start:
            continue
        day = dt.astimezone(tz).date().isoformat()
        if m["metric_type"] == MetricType.ACTIVE_CALORIES:
            walking[day] = walking.get(day, 0.0) + float((m["value_json"] or {}).get("value") or 0)
    if not walking:
        # Steps need deduplication (Health Connect sends overlapping intervals)
        step_recs = [m for m in measurements
                     if m.get("metric_type") == MetricType.STEPS and
                     (m.get("start_at") or m.get("recorded_at")) and
                     (m.get("start_at") or m.get("recorded_at")) >= active_start]
        if step_recs:
            for m in step_recs:
                if not m.get("end_at"):
                    m["end_at"] = m["start_at"]
            deduped_total = _dedup_steps(step_recs)
            raw_total = sum(
                float((m.get("value_json") or {}).get("count") or
                      (m.get("value_json") or {}).get("value") or 0)
                for m in step_recs
            )
            ratio = deduped_total / raw_total if raw_total > 0 else 0.0
            for m in step_recs:
                dt = m.get("start_at") or m.get("recorded_at")
                count = float((m.get("value_json") or {}).get("count") or
                              (m.get("value_json") or {}).get("value") or 0)
                steps = count * ratio
                day = dt.astimezone(tz).date().isoformat()
                walking[day] = walking.get(day, 0.0) + steps * 0.04

    points = []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        burned = bmr + walking.get(d, 0.0) + sport.get(d, 0.0)
        taken = intake.get(d, 0.0)
        points.append({"date": d, "intake_kcal": round(taken, 1), "bmr_kcal": bmr,
                       "walking_kcal": round(walking.get(d, 0.0), 1),
                       "sport_kcal": round(sport.get(d, 0.0), 1),
                       "burned_kcal": round(burned, 1),
                       "balance_kcal": round(taken - burned, 1)})
    return {"bmr_kcal": bmr, "days": days, "points": points}


@router.get("/summaries/daily/{local_date}")
def daily_summary(local_date: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Daily summary for a local date (YYYY-MM-DD). Uses user timezone."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(user.timezone)
    start = datetime.fromisoformat(local_date).replace(tzinfo=tz)
    end = start + timedelta(days=1)
    measurements = (
        db.query(Measurement)
        .filter(Measurement.user_id == user.id, Measurement.start_at >= start, Measurement.start_at < end)
        .all()
    )
    meals = (
        db.query(Meal)
        .filter(Meal.user_id == user.id, Meal.eaten_at >= start, Meal.eaten_at < end, Meal.status != "deleted")
        .all()
    )
    kcal = sum((m.totals_json or {}).get("kcal") or 0 for m in meals)
    steps = _dedup_steps(
        [{"metric_type": m.metric_type, "start_at": m.start_at, "end_at": m.end_at, "value_json": m.value_json}
         for m in measurements if m.metric_type == MetricType.STEPS]
    )
    return {
        "date": local_date,
        "steps": steps,
        "meals": len(meals),
        "calories_kcal": round(kcal, 1),
        "meals_logged": [{"id": str(m.id), "meal_type": m.meal_type, "totals": m.totals_json} for m in meals],
        "coverage": coverage_score(
            [{"metric_type": m.metric_type, "value_json": m.value_json, "start_at": m.start_at} for m in measurements], 1,

        ),
    }


@router.get("/summaries/weekly/{week}")
def weekly_summary(week: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Weekly summary for an ISO week (YYYY-Www)."""
    from zoneinfo import ZoneInfo
    import datetime as dt

    tz = ZoneInfo(user.timezone)
    try:
        year, w = week.split("-W")
        first = dt.date.fromisocalendar(int(year), int(w), 1)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Use ISO week format YYYY-Www")
    start = datetime.combine(first, dt.time.min, tzinfo=tz)
    end = start + timedelta(days=7)
    measurements = (
        db.query(Measurement)
        .filter(Measurement.user_id == user.id, Measurement.start_at >= start, Measurement.start_at < end)
        .all()
    )
    steps_raw = [
        {"metric_type": m.metric_type, "start_at": m.start_at, "end_at": m.end_at, "value_json": m.value_json}
        for m in measurements if m.metric_type == MetricType.STEPS
    ]
    days_with_steps = len({m.start_at.date() for m in measurements if m.metric_type == MetricType.STEPS})
    return {
        "week": week,
        "days_with_steps": days_with_steps,
        "avg_steps_week": round(_dedup_steps(steps_raw) / max(days_with_steps, 1))
        if days_with_steps else None,
        "weight_trend": weight_trend(
            [{"metric_type": m.metric_type, "value_json": m.value_json, "start_at": m.start_at} for m in measurements], 7
        ),
        "bp_average": bp_average_by_context(
            [{"metric_type": m.metric_type, "value_json": m.value_json, "start_at": m.start_at} for m in measurements]
        ),
    }


@router.get("/trends/daily/steps/hourly")
def steps_intraday(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Today's steps bucketed by hour (local time) for the D-view intraday chart.

    Steps measurements arrive as half-hour buckets (start_at with hour). We sum
    each bucket into its local hour. Missing hours are omitted (never zeroed —
    the chart renders a gap, per 'missing is never zero').
    """
    from collections import defaultdict
    tz = ZoneInfo(user.timezone or "UTC")
    now_local = datetime.now(tz)
    day_start = datetime.combine(now_local.date(), datetime.min.time(), tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    rows = (db.query(Measurement)
            .filter(Measurement.user_id == user.id,
                    Measurement.metric_type == MetricType.STEPS,
                    Measurement.start_at >= day_start,
                    Measurement.start_at < day_end)
            .all())
    hourly: dict[int, float] = defaultdict(float)
    for m in rows:
        v = (m.value_json or {}).get("count")
        if v is None:
            continue
        local = m.start_at.astimezone(tz)
        hourly[local.hour] += float(v)
    points = [{"hour": h, "value": round(hourly[h], 0)} for h in sorted(hourly)]
    return {"date": now_local.date().isoformat(), "total": round(sum(hourly.values()), 0), "points": points}


@router.get("/trends/daily/{metric}")
def trends_daily(metric: str, days: int = Query(7, ge=1, le=90),
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Daily totals/last-value per day for a metric — feeds popup bar charts.

    steps -> summed steps/day; water_ml -> summed; sleep_duration_min -> max;
    heart_rate_bpm -> avg; weight_kg -> last of day; calories_kcal -> meal kcal/day.
    """
    tz = ZoneInfo(user.timezone)
    end = datetime.now(tz).date()
    start = end - timedelta(days=days - 1)
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=tz)
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=tz)

    rows = (db.query(Measurement)
            .filter(Measurement.user_id == user.id,
                    Measurement.start_at >= start_dt, Measurement.start_at < end_dt)
            .order_by(Measurement.start_at.asc())
            .all())

    metric_map = {
        "steps": MetricType.STEPS,
        "water_ml": MetricType.WATER,
        "sleep_duration_min": MetricType.SLEEP,
        "heart_rate_bpm": MetricType.HEART_RATE,
        "weight_kg": MetricType.WEIGHT,
    }
    mt = metric_map.get(metric)
    if mt is None:
        raise HTTPException(status_code=400, detail=f"Unsupported metric: {metric}")

    buckets: dict[str, list] = {}
    for m in rows:
        if m.metric_type != mt:
            continue
        day = m.start_at.astimezone(tz).date().isoformat()
        vj = m.value_json or {}
        v = vj.get("value")
        if v is None:
            # metric-specific keys
            if mt == MetricType.STEPS:
                v = vj.get("count")
            elif mt == MetricType.WEIGHT:
                v = vj.get("weight_kg")
            elif mt == MetricType.SLEEP:
                v = vj.get("duration_min")
            elif mt == MetricType.WATER:
                v = vj.get("amount_ml") or vj.get("volume_ml") or vj.get("water_ml")
            elif mt == MetricType.HEART_RATE:
                v = vj.get("bpm") or vj.get("heart_rate")
        if v is None:
            continue
        buckets.setdefault(day, []).append(float(v))

    points = []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        vals = buckets.get(d, [])
        if not vals:
            # Missing data is never zero — use null so the chart shows a gap
            points.append({"date": d, "value": None})
            continue
        if metric == "steps":
            # Steps: dedup the ACTUAL records for this day (not fake ones).
            day_rows = [m for m in rows
                        if m.metric_type == mt
                        and m.start_at.astimezone(tz).date().isoformat() == d]
            day_records = [
                {"start_at": m.start_at,
                 "end_at": getattr(m, "end_at", None) or m.start_at,
                 "value_json": m.value_json or {}}
                for m in day_rows
            ]
            v = _dedup_steps(day_records)
        elif metric in ("weight_kg",):
            v = vals[-1]  # last reading of the day
        elif metric == "sleep_duration_min":
            day_sleep_recs = [{"start_at": m.start_at, "end_at": getattr(m, "end_at", None) or m.start_at, "value_json": m.value_json or {}} for m in rows if m.metric_type == mt and m.start_at.astimezone(tz).date().isoformat() == d]
            v = _dedup_sleep(day_sleep_recs)  # TOTAL sleep time (all sessions), not longest
        elif metric == "heart_rate_bpm":
            v = sum(vals) / len(vals)
        else:
            v = sum(vals)
        points.append({"date": d, "value": round(v, 1)})

    return {"metric_type": metric, "days": days, "points": points}


def _iso_week_bounds(d):
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=7)


def _quarter_month(d):
    return ((d.month - 1) // 3) * 3 + 1


@router.get("/trends/bucketed")
def trends_bucketed(
    metric: str = Query(...),
    granularity: Literal["day", "week", "month"] = "day",
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Daily/weekly/monthly buckets for a metric, with period navigation.

    granularity:
      day   -> current week (Mon-Sun), 7 daily points; offset = weeks back
      week  -> current quarter, 13 weekly points; offset = quarters back
      month -> last 12 months, 12 monthly points; offset ignored (fixed window)
    Empty buckets render as null (missing), never zero.
    """
    tz = ZoneInfo(user.timezone or "UTC")
    today = datetime.now(tz).date()

    # ---- window resolution ----
    if granularity == "day":
        base_monday, _ = _iso_week_bounds(today)
        start = base_monday - timedelta(weeks=offset)
        end = start + timedelta(days=7)
        n = 7
        def bucket_of(dt):
            idx = (dt.date() - start).days
            if idx < 0 or idx >= 7:
                return None, None
            return idx, dt.date().strftime("%a")
        period_label = f"Week of {start.strftime('%b %d, %Y')}"
        period_key = start.isoformat()
        def day_at(i):
            return start + timedelta(days=i)
    elif granularity == "week":
        # quarter of (today - offset quarters)
        cur_y = today.year
        cur_q = _quarter_month(today)
        total_q = (cur_y * 4 + (cur_q - 1) // 3) - offset
        y = total_q // 4
        qm = ((total_q % 4) * 3) + 1
        start = date_type(y, qm, 1)
        if qm + 3 > 12:
            end = date_type(y + 1, 1, 1)
        else:
            end = date_type(y, qm + 3, 1)
        n = 13
        def bucket_of(dt):
            monday0, _ = _iso_week_bounds(dt.date())
            base_m, _ = _iso_week_bounds(start)
            idx = (monday0 - base_m).days // 7
            if idx < 0 or idx >= 13:
                return None, None
            return idx, f"W{dt.date().isocalendar()[1]}"
        period_label = f"Q{((qm-1)//3)+1} {y}"
        period_key = start.isoformat()
        def day_at(i):
            return start + timedelta(weeks=i)
    else:  # month
        start = date_type(today.year - 1, today.month, 1)
        end = date_type(today.year, today.month, 1)
        n = 12
        def bucket_of(dt):
            d = dt.date()
            idx = (d.year - start.year) * 12 + (d.month - start.month)
            if idx < 0 or idx >= 12:
                return None, None
            return idx, d.strftime("%b")
        period_label = "Last 12 months"
        period_key = start.isoformat()
        def day_at(i):
            m = start.month - 1 + i
            return date_type(start.year + m // 12, m % 12 + 1, 1)

    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=tz)
    end_dt = datetime.combine(end, datetime.min.time(), tzinfo=tz)

    # ---- calories come from meals ----
    if metric in ("calories", "calories_kcal"):
        meals = (db.query(Meal)
                 .filter(Meal.user_id == user.id,
                         Meal.eaten_at >= start_dt, Meal.eaten_at < end_dt,
                         Meal.status != "deleted")
                 .all())
        buckets: list[list[float]] = [[] for _ in range(n)]
        for m in meals:
            idx, _ = bucket_of(m.eaten_at)
            if idx is None:
                continue
            buckets[idx].append(float((m.totals_json or {}).get("kcal") or 0))
        points = []
        for i in range(n):
            v = round(sum(buckets[i]), 1) if buckets[i] else None
            d = day_at(i)
            lab = bucket_of(datetime.combine(d, datetime.min.time(), tzinfo=tz))[1]
            points.append({"date": d.isoformat(), "value": v, "label": lab})
        return {"granularity": granularity, "metric": metric, "period_label": period_label,
                "period_key": period_key, "points": points}

    # ---- measurement metrics ----
    metric_map = {
        "steps": (MetricType.STEPS, "sum"),
        "water_ml": (MetricType.WATER, "sum"),
        "sleep_duration_min": (MetricType.SLEEP, "max"),
        "heart_rate_bpm": (MetricType.HEART_RATE, "avg"),
        "weight_kg": (MetricType.WEIGHT, "last"),
    }
    if metric not in metric_map:
        raise HTTPException(status_code=400, detail=f"Unsupported metric: {metric}")
    mt, agg = metric_map[metric]

    rows = (db.query(Measurement)
            .filter(Measurement.user_id == user.id,
                    Measurement.metric_type == mt,
                    Measurement.start_at >= start_dt, Measurement.start_at < end_dt)
            .order_by(Measurement.start_at.asc())
            .all())

    step_ratio = 1.0
    sleep_ratio = 1.0
    if mt == MetricType.SLEEP and rows:
        sleep_recs = [{'start_at': m.start_at, 'end_at': getattr(m, 'end_at', None) or m.start_at, 'value_json': m.value_json or {}} for m in rows]
        sleep_ded = _dedup_sleep(sleep_recs)
        sleep_raw = sum((m.value_json or {}).get('duration_min', 0) for m in rows)
        sleep_ratio = sleep_ded / sleep_raw if sleep_raw > 0 else 1.0
    if mt == MetricType.STEPS and rows:
        step_records = [{"start_at": m.start_at, "end_at": getattr(m, "end_at", None) or m.start_at, "value_json": m.value_json or {}} for m in rows]
        deduped_total = _dedup_steps(step_records)
        raw_total = sum((m.value_json or {}).get("count", 0) for m in rows)
        step_ratio = deduped_total / raw_total if raw_total > 0 else 1.0

    buckets = [[] for _ in range(n)]
    for m in rows:
        vj = m.value_json or {}
        v = vj.get("value")
        if v is None and mt == MetricType.STEPS:
            v = (vj.get("count", 0)) * step_ratio
        if v is None and mt == MetricType.SLEEP:
            v = (vj.get("duration_min", 0)) * sleep_ratio
        if v is None:
            if mt == MetricType.STEPS:
                v = vj.get("count")
            elif mt == MetricType.WEIGHT:
                v = vj.get("weight_kg")
            elif mt == MetricType.SLEEP:
                v = vj.get("duration_min")
            elif mt == MetricType.WATER:
                v = vj.get("amount_ml") or vj.get("volume_ml") or vj.get("water_ml")
            elif mt == MetricType.HEART_RATE:
                v = vj.get("bpm") or vj.get("heart_rate")
        if v is None:
            continue
        idx, _ = bucket_of(m.start_at)
        if idx is None:
            continue
        buckets[idx].append(float(v))

    points = []
    for i in range(n):
        vals = buckets[i]
        if not vals:
            points.append({"date": day_at(i).isoformat(), "value": None, "label": ""})
            continue
        if agg == "sum":
            v = sum(vals)
        elif agg == "avg":
            v = sum(vals) / len(vals)
        elif agg == "max":
            v = max(vals)
        else:
            v = vals[-1]
        d = day_at(i)
        lab = bucket_of(datetime.combine(d, datetime.min.time(), tzinfo=tz))[1]
        points.append({"date": d.isoformat(), "value": round(v, 1), "label": lab})

    return {"granularity": granularity, "metric": metric, "period_label": period_label,
            "period_key": period_key, "points": points}


@router.get("/trends/liquids")
def liquids_bucketed(
    granularity: Literal["day", "week", "month"] = "day",
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Liquid intake bucketed by category, for the Liquid tile popup.

    Mirrors /trends/bucketed window logic but returns, per bucket, both the
    total and a per-category series so the frontend can draw a total chart
    plus a category distribution chart.

      day   -> current week (Mon-Sun), 7 daily points; offset = weeks back
      week  -> current quarter, 13 weekly points; offset = quarters back
      month -> last 12 months, 12 monthly points; offset ignored (fixed window)
    Empty buckets are null (missing), never zero.
    """
    tz = ZoneInfo(user.timezone or "UTC")
    today = datetime.now(tz).date()
    cats = ["water", "non_alcoholic", "beer", "wine", "spirits", "other_alcohol"]

    if granularity == "day":
        base_monday, _ = _iso_week_bounds(today)
        start = base_monday - timedelta(weeks=offset)
        end = start + timedelta(days=7)
        n = 7
        def bucket_of(dt):
            idx = (dt.date() - start).days
            if idx < 0 or idx >= 7:
                return None, None
            return idx, dt.date().strftime("%a")
        period_label = f"Week of {start.strftime('%b %d, %Y')}"
        period_key = start.isoformat()
        def day_at(i):
            return start + timedelta(days=i)
    elif granularity == "week":
        cur_y = today.year
        cur_q = _quarter_month(today)
        total_q = (cur_y * 4 + (cur_q - 1) // 3) - offset
        y = total_q // 4
        qm = ((total_q % 4) * 3) + 1
        start = date_type(y, qm, 1)
        if qm + 3 > 12:
            end = date_type(y + 1, 1, 1)
        else:
            end = date_type(y, qm + 3, 1)
        n = 13
        def bucket_of(dt):
            monday0, _ = _iso_week_bounds(dt.date())
            base_m, _ = _iso_week_bounds(start)
            idx = (monday0 - base_m).days // 7
            if idx < 0 or idx >= 13:
                return None, None
            return idx, f"W{dt.date().isocalendar()[1]}"
        period_label = f"Q{((qm-1)//3)+1} {y}"
        period_key = start.isoformat()
        def day_at(i):
            return start + timedelta(weeks=i)
    else:  # month
        start = date_type(today.year - 1, today.month, 1)
        end = date_type(today.year, today.month, 1)
        n = 12
        def bucket_of(dt):
            d = dt.date()
            idx = (d.year - start.year) * 12 + (d.month - start.month)
            if idx < 0 or idx >= 12:
                return None, None
            return idx, d.strftime("%b")
        period_label = "Last 12 months"
        period_key = start.isoformat()
        def day_at(i):
            m = start.month - 1 + i
            return date_type(start.year + m // 12, m % 12 + 1, 1)

    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=tz)
    end_dt = datetime.combine(end, datetime.min.time(), tzinfo=tz)

    rows = (db.query(Measurement)
            .filter(Measurement.user_id == user.id,
                    Measurement.metric_type == MetricType.WATER,
                    Measurement.start_at >= start_dt, Measurement.start_at < end_dt)
            .order_by(Measurement.start_at.asc())
            .all())

    # per-bucket per-category sums
    buckets = [dict((c, 0) for c in cats) for _ in range(n)]
    for m in rows:
        vj = m.value_json or {}
        amt = vj.get("amount_ml") or vj.get("volume_ml") or vj.get("water_ml")
        if amt is None:
            continue
        idx, _ = bucket_of(m.start_at)
        if idx is None:
            continue
        cat = vj.get("category") or "water"
        if cat not in cats:
            cat = "water"
        buckets[idx][cat] += float(amt)

    points = []
    for i in range(n):
        row = buckets[i]
        has = any(v > 0 for v in row.values())
        d = day_at(i)
        lab = bucket_of(datetime.combine(d, datetime.min.time(), tzinfo=tz))[1]
        points.append({
            "date": d.isoformat(),
            "label": lab,
            "total_ml": round(sum(row.values()), 1) if has else None,
            **{c: (round(row[c], 1) if row[c] > 0 else None) for c in cats},
        })

    return {"granularity": granularity, "metric": "liquids", "period_label": period_label,
            "period_key": period_key, "categories": cats, "points": points}


@router.get("/goals")
def list_goals(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    goals = db.query(Goal).filter(Goal.user_id == user.id, Goal.status == "active").all()
    return [
        {
            "id": str(g.id),
            "goal_type": g.goal_type,
            "target_json": g.target_json,
            "start_date": g.start_date,
            "end_date": g.end_date,
            "source": g.source,
        }
        for g in goals
    ]


class GoalCreateRequest(BaseModel):
    goal_type: str
    target_json: dict
    start_date: str | None = None
    end_date: str | None = None


@router.post("/goals")
def create_goal(req: GoalCreateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Create a goal for the user. start_date defaults to today (YYYY-MM-DD)."""
    from datetime import date as _date

    start_date = req.start_date or _date.today().isoformat()
    goal = Goal(
        user_id=user.id,
        goal_type=req.goal_type,
        target_json=req.target_json,
        start_date=start_date,
        end_date=req.end_date,
        source="user",
        status="active",
    )
    db.add(goal)
    db.commit()
    db.refresh(goal)
    audit(db, "user", str(user.id), user.id, "goals.create", "goal", str(goal.id))
    db.commit()
    return {
        "id": str(goal.id),
        "goal_type": goal.goal_type,
        "target_json": goal.target_json,
        "start_date": goal.start_date,
        "end_date": goal.end_date,
        "source": goal.source,
        "status": goal.status,
    }


@router.get("/summaries/monthly/{month}")
def monthly_summary(month: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Monthly aggregate for weight and blood pressure (YYYY-MM), in the user's timezone.

    Sections are null when there is no data for the month.
    """
    from zoneinfo import ZoneInfo

    try:
        year_s, mon_s = month.split("-")
        year, mon = int(year_s), int(mon_s)
        if not (1 <= mon <= 12) or not (2000 <= year <= 2100):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=400, detail="Use month format YYYY-MM")

    tz = ZoneInfo(user.timezone)
    start = datetime(year, mon, 1, tzinfo=tz)
    if mon == 12:
        end = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end = datetime(year, mon + 1, 1, tzinfo=tz)

    measurements = (
        db.query(Measurement)
        .filter(
            Measurement.user_id == user.id,
            Measurement.start_at >= start,
            Measurement.start_at < end,
        )
        .all()
    )
    ms = [
        {"metric_type": m.metric_type, "value_json": m.value_json, "start_at": m.start_at}
        for m in measurements
    ]

    weight_meas = [m for m in ms if m["metric_type"] == MetricType.WEIGHT]
    bp_meas = [m for m in ms if m["metric_type"] == MetricType.BLOOD_PRESSURE]

    weight_section = None
    if weight_meas:
        ordered = sorted(weight_meas, key=lambda m: m["start_at"])
        first_kg = ordered[0]["value_json"].get("weight_kg")
        last_kg = ordered[-1]["value_json"].get("weight_kg")
        weight_section = {
            "samples": len(weight_meas),
            "first_kg": first_kg,
            "last_kg": last_kg,
            "delta_kg": round(last_kg - first_kg, 1)
            if first_kg is not None and last_kg is not None
            else None,
        }

    bp_section = None
    if bp_meas:
        avg = bp_average_by_context(bp_meas)
        bp_section = {
            "samples": len(bp_meas),
            "avg_systolic": avg["avg_systolic"] if avg else None,
            "avg_diastolic": avg["avg_diastolic"] if avg else None,
            "avg_pulse": avg.get("avg_pulse") if avg else None,
        }

    return {
        "month": month,
        "weight": weight_section,
        "blood_pressure": bp_section,
    }
