"""Prod-shaped regression: profile timezone != device timezone (0.12.0-b).

Production owner profile was America/Chicago while the Health Connect bridge
stamped every row with source_timezone=Europe/Zagreb. The old bucketing
(start_at >= profile-tz midnight; wake_date in profile tz; 10h-gap night
grouping) made steps_today read 0, shifted every daily steps/sleep point one
day early, and merged a nap + two consecutive nights into one 865-min
"last sleep".

These tests reproduce that exact shape: profile America/Chicago, daily steps
totals anchored at Europe/Zagreb midnight, Xiaomi sleep records in Zagreb.
No production data is touched — fixtures only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from drhiro_api.models import Measurement, User
from drhiro_api.services.series import daily_series
from tests.conftest import auth_headers

ZAGREB = ZoneInfo("Europe/Zagreb")
CHICAGO = ZoneInfo("America/Chicago")


def _user(db, name, telegram_id):
    user = User(display_name=name, timezone="America/Chicago")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _steps_daily(db, user, zagreb_day, count, rid):
    """Bridge daily total: [Zagreb midnight, next Zagreb midnight)."""
    start = datetime(zagreb_day.year, zagreb_day.month, zagreb_day.day, 0, 0, tzinfo=ZAGREB)
    db.add(Measurement(
        user_id=user.id, metric_type="steps",
        start_at=start.astimezone(timezone.utc),
        end_at=(start + timedelta(days=1)).astimezone(timezone.utc),
        value_json={"count": count},
        source_provider="health_connect", source_record_id=rid,
        recording_method="automatic",
        metadata_json={"source_timezone": "Europe/Zagreb",
                       "device": {"manufacturer": "Health Connect", "model": "aggregated-daily"}},
    ))


def _sleep(db, user, start_zagreb, duration_min, rid):
    start = start_zagreb.astimezone(timezone.utc)
    db.add(Measurement(
        user_id=user.id, metric_type="sleep",
        start_at=start, end_at=start + timedelta(minutes=duration_min),
        value_json={"duration_min": duration_min},
        source_provider="health_connect", source_record_id=rid,
        recording_method="automatic",
        metadata_json={"source_timezone": "Europe/Zagreb",
                       "device": {"manufacturer": "xiaomi", "model": ""}},
    ))


def test_steps_today_and_sleep_last_with_device_in_other_timezone(client, db):
    user = _user(db, "TravelTz", "5301")
    today_zag = datetime.now(ZAGREB).date()
    yesterday_zag = today_zag - timedelta(days=1)

    # Prod shape: yesterday 4415, today (partial, still syncing) 6753.
    _steps_daily(db, user, yesterday_zag, 4415, "steps-daily-yday")
    _steps_daily(db, user, today_zag, 6753, "steps-daily-today")

    # Last night: bed 01:27 Zagreb today, slept 376 min (wake 07:43 Zagreb).
    _sleep(db, user, datetime(today_zag.year, today_zag.month, today_zag.day, 1, 27, tzinfo=ZAGREB),
           376, "sleep-last-night")
    # Previous night 384 min + a 105-min midday nap on yesterday (Zagreb).
    _sleep(db, user, datetime(yesterday_zag.year, yesterday_zag.month, yesterday_zag.day, 1, 36, tzinfo=ZAGREB),
           384, "sleep-prev-night")
    _sleep(db, user, datetime(yesterday_zag.year, yesterday_zag.month, yesterday_zag.day, 14, 35, tzinfo=ZAGREB),
           105, "sleep-nap")
    db.commit()

    r = client.get("/api/v1/dashboard/today", headers=auth_headers(user))
    assert r.status_code == 200, r.text
    data = r.json()
    # Today's daily total counts TODAY even though it started 17:00 Chicago
    # yesterday.
    assert data["steps_today"] == 6753, data
    # Last sleep is last night only — not night+nap+night glued together.
    assert data["last_sleep"]["duration_min"] == 376, data["last_sleep"]
    assert data["last_sleep"]["days_old"] == 0, data["last_sleep"]
    assert data["last_sleep"]["is_stale"] is False

    # Daily steps/sleep series attribute to the device (Zagreb) day.
    steps = {dv.day: dv.value for dv in daily_series(
        db, user, "steps", yesterday_zag, today_zag)}
    assert steps[today_zag] == 6753, steps
    assert steps[yesterday_zag] == 4415, steps

    sleep = {dv.day: dv.value for dv in daily_series(
        db, user, "sleep_duration_min", yesterday_zag, today_zag)}
    assert sleep[today_zag] == 376, sleep
    # Yesterday's wake day holds night (384) + nap (105), deduplicated.
    assert sleep[yesterday_zag] == 489, sleep

    # Trends endpoints agree.
    r = client.get("/api/v1/trends/daily/steps?days=2", headers=auth_headers(user))
    by = {p["date"]: p["value"] for p in r.json()["points"]}
    assert by[today_zag.isoformat()] == 6753, by
    assert by[yesterday_zag.isoformat()] == 4415, by

    r = client.get("/api/v1/trends/daily/sleep_duration_min?days=2", headers=auth_headers(user))
    by = {p["date"]: p["value"] for p in r.json()["points"]}
    assert by[today_zag.isoformat()] == 376, by
    assert by[yesterday_zag.isoformat()] == 489, by

    # Daily summary for the device day returns its steps.
    s = client.get(f"/api/v1/summaries/daily/{today_zag.isoformat()}",
                   headers=auth_headers(user)).json()
    assert s["steps"] == 6753, s


def test_sleep_wake_day_uses_device_timezone_not_profile(client, db):
    """A 06:55 Zagreb wake-up is still the previous evening in Chicago; the
    night must key to the Zagreb wake day, not the Chicago evening."""
    user = _user(db, "WakeTz", "5302")
    today_zag = datetime.now(ZAGREB).date()
    # Bed 23:27 Zagreb yesterday, 388 min -> wake 05:55 UTC / 06:55 Zagreb today
    # (= 23:55 Chicago yesterday).
    _sleep(db, user, datetime(today_zag.year, today_zag.month, today_zag.day, 0, 27, tzinfo=ZAGREB)
           - timedelta(hours=1), 388, "sleep-wake-tz")
    db.commit()

    sleep = {dv.day: dv.value for dv in daily_series(
        db, user, "sleep_duration_min", today_zag - timedelta(days=1), today_zag)}
    assert sleep[today_zag] == 388, sleep
    assert sleep[today_zag - timedelta(days=1)] is None, sleep
