"""Food catalog abstraction.

Every food lookup returns a normalized FoodItem with per-100g nutrient
values plus provenance (source + source version). The API and worker
never talk to OFF/USDA directly — they go through this interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class FoodItem:
    external_id: str
    source: str  # "open_food_facts" | "usda" | "drhiro_private"
    source_version: str
    display_name: str
    barcode: str | None = None
    # Nutrients per 100 g / 100 ml
    kcal_per_100g: float | None = None
    protein_g_per_100g: float | None = None
    carbs_g_per_100g: float | None = None
    fat_g_per_100g: float | None = None
    fiber_g_per_100g: float | None = None
    sodium_mg_per_100g: float | None = None
    serving_grams: float | None = None
    serving_unit: str | None = None
    is_liquid: bool = False


@dataclass(frozen=True)
class NutrientTotals:
    kcal: float | None = None
    protein_g: float | None = None
    carbs_g: float | None = None
    fat_g: float | None = None
    fiber_g: float | None = None
    sodium_mg: float | None = None
    confidence: float = 1.0
    sources: list[str] = field(default_factory=list)


class FoodCatalog(Protocol):
    def search(self, query: str, limit: int = 10) -> list[FoodItem]: ...
    def by_barcode(self, barcode: str) -> FoodItem | None: ...
    def by_id(self, external_id: str) -> FoodItem | None: ...


def scale_nutrients(item: FoodItem, grams: float) -> NutrientTotals:
    """Scale per-100g nutrients to a consumed amount in grams."""
    factor = grams / 100.0
    return NutrientTotals(
        kcal=item.kcal_per_100g * factor if item.kcal_per_100g is not None else None,
        protein_g=item.protein_g_per_100g * factor if item.protein_g_per_100g is not None else None,
        carbs_g=item.carbs_g_per_100g * factor if item.carbs_g_per_100g is not None else None,
        fat_g=item.fat_g_per_100g * factor if item.fat_g_per_100g is not None else None,
        fiber_g=item.fiber_g_per_100g * factor if item.fiber_g_per_100g is not None else None,
        sodium_mg=item.sodium_mg_per_100g * factor if item.sodium_mg_per_100g is not None else None,
        confidence=1.0,
        sources=[f"{item.source}:{item.source_version}"],
    )
