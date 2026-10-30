"""Device-clock timezone + meal-feedback-loop tests (0.1.22).

Covers requirement A (the user's timezone is a settable, device-driven setting
exposed in the today-summary payload) and requirement B (enriched log/confirm
payloads plus an update_meal correction path). Runs against the DEV database
only (see conftest).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from drhiro_api.models import Meal, User
from drhiro_api.security import create_service_token
from drhiro_api.services.day_boundary import parse_timezone
from tests.conftest import auth_headers, make_user

TOKYO = ZoneInfo("Asia/Tokyo")
ZAGREB = ZoneInfo("Europe/Zagreb")


def tool_headers(telegram_id: str) -> dict:
    return {"X-Service-Token": create_service_token(), "X-Telegram-Id": str(telegram_id)}


# --------------------------------------------------------------------------- #
# Requirement A — settable, device-driven timezone
# --------------------------------------------------------------------------- #

def test_parse_timezone_accepts_iana_and_offsets():
    assert parse_timezone("Asia/Tokyo") == "Asia/Tokyo"
    assert parse_timezone("Europe/Zagreb") == "Europe/Zagreb"
    assert parse_timezone("UTC") == "UTC"
    assert parse_timezone("UTC+9") == "UTC+09:00"
    assert parse_timezone("+09:00") == "UTC+09:00"
    assert parse_timezone("+0530") == "UTC+05:30"
    assert parse_timezone("GMT-3") == "UTC-03:00"


def test_parse_timezone_rejects_garbage():
    for bad in ("Not/AZone", "garbage", "", "+15:00", "+09:99"):
        try:
            parse_timezone(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected")


def test_set_my_timezone_round_trip_and_restore(client, db):
    user = make_user(db, "Traveller", telegram_id="4101")
    h = tool_headers("4101")

    r = client.post("/api/v1/tools/set_my_timezone", json={"timezone": "Asia/Tokyo"}, headers=h)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["timezone"] == "Asia/Tokyo"
    assert data["previous_timezone"] == "Europe/Zagreb"
    assert data["day_start_hour"] == 4
    # Returned local clock must match Asia/Tokyo (UTC+9).
    now_tokyo = datetime.now(TOKYO)
    assert data["local_date"] == now_tokyo.date().isoformat()
    assert data["local_time"].startswith(now_tokyo.strftime("%H:%M"))
    assert data["utc_offset"] == "+09:00"

    db.expire_all()
    assert db.get(User, user.id).timezone == "Asia/Tokyo"

    # Restore (proves the previous value is restorable).
    r2 = client.post("/api/v1/tools/set_my_timezone", json={"timezone": "Europe/Zagreb"}, headers=h)
    assert r2.status_code == 200
    assert r2.json()["data"]["timezone"] == "Europe/Zagreb"
    db.expire_all()
    assert db.get(User, user.id).timezone == "Europe/Zagreb"


def test_set_my_timezone_offset_form_and_invalid(client, db):
    user = make_user(db, "OffsetTraveller", telegram_id="4102")
    h = tool_headers("4102")

    r = client.post("/api/v1/tools/set_my_timezone", json={"timezone": "UTC+9"}, headers=h)
    assert r.status_code == 200
    assert r.json()["data"]["timezone"] == "UTC+09:00"
    assert r.json()["data"]["utc_offset"] == "+09:00"

    bad = client.post("/api/v1/tools/set_my_timezone", json={"timezone": "Not/AZone"}, headers=h)
    assert bad.status_code == 400
    assert "timezone" in bad.text.lower()

    db.expire_all()
    # A rejected value must not have been stored.
    assert db.get(User, user.id).timezone == "UTC+09:00"


def test_me_timezone_endpoint_user_auth(client, db):
    user = make_user(db, "WebUser", telegram_id="4103")
    r = client.post("/api/v1/me/timezone", json={"timezone": "America/New_York"}, headers=auth_headers(user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["timezone"] == "America/New_York"
    assert body["utc_offset"] in ("-04:00", "-05:00")  # DST-aware
    db.expire_all()
    assert db.get(User, user.id).timezone == "America/New_York"

    bad = client.post("/api/v1/me/timezone", json={"timezone": "nope/nope"}, headers=auth_headers(user))
    assert bad.status_code == 400
    # No token -> 401.
    assert client.post("/api/v1/me/timezone", json={"timezone": "UTC"}).status_code == 401

    me = client.get("/api/v1/me", headers=auth_headers(user))
    assert me.status_code == 200
    assert me.json()["timezone"] == "America/New_York"


def test_today_summary_reports_device_clock(client, db):
    user = make_user(db, "ClockUser", telegram_id="4104")
    user.timezone = "Asia/Tokyo"
    db.commit()

    r = client.get("/api/v1/tools/get_my_today_summary", headers=tool_headers("4104"))
    assert r.status_code == 200
    data = r.json()["data"]
    now_tokyo = datetime.now(TOKYO)
    assert data["timezone"] == "Asia/Tokyo"
    assert data["local_date"] == now_tokyo.date().isoformat()
    assert data["local_time"].startswith(now_tokyo.strftime("%H:%M"))
    assert data["utc_offset"] == "+09:00"
    assert "nutrition_date" in data and "day_start_hour" in data


def test_meal_logged_at_0030_in_far_zone_filed_on_device_day(client, db):
    user = make_user(db, "NightOwl", telegram_id="4105")
    user.timezone = "Asia/Tokyo"
    db.commit()

    now_tokyo = datetime.now(TOKYO)
    target_local = datetime(now_tokyo.year, now_tokyo.month, now_tokyo.day, 0, 30, tzinfo=TOKYO)

    r = client.post(
        "/api/v1/tools/create_meal_from_text",
        json={"telegram_chat_id": "pgchat-7201", "telegram_message_id": "pg-devi-0","text": "rice 200 g", "meal_type": "dinner", "eaten_at": target_local.isoformat()},
        headers=tool_headers("4105"),
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["timezone"] == "Asia/Tokyo"
    assert data["local_date"] == target_local.date().isoformat()
    assert data["local_time"] == "00:30"
    assert data["utc_offset"] == "+09:00"
    # Nutrition day (boundary unchanged, default hour 4): 00:30 belongs to the
    # previous day, exactly as the user's device would report it.
    assert data["nutrition_date"] == (target_local.date() - timedelta(days=1)).isoformat()


# --------------------------------------------------------------------------- #
# Requirement B — feedback loop on every log + correction path
# --------------------------------------------------------------------------- #

def test_create_meal_from_text_payload_has_confirmation_fields(client, db):
    user = make_user(db, "Logger", telegram_id="4201")
    user.timezone = "Europe/Zagreb"
    db.commit()

    r = client.post(
        "/api/v1/tools/create_meal_from_text",
        json={"telegram_chat_id": "pgchat-7201", "telegram_message_id": "pg-devi-1","text": "rice 200 g", "meal_type": "dinner"},
        headers=tool_headers("4201"),
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    # Every field the 'For Monday dinner I logged …' message needs.
    for key in ("meal_id", "meal_type", "weekday", "local_date", "local_time",
                "timezone", "nutrition_date", "day_start_hour", "total_kcal", "items"):
        assert key in data, key
    assert data["meal_type"] == "dinner"
    assert data["weekday"] in ("Monday", "Tuesday", "Wednesday", "Thursday",
                               "Friday", "Saturday", "Sunday")
    # Legacy item keys preserved, enriched keys added.
    assert len(data["items"]) >= 1
    item = data["items"][0]
    for key in ("item_id", "display_name", "grams", "kcal", "quantity", "unit"):
        assert key in item, key


def test_confirm_payload_has_kcal_and_total(client, db):
    user = make_user(db, "ConfirmUser", telegram_id="4202")
    db.commit()

    r = client.post(
        "/api/v1/tools/create_meal_from_text",
        json={"telegram_chat_id": "pgchat-7201", "telegram_message_id": "pg-devi-2","text": "rice 200 g", "meal_type": "lunch"},
        headers=tool_headers("4202"),
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    meal_id = data["meal_id"]
    item_id = data["items"][0]["item_id"]
    assert meal_id and item_id

    # Pin exact nutrition (what the agent does), then confirm.
    r2 = client.post(
        "/api/v1/tools/update_meal_item",
        json={"meal_id": meal_id, "item_id": item_id,
              "patch": {"display_name": "Rice, white, long grain, cooked",
                        "grams": 200, "kcal_per_100g": 130}},
        headers=tool_headers("4202"),
    )
    assert r2.status_code == 200, r2.text

    r3 = client.post("/api/v1/tools/confirm_meal", json={"meal_id": meal_id}, headers=tool_headers("4202"))
    assert r3.status_code == 200, r3.text
    c = r3.json()["data"]
    assert c["status"] == "confirmed"
    assert c["items"][0]["kcal"] == 260.0
    assert c["total_kcal"] == 260.0
    assert c["totals"]["kcal"] == 260.0
    assert c["weekday"] and c["local_time"]

    # And get_pending_meal reports the same enriched shape on a confirmed meal.
    gp = client.post("/api/v1/tools/get_pending_meal", json={"meal_id": meal_id}, headers=tool_headers("4202"))
    assert gp.status_code == 200
    assert gp.json()["data"]["total_kcal"] == 260.0


def test_update_meal_moves_day_and_type(client, db):
    user = make_user(db, "Corrector", telegram_id="4203")
    user.timezone = "Europe/Zagreb"
    db.commit()

    r = client.post(
        "/api/v1/tools/create_meal_from_text",
        json={"telegram_chat_id": "pgchat-7201", "telegram_message_id": "pg-devi-3","text": "rice 200 g", "meal_type": "dinner"},
        headers=tool_headers("4203"),
    )
    meal_id = r.json()["data"]["meal_id"]
    before_nd = r.json()["data"]["nutrition_date"]

    yesterday = (datetime.now(ZAGREB).date() - timedelta(days=1)).isoformat()
    r2 = client.post(
        "/api/v1/tools/update_meal",
        json={"meal_id": meal_id, "eaten_at": yesterday, "meal_type": "lunch"},
        headers=tool_headers("4203"),
    )
    assert r2.status_code == 200, r2.text
    d = r2.json()["data"]
    assert d["changed"] == ["meal_type", "eaten_at"]
    assert d["meal_type"] == "lunch"
    assert d["nutrition_date"] == yesterday
    assert d["local_date"] == yesterday
    assert d["previous"]["nutrition_date"] == before_nd

    # Visible in the DB afterwards.
    db.expire_all()
    meal = db.get(Meal, uuid.UUID(meal_id))
    assert meal.meal_type == "lunch"
    assert meal.eaten_at.astimezone(ZAGREB).date().isoformat() == yesterday


def test_update_meal_rejects_far_date_and_foreign_meal(client, db):
    user = make_user(db, "Corrector2", telegram_id="4204")
    other = make_user(db, "OtherUser", telegram_id="4205")
    db.commit()

    r = client.post(
        "/api/v1/tools/create_meal_from_text",
        json={"telegram_chat_id": "pgchat-7201", "telegram_message_id": "pg-devi-4","text": "rice 200 g", "meal_type": "dinner"},
        headers=tool_headers("4204"),
    )
    meal_id = r.json()["data"]["meal_id"]

    far = (datetime.now(ZAGREB).date() + timedelta(days=30)).isoformat()
    bad = client.post("/api/v1/tools/update_meal", json={"meal_id": meal_id, "eaten_at": far},
                      headers=tool_headers("4204"))
    assert bad.status_code == 400
    assert "7 days" in bad.text

    # A different user cannot move this meal.
    foreign = client.post("/api/v1/tools/update_meal", json={"meal_id": meal_id, "meal_type": "lunch"},
                          headers=tool_headers("4205"))
    assert foreign.status_code == 404
    db.expire_all()
    assert db.get(Meal, uuid.UUID(meal_id)).meal_type == "dinner"
