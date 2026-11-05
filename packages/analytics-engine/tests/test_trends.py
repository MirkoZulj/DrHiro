"""Theil–Sen trend tests (spec §3.2 trends.py)."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from helpers import make_points  # noqa: E402

from drhiro_analytics.thresholds import thresholds  # noqa: E402
from drhiro_analytics.trends import trend_finding  # noqa: E402
from drhiro_analytics.types import SeriesInput  # noqa: E402

PERIOD = (date(2026, 6, 1), date(2026, 8, 30))


def test_weight_rise_detected():
    pts = []
    for i in range(180):
        d = date(2026, 3, 4) + timedelta(days=i)
        pts.append((d, 80.0 + i * 0.02))  # +0.6 kg / 30 days
    f = trend_finding(SeriesInput("weight_kg", "kg", pts), thresholds("weight_kg"), PERIOD)
    assert f is not None
    assert f.direction == "up"
    assert abs(f.stats["slope_per_30d"] - 0.6) < 0.05
    assert f.stats["method"] == "theilsen"


def test_flat_weight_no_finding():
    pts = make_points(date(2026, 3, 4), 180, 80.0, noise=0.2)
    assert trend_finding(SeriesInput("weight_kg", "kg", pts), thresholds("weight_kg"), PERIOD) is None


def test_short_history_no_finding():
    pts = []
    for i in range(40):
        d = date(2026, 7, 1) + timedelta(days=i)
        pts.append((d, 80.0 + i * 0.05))
    assert trend_finding(SeriesInput("weight_kg", "kg", pts), thresholds("weight_kg"), PERIOD) is None
