"""Unit tests for the rule-engine package (no DB needed)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
