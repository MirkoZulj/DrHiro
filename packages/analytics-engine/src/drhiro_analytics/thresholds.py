"""Versioned, reviewed thresholds (spec §3.2 thresholds.py).

Changing any number means bumping THRESHOLDS_VERSION, getting an entry in
docs/rule-governance.md, and owner review. The LLM never sees or edits these.
"""

THRESHOLDS_VERSION = "analytics-1.0"

THRESHOLDS_V1 = {
    "sleep_duration_min": dict(min_effect=30,  min_seg=14, window=28, min_cov=0.6, pen=3.0, alpha=0.01),
    "sleep_midpoint_min": dict(min_effect=40,  min_seg=14, window=28, min_cov=0.6, pen=3.0, alpha=0.01),
    "steps":              dict(min_effect=1500, min_seg=14, window=28, min_cov=0.6, pen=3.0, alpha=0.01),
    "active_minutes":     dict(min_effect=10,  min_seg=14, window=28, min_cov=0.6, pen=3.0, alpha=0.01),
    "active_kcal":        dict(min_effect=100, min_seg=14, window=28, min_cov=0.6, pen=3.0, alpha=0.01),
    "activity_kcal_logged": dict(min_effect=100, min_seg=14, window=28, min_cov=0.3, pen=3.0, alpha=0.01),
    "weight_kg":          dict(min_effect=1.0, min_seg=21, window=42, min_cov=0.3, pen=4.0, alpha=0.01,
                               trend_min_slope_30d=0.3, trend_min_days=60),
    "bp_sys":             dict(min_effect=6,   min_seg=14, window=28, min_cov=0.3, pen=3.0, alpha=0.01),
    "bp_dia":             dict(min_effect=4,   min_seg=14, window=28, min_cov=0.3, pen=3.0, alpha=0.01),
    "bp_pulse":           dict(min_effect=5,   min_seg=14, window=28, min_cov=0.3, pen=3.0, alpha=0.01),
    "resting_hr":         dict(min_effect=4,   min_seg=14, window=28, min_cov=0.6, pen=3.0, alpha=0.01),
    "heart_rate_avg":     dict(min_effect=5,   min_seg=14, window=28, min_cov=0.6, pen=3.0, alpha=0.01),
    "spo2_avg":           dict(min_effect=1.5, min_seg=14, window=28, min_cov=0.6, pen=3.0, alpha=0.01),
    "stress_avg":         dict(min_effect=8,   min_seg=14, window=28, min_cov=0.6, pen=3.0, alpha=0.01),
    "kcal_in":            dict(min_effect=250, min_seg=14, window=28, min_cov=0.5, pen=3.0, alpha=0.01),
    "protein_g":          dict(min_effect=20,  min_seg=14, window=28, min_cov=0.5, pen=3.0, alpha=0.01),
    "carbs_g":            dict(min_effect=40,  min_seg=14, window=28, min_cov=0.5, pen=3.0, alpha=0.01),
    "fat_g":              dict(min_effect=15,  min_seg=14, window=28, min_cov=0.5, pen=3.0, alpha=0.01),
    "liquids_total_ml":   dict(min_effect=400, min_seg=14, window=28, min_cov=0.5, pen=3.0, alpha=0.01),
    "water_ml":           dict(min_effect=300, min_seg=14, window=28, min_cov=0.5, pen=3.0, alpha=0.01),
    "alcohol_ml":         dict(min_effect=250, min_seg=3,  window=6,  min_cov=0.8, pen=3.0, alpha=0.05),  # units = ISO weeks
}

CONFIDENCE = dict(high=dict(p=0.001, cov=0.8), medium=dict(p=0.01, cov=0.6))  # else low
COOCCUR_WINDOW_DAYS = 7
TIMELINE_WINDOW_DAYS = 7
MAX_FINDINGS = 25

# D3 wording guard: these strings must never appear in a rendered finding/unknown.
BANNED_WORDS = [
    "because", "due to", "caused", "correlat", "diagnos",
    "zbog", "uzrok", "dijagnoz",
    "you should", "trebao bi", "you must", "moraš",
]

CONFIDENCE_WEIGHT = {"high": 3, "medium": 2, "low": 1}


def thresholds(key: str) -> dict:
    return THRESHOLDS_V1[key]


def confidence_for(p_value: float | None, min_coverage: float) -> str:
    cov = min_coverage
    if p_value is not None and p_value <= CONFIDENCE["high"]["p"] and cov >= CONFIDENCE["high"]["cov"]:
        return "high"
    if p_value is not None and p_value <= CONFIDENCE["medium"]["p"] and cov >= CONFIDENCE["medium"]["cov"]:
        return "medium"
    return "low"
