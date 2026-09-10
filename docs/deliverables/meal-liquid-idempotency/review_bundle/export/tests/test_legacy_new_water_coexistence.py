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
    log_manual_liquid,
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
        # Child rows referencing users must be cleared before users (FK order).
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

    def test_same_event_meal_plus_liquid_one_consumption(self, db, user, food_catalog):
        """Same Telegram message → meal tool call AND liquid tool call for the
        SAME drink → ONE consumption (volume once, calories once).

        The meal confirm and the manual-liquid log carry the same source
        identity (chat_id + message_id + bot_id). The second call must replay
        the saved result — no new row, no double count."""

        # Meal-tool call: beer with lunch (message msg100)
        items = [
            _make_item("beer", grams=330, volume_ml=330, beverage_category="beer",
                       is_beverage=True, food=food_catalog["beer"], db=db),
        ]
        r1 = confirm_consumption(
            db=db, user_id=user.id, items=items, meal_type="lunch",
            source="telegram", source_chat_id="chat1",
            source_message_id="msg100", source_bot_id="bot1",
        )
        assert r1["ok"] is True

        # Liquid-tool call for the SAME message identity (same chat+msg+bot).
        r2 = log_manual_liquid(
            db=db, user_id=user.id, amount_ml=330, category="beer",
            source="telegram", source_chat_id="chat1",
            source_message_id="msg100", source_bot_id="bot1",
        )
        assert r2["ok"] is True

        db.commit()

        # Same meal_id, no new meal, no new beverage.
        assert r2["data"]["meal_id"] == r1["data"]["meal_id"]
        assert db.query(Meal).filter(Meal.user_id == user.id).count() == 1
        rows = db.query(Measurement).filter(
            Measurement.user_id == user.id, Measurement.metric_type == "water",
        ).all()
        beer_rows = [m for m in rows if (m.value_json or {}).get("category") == "beer"]
        total_beer_ml = sum(m.value_json.get("amount_ml", 0) for m in beer_rows)
        # ONE beer, not two.
        assert total_beer_ml == 330

    def test_retry_returns_saved_result(self, db, user, food_catalog):
        """Retry of the same operation returns the existing result, NO new
        contribution."""
        items = [
            _make_item("beer", grams=330, volume_ml=330, beverage_category="beer",
                       is_beverage=True, food=food_catalog["beer"], db=db),
        ]
        r1 = confirm_consumption(
            db=db, user_id=user.id, items=items, meal_type="lunch",
            source="telegram", source_chat_id="chat1",
            source_message_id="msgRetry", source_bot_id="bot1",
        )
        assert r1["ok"] is True
        original_meal_id = r1["data"]["meal_id"]

        # Retry with same source identity.
        r2 = log_manual_liquid(
            db=db, user_id=user.id, amount_ml=330, category="beer",
            source="telegram", source_chat_id="chat1",
            source_message_id="msgRetry", source_bot_id="bot1",
        )
        assert r2["ok"] is True
        assert r2["data"]["meal_id"] == original_meal_id

        db.commit()
        assert db.query(Meal).filter(Meal.user_id == user.id).count() == 1
        assert db.query(Measurement).filter(
            Measurement.user_id == user.id, Measurement.metric_type == "water",
        ).count() == 1

    def test_reconcile_existing_item_links_once(self, db, user, food_catalog):
        """'also count that milk as liquid' → explicit reference to existing
        milk item → volume linked to existing item once; dashboard sum shows
        it once, not twice."""
        # First: a meal with milk (message msg200)
        items = [
            _make_item("milk", grams=250, volume_ml=250, beverage_category="non_alcoholic",
                       is_beverage=True, food=food_catalog["beer"], db=db),
        ]
        r1 = confirm_consumption(
            db=db, user_id=user.id, items=items, meal_type="lunch",
            source="telegram", source_chat_id="chat1",
            source_message_id="msg200", source_bot_id="bot1",
        )
        assert r1["ok"] is True
        milk_meas_id = r1["data"]["items"][0]["measurement_id"]

        # Reconciliation: "also count that milk as liquid" → explicit reference.
        r2 = log_manual_liquid(
            db=db, user_id=user.id, amount_ml=100, category="non_alcoholic",
            existing_item_id=milk_meas_id,
        )
        assert r2["ok"] is True
        assert r2["data"]["reconciled"] is True
        # Volume added to existing measurement (250 + 100 = 350).
        assert r2["data"]["total_amount_ml"] == 350

        db.commit()

        # Only ONE measurement row for this drink.
        rows = db.query(Measurement).filter(
            Measurement.user_id == user.id, Measurement.metric_type == "water",
        ).all()
        non_alc_rows = [m for m in rows if (m.value_json or {}).get("category") == "non_alcoholic"]
        total_ml = sum(m.value_json.get("amount_ml", 0) for m in non_alc_rows)
        # 350 total (250 original + 100 reconciled), NOT 250 + 250 = 500.
        assert total_ml == 350
        assert len(non_alc_rows) == 1

    def test_genuinely_new_drink_additional_volume_and_calories(self, db, user, food_catalog):
        """'another glass of milk' in a SEPARATE message → NEW consumption with
        additional volume AND calories; both count as two distinct items."""
        # First drink: milk in message msg300
        items1 = [
            _make_item("milk", grams=250, volume_ml=250, beverage_category="non_alcoholic",
                       is_beverage=True, food=food_catalog["beer"], db=db),
        ]
        r1 = confirm_consumption(
            db=db, user_id=user.id, items=items1, meal_type="lunch",
            source="telegram", source_chat_id="chat1",
            source_message_id="msg300", source_bot_id="bot1",
        )
        assert r1["ok"] is True

        # Second drink: "another glass of milk" — new message, intent=new.
        items2 = [
            _make_item("milk", grams=200, volume_ml=200, beverage_category="non_alcoholic",
                       is_beverage=True, food=food_catalog["beer"], db=db),
        ]
        r2 = log_manual_liquid(
            db=db, user_id=user.id, amount_ml=200, category="non_alcoholic",
            source="telegram", source_chat_id="chat1",
            source_message_id="msg301", source_bot_id="bot1",
            intent="new", items=items2,
        )
        assert r2["ok"] is True

        db.commit()

        # Two distinct meals, two distinct measurements.
        assert db.query(Meal).filter(Meal.user_id == user.id).count() == 2
        rows = db.query(Measurement).filter(
            Measurement.user_id == user.id, Measurement.metric_type == "water",
        ).all()
        non_alc_rows = [m for m in rows if (m.value_json or {}).get("category") == "non_alcoholic"]
        total_ml = sum(m.value_json.get("amount_ml", 0) for m in non_alc_rows)
        # 250 + 200 = 450 (two distinct drinks).
        assert total_ml == 450
        assert len(non_alc_rows) == 2

    def test_ambiguous_intent_clarifies(self, db, user):
        """Ambiguous intent (no source identity, no reference, no intent=new)
        → CLARIFY response, nothing written.

        Uses source="api" (non-telegram) to test the ambiguous path: telegram
        source without identity is now rejected fail-closed by B3."""
        r = log_manual_liquid(
            db=db, user_id=user.id, amount_ml=250, category="water",
            source="api",
        )
        assert r["ok"] is False
        assert r.get("clarify") is True

        db.rollback()
        # Nothing written.
        assert db.query(Meal).filter(Meal.user_id == user.id).count() == 0
        assert db.query(Measurement).filter(
            Measurement.user_id == user.id, Measurement.metric_type == "water",
        ).count() == 0

    def test_standalone_caloric_drink_contributes_nutrition(self, db, user, food_catalog):
        """Standalone caloric drink via manual path → contributes NUTRITION once
        (kcal present, not a bare water row with 0 kcal)."""
        # Milk with real nutrients.
        milk_item = _make_item(
            "milk", grams=250, volume_ml=250, beverage_category="non_alcoholic",
            is_beverage=True, food=food_catalog["beer"], db=db,
        )
        # Genuinely new drink with intent="new" and no source identity.
        # Uses source="api" (non-telegram) so the ambiguous guard does not apply;
        # telegram source without identity is rejected fail-closed by B3.
        r = log_manual_liquid(
            db=db, user_id=user.id, amount_ml=250, category="non_alcoholic",
            source="api",
            intent="new", items=[milk_item],
        )
        assert r["ok"] is True

        db.commit()

        # The meal totals must reflect real kcal (not zero).
        meal = db.query(Meal).filter(Meal.user_id == user.id).first()
        assert meal is not None
        totals = meal.totals_json
        assert totals.get("kcal", 0) > 0, f"Expected kcal > 0, got {totals}"

        # The measurement row exists with volume.
        meas = db.query(Measurement).filter(
            Measurement.user_id == user.id, Measurement.metric_type == "water",
        ).first()
        assert meas is not None
        assert meas.value_json.get("amount_ml") == 250

    def test_same_drink_cannot_yield_two_rows_in_sum(self, db, user, food_catalog):
        """Same drink cannot produce two rows in the dashboard liquid sum
        across legacy + new paths. Attempting to log the same (operation, item)
        twice through both mechanisms must not double-count."""
        # Log via new path (confirm_consumption) with source identity.
        items = [
            _make_item("beer", grams=330, volume_ml=330, beverage_category="beer",
                       is_beverage=True, food=food_catalog["beer"], db=db),
        ]
        r1 = confirm_consumption(
            db=db, user_id=user.id, items=items, meal_type="lunch",
            source="telegram", source_chat_id="chat1",
            source_message_id="msgDedup", source_bot_id="bot1",
        )
        assert r1["ok"] is True

        # Attempt to log the SAME drink again via the manual-liquid path with
        # the SAME source identity → must replay, not insert.
        r2 = log_manual_liquid(
            db=db, user_id=user.id, amount_ml=330, category="beer",
            source="telegram", source_chat_id="chat1",
            source_message_id="msgDedup", source_bot_id="bot1",
        )
        assert r2["ok"] is True
        assert r2["data"]["meal_id"] == r1["data"]["meal_id"]

        db.commit()

        # The metric_type=water sum must show ONE beer (330ml), not two.
        rows = db.query(Measurement).filter(
            Measurement.user_id == user.id, Measurement.metric_type == "water",
        ).all()
        beer_rows = [m for m in rows if (m.value_json or {}).get("category") == "beer"]
        total_beer_ml = sum(m.value_json.get("amount_ml", 0) for m in beer_rows)
        assert total_beer_ml == 330
        assert len(beer_rows) == 1

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
