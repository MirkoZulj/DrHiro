"""Period comparison (spec §3.2 compare.py)."""

from __future__ import annotations

from datetime import date

from scipy.stats import mannwhitneyu

from drhiro_analytics.prep import coverage, slice_range, to_frame, weekday_adjust, weekly_sum
from drhiro_analytics.thresholds import confidence_for
from drhiro_analytics.types import Finding, SeriesInput

from drhiro_analytics.changes import _finding_id


def compare(s: SeriesInput, thr: dict, period: tuple[date, date], baseline_start) -> Finding | None:
    from drhiro_analytics.catalogue import METRIC_CATALOGUE
    import pandas as pd

    e = METRIC_CATALOGUE[s.key]
    x = to_frame(s)
    if e[2]:
        x = weekday_adjust(x)
    if e[1] == "weekly":
        x = weekly_sum(x)
    period_len = (period[1] - period[0]).days + 1
    base_end = period[0] - pd.Timedelta(days=1)
    base_start = (base_end - pd.Timedelta(days=period_len - 1)).date()
    base_start = max(base_start, baseline_start)
    cur = slice_range(x, period[0], period[1])
    prev = slice_range(x, base_start, base_end.date())
    cov_c = coverage(x, period[0], period[1])
    cov_p = coverage(prev, base_start, base_end.date())
    if cov_c < thr["min_cov"] or cov_p < thr["min_cov"]:
        return None
    c_vals, p_vals = cur.dropna(), prev.dropna()
    if len(c_vals) < 2 or len(p_vals) < 2:
        return None
    m_c, m_p = float(c_vals.median()), float(p_vals.median())
    if abs(m_c - m_p) < thr["min_effect"]:
        return None
    p = float(mannwhitneyu(p_vals.values, c_vals.values, alternative="two-sided").pvalue)
    if p > thr["alpha"]:
        return None
    direction = "up" if m_c > m_p else "down"
    iqr_c = float(c_vals.quantile(0.75) - c_vals.quantile(0.25))
    iqr_p = float(p_vals.quantile(0.75) - p_vals.quantile(0.25))
    return Finding(
        id=_finding_id("period_delta", s.key, period[0], direction),
        kind="period_delta",
        metric=s.key,
        direction=direction,
        anchor_date=period[0],
        before=m_p,
        after=m_c,
        delta=m_c - m_p,
        unit=s.unit,
        stats={
            "p_value": p,
            "n_before": int(len(p_vals)), "n_after": int(len(c_vals)),
            "coverage_before": cov_p, "coverage_after": cov_c,
            "iqr_before": iqr_p, "iqr_after": iqr_c, "method": "mannwhitney_period",
            "caveats": [],
        },
        confidence=confidence_for(p, min(cov_c, cov_p)),
        text="",
    )
