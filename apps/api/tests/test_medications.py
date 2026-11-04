"""0.9.0 medications tests (spec §5.9)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from tests.conftest import auth_headers, make_user
from drhiro_api.models import MedicationItem, MedicationItemEvent, Reminder
from drhiro_api.services.medications import (
    adherence,
    expected_slots,
    match_slot,
)


def make_item(db, user, **overrides) -> MedicationItem:
    fields = dict(
        user_id=user.id,
        kind="supplement",
        name="Vitamin D3",
        status="active",
        schedule_json={"type": "daily", "times": ["08:00"]},
        start_date=date(2026, 1, 1),
        timezone="Europe/Zagreb",
    )
    fields.update(overrides)
    item = MedicationItem(**fields)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


# ---------- expected_slots ----------

def test_expected_slots_daily_basic(db, user_a):
    item = make_item(db, user_a)
    slots = expected_slots(item, date(2026, 6, 1), date(2026, 6, 3), "Europe/Zagreb", db)
    assert len(slots) == 3
    # 08:00 Europe/Zagreb in June (CEST) = 06:00 UTC
    assert all(s.utctimetuple()[3:5] == (6, 0) for s in slots)


def test_expected_slots_dst_spring_forward(db, user_a):
    """29 Mar 2026: 02:00 CET -> 03:00 CEST. The 08:00 local slot keeps local
    wall time, so its UTC instant shifts back by one hour across the boundary."""
    item = make_item(db, user_a)
    slots = expected_slots(item, date(2026, 3, 28), date(2026, 3, 30), "Europe/Zagreb", db)
    assert len(slots) == 3
    utc_hours = [s.astimezone(timezone.utc).hour for s in slots]
    # 28 Mar = CET (UTC+1) -> 07:00Z; 29/30 Mar = CEST (UTC+2) -> 06:00Z
    assert utc_hours == [7, 6, 6]


def test_expected_slots_dst_fall_back(db, user_a):
    """25 Oct 2026: 03:00 CEST -> 02:00 CET. 08:00 local -> 07:00Z after the change."""
    item = make_item(db, user_a)
    slots = expected_slots(item, date(2026, 10, 24), date(2026, 10, 26), "Europe/Zagreb", db)
    utc_hours = [s.astimezone(timezone.utc).hour for s in slots]
    # 24 Oct = CEST (UTC+2) -> 06:00Z; 25 Oct (DST ends 03:00) and 26 Oct are
    # CET (UTC+1) -> 07:00Z — the 08:00 local slot keeps wall time.
    assert utc_hours == [6, 7, 7]


def test_expected_slots_interval_schedule(db, user_a):
    item = make_item(db, user_a, schedule_json={"type": "interval", "every_days": 2, "times": ["20:00"], "anchor": "2026-06-01"})
    slots = expected_slots(item, date(2026, 6, 1), date(2026, 6, 7), "Europe/Zagreb", db)
    days = {s.astimezone(timezone.utc).date() for s in slots}
    assert days == {date(2026, 6, 1), date(2026, 6, 3), date(2026, 6, 5), date(2026, 6, 7)}


def test_expected_slots_weekday_schedule(db, user_a):
    item = make_item(db, user_a, schedule_json={"type": "weekly", "days": ["mon"], "times": ["08:00"]})
    slots = expected_slots(item, date(2026, 6, 1), date(2026, 6, 7), "Europe/Zagreb", db)  # Mon..Sun
    assert len(slots) == 1


def test_expected_slots_excludes_paused(db, user_a):
    item = make_item(db, user_a)
    db.add(MedicationItemEvent(
        user_id=user_a.id, item_id=item.id, kind="paused", effective_date=date(2026, 6, 2),
    ))
    db.add(MedicationItemEvent(
        user_id=user_a.id, item_id=item.id, kind="resumed", effective_date=date(2026, 6, 4),
    ))
    db.commit()
    slots = expected_slots(item, date(2026, 6, 1), date(2026, 6, 5), "Europe/Zagreb", db)
    days = {s.astimezone(timezone.utc).date() for s in slots}
    assert days == {date(2026, 6, 1), date(2026, 6, 4), date(2026, 6, 5)}


def test_expected_slots_end_date(db, user_a):
    item = make_item(db, user_a, end_date=date(2026, 6, 2))
    slots = expected_slots(item, date(2026, 6, 1), date(2026, 6, 5), "Europe/Zagreb", db)
    assert len(slots) == 2


def test_expected_slots_prn_empty(db, user_a):
    item = make_item(db, user_a, schedule_json={"type": "prn"})
    assert expected_slots(item, date(2026, 6, 1), date(2026, 6, 5), "Europe/Zagreb", db) == []


# ---------- adherence ----------

def test_adherence_no_logs_is_not_logged_never_zero_pct(db, user_a):
    """D3/spec §5.1: zero logs -> pct_taken None (unknown), not 0%."""
    item = make_item(db, user_a)
    a = adherence(db, user_a, item, date(2026, 6, 1), date(2026, 6, 7))
    assert a["expected"] == 7
    assert a["not_logged"] == 7
    assert a["taken"] == 0 and a["skipped"] == 0
    assert a["pct_taken"] is None


def test_adherence_skipped_counts_as_logged(db, user_a):
    item = make_item(db, user_a)
    from drhiro_api.models import DoseEvent
    slot = expected_slots(item, date(2026, 6, 1), date(2026, 6, 1), "Europe/Zagreb", db)[0]
    db.add(DoseEvent(user_id=user_a.id, item_id=item.id, scheduled_for=slot,
                     taken_at=slot, status="skipped", amount=1.0))
    db.commit()
    a = adherence(db, user_a, item, date(2026, 6, 1), date(2026, 6, 1))
    assert a["skipped"] == 1 and a["pct_taken"] == 0.0


# ---------- slot matching ----------

def test_slot_match_within_4h(db, user_a):
    item = make_item(db, user_a)
    slot = expected_slots(item, date(2026, 6, 1), date(2026, 6, 1), "Europe/Zagreb", db)[0]
    taken = slot + timedelta(hours=3)
    assert match_slot(item, taken, "Europe/Zagreb", db) == slot


def test_slot_match_outside_4h_is_adhoc(db, user_a):
    item = make_item(db, user_a)
    slot = expected_slots(item, date(2026, 6, 1), date(2026, 6, 1), "Europe/Zagreb", db)[0]
    assert match_slot(item, slot + timedelta(hours=5), "Europe/Zagreb", db) is None


def test_second_dose_same_slot_is_adhoc(db, user_a):
    """A second dose in an already-logged slot does not match -> ad-hoc (scheduled_for NULL)."""
    item = make_item(db, user_a)
    slot = expected_slots(item, date(2026, 6, 1), date(2026, 6, 1), "Europe/Zagreb", db)[0]
    m1 = match_slot(item, slot, "Europe/Zagreb", db)
    assert m1 == slot
    # record the dose
    from drhiro_api.models import DoseEvent
    db.add(DoseEvent(user_id=user_a.id, item_id=item.id, scheduled_for=slot,
                     taken_at=slot, status="taken", amount=1.0))
    db.commit()
    assert match_slot(item, slot + timedelta(minutes=30), "Europe/Zagreb", db) is None


def test_unique_slot_index_rejects_duplicate(db, user_a):
    from drhiro_api.models import DoseEvent
    item = make_item(db, user_a)
    slot = expected_slots(item, date(2026, 6, 1), date(2026, 6, 1), "Europe/Zagreb", db)[0]
    db.add(DoseEvent(user_id=user_a.id, item_id=item.id, scheduled_for=slot,
                     taken_at=slot, status="taken", amount=1.0))
    db.commit()
    db.add(DoseEvent(user_id=user_a.id, item_id=item.id, scheduled_for=slot,
                     taken_at=slot, status="taken", amount=1.0))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---------- API: tenant isolation, reminders ----------

def _create_via_api(client, user, **overrides):
    payload = dict(kind="supplement", name="Vitamin D3", dose_amount=1, dose_unit="capsule",
                   schedule_json={"type": "daily", "times": ["08:00"]})
    payload.update(overrides)
    return client.post("/api/v1/medications", json=payload, headers=auth_headers(user))


def test_medication_api_crud_and_tenant_isolation(client, db, user_a, user_b):
    r = _create_via_api(client, user_a)
    assert r.status_code in (200, 201), r.text
    item = r.json()
    item_id = item["id"]

    # B cannot read A's item
    r = client.get(f"/api/v1/medications/{item_id}", headers=auth_headers(user_b))
    assert r.status_code == 404
    # B cannot log a dose on A's item
    r = client.post(f"/api/v1/medications/{item_id}/doses", json={"status": "taken"}, headers=auth_headers(user_b))
    assert r.status_code == 404
    # B cannot stop A's item
    r = client.post(f"/api/v1/medications/{item_id}/stop", json={}, headers=auth_headers(user_b))
    assert r.status_code == 404


def test_stop_disables_reminders_resume_reenables(client, db, user_a):
    r = _create_via_api(client, user_a, create_reminder=True)
    item_id = r.json()["id"]
    rid = r.json().get("reminder_ids_json") or []
    if not rid:  # fall back to DB lookup
        item = db.get(MedicationItem, uuid.UUID(item_id))
        rid = list(item.reminder_ids_json or [])
    assert len(rid) == 1  # D1: one reminder per time slot

    def _enabled():
        return db.query(Reminder).filter(Reminder.id.in_([uuid.UUID(x) for x in rid])).all()

    assert all(x.enabled for x in _enabled())
    db.expire_all()
    client.post(f"/api/v1/medications/{item_id}/pause", json={}, headers=auth_headers(user_a))
    db.expire_all()
    assert all(not x.enabled for x in _enabled())
    client.post(f"/api/v1/medications/{item_id}/resume", json={}, headers=auth_headers(user_a))
    db.expire_all()
    assert all(x.enabled for x in _enabled())


def test_duplicate_ingredients_with_synonyms(client, db, user_a):
    """'Vitamin D3' + 'Cholecalciferol' -> one MED_DUPLICATE_INGREDIENT alert."""
    _create_via_api(client, user_a, name="Vitamin D3",
                    ingredients_json=[{"name": "cholecalciferol", "amount": 25, "unit": "mcg"}])
    _create_via_api(client, user_a, name="D3 drops",
                    ingredients_json=[{"name": "vitamin d3", "amount": 10, "unit": "mcg"}])
    from drhiro_api.models import Alert
    alerts = db.query(Alert).filter(Alert.user_id == user_a.id,
                                    Alert.rule_code == "MED_DUPLICATE_INGREDIENT").all()
    assert len(alerts) == 1
