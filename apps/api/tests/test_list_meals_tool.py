"""list_meals read-back tool + confirmed-meal item patch (0.1.34).

The agent could create, verify and confirm meals but had no way to LIST them,
so "the bread was 250 g, not 300" degraded into "send me the meal id from the
dashboard" — and then into a flatly false claim that a confirmed meal cannot be
edited. These tests pin both halves of that flow:

* ``POST /tools/list_meals`` returns the ids (meal_id, item_id) for a local day,
  using the same day boundary as ``nutrition_date``.
* ``POST /tools/update_meal_item`` patches a CONFIRMED meal in place and
  rewrites the parent totals — no re-log, no undo, no status guard.

Runs against the DEV database only (see conftest).
"""

from __future__ import annotations

from datetime import datetime, timezone

from drhiro_api.models import Meal, MealItem
from drhiro_api.security import create_service_token
from tests.conftest import make_user


def tool_headers(telegram_id: str) -> dict:
    return {"X-Service-Token": create_service_token(), "X-Telegram-Id": str(telegram_id)}


def _meal(db, user, *, eaten_at, meal_type="dinner", status="confirmed",
          items=(("WHITE BREAD", 300.0, 831.0),)):
    total = sum(i[2] for i in items)
    meal = Meal(user_id=user.id, eaten_at=eaten_at, meal_type=meal_type,
                status=status, totals_json={"kcal": total})
    db.add(meal)
    db.flush()
    out = []
    for name, grams, kcal in items:
        item = MealItem(meal_id=meal.id, display_name=name, quantity=1.0, grams=grams,
                        nutrients_json={"kcal": kcal, "resolved_food": name}, source="usda")
        db.add(item)
        out.append(item)
    db.commit()
    db.refresh(meal)
    return meal, out


def test_list_meals_returns_ids_for_a_past_day(client, db, monkeypatch):
    monkeypatch.setenv("DRHIRO_DAY_START_HOUR", "4")
    user = make_user(db, "MealLister", telegram_id="4401")
    # Tuesday 2026-10-27 23:44 Europe/Zagreb (CET, +01:00).
    meal, items = _meal(db, user, eaten_at=datetime(2026, 10, 27, 22, 44, tzinfo=timezone.utc))

    r = client.post("/api/v1/tools/list_meals", json={"date": "2026-10-27"},
                    headers=tool_headers("4401"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["date"] == "2026-10-27"
    assert data["weekday"] == "Tuesday"
    assert data["count"] == 1

    entry = data["meals"][0]
    assert entry["meal_id"] == str(meal.id)
    assert entry["status"] == "confirmed"
    assert entry["local_time"] == "23:44"
    assert entry["total_kcal"] == 831.0
    assert [i["item_id"] for i in entry["items"]] == [str(items[0].id)]
    assert entry["items"][0]["grams"] == 300.0
    assert entry["items"][0]["kcal"] == 831.0


def test_list_meals_uses_the_nutrition_day_boundary(client, db, monkeypatch):
    monkeypatch.setenv("DRHIRO_DAY_START_HOUR", "4")
    user = make_user(db, "MealListerBoundary", telegram_id="4402")
    # 2026-10-27 01:30 local is BEFORE the 04:00 day start -> 10-26's day.
    _meal(db, user, eaten_at=datetime(2026, 10, 27, 0, 30, tzinfo=timezone.utc),
          meal_type="snack")

    same_day = client.post("/api/v1/tools/list_meals", json={"date": "2026-10-27"},
                           headers=tool_headers("4402")).json()["data"]
    assert same_day["count"] == 0, same_day

    previous = client.post("/api/v1/tools/list_meals", json={"date": "2026-10-26"},
                           headers=tool_headers("4402")).json()["data"]
    assert previous["count"] == 1
    assert previous["meals"][0]["meal_type"] == "snack"
    assert previous["meals"][0]["local_time"] == "01:30"


def test_list_meals_excludes_deleted_and_other_users_and_filters_type(client, db, monkeypatch):
    monkeypatch.setenv("DRHIRO_DAY_START_HOUR", "4")
    user = make_user(db, "MealListerFilter", telegram_id="4403")
    other = make_user(db, "MealListerOther", telegram_id="4404")
    _meal(db, user, eaten_at=datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc), meal_type="breakfast")
    _meal(db, user, eaten_at=datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc), meal_type="lunch")
    _meal(db, user, eaten_at=datetime(2026, 10, 27, 13, 0, tzinfo=timezone.utc),
          meal_type="lunch", status="deleted")
    _meal(db, other, eaten_at=datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc), meal_type="lunch")

    everything = client.post("/api/v1/tools/list_meals", json={"date": "2026-10-27"},
                             headers=tool_headers("4403")).json()["data"]
    assert everything["count"] == 2
    assert [m["meal_type"] for m in everything["meals"]] == ["breakfast", "lunch"]

    lunches = client.post("/api/v1/tools/list_meals",
                          json={"date": "2026-10-27", "meal_type": "lunch"},
                          headers=tool_headers("4403")).json()["data"]
    assert lunches["count"] == 1
    # 12:00 UTC is 13:00 Europe/Zagreb (CET — DST ended 2026-10-25).
    assert lunches["meals"][0]["local_time"] == "13:00"


def test_update_meal_item_patches_a_confirmed_meal_and_rewrites_totals(client, db):
    """The incident: 'you can't edit a confirmed meal' was false."""
    user = make_user(db, "ConfirmedEditor", telegram_id="4405")
    meal, items = _meal(
        db, user,
        eaten_at=datetime(2026, 10, 27, 22, 44, tzinfo=timezone.utc),
        items=(("WHITE BREAD", 300.0, 831.0), ("PROSCIUTTO", 50.0, 107.0)),
    )
    assert meal.status == "confirmed"

    r = client.post(
        "/api/v1/tools/update_meal_item",
        json={"meal_id": str(meal.id), "item_id": str(items[0].id),
              "patch": {"grams": 250, "kcal_per_100g": 277}},
        headers=tool_headers("4405"),
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "confirmed"
    assert data["item"]["kcal"] == 692.5

    db.expire_all()
    patched = db.query(MealItem).filter(MealItem.id == items[0].id).first()
    assert patched.grams == 250
    assert patched.nutrients_json["kcal"] == 692.5
    parent = db.query(Meal).filter(Meal.id == meal.id).first()
    assert parent.status == "confirmed"
    assert round(parent.totals_json["kcal"], 1) == 799.5
