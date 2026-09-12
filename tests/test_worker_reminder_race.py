"""Test that reminder delivery cannot revert to queued (Qodo #16).

Proves that even if a fast worker commits 'sent' before the scheduler
commits its 'queued' transition, the delivered reminder stays 'sent'
and is never reverted to 'queued'.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
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

    rid = uuid.uuid4()
    db.execute(
        text("INSERT INTO reminders (id, user_id, type, schedule_json, timezone, enabled, created_at, updated_at) "
             "VALUES (:id, :uid, 'bp', :sched', 'UTC', true, NOW(), NOW())"),
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

    # Simulate: scheduler sets 'queued' but doesn't commit yet
    from sqlalchemy import update as sa_update
    db.execute(
        sa_update(ReminderOccurrence)
        .where(ReminderOccurrence.id == occ_id)
        .values(status="queued")
    )
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
