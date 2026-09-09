"""Stage 2 regression tests for B1 (nutrient resolution) and B8 (parser).

These tests reproduce the failures BEFORE the fix. Each test asserts the
expected correct behavior and will fail against the pre-fix code.

RED phase: run this file first to confirm failures.
GREEN phase: after implementing B1/B8, these must all pass.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))

from drhiro_api.models import (
    Base, User, Food, DataSource, Nutrient, FoodNutrient,
    Meal, MealItem, Measurement,
    ConsumptionOperation, ConsumptionItem, BeverageMeasurement,
)
from drhiro_api.services.consumption import (
    parse_consumption_text,
    confirm_consumption,
    write_consumption,
    _classify_beverage,
    _scale_nutrients,
    _sum_nutrients,
    NUTRIENT_KEYS,
    ParsedItem,
)
from drhiro_api.food_search import resolve_food, nutrient_map

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_DB_URL = os.environ.get(
    "DRHIRO_TEST_DB_URL",
    "postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_test",
)


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def tables(engine):
    Base.metadata.create_all(engine)
    yield


@pytest.fixture()
def db(engine, tables):
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM beverage_measurements"))
        conn.execute(text("DELETE FROM consumption_items"))
        conn.execute(text("DELETE FROM consumption_operations"))
        conn.execute(text("DELETE FROM meal_items"))
        conn.execute(text("DELETE FROM meals"))
        conn.execute(text("DELETE FROM measurements"))
        conn.execute(text("DELETE FROM food_nutrients"))
        conn.execute(text("DELETE FROM foods"))
        conn.execute(text("DELETE FROM food_brands"))
        conn.execute(text("DELETE FROM food_ingredients"))
        conn.execute(text("DELETE FROM food_resolution_rules"))
        conn.execute(text("DELETE FROM food_catalog_items"))
        conn.execute(text("DELETE FROM nutrients"))
        conn.execute(text("DELETE FROM data_sources"))
        conn.execute(text("DELETE FROM external_identities"))
        conn.execute(text("DELETE FROM device_connections"))
        conn.execute(text("DELETE FROM users"))
        conn.commit()
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def user(db) -> User:
    u = User(
        id=str(uuid.uuid4()),
        display_name="Stage2 User",
        timezone="Europe/Zagreb",
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture()
def food_catalog(db):
    """Seed the food catalog with realistic items for B1 resolution tests."""
    ds = DataSource(
        id=str(uuid.uuid4()),
        source_key="usda",
        source_label="USDA Foundation",
    )
    db.add(ds)
    db.flush()

    nutrients = {}
    for code, label, unit in [
        ("energy", "Energy", "kcal"),
        ("protein", "Protein", "g"),
        ("carbs", "Carbohydrate", "g"),
        ("fat", "Fat", "g"),
        ("fiber", "Fiber", "g"),
        ("sodium", "Sodium", "mg"),
    ]:
        n = Nutrient(
            id=str(uuid.uuid4()),
            nutrient_code=code,
            nutrient_label=label,
            unit=unit,
        )
        db.add(n)
        db.flush()
        nutrients[code] = n

    specs = {
        "milk": ("Milk, whole, 3.25% milkfat, with added vitamin D", True,
                 (61, 3.15, 4.8, 3.25, 0, 43)),
        "coffee": ("Coffee, brewed, prepared with tap water", True,
                   (2, 0.1, 0, 0, 0, 2)),
        "beer": ("Alcoholic beverage, beer, regular, BUDWEISER", True,
                 (43, 0.5, 3.6, 0, 0, 4)),
        "steak": ("Beef, ribeye, steak, separable lean and fat, trimmed to 0\" fat, choice, raw", False,
                  (271, 26, 0, 18, 0, 55)),
        "water": ("Water, tap, drinking", True,
                  (0, 0, 0, 0, 0, 0)),
        "wine": ("Alcoholic beverage, wine, table, red", True,
                 (85, 0.1, 2.6, 0, 0, 4)),
        "rice": ("Rice, white, long-grain, unenriched, raw", False,
                 (365, 7.1, 80, 0.7, 1.3, 5)),
        "chicken": ("Chicken, broilers or fryers, breast, boneless, skinless, raw", False,
                    (120, 22.5, 0, 2.6, 0, 45)),
    }
    foods = {}
    for key, (name, is_liquid, amt) in specs.items():
        food = Food(
            id=str(uuid.uuid4()),
            data_source_id=ds.id,
            external_id=f"test-{key}",
            display_name=name,
            is_generic=True,
            is_liquid=is_liquid,
            serving_grams=250 if is_liquid else 100,
            serving_unit="ml" if is_liquid else "g",
        )
        db.add(food)
        db.flush()
        for code, a in zip(["energy", "protein", "carbs", "fat", "fiber", "sodium"], amt):
            db.add(FoodNutrient(
                id=str(uuid.uuid4()),
                food_id=food.id,
                nutrient_id=nutrients[code].id,
                amount_per_100g=a,
            ))
        foods[key] = food

    db.commit()
    return foods


# ──────────────────────────────────────────────────────────────────────────
# B8 — PARSER REGRESSION TESTS
# ──────────────────────────────────────────────────────────────────────────

class TestB8Parser:
    """B8 regression: parser conversions and classification."""

    def test_2_dl_beer_is_200ml(self):
        """'2 dl beer' → 200 ml, not 2 ml."""
        items = parse_consumption_text("2 dl beer")
        assert len(items) == 1
        assert items[0].volume_ml == 200, f"Expected 200ml, got {items[0].volume_ml}"
        assert items[0].is_beverage

    def test_2_dcl_wine_is_200ml(self):
        """'2 dcl wine' → 200 ml."""
        items = parse_consumption_text("2 dcl wine")
        assert len(items) == 1
        assert items[0].volume_ml == 200, f"Expected 200ml, got {items[0].volume_ml}"
        assert items[0].is_beverage

    def test_2_mugs_coffee(self):
        """'2 mugs coffee' → 600 ml (plural mugs recognized)."""
        items = parse_consumption_text("2 mugs coffee")
        assert len(items) == 1
        assert items[0].volume_ml == 600, f"Expected 600ml, got {items[0].volume_ml}"
        assert items[0].is_beverage

    def test_2_espressos_coffee(self):
        """'2 espressos coffee' → 60 ml (plural espressos recognized)."""
        items = parse_consumption_text("2 espressos coffee")
        assert len(items) == 1
        assert items[0].volume_ml == 60, f"Expected 60ml, got {items[0].volume_ml}"
        assert items[0].is_beverage

    def test_1_cup_rice_is_food_not_beverage(self):
        """'1 cup rice' must be FOOD — container unit alone must not imply drink."""
        items = parse_consumption_text("1 cup rice")
        assert len(items) == 1
        assert items[0].is_beverage is False, "Rice is NOT a beverage"
        assert items[0].beverage_category is None

    def test_a_cup_of_rice_agrees_with_1_cup_rice(self):
        """'a cup of rice' and '1 cup rice' must parse to the same item type (food)."""
        a = parse_consumption_text("1 cup rice")
        b = parse_consumption_text("a cup of rice")
        assert len(a) == 1 and len(b) == 1
        assert a[0].is_beverage == b[0].is_beverage == False
        assert a[0].display_name.lower() == b[0].display_name.lower()

    def test_coffee_bare_preserves_beverage_identity(self):
        """'coffee' and 'one coffee' preserve beverage identity even without explicit qty."""
        a = parse_consumption_text("coffee")
        b = parse_consumption_text("one coffee")
        assert len(a) == 1 and len(b) == 1
        assert a[0].is_beverage, "coffee is a beverage"
        assert b[0].is_beverage, "one coffee is a beverage"

    def test_250g_milk_preserves_beverage_identity(self):
        """'250 g milk' must preserve beverage identity (mass must not erase it)."""
        items = parse_consumption_text("250 g milk")
        assert len(items) == 1
        assert items[0].is_beverage, "milk remains a beverage even with mass unit"
        assert items[0].grams == 250

    def test_250ml_milk_330ml_beer_splits_despite_no_space(self):
        """'250ml milk,330ml beer' must split into TWO items despite no space after comma."""
        items = parse_consumption_text("250ml milk,330ml beer")
        assert len(items) == 2, f"Expected 2 items, got {len(items)}: {[i.display_name for i in items]}"
        assert items[0].display_name.lower() == "milk"
        assert items[0].volume_ml == 250
        assert items[1].display_name.lower() == "beer"
        assert items[1].volume_ml == 330

    def test_i_drank_250ml_milk_parses(self):
        """'I drank 250 ml milk' parses quantity despite NL prefix."""
        items = parse_consumption_text("I drank 250 ml milk")
        assert len(items) >= 1
        milk = [i for i in items if "milk" in i.display_name.lower()]
        assert len(milk) == 1
        assert milk[0].volume_ml == 250

    def test_yesterday_i_drank_250ml_milk_parses(self):
        """'Yesterday I drank 250 ml milk' parses quantity + date reference."""
        items = parse_consumption_text("Yesterday I drank 250 ml milk")
        assert len(items) >= 1
        milk = [i for i in items if "milk" in i.display_name.lower()]
        assert len(milk) == 1
        assert milk[0].volume_ml == 250

    def test_decimal_comma_beer_still_works(self):
        """Preserve: '0,5 l beer' → 500 ml, one item."""
        items = parse_consumption_text("0,5 l beer")
        assert len(items) == 1
        assert items[0].volume_ml == 500
        assert items[0].is_beverage

    def test_tea_not_in_steak_preserved(self):
        """Preserve: 'tea' inside 'steak' must NOT match."""
        assert _classify_beverage("steak") is None
        assert _classify_beverage("tea") == "non_alcoholic"


# ──────────────────────────────────────────────────────────────────────────
# B1 — NUTRIENT RESOLUTION REGRESSION TESTS
# ──────────────────────────────────────────────────────────────────────────

def _seed_and_get_food(db, foods, key):
    return foods[key]


def _real_resolve_and_confirm(db, user, foods, text, meal_type="lunch"):
    """Full real path: parse → resolve nutrients through REAL DB → confirm → persist.
    Returns the confirm result dict.
    """
    from drhiro_api.services.consumption import resolve_item_nutrition
    parsed = parse_consumption_text(text)
    enriched = []
    for pi in parsed:
        resolve_item_nutrition(db, pi)
        enriched.append(pi)
    result = write_consumption(
        db=db,
        user_id=user.id,
        items=enriched,
        meal_type=meal_type,
    )
    return result


class TestB1NutrientResolution:
    """B1 regression: nutrient resolution in the draft→confirm path."""

    def test_original_failing_example_1_cup_coffee(self, db, user, food_catalog):
        """Original failing: '1 cup coffee' → real coffee kcal through real path."""
        result = _real_resolve_and_confirm(db, user, food_catalog, "1 cup coffee")
        assert result["ok"] is True
        totals = result["data"]["totals"]
        # 240ml coffee ≈ 240g, 2 kcal/100g → 4.8 kcal
        assert totals["kcal"] > 0, f"coffee should have kcal, got {totals['kcal']}"
        # All 6 nutrients present
        for k in NUTRIENT_KEYS:
            assert k in totals, f"Missing {k}"

    def test_original_failing_example_a_cup_of_coffee(self, db, user, food_catalog):
        """Original failing: 'a cup of coffee' → same as above."""
        result = _real_resolve_and_confirm(db, user, food_catalog, "a cup of coffee")
        assert result["ok"] is True
        totals = result["data"]["totals"]
        assert totals["kcal"] > 0

    def test_original_failing_example_1_glass_wine(self, db, user, food_catalog):
        """Original failing: '1 glass wine' → real wine kcal."""
        result = _real_resolve_and_confirm(db, user, food_catalog, "1 glass wine")
        assert result["ok"] is True
        totals = result["data"]["totals"]
        # 250ml wine, 85 kcal/100g → ~212 kcal
        assert totals["kcal"] > 100, f"wine should have kcal > 100, got {totals['kcal']}"

    def test_original_failing_example_05l_beer(self, db, user, food_catalog):
        """Original failing: '0,5 l beer' → real beer kcal."""
        result = _real_resolve_and_confirm(db, user, food_catalog, "0,5 l beer")
        assert result["ok"] is True
        totals = result["data"]["totals"]
        # 500ml beer, 43 kcal/100g → ~215 kcal
        assert totals["kcal"] > 100, f"beer should have kcal > 100, got {totals['kcal']}"

    def test_original_failing_example_200g_steak_250ml_water(self, db, user, food_catalog):
        """Original failing: '200 g steak and 250 ml water' → real steak kcal + 250ml water."""
        result = _real_resolve_and_confirm(
            db, user, food_catalog, "200 g steak and 250 ml water"
        )
        assert result["ok"] is True
        totals = result["data"]["totals"]
        # 200g steak, 271 kcal/100g → ~542 kcal (water adds 0)
        assert totals["kcal"] > 400, f"steak should have kcal > 400, got {totals['kcal']}"
        # Verify 2 items
        assert len(result["data"]["items"]) == 2
        # Volume: 250ml water
        water_item = [i for i in result["data"]["items"] if "water" in i["display_name"].lower()]
        assert len(water_item) == 1
        assert water_item[0]["volume_ml"] == 250

    def test_unknown_food_does_not_become_0kcal_meal(self, db, user, food_catalog):
        """A lookup failure must NOT become a successful 0-kcal meal."""
        result = _real_resolve_and_confirm(db, user, food_catalog, "dragonfruit with unicorn sauce")
        # Either the meal is flagged incomplete, or the item has nutrition_complete=False
        assert result["ok"] is True  # We log it but mark incomplete
        # The meal must be flagged as having incomplete nutrition
        assert result["data"].get("nutrition_complete") is False or any(
            it.get("nutrition_complete") is False for it in result["data"]["items"]
        ), "Unknown food must NOT be a successful complete 0-kcal meal"

    def test_persist_selected_food_and_nutrient_basis(self, db, user, food_catalog):
        """Persist selected food + nutrient basis + quantity + provenance."""
        result = _real_resolve_and_confirm(db, user, food_catalog, "250 ml milk")
        meal_id = result["data"]["meal_id"]
        # Check consumption_items persisted
        ci = db.query(ConsumptionItem).filter(
            ConsumptionItem.operation_id.in_(
                db.query(ConsumptionOperation.id).filter(
                    ConsumptionOperation.user_id == user.id
                )
            )
        ).first()
        assert ci is not None
        # Must have nutrient_basis
        assert ci.nutrient_basis in ("per_100_g", "per_100_ml")
        # Must have resolution_source
        assert ci.resolution_source in ("db", "external", "unmatched")
        # Must have food_catalog_item_id for resolved items
        if ci.resolution_source == "db":
            assert ci.food_catalog_item_id is not None

    def test_volume_stored_correctly_beverage(self, db, user, food_catalog):
        """Volume must be stored correctly for beverages."""
        result = _real_resolve_and_confirm(db, user, food_catalog, "250 ml milk")
        meal_id = result["data"]["meal_id"]
        meal = db.query(Meal).filter(Meal.id == meal_id).first()
        assert len(meal.items) == 1
        mi = meal.items[0]
        assert mi.volume_ml == 250

    def test_mass_and_volume_totals_all_6_nutrients(self, db, user, food_catalog):
        """Combined food+drink message yields all-6-nutrient totals + volume."""
        result = _real_resolve_and_confirm(
            db, user, food_catalog, "200 g steak and 250 ml water"
        )
        totals = result["data"]["totals"]
        for k in NUTRIENT_KEYS:
            assert k in totals
        # Steak 200g: 271*2 = 542 kcal
        assert totals["kcal"] == 542.0
        # Protein: 26*2 = 52
        assert totals["protein_g"] == 52.0
        # Fat: 18*2 = 36
        assert totals["fat_g"] == 36.0

    def test_mass_vs_volume_beverage_uses_ml_basis(self, db, user, food_catalog):
        """Beverages use volume/ml basis; foods use mass/g basis."""
        from drhiro_api.services.consumption import resolve_item_nutrition
        # Beverage: 250 ml milk
        bev_item = parse_consumption_text("250 ml milk")[0]
        resolve_item_nutrition(db, bev_item)
        assert bev_item.nutrient_basis == "per_100_ml"
        assert bev_item.is_beverage

        # Food: 200 g steak
        food_item = parse_consumption_text("200 g steak")[0]
        resolve_item_nutrition(db, food_item)
        assert food_item.nutrient_basis == "per_100_g"
        assert not food_item.is_beverage

    def test_retrieve_saved_resolution_on_retry(self, db, user, food_catalog):
        """On a retry, the same saved resolution must be returned."""
        from drhiro_api.services.consumption import resolve_item_nutrition
        text = "250 ml milk"
        parsed = parse_consumption_text(text)
        for pi in parsed:
            resolve_item_nutrition(db, pi)
        result1 = write_consumption(db, user.id, parsed, meal_type="lunch")
        meal_id1 = result1["data"]["meal_id"]
        op_id = db.query(Meal).filter(Meal.id == meal_id1).first().source_operation_id

        # Retry: parse + resolve again
        parsed2 = parse_consumption_text(text)
        for pi in parsed2:
            resolve_item_nutrition(db, pi)
        result2 = write_consumption(db, user.id, parsed2, meal_type="lunch",
                                    operation_id=op_id)
        assert result1["data"]["meal_id"] == result2["data"]["meal_id"]
        assert result1["data"]["totals"] == result2["data"]["totals"]

    def test_mock_external_provider_at_boundary(self, db, user, food_catalog):
        """Test with mocked external nutrition provider at the boundary.

        When the DB has no match, an external provider (USDA/DDG) should be
        called. We mock that provider's response to verify it's invoked at
        the correct boundary, and the resolved nutrients are persisted.
        """
        from unittest.mock import patch
        from drhiro_api.services.consumption import resolve_item_nutrition

        # A food not in our test catalog
        item = parse_consumption_text("150 g dragonfruit")[0]

        # Mock the external provider to return structured nutrition
        mock_external_response = [
            {
                "display_name": "Dragonfruit, raw",
                "kcal_per_100g": 60,
                "protein_g_per_100g": 1.2,
                "carbs_g_per_100g": 13,
                "fat_g_per_100g": 0.4,
                "fiber_g_per_100g": 3.0,
                "sodium_mg_per_100g": 2,
                "source": "mock_external",
                "confidence": 0.7,
                "food_id": "ext-dragonfruit",
            }
        ]

        with patch(
            "drhiro_api.services.consumption._external_nutrition_search",
            return_value=mock_external_response,
        ) as mock_search:
            resolve_item_nutrition(db, item)

            # External provider WAS called (DB had no match)
            mock_search.assert_called_once()

        # Nutrients from the mock are attached
        assert item.nutrients_per_100["kcal"] == 60
        # The source comes from the candidate (mock returns "mock_external")
        assert item.resolution_source in ("external", "mock_external")
        # Scaled: 150g / 100g * 60 = 90 kcal
        assert item.nutrients_scaled["kcal"] == 90.0


class TestB1B8OriginalFailingExamples:
    """The 5 original failing examples, fully resolved through real path."""

    @pytest.mark.parametrize("text,expected_items,expected_first_volume", [
        ("1 cup coffee", 1, 240),
        ("a cup of coffee", 1, 240),
        ("1 glass wine", 1, 250),
        ("0,5 l beer", 1, 500),
    ])
    def test_original_failing_single_items(self, db, user, food_catalog, text, expected_items, expected_first_volume):
        result = _real_resolve_and_confirm(db, user, food_catalog, text)
        assert result["ok"] is True
        assert len(result["data"]["items"]) == expected_items
        assert result["data"]["items"][0]["volume_ml"] == expected_first_volume

    def test_original_failing_mixed(self, db, user, food_catalog):
        """'200 g steak and 250 ml water' → real steak kcal + 250ml water."""
        result = _real_resolve_and_confirm(
            db, user, food_catalog, "200 g steak and 250 ml water"
        )
        assert result["ok"] is True
        assert len(result["data"]["items"]) == 2
        assert result["data"]["totals"]["kcal"] > 400
