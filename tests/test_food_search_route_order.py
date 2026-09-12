"""Verify the food-search route is not shadowed by the generic /{meal_id} route.

Before the fix, GET /api/v1/meals/foods/search was captured as meal_id="foods"
by the two-segment route, causing a UUID parse failure (422/500) instead of
returning catalogue results. FastAPI matches routes in declaration order.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))

from drhiro_api.main import app
from drhiro_api.deps import get_current_user
from drhiro_api.models import User


@pytest.fixture()
def client():
    def _get_db():
        yield None

    from drhiro_api.db import get_db
    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_food_search_not_shadowed(client):
    """GET /api/v1/meals/foods/search must return 200, not a UUID parse error."""
    fake_user = User(id=str(uuid.uuid4()), display_name="test", timezone="UTC", status="active")

    def _fake_user():
        return fake_user

    app.dependency_overrides[get_current_user] = _fake_user
    try:
        r = client.get("/api/v1/meals/foods/search?q=apple&limit=5")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
    assert isinstance(r.json(), list)


def test_food_barcode_not_shadowed(client):
    """GET /api/v1/meals/foods/barcode/12345678 must return 200 or 404, never a UUID error."""
    fake_user = User(id=str(uuid.uuid4()), display_name="test", timezone="UTC", status="active")

    def _fake_user():
        return fake_user

    app.dependency_overrides[get_current_user] = _fake_user
    try:
        r = client.get("/api/v1/meals/foods/barcode/12345678")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    # 404 (not in catalog) is acceptable; 422/500 is NOT.
    assert r.status_code in (200, 404), f"{r.status_code}: {r.text[:200]}"
