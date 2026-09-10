"""R1 tests — nutrition resolution in the real draft→confirm path.

STATUS: **PARTIAL R1** (see labels per test). R1 is NOT closed.

What this file establishes:
  * VERIFIED: the real draft handler (create_meal_from_text_intelligent) now
    resolves nutrition through the DB catalog and persists provenance into the
    draft. (test_draft_resolves_real_nutrition_and_provenance → PASS)
  * VERIFIED (labelled): confirmation persists the resolved, scaled nutrition and
    provenance end-to-end via the SUPPORTED NON-TELEGRAM path that supplies a
    caller idempotency_key. This test intentionally supplies an idempotency key
    and is labelled as such — it does not exercise, and does not stand in for,
    the Telegram event-identity path.
    (test_confirm_persists_nutrition_via_idempotency_key — SUPPLIES IDENTITY)
  * BLOCKED BY R2: the Telegram end-to-end path cannot persist because the
    authenticated transport never supplies chat_id/message_id/bot_id, so
    confirm_consumption correctly refuses a telegram source without identity.
    (test_telegram_confirm_blocked_by_missing_event_identity → documents R2)

Run against an ALEMBIC-BUILT database (no create_all):
    DRHIRO_TEST_DB_URL=postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_r4test_alembic \
    REDIS_URL=redis://localhost:6382/15 \
    python -m pytest tests/test_r1_nutrition_resolution.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

TEST_DB_URL = os.environ.get(
    "DRHIRO_TEST_DB_URL",
    "postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_r4test_alembic",
)

# These decisive tests require an ALEMBIC-BUILT database and a reachable Redis.
# They are skipped unless explicitly opted in, so the default (create_all-based)
# suite is unaffected. Enable with:
#   DRHIRO_R1R2_ALEMBIC_DB=1 DRHIRO_TEST_DB_URL=<alembic-db> REDIS_URL=<redis>
pytestmark = pytest.mark.skipif(
    os.environ.get("DRHIRO_R1R2_ALEMBIC_DB") != "1",
    reason="requires an Alembic-built DB + Redis; set DRHIRO_R1R2_ALEMBIC_DB=1",
)


@pytest.fixture(scope="session", autouse=True)
def _clean_test_owned_data():
    """Remove artifacts from prior runs of THIS test module only.

    Scoped strictly to foods owned by test data sources (source_key LIKE 'ts-%'
    or 'dbg-%'). Production/other data is untouched.
    """
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    with eng.begin() as conn:
        conn.execute(text(
            "DELETE FROM food_nutrients fn USING foods f JOIN data_sources ds "
            "ON f.data_source_id = ds.id "
            "WHERE fn.food_id = f.id AND (ds.source_key LIKE 'ts-%' OR ds.source_key LIKE 'dbg-%')"))
        conn.execute(text(
            "DELETE FROM foods f USING data_sources ds "
            "WHERE f.data_source_id = ds.id AND (ds.source_key LIKE 'ts-%' OR ds.source_key LIKE 'dbg-%')"))
        conn.execute(text("DELETE FROM data_sources WHERE source_key LIKE 'ts-%' OR source_key LIKE 'dbg-%'"))
    eng.dispose()
    yield


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    with eng.connect() as conn:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
        assert row is not None, "target DB is not alembic-built"
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    S = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = S()
    try:
        yield s
    finally:
        s.close()


def _seed_food(db, display_name, kcal, protein, is_liquid=False):
    """Insert a food + nutrient rows reachable by resolve_food's DB path.

    Nutrient codes MUST be the canonical names (energy, protein) that
    food_search.nutrient_map resolves; rows are reused if already present
    (nutrient_code is unique).
    """
    from drhiro_api.models import DataSource, Food, FoodNutrient, Nutrient
    ds = DataSource(source_key=f"ts-{uuid.uuid4().hex[:8]}", source_label="Test",
                    is_active=True)
    db.add(ds); db.flush()
    food = Food(data_source_id=ds.id, external_id=str(uuid.uuid4()),
                display_name=display_name, is_generic=True, is_liquid=is_liquid)
    db.add(food); db.flush()
    for code, label, unit, val in [("energy", "Energy", "kcal", kcal),
                                   ("protein", "Protein", "g", protein)]:
        n = db.query(Nutrient).filter(Nutrient.nutrient_code == code).first()
        if n is None:
            n = Nutrient(nutrient_code=code, nutrient_label=label, unit=unit,
                         category="macro")
            db.add(n); db.flush()
        db.add(FoodNutrient(food_id=food.id, nutrient_id=n.id, amount_per_100g=val))
    db.commit()
    return str(food.id)


def _mk_user(db):
    from drhiro_api.models import User
    uid = uuid.uuid4()
    db.add(User(id=uid, display_name="R1U", timezone="UTC", locale="en", status="active"))
    db.commit()
    return str(uid)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_draft_resolves_real_nutrition_and_provenance(db):
    """VERIFIED (R1 wiring): the real draft handler resolves via the DB catalog
    and records provenance, instead of storing the parser's empty nutrient dict."""
    from drhiro_api.services import intelligent_meal_service_patch as svc
    uid = _mk_user(db)
    _seed_food(db, "grilled steak", 250.0, 26.0)
    svc.SessionLocal = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)

    resp = _run(svc.create_meal_from_text_intelligent(
        req=svc.MealIn(text="200 g grilled steak", meal_type="dinner"),
        user={"id": uid}, db=db))
    assert resp["ok"] is True
    items = resp["data"]["items"]
    assert len(items) == 1
    di = items[0]
    assert di["food_catalog_item_id"] is not None, "handler did not resolve a catalog food"
    assert di["resolution_source"] == "db"
    assert di["nutrition_complete"] is True
    assert di["nutrients_per_100"].get("kcal") == 250.0
    assert di["nutrients_scaled"].get("kcal") == pytest.approx(500.0, abs=1.0)
    assert di["nutrients_scaled"].get("protein_g") == pytest.approx(52.0, abs=1.0)
    assert di["nutrient_basis"] == "per_100_g"


