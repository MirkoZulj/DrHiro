"""Scheduler loop: generates reminder occurrences and enqueues delivery.

Runs as a long-lived process (scheduler service). On each tick:
1. For every enabled reminder whose next_due_at has passed, create a
   pending occurrence and advance next_due_at.
2. Enqueue due occurrences for delivery on the 'drhiro' queue.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import redis
from rq import Queue

from drhiro_api.config import get_settings
from drhiro_api.db import SessionLocal
from drhiro_api.models import Reminder, ReminderOccurrence
from drhiro_api.routers.reminders import _compute_next_due

logger = logging.getLogger(__name__)

TICK_SECONDS = 30

# Periodic daily jobs (spec §2.4): (name, local HH:MM in system tz, callable
# returning a report dict). Fire-once bookkeeping is a Redis key
# drhiro:periodic:<name>:<YYYY-MM-DD> SET NX EX 172800 — safe if the scheduler
# restarts. Analytics is NOT scheduled (runs on demand only).
PERIODIC = [
    ("files_retention", "03:30", "drhiro_worker.files.expire_files"),
    ("backup_staleness", "09:00", "drhiro_worker.jobs_backup.check_backup_staleness"),
]


def run_periodic(db) -> dict:
    import importlib

    from zoneinfo import ZoneInfo

    settings = get_settings()
    tz = ZoneInfo(os.environ.get("DRHIRO_SYSTEM_TZ", "Europe/Zagreb"))
    now_local = datetime.now(tz)
    today = now_local.strftime("%Y-%m-%d")
    r = redis.Redis.from_url(settings.redis_url)
    out = {}
    for name, hhmm, func_ref in PERIODIC:
        if now_local.strftime("%H:%M") < hhmm:
            continue
        key = f"drhiro:periodic:{name}:{today}"
        if not r.set(key, "1", nx=True, ex=172800):
            continue
        try:
            mod_name, fn_name = func_ref.rsplit(".", 1)
            fn = getattr(importlib.import_module(mod_name), fn_name)
            out[name] = fn()
        except Exception as e:  # a failed periodic job must not kill the tick
            logger.error("periodic %s failed: %s", name, e)
            out[name] = {"error": str(e)}
    return out


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
        periodic = run_periodic(db)
        due = due_occurrences(db)
        settings = get_settings()
        r = redis.Redis.from_url(settings.redis_url)
        q = Queue("drhiro", connection=r)
        delivered = 0
        for occ in due:
            # Mark as 'queued' and COMMIT BEFORE enqueueing. This ensures:
            #   1. The worker never observes the row in 'pending' status.
            #   2. If the worker processes and commits 'sent' first, the
            #      scheduler's earlier commit has already moved the row past
            #      'pending' and the delivery job's conditional UPDATE (WHERE
            #      status = 'queued') cannot overwrite the completed 'sent'.
            occ.status = "queued"
            db.commit()
            try:
                q.enqueue("drhiro_worker.jobs.deliver_reminder", str(occ.id))
                delivered += 1
            except Exception as e:
                # Enqueue failed (Redis down, queue unavailable). Restore the
                # occurrence to 'pending' so the next scheduler tick retries
                # instead of stranding the row forever as 'queued' with no job.
                logger.error(
                    "enqueue failed for occurrence %s: %s; restoring to pending",
                    occ.id,
                    e,
                )
                occ.status = "pending"
                db.commit()
        return {"occurrences_created": created, "due_delivered": delivered, "periodic": periodic}
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
