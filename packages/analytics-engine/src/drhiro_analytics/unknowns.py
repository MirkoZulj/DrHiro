"""Unknowns builder (spec §3.3). Facts about what the data cannot tell."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from drhiro_analytics.catalogue import METRIC_CATALOGUE
from drhiro_analytics.prep import coverage, first_data_day, slice_range, to_frame, weekday_adjust, weekly_sum
from drhiro_analytics.thresholds import thresholds
from drhiro_analytics.types import SeriesInput, Unknown


def _t(code: str, metric: str | None, detail: dict, en: str) -> Unknown:
    return Unknown(code=code, metric=metric, detail=detail, text=en)


def build(s: SeriesInput, period: tuple[date, date], baseline_start: date, context: dict) -> list[Unknown]:
    from drhiro_analytics.catalogue import METRIC_CATALOGUE
    e = METRIC_CATALOGUE[s.key]
    thr = thresholds(s.key)
    x = to_frame(s)
    if e[2]:
        x = weekday_adjust(x)
    if e[1] == "weekly":
        x = weekly_sum(x)
    out = []
    label = s.key
    first = first_data_day(s)
    cov_period = coverage(x, period[0], period[1])

    if first is None:
        out.append(_t("not_tracked", s.key, {},
                      f"{label}: no data, so changes can't be assessed."))
        return out

    if cov_period < thr["min_cov"]:
        n_obs = int(slice_range(x, period[0], period[1]).notna().sum())
        out.append(_t("low_coverage", s.key, {"coverage": cov_period},
                      f"{label}: coverage {cov_period:.0%} in the period, below the {thr['min_cov']:.0%} needed."))

    # coverage_drop: coverage fell ≥ 30 pp vs baseline
    base_len = (period[0] - baseline_start).days
    if base_len > 0:
        cov_base = coverage(x, baseline_start, period[0] - timedelta(days=1))
        if cov_base > 0 and (cov_base - cov_period) >= 0.30:
            out.append(_t("coverage_drop", s.key, {"before": cov_base, "after": cov_period},
                          f"{label}: data coverage dropped (baseline {cov_base:.0%} -> {cov_period:.0%})."))

    # gap: longest run of missing days >= 7 inside the period
    cur_slice = slice_range(x, period[0], period[1])
    missing = cur_slice.isna()
    longest = 0
    run = 0
    gap_end = None
    for idx, m in missing.items():
        if m:
            run += 1
            if run > longest:
                longest, gap_end = run, idx
        else:
            run = 0
    if longest >= 7 and gap_end is not None:
        gap_start = (gap_end - pd.Timedelta(days=longest - 1)).date()
        out.append(_t("gap", s.key, {"days": longest},
                      f"No {label} readings {gap_start.isoformat()}–{gap_end.date().isoformat()} ({longest} days)."))

    # short_history
    if first and (period[1] - first).days < 2 * thr["min_seg"]:
        out.append(_t("short_history", s.key, {"days": (period[1] - first).days},
                      f"{label}: only {(period[1] - first).days} days of history, too short to detect changes."))

    # baseline_missing
    if base_len > 0 and coverage(x, baseline_start, period[0] - timedelta(days=1)) == 0.0:
        out.append(_t("baseline_missing", s.key, {},
                      f"No earlier data to compare with for {label}."))

    return out


def build_context_unknowns(context: dict) -> list[Unknown]:
    out = []
    if context.get("drafts_pending"):
        out.append(_t("drafts_pending", None, {"count": context["drafts_pending"]},
                      f"{context['drafts_pending']} meals are unconfirmed drafts and are not counted."))
    if context.get("unresolved_items"):
        out.append(_t("unresolved_items", None, {"count": context["unresolved_items"]},
                      f"{context['unresolved_items']} food items have no nutrition values; calorie totals may be low."))
    if context.get("liquids_without_category"):
        out.append(_t("liquids_without_category", None, {"count": context["liquids_without_category"]},
                      f"{context['liquids_without_category']} drinks have no category; treated as non-alcoholic."))
    if context.get("bp_context_missing"):
        out.append(_t("bp_context_missing", None, {"count": context["bp_context_missing"]},
                      f"{context['bp_context_missing']} BP readings have no time-of-day context."))
    for name in context.get("doses_not_logged", []):
        out.append(_t("doses_not_logged", None, {"item": name},
                      f"{name}: no doses logged, so adherence is unknown (not zero)."))
    if context.get("source_switch"):
        sw = context["source_switch"]
        out.append(_t("single_source_switch", sw.get("metric"), dict(sw),
                      f"{sw.get('metric')}: data source changed{(' to ' + sw['new']) if sw.get('new') else ''} on {sw.get('day')}; part of any shift may come from the device."))
    return out
