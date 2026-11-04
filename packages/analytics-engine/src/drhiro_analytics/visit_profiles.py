"""Visit metric profiles (0.10.x wave 3, spec §3.8).

Versioned and reviewed like thresholds. Final metric set for a visit run =
profile ∪ include_metrics − exclude_metrics, restricted to the catalogue.
"""

from __future__ import annotations

VISIT_PROFILES_VERSION = "visit-1.0"

VISIT_PROFILES: dict[str, list[str]] = {
    "gp": ["weight_kg", "bp_sys", "bp_dia", "bp_pulse", "sleep_duration_min", "steps",
           "kcal_in", "liquids_total_ml", "alcohol_ml", "resting_hr"],
    "cardiology": ["bp_sys", "bp_dia", "bp_pulse", "resting_hr", "heart_rate_avg", "spo2_avg",
                   "weight_kg", "steps", "active_minutes", "alcohol_ml", "liquids_total_ml",
                   "sleep_duration_min"],
    "endocrinology": ["weight_kg", "kcal_in", "carbs_g", "protein_g", "fat_g", "steps",
                      "active_kcal", "sleep_duration_min"],
    "gastroenterology": ["kcal_in", "fat_g", "carbs_g", "protein_g", "liquids_total_ml",
                         "alcohol_ml", "weight_kg", "meals_confirmed"],
    "nephrology": ["bp_sys", "bp_dia", "liquids_total_ml", "water_ml", "protein_g",
                   "weight_kg", "alcohol_ml"],
    "sleep": ["sleep_duration_min", "sleep_midpoint_min", "resting_hr", "spo2_avg",
              "stress_avg", "alcohol_ml", "steps"],
    "nutrition": ["kcal_in", "protein_g", "carbs_g", "fat_g", "liquids_total_ml",
                  "alcohol_ml", "weight_kg", "activity_kcal_logged"],
    "sports_medicine": ["steps", "active_minutes", "active_kcal", "activity_kcal_logged",
                        "resting_hr", "heart_rate_avg", "sleep_duration_min", "weight_kg"],
    "psychiatry": ["sleep_duration_min", "sleep_midpoint_min", "stress_avg", "steps",
                   "alcohol_ml", "kcal_in"],
    "other": [],  # empty -> all catalogue keys
}

PROVIDER_TYPES = tuple(VISIT_PROFILES.keys())


def resolve_visit_metrics(provider_type: str, include: list[str] | None = None,
                          exclude: list[str] | None = None,
                          catalogue: dict | None = None) -> list[str]:
    """Deterministic selection: profile ∪ include − exclude, catalogue-restricted,
    catalogue order preserved."""
    from .catalogue import METRIC_CATALOGUE

    catalogue = catalogue or METRIC_CATALOGUE
    if provider_type not in VISIT_PROFILES:
        raise ValueError(f"unknown provider_type: {provider_type}")
    base = set(VISIT_PROFILES[provider_type]) if VISIT_PROFILES[provider_type] else set(catalogue.keys())
    keys = base | set(include or [])
    keys -= set(exclude or [])
    return [k for k in catalogue if k in keys]
