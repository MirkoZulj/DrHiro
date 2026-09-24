"""Atwater-factor energy validation for online USDA imports.

Background (2026-09-23 production data audit): USDA FDC publishes entries
whose published energy wildly contradicts their own macros. The verified
live mechanism: USDA *search* returns TWO ``Energy`` rows per food — kcal
(nutrientId 1008) and kJ (nutrientId 1062) — and a name-keyed parser keeps
the kJ figure, inflating energy ~4.2x ("chicken breast raw: 501 kcal" where
the macros imply 113.6). These tests pin:

  * the helper: 5 audited corrupt entries are corrected, 3 known-good foods
    (incl. alcohol handling for beer) pass untouched, and macro-less foods
    are skipped entirely;
  * the persist path (_persist_food): a corrupt candidate is stored with the
    Atwater value plus a persisted audit marker, never the published value.

Run:  .venv/bin/python -m pytest tests/test_atwater_energy_validation.py -q
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))

from drhiro_api.nutrition_validation import (  # noqa: E402
    atwater_kcal,
    validate_candidate_energy,
    validate_energy,
)

# ---------------------------------------------------------------------------
# The 5 audited corrupt USDA entries (published kcal vs their own macros).
# The published figures are the kJ row of each food; the macros are the real
# USDA values. All must be flagged and replaced by the Atwater value.
# ---------------------------------------------------------------------------
CORRUPT_CASES = [
    # fdc 171077 — Chicken, broiler/fryers, breast, skinless, boneless, raw
    # published 501 (kJ), macros P22.5/C0/F2.62 -> 113.6
    pytest.param("171077", 501, 22.5, 0.0, 2.62, 113.58, id="chicken-raw-171077"),
    # fdc 171140 — same food, cooked, braised
    # published 659 (kJ), macros P32.06/C0/F3.24 -> 157.4
    pytest.param("171140", 659, 32.06, 0.0, 3.24, 157.4, id="chicken-braised-171140"),
    # fdc 171509 — same food, with added solution, raw
    # published 453 (kJ), macros P20.32/C0/F3.0 -> 108.3
    pytest.param("171509", 453, 20.32, 0.0, 3.0, 108.28, id="chicken-solution-171509"),
    # fdc 331960 — Foundation, same braised food
    # published 695 (kJ), macros P32.1/C0/F3.24 -> 157.6
    pytest.param("331960", 695, 32.1, 0.0, 3.24, 157.56, id="chicken-foundation-331960"),
    # fdc 172831 — Ruffed Grouse, breast, skinless, raw
    # published 467 (kJ), macros P25.94/C0/F0.88 -> 111.7
    pytest.param("172831", 467, 25.94, 0.0, 0.88, 111.68, id="grouse-raw-172831"),
]

# Known-good foods: label values within tolerance of their macros. None may
# be flagged. (Values per USDA / standard labels, per 100 g.)
GOOD_CASES = [
    # Beer, regular ~5% abv: 43 kcal, macros P0.46/C3.55/F0, alcohol 3.63 g
    # -> Atwater with alcohol 41.2 (within tolerance). Without alcohol
    # handling this would be falsely corrected to 16.0.
    pytest.param("beer-with-alcohol", 43, 0.46, 3.55, 0.0, 3.63, id="beer-43-alcohol"),
    # Egg, whole, raw: 143 kcal, macros P12.56/C0.72/F9.51 -> 143.7
    pytest.param("egg-whole-raw", 143, 12.56, 0.72, 9.51, None, id="egg-143"),
    # Peanuts, raw: 567 kcal, macros P25.8/C16.13/F49.24 -> 567.1
    pytest.param("peanuts-raw", 567, 25.8, 16.13, 49.24, None, id="peanuts-567"),
]


class TestAtwaterHelper:
    def test_atwater_factors(self):
        assert atwater_kcal(10, 10, 10) == pytest.approx(10 * 4 + 10 * 4 + 10 * 9)
        assert atwater_kcal(1, 0, 0, 1) == pytest.approx(4 + 7)

    @pytest.mark.parametrize("fdc,pub,p,c,f,expected", CORRUPT_CASES)
    def test_corrupt_usda_entries_are_corrected(self, fdc, pub, p, c, f, expected):
        check = validate_energy(pub, p, c, f)
        assert check.skipped is False
        assert check.corrected is True
        assert check.published_kcal == pub
        assert check.atwater_kcal == pytest.approx(expected, abs=0.05)
        assert check.corrected_kcal == pytest.approx(expected, abs=0.1)

    @pytest.mark.parametrize("name,pub,p,c,f,alcohol", GOOD_CASES)
    def test_known_good_foods_are_not_flagged(self, name, pub, p, c, f, alcohol):
        check = validate_energy(pub, p, c, f, alcohol)
        assert check.skipped is False
        assert check.corrected is False, f"{name} must not be flagged"
        assert check.corrected_kcal == pub

    def test_beer_without_alcohol_data_would_be_false_flagged(self):
        """Documents WHY alcohol must be carried: macro-only Atwater for beer
        is 16.0, far below the published 43 — the check would 'correct' a
        legitimate label value. Carrying alcohol fixes it."""
        check_no_alc = validate_energy(43, 0.46, 3.55, 0.0)
        assert check_no_alc.corrected is True  # would be wrong to correct
        check_with_alc = validate_energy(43, 0.46, 3.55, 0.0, 3.63)
        assert check_with_alc.corrected is False

    def test_no_macros_is_skipped(self):
        """A food with no (or all-zero) macros cannot be validated: skip."""
        for macros in ((None, None, None), (0, 0, 0), (None, 0, None)):
            check = validate_energy(300, *macros)
            assert check.skipped is True
            assert check.corrected is False
            assert check.corrected_kcal == 300  # published value untouched

    def test_missing_published_value_is_skipped(self):
        check = validate_energy(None, 10, 10, 10)
        assert check.skipped is True
        assert check.corrected is False
        assert check.corrected_kcal is None

    def test_tolerance_boundary(self):
        """max(25, 0.30 * atwater) is the dividing line."""
        # Atwater 100: 130 published (30 off) is corrected, 129 (29 off) is not.
        assert validate_energy(129, 100 / 4, 0, 0).corrected is False
        assert validate_energy(131, 100 / 4, 0, 0).corrected is True
        # Atwater 10: the absolute 25 floor governs, not 30% of 10.
        assert validate_energy(34, 2.5, 0, 0).corrected is False
        assert validate_energy(36, 2.5, 0, 0).corrected is True

    def test_string_values_are_coerced(self):
        """USDA/DDG payloads may arrive as strings; the helper must coerce."""
        check = validate_energy("501", "22.5", "0", "2.62")
        assert check.corrected is True
        assert check.corrected_kcal == pytest.approx(113.6, abs=0.1)

    def test_candidate_dict_shape(self):
        c = {"kcal_per_100g": 501, "protein_g_per_100g": 22.5,
             "carbs_g_per_100g": 0.0, "fat_g_per_100g": 2.62}
        check = validate_candidate_energy(c)
        assert check.corrected is True
        # DDG candidates carry None for absent nutrients — must not crash.
        ddg = {"kcal_per_100g": 500, "protein_g_per_100g": None,
               "carbs_g_per_100g": None, "fat_g_per_100g": None}
        assert validate_candidate_energy(ddg).skipped is True


# ---------------------------------------------------------------------------
# Persist-path gate: _persist_food must store the corrected value + marker.
# ---------------------------------------------------------------------------
@pytest.fixture()
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from drhiro_api.db import Base
    import drhiro_api.models  # noqa: F401  (registers tables on Base)

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    s = S()
    try:
        yield s
    finally:
        s.close()


def _seed(db):
    from drhiro_api.models import DataSource, Nutrient
    db.add(DataSource(source_key="drhiro_private", source_label="drHiro Private Catalog",
                      is_active=True))
    for code, label, unit in (("energy", "Energy", "kcal"), ("protein", "Protein", "g"),
                              ("carbs", "Carbohydrate", "g"), ("fat", "Fat", "g")):
        db.add(Nutrient(nutrient_code=code, nutrient_label=label, unit=unit,
                        category="macro"))
    db.commit()


def _stored_energy(db, food_id):
    from drhiro_api.models import FoodNutrient, Nutrient
    row = (
        db.query(FoodNutrient)
        .join(Nutrient, FoodNutrient.nutrient_id == Nutrient.id)
        .filter(FoodNutrient.food_id == food_id, Nutrient.nutrient_code == "energy")
        .first()
    )
    return row.amount_per_100g if row else None


class TestPersistFoodAtwaterGate:
    def test_corrupt_usda_candidate_stores_atwater_value_and_marker(self, db):
        from drhiro_api.routers.openclaw_tools import _persist_food
        from drhiro_api.models import AuditEvent
        _seed(db)
        cand = {
            "external_id": "171077",
            "display_name": "Chicken, broiler or fryers, breast, skinless, boneless, meat only, raw",
            "kcal_per_100g": 501,  # kJ row read as kcal (the audited bug)
            "protein_g_per_100g": 22.5,
            "carbs_g_per_100g": 0.0,
            "fat_g_per_100g": 2.62,
            "fiber_g_per_100g": 0.0,
            "sodium_mg_per_100g": 68,
            "source": "usda-online",
        }
        food = _persist_food(db, cand)
        db.commit()
        assert food is not None
        # The catalog holds the Atwater value, NOT the published 501.
        assert _stored_energy(db, food.id) == pytest.approx(113.6, abs=0.1)
        # The correction is IN PLACE: tool_search_food returns the same dict
        # to the agent, so the agent too sees the corrected value + marker.
        assert cand["kcal_per_100g"] == pytest.approx(113.6, abs=0.1)
        assert cand["energy_atwater_corrected"] is True
        # Provenance marker: an audit event records the correction.
        evt = db.query(AuditEvent).filter(
            AuditEvent.action == "foods.energy_atwater_corrected").first()
        assert evt is not None
        assert evt.resource_id == str(food.id)
        assert evt.metadata_json["external_id"] == "171077"
        assert evt.metadata_json["published_kcal_per_100g"] == 501
        assert evt.metadata_json["stored_kcal_per_100g"] == pytest.approx(113.6, abs=0.1)

    def test_good_candidate_stores_published_value_no_marker(self, db):
        from drhiro_api.routers.openclaw_tools import _persist_food
        from drhiro_api.models import AuditEvent
        _seed(db)
        cand = {
            "external_id": "171287",
            "display_name": "Egg, whole, raw, fresh",
            "kcal_per_100g": 143,
            "protein_g_per_100g": 12.56,
            "carbs_g_per_100g": 0.72,
            "fat_g_per_100g": 9.51,
            "fiber_g_per_100g": 0.0,
            "sodium_mg_per_100g": 142,
            "source": "usda-online",
        }
        food = _persist_food(db, cand)
        db.commit()
        assert _stored_energy(db, food.id) == 143
        assert db.query(AuditEvent).filter(
            AuditEvent.action == "foods.energy_atwater_corrected").count() == 0

    def test_existing_food_row_is_returned_unchanged(self, db):
        from drhiro_api.routers.openclaw_tools import _persist_food
        from drhiro_api.models import AuditEvent
        _seed(db)
        cand = {"external_id": "171077", "display_name": "Chicken breast",
                "kcal_per_100g": 501, "protein_g_per_100g": 22.5,
                "carbs_g_per_100g": 0.0, "fat_g_per_100g": 2.62}
        first = _persist_food(db, cand)
        db.commit()
        # Second persist of the same external_id returns the existing row.
        again = _persist_food(db, cand)
        assert again.id == first.id
        assert db.query(AuditEvent).count() == 1  # only the first correction


# ---------------------------------------------------------------------------
# Live-bug regression: _usda_search must read kcal (1008), not the kJ row.
# ---------------------------------------------------------------------------
class TestUsdaSearchEnergyParsing:
    def test_kj_row_does_not_clobber_kcal(self, monkeypatch):
        """The audited values were the kJ row (nutrientId 1062) winning over
        kcal (1008) in a name-keyed dict. Keying by nutrientId fixes it."""
        import drhiro_api.services.consumption as cons

        class _Resp:
            status_code = 200
            def json(self):
                # Shape returned by the real USDA search endpoint
                # (verified live 2026-09-23, fdc 171077).
                return {"foods": [{
                    "fdcId": 171077,
                    "description": "Chicken, broiler or fryers, breast, skinless, boneless, meat only, raw",
                    "foodNutrients": [
                        {"nutrientId": 1004, "nutrientName": "Total lipid (fat)", "unitName": "G", "value": 2.62},
                        {"nutrientId": 1008, "nutrientName": "Energy", "unitName": "KCAL", "value": 120},
                        {"nutrientId": 1062, "nutrientName": "Energy", "unitName": "kJ", "value": 501},
                        {"nutrientId": 1003, "nutrientName": "Protein", "unitName": "G", "value": 22.5},
                        {"nutrientId": 1005, "nutrientName": "Carbohydrate, by difference", "unitName": "G", "value": 0.0},
                        {"nutrientId": 1018, "nutrientName": "Alcohol, ethyl", "unitName": "G", "value": 0.0},
                    ],
                }]}

        def _fake_get(url, params=None, timeout=None):
            return _Resp()

        # httpx is imported INSIDE _usda_search, so patch the real module.
        monkeypatch.setattr("httpx.get", _fake_get)
        # not set in test env -> early return; set it for this test
        monkeypatch.setenv("USDA_API_KEY", "test-key")
        cons._USDA_CACHE.clear()
        out = cons._usda_search("chicken breast", limit=3)
        assert len(out) == 1
        cand = out[0]
        # kcal row wins, kJ row ignored, and the value is consistent with
        # the macros so no Atwater correction is needed.
        assert cand["kcal_per_100g"] == 120
        assert cand.get("energy_atwater_corrected") is not True
        assert cand["alcohol_g_per_100g"] == 0.0

    def test_genuinely_impossible_usda_value_is_atwater_corrected(self, monkeypatch):
        """Even with correct kcal parsing, a value that contradicts the
        macros (unit corruption, bad DDG scrape) is replaced and marked."""
        import drhiro_api.services.consumption as cons

        class _Resp:
            status_code = 200
            def json(self):
                return {"foods": [{
                    "fdcId": 999999,
                    "description": "Mystery meat",
                    "foodNutrients": [
                        {"nutrientId": 1008, "nutrientName": "Energy", "unitName": "KCAL", "value": 501},
                        {"nutrientId": 1003, "nutrientName": "Protein", "unitName": "G", "value": 22.5},
                        {"nutrientId": 1005, "nutrientName": "Carbohydrate, by difference", "unitName": "G", "value": 0.0},
                        {"nutrientId": 1004, "nutrientName": "Total lipid (fat)", "unitName": "G", "value": 2.62},
                    ],
                }]}

        monkeypatch.setattr("httpx.get", lambda url, params=None, timeout=None: _Resp())
        monkeypatch.setenv("USDA_API_KEY", "test-key")
        cons._USDA_CACHE.clear()
        out = cons._usda_search("mystery meat", limit=3)
        assert out[0]["kcal_per_100g"] == pytest.approx(113.6, abs=0.1)
        assert out[0]["energy_atwater_corrected"] is True
        assert out[0]["confidence"] == 0.7


# ---------------------------------------------------------------------------
# Use-path gate: the composite-catalog fallback in meals.py must not log an
# impossible online value.
# ---------------------------------------------------------------------------
class TestMealsCompositeAtwaterGate:
    def test_impossible_online_food_energy_is_corrected(self, monkeypatch):
        from drhiro_api.routers import meals

        captured = {}

        class _FakeCatalog:
            def search(self, query, limit=1):
                from drhiro_nutrition.catalog import FoodItem
                return [FoodItem(
                    external_id="171077", source="usda", source_version="fdc-v1",
                    display_name="Chicken breast, raw",
                    kcal_per_100g=501, protein_g_per_100g=22.5,
                    carbs_g_per_100g=0.0, fat_g_per_100g=2.62,
                )]

            def by_id(self, external_id):
                return None

            def close(self):
                pass

        class _NullDB:
            def query(self, *a, **k):
                class _Q:
                    def all(self):
                        return []
                return _Q()

        class _User:
            id = "00000000-0000-0000-0000-000000000000"

        monkeypatch.setattr(meals, "_catalog", _FakeCatalog)
        # Local DB must find nothing so the composite path runs.
        from drhiro_api.food_search import ResolveResult
        monkeypatch.setattr(meals, "resolve_food", lambda db, q, limit=5, user_id=None: ResolveResult())

        class _Item:
            display_name = "chicken breast"
            food_catalog_item_id = None
            grams = 100.0
            quantity = 1.0

        payload, confidence, source = meals._lookup_nutrients(_NullDB(), _Item(), user=_User())
        # 100 g of the corrupt entry: must log 113.6 kcal, not 501.
        assert payload["kcal"] == pytest.approx(113.6, abs=0.1)
        assert "energy-atwater-corrected" in payload["sources"]
        assert confidence == 0.5  # demoted: corrected, not label-verified

    def test_good_online_food_untouched(self, monkeypatch):
        from drhiro_api.routers import meals

        class _FakeCatalog:
            def search(self, query, limit=1):
                from drhiro_nutrition.catalog import FoodItem
                return [FoodItem(
                    external_id="171287", source="usda", source_version="fdc-v1",
                    display_name="Egg, whole, raw, fresh",
                    kcal_per_100g=143, protein_g_per_100g=12.56,
                    carbs_g_per_100g=0.72, fat_g_per_100g=9.51,
                )]

            def by_id(self, external_id):
                return None

            def close(self):
                pass

        monkeypatch.setattr(meals, "_catalog", _FakeCatalog)
        from drhiro_api.food_search import ResolveResult
        monkeypatch.setattr(meals, "resolve_food", lambda db, q, limit=5, user_id=None: ResolveResult())

        class _NullDB:
            def query(self, *a, **k):
                class _Q:
                    def all(self):
                        return []
                return _Q()

        class _User:
            id = "00000000-0000-0000-0000-000000000000"

        class _Item:
            display_name = "egg"
            food_catalog_item_id = None
            grams = 100.0
            quantity = 1.0

        payload, confidence, source = meals._lookup_nutrients(_NullDB(), _Item(), user=_User())
        assert payload["kcal"] == pytest.approx(143.0, abs=0.1)
        assert "energy-atwater-corrected" not in payload["sources"]
        assert confidence == 0.7
