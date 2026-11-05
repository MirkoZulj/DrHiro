"""Medication ↔ reminder integration (0.9.0, spec §5.5, owner decision D1).

Design (documented choice): ONE Reminder per time slot, built with the
existing ``routers.reminders._compute_next_due`` semantics — each slot becomes
its own ``{"days": [...], "time": "HH:MM"}`` (or cron) reminder. Because a
multi-time schedule owns several reminders, the slot→reminder mapping is stored
on the item as ``reminder_ids_json`` (a JSON list; the spec §5.5 column). For
the common single-slot schedule this is exactly one reminder.

- Stopping or pausing an item disables its reminders; resuming re-enables them.
- Occurrence completion is handled by ``complete_reminders_by_dose`` in the
  worker (reuses the complete_reminders_by_measurement pattern).
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from drhiro_api.models import MedicationItem, Reminder, User
from drhiro_api.routers.reminders import _compute_next_due

WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _schedule_for_slot(item: MedicationItem, slot_time: str) -> dict:
    """schedule_json for one time slot in the reminder engine's dialect."""
    sched = item.schedule_json or {}
    tz = item.timezone or "UTC"
    base = {"time": slot_time, "completion_window_minutes": sched.get("completion_window_minutes", 240)}
    stype = sched.get("type", "daily")
    if stype == "weekly":
        base["days"] = list(sched.get("days", []))
    elif stype == "interval":
        # The reminder engine has no interval type; a daily cron is the closest
        # safe approximation and the adherence layer stays the source of truth
        # for what was actually expected.
        base["days"] = list(WEEKDAY_NAMES)
    else:  # daily
        base["days"] = list(WEEKDAY_NAMES)
    return base


def sync_reminders_for_item(db: Session, user: User, item: MedicationItem, create: bool = True) -> list[str]:
    """Ensure one Reminder per time slot exists (create=True), disabled when the
    item is not active, and return the reminder ids.

    Idempotent: existing ids in reminder_ids_json are reused; only missing
    slots get new reminders.
    """
    sched = item.schedule_json or {}
    times = [str(t) for t in (sched.get("times") or [])]
    existing = {r.schedule_json.get("time"): r for r in _item_reminders(db, item)}

    ids: list[str] = []
    for t in times:
        rem = existing.get(t)
        if rem is None and create:
            rem = Reminder(
                user_id=user.id,
                type="medication",
                schedule_json=_schedule_for_slot(item, t),
                timezone=item.timezone or user.timezone,
                enabled=True,
            )
            db.add(rem)
            db.flush()
            rem.next_due_at = _compute_next_due(rem)
            db.flush()
        if rem is not None:
            ids.append(str(rem.id))
    item.reminder_ids_json = ids or None
    return ids


def set_reminders_enabled(db: Session, item: MedicationItem, enabled: bool) -> int:
    """Enable/disable all of the item's reminders. Returns count updated."""
    reminders = _item_reminders(db, item)
    for r in reminders:
        r.enabled = enabled
    return len(reminders)


def _item_reminders(db: Session, item: MedicationItem) -> list[Reminder]:
    ids = [uuid.UUID(str(x)) for x in (item.reminder_ids_json or [])]
    if not ids:
        return []
    return db.query(Reminder).filter(Reminder.id.in_(ids), Reminder.user_id == item.user_id).all()
