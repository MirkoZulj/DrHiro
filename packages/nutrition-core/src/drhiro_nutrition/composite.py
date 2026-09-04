"""Composite catalog: tries OFF, then USDA, then the private catalog.

The private catalog is a JSON-backed store of user-created foods and
corrected AI matches (persisted by the API layer).
"""

from __future__ import annotations

from drhiro_nutrition.catalog import FoodItem
from drhiro_nutrition.off import OpenFoodFactsCatalog
from drhiro_nutrition.usda import USDACatalog


class CompositeCatalog:
    def __init__(self, off: OpenFoodFactsCatalog | None = None, usda: USDACatalog | None = None, private: "PrivateCatalog | None" = None):
        self.off = off or OpenFoodFactsCatalog()
        self.usda = usda or USDACatalog()
        self.private = private or PrivateCatalog()

    def close(self):
        self.off.close()
        self.usda.close()

    def by_barcode(self, barcode: str) -> FoodItem | None:
        item = self.private.by_barcode(barcode)
        if item:
            return item
        item = self.off.by_barcode(barcode)
        if item:
            return item
        return self.usda.by_barcode(barcode)

    def search(self, query: str, limit: int = 10) -> list[FoodItem]:
        results = self.private.search(query, limit)
        if len(results) >= limit:
            return results[:limit]
        results += self.off.search(query, limit - len(results))
        if len(results) >= limit:
            return results[:limit]
        results += self.usda.search(query, limit - len(results))
        return results[:limit]

    def by_id(self, external_id: str) -> FoodItem | None:
        if external_id.startswith("private:"):
            return self.private.by_id(external_id.split(":", 1)[1])
        return self.off.by_id(external_id) or self.usda.by_id(external_id)


class PrivateCatalog:
    """JSON-file-backed store of user-created foods."""

    source = "drhiro_private"
    source_version = "1"

    def __init__(self, path: str | None = None):
        self.path = path
        self._items: dict[str, FoodItem] = {}
        self._barcodes: dict[str, FoodItem] = {}
        if path:
            self._load()

    def _load(self):
        import json, os
        if not self.path or not os.path.exists(self.path):
            return
        with open(self.path) as f:
            raw = json.load(f)
        for entry in raw:
            item = FoodItem(
                external_id=f"private:{entry['id']}",
                source=self.source,
                source_version=self.source_version,
                display_name=entry["display_name"],
                barcode=entry.get("barcode"),
                kcal_per_100g=entry.get("kcal_per_100g"),
                protein_g_per_100g=entry.get("protein_g_per_100g"),
                carbs_g_per_100g=entry.get("carbs_g_per_100g"),
                fat_g_per_100g=entry.get("fat_g_per_100g"),
                fiber_g_per_100g=entry.get("fiber_g_per_100g"),
                sodium_mg_per_100g=entry.get("sodium_mg_per_100g"),
            )
            self._items[item.external_id] = item
            if item.barcode:
                self._barcodes[item.barcode] = item

    def save(self, item: FoodItem) -> FoodItem:
        if not item.external_id.startswith("private:"):
            item = FoodItem(
                external_id=f"private:{len(self._items) + 1}",
                source=self.source,
                source_version=self.source_version,
                display_name=item.display_name,
                barcode=item.barcode,
                kcal_per_100g=item.kcal_per_100g,
                protein_g_per_100g=item.protein_g_per_100g,
                carbs_g_per_100g=item.carbs_g_per_100g,
                fat_g_per_100g=item.fat_g_per_100g,
                fiber_g_per_100g=item.fiber_g_per_100g,
                sodium_mg_per_100g=item.sodium_mg_per_100g,
            )
        self._items[item.external_id] = item
        if item.barcode:
            self._barcodes[item.barcode] = item
        if self.path:
            self._persist()
        return item

    def _persist(self):
        import json, os
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump([{"id": k.split(":", 1)[1], "display_name": v.display_name, "barcode": v.barcode,
                        "kcal_per_100g": v.kcal_per_100g, "protein_g_per_100g": v.protein_g_per_100g,
                        "carbs_g_per_100g": v.carbs_g_per_100g, "fat_g_per_100g": v.fat_g_per_100g,
                        "fiber_g_per_100g": v.fiber_g_per_100g, "sodium_mg_per_100g": v.sodium_mg_per_100g}
                       for k, v in self._items.items()], f, indent=2)

    def search(self, query: str, limit: int = 10) -> list[FoodItem]:
        q = query.lower()
        return [i for i in self._items.values() if q in i.display_name.lower()][:limit]

    def by_barcode(self, barcode: str) -> FoodItem | None:
        return self._barcodes.get(barcode)

    def by_id(self, external_id: str) -> FoodItem | None:
        return self._items.get(f"private:{external_id}")
