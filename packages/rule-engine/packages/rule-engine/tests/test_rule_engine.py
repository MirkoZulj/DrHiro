"""Unit tests for the rule-engine package (no DB needed)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from drhiro_rules.calculations import (
    bp_average_by_context,
    coverage_score,
    rolling_average,
    sleep_duration_consistency,
    weight_trend,
)
from drhiro_rules.engine import all_rules, evaluate_rules, rules_for_jurisdiction


def _m(metric, value_json, start_at=None, id="r1"):
    return {
        "id": id,
        "metric_type": metric,
        "value_json": value_json,
        "start_at": start_at or datetime.now(timezone.utc),
    }


def test_rolling_average_steps():
    now = datetime.now(timezone.utc)
    ms = [
        _m("steps", {"count": 1000}, now - timedelta(days=1), "a"),
        _m("steps", {"count": 3000}, now - timedelta(days=3), "b"),
        _m("steps", {"count": 9000}, now - timedelta(days=10), "c"),  # outside 7d
    ]
    avg = rolling_average(ms, "steps", 7, now=now)
    assert avg == pytest.approx(2000.0)


def test_weight_trend_stable():
    now = datetime.now(timezone.utc)
    ms = [
        _m("weight", {"weight_kg": 80.0}, now - timedelta(days=20 - i * 2), f"w{i}")
        for i in range(10)
    ]
    trend = weight_trend(ms, 30)
    assert trend is not None
    assert trend["direction"] == "stable"


def test_weight_trend_down():
    now = datetime.now(timezone.utc)
    ms = []
    for i in range(10):
        ms.append(_m("weight", {"weight_kg": 85.0 - i * 0.4}, now - timedelta(days=20 - i * 2), f"w{i}"))
    trend = weight_trend(ms, 30)
    assert trend["direction"] == "down"


def test_bp_average_by_context():
    ms = [
        _m("blood_pressure", {"systolic_mmhg": 120, "diastolic_mmhg": 80, "pulse_bpm": 70, "body_position": "sitting"}),
        _m("blood_pressure", {"systolic_mmhg": 130, "diastolic_mmhg": 85, "pulse_bpm": 72, "body_position": "sitting"}),
        _m("blood_pressure", {"systolic_mmhg": 110, "diastolic_mmhg": 70, "body_position": "standing"}),
    ]
    sitting = bp_average_by_context(ms, "sitting")
    assert sitting["avg_systolic"] == 125
    assert sitting["avg_diastolic"] == round((80 + 85) / 2)  # Python banker's rounding


def test_coverage_missing_is_not_zero():
    ms = [_m("steps", {"count": 5000}, datetime.now(timezone.utc) - timedelta(days=1))]
    cov = coverage_score(ms, 7)
    assert cov["days_with_data"] == 1
    assert cov["expected_days"] == 7
    assert cov["score"] == pytest.approx(round(1 / 7, 2))


# ---------------------------------------------------------------------------
# coverage_score: local-calendar-day window + clamp
# ---------------------------------------------------------------------------

def _step_days(base_utc: datetime, local_dates: list[date], records_per_day: int = 5,
               tz_name: str = "UTC") -> list[dict]:
    """Build *records_per_day* step measurements for each local date.

    Each record is placed at local noon (12:00) so it unambiguously maps to
    the intended local calendar day regardless of the user's UTC offset.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    ms: list[dict] = []
    for d in local_dates:
        local_noon = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=tz)
        utc_noon = local_noon.astimezone(timezone.utc)
        for _ in range(records_per_day):
            ms.append(_m("steps", {"count": 8000}, utc_noon))
    return ms


def test_coverage_local_tz_boundary_fragment_excluded():
    """UTC+2 user whose data includes a boundary-day fragment.

    With a 7-day window ending 2026-08-24, records on 2026-08-17 (an
    evening sliver) must NOT inflate the score.
    """
    from zoneinfo import ZoneInfo
    tz_name = "Europe/Zagreb"
    tz = ZoneInfo(tz_name)
    now_local = datetime(2026, 8, 24, 17, 0, 0, tzinfo=tz)
    now_utc = now_local.astimezone(timezone.utc)

    # 7 full days + a fragment on day -7
    full_days = [date(2026, 8, d) for d in range(18, 25)]  # 18..24 inclusive
    fragment_day = date(2026, 8, 17)
    ms = _step_days(now_utc, full_days + [fragment_day], records_per_day=4, tz_name=tz_name)

    cov = coverage_score(ms, 7, now=now_utc, timezone_name=tz_name)
    assert cov["days_with_data"] <= 7
    assert cov["score"] <= 1.0


def test_coverage_live_scenario_impossible_ratio_fixed():
    """Exact live scenario: 8 calendar dates touched, one a 4-record fragment.

    Before the fix this yielded days_with_data=8, score=1.14.  After the
    fix it must report at most 7 days and score <= 1.0.
    """
    from zoneinfo import ZoneInfo
    tz_name = "Europe/Zagreb"
    tz = ZoneInfo(tz_name)
    now_local = datetime(2026, 8, 24, 17, 0, 0, tzinfo=tz)
    now_utc = now_local.astimezone(timezone.utc)

    records_per_day = {
        date(2026, 8, 17): 4,   # evening fragment — should be excluded
        date(2026, 8, 18): 162,
        date(2026, 8, 19): 41,
        date(2026, 8, 20): 31,
        date(2026, 8, 21): 43,
        date(2026, 8, 22): 166,
        date(2026, 8, 23): 4,
        date(2026, 8, 24): 160,
    }
    ms: list[dict] = []
    for d, count in records_per_day.items():
        ms.extend(_step_days(now_utc, [d], records_per_day=count, tz_name=tz_name))

    cov = coverage_score(ms, 7, now=now_utc, timezone_name=tz_name)
    assert cov["days_with_data"] <= 7
    assert cov["score"] <= 1.0


