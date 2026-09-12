"""T1 — route-by-route writer-ownership gate + transition tests.

Presents VALID auth (Bearer user JWT for /meals, service token + telegram id for
/tools) so the require_model_writer_allowed GATE is what fires, not auth. A
seeded user makes get_current_user/_resolve_user pass.

Design note (honest): the API header gate is defense-in-depth for the
service-token surface (/meals, /tools). The model reaches /ingest with a USER
JWT that the API cannot distinguish from a real user, so /ingest is enforced at
the MCP tool layer instead (see test_t1_mcp_writer_gate.py) — never at the API.

Run: python -m pytest tests/test_t1_writer_gate_routes.py -q
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))

from drhiro_api import config as api_config  # noqa: E402
from drhiro_api.db import get_db  # noqa: E402
from drhiro_api.main import app  # noqa: E402
from drhiro_api.models import Base, ExternalIdentity, Meal, User  # noqa: E402
from drhiro_api.routers import telegram_ingress as router_mod  # noqa: E402
from drhiro_api.security import create_access_token, create_service_token  # noqa: E402

URL = "/api/v1"
TEST_DB_URL = os.environ.get(
    "DRHIRO_TEST_DB_URL",
    "postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_test",
)


class _Settings:
    def __init__(self, enabled=True, legacy=True):
        self.telegram_ingress_secret = "s"
        self.telegram_bot_id = "1"
        self.telegram_ingress_enabled = enabled
        self.legacy_consumption_writers_enabled = legacy


# (method, path, body, auth) — auth supplies the headers that make the route's
# own auth dependency pass, so the gate is what is being tested.
GATED_ROUTES = [
    ("POST", "/meals", {"items": [{"food": "x", "grams": 100}], "meal_type": "lunch"}, "model"),
    ("POST", "/meals/from-text", {"text": "chicken 200g"}, "model"),
    ("POST", "/meals/from-photo", {}, "model"),
    ("POST", "/meals/from-barcode", {"barcode": "x"}, "model"),
    ("PATCH", "/meals/{meal_id}", {"meal_type": "dinner"}, "model"),
    ("PATCH", "/meals/{meal_id}/items/{item_id}", {"grams": 200}, "model"),
    ("POST", "/meals/{meal_id}/items", {"items": [{"food": "x", "grams": 100}]}, "model"),
    ("POST", "/meals/{meal_id}/confirm", {}, "model"),
    ("POST", "/tools/create_meal_from_text", {"text": "chicken 200g"}, "service"),
    ("POST", "/tools/update_meal_item", {"meal_id": "x", "item": "steak", "grams": 200}, "service"),
    ("POST", "/tools/confirm_meal", {"draft_id": "x", "selections": [1]}, "service"),
]


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def seeded(engine):
    with engine.connect() as conn:
        from sqlalchemy import text
        for tbl in (
            "beverage_measurements", "consumption_items", "consumption_operations",
            "meal_items", "meals", "measurements", "external_identities", "users",
        ):
            conn.execute(text(f"DELETE FROM {tbl}"))
        conn.commit()
    SessionLocal = sessionmaker(bind=engine, autoflush=False)
    db = SessionLocal()
    try:
        u = User(id=str(uuid.uuid4()), display_name="Gate User", timezone="Europe/Zagreb")
        db.add(u)
        db.flush()
        db.add(ExternalIdentity(id=str(uuid.uuid4()), user_id=u.id,
                                provider="telegram", provider_subject="555"))
        from datetime import datetime, timezone
        meal = Meal(id=str(uuid.uuid4()), user_id=u.id, meal_type="lunch",
                    status="confirmed", eaten_at=datetime.now(timezone.utc), totals_json={})
        db.add(meal)
        db.commit()
        _bearer_uid["uid"] = str(u.id)
        return {"user_id": str(u.id), "meal_id": str(meal.id)}
    finally:
        db.close()


@pytest.fixture()
def client(engine, seeded):
    SessionLocal = sessionmaker(bind=engine, autoflush=False)

    def _get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


_bearer_uid = {"uid": None}


def _bearer():
    return {"Authorization": f"Bearer {create_access_token(_bearer_uid['uid'], scope='user')}"}


def _service():
    return {
        "x-service-token": create_service_token("openclaw", expires_minutes=5),
        "x-telegram-id": "555",
    }


def _model():
    h = _bearer()
    h.update(_service())
    return h


AUTH = {"model": _model, "service": _service, "bearer": _bearer}


class TestRouteByRouteGate:
    @pytest.mark.parametrize(
        "method,path,body,auth", GATED_ROUTES,
        ids=[f"{m}{p}" for m, p, _, _ in GATED_ROUTES],
    )
    def test_model_service_identity_is_refused(self, client, monkeypatch, method, path, body, auth):
        monkeypatch.setattr(router_mod, "get_settings", lambda: _Settings(legacy=False))
        headers = AUTH[auth]()
        r = client.request(method, URL + path, json=body, headers=headers)
        assert r.status_code == 403, f"{method} {path}: {r.status_code} {r.text[:140]}"
        assert "model_writer_disabled" in r.text

    @pytest.mark.parametrize(
        "method,path,body,auth", GATED_ROUTES,
        ids=[f"{m}{p}" for m, p, _, _ in GATED_ROUTES],
    )
    def test_non_model_caller_is_not_gated(self, client, monkeypatch, method, path, body, auth):
        """Without a service token (user JWT only), the gate must NOT fire."""
        monkeypatch.setattr(router_mod, "get_settings", lambda: _Settings(legacy=False))
        headers = {"Authorization": f"Bearer {create_access_token(_bearer_uid['uid'], scope='user')}"}
        resolved = path.replace("{meal_id}", "00000000-0000-0000-0000-000000000000").replace("{item_id}", "0")
        r = client.request(method, URL + resolved, json=body, headers=headers)
        assert r.status_code not in (403, 503), f"{method} {path}: {r.status_code}"


class TestTransition:
    def test_default_config_is_unchanged(self, client, monkeypatch):
        monkeypatch.setattr(router_mod, "get_settings", lambda: _Settings(enabled=False, legacy=True))
        r = client.post(URL + "/ingest/manual/water", json={"amount_ml": 250})
        assert r.status_code not in (403, 503)

    def test_closing_writers_without_trusted_path_refuses(self, client, monkeypatch):
        monkeypatch.setattr(router_mod, "get_settings", lambda: _Settings(enabled=False, legacy=False))
        r = client.post(URL + "/tools/create_meal_from_text", json={"text": "chicken 200g"},
                        headers=_service())
        assert r.status_code == 503
        assert "legacy_writers_closed_without_trusted_path" in r.text

    def test_activating_trusted_path_closes_model_writer(self, client, monkeypatch):
        monkeypatch.setattr(router_mod, "get_settings", lambda: _Settings(enabled=True, legacy=False))
        r = client.post(URL + "/tools/create_meal_from_text", json={"text": "chicken 200g"},
                        headers=_service())
        assert r.status_code == 403
        assert "model_writer_disabled" in r.text

    def test_manual_caller_keeps_working_through_transition(self, client, monkeypatch):
        monkeypatch.setattr(router_mod, "get_settings", lambda: _Settings(enabled=True, legacy=False))
        r = client.post(URL + "/ingest/manual/water", json={"amount_ml": 250})
        assert r.status_code != 403

    def test_writer_gate_only_applies_to_consumption_routes(self, client, monkeypatch):
        monkeypatch.setattr(router_mod, "get_settings", lambda: _Settings(enabled=True, legacy=False))
        r = client.post(URL + "/tools/issue_device_code", json={}, headers=_service())
        assert r.status_code != 403
