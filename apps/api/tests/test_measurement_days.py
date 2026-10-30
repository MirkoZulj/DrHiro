"""Measurement-day bucketing tests (0.1.23).

Device MEASUREMENTS (steps, sleep, exercise, ...) belong to the LOCAL CALENDAR
day of their own timestamp — the 04:00 nutrition-day boundary is for food and
water a user logs, NOT for wearable readings. Sleep is keyed to the WAKE-UP day
(Xiaomi/Mi Fitness convention). Meals and water keep the eating-day boundary.

Runs against the DEV database only (see conftest).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from drhiro_api.models import Activity, Measurement, Meal
from drhiro_api.services.day_boundary import (
    local_calendar_day_start,
    nutrition_day_start,
    nutrition_date,
)
from tests.conftest import auth_headers, make_user

ZAGREB = ZoneInfo("Europe/Zagreb")


def _steps(db, user, start_utc, count, rid):
    db.add(Measurement(
        user_id=user.id, metric_type="steps", start_at=start_utc,
        end_at=start_utc + timedelta(minutes=30), value_json={"count": count},
        source_provider="health_connect", source_record_id=rid,
        recording_method="automatic",
    ))


# --------------------------------------------------------------------------- #
# A — steps stamped at LOCAL MIDNIGHT belong to that calendar day
# --------------------------------------------------------------------------- #

def test_today_steps_stamped_at_local_midnight_count_as_today(client, db):
    user = make_user(db, "StepMidnight", telegram_id="5201")
    now_local = datetime.now(ZAGREB)
    midnight_local = datetime(now_local.year, now_local.month, now_local.day, 0, 0, tzinfo=ZAGREB)
    # The Bridge stamps a daily total at LOCAL midnight (00:00 local).
    _steps(db, user, midnight_local.astimezone(timezone.utc), 5650, "steps-midnight-test")
    db.commit()

    r = client.get("/api/v1/dashboard/today", headers=auth_headers(user))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["steps_today"] == 5650
    # The eating day is unchanged and still reported.
    assert data["nutrition_date"] == nutrition_date(datetime.now(timezone.utc), ZAGREB).isoformat()
    assert data["day_start_hour"] == 4


def test_daily_summary_steps_use_calendar_day_meals_use_eating_day(client, db):
    user = make_user(db, "SummarySplit", telegram_id="5202")
    # 4 Oct's own step total: stamped 22:00 UTC 3 Oct == 00:00 local 4 Oct.
    _steps(db, user, datetime(2026, 10, 3, 22, 0, tzinfo=timezone.utc), 18558, "steps-oct04")
    # 5 Oct's own step total: stamped 22:00 UTC 4 Oct == 00:00 local 5 Oct.
    _steps(db, user, datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc), 5650, "steps-oct05")
    # A meal eaten at 00:01 local on 5 Oct belongs to the 4 Oct EATING day.
    db.add(Meal(user_id=user.id, eaten_at=datetime(2026, 10, 4, 22, 1, tzinfo=timezone.utc),
                meal_type="dinner", status="confirmed", totals_json={"kcal": 810.0}))
    db.commit()

    s5 = client.get("/api/v1/summaries/daily/2026-10-05", headers=auth_headers(user)).json()
    s4 = client.get("/api/v1/summaries/daily/2026-10-04", headers=auth_headers(user)).json()
    # Steps follow the calendar day.
    assert s5["steps"] == 5650
    assert s4["steps"] == 18558
    # The 00:01 meal follows the eating day: previous day, not the 5th.
    assert s5["meals"] == 0
    assert s4["meals"] == 1
    assert s4["calories_kcal"] == 810.0


def test_sleep_buckets_by_wake_up_day(client, db):
    user = make_user(db, "WakeDay", telegram_id="5203")
    now_local = datetime.now(ZAGREB)
    wake_d = now_local.date()
    bed_d = wake_d - timedelta(days=1)
    bed = datetime.combine(bed_d, time(23, 30), tzinfo=ZAGREB)
    # Went to bed 23:30 on bed_d, slept 443 min -> woke on wake_d.
    db.add(Measurement(
        user_id=user.id, metric_type="sleep",
        start_at=bed.astimezone(timezone.utc),
        end_at=(bed + timedelta(minutes=443)).astimezone(timezone.utc),
        value_json={"duration_min": 443}, source_provider="health_connect",
        source_record_id="sleep-wake-test", recording_method="automatic",
    ))
    db.commit()

    r = client.get("/api/v1/trends/daily/sleep_duration_min?days=7", headers=auth_headers(user))
    assert r.status_code == 200, r.text
    by = {p["date"]: p["value"] for p in r.json()["points"]}
    assert by[wake_d.isoformat()] == 443.0
    # NOT attributed to the date the wearer went to bed.
    assert by.get(bed_d.isoformat()) is None


# --------------------------------------------------------------------------- #
# B — /activities surfaces device exercise sessions from `measurements`
# --------------------------------------------------------------------------- #

def test_activities_union_device_exercise(client, db):
    user = make_user(db, "ExerciseUnion", telegram_id="5204")
    d = datetime.now(ZAGREB).date()
    db.add(Activity(user_id=user.id, activity_date=d, title="Walk", calories_burned=180.0))
    for i, (hh, mm, dur) in enumerate([(6, 36, 20), (8, 17, 35)]):
        st = datetime(d.year, d.month, d.day, hh, mm, tzinfo=ZAGREB)
        db.add(Measurement(
            user_id=user.id, metric_type="exercise",
            start_at=st.astimezone(timezone.utc),
            end_at=(st + timedelta(minutes=dur)).astimezone(timezone.utc),
            value_json={"exercise_type": "79", "duration_min": dur},
            source_provider="health_connect", source_record_id=f"exercise-test-{i}",
            recording_method="automatic",
        ))
    db.commit()

    r = client.get(f"/api/v1/activities?date={d.isoformat()}", headers=auth_headers(user))
    assert r.status_code == 200, r.text
    data = r.json()
    # 1 user-logged + 2 device sessions, and the response key set is unchanged.
    assert set(data) == {"date", "items", "total_burned"}
    assert len(data["items"]) == 3
    device = [it for it in data["items"] if it.get("source") == "device"]
    assert len(device) == 2
    assert all(it["title"] and it["calories_burned"] == 0.0 for it in device)
    assert data["total_burned"] == 180.0

    r2 = client.get("/api/v1/activities?days=7", headers=auth_headers(user))
    assert r2.status_code == 200, r2.text
    agg = r2.json()
    assert agg["days"]                       # non-empty
    assert agg["days"][d.isoformat()] == 180.0
    assert agg["sessions_by_day"][d.isoformat()] == 3


# --------------------------------------------------------------------------- #
# The eating-day rule must survive for WATER/consumption
# --------------------------------------------------------------------------- #

def test_water_still_uses_eating_day_boundary(client, db):
    user = make_user(db, "WaterBoundary", telegram_id="5205")
    boundary = nutrition_day_start(user)   # 04:00 local today, as UTC
    # Just BEFORE the boundary -> previous eating day, must NOT count as today.
    db.add(Measurement(
        user_id=user.id, metric_type="water", start_at=boundary - timedelta(minutes=1),
        end_at=boundary - timedelta(minutes=1), value_json={"amount_ml": 200},
        source_provider="manual", source_record_id="water-before", recording_method="manual",
    ))
    # After the boundary -> today's eating day.
    db.add(Measurement(
        user_id=user.id, metric_type="water", start_at=boundary + timedelta(minutes=1),
        end_at=boundary + timedelta(minutes=1), value_json={"amount_ml": 300},
        source_provider="manual", source_record_id="water-after", recording_method="manual",
    ))
    db.commit()

    data = client.get("/api/v1/dashboard/today", headers=auth_headers(user)).json()
    assert data["water_ml_today"] == 300
    # And the calendar-day start is genuinely a different instant than the eating day.
    assert local_calendar_day_start(user) < boundary