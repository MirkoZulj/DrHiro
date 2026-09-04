"""Reminders endpoints. Sections 8.5 and 10.

The reminder engine itself (occurrence generation, completion matching,
anti-nag) lives in the worker; this router provides CRUD + snooze/skip.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import Reminder, ReminderOccurrence, User
from drhiro_api.security import audit

router = APIRouter(prefix="/reminders", tags=["reminders"])


class ReminderCreateRequest(BaseModel):
    type: str
    schedule_json: dict
    timezone: str = "UTC"
    enabled: bool = True
    quiet_hours_json: dict | None = None
    escalation_policy_json: dict | None = None


class ReminderPatchRequest(BaseModel):
    enabled: bool | None = None
    schedule_json: dict | None = None
    timezone: str | None = None
    quiet_hours_json: dict | None = None
    escalation_policy_json: dict | None = None


def _reminder_out(r: Reminder) -> dict:
    return {
        "id": str(r.id),
        "type": r.type,
        "schedule_json": r.schedule_json,
        "timezone": r.timezone,
        "enabled": r.enabled,
        "quiet_hours_json": r.quiet_hours_json,
        "escalation_policy_json": r.escalation_policy_json,
        "next_due_at": r.next_due_at,
    }


@router.get("")
def list_reminders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    reminders = db.query(Reminder).filter(Reminder.user_id == user.id).all()
    return [_reminder_out(r) for r in reminders]


@router.post("")
def create_reminder(req: ReminderCreateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if "cron" not in req.schedule_json and "days" not in req.schedule_json:
        raise HTTPException(status_code=400, detail="schedule_json needs 'cron' or 'days'")
    reminder = Reminder(
        user_id=user.id,
        type=req.type,
        schedule_json=req.schedule_json,
        timezone=req.timezone,
        enabled=req.enabled,
        quiet_hours_json=req.quiet_hours_json,
        escalation_policy_json=req.escalation_policy_json,
    )
    db.add(reminder)
    db.flush()
    reminder.next_due_at = _compute_next_due(reminder)
    audit(db, "user", str(user.id), user.id, "reminders.create", "reminder", str(reminder.id), {"type": req.type})
    db.commit()
    db.refresh(reminder)
    return _reminder_out(reminder)


@router.patch("/{reminder_id}")
def patch_reminder(reminder_id: str, req: ReminderPatchRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    reminder = db.query(Reminder).filter(Reminder.id == uuid.UUID(reminder_id), Reminder.user_id == user.id).first()
    if not reminder:
        raise HTTPException(status_code=404, detail="Reminder not found")
    for field in ("enabled", "schedule_json", "timezone", "quiet_hours_json", "escalation_policy_json"):
        val = getattr(req, field)
        if val is not None:
            setattr(reminder, field, val)
    reminder.next_due_at = _compute_next_due(reminder)
    db.commit()
    db.refresh(reminder)
    return _reminder_out(reminder)


def _compute_next_due(reminder: Reminder) -> datetime:
    """Compute the next due time from schedule_json.

    Supports:
      - {"cron": "0 8 * * *"} -> minute hour dom month dow
      - {"days": ["mon","wed"], "time": "08:00"} -> weekly on listed days
    """
    schedule = reminder.schedule_json or {}
    tz = reminder.timezone or "UTC"
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(tz))
    try:
        if "cron" in schedule:
            parts = schedule["cron"].split()
            if len(parts) != 5:
                return now + timedelta(days=1)
            hour, minute = int(parts[1]), int(parts[0])
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate
        if "days" in schedule and "time" in schedule:
            days = set(schedule["days"])
            hh, mm = schedule["time"].split(":")
            hour, minute = int(hh), int(mm)
            weekday_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            for offset in range(0, 8):
                candidate = now + timedelta(days=offset)
                if candidate.weekday() in [weekday_names.index(d) for d in days] and (offset > 0 or (candidate.hour, candidate.minute) < (hour, minute)):
                    return candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return now + timedelta(days=1)
    except (ValueError, KeyError, IndexError):
        pass
    return now + timedelta(days=1)


@router.get("/occurrences")
def list_occurrences(
    status: str | None = None,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(ReminderOccurrence).join(Reminder).filter(Reminder.user_id == user.id)
    if status:
        q = q.filter(ReminderOccurrence.status == status)
    occurrences = q.order_by(ReminderOccurrence.due_at.desc()).limit(limit).all()
    return [
        {
            "id": str(o.id),
            "reminder_id": str(o.reminder_id),
            "due_at": o.due_at,
            "status": o.status,
            "sent_at": o.sent_at,
            "completed_by_record_id": o.completed_by_record_id,
        }
        for o in occurrences
    ]


class SnoozeRequest(BaseModel):
    duration_minutes: int = Field(default=15, ge=5, le=24 * 60)


@router.post("/occurrences/{occurrence_id}/snooze")
def snooze_occurrence(occurrence_id: str, req: SnoozeRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    occ = (
        db.query(ReminderOccurrence)
        .join(Reminder)
        .filter(ReminderOccurrence.id == uuid.UUID(occurrence_id), Reminder.user_id == user.id)
        .first()
    )
    if not occ:
        raise HTTPException(status_code=404, detail="Occurrence not found")
    if occ.status == "completed":
        raise HTTPException(status_code=400, detail="Already completed")
    occ.status = "snoozed"
    occ.due_at = datetime.now(timezone.utc) + timedelta(minutes=req.duration_minutes)
    db.commit()
    return {"id": str(occ.id), "status": occ.status, "due_at": occ.due_at}


@router.post("/occurrences/{occurrence_id}/skip")
def skip_occurrence(occurrence_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    occ = (
        db.query(ReminderOccurrence)
        .join(Reminder)
        .filter(ReminderOccurrence.id == uuid.UUID(occurrence_id), Reminder.user_id == user.id)
        .first()
    )
    if not occ:
        raise HTTPException(status_code=404, detail="Occurrence not found")
    occ.status = "skipped"
    db.commit()
    return {"id": str(occ.id), "status": occ.status}
