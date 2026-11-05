"""Level change points via PELT + Mann–Whitney (spec §3.2 changes.py)."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu

from drhiro_analytics.catalogue import METRIC_CATALOGUE
from drhiro_analytics.prep import coverage, slice_range, smooth, to_frame, weekday_adjust, weekly_sum
from drhiro_analytics.thresholds import TIMELINE_WINDOW_DAYS, confidence_for
from drhiro_analytics.types import Finding, SeriesInput


def _finding_id(kind: str, metric: str, anchor, direction: str | None) -> str:
    raw = f"{kind}|{metric}|{anchor}|{direction}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _raw_median(x: pd.Series, start, end) -> tuple[float | None, pd.Series]:
    seg = slice_range(x, start, end).dropna()
    if len(seg) == 0:
        return None, seg
    return float(seg.median()), seg


def refine_date(x: pd.Series, cp, radius: int, window: int):
    """Search ±radius days around cp for the split maximising |median(after)-median(before)| on raw data."""
    best_cp, best_score = cp, -1.0
    for off in range(-radius, radius + 1):
        c = cp + timedelta(days=off)
        b, _ = _raw_median(x, c - timedelta(days=window), c - timedelta(days=1))
        a, _ = _raw_median(x, c, c + timedelta(days=window - 1))
        if b is None or a is None:
            continue
        score = abs(a - b)
        if score > best_score:
            best_score, best_cp = score, c
    return best_cp


def level_changes(s: SeriesInput, thr: dict, period: tuple[date, date]) -> list[Finding]:
    e = METRIC_CATALOGUE[s.key]
    x = to_frame(s)
    if e[2]:  # weekday_adjust
        x = weekday_adjust(x)
    if e[1] == "weekly":
        x = weekly_sum(x)
        sm = x.dropna()
    else:
        sm = smooth(x).dropna()
    if len(sm) < 2 * thr["min_seg"]:
        return []
    vals = sm.values.astype(float)
    mad = float(np.median(np.abs(vals - np.median(vals)))) or 1e-9
    import ruptures

    bkps = ruptures.Pelt(model="l1", min_size=thr["min_seg"], jump=1).fit(vals).predict(
        pen=thr["pen"] * mad * float(np.log(len(vals)))
    )
    findings: list[Finding] = []
    window = thr["window"]
    for i in bkps[:-1]:
        cp = sm.index[int(i)]
        if not (period[0] <= cp.date() <= period[1]):
            continue
        before = slice_range(x, (cp - pd.Timedelta(days=window)).date(), (cp - pd.Timedelta(days=1)).date())
        after = slice_range(x, cp.date(), (cp + pd.Timedelta(days=window - 1)).date())
        cov_b = coverage(before, before.index.min().date() if len(before) else cp.date(), (cp - pd.Timedelta(days=1)).date())
        cov_a = coverage(after, cp.date(), (cp + pd.Timedelta(days=window - 1)).date())
        if min(cov_b, cov_a) < thr["min_cov"]:
            continue
        b = float(before.dropna().median()) if before.notna().sum() else None
        a = float(after.dropna().median()) if after.notna().sum() else None
        if b is None or a is None:
            continue
        if abs(a - b) < thr["min_effect"]:
            continue
        bd, ad = before.dropna(), after.dropna()
        if len(bd) < 2 or len(ad) < 2:
            continue
        p = float(mannwhitneyu(bd.values, ad.values, alternative="two-sided").pvalue)
        if p > thr["alpha"]:
            continue
        cp = refine_date(x, cp, radius=5, window=window)
        before = slice_range(x, (cp - pd.Timedelta(days=window)).date(), (cp - pd.Timedelta(days=1)).date())
        after = slice_range(x, cp.date(), (cp + pd.Timedelta(days=window - 1)).date())
        b = float(before.dropna().median())
        a = float(after.dropna().median())
        cov_a = coverage(after, cp.date(), (cp + pd.Timedelta(days=window - 1)).date())
        n_after = int(after.notna().sum())
        partial = n_after < window
        conf = confidence_for(p, min(cov_b, cov_a))
        if partial:
            conf = "low"
        direction = "up" if a > b else "down"
        findings.append(Finding(
            id=_finding_id("level_change", s.key, cp.date(), direction),
            kind="level_change",
            metric=s.key,
            direction=direction,
            anchor_date=cp.date(),
            before=b,
            after=a,
            delta=a - b,
            unit=s.unit,
            stats={
                "p_value": p, "n_before": int(before.notna().sum()), "n_after": n_after,
                "coverage_before": cov_b, "coverage_after": cov_a, "method": "pelt_l1_mwu",
                "partial": partial, "caveats": [],
            },
            confidence=conf,
            text="",  # rendered by render.py in run.py
        ))
    return findings


def related_events_for(finding: Finding, timeline) -> list[str]:
    if finding.anchor_date is None:
        return []
    out = []
    for ev in timeline:
        off = abs((ev.day - finding.anchor_date).days)
        if off <= TIMELINE_WINDOW_DAYS:
            out.append(ev.ref_id)
    return out
