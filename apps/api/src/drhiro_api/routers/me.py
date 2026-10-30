"""Current-user profile endpoints. Section 8.x (device-reported timezone).

The web app runs on the user's DEVICE and can read its real zone with
``Intl.DateTimeFormat().resolvedOptions().timeZone``. It reports that here on
load so meal dates, the day boundary and reminders follow the device clock even
when the user travels — Telegram itself cannot supply a timezone.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import User
from drhiro_api.security import audit
from drhiro_api.services.day_boundary import (
    day_start_hour,
    local_now,
    nutrition_date,
    parse_timezone,
    utc_offset_str,
)

router = APIRouter(prefix="/me", tags=["me"])


class TimezoneRequest(BaseModel):
    # IANA name ("Asia/Tokyo") or UTC offset ("UTC+9" / "+09:00").
    timezone: str


def _tz_payload(user: User, previous: str | None = None) -> dict:
    now_local = local_now(user.timezone)
    data = {
        "timezone": user.timezone,
        "local_date": now_local.date().isoformat(),
        "local_time": now_local.strftime("%H:%M:%S"),
        "utc_offset": utc_offset_str(now_local),
        "nutrition_date": nutrition_date(datetime.now(timezone.utc), user.timezone).isoformat(),
        "day_start_hour": day_start_hour(),
    }
    if previous is not None:
        data["previous_timezone"] = previous
    return data


@router.get("")
def get_me(user: User = Depends(get_current_user)):
    """The caller's profile + the local clock derived from their timezone."""
    return {
        "id": str(user.id),
        "display_name": user.display_name,
        **_tz_payload(user),
    }


@router.post("/timezone")
def set_my_timezone(
    req: TimezoneRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set the current user's timezone (device-reported or explicit)."""
    try:
        tzname = parse_timezone(req.timezone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid timezone: {exc}")
    previous = user.timezone
    user.timezone = tzname
    audit(db, "user", str(user.id), user.id, "me.timezone_update", "user", str(user.id),
          {"from": previous, "to": tzname})
    db.commit()
    db.refresh(user)
    return _tz_payload(user, previous=previous)
