"""Open Food Facts adapter (API v3, product lookup by barcode + search).

Docs: https://openfoodfacts.github.io/openfoodfacts-server/api/
"""

from __future__ import annotations

import httpx

from drhiro_nutrition.catalog import FoodItem

API_V3 = "https://world.openfoodfacts.org/api/v3"


class OpenFoodFactsCatalog:
    source = "open_food_facts"
    source_version = "api-v3"

    def __init__(self, timeout: float = 10.0, api_base: str = API_V3):
        self._client = httpx.Client(base_url=api_base, timeout=timeout)

    def close(self):
        self._client.close()

    def _to_item(self, product: dict) -> FoodItem | None:
        if not product:
            return None
        nutriments = product.get("nutriments") or {}
        g_per100 = lambda key: _num(nutriments.get(f"{key}_100g") or nutriments.get(key))
        return FoodItem(
            external_id=str(product.get("id") or product.get("code") or ""),
            source=self.source,
            source_version=self.source_version,
            display_name=product.get("product_name") or product.get("generic_name") or "Unknown product",
            barcode=product.get("code"),
            kcal_per_100g=_kcal(nutriments),
            protein_g_per_100g=g_per100("proteins"),
            carbs_g_per_100g=g_per100("carbohydrates"),
            fat_g_per_100g=g_per100("fat"),
            fiber_g_per_100g=g_per100("fiber"),
            sodium_mg_per_100g=g_per100("sodium"),
            serving_grams=_num(nutriments.get("serving_quantity")),
            serving_unit=nutriments.get("serving_quantity_unit"),
        )

    def by_barcode(self, barcode: str) -> FoodItem | None:
        resp = self._client.get(f"/product/{barcode}.json", params={"fields": "code,product_name,generic_name,nutriments"})
        if resp.status_code != 200:
            return None
        data = resp.json()
        product = (data.get("product") or {}) if data.get("status") else {}
        return self._to_item(product)

    def search(self, query: str, limit: int = 10) -> list[FoodItem]:
        resp = self._client.get(
            "/search",
            params={"q": query, "page_size": limit, "fields": "code,product_name,generic_name,nutriments"},
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        products = data.get("products") or []
        return [item for item in (self._to_item(p) for p in products) if item]

    def by_id(self, external_id: str) -> FoodItem | None:
        if external_id and external_id.isdigit():
            return self.by_barcode(external_id)
        return None


def _num(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _kcal(nutriments: dict) -> float | None:
    v = _num(nutriments.get("energy-kcal_100g") or nutriments.get("energy-kcal"))
    if v is not None:
        return v
    kj = _num(nutriments.get("energy_100g") or nutriments.get("energy"))
    if kj is not None:
        return round(kj / 4.184, 1)
    return None
