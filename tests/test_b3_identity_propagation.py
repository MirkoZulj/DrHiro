"""B3 regression: Trusted Telegram identity propagation + payload conflict rejection.

Verifies:
1. Identity actually reaches consumption_operations (not dropped).
2. Missing identity → explicit reject (fail closed, not silent new op).
3. Conflicting payload reuse → rejected (same key, different payload).
4. Multi-item messages keep stable per-item discriminators.
"""
from __future__ import annotations

import os
import sys
import uuid

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
    get_or_create_operation,
    log_manual_liquid,
    _compute_payload_hash,
    ParsedItem,
    _scale_nutrients,
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
    """Create all tables from the models (idempotent)."""
    Base.metadata.create_all(engine)
    yield


@pytest.fixture()
def db(engine, tables):
    """Fresh session per test. Cleans up before and rolls back after."""
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
    u = User(id=str(uuid.uuid4()), display_name="Test User", timezone="Europe/Zagreb")
    db.add(u)
    db.flush()
    return u


def _make_item(display_name, *, grams=None, volume_ml=None, beverage_category=None,
               is_beverage=False):
    """Helper to build a ParsedItem with default nutrients."""
    item = ParsedItem(
        display_name=display_name,
        grams=grams,
        volume_ml=volume_ml,
        beverage_category=beverage_category,
        is_beverage=is_beverage,
    )
    item.nutrients_per_100 = {k: 0 for k in NUTRIENT_KEYS}
    item.nutrients_scaled = {k: 0 for k in NUTRIENT_KEYS}
    return item


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestB3IdentityEnforcement:
    """B3: Identity must be present and trusted."""

    def test_telegram_identity_reaches_operation(self, db, user):
        """Identity (chat_id, message_id, bot_id) is stored in the operation."""
        items = [_make_item("milk", grams=250, volume_ml=250,
                            beverage_category="non_alcoholic", is_beverage=True)]
        confirm_consumption(
            db=db, user_id=user.id, items=items, meal_type="breakfast",
            source="telegram",
            source_chat_id="chat_123", source_message_id="msg_456",
            source_bot_id="bot_789",
        )
        # Verify the operation was created with the identity
        op = db.query(ConsumptionOperation).filter(
            ConsumptionOperation.user_id == user.id,
        ).first()
        assert op is not None
        assert op.source_chat_id == "chat_123"
        assert op.source_message_id == "msg_456"
        assert op.source_bot_id == "bot_789"
        assert op.source == "telegram"

    def test_missing_telegram_identity_rejected(self, db, user):
        """Missing telegram identity → ValueError (fail closed)."""
        items = [_make_item("milk", grams=250, volume_ml=250,
                            beverage_category="non_alcoholic", is_beverage=True)]
        with pytest.raises(ValueError, match="telegram_source_requires_identity"):
            confirm_consumption(
                db=db, user_id=user.id, items=items, meal_type="breakfast",
                source="telegram",
                # Missing identity
            )
        # No operation should have been created
        count = db.query(ConsumptionOperation).count()
        assert count == 0

    def test_non_telegram_without_idempotency_key_rejected(self, db, user):
        """Non-Telegram source without idempotency_key → ValueError."""
        items = [_make_item("milk", grams=250, volume_ml=250,
                            beverage_category="non_alcoholic", is_beverage=True)]
        with pytest.raises(ValueError, match="missing_identity"):
            confirm_consumption(
                db=db, user_id=user.id, items=items, meal_type="breakfast",
                source="api",
                # No idempotency_key
            )
        count = db.query(ConsumptionOperation).count()
        assert count == 0

    def test_idempotency_key_non_telegram_works(self, db, user):
        """Non-Telegram entry points dedupe via caller-supplied idempotency_key."""
        items = [_make_item("milk", grams=250, volume_ml=250,
                            beverage_category="non_alcoholic", is_beverage=True)]
        key = "my-service:order-123"
        r1 = confirm_consumption(
            db=db, user_id=user.id, items=items, meal_type="breakfast",
            source="api", idempotency_key=key,
        )
        r2 = confirm_consumption(
            db=db, user_id=user.id, items=items, meal_type="breakfast",
            source="api", idempotency_key=key,
        )
        assert r1["data"]["meal_id"] == r2["data"]["meal_id"]
        assert db.query(Meal).filter(Meal.user_id == user.id).count() == 1


