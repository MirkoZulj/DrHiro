"""Test that meal-confirmation selection indices are validated (Qodo #6).

Proves that an out-of-range selection index is rejected and no empty
zero-total meal row is committed.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

TEST_DB_URL = os.environ.get(
    "DRHIRO_TEST_DB_URL",
    "postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_test",
)

# Skip unless explicitly opted in (same pattern as test_r1_nutrition_resolution).
pytestmark = pytest.mark.skipif(
    os.environ.get("DRHIRO_R1R2_ALEMBIC_DB") != "1",
    reason="requires an Alembic-built DB + Redis; set DRHIRO_R1R2_ALEMBIC_DB=1",
)


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    S = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = S()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture()
def user(db):
    uid = uuid.uuid4()
    db.execute(
        text("INSERT INTO users (id, display_name, timezone, locale, status, created_at, updated_at) "
             "VALUES (:id, 'TestU', 'UTC', 'en', 'active', NOW(), NOW())"),
        {"id": str(uid)},
    )
    db.commit()
    return {"id": str(uid)}


class FakeRedis:
    """Minimal Redis stand-in for draft storage."""
    def __init__(self):
        self._store = {}

    def set(self, key, value):
        self._store[key] = value

    def get(self, key):
        return self._store.get(key)

    def delete(self, key):
        self._store.pop(key, None)


def _import_service():
    """Import the live intelligent-meal service module."""
    from importlib import import_module
    # Ensure the service module uses our test DB and fake Redis
    os.environ.setdefault("DATABASE_URL", TEST_DB_URL)
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    os.environ.setdefault("DRHIRO_JWT_SECRET", "pytest-dummy-not-a-real-secret")
    os.environ.setdefault("DRHIRO_SERVICE_TOKEN", "")
    os.environ.setdefault("DRHIRO_TELEGRAM_ID", "")

    # Add the service directory to sys.path
    svc_dir = os.path.join(os.path.dirname(__file__), "..", "services", "intelligent-meal")
    if svc_dir not in sys.path:
        sys.path.insert(0, svc_dir)

    mod = import_module("service")
    return mod


def test_out_of_range_selection_rejected(db, user):
    """An out-of-range selection index must be rejected with 422 and no meal row created."""
    import json
    svc = _import_service()

    # Patch the service's redis_client with our fake
    fake_redis = FakeRedis()
    svc.redis_client = fake_redis

    # Patch SessionLocal to use our test DB
    svc.SessionLocal = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)

    # Create a draft with one item that has 2 candidates
    draft_id = uuid.uuid4().hex
    draft = {
        "user_id": user["id"],
        "text": "200 g chicken breast",
        "eaten_at": datetime.now(timezone.utc).isoformat(),
        "meal_type": "lunch",
        "items": [
            {
                "fragment": "chicken breast",
                "grams": 200.0,
                "candidates": [
                    {"display_name": "Chicken Breast", "kcal_per_100g": 165.0,
                     "protein_g_per_100g": 31.0, "carbs_g_per_100g": 0.0,
                     "fat_g_per_100g": 3.6, "fiber_g_per_100g": 0.0,
                     "sodium_mg_per_100g": 74.0, "confidence": 0.95,
                     "source": "usda"},
                    {"display_name": "Chicken Thigh", "kcal_per_100g": 200.0,
                     "protein_g_per_100g": 26.0, "carbs_g_per_100g": 0.0,
                     "fat_g_per_100g": 10.0, "fiber_g_per_100g": 0.0,
                     "sodium_mg_per_100g": 80.0, "confidence": 0.7,
                     "source": "usda"},
                ],
            }
        ],
    }
    fake_redis.set(f"intelligent_draft:{draft_id}", json.dumps(draft))

    # Count meals before
    meals_before = db.execute(
        text("SELECT COUNT(*) FROM meals WHERE user_id = :uid"),
        {"uid": user["id"]},
    ).scalar()

    # Try to confirm with an out-of-range selection (index 5, but only 2 candidates)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(svc.confirm_meal(
                req=svc.ConfirmMealRequest(draft_id=draft_id, selections=[5]),
                user=user,
                db=db,
            ))
        finally:
            loop.close()

    assert exc.value.status_code == 422
    assert "out of range" in exc.value.detail.lower()

    # Verify NO meal was created
    meals_after = db.execute(
        text("SELECT COUNT(*) FROM meals WHERE user_id = :uid"),
        {"uid": user["id"]},
    ).scalar()
    assert meals_after == meals_before, "A meal row was committed despite invalid selection"


def test_user_b_cannot_alter_user_a_meal_set_weight(db):
    """User B cannot alter User A's meal via set-weight endpoint (Qodo #2)."""
    import json
    svc = _import_service()
    fake_redis = FakeRedis()
    svc.redis_client = fake_redis
    svc.SessionLocal = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)

    # Create two users
    uid_a = uuid.uuid4()
    uid_b = uuid.uuid4()
    db.execute(
        text("INSERT INTO users (id, display_name, timezone, locale, status, created_at, updated_at) "
             "VALUES (:id, 'UserA', 'UTC', 'en', 'active', NOW(), NOW())"),
        {"id": str(uid_a)},
    )
    db.execute(
        text("INSERT INTO users (id, display_name, timezone, locale, status, created_at, updated_at) "
             "VALUES (:id, 'UserB', 'UTC', 'en', 'active', NOW(), NOW())"),
        {"id": str(uid_b)},
    )
    db.commit()

    # User A creates a meal with one item
    mid = uuid.uuid4()
    db.execute(
        text("INSERT INTO meals (id, user_id, eaten_at, meal_type, status, input_method, notes, totals_json, confidence, created_at, updated_at) "
             "VALUES (:mid, :uid, NOW(), 'lunch', 'confirmed', 'test', 'test meal', '{\"kcal\": 500}', 1.0, NOW(), NOW())"),
        {"mid": str(mid), "uid": str(uid_a)},
    )
    db.execute(
        text("INSERT INTO meal_items (id, meal_id, display_name, quantity, grams, nutrients_json, source, confidence, user_corrected, created_at, updated_at) "
             "VALUES (:iid, :mid, 'Chicken Breast', 1.0, 200, '{\"kcal\": 330}', 'test', 1.0, FALSE, NOW(), NOW())"),
        {"iid": str(uuid.uuid4()), "mid": str(mid)},
    )
    db.commit()

    import asyncio
    loop = asyncio.new_event_loop()
    try:
        # User B tries to set weight on User A's meal
        result = loop.run_until_complete(svc.set_meal_item_weight(
            meal_id=str(mid),
            req=svc.SetWeightRequest(text="chicken", grams=300),
            user={"id": str(uid_b), "telegram_id": "999"},
            db=db,
        ))
    finally:
        loop.close()

    # Should be rejected (meal_not_found from User B's perspective)
    assert result.get("ok") is False, f"User B was able to modify User A's meal: {result}"
    assert result.get("error") == "meal_not_found", f"Expected meal_not_found, got: {result}"

    # Verify the meal was NOT modified
    db.expire_all()
    meal = db.execute(text("SELECT totals_json FROM meals WHERE id = :mid"), {"mid": str(mid)}).fetchone()
    assert meal is not None, "Meal was deleted"
    totals = json.loads(meal[0]) if isinstance(meal[0], str) else meal[0]
    assert totals.get("kcal") == 500, f"Meal totals were modified: {totals}"


