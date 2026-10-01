"""Bucketed macro support on /trends/bucketed.

The macro distribution chart must share the calorie chart's window, so these
tests pin: bucket counts per granularity, Mon-Sun day labels, null (not 0) for
empty buckets, local-midnight bucketing, and per-bucket summation.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from drhiro_api.models import Meal

from tests.conftest import auth_headers

TZ = ZoneInfo("Europe/Zagreb")


def _meal(db, user, when, kcal, protein, carbs, fat, status="confirmed"):
    m = Meal(
        user_id=user.id,
        eaten_at=when,
        status=status,
        meal_type="lunch",
        totals_json={"kcal": kcal, "protein_g": protein, "carbs_g": carbs, "fat_g": fat},
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _local_day_at(d, hour, minute=0):
    return datetime.combine(d, dtime(hour, minute), tzinfo=TZ)


def _this_monday():
    today = datetime.now(TZ).date()
    return today - timedelta(days=today.weekday())


def _get(client, user, gran, offset=0, metric="macros"):
    return client.get(
        f"/api/v1/trends/bucketed?metric={metric}&granularity={gran}&offset={offset}",
        headers=auth_headers(user),
    ).json()


def test_day_has_seven_buckets_with_mon_sun_labels(client, db, user_a):
    out = _get(client, user_a, "day")
    assert out["granularity"] == "day"
    assert out["metric"] == "macros"
    assert len(out["points"]) == 7
    assert [p["label"] for p in out["points"]] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert out["period_label"].startswith("Week of ")


def test_week_has_thirteen_buckets(client, user_a):
    out = _get(client, user_a, "week")
    assert len(out["points"]) == 13
    assert out["period_label"].startswith("Q")


def test_month_has_twelve_buckets(client, user_a):
    out = _get(client, user_a, "month")
    assert len(out["points"]) == 12
    assert out["period_label"] == "Last 12 months"


def test_empty_bucket_is_null_not_zero(client, db, user_a):
    monday = _this_monday()
    _meal(db, user_a, _local_day_at(monday, 12), 600, 30, 60, 20)
    out = _get(client, user_a, "day")
    mon = out["points"][0]
    assert mon["kcal"] == 600.0
    assert mon["meals_count"] == 1
    for p in out["points"][1:]:
        assert p["kcal"] is None
        assert p["protein_g"] is None
        assert p["carbs_g"] is None
        assert p["fat_g"] is None
        assert p["meals_count"] == 0


def test_meal_near_local_midnight_lands_in_local_bucket(client, db, user_a):
    monday = _this_monday()
    # 00:30 Monday local == 22:30 Sunday UTC in summer; must be the Monday bucket.
    _meal(db, user_a, _local_day_at(monday, 0, 30), 100, 5, 10, 4)
    # 23:30 Sunday local == 21:30 Sunday UTC; must be the Sunday bucket.
    _meal(db, user_a, _local_day_at(monday + timedelta(days=6), 23, 30), 200, 10, 20, 8)
    out = _get(client, user_a, "day")
    assert out["points"][0]["label"] == "Mon"
    assert out["points"][0]["kcal"] == 100.0
    assert out["points"][6]["label"] == "Sun"
    assert out["points"][6]["kcal"] == 200.0


def test_totals_sum_across_multiple_meals_in_one_bucket(client, db, user_a):
    monday = _this_monday()
    _meal(db, user_a, _local_day_at(monday, 9), 300, 20, 30, 10)
    _meal(db, user_a, _local_day_at(monday, 14), 450, 25, 40, 18)
    out = _get(client, user_a, "day")
    mon = out["points"][0]
    assert mon["meals_count"] == 2
    assert mon["kcal"] == 750.0
    assert mon["protein_g"] == 45.0
    assert mon["carbs_g"] == 70.0
    assert mon["fat_g"] == 28.0


def test_deleted_meal_is_excluded(client, db, user_a):
    monday = _this_monday()
    _meal(db, user_a, _local_day_at(monday, 9), 300, 20, 30, 10, status="deleted")
    out = _get(client, user_a, "day")
    assert out["points"][0]["kcal"] is None
    assert out["points"][0]["meals_count"] == 0


def test_macro_window_matches_calorie_window(client, db, user_a):
    monday = _this_monday()
    _meal(db, user_a, _local_day_at(monday, 9), 500, 30, 50, 20)
    for gran in ("day", "week", "month"):
        mac = _get(client, user_a, gran, metric="macros")
        cal = _get(client, user_a, gran, metric="calories")
        assert [p["label"] for p in mac["points"]] == [p["label"] for p in cal["points"]]
        assert [p["date"] for p in mac["points"]] == [p["date"] for p in cal["points"]]
        assert mac["period_label"] == cal["period_label"]
        assert mac["period_key"] == cal["period_key"]


def test_calorie_bucket_uses_local_day_like_macros(client, db, user_a):
    """Both charts must bucket a local-midnight meal the same way.

    A meal at 00:30 Monday local is 22:30 Sunday UTC; bucketing the calorie
    chart on the UTC date would drop it from the week entirely (idx < 0).
    """
    monday = _this_monday()
    _meal(db, user_a, _local_day_at(monday, 0, 30), 100, 5, 10, 4)
    cal = _get(client, user_a, "day", metric="calories")
    mac = _get(client, user_a, "day", metric="macros")
    assert cal["points"][0]["label"] == "Mon"
    assert cal["points"][0]["value"] == 100.0
    assert mac["points"][0]["kcal"] == 100.0


def test_calorie_and_macro_values_agree_per_bucket(client, db, user_a):
    """The kcal line of the macro chart and the calorie chart are the same data."""
    monday = _this_monday()
    _meal(db, user_a, _local_day_at(monday, 9), 300, 20, 30, 10)
    _meal(db, user_a, _local_day_at(monday, 23, 30), 450, 25, 40, 18)
    _meal(db, user_a, _local_day_at(monday + timedelta(days=2), 12), 700, 40, 60, 25)
    cal = _get(client, user_a, "day", metric="calories")
    mac = _get(client, user_a, "day", metric="macros")
    assert [p["value"] for p in cal["points"]] == [p["kcal"] for p in mac["points"]]
