"""Series parity test (0.9.0 part 1 — spec §2.1, written BEFORE the refactor).

Asserts that ``services.series.daily_series`` returns exactly what the
dashboard handlers (/trends/daily/{metric} and /trends/bucketed) return for
the same seed data, across:

- 7 / 30 / 90-day windows,
- the Europe/Zagreb DST changes (spring forward 2026-03-29, fall back
  2026-10-25) — the spring window is reached through /trends/bucketed's
  ``offset`` navigation because /trends/daily is anchored at today,
- the ``day_start_hour`` (04:00) nutrition-day boundary.

Run first against the ORIGINAL handler code to capture the baseline; then the
handlers are refactored to call daily_series and this test must stay green
unchanged.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from drhiro_api.models import Activity, Meal, Measurement
from drhiro_api.services.series import daily_series
from tests.conftest import auth_headers, make_user

ZAGREB = ZoneInfo("Europe/Zagreb")


def _local(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ZAGREB).astimezone(timezone.utc)


def _steps(db, user, start_utc, count, minutes=30, provider="health_connect"):
    db.add(Measurement(
        user_id=user.id, metric_type="steps", start_at=start_utc,
        end_at=start_utc + timedelta(minutes=minutes), value_json={"count": count},
        source_provider=provider, source_record_id=f"steps-{start_utc.isoformat()}-{count}",
        recording_method="automatic",
    ))


def _sleep(db, user, start_utc, end_utc, duration_min):
    db.add(Measurement(
        user_id=user.id, metric_type="sleep", start_at=start_utc,
        end_at=end_utc, value_json={"duration_min": duration_min},
        source_provider="mi_fitness",        source_record_id=f"sleep-{start_utc.isoformat()}-{duration_min}",
        recording_method="automatic",
    ))


def _meas(db, user, mt, start_utc, vj, provider="manual"):
    db.add(Measurement(
        user_id=user.id, metric_type=mt, start_at=start_utc,
        value_json=vj, source_provider=provider,
        source_record_id=f"{mt}-{start_utc.isoformat()}-{list(vj.items())}",
        recording_method="manual",
    ))


def _water(db, user, start_utc, ml, category):
    _meas(db, user, "water", start_utc, {"amount_ml": ml, "category": category},
          provider="health_connect")


def _meal(db, user, eaten_utc, kcal, protein=0.0, carbs=0.0, fat=0.0, status="confirmed"):
    db.add(Meal(
        user_id=user.id, eaten_at=eaten_utc, meal_type="lunch", status=status,
        totals_json={"kcal": kcal, "protein_g": protein, "carbs_g": carbs, "fat_g": fat},
    ))


def _seed_dst_spring(db, user):
    """Records across the 2026-03-29 spring-forward (02:00 -> 03:00 local)."""
    # Sleep crossing the spring-forward night: Sat 22:00 -> Sun 07:00 local
    # is only 8 wall hours (clock jumped 02:00->03:00).
    _sleep(db, user, _local(2026, 3, 28, 22, 0), _local(2026, 3, 29, 7, 0), 435)
    _sleep(db, user, _local(2026, 3, 28, 22, 0), _local(2026, 3, 29, 6, 0), 300)  # overlapping sub-stage
    # Steps: a 30-min bucket + a detail record inside it (dedup exercise).
    _steps(db, user, _local(2026, 3, 29, 9, 0), 2000, minutes=30)
    _steps(db, user, _local(2026, 3, 29, 9, 10), 500, minutes=5)
    _steps(db, user, _local(2026, 3, 30, 9, 0), 1500, minutes=30)
    _meas(db, user, "weight", _local(2026, 3, 29, 8, 0), {"weight_kg": 80.2})
    _meas(db, user, "weight", _local(2026, 3, 29, 20, 0), {"weight_kg": 80.6})
    _meas(db, user, "heart_rate", _local(2026, 3, 29, 10, 0), {"bpm": 60})
    _meas(db, user, "heart_rate", _local(2026, 3, 29, 11, 0), {"bpm": 70})
    _water(db, user, _local(2026, 3, 29, 12, 0), 250, "water")
    # Meal at 03:30 local BEFORE the boundary shift: belongs to the previous
    # nutrition day (day_start_hour=4).
    _meal(db, user, _local(2026, 3, 29, 3, 30), 300.0, protein=10.0)
    _meal(db, user, _local(2026, 3, 29, 13, 0), 700.0, carbs=80.0, fat=20.0)
    db.add(Activity(user_id=user.id, activity_date=date(2026, 3, 29),
                    title="run", calories_burned=350.0))
    db.commit()


def _seed_dst_autumn(db, user):
    """Records across the 2026-10-25 fall-back (03:00 -> 02:00 local)."""
    _sleep(db, user, _local(2026, 10, 24, 22, 0), _local(2026, 10, 25, 7, 30), 510)
    _steps(db, user, _local(2026, 10, 25, 9, 0), 3000, minutes=30)
    _steps(db, user, _local(2026, 10, 26, 9, 0), 1200, minutes=30)
    _meas(db, user, "weight", _local(2026, 10, 25, 8, 0), {"weight_kg": 79.4})
    _meal(db, user, _local(2026, 10, 25, 12, 0), 650.0, protein=30.0)


def _seed_today_window(db, user):
    """Records in the last ~10 days around 'now', incl. the 04:00 boundary and
    an alcohol liquid."""
    now_local = datetime.now(ZAGREB)
    for back in range(9):
        day = (now_local - timedelta(days=back)).date()
        base = datetime(day.year, day.month, day.day, tzinfo=ZAGREB)
        _steps(db, user, base.astimezone(timezone.utc) + timedelta(hours=9), 4000 + back * 100, minutes=30)
        _meas(db, user, "weight", base.astimezone(timezone.utc) + timedelta(hours=8),
              {"weight_kg": 80.0 - back * 0.1})
        _water(db, user, base.astimezone(timezone.utc) + timedelta(hours=13), 300, "water")
        if back % 3 == 0:
            _water(db, user, base.astimezone(timezone.utc) + timedelta(hours=20), 500, "beer")
        _meal(db, user, base.astimezone(timezone.utc) + timedelta(hours=13, minutes=30),
              600.0 + back * 10, protein=25.0, carbs=60.0, fat=20.0)
        # A meal at 03:30 local -> previous NUTRITION day.
        if back in (1, 4):
            _meal(db, user, base.astimezone(timezone.utc) + timedelta(hours=3, minutes=30),
                  250.0, fat=5.0, status="draft" if back == 4 else "confirmed")
    # Overlapping sleep for dedup parity, ending this morning.
    _sleep(db, user, (now_local - timedelta(days=1)).replace(hour=23, minute=0)
           .astimezone(timezone.utc), now_local.astimezone(timezone.utc), 420)
    db.commit()


_METRICS = ["steps", "water_ml", "sleep_duration_min", "heart_rate_bpm", "weight_kg"]


def _points_map(points):
    return {p["date"]: p["value"] for p in points}


@pytest.mark.parametrize("days", [7, 30, 90])
def test_parity_trends_daily_today_window(client, db, days):
    user = make_user(db, "SeriesParityToday", telegram_id="9601")
    _seed_today_window(db, user)

    from drhiro_api.services.day_boundary import resolve_tz
    tz = resolve_tz(user.timezone)
    end = datetime.now(tz).date()
    start = end - timedelta(days=days - 1)

    for metric in _METRICS:
        r = client.get(f"/api/v1/trends/daily/{metric}?days={days}",
                       headers=auth_headers(user))
        assert r.status_code == 200, r.text
        handler = _points_map(r.json()["points"])
        series = daily_series(db, user, metric, start, end)
        assert [dv.day.isoformat() for dv in series][:3] == [
            (start + timedelta(days=i)).isoformat() for i in range(3)]
        for dv in series:
            expected = handler.get(dv.day.isoformat(), "MISSING")
            got = None if dv.value is None else round(dv.value, 1)
            assert got == expected, (
                f"{metric} {dv.day.isoformat()}: daily_series={got} handler={expected}")


def test_parity_trends_bucketed_day_granularity(client, db):
    user = make_user(db, "SeriesParityBucketed", telegram_id="9602")
    _seed_today_window(db, user)
    _seed_dst_autumn(db, user)
    _seed_dst_spring(db, user)

    from drhiro_api.services.day_boundary import resolve_tz
    tz = resolve_tz(user.timezone)
    today = datetime.now(tz).date()
    monday = today - timedelta(days=today.weekday())

    # Current week (offset 0) + the DST-autumn week + the DST-spring week.
    autumn_monday = date(2026, 10, 19)
    spring_monday = date(2026, 3, 23)
    for offset in [0, (monday - autumn_monday).days // 7, (monday - spring_monday).days // 7]:
        r = client.get("/api/v1/trends/bucketed?metric=steps&granularity=day"
                       f"&offset={offset}", headers=auth_headers(user))
        assert r.status_code == 200, r.text
        handler = _points_map(r.json()["points"])
        week_start = monday - timedelta(weeks=offset)
        series = daily_series(db, user, "steps", week_start, week_start + timedelta(days=6))
        for dv in series:
            if dv.day.isoformat() not in handler:
                continue  # future days of the current week are not returned
            got = None if dv.value is None else round(dv.value, 1)
            assert got == handler[dv.day.isoformat()], (
                f"steps offset={offset} {dv.day}: {got} != {handler[dv.day.isoformat()]}")


def test_parity_bucketed_water_sleep_weight_hr_and_macros(client, db):
    user = make_user(db, "SeriesParityMacros", telegram_id="9603")
    _seed_today_window(db, user)
    _seed_dst_spring(db, user)
    _seed_dst_autumn(db, user)

    from drhiro_api.services.day_boundary import resolve_tz
    tz = resolve_tz(user.timezone)
    today = datetime.now(tz).date()
    monday = today - timedelta(days=today.weekday())
    autumn_monday = date(2026, 10, 19)

    for offset in [0, (monday - autumn_monday).days // 7]:
        week_start = monday - timedelta(weeks=offset)
        for metric, key in [("water_ml", "water_ml"), ("sleep_duration_min", "sleep_duration_min"),
                            ("weight_kg", "weight_kg"), ("heart_rate_bpm", "heart_rate_bpm")]:
            r = client.get(f"/api/v1/trends/bucketed?metric={metric}&granularity=day"
                           f"&offset={offset}", headers=auth_headers(user))
            assert r.status_code == 200, (metric, r.text)
            handler = _points_map(r.json()["points"])
            series = daily_series(db, user, key, week_start, week_start + timedelta(days=6))
            for dv in series:
                if dv.day.isoformat() not in handler:
                    continue
                got = None if dv.value is None else round(dv.value, 1)
                assert got == handler[dv.day.isoformat()], (
                    f"{metric} offset={offset} {dv.day}: {got} != {handler[dv.day.isoformat()]}")

        # Macros: compare daily_series kcal_in/protein/carbs/fat with the
        # handler's macros points (kcal/protein_g/carbs_g/fat_g per day).
        r = client.get("/api/v1/trends/bucketed?metric=macros&granularity=day"
                       f"&offset={offset}", headers=auth_headers(user))
        assert r.status_code == 200, r.text
        handler = {p["date"]: p for p in r.json()["points"]}
        for key, field in [("kcal_in", "kcal"), ("protein_g", "protein_g"),
                           ("carbs_g", "carbs_g"), ("fat_g", "fat_g")]:
            series = daily_series(db, user, key, week_start, week_start + timedelta(days=6))
            for dv in series:
                if dv.day.isoformat() not in handler:
                    continue
                expected = handler[dv.day.isoformat()][field]
                got = None if dv.value is None else round(dv.value, 1)
                assert got == expected, (
                    f"{key} offset={offset} {dv.day}: {got} != {expected}")


def test_parity_missing_data_is_none_and_alcohol_zero_rule(client, db):
    user = make_user(db, "SeriesParityRules", telegram_id="9604")
    now_local = datetime.now(ZAGREB)
    today = now_local.date()
    # One water-only day (with beer), one empty day, one steps day.
    base = datetime(today.year, today.month, today.day, tzinfo=ZAGREB)
    _water(db, user, base.astimezone(timezone.utc) + timedelta(hours=12), 330, "water")
    _water(db, user, base.astimezone(timezone.utc) + timedelta(hours=18), 250, "beer")
    _steps(db, user, base.astimezone(timezone.utc) + timedelta(hours=10), 1000, minutes=30)
    db.commit()

    # Empty day (2 days ago): everything None, never 0.
    empty = today - timedelta(days=2)
    assert daily_series(db, user, "steps", empty, empty)[0].value is None
    assert daily_series(db, user, "water_ml", empty, empty)[0].value is None
    # Alcohol rule: liquids logged, none alcoholic -> 0.0.
    assert daily_series(db, user, "alcohol_ml", empty, empty)[0].value is None
    # Beer day: alcohol > 0.
    alcohol_today = daily_series(db, user, "alcohol_ml", today, today)[0]
    assert alcohol_today.value == 250.0
    # kcal_in on a day with no confirmed meals -> None.
    assert daily_series(db, user, "kcal_in", empty, empty)[0].value is None


def test_parity_dst_spring_wake_date_and_boundary(client, db):
    """Direct daily_series checks across the 2026-03-29 spring-forward."""
    user = make_user(db, "SeriesParityDST", telegram_id="9605")
    _seed_dst_spring(db, user)

    d29 = date(2026, 3, 29)
    d28 = date(2026, 3, 28)
    # Sleep is keyed to the WAKE-UP day; both sessions dedup into one total.
    # The series window must include the bedtime record (it STARTS 3/28 22:00
    # local), so query from the day before — same as the handler's window.
    sleep = {dv.day: dv for dv in daily_series(db, user, "sleep_duration_min", date(2026, 3, 27), d29)}[d29]
    # Spring-forward night: 22:00 -> 07:00 wall clock is only 8 hours
    # (clock jumped 02:00->03:00). _dedup_sleep credits the longest session
    # fully (300) and the 435-min one only for its uncovered 60 min of the
    # 480-min span: 300 + 435*60/480 = 354.4 -> 354.
    assert sleep.value == 354
    assert sleep.n == 2
    # Steps dedup: the 500-step detail record sits ENTIRELY inside the
    # 30-minute aggregate bucket, so it contributes nothing.
    steps = daily_series(db, user, "steps", d29, d29)[0]
    assert steps.value == 2000
    # Weight: last of day.
    assert daily_series(db, user, "weight_kg", d29, d29)[0].value == 80.6
    # HR mean.
    assert daily_series(db, user, "heart_rate_bpm", d29, d29)[0].value == 65.0
    # 03:30 meal belongs to 2026-03-28 (04:00 boundary), so 3/29 has exactly
    # one confirmed meal with kcal 700 and 3/28 has 300.
    assert daily_series(db, user, "kcal_in", d29, d29)[0].value == 700.0
    assert daily_series(db, user, "kcal_in", date(2026, 3, 28), date(2026, 3, 28))[0].value == 300.0
    assert daily_series(db, user, "kcal_in", date(2026, 3, 27), date(2026, 3, 27))[0].value is None
