"""Level change detection tests (spec §3.2 changes.py)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from helpers import make_points  # noqa: E402

from drhiro_analytics.changes import level_changes  # noqa: E402
from drhiro_analytics.thresholds import thresholds  # noqa: E402
from drhiro_analytics.types import SeriesInput  # noqa: E402

PERIOD = (date(2026, 4, 1), date(2026, 6, 29))


def test_detects_level_up():
    pts = make_points(date(2026, 1, 1), 180, 5000, noise=200, change_at=100, change_to=8000)
    f = level_changes(SeriesInput("steps", "steps", pts), thresholds("steps"), PERIOD)
    assert len(f) == 1
    assert f[0].direction == "up"
    assert abs((f[0].anchor_date - date(2026, 4, 11)).days) <= 5
    assert f[0].confidence in ("medium", "low", "high")
    assert f[0].stats["p_value"] <= thresholds("steps")["alpha"]


def test_no_change_no_finding():
    pts = make_points(date(2026, 1, 1), 180, 5000, noise=200)
    assert level_changes(SeriesInput("steps", "steps", pts), thresholds("steps"), PERIOD) == []


def test_small_effect_suppressed():
    # +600 steps is far below min_effect=1500 -> no finding even though real
    pts = make_points(date(2026, 1, 1), 180, 5000, noise=50, change_at=100, change_to=5600)
    assert level_changes(SeriesInput("steps", "steps", pts), thresholds("steps"), PERIOD) == []


def test_change_outside_period_not_reported():
    pts = make_points(date(2026, 1, 1), 180, 5000, noise=200, change_at=20, change_to=8000)
    f = level_changes(SeriesInput("steps", "steps", pts), thresholds("steps"), PERIOD)
    assert all(PERIOD[0] <= fi.anchor_date <= PERIOD[1] for fi in f)


def test_finding_ids_stable():
    pts = make_points(date(2026, 1, 1), 180, 5000, noise=200, change_at=100, change_to=8000)
    a = level_changes(SeriesInput("steps", "steps", pts), thresholds("steps"), PERIOD)
    b = level_changes(SeriesInput("steps", "steps", pts), thresholds("steps"), PERIOD)
    assert [x.id for x in a] == [x.id for x in b]
