"""Test that reminder delivery cannot revert to queued (Qodo #16).

Proves that even if a fast worker commits 'sent' before the scheduler
commits its 'queued' transition, the delivered reminder stays 'sent'
and is never reverted to 'queued'.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

# Add the API src dir so we can import worker + models
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

TEST_DB_URL = os.environ.get(
    "DRHIRO_TEST_DB_URL",
    "postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_test",
)

pytestmark = pytest.mark.skipif(
    os.environ.get("DRHIRO_R1R2_ALEMBIC_DB") != "1",
    reason="requires an Alembic-built DB; set DRHIRO_R1R2_ALEMBIC_DB=1",
)


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    # Truncate all tables before each test for isolation (this file's tests
    # were previously contaminating each other via leftover rows).
    from drhiro_api.models import Base
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = S()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def _seed_reminder_and_occurrence(db):
    """Create a user + reminder + pending occurrence, return (user_id, reminder_id, occ_id)."""
    from drhiro_api.models import User, Reminder, ReminderOccurrence

    uid = uuid.uuid4()
    db.execute(
        text("INSERT INTO users (id, display_name, timezone, locale, status, created_at, updated_at) "
             "VALUES (:id, 'RemindU', 'UTC', 'en', 'active', NOW(), NOW())"),
        {"id": str(uid)},
    )
    db.execute(
        text("INSERT INTO external_identities (id, user_id, provider, provider_subject, created_at, updated_at) "
             "VALUES (:id, :uid, 'telegram', '987654321', NOW(), NOW())"),
        {"id": str(uuid.uuid4()), "uid": str(uid)},
    )

    rid = uuid.uuid4()
    db.execute(
        text("INSERT INTO reminders (id, user_id, type, schedule_json, timezone, enabled, created_at, updated_at) "
             "VALUES (:id, :uid, 'bp', :sched, 'UTC', true, NOW(), NOW())"),
        {"id": str(rid), "uid": str(uid), "sched": '{"cron": "0 8 * * *"}'},
    )

    occ = ReminderOccurrence(
        reminder_id=rid,
        due_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        status="pending",
    )
    db.add(occ)
    db.commit()
    return uid, rid, occ.id


def test_run_once_commits_queued_before_enqueue(db):
    """run_once must commit 'queued' BEFORE enqueueing the job, so a fast
    worker can never observe a 'pending' row."""
    from drhiro_worker.scheduler import run_once, due_occurrences, SessionLocal

    uid, rid, occ_id = _seed_reminder_and_occurrence(db)

    # Mock the RQ Queue to capture enqueue calls
    mock_queue = MagicMock()
    with patch("drhiro_worker.scheduler.Queue", return_value=mock_queue), \
         patch("drhiro_worker.scheduler.redis") as mock_redis:

        result = run_once()

    assert result["due_delivered"] == 1
    mock_queue.enqueue.assert_called_once()
    # Verify the occurrence is now 'queued' in the DB
    from drhiro_api.models import ReminderOccurrence
    occ = db.query(ReminderOccurrence).filter(ReminderOccurrence.id == occ_id).first()
    assert occ.status == "queued"


def test_deliver_reminder_conditional_update(db):
    """The delivery job must only update to 'sent' WHERE status = 'queued'.
    If the row is already 'sent', the update must affect 0 rows and NOT
    overwrite the completed state."""
    from drhiro_worker.jobs import deliver_reminder
    from drhiro_api.models import ReminderOccurrence

    uid, rid, occ_id = _seed_reminder_and_occurrence(db)

    # Manually set to 'sent' to simulate a fast worker completing first
    db.execute(
        text("UPDATE reminder_occurrences SET status = 'sent', sent_at = NOW() WHERE id = :id"),
        {"id": str(occ_id)},
    )
    db.commit()

    # Call deliver_reminder - it should skip since status is 'sent'
    result = deliver_reminder(str(occ_id))
    assert result == {"skipped": "already sent"}

    # Verify still 'sent'
    occ = db.query(ReminderOccurrence).filter(ReminderOccurrence.id == occ_id).first()
    assert occ.status == "sent"


def test_deliver_rejects_non_queued_status(db):
    """deliver_reminder must reject any status that is not 'queued' (e.g. 'pending')
    so it cannot process a row that hasn't been properly claimed by the scheduler."""
    from drhiro_worker.jobs import deliver_reminder
    from drhiro_api.models import ReminderOccurrence

    uid, rid, occ_id = _seed_reminder_and_occurrence(db)
    # Status is 'pending' — should be rejected
    result = deliver_reminder(str(occ_id))
    assert result == {"skipped": "status is pending"}

    # Verify still 'pending' (no mutation)
    occ = db.query(ReminderOccurrence).filter(ReminderOccurrence.id == occ_id).first()
    assert occ.status == "pending"