def test_user_b_cannot_alter_user_a_meal_set_custom(db):
    """User B cannot alter User A's meal via set-custom endpoint (Qodo #2)."""
    import json
    svc = _import_service()
    fake_redis = FakeRedis()
    svc.redis_client = fake_redis
    svc.SessionLocal = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)

    # Create two users
    uid_a = uuid.uuid4()
    uid_b = uuid.uuid4()
    db.execute(
        text("INSERT INTO users (id, display_name, timezone, locale, status, created_at, updated_at) "
             "VALUES (:id, 'UserA', 'UTC', 'en', 'active', NOW(), NOW())"),
        {"id": str(uid_a)},
    )
    db.execute(
        text("INSERT INTO users (id, display_name, timezone, locale, status, created_at, updated_at) "
             "VALUES (:id, 'UserB', 'UTC', 'en', 'active', NOW(), NOW())"),
        {"id": str(uid_b)},
    )
    db.commit()

    # User A creates a meal with one item
    mid = uuid.uuid4()
    db.execute(
        text("INSERT INTO meals (id, user_id, eaten_at, meal_type, status, input_method, notes, totals_json, confidence, created_at, updated_at) "
             "VALUES (:mid, :uid, NOW(), 'lunch', 'confirmed', 'test', 'test meal', '{\"kcal\": 500}', 1.0, NOW(), NOW())"),
        {"mid": str(mid), "uid": str(uid_a)},
    )
    db.execute(
        text("INSERT INTO meal_items (id, meal_id, display_name, quantity, grams, nutrients_json, source, confidence, user_corrected, created_at, updated_at) "
             "VALUES (:iid, :mid, 'Chicken Breast', 1.0, 200, '{\"kcal\": 330}', 'test', 1.0, FALSE, NOW(), NOW())"),
        {"iid": str(uuid.uuid4()), "mid": str(mid)},
    )
    db.commit()

    # Build a fake request for set_custom_nutrition
    from starlette.requests import Request
    from io import BytesIO

    async def async_bytesio():
        return BytesIO(b"")

    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/meals/{mid}/items/set-custom",
        "query_string": b"kcal_per_100g=999&protein_per_100g=0&carbs_per_100g=0&fat_per_100g=0&fiber_per_100g=0&sodium_per_100g=0",
        "headers": [],
    }
    request = Request(scope)

    import asyncio
    loop = asyncio.new_event_loop()
    try:
        # User B tries to set custom nutrition on User A's meal
        result = loop.run_until_complete(svc.set_custom_nutrition(
            meal_id=str(mid),
            request=request,
            req=svc.MealIn(text="chicken"),
            user={"id": str(uid_b), "telegram_id": "999"},
            db=db,
        ))
    finally:
        loop.close()

    # Should be rejected (meal_not_found from User B's perspective)
    assert result.get("ok") is False, f"User B was able to modify User A's meal: {result}"
    assert result.get("error") == "meal_not_found", f"Expected meal_not_found, got: {result}"

    # Verify the meal was NOT modified
    db.expire_all()
    meal = db.execute(text("SELECT totals_json FROM meals WHERE id = :mid"), {"mid": str(mid)}).fetchone()
    assert meal is not None, "Meal was deleted"
    totals = json.loads(meal[0]) if isinstance(meal[0], str) else meal[0]
    assert totals.get("kcal") == 500, f"Meal totals were modified: {totals}"


