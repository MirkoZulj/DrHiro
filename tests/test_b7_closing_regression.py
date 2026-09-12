"""B7 Closing Regression Tests — Gap 1 (delete_meal cascade) + Gap 2
(beverage→solid rename). These tests are RED before the fix, GREEN after.

Covers:
  - delete_meal router cascades/tombstones ALL liquid projections (no orphan)
  - retry of original creation after delete does NOT resurrect
  - beverage→solid rename clears beverage_category + drops liquid + recompute 6 nutrients
  - beverage→beverage rename keeps liquid + updates category/volume
  - ownership: cross-user delete/rename returns 404, nothing mutated
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))

from drhiro_api.models import (
    Base, User, Food, DataSource, Nutrient, FoodNutrient,
    Meal, MealItem, Measurement, BeverageMeasurement,
)
from drhiro_api.routers.meals import (
    patch_meal_item,
    MealItemPatch,
)
from drhiro_api.services.consumption import (
    _classify_beverage,
    NUTRIENT_KEYS,
)

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
        display_name="B7 User",
        timezone="Europe/Zagreb",
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture()
def other_user(db) -> User:
    u = User(
        id=str(uuid.uuid4()),
        display_name="Other",
        timezone="UTC",
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture()
def food_catalog(db):
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
        "milk": ("Milk, whole", True, (42, 3.4, 5, 1, 0, 44)),
        "juice": ("Orange juice", True, (45, 0.7, 10, 0.2, 0.2, 1)),
        "steak": ("Beef, steak, raw", False, (271, 26, 0, 18, 0, 55)),
        "water": ("Water, tap", True, (0, 0, 0, 0, 0, 0)),
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


def _make_meal(db, user, food_catalog, items_specs, meal_type="lunch"):
    meal = Meal(
        user_id=user.id,
        eaten_at=datetime.now(timezone.utc),
        meal_type=meal_type,
        status="confirmed",
        input_method="direct",
        totals_json={},
        confidence=1.0,
    )
    db.add(meal)
    db.flush()

    for spec in items_specs:
        mi = MealItem(
            meal_id=meal.id,
            food_catalog_item_id=spec.get("food_catalog_item_id"),
            display_name=spec["display_name"],
            quantity=spec.get("quantity", 1.0),
            unit=spec.get("unit"),
            grams=spec.get("grams"),
            nutrients_json=spec.get("nutrients_json"),
            source=spec.get("source", "manual"),
            confidence=spec.get("confidence", 0.8),
            beverage_category=spec.get("beverage_category"),
            volume_ml=spec.get("volume_ml"),
        )
        db.add(mi)

    db.flush()
    db.commit()
    return meal


def _attach_liquid(db, user, meal, mi, food_key, food_cat, grams):
    """Create the BeverageMeasurement + Measurement for a beverage item."""
    from drhiro_api.services.consumption import create_beverage_projection
    create_beverage_projection(db, user.id, mi, float(grams), food_cat, meal.eaten_at)
    db.commit()


# ──────────────────────────────────────────────────────────────────────────
# GAP 1: delete_meal cascade + no-resurrect
# ──────────────────────────────────────────────────────────────────────────

class TestDeleteMealCascade:
    """DELETE /meals/{meal_id} must tombstone/cascade ALL liquid projections."""

    def test_delete_meal_cascades_to_liquid_projections(self, db, user, food_catalog):
        """Deleting a meal with beverages must remove/tombstone the linked
        BeverageMeasurement + Measurement rows so no orphaned liquid remains."""
        meal = _make_meal(db, user, food_catalog, [
            {"display_name": "milk", "grams": 250, "volume_ml": 250,
             "beverage_category": "non_alcoholic", "nutrients_json": {
                 "kcal": 105.0, "protein_g": 8.5, "carbs_g": 12.5,
                 "fat_g": 2.5, "fiber_g": 0.0, "sodium_mg": 110.0,
             }},
        ])
        mi = meal.items[0]
        _attach_liquid(db, user, meal, mi, "milk", "non_alcoholic", 250)

        # Verify liquid exists
        bev_count = db.query(BeverageMeasurement).filter(
            BeverageMeasurement.user_id == user.id
        ).count()
        assert bev_count == 1
        meas_count = db.query(Measurement).filter(
            Measurement.user_id == user.id, Measurement.metric_type == "water"
        ).count()
        assert meas_count == 1

        # Delete the meal via router
        from drhiro_api.routers.meals import delete_meal
        result = delete_meal(str(meal.id), user=user, db=db)
        assert result["ok"] is True

        # Beverage measurements must be gone
        bev_count = db.query(BeverageMeasurement).filter(
            BeverageMeasurement.user_id == user.id
        ).count()
        assert bev_count == 0, f"Expected 0 beverage measurements after delete, got {bev_count}"

        # Measurements must be gone
        meas_count = db.query(Measurement).filter(
            Measurement.user_id == user.id, Measurement.metric_type == "water"
        ).count()
        assert meas_count == 0, f"Expected 0 measurements after delete, got {meas_count}"

        # Meal items must be gone
        mi_count = db.query(MealItem).filter(MealItem.meal_id == meal.id).count()
        assert mi_count == 0, f"Expected 0 meal items after delete, got {mi_count}"

    def test_delete_meal_retry_does_not_resurrect(self, db, user, food_catalog):
        """After deleting a meal, retrying the original creation must NOT
        resurrect the deleted meal's consumption."""
        meal = _make_meal(db, user, food_catalog, [
            {"display_name": "milk", "grams": 250, "volume_ml": 250,
             "beverage_category": "non_alcoholic", "nutrients_json": {
                 "kcal": 105.0, "protein_g": 8.5, "carbs_g": 12.5,
                 "fat_g": 2.5, "fiber_g": 0.0, "sodium_mg": 110.0,
             }},
        ])
        mi = meal.items[0]
        _attach_liquid(db, user, meal, mi, "milk", "non_alcoholic", 250)

        # Delete the meal
        from drhiro_api.routers.meals import delete_meal
        delete_meal(str(meal.id), user=user, db=db)

        # Verify everything is gone
        assert db.query(MealItem).filter(MealItem.meal_id == meal.id).count() == 0
        assert db.query(Measurement).filter(
            Measurement.user_id == user.id, Measurement.metric_type == "water"
        ).count() == 0

        # Retry: create a new meal with the same content
        meal2 = _make_meal(db, user, food_catalog, [
            {"display_name": "milk", "grams": 250, "volume_ml": 250,
             "beverage_category": "non_alcoholic", "nutrients_json": {
                 "kcal": 105.0, "protein_g": 8.5, "carbs_g": 12.5,
                 "fat_g": 2.5, "fiber_g": 0.0, "sodium_mg": 110.0,
             }},
        ])
        mi2 = meal2.items[0]
        _attach_liquid(db, user, meal2, mi2, "milk", "non_alcoholic", 250)

        # The new meal is a SEPARATE meal — the old one stays deleted
        assert str(meal2.id) != str(meal.id)
        # Only the new meal's liquid exists
        meas_count = db.query(Measurement).filter(
            Measurement.user_id == user.id, Measurement.metric_type == "water"
        ).count()
        assert meas_count == 1, f"Expected 1 measurement (new meal only), got {meas_count}"

    def test_delete_meal_ownership_404(self, db, user, other_user, food_catalog):
        """User B cannot delete User A's meal."""
        meal = _make_meal(db, user, food_catalog, [
            {"display_name": "milk", "grams": 250, "volume_ml": 250,
             "beverage_category": "non_alcoholic", "nutrients_json": {
                 "kcal": 105.0, "protein_g": 8.5, "carbs_g": 12.5,
                 "fat_g": 2.5, "fiber_g": 0.0, "sodium_mg": 110.0,
             }},
        ])
        mi = meal.items[0]
        _attach_liquid(db, user, meal, mi, "milk", "non_alcoholic", 250)

        from drhiro_api.routers.meals import delete_meal
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            delete_meal(str(meal.id), user=other_user, db=db)
        assert exc_info.value.status_code == 404

        # Nothing was mutated
        assert db.query(Meal).filter(Meal.id == meal.id).count() == 1
        assert db.query(Measurement).filter(
            Measurement.user_id == user.id, Measurement.metric_type == "water"
        ).count() == 1


