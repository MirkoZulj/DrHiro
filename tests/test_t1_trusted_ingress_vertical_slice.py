"""R2 / T1 — trusted ingress vertical slice.

Drives the REAL path end to end on an **Alembic-built** database:

    raw Telegram update
      -> bridge extract_event  (authentic identity)
      -> bridge sign_event     (trusted transport)
      -> API accept_signed_event (verify; fail closed)
      -> TrustedIngressWorker  (durable receipt, binding, validation)
      -> server-side nutrient resolution
      -> atomic meal/liquid persistence
      -> response

Gated behind DRHIRO_T1_ALEMBIC_DB=1 so it is reported SEPARATELY from the
default suite. The schema is NOT created here: it must already exist via
`alembic upgrade head`, which is the point of running these on an Alembic DB.

Run:
    DRHIRO_T1_ALEMBIC_DB=1 \
    DRHIRO_TEST_DB_URL=postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_t1_alembic \
    REDIS_URL=redis://localhost:6382/15 \
    python -m pytest tests/test_t1_trusted_ingress_vertical_slice.py -v
"""
from __future__ import annotations

import os
import sys
import threading
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

from drhiro_api.models import (  # noqa: E402
    BeverageMeasurement,
    ConsumptionItem,
    ConsumptionOperation,
    DataSource,
    ExternalIdentity,
    Food,
    FoodNutrient,
    Meal,
    MealItem,
    Measurement,
    Nutrient,
    User,
)
from drhiro_api.services import telegram_ingress as ingress  # noqa: E402

from drhiro_bridge import ingress as bridge_ingress  # noqa: E402

