"""Scheduler loop: generates reminder occurrences and enqueues delivery.

Runs as a long-lived process (scheduler service). On each tick:
1. For every enabled reminder whose next_due_at has passed, create a
   pending occurrence and advance next_due_at.
2. Enqueue due occurrences for delivery on the 'drhiro' queue.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import redis
from rq import Queue

from drhiro_api.config import get_settings
from drhiro_api.db import SessionLocal
from drhiro_api.models import Reminder, ReminderOccurrence
from drhiro_api.routers.reminders import _compute_next_due

TICK_SECONDS = 30


def generate_occurrences(db) -> int:
    now = datetime.now(timezone.utc)
    reminders = db.query(Reminder).filter(Reminder.enabled.is_(True)).all()
    created = 0
    for r in reminders:
        if r.next_due_at and r.next_due_at <= now:
            db.add(
                ReminderOccurrence(
                    reminder_id=r.id,
                    due_at=r.next_due_at,
                    status="pending",
                )
            )
            r.next_due_at = _compute_next_due(r)
            created += 1
    db.commit()
    return created


def due_occurrences(db) -> list[ReminderOccurrence]:
    now = datetime.now(timezone.utc)
    return (
        db.query(ReminderOccurrence)
        .filter(ReminderOccurrence.status == "pending", ReminderOccurrence.due_at <= now)
        .limit(100)
        .all()
    )


def run_once() -> dict:
    db = SessionLocal()
    try:
        created = generate_occurrences(db)
        due = due_occurrences(db)
        settings = get_settings()
        r = redis.Redis.from_url(settings.redis_url)
        q = Queue("drhiro", connection=r)
        delivered = 0
        for occ in due:
            # Enqueue delivery job on the drhiro queue
            q.enqueue("drhiro_worker.jobs.deliver_reminder", str(occ.id))
            occ.status = "sent"
            occ.sent_at = datetime.now(timezone.utc)
            delivered += 1
        db.commit()
        return {"occurrences_created": created, "due_delivered": delivered}
    finally:
        db.close()


def main():
    while True:
        try:
            result = run_once()
            if result["occurrences_created"] or result["due_delivered"]:
                print(f"[scheduler] {result}", flush=True)
        except Exception as e:
            print(f"[scheduler] error: {e}", flush=True)
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