# ──────────────────────────────────────────────────────────────────────────
# GAP 2: beverage→solid rename drops liquid; beverage→beverage keeps liquid
# ──────────────────────────────────────────────────────────────────────────

class TestBeverageToSolidRename:
    """PATCH /meals/{id}/items/{id} renaming a beverage to a solid food
    must clear beverage_category + drop the obsolete liquid projection
    (BeverageMeasurement + Measurement) + recompute all-six-nutrient totals."""

    def test_beverage_to_solid_rename_clears_category_and_drops_liquid(self, db, user, food_catalog):
        """Renaming milk→steak must:
        - clear beverage_category on the item
        - remove the BeverageMeasurement + Measurement
        - recompute meal totals from the solid food's nutrients
        """
        meal = _make_meal(db, user, food_catalog, [
            {"display_name": "milk", "grams": 250, "volume_ml": 250,
             "beverage_category": "non_alcoholic", "nutrients_json": {
                 "kcal": 105.0, "protein_g": 8.5, "carbs_g": 12.5,
                 "fat_g": 2.5, "fiber_g": 0.0, "sodium_mg": 110.0,
             }},
        ])
        mi = meal.items[0]
        _attach_liquid(db, user, meal, mi, "milk", "non_alcoholic", 250)

        # Verify liquid exists before rename
        assert db.query(BeverageMeasurement).filter(
            BeverageMeasurement.user_id == user.id
        ).count() == 1

        # Rename milk → steak via the router (using food_catalog_item_id which
        # triggers _apply_explicit_food → sets display_name to canonical "Beef, steak, raw")
        req = MealItemPatch(food_catalog_item_id="test-steak")
        result = patch_meal_item(str(meal.id), str(mi.id), req, user=user, db=db)

        # Item must be renamed (canonical name from the food catalog)
        mi_after = db.query(MealItem).filter(MealItem.id == mi.id).first()
        assert mi_after is not None
        assert mi_after.display_name == "Beef, steak, raw"

        # beverage_category must be cleared
        assert mi_after.beverage_category is None, \
            f"beverage_category should be None after rename to solid, got {mi_after.beverage_category}"

        # volume_ml must be cleared
        assert mi_after.volume_ml is None, \
            f"volume_ml should be None after rename to solid, got {mi_after.volume_ml}"

        # BeverageMeasurement must be gone
        bev_count = db.query(BeverageMeasurement).filter(
            BeverageMeasurement.user_id == user.id
        ).count()
        assert bev_count == 0, f"Expected 0 beverage measurements after rename, got {bev_count}"

        # Measurement must be gone
        meas_count = db.query(Measurement).filter(
            Measurement.user_id == user.id, Measurement.metric_type == "water"
        ).count()
        assert meas_count == 0, f"Expected 0 measurements after rename, got {meas_count}"

        # Meal totals must be recomputed from the solid food (steak: 271 kcal/100g at 250g)
        meal_after = db.query(Meal).filter(Meal.id == meal.id).first()
        totals = meal_after.totals_json
        # steak per 100g: (271, 26, 0, 18, 0, 55) → at 250g: 2.5x
        assert abs(totals["kcal"] - 677.5) < 1.0, f"Expected ~677.5 kcal, got {totals['kcal']}"
        assert abs(totals["protein_g"] - 65.0) < 1.0, f"Expected ~65.0 protein_g, got {totals['protein_g']}"
        assert abs(totals["carbs_g"] - 0.0) < 0.1, f"Expected ~0.0 carbs_g, got {totals['carbs_g']}"
        assert abs(totals["fat_g"] - 45.0) < 1.0, f"Expected ~45.0 fat_g, got {totals['fat_g']}"
        assert abs(totals["fiber_g"] - 0.0) < 0.1, f"Expected ~0.0 fiber_g, got {totals['fiber_g']}"
        assert abs(totals["sodium_mg"] - 137.5) < 1.0, f"Expected ~137.5 sodium_mg, got {totals['sodium_mg']}"

    def test_beverage_to_solid_rename_by_name_only(self, db, user, food_catalog):
        """Renaming milk→steak by display_name only (no food_catalog_item_id)
        must also clear beverage_category + drop liquid projection."""
        meal = _make_meal(db, user, food_catalog, [
            {"display_name": "milk", "grams": 250, "volume_ml": 250,
             "beverage_category": "non_alcoholic", "nutrients_json": {
                 "kcal": 105.0, "protein_g": 8.5, "carbs_g": 12.5,
                 "fat_g": 2.5, "fiber_g": 0.0, "sodium_mg": 110.0,
             }},
        ])
        mi = meal.items[0]
        _attach_liquid(db, user, meal, mi, "milk", "non_alcoholic", 250)

        # Rename milk → steak by name only (no food_catalog_item_id)
        req = MealItemPatch(display_name="steak")
        result = patch_meal_item(str(meal.id), str(mi.id), req, user=user, db=db)

        mi_after = db.query(MealItem).filter(MealItem.id == mi.id).first()
        assert mi_after.display_name == "steak"
        assert mi_after.beverage_category is None
        assert mi_after.volume_ml is None

        bev_count = db.query(BeverageMeasurement).filter(
            BeverageMeasurement.user_id == user.id
        ).count()
        assert bev_count == 0
        meas_count = db.query(Measurement).filter(
            Measurement.user_id == user.id, Measurement.metric_type == "water"
        ).count()
        assert meas_count == 0

    def test_beverage_to_beverage_rename_keeps_liquid(self, db, user, food_catalog):
        """Renaming milk→juice must:
        - keep the BeverageMeasurement + Measurement
        - update beverage_category to the new category
        - update volume_ml (if grams change)
        - recompute meal totals from the new beverage's nutrients
        """
        meal = _make_meal(db, user, food_catalog, [
            {"display_name": "milk", "grams": 250, "volume_ml": 250,
             "beverage_category": "non_alcoholic", "nutrients_json": {
                 "kcal": 105.0, "protein_g": 8.5, "carbs_g": 12.5,
                 "fat_g": 2.5, "fiber_g": 0.0, "sodium_mg": 110.0,
             }},
        ])
        mi = meal.items[0]
        _attach_liquid(db, user, meal, mi, "milk", "non_alcoholic", 250)

        # Rename milk → juice via the router (food_catalog_item_id triggers
        # _apply_explicit_food → sets display_name to canonical "Orange juice")
        req = MealItemPatch(food_catalog_item_id="test-juice")
        result = patch_meal_item(str(meal.id), str(mi.id), req, user=user, db=db)

        # Item must be renamed (canonical name)
        mi_after = db.query(MealItem).filter(MealItem.id == mi.id).first()
        assert mi_after is not None
        assert mi_after.display_name == "Orange juice"

        # beverage_category must be updated (juice is non_alcoholic)
        assert mi_after.beverage_category is not None, \
            "beverage_category should be set after rename to another beverage"
        assert mi_after.beverage_category == "non_alcoholic"

        # BeverageMeasurement must still exist
        bev_count = db.query(BeverageMeasurement).filter(
            BeverageMeasurement.user_id == user.id
        ).count()
        assert bev_count == 1, f"Expected 1 beverage measurement after rename, got {bev_count}"

        # Measurement must still exist
        meas_count = db.query(Measurement).filter(
            Measurement.user_id == user.id, Measurement.metric_type == "water"
        ).count()
        assert meas_count == 1, f"Expected 1 measurement after rename, got {meas_count}"

        # Meal totals must be recomputed from juice (45 kcal/100g at 250g = 2.5x)
        meal_after = db.query(Meal).filter(Meal.id == meal.id).first()
        totals = meal_after.totals_json
        assert abs(totals["kcal"] - 112.5) < 0.5
        assert abs(totals["protein_g"] - 1.75) < 0.2
        assert abs(totals["carbs_g"] - 25.0) < 0.5
        assert abs(totals["fat_g"] - 0.5) < 0.2
        assert abs(totals["fiber_g"] - 0.5) < 0.2
        assert abs(totals["sodium_mg"] - 2.5) < 0.5

    def test_beverage_to_solid_ownership_404(self, db, user, other_user, food_catalog):
        """User B cannot rename User A's meal item."""
        meal = _make_meal(db, user, food_catalog, [
            {"display_name": "milk", "grams": 250, "volume_ml": 250,
             "beverage_category": "non_alcoholic", "nutrients_json": {
                 "kcal": 105.0, "protein_g": 8.5, "carbs_g": 12.5,
                 "fat_g": 2.5, "fiber_g": 0.0, "sodium_mg": 110.0,
             }},
        ])
        mi = meal.items[0]
        _attach_liquid(db, user, meal, mi, "milk", "non_alcoholic", 250)

        req = MealItemPatch(display_name="steak", food_catalog_item_id="test-steak")
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            patch_meal_item(str(meal.id), str(mi.id), req, user=other_user, db=db)
        assert exc_info.value.status_code == 404

        # Nothing was mutated
        mi_after = db.query(MealItem).filter(MealItem.id == mi.id).first()
        assert mi_after.display_name == "milk"
        assert mi_after.beverage_category == "non_alcoholic"
        assert db.query(BeverageMeasurement).filter(
            BeverageMeasurement.user_id == user.id
        ).count() == 1
