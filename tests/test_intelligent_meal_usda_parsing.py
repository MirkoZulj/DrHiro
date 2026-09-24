"""Regression: intelligent-meal usda_search must key nutrients by nutrientId.

USDA FoodData Central search returns TWO ``Energy`` rows per food — kcal
(nutrientId 1008) and kJ (nutrientId 1062). The old name-keyed dict in
services/intelligent-meal/service.py kept whichever row came last (the kJ
one), inflating draft-meal calories ~4.2x. Keying by nutrientId fixes it —
the same fix already applied to consumption._usda_search and
nutrition-core (commit 5b0b302).

This file mirrors
tests/test_atwater_energy_validation.py::TestUsdaSearchEnergyParsing
(which covers consumption.py) for the standalone intelligent-meal service.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE_PATH = REPO_ROOT / "services" / "intelligent-meal" / "service.py"


def _import_service(monkeypatch):
    """Import the standalone intelligent-meal service module.

    The module builds a SQLAlchemy engine and a Redis client at import
    time; dummy URLs are fine because neither connects eagerly. It is
    loaded under a unique module name so it can never collide with
    another ``service`` module on sys.path.
    """
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_dummy",
    )
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    spec = importlib.util.spec_from_file_location(
        "intelligent_meal_service_usda_test", SERVICE_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestUsdaSearchEnergyParsing:
    def test_kj_row_does_not_clobber_kcal(self, monkeypatch):
        """The kJ row (nutrientId 1062) must never win over kcal (1008).

        Payload shape from the real USDA search endpoint (verified live
        2026-09-23, fdc 171077): the kJ row comes AFTER the kcal row, so a
        name-keyed dict ends up keeping 501 kJ instead of 120 kcal.
        """
        svc = _import_service(monkeypatch)

        class _Resp:
            status_code = 200

            def json(self):
                return {"foods": [{
                    "fdcId": 171077,
                    "description": "Chicken, broiler or fryers, breast, skinless, boneless, meat only, raw",
                    "foodNutrients": [
                        {"nutrientId": 1004, "nutrientName": "Total lipid (fat)", "unitName": "G", "value": 2.62},
                        {"nutrientId": 1008, "nutrientName": "Energy", "unitName": "KCAL", "value": 120},
                        {"nutrientId": 1062, "nutrientName": "Energy", "unitName": "kJ", "value": 501},
                        {"nutrientId": 1003, "nutrientName": "Protein", "unitName": "G", "value": 22.5},
                        {"nutrientId": 1005, "nutrientName": "Carbohydrate, by difference", "unitName": "G", "value": 0.0},
                        {"nutrientId": 1079, "nutrientName": "Fiber, total dietary", "unitName": "G", "value": 0.0},
                        {"nutrientId": 1093, "nutrientName": "Sodium, Na", "unitName": "MG", "value": 65},
                    ],
                }]}

        monkeypatch.setattr("httpx.get", lambda url, params=None, timeout=None: _Resp())
        svc._USDA_CACHE.clear()
        out = svc.usda_search("chicken breast", limit=3)
        assert len(out) == 1
        cand = out[0]
        # kcal row (1008) wins; the kJ row (1062 = 501) must never be used.
        assert cand["kcal_per_100g"] == 120
        # macros are read by their own nutrientIds, unchanged in meaning
        assert cand["protein_g_per_100g"] == 22.5
        assert cand["carbs_g_per_100g"] == 0.0
        assert cand["fat_g_per_100g"] == 2.62
        assert cand["fiber_g_per_100g"] == 0.0
        assert cand["sodium_mg_per_100g"] == 65
        assert cand["source"] == "USDA API"
        assert cand["confidence"] == 0.85

    def test_kj_row_first_also_does_not_clobber_kcal(self, monkeypatch):
        """Order-independence: even if the kJ row arrived first, the kcal
        row (1008) must still win — nutrientId keying does not care about
        row order, the name-keyed dict did not either guarantee this."""
        svc = _import_service(monkeypatch)

        class _Resp:
            status_code = 200

            def json(self):
                return {"foods": [{
                    "fdcId": 171077,
                    "description": "Chicken, broiler or fryers, breast, skinless, boneless, meat only, raw",
                    "foodNutrients": [
                        {"nutrientId": 1062, "nutrientName": "Energy", "unitName": "kJ", "value": 501},
                        {"nutrientId": 1008, "nutrientName": "Energy", "unitName": "KCAL", "value": 120},
                        {"nutrientId": 1003, "nutrientName": "Protein", "unitName": "G", "value": 22.5},
                    ],
                }]}

        monkeypatch.setattr("httpx.get", lambda url, params=None, timeout=None: _Resp())
        svc._USDA_CACHE.clear()
        out = svc.usda_search("chicken breast kJ first", limit=3)
        assert out[0]["kcal_per_100g"] == 120