def test_negative_selection_rejected(db, user):
    """A negative selection index must be rejected."""
    import json
    svc = _import_service()

    fake_redis = FakeRedis()
    svc.redis_client = fake_redis
    svc.SessionLocal = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)

    draft_id = uuid.uuid4().hex
    draft = {
        "user_id": user["id"],
        "text": "test food",
        "eaten_at": datetime.now(timezone.utc).isoformat(),
        "meal_type": "snack",
        "items": [
            {
                "fragment": "test food",
                "grams": 100.0,
                "candidates": [
                    {"display_name": "Test Food", "kcal_per_100g": 100.0,
                     "protein_g_per_100g": 10.0, "carbs_g_per_100g": 20.0,
                     "fat_g_per_100g": 5.0, "fiber_g_per_100g": 2.0,
                     "sodium_mg_per_100g": 50.0, "confidence": 0.9,
                     "source": "test"},
                ],
            }
        ],
    }
    fake_redis.set(f"intelligent_draft:{draft_id}", json.dumps(draft))

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(svc.confirm_meal(
                req=svc.ConfirmMealRequest(draft_id=draft_id, selections=[-1]),
                user=user,
                db=db,
            ))
        finally:
            loop.close()

    assert exc.value.status_code == 422
    assert "negative" in exc.value.detail.lower()
