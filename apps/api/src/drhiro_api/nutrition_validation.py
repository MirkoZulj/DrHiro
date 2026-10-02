"""Atwater-factor energy validation for imported nutrition data.

Online catalogs (USDA FoodData Central, DuckDuckGo scrapes) occasionally
publish an energy value that contradicts the food's own macros. The verified
case (2026-09-23 production data audit): USDA search returns TWO ``Energy``
rows per food — kcal (nutrientId 1008) and kJ (nutrientId 1062) — and a
name-keyed parser reads the kJ figure as kcal, inflating energy ~4.2x
("chicken breast, raw: 501 kcal" where the macros imply 113.6).

This module cross-checks a published kcal value against the energy implied
by its protein/carbohydrate/fat (and optional alcohol) macros using the
standard Atwater factors, so impossible values can be corrected — with a
visible marker, never silently — before they are stored.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Standard Atwater factors: kcal per gram.
ATWATER_PROTEIN = 4.0
ATWATER_CARBS = 4.0
ATWATER_FAT = 9.0
ATWATER_ALCOHOL = 7.0  # alcoholic drinks (beer, wine, spirits)

# A published value is impossible when it deviates from its own macros by
# more than max(ABS_TOLERANCE_KCAL, REL_TOLERANCE * atwater). This threshold
# cleanly separates the corrupt entries (300%+ off) from legitimate label
# rounding (the 2026-09-23 audit's good set all stayed under it).
ABS_TOLERANCE_KCAL = 25.0
REL_TOLERANCE = 0.30


@dataclass(frozen=True)
class EnergyCheck:
    """Outcome of an Atwater cross-check of one published energy value."""

    published_kcal: float | None
    atwater_kcal: float | None    # None when the check could not run
    corrected_kcal: float | None  # value to store: published unless corrected
    corrected: bool               # True: published value was impossible, replaced
    skipped: bool                 # True: no macros (or no value) to validate against


def _num(value) -> float | None:
    """Coerce a possibly-string nutrient value to float; None if not numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def atwater_kcal(protein_g, carbs_g, fat_g, alcohol_g=None) -> float:
    """Energy implied by macros via Atwater factors, per reference amount."""
    return (
        ATWATER_PROTEIN * (protein_g or 0.0)
        + ATWATER_CARBS * (carbs_g or 0.0)
        + ATWATER_FAT * (fat_g or 0.0)
        + ATWATER_ALCOHOL * (alcohol_g or 0.0)
    )


def validate_energy(published_kcal, protein_g, carbs_g, fat_g, alcohol_g=None) -> EnergyCheck:
    """Cross-check a published kcal value against the food's own macros.

    Skipped (cannot validate) when no macro carries a non-zero value or the
    published value itself is missing — an all-zero macro set is legitimate
    (e.g. water) and must not be "corrected".

    Corrected when |published - atwater| > max(25, 0.30 * atwater); the
    replacement is the Atwater value rounded to 1 decimal.
    """
    pub = _num(published_kcal)
    p, c, f, a = (_num(x) for x in (protein_g, carbs_g, fat_g, alcohol_g))
    if pub is None or not any((p, c, f, a)):
        return EnergyCheck(published_kcal=pub, atwater_kcal=None,
                           corrected_kcal=pub, corrected=False, skipped=True)
    atw = atwater_kcal(p, c, f, a)
    if abs(pub - atw) > max(ABS_TOLERANCE_KCAL, REL_TOLERANCE * atw):
        return EnergyCheck(published_kcal=pub, atwater_kcal=round(atw, 2),
                           corrected_kcal=round(atw, 1), corrected=True, skipped=False)
    return EnergyCheck(published_kcal=pub, atwater_kcal=round(atw, 2),
                       corrected_kcal=pub, corrected=False, skipped=False)


def validate_candidate_energy(c: dict) -> EnergyCheck:
    """validate_energy over an online candidate dict.

    Accepts the shared candidate shape used by the openclaw tools and the
    consumption service (kcal_per_100g / protein_g_per_100g / carbs_g_per_100g
    / fat_g_per_100g / alcohol_g_per_100g). Missing keys are treated as absent.
    """
    return validate_energy(
        c.get("kcal_per_100g"),
        c.get("protein_g_per_100g"),
        c.get("carbs_g_per_100g"),
        c.get("fat_g_per_100g"),
        c.get("alcohol_g_per_100g"),
    )