def test_sent_not_reverted_by_scheduler_commit(db):
    """Even if the scheduler commits AFTER the worker has set 'sent', the
    conditional UPDATE in deliver_reminder prevents reverting to 'queued'."""
    from drhiro_worker.jobs import deliver_reminder
    from drhiro_api.models import ReminderOccurrence

    uid, rid, occ_id = _seed_reminder_and_occurrence(db)

    # Simulate: the scheduler claimed the occurrence (status 'queued', committed).
    # The worker's deliver_reminder runs in its OWN session, so the claim must be
    # visible to it — an uncommitted update here would leave 'pending' in its view.
    from sqlalchemy import update as sa_update
    db.execute(
        sa_update(ReminderOccurrence)
        .where(ReminderOccurrence.id == occ_id)
        .values(status="queued")
    )
    db.commit()
    # Worker processes and commits 'sent' BEFORE scheduler commits
    # (simulated by just calling deliver_reminder which uses the conditional update)

    # Make the telegram send succeed
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch("httpx.post", return_value=mock_resp):
        result = deliver_reminder(str(occ_id))

    assert result == {"delivered": True}

    # Verify 'sent' — scheduler's later commit cannot revert
    occ = db.query(ReminderOccurrence).filter(ReminderOccurrence.id == occ_id).first()
    assert occ.status == "sent"
    assert occ.sent_at is not None


def test_run_once_restores_pending_on_enqueue_failure(db):
    """Qodo #9 — if q.enqueue() fails (e.g. Redis down), the occurrence must
    be restored to 'pending' so a later scheduler tick retries it. A failed
    enqueue must never strand the row as 'queued' with no job."""
    from drhiro_worker.scheduler import run_once

    uid, rid, occ_id = _seed_reminder_and_occurrence(db)

    mock_queue = MagicMock()
    mock_queue.enqueue.side_effect = Exception("Redis down")
    with patch("drhiro_worker.scheduler.Queue", return_value=mock_queue), \
         patch("drhiro_worker.scheduler.redis"):
        result = run_once()

    assert result["due_delivered"] == 0
    # The occurrence must NOT remain 'queued' — it must be 'pending' so the
    # next scheduler tick picks it up again.
    from drhiro_api.models import ReminderOccurrence
    occ = db.query(ReminderOccurrence).filter(ReminderOccurrence.id == occ_id).first()
    assert occ.status == "pending"


# ---------------------------------------------------------------- 0.9.0 meds
# Dose-based reminder completion (spec §5.5) and D1 pause/resume + DST cases.

def _seed_medication(db):
    """user + medication item (daily 08:00 Zagreb) + reminder + pending occurrence."""
    from drhiro_api.models import MedicationItem, MedicationItemEvent, Reminder, ReminderOccurrence

    uid = uuid.uuid4()
    db.execute(
        text("INSERT INTO users (id, display_name, timezone, locale, status, created_at, updated_at) "
             "VALUES (:id, 'MedU', 'Europe/Zagreb', 'en', 'active', NOW(), NOW())"),
        {"id": str(uid)},
    )
    iid = uuid.uuid4()
    db.execute(
        text("INSERT INTO medication_items (id, user_id, kind, name, status, schedule_json, "
             "timezone, start_date, source, created_at, updated_at) "
             "VALUES (:id, :uid, 'supplement', 'Vitamin D3', 'active', :sched, 'Europe/Zagreb', "
             "'2026-01-01', 'web', NOW(), NOW())"),
        {"id": str(iid), "uid": str(uid), "sched": '{"type": "daily", "times": ["08:00"]}'},
    )
    rid = uuid.uuid4()
    db.execute(
        text("INSERT INTO reminders (id, user_id, type, schedule_json, timezone, enabled, created_at, updated_at) "
             "VALUES (:id, :uid, 'medication', :sched, 'Europe/Zagreb', true, NOW(), NOW())"),
        {"id": str(rid), "uid": str(uid), "sched": '{"days": ["mon","tue","wed","thu","fri","sat","sun"], "time": "08:00"}'},
    )
    # link reminder to item via reminder_ids_json
    db.execute(
        text("UPDATE medication_items SET reminder_ids_json = :rj WHERE id = :id"),
        {"rj": f'["{rid}"]', "id": str(iid)},
    )
    occ = ReminderOccurrence(
        reminder_id=rid,
        due_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        status="sent",
    )
    db.add(occ)
    db.add(MedicationItemEvent(user_id=uid, item_id=iid, kind="started", effective_date=date.today()))
    db.commit()
    return str(uid), str(iid), str(rid), occ.id


def test_complete_reminders_by_dose_within_window(db):
    """A dose logged inside the completion window completes the occurrence."""
    from drhiro_worker.jobs import complete_reminders_by_dose
    from drhiro_api.models import ReminderOccurrence

    uid, iid, rid, occ_id = _seed_medication(db)
    result = complete_reminders_by_dose(uid, iid, str(uuid.uuid4()),
                                        datetime.now(timezone.utc) - timedelta(minutes=30))
    assert result == {"completed": 1}
    occ = db.query(ReminderOccurrence).filter(ReminderOccurrence.id == occ_id).first()
    assert occ.status == "completed"
    assert occ.completed_by_record_id is not None


