"""Bucketed macro support on /trends/bucketed.

The macro distribution chart must share the calorie chart's window, so these
tests pin: bucket counts per granularity, Mon-Sun day labels, null (not 0) for
empty buckets, local-midnight bucketing, and per-bucket summation.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from drhiro_api.models import Meal

from tests.conftest import auth_headers

TZ = ZoneInfo("Europe/Zagreb")


def _monday_of(d):
    """Monday of the ISO week containing d (mirrors the endpoint's bucketing)."""
    return d - timedelta(days=d.weekday())


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


def test_week_covers_the_whole_quarter(client, user_a):
    """A calendar quarter is 13 or 14 ISO weeks — never fewer than it needs."""
    out = _get(client, user_a, "week")
    assert 13 <= len(out["points"]) <= 14
    assert out["period_label"].startswith("Q")
    # every bucket is a real ISO week label, and the first one covers the
    # quarter's opening days (bucket 0 may start before the 1st; the SQL window
    # keeps only in-quarter meals)
    assert all(p["label"].startswith("W") for p in out["points"])
    start, end = _current_quarter_bounds()
    base = _monday_of(start)
    last = _monday_of(end - timedelta(days=1))
    assert len(out["points"]) == (last - base).days // 7 + 1


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


# ---------------------------------------------------------------------------
# Offset navigation — the back-navigation math was previously unexercised.
# ---------------------------------------------------------------------------

def test_day_offset_moves_one_week_back_in_both_charts(client, user_a):
    cur = _get(client, user_a, "day", offset=0)
    prev = _get(client, user_a, "day", offset=1)
    cal_prev = _get(client, user_a, "day", offset=1, metric="calories")
    assert prev["period_key"] != cur["period_key"]
    assert prev["period_label"] == cal_prev["period_label"]
    assert prev["period_key"] == cal_prev["period_key"]
    assert [p["date"] for p in prev["points"]] == [p["date"] for p in cal_prev["points"]]
    # one week earlier, to the day
    first = datetime.fromisoformat(cur["points"][0]["date"]).date()
    first_prev = datetime.fromisoformat(prev["points"][0]["date"]).date()
    assert (first - first_prev).days == 7


def test_week_offset_moves_one_quarter_back_in_both_charts(client, user_a):
    cur = _get(client, user_a, "week", offset=0)
    prev = _get(client, user_a, "week", offset=1)
    cal_prev = _get(client, user_a, "week", offset=1, metric="calories")
    assert prev["period_label"] != cur["period_label"]
    assert prev["period_label"] == cal_prev["period_label"]
    assert prev["period_key"] == cal_prev["period_key"]
    assert [p["label"] for p in prev["points"]] == [p["label"] for p in cal_prev["points"]]
    # the previous quarter must start before the current one and be a real date
    assert prev["period_key"] < cur["period_key"]
    assert datetime.fromisoformat(prev["period_key"]).date() < datetime.fromisoformat(cur["period_key"]).date()


# ---------------------------------------------------------------------------
# Quarter boundary — a calendar quarter is not a whole number of ISO weeks, so
# its final days used to fall outside the 13 buckets and vanish from the chart.
# ---------------------------------------------------------------------------

def _current_quarter_bounds():
    today = datetime.now(TZ).date()
    qm = (today.month - 1) // 3 * 3 + 1
    start = date(today.year, qm, 1)
    end = date(today.year + 1, 1, 1) if qm + 3 > 12 else date(today.year, qm + 3, 1)
    return start, end


def test_quarter_tail_days_are_not_dropped(client, db, user_a):
    """The last days of a quarter are inside the window and must be bucketed."""
    start, end = _current_quarter_bounds()
    tail = end - timedelta(days=2)  # inside the quarter's final ISO week
    assert start <= tail < end
    _meal(db, user_a, _local_day_at(tail, 12), 900, 50, 60, 30)

    mac = _get(client, user_a, "week", offset=0)
    cal = _get(client, user_a, "week", offset=0, metric="calories")

    assert len(mac["points"]) >= 13
    assert sum(p["kcal"] or 0 for p in mac["points"]) == 900.0, "meal on a quarter tail day vanished from the macro chart"
    assert sum(p["value"] or 0 for p in cal["points"]) == 900.0, "meal on a quarter tail day vanished from the calorie chart"
    # and the two charts still agree bucket for bucket
    assert [p["kcal"] for p in mac["points"]] == [p["value"] for p in cal["points"]]
    # no in-window day may be left without a bucket
    labels = [p["label"] for p in mac["points"]]
    assert labels[-1].startswith("W")


def test_week_bucket_indexes_the_meal_correctly(client, db, user_a):
    """A meal in week k of the quarter lands in bucket k (not merely len==13)."""
    start, end = _current_quarter_bounds()
    base_monday = _monday_of(start)
    meal_day = start + timedelta(days=8)  # second week of the quarter
    _meal(db, user_a, _local_day_at(meal_day, 12), 640, 30, 40, 20)

    out = _get(client, user_a, "week", offset=0)
    expected = (_monday_of(meal_day) - base_monday).days // 7
    assert out["points"][expected]["kcal"] == 640.0
    assert out["points"][expected]["label"] == f"W{meal_day.isocalendar()[1]}"
    others = [p["kcal"] for i, p in enumerate(out["points"]) if i != expected]
    assert all(v is None for v in others)


# ---------------------------------------------------------------------------
# Month window boundary
# ---------------------------------------------------------------------------

def test_month_window_boundary(client, db, user_a):
    """The oldest month is included; a meal just before the window is not."""
    today = datetime.now(TZ).date()
    start = date(today.year - 1, today.month, 1)  # matches the endpoint's month window

    _meal(db, user_a, _local_day_at(start, 12), 500, 30, 40, 20)          # in window
    _meal(db, user_a, _local_day_at(start - timedelta(days=1), 12), 777, 1, 1, 1)  # out

    out = _get(client, user_a, "month", offset=0)
    assert out["points"][0]["kcal"] == 500.0
    assert all(p["kcal"] != 777.0 for p in out["points"])


# ---------------------------------------------------------------------------
# /trends daily nutrition must use the same local day as the bucketed charts
# ---------------------------------------------------------------------------

def _get_trends(client, user, period="7d", metric="calories"):
    return client.get(
        f"/api/v1/trends?metric={metric}&period={period}",
        headers=auth_headers(user),
    ).json()


def test_daily_nutrition_uses_local_day_not_utc(client, db, user_a):
    """A meal at local 00:30 belongs to the local day, as it does in the charts."""
    monday = _this_monday()
    late = _local_day_at(monday, 0, 30)  # 22:30 the previous day in UTC
    _meal(db, user_a, late, 100, 5, 10, 4)

    trend = _get_trends(client, user_a, "7d")
    assert trend["points"], "meal missing from the daily nutrition trend"
    assert trend["points"][-1]["date"] == monday.isoformat(), (
        "daily nutrition keyed the meal to the UTC day while the charts use the local day"
    )

    # and the bucketed charts agree with it
    mac = _get(client, user_a, "day", metric="macros")
    assert mac["points"][0]["kcal"] == 100.0
    assert mac["points"][0]["date"] == monday.isoformat()
