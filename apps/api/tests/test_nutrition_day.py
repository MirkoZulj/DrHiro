"""Nutrition-day boundary regression tests.

A nutrition day begins with the first meal of the day, not at local midnight:
a meal eaten at 00:01 local belongs to the PREVIOUS day. The boundary is
configurable via DRHIRO_DAY_START_HOUR (default 4); 0 restores the old
calendar-day behaviour.

Runs against the DEV database only (see conftest).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from drhiro_api.models import Meal
from drhiro_api.routers.dashboard import _nutrition_trend
from drhiro_api.services.day_boundary import (
    day_start_hour,
    local_date_window,
    nutrition_date,
)
from drhiro_api.services.log_intents import _local_date
from tests.conftest import auth_headers, make_user

ZAGREB = ZoneInfo("Europe/Zagreb")


def test_day_start_hour_default_is_four(monkeypatch):
    monkeypatch.delenv("DRHIRO_DAY_START_HOUR", raising=False)
    assert day_start_hour() == 4


def test_day_start_hour_env_override_and_fallback(monkeypatch):
    monkeypatch.setenv("DRHIRO_DAY_START_HOUR", "0")
    assert day_start_hour() == 0
    monkeypatch.setenv("DRHIRO_DAY_START_HOUR", "6")
    assert day_start_hour() == 6
    # Garbage / out-of-range falls back to the default, never raises.
    monkeypatch.setenv("DRHIRO_DAY_START_HOUR", "not-an-hour")
    assert day_start_hour() == 4
    monkeypatch.setenv("DRHIRO_DAY_START_HOUR", "99")
    assert day_start_hour() == 4


def test_nutrition_date_shifts_before_the_boundary(monkeypatch):
    monkeypatch.setenv("DRHIRO_DAY_START_HOUR", "4")
    # 00:01 and 00:11 local on the 5th belong to the 4th.
    assert nutrition_date(datetime(2026, 10, 5, 0, 1, tzinfo=ZAGREB), ZAGREB).isoformat() == "2026-10-04"
    assert nutrition_date(datetime(2026, 10, 5, 0, 11, tzinfo=ZAGREB), ZAGREB).isoformat() == "2026-10-04"
    # 03:59 still the 4th; 04:00 is the 5th.
    assert nutrition_date(datetime(2026, 10, 5, 3, 59, tzinfo=ZAGREB), ZAGREB).isoformat() == "2026-10-04"
    assert nutrition_date(datetime(2026, 10, 5, 4, 0, tzinfo=ZAGREB), ZAGREB).isoformat() == "2026-10-05"
    # Midday is unaffected.
    assert nutrition_date(datetime(2026, 10, 5, 12, 0, tzinfo=ZAGREB), ZAGREB).isoformat() == "2026-10-05"


def test_nutrition_date_zero_hour_is_calendar_day(monkeypatch):
    monkeypatch.setenv("DRHIRO_DAY_START_HOUR", "0")
    # With the boundary at midnight the old calendar-day rule returns.
    assert nutrition_date(datetime(2026, 10, 5, 0, 1, tzinfo=ZAGREB), ZAGREB).isoformat() == "2026-10-05"
    assert nutrition_date(datetime(2026, 10, 5, 0, 11, tzinfo=ZAGREB), ZAGREB).isoformat() == "2026-10-05"


def test_nutrition_date_naive_is_utc(monkeypatch):
    monkeypatch.setenv("DRHIRO_DAY_START_HOUR", "4")
    # 2026-10-05 01:00 UTC = 03:00 local -> previous day.
    assert nutrition_date(datetime(2026, 10, 5, 1, 0), ZAGREB).isoformat() == "2026-10-04"


def test_local_date_window_uses_boundary(monkeypatch):
    monkeypatch.setenv("DRHIRO_DAY_START_HOUR", "4")
    start, end = local_date_window("2026-10-04", ZAGREB)
    # 2026-10-04 04:00 local (UTC+2) -> 02:00 UTC
    assert start.isoformat() == "2026-10-04T02:00:00+00:00"
    assert end.isoformat() == "2026-10-05T02:00:00+00:00"
    monkeypatch.setenv("DRHIRO_DAY_START_HOUR", "0")
    start0, end0 = local_date_window("2026-10-04", ZAGREB)
    assert start0.isoformat() == "2026-10-03T22:00:00+00:00"
    assert end0.isoformat() == "2026-10-04T22:00:00+00:00"


def test_activity_local_date_uses_boundary(monkeypatch):
    monkeypatch.setenv("DRHIRO_DAY_START_HOUR", "4")

    class U:
        timezone = "Europe/Zagreb"

    # 00:30 local on the 5th -> activity_date 2026-10-04.
    assert _local_date(U(), datetime(2026, 10, 5, 0, 30, tzinfo=ZAGREB)).isoformat() == "2026-10-04"
    # 12:00 local on the 5th -> 2026-10-05.
    assert _local_date(U(), datetime(2026, 10, 5, 12, 0, tzinfo=ZAGREB)).isoformat() == "2026-10-05"


def test_nutrition_trend_buckets_post_midnight_meal_on_previous_day(db, monkeypatch):
    monkeypatch.setenv("DRHIRO_DAY_START_HOUR", "4")
    user = make_user(db, "BoundaryTrend", telegram_id="3101")
    now = datetime.now(timezone.utc)
    recent = now - timedelta(hours=2)
    older = now - timedelta(hours=26)
    db.add(Meal(user_id=user.id, eaten_at=recent, meal_type="lunch", status="confirmed",
                totals_json={"kcal": 810.0}))
    db.add(Meal(user_id=user.id, eaten_at=older, meal_type="breakfast", status="confirmed",
                totals_json={"kcal": 300.0}))
    db.commit()

    data = _nutrition_trend(db, user.id, 7, ZAGREB)
    by = {p["date"]: p for p in data["points"]}
    d_recent = nutrition_date(recent, ZAGREB).isoformat()
    d_older = nutrition_date(older, ZAGREB).isoformat()
    assert d_recent != d_older
    assert by[d_recent]["kcal"] == 810.0
    assert by[d_older]["kcal"] == 300.0


def test_today_tile_reports_nutrition_date_and_hour(client, db, monkeypatch):
    monkeypatch.setenv("DRHIRO_DAY_START_HOUR", "4")
    user = make_user(db, "BoundaryTile", telegram_id="3102")
    now = datetime.now(timezone.utc)
    recent = now - timedelta(hours=2)
    db.add(Meal(user_id=user.id, eaten_at=recent, meal_type="lunch", status="confirmed",
                totals_json={"kcal": 640.0}))
    db.commit()

    r = client.get("/api/v1/dashboard/today", headers=auth_headers(user))
    assert r.status_code == 200
    data = r.json()
    assert data["day_start_hour"] == 4
    assert data["nutrition_date"] == nutrition_date(now, ZAGREB).isoformat()
    d_recent = nutrition_date(recent, ZAGREB).isoformat()
    expected = 640.0 if d_recent == data["nutrition_date"] else 0.0
    assert data["calories_kcal_today"] == expected
    # Existing keys are preserved.
    assert "meals_logged_today" in data
    assert "calories_kcal_today" in data


def test_today_tile_hour_zero_uses_calendar_day(client, db, monkeypatch):
    monkeypatch.setenv("DRHIRO_DAY_START_HOUR", "0")
    user = make_user(db, "BoundaryTile0", telegram_id="3103")
    now = datetime.now(timezone.utc)
    # A meal 2h ago is on the local calendar day only if now is >= 02:00 local.
    recent = now - timedelta(hours=2)
    db.add(Meal(user_id=user.id, eaten_at=recent, meal_type="lunch", status="confirmed",
                totals_json={"kcal": 111.0}))
    db.commit()
    r = client.get("/api/v1/dashboard/today", headers=auth_headers(user))
    assert r.status_code == 200
    data = r.json()
    assert data["day_start_hour"] == 0
    assert data["nutrition_date"] == now.astimezone(ZAGREB).date().isoformat()
