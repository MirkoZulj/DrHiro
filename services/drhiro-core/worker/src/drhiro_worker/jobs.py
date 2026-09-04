"""RQ job functions. Each is importable and enqueued by the scheduler or
the API (e.g. vision analysis, aggregate recompute).

The RQ worker entry point runs: rq worker --url <redis> drhiro
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from drhiro_api.config import get_settings
from drhiro_api.db import SessionLocal
from drhiro_api.models import Alert, Measurement, Reminder, ReminderOccurrence, User
from drhiro_api.services.alerts import recompute_alerts_for_user


def recompute_alerts(user_id: str) -> dict:
    """Recompute deterministic rule alerts for a user after ingestion."""
    db = SessionLocal()
    try:
        created = recompute_alerts_for_user(db, uuid.UUID(user_id))
        return {"user_id": user_id, "alerts_created": len(created)}
    finally:
        db.close()


def recompute_all_alerts() -> dict:
    """Recompute alerts for every active user (maintenance pass)."""
    db = SessionLocal()
    try:
        users = db.query(User).filter(User.status == "active").all()
        total = 0
        for u in users:
            created = recompute_alerts_for_user(db, u.id)
            total += len(created)
        return {"users": len(users), "alerts_created": total}
    finally:
        db.close()


def deliver_reminder(occurrence_id: str) -> dict:
    """Deliver a pending reminder occurrence via Telegram Bot API.

    Called by the RQ worker consuming the drhiro:reminder-delivery queue.
    Marks the occurrence as sent on success, failed on error.
    """
    import httpx

    db = SessionLocal()
    try:
        occ = db.query(ReminderOccurrence).filter(ReminderOccurrence.id == uuid.UUID(occurrence_id)).first()
        if not occ:
            return {"error": "occurrence not found"}
        if occ.status == "sent":
            return {"skipped": "already sent"}

        reminder = db.query(Reminder).filter(Reminder.id == occ.reminder_id).first()
        if not reminder or not reminder.enabled:
            occ.status = "cancelled"
            occ.sent_at = datetime.now(timezone.utc)
            db.commit()
            return {"skipped": "reminder disabled or missing"}

        user = db.query(User).filter(User.id == reminder.user_id).first()
        if not user:
            return {"error": "user not found"}

        # Get Telegram ID
        telegram_id = None
        for identity in user.identities:
            if identity.provider == "telegram":
                telegram_id = identity.provider_subject
                break

        if not telegram_id:
            return {"error": "user has no telegram identity"}

        # Build message
        msg = _build_reminder_message(reminder, user)

        # Send via Telegram Bot API
        settings = get_settings()
        if not settings.telegram_bot_token:
            return {"error": "telegram bot token not configured"}

        resp = httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": telegram_id, "text": msg, "parse_mode": "HTML"},
            timeout=30,
        )

        if resp.status_code == 200:
            occ.status = "sent"
            occ.sent_at = datetime.now(timezone.utc)
            db.commit()
            return {"delivered": True}
        else:
            occ.status = "failed"
            occ.sent_at = datetime.now(timezone.utc)
            db.commit()
            return {"error": f"telegram api error {resp.status_code}", "detail": resp.text}

    except Exception as e:
        return {"error": str(e)}
    finally:
        db.close()


def _build_reminder_message(reminder: Reminder, user: User) -> str:
    """Build a human-readable reminder message."""
    type_labels = {
        "bp": "Blood Pressure",
        "weight": "Weight",
        "water": "Water",
        "meal": "Meal",
        "sync": "Sync",
        "activity": "Activity",
        "bedtime": "Bedtime",
        "weekly": "Weekly Check-in",
    }
    label = type_labels.get(reminder.type, reminder.type.replace("_", " ").title())
    return (
        f"🔔 <b>{label} Reminder</b>\n\n"
        f"Hi {user.display_name}, this is your scheduled reminder "
        f"to check in on your {label.lower()}."
    )


def complete_reminders_by_measurement(user_id: str, metric_type: str, measurement_id: str, measured_at: datetime) -> dict:
    """Auto-complete reminder occurrences when a matching record arrives
    inside its completion window. Section 10.2."""
    db = SessionLocal()
    try:
        reminders = (
            db.query(Reminder)
            .filter(Reminder.user_id == uuid.UUID(user_id), Reminder.type == metric_type, Reminder.enabled.is_(True))
            .all()
        )
        completed = 0
        for r in reminders:
            window = r.schedule_json.get("completion_window_minutes", 180)
            occ = (
                db.query(ReminderOccurrence)
                .filter(
                    ReminderOccurrence.reminder_id == r.id,
                    ReminderOccurrence.status.in_(["pending", "sent", "snoozed"]),
                )
                .order_by(ReminderOccurrence.due_at.desc())
                .first()
            )
            if not occ:
                continue
            delta_min = abs((measured_at - occ.due_at).total_seconds()) / 60
            if delta_min <= window:
                occ.status = "completed"
                occ.completed_by_record_id = measurement_id
                completed += 1
        db.commit()
        return {"completed": completed}
    finally:
        db.close()