TEST_DB_URL = os.environ.get(
    "DRHIRO_TEST_DB_URL",
    "postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_t1_alembic",
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6382/15")

SECRET = "test-ingress-secret"
BOT_ID = "1234567890"  # verified getMe.id
CHAT_ID = "-100200300"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def schema_is_alembic(engine):
    """Prove the schema came from Alembic, not create_all()."""
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version, "alembic_version is empty — DB was not built by Alembic"
        has_ops = conn.execute(
            text("SELECT to_regclass('public.consumption_operations') IS NOT NULL")
        ).scalar()
        assert has_ops, "consumption_operations missing from the Alembic schema"
    return version


@pytest.fixture()
def SessionLocal(engine, schema_is_alembic):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db(SessionLocal, engine):
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

    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def user(db):
    u = User(id=str(uuid.uuid4()), display_name="T1 User", timezone="Europe/Zagreb")
    db.add(u)
    db.flush()
    db.add(ExternalIdentity(
        id=str(uuid.uuid4()), user_id=u.id,
        provider="telegram", provider_subject="555",
    ))
    db.flush()
    db.commit()  # concurrent workers run in their own sessions
    return u


@pytest.fixture()
def other_user(db):
    u = User(id=str(uuid.uuid4()), display_name="Intruder", timezone="Europe/Zagreb")
    db.add(u)
    db.flush()
    db.add(ExternalIdentity(
        id=str(uuid.uuid4()), user_id=u.id,
        provider="telegram", provider_subject="999",
    ))
    db.flush()
    db.commit()
    return u


# Seeded catalog: simple names so resolution is deterministic and offline.
_CATALOG = {
    "steak":  (271.0, 26.0, 0.0, 18.0, 0.0, 55.0, False),
    "chicken": (165.0, 31.0, 0.0, 3.6, 0.0, 74.0, False),
    "bread":  (265.0, 9.0, 49.0, 3.2, 2.7, 490.0, False),
    "wine":   (83.0, 0.1, 2.6, 0.0, 0.0, 5.0, True),
    "beer":   (43.0, 0.5, 3.6, 0.0, 0.0, 4.0, True),
    "water":  (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, True),
}


@pytest.fixture()
def catalog(db):
    ds = DataSource(id=str(uuid.uuid4()), source_key="t1test", source_label="T1 Test")
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

    for name, (kcal, prot, carbs, fat, fiber, sodium, liquid) in _CATALOG.items():
        food = Food(
            id=str(uuid.uuid4()),
            data_source_id=ds.id,
            external_id=f"t1-{name}",
            display_name=name.capitalize(),
            is_generic=True,
            is_liquid=liquid,
            serving_grams=100,
            serving_unit="ml" if liquid else "g",
        )
        db.add(food)
        db.flush()
        for code, amt in [
            ("energy", kcal), ("protein", prot), ("carbs", carbs),
            ("fat", fat), ("fiber", fiber), ("sodium", sodium),
        ]:
            db.add(FoodNutrient(
                id=str(uuid.uuid4()), food_id=food.id,
                nutrient_id=nutrients[code].id, amount_per_100g=amt,
            ))
    db.flush()
    db.commit()  # concurrent workers run in their own sessions
    return _CATALOG


# ---------------------------------------------------------------------------
# Helpers: raw updates, bridge extraction, signing
# ---------------------------------------------------------------------------

def raw_message(text: str, message_id: str, chat_id: str = CHAT_ID, user_id: str = "555"):
    return {
        "update_id": 1000 + int(message_id) if message_id.isdigit() else 1000,
        "message": {
            "message_id": int(message_id) if message_id.isdigit() else message_id,
            "date": 1757000000,
            "chat": {"id": int(chat_id), "type": "private"},
            "from": {"id": int(user_id), "username": "alice"},
            "text": text,
        },
    }


def raw_edit(text: str, message_id: str, chat_id: str = CHAT_ID, user_id: str = "555"):
    upd = raw_message(text, message_id, chat_id, user_id)
    upd["edited_message"] = upd.pop("message")
    return upd


def raw_callback(data: str, chat_id: str = CHAT_ID, user_id: str = "555", cb_id: str = "cb1"):
    return {
        "update_id": 2000,
        "callback_query": {
            "id": cb_id,
            "from": {"id": int(user_id), "username": "alice"},
            "message": {"message_id": 4242, "chat": {"id": int(chat_id), "type": "private"}},
            "data": data,
        },
    }


def signed_payload(update: dict, bot_id: str = BOT_ID) -> dict:
    """Bridge side: extract authentic identity and sign it."""
    ev = bridge_ingress.extract_event(update, bot_id)
    assert ev is not None, "extract_event returned None"
    payload = ev.to_payload()
    payload["signature"] = bridge_ingress.sign_event(
        SECRET,
        service=payload["service"],
        bot_id=ev.bot_id,
        chat_id=ev.chat_id,
        message_id=ev.message_id,
        user_id=ev.user_id,
        digest=payload["digest"],
        kind=ev.kind,
    )
    return payload


def accept(payload: dict) -> ingress.TrustedEvent:
    """API side: verify the trusted transport envelope against the verified bot."""
    return ingress.accept_signed_event(SECRET, payload, trusted_bot_id=BOT_ID)


def proposer_for(mapping: dict):
    """A deterministic stand-in for the (untrusted) model.

    Returns proposals shaped exactly like model output; the worker must still
    validate them and resolve nutrition server-side.
    """
    def _propose(operation_id: str, text: str):
        return mapping.get(text, {"items": []})
    return _propose


MEAL_PROPOSALS = {
    "I had 300g steak": {
        "meal_type": "lunch",
        "items": [{"name": "Steak", "grams": 300}],
    },
}


# ---------------------------------------------------------------------------
# 1. Vertical slice
# ---------------------------------------------------------------------------

class TestVerticalSlice:
    def test_raw_update_to_persisted_meal(self, db, user, catalog):
        """One raw Telegram event -> ingress -> resolution -> persistence."""
        text = "I had 300g steak"
        worker = ingress.TrustedIngressWorker(db, proposer_for(MEAL_PROPOSALS))

        payload = signed_payload(raw_message(text, "1"), bot_id=BOT_ID)
        event = accept(payload)
        out = worker.handle(event)

        assert out["status"] == "completed", out
        result = out["result"]
        assert result["ok"] is True

        # Persisted meal with REAL nutrition resolved server-side:
        # steak 271 kcal / 100 g * 300 g = 813 kcal
        meal = db.query(Meal).filter(Meal.id == result["data"]["meal_id"]).first()
        assert meal is not None
        assert meal.totals_json["kcal"] == pytest.approx(813.0, rel=1e-6)
        assert meal.totals_json["protein_g"] == pytest.approx(78.0, rel=1e-6)

        items = db.query(MealItem).filter(MealItem.meal_id == meal.id).all()
        assert len(items) == 1
        assert items[0].grams == pytest.approx(300.0)

        # Durable operation, marked completed, identity recorded.
        op = db.query(ConsumptionOperation).filter(
            ConsumptionOperation.id == out["operation_id"]
        ).first()
        assert op.status == "completed"
        assert op.source_bot_id == BOT_ID
        assert op.source_message_id == "1"
        assert op.result_json["data"]["meal_id"] == str(meal.id)

        # Item provenance: resolved from the DB, nutrition complete.
        ci = db.query(ConsumptionItem).filter(
            ConsumptionItem.operation_id == op.id
        ).all()
        assert len(ci) == 1
        assert ci[0].resolution_source == "db"
        assert ci[0].nutrition_complete is True
        assert ci[0].nutrients_scaled["kcal"] == pytest.approx(813.0, rel=1e-6)

    def test_unknown_food_is_not_reported_as_known_zero(self, db, user, catalog):
        """UNKNOWN != KNOWN-ZERO must survive the trusted path."""
        text = "I had 100g unobtainium"
        worker = ingress.TrustedIngressWorker(
            db,
            proposer_for({text: {"items": [{"name": "Unobtainium", "grams": 100}]}}),
        )
        out = worker.handle(accept(signed_payload(raw_message(text, "2"))))
        assert out["status"] == "completed"

        op = db.query(ConsumptionOperation).filter(
            ConsumptionOperation.id == out["operation_id"]
        ).first()
        ci = db.query(ConsumptionItem).filter(ConsumptionItem.operation_id == op.id).one()
        assert ci.nutrition_complete is False
        assert ci.resolution_source == "unmatched"
        assert op.result_json["data"]["nutrition_complete"] is False


# ---------------------------------------------------------------------------
# 2. Concurrent identical messages
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_two_identical_messages_are_two_consumptions(
        self, SessionLocal, db, user, catalog
    ):
        """Identical text, different message ids -> two consumptions.

        This is the case a content digest cannot distinguish; only identity can.
        """
        text = "I had 300g steak"
        updates = [raw_message(text, "10"), raw_message(text, "11")]

        errors = []

        def run(update):
            session = SessionLocal()
            try:
                worker = ingress.TrustedIngressWorker(session, proposer_for(MEAL_PROPOSALS))
                worker.handle(accept(signed_payload(update)))
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
            finally:
                session.close()

        threads = [threading.Thread(target=run, args=(u,)) for u in updates]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        meals = db.query(Meal).all()
        assert len(meals) == 2, f"expected 2 consumptions, got {len(meals)}"
        ops = db.query(ConsumptionOperation).all()
        assert len(ops) == 2
        assert {o.source_message_id for o in ops} == {"10", "11"}
        # Same event key? No — identities differ.
        assert len({ingress.event_key(BOT_ID, CHAT_ID, "10"),
                    ingress.event_key(BOT_ID, CHAT_ID, "11")}) == 2

    def test_concurrent_redelivery_of_one_message_yields_one_consumption(
        self, SessionLocal, db, user, catalog
    ):
        text = "I had 300g steak"
        update = raw_message(text, "12")
        errors = []

        def run():
            session = SessionLocal()
            try:
                worker = ingress.TrustedIngressWorker(session, proposer_for(MEAL_PROPOSALS))
                worker.handle(accept(signed_payload(update)))
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
            finally:
                session.close()

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        assert db.query(Meal).count() == 1
        assert db.query(ConsumptionOperation).count() == 1


# ---------------------------------------------------------------------------
# 3. Redelivery / lost response
# ---------------------------------------------------------------------------

class TestRedelivery:
    def test_redelivery_replays_without_second_consumption(self, db, user, catalog):
        text = "I had 300g steak"
        worker = ingress.TrustedIngressWorker(db, proposer_for(MEAL_PROPOSALS))
        update = raw_message(text, "20")

        first = worker.handle(accept(signed_payload(update)))
        assert first["status"] == "completed"
        meal_id = first["result"]["data"]["meal_id"]

        # Redelivery of the SAME message (lost response / retry).
        second = worker.handle(accept(signed_payload(update)))
        assert second["status"] == "replayed", second
        assert second["result"]["data"]["meal_id"] == meal_id
        assert db.query(Meal).count() == 1
        assert db.query(ConsumptionOperation).count() == 1

    def test_same_identity_different_content_without_edit_is_rejected(
        self, db, user, catalog
    ):
        """Same event, different content, NOT an edit -> explicit conflict."""
        worker = ingress.TrustedIngressWorker(db, proposer_for(MEAL_PROPOSALS))
        worker.handle(accept(signed_payload(raw_message("I had 300g steak", "21"))))

        # A different text arriving as an ordinary 'message' for the same id.
        with pytest.raises(ingress.IngressConflict):
            worker.handle(accept(signed_payload(raw_message("I had 500g steak", "21"))))

        assert db.query(Meal).count() == 1


# ---------------------------------------------------------------------------
# 4. Multiple drinks and same-item overlap
# ---------------------------------------------------------------------------

class TestSameItemOverlap:
    def test_drinks_and_meal_contribute_exactly_once(self, db, user, catalog):
        text = "steak with wine and beer"
        proposals = {
            "meal_type": "dinner",
            "items": [
                {"name": "Steak", "grams": 200},
                {"name": "Wine", "volume_ml": 150, "category": "wine", "is_beverage": True},
                {"name": "Beer", "volume_ml": 330, "category": "beer", "is_beverage": True},
            ],
        }
        worker = ingress.TrustedIngressWorker(db, proposer_for({text: proposals}))
        out = worker.handle(accept(signed_payload(raw_message(text, "30"))))
        assert out["status"] == "completed"

        meal = db.query(Meal).filter(
            Meal.id == out["result"]["data"]["meal_id"]
        ).first()
        assert db.query(Meal).count() == 1

        # 2 drinks -> exactly 2 measurement rows, each once.
        measurements = db.query(Measurement).all()
        assert len(measurements) == 2
        volumes = sorted(m.value_json["amount_ml"] for m in measurements)
        assert volumes == [150.0, 330.0]

        # Each is linked 1:1 to its meal item.
        assert db.query(BeverageMeasurement).count() == 2

        # Nutrition: steak 271*2 = 542, wine 83*1.5 = 124.5, beer 43*3.3 = 141.9
        expected_kcal = 542.0 + 124.5 + 141.9
        assert meal.totals_json["kcal"] == pytest.approx(expected_kcal, rel=1e-6)

        # One consumption item per drink -> no duplicate volume/nutrition.
        items = db.query(ConsumptionItem).all()
        assert len(items) == 3
        assert len({i.item_key for i in items}) == 3

    def test_repeated_logging_of_same_drink_converges(self, db, user, catalog):
        """The same drink re-offered under the SAME event does not double-count."""
        text = "wine"
        proposals = {
            "items": [{"name": "Wine", "volume_ml": 150, "category": "wine", "is_beverage": True}]
        }
        worker = ingress.TrustedIngressWorker(db, proposer_for({text: proposals}))
        update = raw_message(text, "31")
        worker.handle(accept(signed_payload(update)))
        worker.handle(accept(signed_payload(update)))  # redelivery

        assert db.query(Measurement).count() == 1
        assert db.query(Meal).count() == 1


# ---------------------------------------------------------------------------
# 5. Edits and ownership-checked callbacks
# ---------------------------------------------------------------------------

class TestEditsAndCallbacks:
    def test_edit_is_a_revision_not_a_second_consumption(self, db, user, catalog):
        text = "I had 300g steak"
        edited = "I had 500g steak"
        proposals = {
            text: {"meal_type": "lunch", "items": [{"name": "Steak", "grams": 300}]},
            edited: {"meal_type": "lunch", "items": [{"name": "Steak", "grams": 500}]},
        }
        worker = ingress.TrustedIngressWorker(db, proposer_for(proposals))

        first = worker.handle(accept(signed_payload(raw_message(text, "40"))))
        assert first["status"] == "completed"
        assert db.query(Meal).count() == 1

        rev = worker.handle(accept(signed_payload(raw_edit(edited, "40"))))
        assert rev["status"] == "revised", rev
        assert rev["revision"] == 1

        # Still ONE operation and ONE meal: the edit replaced, not added.
        assert db.query(ConsumptionOperation).count() == 1
        assert db.query(Meal).count() == 1
        meal = db.query(Meal).first()
        assert meal.totals_json["kcal"] == pytest.approx(271.0 * 5, rel=1e-6)

    def test_callback_confirm_replays_and_checks_ownership(self, db, user, other_user, catalog):
        text = "I had 300g steak"
        worker = ingress.TrustedIngressWorker(db, proposer_for(MEAL_PROPOSALS))
        out = worker.handle(accept(signed_payload(raw_message(text, "50"))))
        op_id = out["operation_id"]

        # Owner confirming -> returns the original result, no new consumption.
        owned = worker.handle(
            accept(signed_payload(raw_callback(f"confirm:{op_id}", user_id="555")))
        )
        assert owned["status"] == "replayed"
        assert owned["result"]["data"]["meal_id"] == out["result"]["data"]["meal_id"]
        assert db.query(Meal).count() == 1

        # A DIFFERENT user's callback for that operation -> rejected.
        stolen = worker.handle(
            accept(signed_payload(raw_callback(f"confirm:{op_id}", user_id="999")))
        )
        assert stolen["status"] == "rejected"
        assert stolen["reason"] == "callback_not_owner"
        assert db.query(Meal).count() == 1

    def test_callback_for_unknown_operation_is_ignored(self, db, user, catalog):
        worker = ingress.TrustedIngressWorker(db, proposer_for(MEAL_PROPOSALS))
        out = worker.handle(accept(signed_payload(raw_callback(f"confirm:{uuid.uuid4()}"))))
        assert out["status"] == "ignored"
        assert db.query(Meal).count() == 0


# ---------------------------------------------------------------------------
# 6. Worker restart and complete Redis loss
# ---------------------------------------------------------------------------

class TestRestartAndRedisLoss:
    def test_complete_redis_loss_replays_from_postgres(self, db, user, catalog):
        """Drop the ENTIRE Redis dataset, restart the worker, redeliver.

        The durable result must come from PostgreSQL. Redis is not required to
        recover ownership or replay.
        """
        import redis as redis_lib

        text = "I had 300g steak"
        worker = ingress.TrustedIngressWorker(db, proposer_for(MEAL_PROPOSALS))
        update = raw_message(text, "60")
        first = worker.handle(accept(signed_payload(update)))
        assert first["status"] == "completed"
        meal_id = first["result"]["data"]["meal_id"]

        # COMPLETE Redis loss (not a restart).
        client = redis_lib.Redis.from_url(REDIS_URL)
        before = client.dbsize()
        client.flushdb()
        client.flushall()
        assert client.dbsize() == 0

        # "Worker restart": brand new session, no in-memory state carried over.
        SessionLocal = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
        fresh = SessionLocal()
        try:
            restarted = ingress.TrustedIngressWorker(fresh, proposer_for(MEAL_PROPOSALS))
            replayed = restarted.handle(accept(signed_payload(update)))
            assert replayed["status"] == "replayed", replayed
            assert replayed["result"]["data"]["meal_id"] == meal_id
            assert fresh.query(Meal).count() == 1
        finally:
            fresh.close()

        assert client.dbsize() == 0, "replay must not depend on Redis state"
        assert before is not None  # recorded for the report


# ---------------------------------------------------------------------------
# 7. Trust boundary
# ---------------------------------------------------------------------------

class TestTrustBoundary:
    def test_forged_signature_is_rejected(self, db, user, catalog):
        payload = signed_payload(raw_message("I had 300g steak", "70"))
        payload["signature"] = "00" * 32
        with pytest.raises(ingress.IngressRejected, match="invalid_signature"):
            accept(payload)

    def test_missing_envelope_is_rejected(self, db, user, catalog):
        payload = signed_payload(raw_message("I had 300g steak", "71"))
        payload.pop("signature")
        with pytest.raises(ingress.IngressRejected):
            accept(payload)

    def test_content_swapped_after_signing_is_rejected(self, db, user, catalog):
        """A valid signature for different content must not authorize this one."""
        payload = signed_payload(raw_message("I had 300g steak", "72"))
        payload["text"] = "I had 1000g steak"
        with pytest.raises(ingress.IngressRejected, match="content_digest_mismatch"):
            accept(payload)

    def test_cross_account_substitution_is_rejected(self, db, user, catalog):
        """An envelope signed for another bot id cannot be replayed as this one."""
        payload = signed_payload(raw_message("I had 300g steak", "73"), bot_id="9999999999")
        # Validly signed for THAT bot account, but it is not our verified account.
        with pytest.raises(ingress.IngressRejected, match="cross_account_bot_mismatch"):
            accept(payload)

    def test_model_output_cannot_supply_identity_or_nutrition(self, db, user, catalog):
        """Untrusted proposals carrying identity/nutrition fields are ignored.

        Nutrition is resolved server-side; identity comes from transport only.
        """
        text = "I had 300g steak"
        hostile = {
            "meal_type": "lunch",
            "user_id": "somebody-else",
            "bot_id": "9999999999",
            "chat_id": "0",
            "message_id": "0",
            "items": [{
                "name": "Steak", "grams": 300,
                "kcal": 99999, "nutrients_per_100": {"kcal": 99999},
                "user_id": "somebody-else",
            }],
        }
        worker = ingress.TrustedIngressWorker(db, proposer_for({text: hostile}))
        out = worker.handle(accept(signed_payload(raw_message(text, "74"))))
        assert out["status"] == "completed"

        op = db.query(ConsumptionOperation).filter(
            ConsumptionOperation.id == out["operation_id"]
        ).first()
        # Identity is from transport, not from the model's fields.
        assert op.source_bot_id == BOT_ID
        assert op.source_message_id == "74"
        assert str(op.user_id) == str(user.id)
        # Nutrition is server-resolved (813 kcal), not the model's 99999.
        assert op.result_json["data"]["totals"]["kcal"] == pytest.approx(813.0, rel=1e-6)

    def test_malformed_proposals_are_rejected_not_repaired(self, db, user, catalog):
        text = "garbage"
        bad = {
            "items": [
                {"grams": 100},                      # no name
                {"name": "Steak", "grams": -5},      # negative
                {"name": "Beer", "is_beverage": True},  # beverage w/o volume
                "not-an-object",
                {"name": "Steak", "grams": float("inf")},  # non-finite
            ]
        }
        worker = ingress.TrustedIngressWorker(db, proposer_for({text: bad}))
        out = worker.handle(accept(signed_payload(raw_message(text, "75"))))
        assert out["status"] == "needs_clarification"
        assert db.query(Meal).count() == 0
        assert len(out["rejected"]) == 5