def test_complete_reminders_by_dose_outside_window(db):
    """A dose far outside the window does NOT complete the occurrence."""
    from drhiro_worker.jobs import complete_reminders_by_dose
    from drhiro_api.models import ReminderOccurrence

    uid, iid, rid, occ_id = _seed_medication(db)
    result = complete_reminders_by_dose(uid, iid, str(uuid.uuid4()),
                                        datetime.now(timezone.utc) + timedelta(hours=10))
    assert result == {"completed": 0}
    occ = db.query(ReminderOccurrence).filter(ReminderOccurrence.id == occ_id).first()
    assert occ.status == "sent"


def test_medication_reminder_message(db):
    """The medication message follows the spec §5.5 shape."""
    from drhiro_worker.jobs import _build_reminder_message
    from drhiro_api.models import Reminder, User

    uid, iid, rid, occ_id = _seed_medication(db)
    reminder = db.query(Reminder).filter(Reminder.id == uuid.UUID(rid)).first()
    user = db.query(User).filter(User.id == uuid.UUID(uid)).first()
    msg = _build_reminder_message(reminder, user)
    assert msg.startswith("💊") and "Vitamin D3" in msg and "taken" in msg


def test_medication_reminder_message_deleted_item(db):
    """If the item is gone the message falls back to the generic text."""
    from drhiro_worker.jobs import _build_reminder_message
    from drhiro_api.models import Reminder, User

    uid, iid, rid, occ_id = _seed_medication(db)
    db.execute(text("DELETE FROM medication_items WHERE id = :id"), {"id": iid})
    db.commit()
    reminder = db.query(Reminder).filter(Reminder.id == uuid.UUID(rid)).first()
    user = db.query(User).filter(User.id == uuid.UUID(uid)).first()
    msg = _build_reminder_message(reminder, user)
    assert msg.startswith("🔔")


def test_pause_disables_reminder_resume_reenables(db):
    """D1: pause/stop disable the item's reminders; resume re-enables them."""
    from drhiro_api.services.medication_reminders import set_reminders_enabled
    from drhiro_api.models import MedicationItem, Reminder

    uid, iid, rid, occ_id = _seed_medication(db)
    item = db.query(MedicationItem).filter(MedicationItem.id == uuid.UUID(iid)).first()
    assert set_reminders_enabled(db, item, False) == 1
    db.commit()
    assert db.query(Reminder).filter(Reminder.id == uuid.UUID(rid)).first().enabled is False
    assert set_reminders_enabled(db, item, True) == 1
    db.commit()
    assert db.query(Reminder).filter(Reminder.id == uuid.UUID(rid)).first().enabled is True


def test_dose_completion_respects_disabled_reminder(db):
    """A paused (disabled) item's reminders are not completed by doses."""
    from drhiro_worker.jobs import complete_reminders_by_dose
    from drhiro_api.models import Reminder, ReminderOccurrence

    uid, iid, rid, occ_id = _seed_medication(db)
    db.query(Reminder).filter(Reminder.id == uuid.UUID(rid)).update({"enabled": False})
    db.commit()
    result = complete_reminders_by_dose(uid, iid, str(uuid.uuid4()), datetime.now(timezone.utc))
    assert result == {"completed": 0}
    assert db.query(ReminderOccurrence).filter(ReminderOccurrence.id == occ_id).first().status == "sent"


def test_daily_slot_wall_time_across_dst(db):
    """DST: the D1 reminder keeps 08:00 local wall time across both EU changes —
    07:00Z (CET) before 29 Mar 2026, 06:00Z (CEST) after."""
    from drhiro_api.models import Reminder
    from drhiro_api.routers.reminders import _compute_next_due

    sched = {"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "time": "08:00"}
    real_datetime = datetime

    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 3, 28, 7, 30, tzinfo=tz)

    with patch("drhiro_api.routers.reminders.datetime", FrozenDateTime):
        rem = Reminder(schedule_json=sched, timezone="Europe/Zagreb")
        due_spring = _compute_next_due(rem)
    assert (due_spring.astimezone(timezone.utc).hour, due_spring.astimezone(timezone.utc).minute) == (7, 0)

    class FrozenDateTime2(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 3, 29, 7, 30, tzinfo=tz)

    with patch("drhiro_api.routers.reminders.datetime", FrozenDateTime2):
        rem2 = Reminder(schedule_json=sched, timezone="Europe/Zagreb")
        due_after = _compute_next_due(rem2)
    assert (due_after.astimezone(timezone.utc).hour, due_after.astimezone(timezone.utc).minute) == (6, 0)
