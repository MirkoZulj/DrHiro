"""False-positive-rate test (spec §3.2): flat noisy series must produce ~no
findings across many seeds; real shifts must be caught. Deterministic seeds."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from helpers import make_points  # noqa: E402

from drhiro_analytics.changes import level_changes  # noqa: E402
from drhiro_analytics.run import analyse  # noqa: E402
from drhiro_analytics.thresholds import thresholds  # noqa: E402
from drhiro_analytics.types import AnalysisInputs, SeriesInput  # noqa: E402

PERIOD = (date(2026, 4, 1), date(2026, 6, 29))


def test_flat_series_false_positive_rate():
    fp = 0
    trials = 30
    for seed in range(trials):
        pts = make_points(date(2026, 1, 1), 180, 5000, noise=300, seed=seed)
        f = level_changes(SeriesInput("steps", "steps", pts), thresholds("steps"), PERIOD)
        fp += len(f)
    # allow at most one false positive across 30 flat series
    assert fp <= 1, f"{fp} false positives in {trials} flat series"


def test_real_changes_are_caught():
    caught = 0
    trials = 10
    for seed in range(trials):
        pts = make_points(date(2026, 1, 1), 180, 5000, noise=300, seed=seed,
                          change_at=100, change_to=8500)
        f = level_changes(SeriesInput("steps", "steps", pts), thresholds("steps"), PERIOD)
        caught += 1 if f else 0
    assert caught >= 9, f"only {caught}/{trials} real changes detected"


def test_full_analysis_deterministic():
    pts = make_points(date(2026, 1, 1), 180, 5000, noise=300, seed=3, change_at=100, change_to=8500)
    inp = AnalysisInputs(period_start=PERIOD[0], period_end=PERIOD[1], baseline_start=date(2026, 1, 1),
                         series=[SeriesInput("steps", "steps", pts)], timeline=[], context={}, locale="en")
    a = analyse(inp)
    b = analyse(inp)
    assert [f.id for f in a.findings] == [f.id for f in b.findings]
    assert [f.text for f in a.findings] == [f.text for f in b.findings]
    # numeric guard: every finding text number is finite
    for f in a.findings:
        assert all(abs(float(x)) < 1e9 for x in _numbers(f.text))


def _numbers(text: str):
    import re
    for m in re.findall(r"\d+(?:[.,]\d+)?", text):
        try:
            yield float(m.replace(",", "."))
        except ValueError:
            pass
