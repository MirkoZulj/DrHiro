"""Metric catalogue (spec §3.2 catalogue.py).

key: (unit, kind, weekday_adjust, group)
kind: level | slow | weekly (alcohol analysed on ISO-week sums)
"""

METRIC_CATALOGUE = {
    "sleep_duration_min": ("min", "level", False, "sleep"),
    "sleep_midpoint_min": ("min", "level", False, "sleep"),
    "steps":              ("steps", "level", True,  "activity"),
    "active_minutes":     ("min", "level", True,  "activity"),
    "active_kcal":        ("kcal", "level", True, "activity"),
    "activity_kcal_logged": ("kcal", "level", True, "activity"),
    "weight_kg":          ("kg", "slow", False, "body"),
    "bp_sys":             ("mmHg", "level", False, "cardio"),
    "bp_dia":             ("mmHg", "level", False, "cardio"),
    "bp_pulse":           ("bpm", "level", False, "cardio"),
    "resting_hr":         ("bpm", "level", False, "cardio"),
    "heart_rate_avg":     ("bpm", "level", False, "cardio"),
    "spo2_avg":           ("%", "level", False, "cardio"),
    "stress_avg":         ("score", "level", False, "recovery"),
    "kcal_in":            ("kcal", "level", True, "nutrition"),
    "protein_g":          ("g", "level", True, "nutrition"),
    "carbs_g":            ("g", "level", True, "nutrition"),
    "fat_g":              ("g", "level", True, "nutrition"),
    "liquids_total_ml":   ("ml", "level", True, "liquids"),
    "water_ml":           ("ml", "level", True, "liquids"),
    "alcohol_ml":         ("ml", "weekly", False, "liquids"),
}


def entry(key: str):
    return METRIC_CATALOGUE.get(key)


def unit(key: str) -> str:
    e = METRIC_CATALOGUE.get(key)
    return e[0] if e else ""
