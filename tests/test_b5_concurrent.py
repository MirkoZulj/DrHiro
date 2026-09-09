"""Genuine concurrent first-creation test for B5.

Verifies that when two separate DB sessions attempt first creation
simultaneously for the same Telegram identity, exactly one operation row is
created (INSERT ... ON CONFLICT DO NOTHING), and both callers converge on the
same saved meal result.

This test uses threading.Barrier for synchronization and separate sessions
per thread, connecting directly to PostgreSQL so the ON CONFLICT behavior is
exercised.
"""
from __future__ import annotations

import os
import sys
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))

from drhiro_api.models import Base, User, Food, DataSource, Nutrient, FoodNutrient
from drhiro_api.services.consumption import (
    confirm_consumption,
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
    u = User(id=str(uuid.uuid4()), display_name="Concurrent User", timezone="Europe/Zagreb")
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
        "milk":   ("Milk, whole",       True,  (42, 3.4, 5, 1, 0, 44)),
        "coffee": ("Coffee, brewed",    True,  (2, 0.1, 0, 0, 0, 2)),
        "water":  ("Water, tap",        True,  (0, 0, 0, 0, 0, 0)),
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


class TestB5ConcurrentFirstCreation:
    """Genuine concurrent first creation: two sessions, barrier, PostgreSQL ON CONFLICT."""

    def test_concurrent_first_creation_two_sessions_barrier(self, engine, user, food_catalog):
        """Two threads, each with its own session, attempt first creation simultaneously.

        Uses a threading.Barrier to synchronize both threads right before the INSERT.
        The atomic INSERT ... ON CONFLICT DO NOTHING ensures exactly one operation row
        is created; both callers converge on the same op.id and same saved meal result.
        """
        SessionLocal = sessionmaker(bind=engine)

        # Build item data dict (serializable across threads)
        milk_food = food_catalog["milk"]
        n100 = {}
        for fn in milk_food.nutrients:
            code = fn.nutrient.nutrient_code
            amt = fn.amount_per_100g
            if code == "energy": n100["kcal"] = amt
            elif code == "protein": n100["protein_g"] = amt
            elif code == "carbs": n100["carbs_g"] = amt
            elif code == "fat": n100["fat_g"] = amt
            elif code == "fiber": n100["fiber_g"] = amt
            elif code == "sodium": n100["sodium_mg"] = amt
        factor = 250 / 100.0
        scaled = _scale_nutrients(n100, factor)

        parsed_item_data = {
            "display_name": "milk", "grams": 250, "volume_ml": 250,
            "beverage_category": "dairy", "is_beverage": True,
            "nutrients_per_100": n100, "nutrients_scaled": scaled,
            "source": "db", "confidence": 0.9,
        }

        results = []
        errors = []
        lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=10)

        def worker():
            try:
                db = SessionLocal()
                try:
                    # Synchronize both threads here so they race on the INSERT
                    barrier.wait()

                    result = confirm_consumption(
                        db=db,
                        user_id=user.id,
                        items=[ParsedItem(**parsed_item_data)],
                        meal_type="breakfast",
                        source="telegram",
                        source_chat_id="concurrent-chat",
                        source_message_id="concurrent-msg",
                        source_bot_id="concurrent-bot",
                        raw_text="250ml milk",
                    )
                    db.commit()
                    with lock:
                        results.append(result)
                finally:
                    db.close()
            except Exception as e:
                with lock:
                    errors.append(e)

        t0 = threading.Thread(target=worker)
        t1 = threading.Thread(target=worker)
        t0.start()
        t1.start()
        t0.join(timeout=15)
        t1.join(timeout=15)

        # No errors
        assert len(errors) == 0, f"Thread errors: {errors}"

        # Both got a result
        assert len(results) == 2, f"Expected 2 results, got {len(results)}"

        # Both returned ok
        assert results[0]["ok"] is True
        assert results[1]["ok"] is True

        # Both callers got the SAME meal_id
        meal_id_0 = results[0]["data"]["meal_id"]
        meal_id_1 = results[1]["data"]["meal_id"]
        assert meal_id_0 == meal_id_1, f"Meal IDs differ: {meal_id_0} vs {meal_id_1}"

        # Verify in DB: exactly ONE operation row and ONE meal
        verify_session = SessionLocal()
        from drhiro_api.models import ConsumptionOperation, Meal, Measurement
        op_count = verify_session.query(ConsumptionOperation).filter(
            ConsumptionOperation.user_id == user.id,
            ConsumptionOperation.source_bot_id == "concurrent-bot",
            ConsumptionOperation.source_chat_id == "concurrent-chat",
            ConsumptionOperation.source_message_id == "concurrent-msg",
        ).count()
        meal_count = verify_session.query(Meal).filter(Meal.user_id == user.id).count()
        meas_count = verify_session.query(Measurement).filter(
            Measurement.user_id == user.id
        ).count()

        verify_session.close()

        assert op_count == 1, f"Expected 1 operation, got {op_count}"
        assert meal_count == 1, f"Expected 1 meal, got {meal_count}"
        assert meas_count == 1, f"Expected 1 measurement, got {meas_count}"
