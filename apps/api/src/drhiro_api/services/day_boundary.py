"""Canonical nutrition-day boundary for drHiro.

A nutrition day begins with the day's FIRST MEAL (breakfast), not at local
midnight. A meal eaten at 00:01 local therefore belongs to the PREVIOUS day.
That is expressed as a boundary hour: with the default
``DRHIRO_DAY_START_HOUR=4``, any local time-of-day before 04:00 counts as the
previous day. ``DRHIRO_DAY_START_HOUR=0`` restores the old calendar-day
behaviour (local midnight boundary).

Every endpoint that buckets meals, liquids or activities into a day must go
through this module so the "today" tile, the trend charts, the daily summary
and the activity dates all agree.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_DAY_START_HOUR = 4
_ENV_VAR = "DRHIRO_DAY_START_HOUR"

# UTC-offset spellings a user may give instead of an IANA name:
#   "+09:00", "+9", "UTC+9", "GMT-3", "-0530", "utc +5:30"
_OFFSET_RE = re.compile(
    r"^\s*(?:(?:UTC|GMT)\s*)?([+-])\s*(\d{1,2})(?::?(\d{2}))?\s*$",
    re.IGNORECASE,
)


def parse_timezone(raw) -> str:
    """Validate and canonicalise a user timezone.

    Accepts an IANA / tz-database name (``Asia/Tokyo``) or a UTC-offset
    spelling (``UTC+9``, ``+09:00``, ``GMT-3``, ``-0530``). Returns the value
    to STORE in ``users.timezone``: the IANA name unchanged, or a canonical
    fixed-offset string like ``UTC+09:00``. Raises ``ValueError`` for anything
    that is neither a loadable zone nor a valid offset — the caller turns that
    into a 400 so the bot can tell the user the name was not understood.
    """
    if raw is None:
        raise ValueError("timezone is required")
    s = str(raw).strip()
    if not s:
        raise ValueError("timezone is empty")
    # 1) IANA / tz database name (ZoneInfo is the authority).
    try:
        ZoneInfo(s)
        return s
    except Exception:
        pass
    # 2) UTC-offset spelling.
    m = _OFFSET_RE.match(s)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2))
        minutes = int(m.group(3) or 0)
        if minutes > 59 or hours > 14 or (hours == 14 and minutes > 0):
            raise ValueError(f"UTC offset out of range: {raw!r}")
        total = sign * (hours * 60 + minutes)
        if abs(total) > 14 * 60:
            raise ValueError(f"UTC offset out of range: {raw!r}")
        if total == 0:
            return "UTC"
        off_sign = "+" if total >= 0 else "-"
        off_abs = abs(total)
        return f"UTC{off_sign}{off_abs // 60:02d}:{off_abs % 60:02d}"
    raise ValueError(f"Unrecognised timezone: {raw!r}")


def day_start_hour() -> int:
    """The configured day boundary hour (0-23); default 4.

    4 means "a local time before 04:00 counts as the previous day".
    A missing, unparsable or out-of-range value falls back to the default.
    """
    raw = os.environ.get(_ENV_VAR)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_DAY_START_HOUR
    try:
        hour = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_DAY_START_HOUR
    if 0 <= hour <= 23:
        return hour
    return DEFAULT_DAY_START_HOUR


def resolve_tz(tz) -> ZoneInfo | timezone:
    """Coerce a tz name / tzinfo / None into a usable tzinfo (UTC on failure).

    Handles the canonical fixed-offset strings ``parse_timezone`` produces
    (``UTC+09:00``) as well as IANA names, so a user who set their zone by
    offset is bucketed on the correct wall clock everywhere.
    """
    if tz is None:
        return timezone.utc
    if isinstance(tz, str):
        s = tz.strip()
        try:
            return ZoneInfo(s)
        except Exception:
            pass
        m = _OFFSET_RE.match(s)
        if m:
            sign = 1 if m.group(1) == "+" else -1
            hours = int(m.group(2))
            minutes = int(m.group(3) or 0)
            try:
                return timezone(sign * timedelta(hours=hours, minutes=minutes), s)
            except Exception:
                return timezone.utc
        return timezone.utc
    return tz


def local_now(tz) -> datetime:
    """Current wall-clock time in ``tz`` (aware)."""
    return datetime.now(resolve_tz(tz))


def utc_offset_str(dt: datetime) -> str:
    """``+09:00`` / ``-05:30`` for an aware datetime."""
    off = dt.utcoffset() or timedelta(0)
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def nutrition_date(ts: datetime, tz) -> date:
    """The nutrition day that ``ts`` belongs to, in timezone ``tz``.

    This is the local calendar date shifted back one day when the local
    time-of-day is before :func:`day_start_hour`. Naive datetimes are treated
    as UTC.
    """
    zone = resolve_tz(tz)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    local = ts.astimezone(zone)
    return (local - timedelta(hours=day_start_hour())).date()


def nutrition_day_start(user) -> datetime:
    """tz-aware UTC instant at which the user's CURRENT nutrition day began."""
    start_utc, _ = nutrition_day_window(user)
    return start_utc


