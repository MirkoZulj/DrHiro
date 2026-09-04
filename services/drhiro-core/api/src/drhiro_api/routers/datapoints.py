"""Generic data-point CRUD router.

Provides a uniform log / list (find) / update (edit) / delete surface for any
metric stored in the ``measurements`` table (water, steps, sleep, weight,
blood_pressure, exercise, heart_rate, distance, active_calories, ...) plus
edit/delete for activities (which live in the separate ``activities`` table).

Endpoints (all under /api/v1/data-points):
  GET    /data-points                list/find measurements (metric_type, from/to)
  POST   /data-points                log a measurement (generic)
  PATCH  /data-points/{id}           edit a measurement value_json
  DELETE /data-points/{id}           delete a measurement
  PATCH  /data-points/activity/{id}  edit an activity (title/calories/description)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import Activity, Measurement, User
from drhiro_api.security import audit

router = APIRouter(prefix="/data-points", tags=["data-points"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class DataPointLogIn(BaseModel):
    metric_type: str = Field(description="e.g. water, steps, sleep, weight, blood_pressure, exercise")
    value: dict[str, Any] = Field(description="value payload, e.g. {'amount_ml': 300} or {'steps': 8000}")
    measured_at: datetime | None = None
    unit: str | None = None
    note: str | None = None


class DataPointOut(BaseModel):
    id: str
    metric_type: str
    value: dict[str, Any]
    unit: str | None
    measured_at: datetime
    end_at: datetime | None
    created_at: datetime


class DataPointListOut(BaseModel):
    metric_type: str | None
    from_: datetime | None = None
    to_: datetime | None = None
    count: int
    items: list[DataPointOut]


class DataPointUpdateIn(BaseModel):
    value: dict[str, Any] | None = None
    measured_at: datetime | None = None
    unit: str | None = None


class ActivityUpdateIn(BaseModel):
    title: str | None = None
    description: str | None = None
    calories_burned: float | None = None
    activity_date: str | None = None


class SimpleResult(BaseModel):
    ok: bool
    id: str | None = None
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _measurement_out(m: Measurement) -> DataPointOut:
    return DataPointOut(
        id=str(m.id),
        metric_type=m.metric_type,
        value=m.value_json or {},
        unit=m.unit,
        measured_at=m.start_at,
        end_at=m.end_at,
        created_at=m.created_at,
    )


def _get_measurement(db: Session, user: User, mid: str) -> Measurement:
    try:
        mid_uuid = uuid.UUID(mid)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Measurement not found.")
    m = (
        db.query(Measurement)
        .filter(Measurement.id == mid_uuid, Measurement.user_id == user.id)
        .first()
    )
    if not m:
        raise HTTPException(status_code=404, detail="Measurement not found.")
    return m


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
@router.post("", response_model=SimpleResult)
def log_data_point(
    req: DataPointLogIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generic log of a measurement of any metric type."""
    metric = req.metric_type.strip().lower()
    if not metric:
        raise HTTPException(status_code=422, detail="metric_type is required.")
    now = datetime.now(timezone.utc)
    start = req.measured_at or now
    m = Measurement(
        user_id=user.id,
        metric_type=metric,
        start_at=start,
        end_at=req.measured_at or now,
        value_json=req.value or {},
        unit=req.unit,
        source_provider="manual",
        source_record_id=f"manual-{uuid.uuid4().hex}",
        recording_method="manual",
        confidence=1.0,
        metadata_json={"note": req.note} if req.note else None,
    )
    db.add(m)
    audit(db, "user", str(user.id), user.id, f"ingest.manual_{metric}", "measurement", str(m.id))
    db.commit()
    db.refresh(m)
    return SimpleResult(ok=True, id=str(m.id), message=f"Logged {metric}.")


@router.get("", response_model=DataPointListOut)
def list_data_points(
    metric_type: str | None = Query(None),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Find measurements. Filter by metric_type and/or time window."""
    q = db.query(Measurement).filter(Measurement.user_id == user.id)
    if metric_type:
        q = q.filter(Measurement.metric_type == metric_type.strip().lower())
    if from_:
        q = q.filter(Measurement.start_at >= from_)
    if to:
        q = q.filter(Measurement.start_at <= to)
    rows = q.order_by(Measurement.start_at.desc()).limit(limit).all()
    return DataPointListOut(
        metric_type=metric_type.strip().lower() if metric_type else None,
        from_=from_,
        to_=to,
        count=len(rows),
        items=[_measurement_out(r) for r in rows],
    )


@router.patch("/{mid}", response_model=SimpleResult)
def update_data_point(
    mid: str,
    req: DataPointUpdateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Edit a measurement (its value payload, timestamp, or unit)."""
    m = _get_measurement(db, user, mid)
    if req.value is not None:
        m.value_json = req.value
    if req.measured_at is not None:
        m.start_at = req.measured_at
        m.end_at = req.measured_at
    if req.unit is not None:
        m.unit = req.unit
    audit(db, "user", str(user.id), user.id, "data_point.update", "measurement", str(m.id))
    db.commit()
    return SimpleResult(ok=True, id=str(m.id), message="Measurement updated.")


@router.delete("/{mid}", response_model=SimpleResult)
def delete_data_point(
    mid: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a measurement."""
    m = _get_measurement(db, user, mid)
    db.delete(m)
    audit(db, "user", str(user.id), user.id, "data_point.delete", "measurement", str(m.id))
    db.commit()
    return SimpleResult(ok=True, id=str(m.id), message="Measurement deleted.")


# ---------------------------------------------------------------------------
# Activities (separate table)
# ---------------------------------------------------------------------------
@router.patch("/activity/{aid}", response_model=SimpleResult)
def update_activity(
    aid: str,
    req: ActivityUpdateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Edit an activity (title, description, calories, or date)."""
    try:
        aid_uuid = uuid.UUID(aid)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Activity not found.")
    a = (
        db.query(Activity)
        .filter(Activity.id == aid_uuid, Activity.user_id == user.id)
        .first()
    )
    if not a:
        raise HTTPException(status_code=404, detail="Activity not found.")
    if req.title is not None:
        a.title = req.title.strip()
    if req.description is not None:
        a.description = req.description
    if req.calories_burned is not None:
        a.calories_burned = req.calories_burned
    if req.activity_date is not None:
        from datetime import date as date_type
        a.activity_date = date_type.fromisoformat(req.activity_date)
    audit(db, "user", str(user.id), user.id, "data_point.update_activity", "activity", str(a.id))
    db.commit()
    return SimpleResult(ok=True, id=str(a.id), message="Activity updated.")
