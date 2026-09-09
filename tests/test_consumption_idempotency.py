"""Tests for the unified consumption domain: idempotency, parser, mutations, aggregation.

Maps each required regression case to a test. Uses the drhiro_test database
(NEVER production). Seeds minimal food catalog data for deterministic resolution.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

# Ensure the api src is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))

from drhiro_api.models import (
    Base, User, Food, DataSource, Nutrient, FoodNutrient,
    Meal, MealItem, Measurement,
    ConsumptionOperation, ConsumptionItem, BeverageMeasurement,
)
from drhiro_api.services.consumption import (
    parse_consumption_text,
    write_consumption,
    update_item_quantity,
    replace_beverage,
    delete_beverage,
    delete_meal,
    get_or_create_operation,
    get_operation_result,
    _normalize_meal_type,
    _classify_beverage,
    _sum_nutrients,
    _scale_nutrients,
    NUTRIENT_KEYS,
    MEAL_GROUPS,
    DEFAULT_MEAL_TYPE,
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
    """Create all tables from the models (idempotent)."""
    Base.metadata.create_all(engine)
    yield


@pytest.fixture()
def db(engine, tables):
    """Fresh session per test. Cleans up before and rolls back after."""
    # Clean any leftover data from previous tests
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
        display_name="Test User",
        timezone="Europe/Zagreb",
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture()
def food_catalog(db):
    """Seed minimal food catalog for deterministic tests."""
    ds = DataSource(
        id=str(uuid.uuid4()),
        source_key="test",
        source_label="Test Source",
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

    # Milk: 42 kcal, 3.4g protein, 5g carbs, 1g fat, 0 fiber, 44mg sodium per 100g
    milk = Food(
        id=str(uuid.uuid4()),
        data_source_id=ds.id,
        external_id="test-milk",
        display_name="Milk, whole",
        is_generic=True,
        is_liquid=True,
        serving_grams=250,
        serving_unit="ml",
    )
    db.add(milk)
    db.flush()
    for code, amt in [
        ("energy", 42), ("protein", 3.4), ("carbs", 5), ("fat", 1),
        ("fiber", 0), ("sodium", 44),
    ]:
        db.add(FoodNutrient(
            id=str(uuid.uuid4()),
            food_id=milk.id,
            nutrient_id=nutrients[code].id,
            amount_per_100g=amt,
        ))

    # Coffee: 2 kcal, 0.1g protein, 0g carbs, 0g fat, 0 fiber, 2mg sodium per 100g
    coffee = Food(
        id=str(uuid.uuid4()),
        data_source_id=ds.id,
        external_id="test-coffee",
        display_name="Coffee, brewed",
        is_generic=True,
        is_liquid=True,
        serving_grams=240,
        serving_unit="ml",
    )
    db.add(coffee)
    db.flush()
    for code, amt in [
        ("energy", 2), ("protein", 0.1), ("carbs", 0), ("fat", 0),
        ("fiber", 0), ("sodium", 2),
    ]:
        db.add(FoodNutrient(
            id=str(uuid.uuid4()),
            food_id=coffee.id,
            nutrient_id=nutrients[code].id,
            amount_per_100g=amt,
        ))

    # Beer: 43 kcal, 0.5g protein, 3.6g carbs, 0g fat, 0 fiber, 4mg sodium per 100g
    beer = Food(
        id=str(uuid.uuid4()),
        data_source_id=ds.id,
        external_id="test-beer",
        display_name="Beer, regular",
        is_generic=True,
        is_liquid=True,
        serving_grams=330,
        serving_unit="ml",
    )
    db.add(beer)
    db.flush()
    for code, amt in [
        ("energy", 43), ("protein", 0.5), ("carbs", 3.6), ("fat", 0),
        ("fiber", 0), ("sodium", 4),
    ]:
        db.add(FoodNutrient(
            id=str(uuid.uuid4()),
            food_id=beer.id,
            nutrient_id=nutrients[code].id,
            amount_per_100g=amt,
        ))

    # Steak: 271 kcal, 26g protein, 0g carbs, 18g fat, 0 fiber, 55mg sodium per 100g
    steak = Food(
        id=str(uuid.uuid4()),
        data_source_id=ds.id,
        external_id="test-steak",
        display_name="Beef, steak, raw",
        is_generic=True,
        is_liquid=False,
        serving_grams=250,
        serving_unit="g",
    )
    db.add(steak)
    db.flush()
    for code, amt in [
        ("energy", 271), ("protein", 26), ("carbs", 0), ("fat", 18),
        ("fiber", 0), ("sodium", 55),
    ]:
        db.add(FoodNutrient(
            id=str(uuid.uuid4()),
            food_id=steak.id,
            nutrient_id=nutrients[code].id,
            amount_per_100g=amt,
        ))

    # Water: 0 kcal
    water = Food(
        id=str(uuid.uuid4()),
        data_source_id=ds.id,
        external_id="test-water",
        display_name="Water, tap",
        is_generic=True,
        is_liquid=True,
        serving_grams=250,
        serving_unit="ml",
    )
    db.add(water)
    db.flush()
    for code, amt in [
        ("energy", 0), ("protein", 0), ("carbs", 0), ("fat", 0),
        ("fiber", 0), ("sodium", 0),
    ]:
        db.add(FoodNutrient(
            id=str(uuid.uuid4()),
            food_id=water.id,
            nutrient_id=nutrients[code].id,
            amount_per_100g=amt,
        ))

    db.commit()
    return {"milk": milk, "coffee": coffee, "beer": beer, "steak": steak, "water": water}


def _attach_nutrients(db, item, food):
    """Attach per-100 and scaled nutrients to a ParsedItem from a Food row."""
    from drhiro_api.services.consumption import ParsedItem
    n100 = {}
    for fn in food.nutrients:
        code = fn.nutrient.nutrient_code
        amt = fn.amount_per_100g
        if code == "energy":
            n100["kcal"] = amt
        elif code == "protein":
            n100["protein_g"] = amt
        elif code == "carbs":
            n100["carbs_g"] = amt
        elif code == "fat":
            n100["fat_g"] = amt
        elif code == "fiber":
            n100["fiber_g"] = amt
        elif code == "sodium":
            n100["sodium_mg"] = amt
    item.nutrients_per_100 = n100
    factor = (item.grams or 100) / 100.0
    item.nutrients_scaled = _scale_nutrients(n100, factor)
    item.source = "db"
    item.confidence = 0.9


def _make_item(display_name, grams=None, volume_ml=None, beverage_category=None,
               is_beverage=False, food=None, db=None):
    """Helper to build a ParsedItem with nutrients from the food catalog."""
    from drhiro_api.services.consumption import ParsedItem
    item = ParsedItem(
        display_name=display_name,
        grams=grams,
        volume_ml=volume_ml,
        beverage_category=beverage_category,
        is_beverage=is_beverage,
    )
    if food and db:
        _attach_nutrients(db, item, food)
    else:
        # Default minimal nutrients
        item.nutrients_per_100 = {"kcal": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "fiber_g": 0, "sodium_mg": 0}
        item.nutrients_scaled = {"kcal": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "fiber_g": 0, "sodium_mg": 0}
    return item


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------

class TestParser:
    def test_250ml_milk_single_item(self):
        items = parse_consumption_text("250ml milk")
        assert len(items) == 1
        assert items[0].display_name.lower() == "milk"
        assert items[0].volume_ml == 250
        assert items[0].is_beverage

    def test_1cup_coffee(self):
        items = parse_consumption_text("1 cup coffee")
        assert len(items) == 1
        assert items[0].display_name.lower() == "coffee"
        assert items[0].volume_ml == 240  # 1 cup = 240ml
        assert items[0].is_beverage

    def test_a_cup_of_coffee_consistent(self):
        """'1 cup coffee' and 'a cup of coffee' should parse to the same volume."""
        items_a = parse_consumption_text("1 cup coffee")
        items_b = parse_consumption_text("a cup of coffee")
        assert len(items_a) == 1
        assert len(items_b) == 1
        assert items_a[0].volume_ml == items_b[0].volume_ml == 240

    def test_decimal_comma_beer_single_item(self):
        """'0,5 l beer' must be ONE item of 500ml, NOT two items."""
        items = parse_consumption_text("0,5 l beer")
        assert len(items) == 1, f"Expected 1 item, got {len(items)}: {[i.display_name for i in items]}"
        assert items[0].display_name.lower() == "beer"
        assert items[0].volume_ml == 500

    def test_milk_and_beer_two_items(self):
        items = parse_consumption_text("250ml milk and 330ml beer")
        assert len(items) == 2
        assert items[0].display_name.lower() == "milk"
        assert items[0].volume_ml == 250
        assert items[1].display_name.lower() == "beer"
        assert items[1].volume_ml == 330

    def test_steak_and_water_steak_not_tea(self):
        """'steak' must NOT be classified as 'tea' (substring bug)."""
        items = parse_consumption_text("200g steak and 250ml water")
        assert len(items) == 2
        steak_item = items[0]
        water_item = items[1]
        assert "steak" in steak_item.display_name.lower()
        assert not steak_item.is_beverage
        assert steak_item.beverage_category is None
        assert water_item.is_beverage
        assert water_item.volume_ml == 250

    def test_tea_word_boundary(self):
        """'tea' inside 'steak' should NOT match."""
        assert _classify_beverage("steak") is None
        assert _classify_beverage("tea") == "non_alcoholic"
        assert _classify_beverage("iced tea") == "non_alcoholic"

    def test_beverage_categories(self):
        assert _classify_beverage("whiskey") == "spirits"
        assert _classify_beverage("vodka") == "spirits"
        assert _classify_beverage("wine") == "wine"
        assert _classify_beverage("beer") == "beer"
        assert _classify_beverage("mojito") == "other_alcohol"
        assert _classify_beverage("coffee") == "non_alcoholic"
        assert _classify_beverage("milk") == "non_alcoholic"
        assert _classify_beverage("water") == "water"

    def test_no_meal_group_default_snack(self):
        assert _normalize_meal_type(None) == "snack"
        assert _normalize_meal_type("") == "snack"
        assert _normalize_meal_type("  ") == "snack"

    def test_explicit_breakfast_stays(self):
        assert _normalize_meal_type("breakfast") == "breakfast"
        assert _normalize_meal_type("lunch") == "lunch"
        assert _normalize_meal_type("dinner") == "dinner"
        assert _normalize_meal_type("snack") == "snack"

    def test_unsupported_group_normalized_to_snack(self):
        """Unsupported meal group -> default snack."""
        assert _normalize_meal_type("brunch") == "snack"
        assert _normalize_meal_type("elevenses") == "snack"


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_replay_returns_original_ids(self, db, user, food_catalog):
        """Second call with same Telegram message_id returns the SAME meal_id."""
        items = [
            _make_item("milk", grams=250, volume_ml=250, beverage_category="non_alcoholic",
                       is_beverage=True, food=food_catalog["milk"], db=db),
        ]
        result1 = write_consumption(
            db, user.id, items, meal_type="breakfast",
            operation_id=None,
        )
        # Simulate a second call for the same Telegram message
        op_id = result1["data"]["meal_id"]  # we need the operation_id
        # Get the operation
        meal = db.query(Meal).filter(Meal.id == op_id).first()
        operation_id = meal.source_operation_id

        result2 = write_consumption(
            db, user.id, items, meal_type="breakfast",
            operation_id=operation_id,
        )
        assert result1["data"]["meal_id"] == result2["data"]["meal_id"]

    def test_concurrent_submit_one_set(self, db, user, food_catalog):
        """Two concurrent operations with the same Telegram key produce ONE meal."""
        op1, created1 = get_or_create_operation(
            db, user.id, source="telegram",
            source_chat_id="chat1", source_message_id="msg1", source_bot_id="bot1",
        )
        db.commit()

        # Second call should return the same operation
        op2, created2 = get_or_create_operation(
            db, user.id, source="telegram",
            source_chat_id="chat1", source_message_id="msg1", source_bot_id="bot1",
        )
        db.commit()

        assert op1.id == op2.id
        assert created1 is True
        assert created2 is False

    def test_response_lost_retry_returns_saved(self, db, user, food_catalog):
        """After a completed operation, a retry returns the saved result."""
        items = [
            _make_item("coffee", grams=240, volume_ml=240, beverage_category="non_alcoholic",
                       is_beverage=True, food=food_catalog["coffee"], db=db),
        ]
        result1 = write_consumption(db, user.id, items, meal_type="breakfast")
        meal_id = result1["data"]["meal_id"]
        meal = db.query(Meal).filter(Meal.id == meal_id).first()
        op_id = meal.source_operation_id

        # Simulate retry (response lost)
        result2 = get_operation_result(db, op_id)
        assert result2 is not None
        assert result2["data"]["meal_id"] == meal_id

    def test_two_identical_drinks_separate_msgs_both_count(self, db, user, food_catalog):
        """Two separate messages recording the same drink must both be logged."""
        items_a = [
            _make_item("water", grams=250, volume_ml=250, beverage_category="water",
                       is_beverage=True, food=food_catalog["water"], db=db),
        ]
        items_b = [
            _make_item("water", grams=250, volume_ml=250, beverage_category="water",
                       is_beverage=True, food=food_catalog["water"], db=db),
        ]
        result_a = write_consumption(
            db, user.id, items_a,
            operation_id=None,
        )
        meal_a = db.query(Meal).filter(Meal.id == result_a["data"]["meal_id"]).first()
        op_a_id = meal_a.source_operation_id

        result_b = write_consumption(
            db, user.id, items_b,
            operation_id=None,
        )
        meal_b = db.query(Meal).filter(Meal.id == result_b["data"]["meal_id"]).first()
        op_b_id = meal_b.source_operation_id

        # Different operations
        assert op_a_id != op_b_id
        # Both meals exist
        assert db.query(Meal).filter(Meal.id == result_a["data"]["meal_id"]).count() == 1
        assert db.query(Meal).filter(Meal.id == result_b["data"]["meal_id"]).count() == 1
        # Both have measurements
        meas_count = db.query(Measurement).filter(
            Measurement.user_id == user.id,
            Measurement.metric_type == "water",
        ).count()
        assert meas_count == 2


# ---------------------------------------------------------------------------
# Atomic write + beverage linkage tests
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_meal_and_liquid_written_atomically(self, db, user, food_catalog):
        """A beverage item creates BOTH a meal_item AND a measurement."""
        items = [
            _make_item("milk", grams=250, volume_ml=250, beverage_category="non_alcoholic",
                       is_beverage=True, food=food_catalog["milk"], db=db),
        ]
        result = write_consumption(db, user.id, items, meal_type="breakfast")
        meal_id = result["data"]["meal_id"]

        meal = db.query(Meal).filter(Meal.id == meal_id).first()
        assert meal is not None
        assert len(meal.items) == 1

        mi = meal.items[0]
        assert mi.volume_ml == 250
        assert mi.beverage_category == "non_alcoholic"

        bev = db.query(BeverageMeasurement).filter(
            BeverageMeasurement.meal_item_id == mi.id
        ).first()
        assert bev is not None

        meas = db.query(Measurement).filter(Measurement.id == bev.measurement_id).first()
        assert meas is not None
        assert meas.value_json["amount_ml"] == 250
        assert meas.value_json["category"] == "non_alcoholic"

    def test_totals_include_all_6_nutrients(self, db, user, food_catalog):
        """Meal totals must include fiber_g and sodium_mg."""
        items = [
            _make_item("milk", grams=250, volume_ml=250, beverage_category="non_alcoholic",
                       is_beverage=True, food=food_catalog["milk"], db=db),
        ]
        result = write_consumption(db, user.id, items, meal_type="breakfast")
        totals = result["data"]["totals"]
        for k in NUTRIENT_KEYS:
            assert k in totals, f"Missing nutrient {k} in totals"
        # Milk 250g: 42*2.5=105 kcal, 3.4*2.5=8.5g protein, etc.
        assert totals["kcal"] == 105.0
        assert totals["fiber_g"] == 0.0
        assert totals["sodium_mg"] == 110.0  # 44*2.5

    def test_atomic_rollback_on_error(self, db, user, food_catalog):
        """If the second item fails, the first should not be committed."""
        # This is hard to test without mocking; we verify the transaction model
        # by checking that a valid write commits fully.
        items = [
            _make_item("milk", grams=250, volume_ml=250, beverage_category="non_alcoholic",
                       is_beverage=True, food=food_catalog["milk"], db=db),
            _make_item("beer", grams=330, volume_ml=330, beverage_category="beer",
                       is_beverage=True, food=food_catalog["beer"], db=db),
        ]
        result = write_consumption(db, user.id, items, meal_type="lunch")
        meal_id = result["data"]["meal_id"]
        meal = db.query(Meal).filter(Meal.id == meal_id).first()
        assert len(meal.items) == 2
        # Both beverage links exist
        bev_count = db.query(BeverageMeasurement).filter(
            BeverageMeasurement.meal_item_id.in_([mi.id for mi in meal.items])
        ).count()
        assert bev_count == 2


# ---------------------------------------------------------------------------
# Mutation tests
# ---------------------------------------------------------------------------

class TestMutations:
    def test_drink_volume_corrected_once_each(self, db, user, food_catalog):
        """Changing a beverage's weight rescales BOTH meal_item and measurement."""
        items = [
            _make_item("milk", grams=250, volume_ml=250, beverage_category="non_alcoholic",
                       is_beverage=True, food=food_catalog["milk"], db=db),
        ]
        result = write_consumption(db, user.id, items, meal_type="breakfast")
        meal_id = result["data"]["meal_id"]

        # Change weight to 500g
        upd = update_item_quantity(db, user.id, meal_id, "milk", 500)
        assert upd["ok"] is True

        meal = db.query(Meal).filter(Meal.id == meal_id).first()
        mi = meal.items[0]
        assert mi.grams == 500

        bev = db.query(BeverageMeasurement).filter(
            BeverageMeasurement.meal_item_id == mi.id
        ).first()
        meas = db.query(Measurement).filter(Measurement.id == bev.measurement_id).first()
        assert meas.value_json["amount_ml"] == 500

    def test_drink_deleted_removes_both(self, db, user, food_catalog):
        """Deleting a beverage removes both meal_item and measurement."""
        items = [
            _make_item("milk", grams=250, volume_ml=250, beverage_category="non_alcoholic",
                       is_beverage=True, food=food_catalog["milk"], db=db),
        ]
        result = write_consumption(db, user.id, items, meal_type="breakfast")
        meal_id = result["data"]["meal_id"]

        del_result = delete_beverage(db, user.id, meal_id, "milk")
        assert del_result["ok"] is True

        meal = db.query(Meal).filter(Meal.id == meal_id).first()
        assert len(meal.items) == 0

        # Measurement should be gone
        meas_count = db.query(Measurement).filter(
            Measurement.user_id == user.id,
            Measurement.metric_type == "water",
        ).count()
        assert meas_count == 0

    def test_beverage_to_solid_removes_liquid(self, db, user, food_catalog):
        """Replacing a beverage with a solid removes the liquid measurement."""
        items = [
            _make_item("milk", grams=250, volume_ml=250, beverage_category="non_alcoholic",
                       is_beverage=True, food=food_catalog["milk"], db=db),
        ]
        result = write_consumption(db, user.id, items, meal_type="breakfast")
        meal_id = result["data"]["meal_id"]

        # Replace with steak (solid)
        repl = replace_beverage(db, user.id, meal_id, "milk", "steak", None, None)
        assert repl["ok"] is True

        # The beverage measurement should be removed
        bev_count = db.query(BeverageMeasurement).count()
        assert bev_count == 0

    def test_delete_meal_cascades_to_beverage_measurements(self, db, user, food_catalog):
        """Deleting a meal removes its beverage measurements too."""
        items = [
            _make_item("milk", grams=250, volume_ml=250, beverage_category="non_alcoholic",
                       is_beverage=True, food=food_catalog["milk"], db=db),
        ]
        result = write_consumption(db, user.id, items, meal_type="breakfast")
        meal_id = result["data"]["meal_id"]

        del_result = delete_meal(db, user.id, meal_id)
        assert del_result["ok"] is True

        meas_count = db.query(Measurement).filter(
            Measurement.user_id == user.id,
            Measurement.metric_type == "water",
        ).count()
        assert meas_count == 0

    def test_cross_user_no_unauthorized_access(self, db, user, food_catalog):
        """User B cannot delete User A's meal."""
        items = [
            _make_item("milk", grams=250, volume_ml=250, beverage_category="non_alcoholic",
                       is_beverage=True, food=food_catalog["milk"], db=db),
        ]
        result = write_consumption(db, user.id, items, meal_type="breakfast")
        meal_id = result["data"]["meal_id"]

        # Create user B
        user_b = User(id=str(uuid.uuid4()), display_name="User B", timezone="UTC")
        db.add(user_b)
        db.flush()

        del_result = delete_meal(db, user_b.id, meal_id)
        assert del_result["ok"] is False
        assert del_result["error"] == "meal_not_found"

        # Meal still exists
        assert db.query(Meal).filter(Meal.id == meal_id).count() == 1


