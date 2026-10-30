"""Eaten-at stamping regression tests (0.1.29).

Covers the write-path stamp bugs:

1. `create_meal_from_text`'s fallback stamp must be LOCAL now, not UTC now:
   between 00:00 and ~02:00 local (UTC+2) the UTC clock is still on the
   previous calendar day, so a UTC-now fallback books the meal on the wrong
   nutrition day.
2. `update_meal` with a BARE `YYYY-MM-DD` must resolve at the current local
   time-of-day (clamped to the past), never at midnight UTC — midnight local
   falls BEFORE the 04:00 day boundary and moves the meal to the previous
   nutrition day.

Runs against the DEV database only (see conftest).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from drhiro_api.models import Meal
from drhiro_api.services.day_boundary import day_start_hour
from drhiro_api.security import create_access_token, create_service_token
from tests.conftest import make_user

ZAGREB = ZoneInfo("Europe/Zagreb")


def tool_headers(telegram_id: str) -> dict:
    return {"X-Service-Token": create_service_token(), "X-Telegram-Id": str(telegram_id)}


def _bearer(user) -> str:
    return f"Bearer {create_access_token(user.id)}"


def _seed_confirmed_meal(client, telegram_id: str) -> str:
    r = client.post(
        "/api/v1/tools/create_meal_from_text",
        json={"telegram_chat_id": "pgchat-7001", "telegram_message_id": "pg-eate-0","text": "rice 200 g", "meal_type": "lunch"},
        headers=tool_headers(telegram_id),
    )
    assert r.status_code == 200, r.text
    meal_id = r.json()["data"]["meal_id"]
    item_id = r.json()["data"]["items"][0]["item_id"]
    # Pin exact nutrition (what the agent does), then confirm the draft so its
    # status is 'confirmed' (the write path of FIX 3).
    r2 = client.post(
        "/api/v1/tools/update_meal_item",
        json={"meal_id": meal_id, "item_id": item_id,
              "patch": {"display_name": "Rice, white, long grain, cooked",
                        "grams": 200, "kcal_per_100g": 130}},
        headers=tool_headers(telegram_id),
    )
    assert r2.status_code == 200, r2.text
    c = client.post(
        "/api/v1/tools/confirm_meal", json={"meal_id": meal_id}, headers=tool_headers(telegram_id)
    )
    assert c.status_code == 200, c.text
    return meal_id


# --------------------------------------------------------------------------- #
# FIX 2 — fallback stamp is local now, not UTC now
# --------------------------------------------------------------------------- #

def test_fallback_stamp_uses_local_now_before_0300(client, db, monkeypatch):
    """A meal logged at 00:30 local (UTC+2) must be stamped on the LOCAL date,
    not the previous UTC date. UTC-now fallback would stamp 22:30 the day
    before, putting the meal on the previous nutrition day."""
    user = make_user(db, "LateNight", telegram_id="4401")
    user.timezone = "Europe/Zagreb"
    db.commit()

    import drhiro_api.routers.openclaw_tools as ot
    # Local 00:30 on 2026-10-12 -> 22:30 UTC on 2026-10-11.
    frozen = datetime(2026, 10, 12, 0, 30, tzinfo=ZAGREB)
    monkeypatch.setattr(ot, "local_now", lambda tz=None: frozen)

    r = client.post(
        "/api/v1/tools/create_meal_from_text",
        json={"telegram_chat_id": "pgchat-7001", "telegram_message_id": "pg-eate-1","text": "toast with cheese", "meal_type": "snack"},
        headers=tool_headers("4401"),
    )
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    # The stamp must be the local instant, so its local date is 2026-10-12.
    assert d["local_date"] == "2026-10-12"
    assert d["local_time"] == "00:30"
    # 00:30 local is before the 04:00 boundary -> previous nutrition day.
    assert d["nutrition_date"] == "2026-10-11"


def test_fallback_stamp_matches_frozen_local_afternoon(client, db, monkeypatch):
    """Sanity: with local_now frozen at 14:05 local, the stamp matches it."""
    user = make_user(db, "MidDay", telegram_id="4402")
    user.timezone = "Europe/Zagreb"
    db.commit()

    import drhiro_api.routers.openclaw_tools as ot
    frozen = datetime(2026, 10, 12, 14, 5, tzinfo=ZAGREB)
    monkeypatch.setattr(ot, "local_now", lambda tz=None: frozen)

    r = client.post(
        "/api/v1/tools/create_meal_from_text",
        json={"telegram_chat_id": "pgchat-7001", "telegram_message_id": "pg-eate-2","text": "chicken salad", "meal_type": "lunch"},
        headers=tool_headers("4402"),
    )
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["local_date"] == "2026-10-12"
    assert d["local_time"] == "14:05"
    assert d["nutrition_date"] == "2026-10-12"


# --------------------------------------------------------------------------- #
# FIX 1 — bare-date correction resolves at current local time-of-day
# --------------------------------------------------------------------------- #

def test_bare_date_correction_lands_on_intended_nutrition_day(client, db, monkeypatch):
    """A bare `YYYY-MM-DD` update must land on that date's nutrition day,
    not the previous one. The old midnight-UTC resolution booked the meal at
    ~02:00 local — before the 04:00 boundary — on the PREVIOUS day.

    Time is frozen at 18:00 local so the resolved time-of-day is after the
    boundary regardless of when the suite runs.
    """
    user = make_user(db, "DayMover", telegram_id="4403")
    user.timezone = "Europe/Zagreb"
    db.commit()

    import drhiro_api.routers.openclaw_tools as ot
    frozen = datetime(2026, 10, 12, 18, 0, tzinfo=ZAGREB)
    monkeypatch.setattr(ot, "local_now", lambda tz=None: frozen)

    meal_id = _seed_confirmed_meal(client, "4403")
    target = (frozen.date() - timedelta(days=2)).isoformat()

    r = client.post(
        "/api/v1/tools/update_meal",
        json={"meal_id": meal_id, "eaten_at": target},
        headers=tool_headers("4403"),
    )
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    # The whole point: the nutrition date must EQUAL the requested date.
    assert d["nutrition_date"] == target
    assert d["local_date"] == target
    # The stamp sits after the 04:00 boundary (frozen time-of-day).
    assert day_start_hour() == 4

    db.expire_all()
    meal = db.get(Meal, uuid.UUID(meal_id))
    local = meal.eaten_at.astimezone(ZAGREB)
    assert local.date().isoformat() == target
    assert local.hour == 18  # the frozen current time-of-day, not midnight


def test_bare_date_correction_before_boundary_clamps_to_previous_day(client, db, monkeypatch):
    """With local_now frozen at 01:00 local (before the 04:00 boundary), a
    bare-date move to TODAY must land on the PREVIOUS nutrition day — the
    current-time resolution is correct and the boundary rule is intact."""
    user = make_user(db, "EarlyBird", telegram_id="4404")
    user.timezone = "Europe/Zagreb"
    db.commit()

    import drhiro_api.routers.openclaw_tools as ot
    frozen = datetime(2026, 10, 12, 1, 0, tzinfo=ZAGREB)
    monkeypatch.setattr(ot, "local_now", lambda tz=None: frozen)

    meal_id = _seed_confirmed_meal(client, "4404")
    today = frozen.date().isoformat()

    r = client.post(
        "/api/v1/tools/update_meal",
        json={"meal_id": meal_id, "eaten_at": today},
        headers=tool_headers("4404"),
    )
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["local_date"] == today
    assert d["local_time"] == "01:00"
    # 01:00 local < 04:00 boundary -> previous nutrition day, BY DESIGN.
    assert d["nutrition_date"] == (frozen.date() - timedelta(days=1)).isoformat()


def test_full_iso_datetime_correction_kept_verbatim(client, db):
    """A full ISO datetime with an explicit time keeps its exact value — the
    bare-date rule must not touch it."""
    user = make_user(db, "TimeMover", telegram_id="4405")
    user.timezone = "Europe/Zagreb"
    db.commit()

    meal_id = _seed_confirmed_meal(client, "4405")
    # 20:00 local on a date two days back — after the boundary, same-day.
    target = datetime.now(ZAGREB) - timedelta(days=2)
    iso = target.replace(hour=20, minute=0, second=0, microsecond=0).isoformat()

    r = client.post(
        "/api/v1/tools/update_meal",
        json={"meal_id": meal_id, "eaten_at": iso},
        headers=tool_headers("4405"),
    )
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["local_time"] == "20:00"
    assert d["local_date"] == target.date().isoformat()

    db.expire_all()
    meal = db.get(Meal, uuid.UUID(meal_id))
    local = meal.eaten_at.astimezone(ZAGREB)
    assert local.hour == 20 and local.minute == 0


def test_update_meal_still_rejects_unparsable_and_far(client, db, monkeypatch):
    """The 7-day guard and the 400-on-garbage behaviour must survive."""
    user = make_user(db, "Guard", telegram_id="4406")
    user.timezone = "Europe/Zagreb"
    db.commit()

    meal_id = _seed_confirmed_meal(client, "4406")

    bad_text = client.post(
        "/api/v1/tools/update_meal", json={"meal_id": meal_id, "eaten_at": "not-a-date"},
        headers=tool_headers("4406"),
    )
    assert bad_text.status_code == 400
    assert "eaten_at must be" in bad_text.text

    # The 7-day guard compares against datetime.now(utc), so a full ISO
    # datetime 30 days ahead is the deterministic way to trip it (a bare
    # date is clamped to local-now and can never be far in the future).
    far_iso = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    bad_far = client.post(
        "/api/v1/tools/update_meal", json={"meal_id": meal_id, "eaten_at": far_iso},
        headers=tool_headers("4406"),
    )
    assert bad_far.status_code == 400
    assert "7 days" in bad_far.text


def test_far_bare_date_still_rejected_by_guard(client, db):
    """A BARE date 30 days ahead must be rejected by the 7-day guard, not
    silently clamped to now (which would swallow the guard). Regression: the
    first cut of FIX 1 clamped every future bare date to the current instant,
    letting a 30-day jump through."""
    user = make_user(db, "BareGuard", telegram_id="4408")
    user.timezone = "Europe/Zagreb"
    db.commit()

    meal_id = _seed_confirmed_meal(client, "4408")
    far_bare = (datetime.now(ZAGREB).date() + timedelta(days=30)).isoformat()

    bad = client.post(
        "/api/v1/tools/update_meal", json={"meal_id": meal_id, "eaten_at": far_bare},
        headers=tool_headers("4408"),
    )
    assert bad.status_code == 400
    assert "7 days" in bad.text


# --------------------------------------------------------------------------- #
# FIX 3 — today tile counts CONFIRMED meals only
# --------------------------------------------------------------------------- #

def test_today_tile_excludes_needs_review_drafts(client, db, monkeypatch):
    """A needs_review draft (e.g. a failed 0-kcal parse) inside today's window
    must NOT count toward meals_logged_today or calories_kcal_today."""
    user = make_user(db, "DraftProbe", telegram_id="4407")
    user.timezone = "Europe/Zagreb"
    db.commit()

    # One confirmed meal in today's window.
    _seed_confirmed_meal(client, "4407")

    # A raw needs_review draft inserted directly (the failed-parse path the
    # old `status != 'deleted'` filter let through).
    now_utc = datetime.now(timezone.utc)
    db.add(Meal(
        user_id=user.id, eaten_at=now_utc, meal_type="dinner",
        status="needs_review", totals_json={"kcal": 0.0},
    ))
    db.commit()

    r = client.get("/api/v1/dashboard/today", headers={"Authorization": _bearer(user)})
    assert r.status_code == 200, r.text
    d = r.json()
    # Only the confirmed meal counts.
    assert d["meals_logged_today"] == 1
    # The draft's 0 kcal must not appear; confirmed kcal is the rice value.
    assert d["calories_kcal_today"] == 260.0