def test_confirm_persists_nutrition_via_idempotency_key(db):
    """VERIFIED (R1 persistence) — SUPPLIES IDENTITY (caller idempotency_key).

    This test deliberately supplies an idempotency_key, which is the SUPPORTED
    NON-TELEGRAM entry point for non-Telegram callers. It proves the resolved
    nutrition + provenance are persisted and reported. It does NOT exercise the
    Telegram event-identity path (that is R2), and must not be read as doing so.
    """
    from drhiro_api.services import intelligent_meal_service_patch as svc
    from sqlalchemy.orm import sessionmaker as sm
    uid = _mk_user(db)
    _seed_food(db, "grilled steak", 250.0, 26.0)
    svc.SessionLocal = sm(bind=db.get_bind(), autoflush=False, expire_on_commit=False)

    draft = _run(svc.create_meal_from_text_intelligent(
        req=svc.MealIn(text="200 g grilled steak", meal_type="dinner"),
        user={"id": uid}, db=db))
    draft_id = draft["data"]["draft_id"]

    # NOTE: supply caller idempotency key by using the supported non-telegram
    # path directly (the handler hard-codes source="telegram", which is the R2
    # gap). We call confirm_consumption with an explicit idempotency_key.
    from drhiro_api.services.consumption import ParsedItem, confirm_consumption
    items = [ParsedItem(**it) for it in draft["data"]["items"]]
    result = confirm_consumption(
        db=db, user_id=uid, items=items, meal_type="dinner",
        source="api", idempotency_key=f"test-key-{uuid.uuid4().hex}",
        raw_text="200 g grilled steak",
    )
    assert result["ok"] is True
    totals = result["data"]["totals"]
    assert totals["kcal"] == pytest.approx(500.0, abs=1.0)
    assert totals["protein_g"] == pytest.approx(52.0, abs=1.0)

    row = db.execute(text(
        "SELECT ci.resolution_source, ci.nutrient_basis, ci.food_catalog_item_id, "
        "ci.nutrition_complete, ci.nutrients_scaled "
        "FROM consumption_items ci JOIN meals m "
        "ON ci.operation_id = m.source_operation_id WHERE m.user_id = :uid"),
        {"uid": uid}).fetchone()
    assert row is not None, "no consumption_item persisted"
    assert row[0] == "db"
    assert row[1] == "per_100_g"
    assert row[2] is not None
    assert row[3] is True
    scaled = row[4] if isinstance(row[4], dict) else {}
    assert scaled.get("kcal") == pytest.approx(500.0, abs=1.0)


def test_telegram_confirm_blocked_by_missing_event_identity(db):
    """DOCUMENTS R2 (not an R1 assertion): the Telegram path through the real
    handler cannot confirm because the authenticated transport supplies no
    chat_id/message_id/bot_id. confirm_consumption correctly REFUSES rather than
    writing — proving the identity gate is enforced and that R2 is the blocker.

    This test asserts the CURRENT (correct) failure and will need to be
    INVERTED once R2 propagates trusted event identity."""
    from drhiro_api.services import intelligent_meal_service_patch as svc
    from sqlalchemy.orm import sessionmaker as sm
    uid = _mk_user(db)
    _seed_food(db, "grilled steak", 250.0, 26.0)
    svc.SessionLocal = sm(bind=db.get_bind(), autoflush=False, expire_on_commit=False)

    draft = _run(svc.create_meal_from_text_intelligent(
        req=svc.MealIn(text="200 g grilled steak", meal_type="dinner"),
        user={"id": uid}, db=db))
    draft_id = draft["data"]["draft_id"]

    # user dict mirrors what production auth actually returns: only {"id": ...}.
    with pytest.raises(ValueError) as exc:
        _run(svc.confirm_meal(
            req=svc.ConfirmMealRequest(draft_id=draft_id, selections=[0]),
            user={"id": uid}, db=db))
    assert "telegram_source_requires_identity" in str(exc.value)
