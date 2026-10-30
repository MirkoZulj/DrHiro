"""Activity-logging tool tests (0.1.25).

The bot must be able to log a workout / activity with calories BURNED, list the
day's activities, and delete one — and logging an activity must NEVER touch the
meal / food totals (the old "file it as a meal" workaround added burned kcal to
the day's eaten total and lost logs).

Runs against the DEV database only (see conftest).
"""

from __future__ import annotations

from datetime import datetime, timezone

from drhiro_api.models import Activity, Meal
from drhiro_api.security import create_service_token
from tests.conftest import make_user


def tool_headers(telegram_id: str) -> dict:
    return {"X-Service-Token": create_service_token(), "X-Telegram-Id": str(telegram_id)}


def test_log_activity_returns_id_and_day_total(client, db):
    user = make_user(db, "ActivityLogger", telegram_id="4301")
    h = tool_headers("4301")

    r = client.post(
        "/api/v1/tools/log_activity",
        json={"title": "Gardening", "calories_burned": 850, "activity_date": "2020-01-01"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["title"] == "Gardening"
    assert data["calories_burned"] == 850
    assert data["date"] == "2020-01-01"
    assert data["activity_kcal_today"] == 850
    activity_id = data["id"]

    # The row is real and owned by the user.
    row = db.query(Activity).filter(Activity.id == activity_id).first()
    assert row is not None
    assert row.user_id == user.id
    assert row.calories_burned == 850

    # A second activity on the same day accumulates the day total.
    r2 = client.post(
        "/api/v1/tools/log_activity",
        json={"title": "Walk", "calories_burned": 100, "activity_date": "2020-01-01"},
        headers=h,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["data"]["activity_kcal_today"] == 950


def test_list_activities_shows_day(client, db):
    make_user(db, "ActivityLister", telegram_id="4302")
    h = tool_headers("4302")
    client.post(
        "/api/v1/tools/log_activity",
        json={"title": "Run", "calories_burned": 300, "activity_date": "2020-02-02"},
        headers=h,
    )

    r = client.post("/api/v1/tools/list_activities", json={"date": "2020-02-02"}, headers=h)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["date"] == "2020-02-02"
    assert data["count"] == 1
    assert data["total_burned"] == 300
    assert data["items"][0]["title"] == "Run"

    # A different day is empty.
    empty = client.post("/api/v1/tools/list_activities", json={"date": "2020-02-03"}, headers=h)
    assert empty.json()["data"]["count"] == 0


def test_delete_activity_removes_row(client, db):
    make_user(db, "ActivityDeleter", telegram_id="4303")
    h = tool_headers("4303")
    r = client.post(
        "/api/v1/tools/log_activity",
        json={"title": "Yoga", "calories_burned": 200, "activity_date": "2020-03-03"},
        headers=h,
    )
    activity_id = r.json()["data"]["id"]

    d = client.post("/api/v1/tools/delete_activity", json={"activity_id": activity_id}, headers=h)
    assert d.status_code == 200, d.text
    assert d.json()["data"]["deleted"] is True
    assert d.json()["data"]["activity_kcal_today"] == 0

    # Gone from the DB and from the list.
    assert db.query(Activity).filter(Activity.id == activity_id).first() is None
    after = client.post("/api/v1/tools/list_activities", json={"date": "2020-03-03"}, headers=h)
    assert after.json()["data"]["count"] == 0

    # Deleting again is a clean 404.
    again = client.post("/api/v1/tools/delete_activity", json={"activity_id": activity_id}, headers=h)
    assert again.status_code == 404


def test_log_activity_is_tenant_scoped(client, db):
    make_user(db, "Owner", telegram_id="4304")
    make_user(db, "Intruder", telegram_id="4305")
    r = client.post(
        "/api/v1/tools/log_activity",
        json={"title": "Swim", "calories_burned": 400, "activity_date": "2020-04-04"},
        headers=tool_headers("4304"),
    )
    activity_id = r.json()["data"]["id"]

    # Another paired user cannot delete it.
    bad = client.post(
        "/api/v1/tools/delete_activity",
        json={"activity_id": activity_id},
        headers=tool_headers("4305"),
    )
    assert bad.status_code == 404
    assert db.query(Activity).filter(Activity.id == activity_id).first() is not None


def test_log_activity_rejects_negative_calories(client, db):
    make_user(db, "NegUser", telegram_id="4306")
    r = client.post(
        "/api/v1/tools/log_activity",
        json={"title": "Impossible", "calories_burned": -5},
        headers=tool_headers("4306"),
    )
    assert r.status_code == 422


def test_log_activity_does_not_change_meal_or_food_totals(client, db):
    """THE guard that matters: an activity is burned energy, not food.

    Seed a real meal for today, snapshot the today-summary food totals, log an
    activity, and prove the meal count and eaten kcal are byte-for-byte
    unchanged while the activity total rises by exactly the logged amount.
    """
    user = make_user(db, "FoodGuard", telegram_id="4307")
    h = tool_headers("4307")

    meal = Meal(
        user_id=user.id,
        eaten_at=datetime.now(timezone.utc),
        status="confirmed",
        meal_type="lunch",
        totals_json={"kcal": 500.0, "protein_g": 30.0, "carbs_g": 40.0, "fat_g": 20.0},
    )
    db.add(meal)
    db.commit()

    before = client.get("/api/v1/tools/get_my_today_summary", headers=h).json()["data"]
    assert before["meals_logged_today"] == 1
    assert before["calories_kcal_today"] == 500.0

    r = client.post(
        "/api/v1/tools/log_activity",
        json={"title": "Gardening", "calories_burned": 850},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["activity_kcal_today"] == 850

    after = client.get("/api/v1/tools/get_my_today_summary", headers=h).json()["data"]
    # Food ledger untouched.
    assert after["meals_logged_today"] == before["meals_logged_today"] == 1
    assert after["calories_kcal_today"] == before["calories_kcal_today"] == 500.0
    # Activity ledger moved by exactly the logged amount.
    assert after["activity_kcal_today"] == before["activity_kcal_today"] + 850

    # The meal row is still a confirmed meal, not deleted/altered.
    db.refresh(meal)
    assert meal.status == "confirmed"
    assert (meal.totals_json or {}).get("kcal") == 500.0
    # And no phantom meal was created.
    assert db.query(Meal).filter(Meal.user_id == user.id).count() == 1


def test_undo_last_user_action_undoes_activity(client, db):
    make_user(db, "ActivityUndo", telegram_id="4308")
    h = tool_headers("4308")
    r = client.post(
        "/api/v1/tools/log_activity",
        json={"title": "Cycling", "calories_burned": 350},
        headers=h,
    )
    activity_id = r.json()["data"]["id"]

    u = client.post("/api/v1/tools/undo_last_user_action", headers=h)
    assert u.status_code == 200, u.text
    assert "activity" in u.json()["message"].lower()
    assert db.query(Activity).filter(Activity.id == activity_id).first() is None