def test_coverage_full_week_score_one():
    """A genuine full week (data on all 7 complete local days) → score 1.0."""
    from zoneinfo import ZoneInfo
    tz_name = "Europe/Zagreb"
    tz = ZoneInfo(tz_name)
    now_local = datetime(2026, 8, 24, 17, 0, 0, tzinfo=tz)
    now_utc = now_local.astimezone(timezone.utc)

    full_days = [date(2026, 8, d) for d in range(18, 25)]
    ms = _step_days(now_utc, full_days, records_per_day=10, tz_name=tz_name)

    cov = coverage_score(ms, 7, now=now_utc, timezone_name=tz_name)
    assert cov["days_with_data"] == 7
    assert cov["score"] == 1.0


def test_coverage_partial_week_not_pinned():
    """3 of 7 days → score ≈ 0.43, proving the fix does not pin to 1.0."""
    from zoneinfo import ZoneInfo
    tz_name = "Europe/Zagreb"
    tz = ZoneInfo(tz_name)
    now_local = datetime(2026, 8, 24, 17, 0, 0, tzinfo=tz)
    now_utc = now_local.astimezone(timezone.utc)

    partial_days = [date(2026, 8, 20), date(2026, 8, 22), date(2026, 8, 24)]
    ms = _step_days(now_utc, partial_days, records_per_day=5, tz_name=tz_name)

    cov = coverage_score(ms, 7, now=now_utc, timezone_name=tz_name)
    assert cov["days_with_data"] == 3
    assert cov["score"] == pytest.approx(round(3 / 7, 2))


def test_coverage_activity_buckets_to_local_date():
    """An activity at 00:30 local (22:30 UTC previous day) → LOCAL date."""
    from zoneinfo import ZoneInfo
    tz_name = "Europe/Zagreb"
    tz = ZoneInfo(tz_name)
    now_local = datetime(2026, 8, 24, 17, 0, 0, tzinfo=tz)
    now_utc = now_local.astimezone(timezone.utc)

    # 00:30 Zagreb on 2026-08-19 = 2026-08-18 22:30 UTC
    local_time = datetime(2026, 8, 19, 0, 30, 0, tzinfo=tz)
    utc_time = local_time.astimezone(timezone.utc)
    assert utc_time.date() == date(2026, 8, 18)  # UTC date is the *previous* day

    ms = [_m("steps", {"count": 5000}, utc_time)]
    cov = coverage_score(ms, 7, now=now_utc, timezone_name=tz_name)

    # The record falls on local 2026-08-19, which is within the 7-day window
    # (2026-08-18..2026-08-24).  It should count as 1 day.
    assert cov["days_with_data"] == 1
    assert cov["expected_days"] == 7


def test_sleep_consistency():
    ms = [
        _m("sleep", {"duration_min": 420}, datetime.now(timezone.utc) - timedelta(days=1)),
        _m("sleep", {"duration_min": 440}, datetime.now(timezone.utc) - timedelta(days=2)),
        _m("sleep", {"duration_min": 430}, datetime.now(timezone.utc) - timedelta(days=3)),
    ]
    result = sleep_duration_consistency(ms, 7)
    assert result["consistent"] is True


def test_rules_registered():
    codes = {r.code for r in all_rules()}
    assert "bp_extreme_single_reading" in codes
    assert "weight_extreme" in codes


def test_rules_jurisdiction():
    global_rules = rules_for_jurisdiction("global")
    eu_rules = rules_for_jurisdiction("eu")
    assert len(eu_rules) >= len(global_rules)  # global rules always apply


def test_evaluate_rules_extreme_bp():
    now = datetime.now(timezone.utc)
    ms = [_m("blood_pressure", {"systolic_mmhg": 185, "diastolic_mmhg": 122}, now, "bp1")]
    candidates = evaluate_rules(ms)
    codes = [c.rule_code for c in candidates]
    assert "bp_extreme_single_reading" in codes
    cand = next(c for c in candidates if c.rule_code == "bp_extreme_single_reading")
    assert cand.severity == "warning"
    assert cand.trigger_record_ids == ["bp1"]


def test_evaluate_rules_repeated_elevated():
    now = datetime.now(timezone.utc)
    ms = [
        _m("blood_pressure", {"systolic_mmhg": 140, "diastolic_mmhg": 90}, now - timedelta(days=1), "b1"),
        _m("blood_pressure", {"systolic_mmhg": 142, "diastolic_mmhg": 91}, now - timedelta(days=2), "b2"),
        _m("blood_pressure", {"systolic_mmhg": 138, "diastolic_mmhg": 89}, now - timedelta(days=3), "b3"),
    ]
    candidates = evaluate_rules(ms)
    codes = [c.rule_code for c in candidates]
    assert "bp_elevated_repeated" in codes
