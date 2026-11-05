"""Variability change points: rolling 14-day IQR (spec §3.2 variability.py).

Run on sleep_duration_min, sleep_midpoint_min, bp_sys. Same PELT detection,
min_effect × 0.75 (in the metric's unit).
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import ruptures
from scipy.stats import mannwhitneyu

from drhiro_analytics.catalogue import METRIC_CATALOGUE
from drhiro_analytics.prep import coverage, slice_range, to_frame, weekday_adjust
from drhiro_analytics.thresholds import confidence_for
from drhiro_analytics.types import Finding, SeriesInput

from drhiro_analytics.changes import _finding_id

VARIABILITY_METRICS = {"sleep_duration_min", "sleep_midpoint_min", "bp_sys"}
IQR_WINDOW = 14


def variability_changes(s: SeriesInput, thr: dict, period: tuple[date, date]) -> list[Finding]:
    if s.key not in VARIABILITY_METRICS:
        return []
    x = to_frame(s)
    if METRIC_CATALOGUE[s.key][2]:
        x = weekday_adjust(x)
    q75 = x.rolling(IQR_WINDOW, min_periods=8).quantile(0.75)
    q25 = x.rolling(IQR_WINDOW, min_periods=8).quantile(0.25)
    roll = q75 - q25
    sm = roll.dropna()
    if len(sm) < 2 * thr["min_seg"]:
        return []
    vals = sm.values.astype(float)
    mad = float(np.median(np.abs(vals - np.median(vals)))) or 1e-9
    bkps = ruptures.Pelt(model="l1", min_size=thr["min_seg"], jump=1).fit(vals).predict(
        pen=thr["pen"] * mad * float(np.log(len(vals)))
    )
    findings: list[Finding] = []
    min_effect = thr["min_effect"] * 0.75
    for i in bkps[:-1]:
        cp = sm.index[int(i)]
        if not (period[0] <= cp.date() <= period[1]):
            continue
        before = slice_range(roll, (cp - pd.Timedelta(days=thr["window"])).date(), (cp - timedelta(days=1)).date()).dropna()
        after = slice_range(roll, cp.date(), (cp + pd.Timedelta(days=thr["window"] - 1)).date()).dropna()
        if len(before) < thr["min_seg"] or len(after) < thr["min_seg"]:
            continue
        b, a = float(before.median()), float(after.median())
        if abs(a - b) < min_effect:
            continue
        p = float(mannwhitneyu(before.values, after.values, alternative="two-sided").pvalue)
        if p > thr["alpha"]:
            continue
        cov = coverage(x, period[0], period[1])
        direction = "up" if a > b else "down"
        findings.append(Finding(
            id=_finding_id("variability_change", s.key, cp.date(), direction),
            kind="variability_change",
            metric=s.key,
            direction=direction,
            anchor_date=cp.date(),
            before=b,
            after=a,
            delta=a - b,
            unit=s.unit,
            stats={
                "p_value": p, "n_before": int(len(before)), "n_after": int(len(after)),
                "iqr_before": b, "iqr_after": a, "coverage": cov,
                "method": "pelt_rolling_iqr", "caveats": [],
            },
            confidence=confidence_for(p, cov),
            text="",
        ))
    return findings
