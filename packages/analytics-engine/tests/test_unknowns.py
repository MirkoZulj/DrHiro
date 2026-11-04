"""Unknowns tests (spec §3.3)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from helpers import make_points  # noqa: E402

from drhiro_analytics.thresholds import thresholds  # noqa: E402
from drhiro_analytics.unknowns import build, build_context_unknowns  # noqa: E402
from drhiro_analytics.types import SeriesInput  # noqa: E402

PERIOD = (date(2026, 4, 1), date(2026, 4, 30))


def test_not_tracked():
    pts = [(date(2026, 4, 1) + __import__("datetime").timedelta(days=i), None) for i in range(60)]
    u = build(SeriesInput("resting_hr", "bpm", pts), PERIOD, date(2026, 3, 1), {})
    assert any(x.code == "not_tracked" for x in u)


def test_gap_unknown():
    pts = make_points(date(2026, 3, 1), 90, 80.0, noise=0.2, gap=(45, 66))
    u = build(SeriesInput("weight_kg", "kg", pts), (date(2026, 4, 1), date(2026, 4, 29)), date(2026, 3, 1), {})
    assert any(x.code == "gap" and x.detail["days"] >= 7 for x in u)


def test_low_coverage():
    pts = make_points(date(2026, 3, 1), 90, 80.0, noise=0.2, gap=(0, 75))
    u = build(SeriesInput("weight_kg", "kg", pts), PERIOD, date(2026, 3, 1), {})
    assert any(x.code == "low_coverage" for x in u)


def test_baseline_missing():
    pts = make_points(date(2026, 4, 1), 60, 80.0, noise=0.2)
    u = build(SeriesInput("weight_kg", "kg", pts), PERIOD, date(2026, 3, 1), {})
    assert any(x.code == "baseline_missing" for x in u)


def test_context_unknowns_and_banned_words():
    u = build_context_unknowns({"drafts_pending": 4, "doses_not_logged": ["Vitamin D3"]})
    codes = {x.code for x in u}
    assert "drafts_pending" in codes and "doses_not_logged" in codes
    assert all("0%" not in x.text for x in u if x.code == "doses_not_logged")
    assert any("unknown (not zero)" in x.text for x in u if x.code == "doses_not_logged")
