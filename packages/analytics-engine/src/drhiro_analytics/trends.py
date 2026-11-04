"""Gradual trends: Theil–Sen + Mann–Kendall (spec §3.2 trends.py).

For `slow` metrics (weight) and every metric with no level change found.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from scipy.stats import kendalltau, theilslopes

from drhiro_analytics.catalogue import METRIC_CATALOGUE
from drhiro_analytics.prep import coverage, to_frame, weekday_adjust
from drhiro_analytics.thresholds import confidence_for
from drhiro_analytics.types import Finding, SeriesInput

from drhiro_analytics.changes import _finding_id


def trend_finding(s: SeriesInput, thr: dict, period: tuple[date, date], series_x=None) -> Finding | None:
    x = series_x if series_x is not None else to_frame(s)
    if METRIC_CATALOGUE[s.key][2]:
        x = weekday_adjust(x)
    y = x.dropna()
    min_days = thr.get("trend_min_days", 60)
    if len(y) < min_days:
        return None
    # The trend window must reach into the period; a trend that ended long
    # before the period is not part of this report.
    if y.index.max().date() < period[0]:
        return None
    t = (y.index - y.index[0]).days.values.astype(float)
    res = theilslopes(y.values.astype(float), t)
    slope = float(res.slope)
    lo, hi = float(res.low_slope), float(res.high_slope)
    tau_p = float(kendalltau(t, y.values.astype(float)).pvalue)
    cov = coverage(x, period[0], period[1])
    if abs(slope * 30) < thr.get("trend_min_slope_30d", thr["min_effect"] / 2):
        return None
    if tau_p > thr["alpha"] or lo * hi <= 0:
        return None
    direction = "up" if slope > 0 else "down"
    anchor = y.index[0].date()
    return Finding(
        id=_finding_id("trend", s.key, anchor, direction),
        kind="trend",
        metric=s.key,
        direction=direction,
        anchor_date=anchor,
        before=None,
        after=None,
        delta=slope * 30,  # per 30 days
        unit=s.unit,
        stats={
            "p_value": tau_p, "slope_per_30d": slope * 30,
            "ci_low_per_30d": lo * 30, "ci_high_per_30d": hi * 30,
            "n": int(len(y)), "coverage": cov, "method": "theilsen",
            "days_with_data": int(len(y)), "caveats": [],
        },
        confidence=confidence_for(tau_p, cov),
        text="",  # rendered by render.py in run.py
    )
