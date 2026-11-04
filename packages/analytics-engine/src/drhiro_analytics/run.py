"""analyse(inputs) -> AnalysisResult (spec §3.2 run.py). Deterministic."""

from __future__ import annotations

from dataclasses import asdict

from drhiro_analytics.changes import level_changes, related_events_for
from drhiro_analytics.compare import compare
from drhiro_analytics.cooccur import cooccur_findings
from drhiro_analytics.render import (
    assert_no_banned_words,
    fmt_num,
    render_finding,
    render_timeline_suffix,
)
from drhiro_analytics.thresholds import (
    CONFIDENCE_WEIGHT,
    MAX_FINDINGS,
    THRESHOLDS_V1,
    THRESHOLDS_VERSION,
    confidence_for,
    thresholds,
)
from drhiro_analytics.trends import trend_finding
from drhiro_analytics.types import AnalysisInputs, AnalysisResult, Finding, SeriesInput
from drhiro_analytics.unknowns import build, build_context_unknowns
from drhiro_analytics import ENGINE_VERSION
from drhiro_analytics.catalogue import METRIC_CATALOGUE


def _metric_summary(s: SeriesInput, period, baseline_start) -> dict:
    from drhiro_analytics.prep import coverage, first_data_day, slice_range, to_frame, weekly_sum
    e = METRIC_CATALOGUE[s.key]
    x = to_frame(s)
    if e[1] == "weekly":
        x = weekly_sum(x)
    import pandas as pd
    from datetime import timedelta as _td
    cur = slice_range(x, period[0], period[1]).dropna()
    base_start = max(period[0] - _td(days=(period[1] - period[0]).days + 1), baseline_start)
    prev = slice_range(x, base_start, period[0] - pd.Timedelta(days=1)).dropna()
    base_end = period[0] - pd.Timedelta(days=1)
    return {
        "key": s.key,
        "unit": s.unit,
        "coverage": coverage(x, period[0], period[1]),
        "median": float(cur.median()) if len(cur) else None,
        "iqr": float(cur.quantile(0.75) - cur.quantile(0.25)) if len(cur) else None,
        "min": float(cur.min()) if len(cur) else None,
        "max": float(cur.max()) if len(cur) else None,
        "n": int(len(cur)),
        "baseline_median": float(prev.median()) if len(prev) else None,
        "tracked": first_data_day(s) is not None,
    }


def analyse(inputs: AnalysisInputs) -> AnalysisResult:
    period = (inputs.period_start, inputs.period_end)
    all_findings: list[Finding] = []
    metrics = []
    level_metrics = set()

    for s in inputs.series:
        if s.key not in METRIC_CATALOGUE:
            continue
        thr = thresholds(s.key)
        metrics.append(_metric_summary(s, period, inputs.baseline_start))
        lc = level_changes(s, thr, period)
        for f in lc:
            f.related_events = related_events_for(f, inputs.timeline)
            f.text = render_finding(f, inputs.locale)
            suffix = render_timeline_suffix(
                [ev for ev in inputs.timeline if f.anchor_date and ev.ref_id in f.related_events],
                inputs.locale,
            )
            if suffix:
                f.text += suffix
            assert assert_no_banned_words(f.text), f"banned wording in finding: {f.text}"
        all_findings.extend(lc)
        if lc:
            level_metrics.add(s.key)

    for s in inputs.series:
        if s.key not in METRIC_CATALOGUE:
            continue
        thr = thresholds(s.key)
        if METRIC_CATALOGUE[s.key][1] == "slow" or s.key not in level_metrics:
            t = trend_finding(s, thr, period)
            if t:
                t.related_events = related_events_for(t, inputs.timeline)
                t.text = render_finding(t, inputs.locale)
                assert assert_no_banned_words(t.text), f"banned wording in finding: {t.text}"
                all_findings.append(t)

    for s in inputs.series:
        if s.key not in METRIC_CATALOGUE:
            continue
        thr = thresholds(s.key)
        vf = _variability(s, thr, period)
        for f in vf:
            f.related_events = related_events_for(f, inputs.timeline)
            f.text = render_finding(f, inputs.locale)
            assert assert_no_banned_words(f.text), f"banned wording in finding: {f.text}"
        all_findings.extend(vf)

    for s in inputs.series:
        if s.key not in METRIC_CATALOGUE or s.key in level_metrics:
            continue
        thr = thresholds(s.key)
        pc = compare(s, thr, period, inputs.baseline_start)
        if pc:
            pc.text = render_finding(pc, inputs.locale)
            all_findings.append(pc)

    cooc = cooccur_findings(all_findings, inputs.timeline, inputs.locale)
    for c in cooc:
        assert assert_no_banned_words(c.text)
    all_findings = all_findings + cooc

    for f in all_findings:
        f.rank = _rank(f)

    all_findings.sort(key=lambda f: (-f.rank, f.anchor_date or period[1]))
    all_findings = all_findings[:MAX_FINDINGS]

    unknowns = []
    for s in inputs.series:
        if s.key in METRIC_CATALOGUE:
            unknowns.extend(build(s, period, inputs.baseline_start, inputs.context))
    unknowns.extend(build_context_unknowns(inputs.context or {}))
    for u in unknowns:
        assert assert_no_banned_words(u.text), f"banned wording in unknown: {u.text}"

    return AnalysisResult(
        engine_version=ENGINE_VERSION,
        thresholds_version=THRESHOLDS_VERSION,
        period={"start": period[0], "end": period[1], "baseline_start": inputs.baseline_start},
        metrics=metrics,
        findings=all_findings,
        unknowns=unknowns,
        timeline=list(inputs.timeline),
    )


def _variability(s, thr, period):
    from drhiro_analytics.variability import variability_changes
    return variability_changes(s, thr, period)


def _rank(f: Finding) -> float:
    thr = thresholds(f.metric.split(",")[0])
    min_effect = max(thr["min_effect"], 1e-9)
    delta = abs(f.delta) if f.delta is not None else 0.0
    cov = f.stats.get("coverage_after", f.stats.get("coverage", 0.0)) or 0.0
    cov = min(max(float(cov), 0.0), 1.0)
    return CONFIDENCE_WEIGHT[f.confidence] * min(delta / min_effect, 4.0) * cov


def result_to_dict(result: AnalysisResult) -> dict:
    return asdict(result)
