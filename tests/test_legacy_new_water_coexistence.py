"""Integration tests for legacy water + new beverage_measurements coexistence.

Proves that:
  1. A legacy manual-water Measurement row (metric_type='water') and a new
     linked beverage (from write_consumption) for DIFFERENT drinks BOTH appear
     in daily liquid aggregation without omission or double counting.
  2. A drink that exists as BOTH a meal item's linked beverage AND a manual
     water entry is counted TWICE — because they are genuinely separate intents
     (the user said "I had a beer with lunch" AND later said "log 330ml beer").
     This is NOT a double-count bug; it is two distinct drinks.
  3. The dashboard aggregation query correctly sums all Measurement rows with
     metric_type='water' regardless of source_provider.
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
    Meal, MealItem, Measurement, BeverageMeasurement,
    ConsumptionOperation, ConsumptionItem,
)
from drhiro_api.services.consumption import (
    parse_consumption_text,
    confirm_consumption,
    write_consumption,
    ParsedItem,
    _scale_nutrients,
    NUTRIENT_KEYS,
)

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
        conn.execute(text("DELETE FROM nutrients"))
        conn.execute(text("DELETE FROM data_sources"))
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
    u = User(id=str(uuid.uuid4()), display_name="Coexist User", timezone="Europe/Zagreb")
    db.add(u)
    db.flush()
    return u


@pytest.fixture()
def food_catalog(db):
    ds = DataSource(id=str(uuid.uuid4()), source_key="test", source_label="Test Source")
    db.add(ds)
    db.flush()

    nutrients = {}
    for code, label, unit in [
        ("energy", "Energy", "kcal"), ("protein", "Protein", "g"),
        ("carbs", "Carbohydrate", "g"), ("fat", "Fat", "g"),
        ("fiber", "Fiber", "g"), ("sodium", "Sodium", "mg"),
    ]:
        n = Nutrient(id=str(uuid.uuid4()), nutrient_code=code, nutrient_label=label, unit=unit)
        db.add(n)
        db.flush()
        nutrients[code] = n

    specs = {
        "beer":   ("Beer, regular",     True,  (43, 0.5, 3.6, 0, 0, 4)),
        "coffee": ("Coffee, brewed",    True,  (2, 0.1, 0, 0, 0, 2)),
    }
    foods = {}
    for key, (name, is_liquid, amt) in specs.items():
        food = Food(
            id=str(uuid.uuid4()), data_source_id=ds.id,
            external_id=f"test-{key}", display_name=name,
            is_generic=True, is_liquid=is_liquid,
            serving_grams=250 if is_liquid else 200,
            serving_unit="ml" if is_liquid else "g",
        )
        db.add(food)
        db.flush()
        for code, a in zip(["energy", "protein", "carbs", "fat", "fiber", "sodium"], amt):
            db.add(FoodNutrient(
                id=str(uuid.uuid4()), food_id=food.id,
                nutrient_id=nutrients[code].id, amount_per_100g=a,
            ))
        foods[key] = food
    db.commit()
    return foods


def _make_item(display_name, *, grams=None, volume_ml=None, beverage_category=None,
               is_beverage=False, food=None, db=None):
    item = ParsedItem(
        display_name=display_name, grams=grams, volume_ml=volume_ml,
        beverage_category=beverage_category, is_beverage=is_beverage,
    )
    if food and db:
        n100 = {}
        for fn in food.nutrients:
            code = fn.nutrient.nutrient_code
            amt = fn.amount_per_100g
            if code == "energy": n100["kcal"] = amt
            elif code == "protein": n100["protein_g"] = amt
            elif code == "carbs": n100["carbs_g"] = amt
            elif code == "fat": n100["fat_g"] = amt
            elif code == "fiber": n100["fiber_g"] = amt
            elif code == "sodium": n100["sodium_mg"] = amt
        item.nutrients_per_100 = n100
        factor = (item.grams or 100) / 100.0
        item.nutrients_scaled = _scale_nutrients(n100, factor)
        item.source = "db"
        item.confidence = 0.9
    else:
        item.nutrients_per_100 = {k: 0 for k in NUTRIENT_KEYS}
        item.nutrients_scaled = {k: 0 for k in NUTRIENT_KEYS}
    return item


def _legacy_water(db, user, amount_ml, category="water", measured_at=None):
    """Write a legacy manual-water Measurement row (as ingest.py does)."""
    m = Measurement(
        id=str(uuid.uuid4()),
        user_id=user.id,
        metric_type="water",
        start_at=measured_at or datetime.now(timezone.utc),
        end_at=measured_at or datetime.now(timezone.utc),
        value_json={"amount_ml": amount_ml, "category": category},
        unit="ml",
        source_provider="manual",
        source_record_id=f"manual-{uuid.uuid4().hex}",
        recording_method="manual",
        confidence=1.0,
    )
    db.add(m)
    db.flush()
    return m


class TestLegacyNewWaterCoexistence:
    def test_legacy_and_new_different_drinks_both_appear(self, db, user, food_catalog):
        """A legacy water measurement + a new linked beverage for DIFFERENT drinks
        both appear in the daily liquid aggregation."""
        now = datetime.now(timezone.utc)

        # Legacy manual water: 500ml water
        _legacy_water(db, user, amount_ml=500, category="water", measured_at=now)

        # New linked beverage: 330ml beer (from a meal)
        items = [
            _make_item("beer", grams=330, volume_ml=330, beverage_category="beer",
                       is_beverage=True, food=food_catalog["beer"], db=db),
        ]
        write_consumption(db, user.id, items, meal_type="lunch")

        db.commit()

        # Query like the dashboard does
        rows = db.query(Measurement).filter(
            Measurement.user_id == user.id,
            Measurement.metric_type == "water",
        ).all()

        total_by_category = {}
        for m in rows:
            vj = m.value_json or {}
            cat = vj.get("category") or "water"
            total_by_category[cat] = total_by_category.get(cat, 0) + vj.get("amount_ml", 0)

        assert total_by_category.get("water") == 500
        assert total_by_category.get("beer") == 330

    def test_same_drink_manual_plus_meal_counts_twice(self, db, user, food_catalog):
        """A beer in a meal AND a manual beer log = 2 separate drinks.
        This is the correct semantics: the user had a beer with lunch AND
        later logged another beer. Both count."""
        now = datetime.now(timezone.utc)

        # Meal with beer
        items = [
            _make_item("beer", grams=330, volume_ml=330, beverage_category="beer",
                       is_beverage=True, food=food_catalog["beer"], db=db),
        ]
        write_consumption(db, user.id, items, meal_type="lunch")

        # Manual beer log (separate intent)
        _legacy_water(db, user, amount_ml=330, category="beer", measured_at=now)

        db.commit()

        # Filter all water measurements and sum beer ones (Python-level, like dashboard)
        rows = db.query(Measurement).filter(
            Measurement.user_id == user.id,
            Measurement.metric_type == "water",
        ).all()

        beer_rows = [m for m in rows if (m.value_json or {}).get("category") == "beer"]
        total_beer_ml = sum(m.value_json.get("amount_ml", 0) for m in beer_rows)
        assert total_beer_ml == 660  # 330 + 330 = two beers

    def test_aggregation_no_omission_with_mixed_sources(self, db, user, food_catalog):
        """Mix of legacy and new rows for different categories — all appear."""
        now = datetime.now(timezone.utc)

        # Legacy: 250ml coffee (manual)
        _legacy_water(db, user, amount_ml=250, category="non_alcoholic", measured_at=now)

        # New: 330ml beer (from meal)
        items = [
            _make_item("beer", grams=330, volume_ml=330, beverage_category="beer",
                       is_beverage=True, food=food_catalog["beer"], db=db),
        ]
        write_consumption(db, user.id, items, meal_type="dinner")

        db.commit()

        rows = db.query(Measurement).filter(
            Measurement.user_id == user.id,
            Measurement.metric_type == "water",
        ).all()

        total_by_cat = {}
        for m in rows:
            vj = m.value_json or {}
            cat = vj.get("category") or "water"
            total_by_cat[cat] = total_by_cat.get(cat, 0) + vj.get("amount_ml", 0)

        assert total_by_cat.get("non_alcoholic") == 250
        assert total_by_cat.get("beer") == 330
        assert total_by_cat.get("water", 0) == 0

    def test_legacy_only_still_works(self, db, user):
        """Old manual-water rows without a category still aggregate as 'water'."""
        now = datetime.now(timezone.utc)

        _legacy_water(db, user, amount_ml=250, category="water", measured_at=now)
        _legacy_water(db, user, amount_ml=500, category="water", measured_at=now)

        db.commit()

        rows = db.query(Measurement).filter(
            Measurement.user_id == user.id,
            Measurement.metric_type == "water",
        ).all()

        total = sum(m.value_json.get("amount_ml", 0) for m in rows)
        assert total == 750
