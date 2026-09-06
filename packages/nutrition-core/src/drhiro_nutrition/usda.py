"""USDA FoodData Central adapter.

Docs: https://fdc.nal.usda.gov/api-guide
Default rate limit: 1000 requests/hour/IP. API key optional for higher
limits; pass api_key to enable.
"""

from __future__ import annotations

import httpx

from drhiro_nutrition.catalog import FoodItem

API = "https://api.nal.usda.gov/fdc/v1"
NUTRIENT_IDS = {
    "energy": 1008,       # kcal
    "protein": 1003,      # g
    "carbs": 1005,        # g
    "fat": 1004,          # g
    "fiber": 1079,        # g
    "sodium": 1093,       # mg
}


class USDACatalog:
    source = "usda"
    source_version = "fdc-v1"

    def __init__(self, api_key: str | None = None, timeout: float = 10.0):
        self._api_key = api_key
        self._client = httpx.Client(base_url=API, timeout=timeout)

    def close(self):
        self._client.close()

    def _params(self, extra: dict | None = None) -> dict:
        p = {"api_key": self._api_key} if self._api_key else {}
        if extra:
            p.update(extra)
        return p

    def _parse(self, food: dict) -> FoodItem | None:
        if not food:
            return None
        nutrients = {}
        for n in food.get("foodNutrients") or []:
            nid = n.get("nutrient", {}).get("id") or n.get("nutrientId")
            amount = n.get("amount") or n.get("value")
            if nid and amount is not None:
                nutrients[nid] = amount
        return FoodItem(
            external_id=str(food.get("fdcId") or ""),
            source=self.source,
            source_version=self.source_version,
            display_name=food.get("description") or "Unknown food",
            kcal_per_100g=nutrients.get(NUTRIENT_IDS["energy"]),
            protein_g_per_100g=nutrients.get(NUTRIENT_IDS["protein"]),
            carbs_g_per_100g=nutrients.get(NUTRIENT_IDS["carbs"]),
            fat_g_per_100g=nutrients.get(NUTRIENT_IDS["fat"]),
            fiber_g_per_100g=nutrients.get(NUTRIENT_IDS["fiber"]),
            sodium_mg_per_100g=nutrients.get(NUTRIENT_IDS["sodium"]),
        )

    def search(self, query: str, limit: int = 10) -> list[FoodItem]:
        resp = self._client.post(
            "/foods/search",
            params=self._params(),
            json={"query": query, "pageSize": limit, "dataType": ["Foundation", "SR Legacy", "Branded"]},
        )
        if resp.status_code != 200:
            return []
        foods = resp.json().get("foods") or []
        return [item for item in (self._parse(f) for f in foods) if item]

    def by_barcode(self, barcode: str) -> FoodItem | None:
        # USDA branded foods support GTIN lookup via search with a gtinUpc filter.
        resp = self._client.post(
            "/foods/search",
            params=self._params(),
            json={"query": barcode, "pageSize": 1, "dataType": ["Branded"]},
        )
        if resp.status_code != 200:
            return None
        foods = resp.json().get("foods") or []
        return self._parse(foods[0]) if foods else None

    def by_id(self, external_id: str) -> FoodItem | None:
        resp = self._client.get(f"/food/{external_id}", params=self._params())
        if resp.status_code != 200:
            return None
        return self._parse(resp.json())
