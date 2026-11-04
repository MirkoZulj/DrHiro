"""Medications domain service (0.9.0, spec §5.3).

Pure functions over a Session. Principles (spec §5.1):
- Records what the user says they take; never suggests changes.
- An expected dose with no log is "not logged", never "missed", never 0%.
- The only deterministic safety check is duplicate ingredients (rule engine).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from drhiro_api.models import (
    DoseEvent,
    MedicationItem,
    MedicationItemEvent,
    User,
)

SLOT_MATCH_WINDOW_HOURS = 4
WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def item_tz(item: MedicationItem, user: User) -> ZoneInfo:
    return ZoneInfo(item.timezone or user.timezone or "UTC")


def _paused_intervals(db: Session, item: MedicationItem) -> list[tuple[date | None, date | None]]:
    """[start, end) paused ranges from the append-only events. Open end = still paused."""
    intervals: list[tuple[date | None, date | None]] = []
    events = (
        db.query(MedicationItemEvent)
        .filter(MedicationItemEvent.item_id == item.id, MedicationItemEvent.kind.in_(["paused", "resumed"]))
        .order_by(MedicationItemEvent.effective_date, MedicationItemEvent.created_at)
        .all()
    )
    open_start: date | None = None
    for ev in events:
        if ev.kind == "paused":
            if open_start is None:
                open_start = ev.effective_date
        else:  # resumed
            if open_start is not None:
                intervals.append((open_start, ev.effective_date))
                open_start = None
    if open_start is not None:
        intervals.append((open_start, None))
    return intervals


def _day_paused(intervals: list[tuple[date | None, date | None]], day: date) -> bool:
    for start, end in intervals:
        if start is not None and day < start:
            continue
        if end is not None and day >= end:
            continue
        if start is None and end is None:
            return True
        return True
    return False


def expected_slots(item: MedicationItem, start: date, end: date, user_tz: str, db: Session) -> list[datetime]:
    """Expand schedule_json in item tz (or user tz) between
    max(start, item.start_date) and min(end, item.end_date, today).

    Excludes paused intervals; PRN -> []. DST-safe: local datetimes built with
    zoneinfo, converted to UTC.
    """
    sched = item.schedule_json or {}
    stype = sched.get("type", "daily")
    if stype == "prn":
        return []
    tz = item_tz(item, User(timezone=user_tz))
    today = datetime.now(tz).date()
    d0 = max(start, item.start_date or start)
    d1 = min(end, item.end_date or end, today)
    if d1 < d0:
        return []
    times: list[time] = []
    for t in sched.get("times", []):
        hh, mm = str(t).split(":")
        times.append(time(int(hh), int(mm)))
    if not times:
        return []
    paused = _paused_intervals(db, item)
    weekdays = {WEEKDAY_NAMES.index(d) for d in sched.get("days", [])}
    every_days = sched.get("every_days")
    anchor = date.fromisoformat(sched["anchor"]) if sched.get("anchor") else item.start_date
    slots: list[datetime] = []
    day = d0
    while day <= d1:
        if not _day_paused(paused, day):
            include = True
            if stype == "weekly":
                include = day.weekday() in weekdays
            elif stype == "interval":
                include = bool(anchor) and ((day - anchor).days % max(1, int(every_days or 1)) == 0)
            if include:
                for t in times:
                    # DST-safe: a nonexistent local time (spring forward) resolves
                    # via fold semantics; convert to UTC.
                    local_dt = datetime.combine(day, t, tzinfo=tz)
                    slots.append(local_dt.astimezone(timezone.utc))
        day += timedelta(days=1)
    slots.sort()
    return slots


def match_slot(item: MedicationItem, taken_at_utc: datetime, user_tz: str, db: Session) -> datetime | None:
    """Nearest expected slot within ±4 h that has no dose_event yet; else None (ad-hoc)."""
    day = taken_at_utc.astimezone(ZoneInfo(user_tz)).date()
    window_days = [day - timedelta(days=1), day, day + timedelta(days=1)]
    slots = []
    for d in window_days:
        slots.extend(expected_slots(item, d, d, user_tz, db))
    taken = {
        de.scheduled_for
        for de in db.query(DoseEvent)
        .filter(DoseEvent.item_id == item.id, DoseEvent.scheduled_for.isnot(None))
        .all()
    }
    best: tuple[float, datetime] | None = None
    for s in slots:
        if s in taken:
            continue
        delta = abs((taken_at_utc - s).total_seconds())
        if delta <= SLOT_MATCH_WINDOW_HOURS * 3600 and (best is None or delta < best[0]):
            best = (delta, s)
    return best[1] if best else None


def adherence(db: Session, user: User, item: MedicationItem, start: date, end: date) -> dict:
    """{"expected": n, "taken": t, "skipped": s, "not_logged": n - t - s,
    "pct_taken": t/n*100 if (t+s) > 0 else None,   # None => unknown, never 0
    "logging_coverage": (t+s)/n}"""
    slots = expected_slots(item, start, end, user.timezone, db)
    n = len(slots)
    events = (
        db.query(DoseEvent)
        .filter(DoseEvent.user_id == user.id, DoseEvent.item_id == item.id)
        .filter(DoseEvent.scheduled_for.isnot(None))
        .all()
    )
    slot_status = {}
    for ev in events:
        # nearest slot within ±4h of the event's scheduled_for
        best = min(slots, key=lambda s: abs((ev.scheduled_for - s).total_seconds()), default=None)
        if best is not None and abs((ev.scheduled_for - best).total_seconds()) <= SLOT_MATCH_WINDOW_HOURS * 3600:
            slot_status[best] = ev.status
    taken = sum(1 for st in slot_status.values() if st == "taken")
    skipped = sum(1 for st in slot_status.values() if st == "skipped")
    logged = taken + skipped
    return {
        "expected": n,
        "taken": taken,
        "skipped": skipped,
        "not_logged": n - logged,
        "pct_taken": (taken / n * 100) if n and logged else None,
        "logging_coverage": (logged / n) if n else 0.0,
    }


def record_change(db: Session, item: MedicationItem, kind: str, effective_date: date, before: dict | None, after: dict | None) -> MedicationItemEvent:
    ev = MedicationItemEvent(
        user_id=item.user_id,
        item_id=item.id,
        kind=kind,
        effective_date=effective_date,
        before_json=before,
        after_json=after,
    )
    db.add(ev)
    db.flush()
    return ev


def decrement_stock(item: MedicationItem, amount: float) -> float | None:
    """If stock_quantity set, decrement; never below 0. Returns new value."""
    if item.stock_quantity is None:
        return None
    item.stock_quantity = max(0.0, float(item.stock_quantity) - float(amount))
    item.stock_updated_at = datetime.now(timezone.utc)
    return item.stock_quantity
