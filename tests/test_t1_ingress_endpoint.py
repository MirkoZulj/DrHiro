"""T1 ingress endpoint — HTTP-level evidence through the real ASGI app.

Exercises the actual FastAPI route (not the worker directly) against the
Alembic-built database, including the fail-closed trust boundary and the
writer-ownership gate.

Gated behind DRHIRO_T1_ALEMBIC_DB=1; reported separately from the default suite.

Run:
    DRHIRO_T1_ALEMBIC_DB=1 \
    DRHIRO_TEST_DB_URL=postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_t1_alembic \
    python -m pytest tests/test_t1_ingress_endpoint.py -v
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "telegram-bridge", "src"))

pytestmark = pytest.mark.skipif(
    os.environ.get("DRHIRO_T1_ALEMBIC_DB") != "1",
    reason="requires an Alembic-built DB; set DRHIRO_T1_ALEMBIC_DB=1",
)

from fastapi.testclient import TestClient  # noqa: E402

from drhiro_api.db import get_db  # noqa: E402
from drhiro_api.main import app  # noqa: E402
from drhiro_api.models import (  # noqa: E402
    DataSource,
    ExternalIdentity,
    Food,
    FoodNutrient,
    Meal,
    Nutrient,
    User,
)
from drhiro_api.routers import telegram_ingress as router_mod  # noqa: E402

from drhiro_bridge import ingress as bridge_ingress  # noqa: E402

TEST_DB_URL = os.environ.get(
    "DRHIRO_TEST_DB_URL",
    "postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_t1_alembic",
)
SECRET = "endpoint-secret"
BOT_ID = "1234567890"
CHAT_ID = "-100200300"
URL = "/api/v1/ingest/telegram/event"


class _Settings:
    def __init__(self, secret=SECRET, bot_id=BOT_ID, enabled=True, legacy=True):
        self.telegram_ingress_secret = secret
        self.telegram_bot_id = bot_id
        self.telegram_ingress_enabled = enabled
        self.legacy_consumption_writers_enabled = legacy


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def SessionLocal(engine):
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version, "not an Alembic-built DB"
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def client(SessionLocal, engine):
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


@pytest.fixture()
def seed(SessionLocal, engine):
    with engine.connect() as conn:
        for tbl in (
            "beverage_measurements", "consumption_items", "consumption_operations",
            "meal_items", "meals", "measurements", "food_nutrients", "foods",
            "food_brands", "food_ingredients", "food_resolution_rules",
            "food_catalog_items", "nutrients", "data_sources",
            "external_identities", "device_connections", "users",
        ):
            conn.execute(text(f"DELETE FROM {tbl}"))
        conn.commit()

    db = SessionLocal()
    try:
        u = User(id=str(uuid.uuid4()), display_name="EP User", timezone="Europe/Zagreb")
        db.add(u)
        db.flush()
        db.add(ExternalIdentity(
            id=str(uuid.uuid4()), user_id=u.id,
            provider="telegram", provider_subject="555",
        ))
        ds = DataSource(id=str(uuid.uuid4()), source_key="ep", source_label="EP")
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
        food = Food(
            id=str(uuid.uuid4()), data_source_id=ds.id, external_id="ep-steak",
            display_name="Steak", is_generic=True, is_liquid=False,
            serving_grams=100, serving_unit="g",
        )
        db.add(food)
        db.flush()
        for code, amt in [
            ("energy", 271.0), ("protein", 26.0), ("carbs", 0.0),
            ("fat", 18.0), ("fiber", 0.0), ("sodium", 55.0),
        ]:
            db.add(FoodNutrient(
                id=str(uuid.uuid4()), food_id=food.id,
                nutrient_id=nutrients[code].id, amount_per_100g=amt,
            ))
        db.commit()
    finally:
        db.close()
    return True


def _signed(text="I had 300g steak", message_id="1", bot_id=BOT_ID):
    """Return (payload, headers) exactly as the bridge delivers them.

    The signature travels in a HEADER (trusted transport), never in the JSON
    body, and the endpoint deliberately lets the header win.
    """
    update = {
        "update_id": 1,
        "message": {
            "message_id": int(message_id),
            "chat": {"id": int(CHAT_ID), "type": "private"},
            "from": {"id": 555, "username": "alice"},
            "text": text,
        },
    }
    ev = bridge_ingress.extract_event(update, bot_id)
    payload = ev.to_payload()
    signature = bridge_ingress.sign_event(
        SECRET,
        service=payload["service"], bot_id=ev.bot_id, chat_id=ev.chat_id,
        message_id=ev.message_id, user_id=ev.user_id,
        digest=payload["digest"], kind=ev.kind,
    )
    return payload, {"X-DrHiro-Ingress-Signature": signature}


def _payload(text="I had 300g steak", message_id="1", bot_id=BOT_ID):
    return _signed(text, message_id, bot_id)[0]


@pytest.fixture()
def proposer(monkeypatch):
    monkeypatch.setattr(
        router_mod, "llm_proposer",
        lambda op_id, text: {"meal_type": "lunch", "items": [{"name": "Steak", "grams": 300}]},
    )


class TestFailClosed:
    def test_missing_secret_is_503(self, client, monkeypatch):
        monkeypatch.setattr(router_mod, "get_settings", lambda: _Settings(secret=""))
        r = client.post(URL, json=_payload())
        assert r.status_code == 503
        assert "ingress_secret_not_configured" in r.text

    def test_missing_verified_bot_id_is_503(self, client, monkeypatch):
        monkeypatch.setattr(router_mod, "get_settings", lambda: _Settings(bot_id=""))
        r = client.post(URL, json=_payload())
        assert r.status_code == 503
        assert "trusted_bot_id_not_configured" in r.text

    def test_missing_signature_is_401(self, client, seed, monkeypatch):
        monkeypatch.setattr(router_mod, "get_settings", lambda: _Settings())
        p = _payload()
        r = client.post(URL, json=p)  # no signature header at all
        assert r.status_code == 401

    def test_forged_signature_is_401(self, client, seed, monkeypatch):
        monkeypatch.setattr(router_mod, "get_settings", lambda: _Settings())
        p = _payload()
        r = client.post(URL, json=p, headers={"X-DrHiro-Ingress-Signature": "00" * 32})
        assert r.status_code == 401

    def test_cross_account_bot_is_401(self, client, seed, monkeypatch):
        monkeypatch.setattr(router_mod, "get_settings", lambda: _Settings())
        body, headers = _signed(bot_id="9999999999")
        r = client.post(URL, json=body, headers=headers)
        assert r.status_code == 401
        assert "cross_account" in r.text


class TestHappyPath:
    def test_signed_event_persists_a_meal(self, client, SessionLocal, seed, proposer, monkeypatch):
        monkeypatch.setattr(router_mod, "get_settings", lambda: _Settings())
        body, headers = _signed()
        r = client.post(URL, json=body, headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "completed"
        assert body["result"]["data"]["totals"]["kcal"] == pytest.approx(813.0, rel=1e-6)

        db = SessionLocal()
        try:
            assert db.query(Meal).count() == 1
        finally:
            db.close()

    def test_redelivery_over_http_replays(self, client, SessionLocal, seed, proposer, monkeypatch):
        monkeypatch.setattr(router_mod, "get_settings", lambda: _Settings())
        body, headers = _signed()
        first = client.post(URL, json=body, headers=headers).json()
        body2, headers2 = _signed()
        second = client.post(URL, json=body2, headers=headers2).json()
        assert first["status"] == "completed"
        assert second["status"] == "replayed"
        assert second["result"]["data"]["meal_id"] == first["result"]["data"]["meal_id"]

        db = SessionLocal()
        try:
            assert db.query(Meal).count() == 1
        finally:
            db.close()


class TestWriterOwnership:
    def test_model_writer_is_closed_when_gate_active(self, client, seed, monkeypatch):
        """With the trusted path active and legacy writers closed, the model's
        service identity cannot log a consumption through the /tools writer."""
        monkeypatch.setattr(
            router_mod, "get_settings",
            lambda: _Settings(enabled=True, legacy=False),
        )
        r = client.post(
            "/api/v1/tools/create_meal_from_text",
            json={"text": "chicken 200g"},
            headers={"x-service-token": _service_token(), "x-telegram-id": "555"},
        )
        assert r.status_code == 403
        assert "model_writer_disabled" in r.text

    def test_manual_caller_is_unaffected(self, client, seed, monkeypatch):
        """A non-model (user JWT) caller keeps working."""
        monkeypatch.setattr(
            router_mod, "get_settings",
            lambda: _Settings(enabled=True, legacy=False),
        )
        r = client.post("/api/v1/ingest/manual/water", json={"amount_ml": 250})
        # Not 403: the gate does not apply to non-service callers.
        assert r.status_code != 403

    def test_closing_writers_without_trusted_path_refuses(self, client, seed, monkeypatch):
        """Refuse to close legacy writers when no trusted path is active."""
        monkeypatch.setattr(
            router_mod, "get_settings",
            lambda: _Settings(enabled=False, legacy=False),
        )
        r = client.post(
            "/api/v1/tools/create_meal_from_text",
            json={"text": "chicken 200g"},
            headers={"x-service-token": _service_token(), "x-telegram-id": "555"},
        )
        assert r.status_code == 503
        assert "legacy_writers_closed_without_trusted_path" in r.text


def _service_token() -> str:
    from drhiro_api.security import create_service_token

    return create_service_token("openclaw", expires_minutes=5)
