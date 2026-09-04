"""Layer 1: deterministic calculations over measurements.

All functions are pure: (measurements) -> value. No I/O, no LLM.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from statistics import median

from drhiro_schema.metrics import MetricType


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def numeric_value(value_json: dict) -> float | None:
    """Extract the primary numeric value from a metric value_json."""
    if not value_json:
        return None
    for key in ("count", "weight_kg", "bpm", "percent", "duration_min", "distance_m", "kcal", "amount_ml"):
        if key in value_json and value_json[key] is not None:
            return float(value_json[key])
    if "systolic_mmhg" in value_json:
        return float(value_json["systolic_mmhg"])
    return None


def rolling_average(
    measurements: list[dict],
    metric: MetricType | str,
    window_days: int,
    now: datetime | None = None,
) -> float | None:
    """Mean of numeric values for a metric within the trailing window."""
    now = _as_utc(now) or datetime.now(timezone.utc)
    cutoff = now.timestamp() - window_days * 86400
    vals = [
        numeric_value(m["value_json"])
        for m in measurements
        if m["metric_type"] == metric
        and (_as_utc(m.get("start_at")) or _as_utc(m.get("recorded_at"))).timestamp() >= cutoff
    ]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def rolling_median(
    measurements: list[dict],
    metric: MetricType | str,
    window_days: int,
    now: datetime | None = None,
) -> float | None:
    now = _as_utc(now) or datetime.now(timezone.utc)
    cutoff = now.timestamp() - window_days * 86400
    vals = [
        numeric_value(m["value_json"])
        for m in measurements
        if m["metric_type"] == metric
        and (_as_utc(m.get("start_at")) or _as_utc(m.get("recorded_at"))).timestamp() >= cutoff
    ]
    vals = [v for v in vals if v is not None]
    return median(vals) if vals else None


def weight_trend(measurements: list[dict], window_days: int = 30) -> dict | None:
    """Robust weight trend: median of per-day medians, first vs last.

    Returns {"start": kg, "end": kg, "delta": kg, "direction": str}
    or None when there is not enough data.
    """
    per_day: dict[date, list[float]] = defaultdict(list)
    for m in measurements:
        if m["metric_type"] != MetricType.WEIGHT:
            continue
        dt = _as_utc(m.get("start_at")) or _as_utc(m.get("recorded_at"))
        if dt is None:
            continue
        v = numeric_value(m["value_json"])
        if v is None:
            continue
        per_day[dt.date()].append(v)

    if len(per_day) < 3:
        return None

    days = sorted(per_day)
    daily_medians = [median(per_day[d]) for d in days]
    # trim extremes: use median of first third vs last third
    n = len(days)
    third = max(1, n // 3)
    start = median(daily_medians[:third])
    end = median(daily_medians[-third:])
    delta = end - start
    direction = "stable" if abs(delta) < 0.5 else ("down" if delta < 0 else "up")
    return {
        "start_kg": round(start, 1),
        "end_kg": round(end, 1),
        "delta_kg": round(delta, 1),
        "direction": direction,
        "days": n,
    }


def bp_average_by_context(measurements: list[dict], context: str | None = None) -> dict | None:
    """Average BP by morning/evening and optional context (e.g. sitting)."""
    sys_vals, dia_vals, pulse_vals = [], [], []
    for m in measurements:
        if m["metric_type"] != MetricType.BLOOD_PRESSURE:
            continue
        v = m.get("value_json") or {}
        if context and v.get("body_position") != context:
            continue
        if "systolic_mmhg" in v:
            sys_vals.append(v["systolic_mmhg"])
            dia_vals.append(v["diastolic_mmhg"])
        if v.get("pulse_bpm"):
            pulse_vals.append(v["pulse_bpm"])
    if not sys_vals:
        return None
    return {
        "avg_systolic": round(sum(sys_vals) / len(sys_vals)),
        "avg_diastolic": round(sum(dia_vals) / len(dia_vals)),
        "avg_pulse": round(sum(pulse_vals) / len(pulse_vals)) if pulse_vals else None,
        "count": len(sys_vals),
    }


def coverage_score(
    measurements: list[dict],
    expected_days: int,
    now: datetime | None = None,
) -> dict:
    """Coverage/quality score for a metric over the trailing window."""
    now = _as_utc(now) or datetime.now(timezone.utc)
    cutoff = now.timestamp() - expected_days * 86400
    days_with_data = {
        (_as_utc(m.get("start_at")) or _as_utc(m.get("recorded_at"))).date()
        for m in measurements
        if (_as_utc(m.get("start_at")) or _as_utc(m.get("recorded_at"))).timestamp() >= cutoff
    }
    score = round(len(days_with_data) / expected_days, 2) if expected_days else 0.0
    return {
        "days_with_data": len(days_with_data),
        "expected_days": expected_days,
        "score": score,
    }


def sleep_duration_consistency(measurements: list[dict], window_days: int = 14) -> dict | None:
    """Median sleep duration and variance over the window (minutes)."""
    durations = []
    for m in measurements:
        if m["metric_type"] != MetricType.SLEEP:
            continue
        v = m.get("value_json") or {}
        if v.get("duration_min"):
            durations.append(v["duration_min"])
    if len(durations) < 3:
        return None
    med = median(durations)
    spread = max(durations) - min(durations)
    return {
        "median_min": int(med),
        "min_min": min(durations),
        "max_min": max(durations),
        "spread_min": spread,
        "consistent": spread <= 120,
    }