def nutrition_day_window(user) -> tuple[datetime, datetime]:
    """(start_utc, end_utc) of the user's current nutrition day.

    ``start_utc`` is the boundary-hour instant of the nutrition day the user is
    currently in; ``end_utc`` is the boundary hour of the following day.
    """
    zone = resolve_tz(getattr(user, "timezone", None))
    now = datetime.now(timezone.utc)
    d = nutrition_date(now, zone)
    hour = day_start_hour()
    start_local = datetime(d.year, d.month, d.day, hour, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def local_date_window(local_date: str, tz) -> tuple[datetime, datetime]:
    """(start_utc, end_utc) of the nutrition day labelled ``local_date``.

    ``local_date`` is the ISO date reported by :func:`nutrition_date`; the
    window runs from ``day_start_hour`` on that date to the same hour the next
    day (both in ``tz``).
    """
    zone = resolve_tz(tz)
    day = date.fromisoformat(local_date)
    hour = day_start_hour()
    start_local = datetime(day.year, day.month, day.day, hour, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Device-measurement bucketing — the LOCAL CALENDAR DAY, never the boundary
# --------------------------------------------------------------------------- #
# The 04:00 nutrition-day boundary exists for FOOD a user eats and for the
# water/consumption they log. It must NOT be applied to wearable MEASUREMENTS:
# steps, sleep, exercise, heart_rate, resting_heart_rate, distance,
# active_calories, spo2, weight, blood_pressure. Those belong to the calendar
# day of their own timestamp. A daily step total the Bridge stamps at local
# midnight (``start_at`` == 00:00 local, ``22:00 UTC`` in Europe/Zagreb) is
# therefore TODAY's total, not yesterday's.

def local_calendar_date(ts: datetime, tz) -> date:
    """The plain LOCAL CALENDAR date of ``ts`` (no boundary shift)."""
    zone = resolve_tz(tz)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(zone).date()


def local_calendar_day_start(user) -> datetime:
    """tz-aware UTC instant of the user's local MIDNIGHT today (calendar day).

    The measurement counterpart of :func:`nutrition_day_start`: steps logged at
    00:30 local are TODAY's steps, even though a meal eaten at 00:30 belongs to
    the previous eating day.
    """
    zone = resolve_tz(getattr(user, "timezone", None))
    now = datetime.now(zone)
    start_local = datetime(now.year, now.month, now.day, 0, 0, tzinfo=zone)
    return start_local.astimezone(timezone.utc)


def local_calendar_date_window(local_date: str, tz) -> tuple[datetime, datetime]:
    """(start_utc, end_utc) of the LOCAL CALENDAR day labelled ``local_date``."""
    zone = resolve_tz(tz)
    day = date.fromisoformat(local_date)
    start_local = datetime(day.year, day.month, day.day, 0, 0, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def wake_date(start_at: datetime, duration_min, end_at: datetime | None, tz) -> date | None:
    """The WAKE-UP local calendar day of a sleep record.

    Xiaomi/Mi Fitness (and Health Connect) key a night's sleep to the date the
    wearer WOKE, not the date they went to bed. Our rows carry ``start_at`` plus
    ``duration_min`` (and usually ``end_at``), so the wake-up day is computable.
    Falls back to ``end_at`` and then to the start's own calendar day.
    """
    if start_at is None:
        return None
    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=timezone.utc)
    if duration_min:
        wake = start_at + timedelta(minutes=float(duration_min))
    elif end_at is not None:
        if end_at.tzinfo is None:
            end_at = end_at.replace(tzinfo=timezone.utc)
        wake = end_at
    else:
        wake = start_at
    return wake.astimezone(resolve_tz(tz)).date()


# --------------------------------------------------------------------------- #
# Device-measurement day attribution — the DEVICE's own timezone + interval
# --------------------------------------------------------------------------- #
# A wearable stamps its daily totals in the timezone the DEVICE was in
# (``metadata_json.source_timezone``, sent by the Health Connect bridge), which
# need not equal the profile timezone (travel, a stale profile setting, ...).
# Bucketing such a row by the PROFILE timezone shifts a whole day: a
# Europe/Zagreb-midnight daily steps total starts at 17:00 the previous
# America/Chicago day, so "today" read 0 until 23:00 UTC and every trend point
# landed one day early. Sleep had the mirror bug: a 06:00 Zagreb wake-up is
# still the previous evening in Chicago, so wake_date keyed the night to the
# bedtime day.
#
# Rule: a device MEASUREMENT belongs to the local calendar day, in the
# timezone the DEVICE stamped it with, of the day its INTERVAL covers:
#   * steps / interval metrics -> the interval MIDPOINT (a 24h daily total
#     anchored at device midnight attributes to its own day in any profile
#     timezone; short buckets are unaffected, midpoint ~= start);
#   * sleep -> the WAKE-UP day (``wake_date`` above), in the device timezone.
# Rows without a source timezone fall back to the profile timezone, which is
# the historical behaviour.


def source_tz_of(metadata_json, fallback_tz):
    """The tzinfo a measurement row was stamped in.

    Reads ``metadata_json["source_timezone"]`` (written by the ingest router
    from the bridge's ``source_timezone`` field); falls back to ``fallback_tz``
    (usually the profile timezone) when absent/unparsable.
    """
    raw = None
    if isinstance(metadata_json, dict):
        raw = metadata_json.get("source_timezone")
    if raw:
        try:
            return resolve_tz(raw)
        except Exception:
            pass
    return resolve_tz(fallback_tz)


def interval_midpoint(start_at: datetime, end_at: datetime | None) -> datetime:
    """Midpoint instant of a measurement interval (zero-duration -> start)."""
    if start_at is None:
        return None
    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=timezone.utc)
    end = end_at or start_at
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if end <= start_at:
        return start_at
    return start_at + (end - start_at) / 2


def interval_day(start_at: datetime, end_at: datetime | None, tz) -> date | None:
    """Local calendar day, in ``tz``, of a measurement interval's midpoint."""
    mid = interval_midpoint(start_at, end_at)
    if mid is None:
        return None
    return mid.astimezone(resolve_tz(tz)).date()


def measurement_day(row: dict, fallback_tz) -> date | None:
    """The day a steps/interval measurement belongs to.

    ``row`` carries ``start_at`` / ``end_at`` / ``metadata_json`` (the shape
    ``_measurements_since`` and ``daily_series`` build). Day = interval
    midpoint in the row's own source timezone.
    """
    start = row.get("start_at") or row.get("recorded_at")
    if start is None:
        return None
    tz = source_tz_of(row.get("metadata_json"), fallback_tz)
    return interval_day(start, row.get("end_at"), tz)


def sleep_day(row: dict, fallback_tz) -> date | None:
    """The wake-up day a sleep measurement belongs to, in its source timezone."""
    start = row.get("start_at") or row.get("recorded_at")
    if start is None:
        return None
    tz = source_tz_of(row.get("metadata_json"), fallback_tz)
    return wake_date(
        start,
        (row.get("value_json") or {}).get("duration_min"),
        row.get("end_at"),
        tz,
    )