class TestB3PayloadConflict:
    """B3: Conflicting payload reuse is rejected."""

    def test_conflicting_payload_same_telegram_key_rejected(self, db, user):
        """Same Telegram identity with different payload → rejected."""
        items_a = [_make_item("milk", grams=250, volume_ml=250,
                              beverage_category="non_alcoholic", is_beverage=True)]
        items_b = [_make_item("coffee", grams=240, volume_ml=240,
                              beverage_category="non_alcoholic", is_beverage=True)]

        # First call succeeds
        r1 = confirm_consumption(
            db=db, user_id=user.id, items=items_a, meal_type="breakfast",
            source="telegram",
            source_chat_id="chat1", source_message_id="msg1", source_bot_id="bot1",
        )
        assert r1["ok"] is True

        # Second call with same key but different payload → must raise
        with pytest.raises(ValueError, match="conflicting_payload_reuse"):
            confirm_consumption(
                db=db, user_id=user.id, items=items_b, meal_type="breakfast",
                source="telegram",
                source_chat_id="chat1", source_message_id="msg1", source_bot_id="bot1",
            )

    def test_conflicting_payload_same_idempotency_key_rejected(self, db, user):
        """Same idempotency_key with different payload → rejected."""
        items_a = [_make_item("milk", grams=250, volume_ml=250,
                              beverage_category="non_alcoholic", is_beverage=True)]
        items_b = [_make_item("water", grams=300, volume_ml=300,
                              beverage_category="water", is_beverage=True)]

        r1 = confirm_consumption(
            db=db, user_id=user.id, items=items_a, meal_type="breakfast",
            source="api", idempotency_key="key-abc",
        )
        assert r1["ok"] is True

        with pytest.raises(ValueError, match="conflicting_payload_reuse"):
            confirm_consumption(
                db=db, user_id=user.id, items=items_b, meal_type="breakfast",
                source="api", idempotency_key="key-abc",
            )

    def test_same_payload_same_key_returns_same_result(self, db, user):
        """Same identity + same payload → idempotent replay."""
        items = [_make_item("milk", grams=250, volume_ml=250,
                            beverage_category="non_alcoholic", is_beverage=True)]
        r1 = confirm_consumption(
            db=db, user_id=user.id, items=items, meal_type="breakfast",
            source="telegram",
            source_chat_id="chat1", source_message_id="msg1", source_bot_id="bot1",
        )
        r2 = confirm_consumption(
            db=db, user_id=user.id, items=items, meal_type="breakfast",
            source="telegram",
            source_chat_id="chat1", source_message_id="msg1", source_bot_id="bot1",
        )
        assert r1["data"]["meal_id"] == r2["data"]["meal_id"]
        assert db.query(Meal).filter(Meal.user_id == user.id).count() == 1