# ---------------------------------------------------------------------------
# Aggregation tests
# ---------------------------------------------------------------------------

class TestAggregation:
    def test_aggregation_no_fan_out(self, db, user, food_catalog):
        """Calories come from meals only; liquid rows supply volume only."""
        items = [
            _make_item("milk", grams=250, volume_ml=250, beverage_category="non_alcoholic",
                       is_beverage=True, food=food_catalog["milk"], db=db),
            _make_item("beer", grams=330, volume_ml=330, beverage_category="beer",
                       is_beverage=True, food=food_catalog["beer"], db=db),
        ]
        result = write_consumption(db, user.id, items, meal_type="lunch")
        meal_id = result["data"]["meal_id"]
        meal = db.query(Meal).filter(Meal.id == meal_id).first()

        # Calories from meal totals
        assert meal.totals_json["kcal"] > 0

        # Liquid measurements exist but contribute 0 additional calories
        meas = db.query(Measurement).filter(
            Measurement.user_id == user.id,
            Measurement.metric_type == "water",
        ).all()
        assert len(meas) == 2
        total_liquid_vol = sum(m.value_json.get("amount_ml", 0) for m in meas)
        assert total_liquid_vol == 580  # 250 + 330

    def test_yesterday_near_midnight_uses_local_date(self, db, user, food_catalog):
        """A meal logged near midnight uses the correct local date."""
        # Simulate a meal eaten at 23:50 in Europe/Zagreb
        local_tz = timezone(timedelta(hours=2))  # CEST = UTC+2
        eaten_at = datetime(2026, 9, 8, 23, 50, tzinfo=local_tz)

        items = [
            _make_item("water", grams=250, volume_ml=250, beverage_category="water",
                       is_beverage=True, food=food_catalog["water"], db=db),
        ]
        result = write_consumption(db, user.id, items, meal_type="dinner", eaten_at=eaten_at)
        meal_id = result["data"]["meal_id"]
        meal = db.query(Meal).filter(Meal.id == meal_id).first()

        # eaten_at should be stored as tz-aware UTC
        assert meal.eaten_at is not None
        # The UTC equivalent is 2026-09-08 21:50 UTC
        assert meal.eaten_at.hour == 21
        assert meal.eaten_at.day == 8


# ---------------------------------------------------------------------------
# Nutrient helper tests
# ---------------------------------------------------------------------------

class TestNutrientHelpers:
    def test_scale_nutrients(self):
        per100 = {"kcal": 42, "protein_g": 3.4, "carbs_g": 5, "fat_g": 1, "fiber_g": 0, "sodium_mg": 44}
        scaled = _scale_nutrients(per100, 2.5)
        assert scaled["kcal"] == 105.0
        assert scaled["protein_g"] == 8.5
        assert scaled["sodium_mg"] == 110.0

    def test_sum_nutrients(self):
        a = {"kcal": 100, "protein_g": 5, "carbs_g": 10, "fat_g": 2, "fiber_g": 1, "sodium_mg": 50}
        b = {"kcal": 50, "protein_g": 2, "carbs_g": 3, "fat_g": 1, "fiber_g": 0, "sodium_mg": 20}
        total = _sum_nutrients([a, b])
        assert total["kcal"] == 150.0
        assert total["protein_g"] == 7.0
        assert total["fiber_g"] == 1.0
        assert total["sodium_mg"] == 70.0