class TestB3MultiItemDiscriminators:
    """B3: Multi-item messages keep stable per-item discriminators."""

    def test_multi_item_message_each_item_has_unique_key(self, db, user):
        """A multi-item message creates consumption_items with unique keys."""
        items = [
            _make_item("milk", grams=250, volume_ml=250,
                       beverage_category="non_alcoholic", is_beverage=True),
            _make_item("steak", grams=200, is_beverage=False),
        ]
        result = confirm_consumption(
            db=db, user_id=user.id, items=items, meal_type="lunch",
            source="telegram",
            source_chat_id="chat1", source_message_id="multi1", source_bot_id="bot1",
        )
        assert result["ok"] is True
        meal_id = result["data"]["meal_id"]
        meal = db.query(Meal).filter(Meal.id == meal_id).first()
        assert len(meal.items) == 2

        # Two distinct consumption_items
        ops = db.query(ConsumptionOperation).filter(
            ConsumptionOperation.user_id == user.id,
        ).all()
        assert len(ops) == 1
        cis = db.query(ConsumptionItem).filter(
            ConsumptionItem.operation_id == ops[0].id,
        ).all()
        assert len(cis) == 2
        # Each has a unique item_key
        keys = {ci.item_key for ci in cis}
        assert len(keys) == 2

    def test_replayed_multi_item_same_keys(self, db, user):
        """Replay of a multi-item message does NOT collapse items."""
        items = [
            _make_item("milk", grams=250, volume_ml=250,
                       beverage_category="non_alcoholic", is_beverage=True),
            _make_item("steak", grams=200, is_beverage=False),
        ]
        r1 = confirm_consumption(
            db=db, user_id=user.id, items=items, meal_type="lunch",
            source="telegram",
            source_chat_id="chat1", source_message_id="multi1", source_bot_id="bot1",
        )
        r2 = confirm_consumption(
            db=db, user_id=user.id, items=items, meal_type="lunch",
            source="telegram",
            source_chat_id="chat1", source_message_id="multi1", source_bot_id="bot1",
        )
        assert r1["data"]["meal_id"] == r2["data"]["meal_id"]
        # Still only 2 consumption_items (no duplicates on replay)
        cis = db.query(ConsumptionItem).filter(
            ConsumptionItem.user_id == user.id,
        ).all()
        assert len(cis) == 2


class TestB3LogManualLiquidIdentity:
    """B3: log_manual_liquid enforces identity."""

    def test_liquid_log_missing_identity_rejected(self, db, user):
        """log_manual_liquid with telegram source but no identity → explicit error."""
        result = log_manual_liquid(
            db=db, user_id=user.id, amount_ml=250, category="water",
            source="telegram",
            # Missing identity
        )
        assert result["ok"] is False
        assert result.get("error") == "identity_required"

    def test_liquid_log_with_idempotency_key_works(self, db, user):
        """log_manual_liquid with idempotency_key works."""
        r1 = log_manual_liquid(
            db=db, user_id=user.id, amount_ml=250, category="water",
            source="api", idempotency_key="water-key-1",
        )
        assert r1["ok"] is True
        r2 = log_manual_liquid(
            db=db, user_id=user.id, amount_ml=250, category="water",
            source="api", idempotency_key="water-key-1",
        )
        # Replay returns the saved result
        assert r2["ok"] is True
        assert r1["data"]["meal_id"] == r2["data"]["meal_id"]

    def test_liquid_log_new_intent_without_identity_accepted(self, db, user):
        """intent='new' with telegram source but no identity → accepted (Qodo #7)."""
        result = log_manual_liquid(
            db=db, user_id=user.id, amount_ml=330, category="coffee",
            source="telegram",
            intent="new",
            display_name="morning coffee",
        )
        assert result["ok"] is True
        assert result.get("data") is not None
        # Verify a consumption was actually written
        meal_id = result["data"].get("meal_id")
        assert meal_id is not None
        items = result["data"].get("items", [])
        assert len(items) >= 1
        assert items[0].get("volume_ml") == 330
        # Verify it was persisted to the database
        meal = db.query(Meal).filter(Meal.id == meal_id).first()
        assert meal is not None

    def test_liquid_log_missing_identity_still_requires_identity(self, db, user):
        """No identity AND no intent='new' → identity_required (regression guard)."""
        for bad_intent in (None, "edit", "update", "delete"):
            result = log_manual_liquid(
                db=db, user_id=user.id, amount_ml=250, category="water",
                source="telegram",
                intent=bad_intent,
            )
            assert result["ok"] is False, f"intent={bad_intent!r} should be rejected"
            assert result.get("error") == "identity_required"
