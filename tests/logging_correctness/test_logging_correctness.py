"""Logging-correctness suites A-L from artifacts/TEST_AND_RELEASE_PLAN.md.

Runs against a DISPOSABLE database never migrated past d5e6f7a8b9c0 (b7c8d9e0f1a2 is
never applied). Writes go through the LIVE writer path the Telegram bridge drives:

    POST /api/v1/tools/create_meal_from_text   (service token + X-Telegram-Id)

Every assertion reads the DATABASE, not the HTTP status: a 200 is not a pass.
"""
from __future__ import annotations

import os
import uuid

DB_URL = "postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_logtest"
os.environ["DRHIRO_DATABASE_URL"] = DB_URL
os.environ.setdefault("DRHIRO_REDIS_URL", "redis://localhost:6379/0")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from drhiro_api.main import app  # noqa: E402
from drhiro_api.db import engine  # noqa: E402
from drhiro_api.models import ExternalIdentity, User  # noqa: E402
from drhiro_api.security import create_service_token  # noqa: E402

CHAT_ID = "1001"
TOOL = "/api/v1/tools/create_meal_from_text"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def user():
    """Fresh user per test -> no cross-case contamination."""
    from drhiro_api.db import SessionLocal
    telegram_id = str(uuid.uuid4().int)[:12]
    with SessionLocal() as db:
        u = User(display_name="Logging Test", status="active",
                 timezone="Europe/Zagreb")
        db.add(u)
        db.flush()
        db.add(ExternalIdentity(user_id=u.id, provider="telegram",
                                provider_subject=telegram_id))
        db.commit()
        uid = str(u.id)
    return {"id": uid, "telegram_id": telegram_id}


@pytest.fixture()
def auth(user):
    return {"X-Service-Token": create_service_token(),
            "X-Telegram-Id": user["telegram_id"]}


def log(c, auth, text_, message_id=None):
    payload = {"text": text_}
    if message_id is not None:
        payload["telegram_message_id"] = str(message_id)
        payload["telegram_chat_id"] = CHAT_ID
    r = c.post(TOOL, json=payload, headers=auth)
    return r


def q(sql, **p):
    with engine.connect() as conn:
        return [dict(m) for m in conn.execute(text(sql), p).mappings().all()]


def observe(uid):
    """Row counts across the three ledgers + detail."""
    meals = q("SELECT id, meal_type, status, totals_json, notes FROM meals"
              " WHERE user_id = :u AND status <> 'deleted' ORDER BY created_at", u=uid)
    _deleted_meals = q("SELECT id, status FROM meals WHERE user_id = :u"
                       " AND status = 'deleted' ORDER BY created_at", u=uid)
    items = q("""SELECT mi.id, mi.meal_id, mi.display_name, mi.grams, mi.volume_ml,
                        mi.beverage_category, mi.nutrients_json
                 FROM meal_items mi JOIN meals m ON m.id = mi.meal_id
                 WHERE m.user_id = :u AND m.status <> 'deleted'
                 ORDER BY mi.created_at""", u=uid)
    liquids = q("""SELECT id, metric_type, unit, value_json, start_at, deleted_at
                   FROM measurements WHERE user_id = :u AND deleted_at IS NULL
                   ORDER BY created_at""", u=uid)
    acts = q("SELECT id, title, calories_burned, activity_date, deleted_at"
             " FROM activities WHERE user_id = :u AND deleted_at IS NULL"
             " ORDER BY created_at", u=uid)
    return {"meals": meals, "items": items, "liquids": liquids, "activities": acts,
            "deleted_meals": _deleted_meals}


def meal_kcal(nutrients_json):
    if not nutrients_json:
        return None
    for k in ("calories", "kcal", "energy_kcal", "calories_kcal"):
        if isinstance(nutrients_json, dict) and nutrients_json.get(k) is not None:
            return nutrients_json[k]
    return None


def show(tag, o):
    print(f"\n--- {tag} ---")
    print(f"  meals={len(o['meals'])} liquids={len(o['liquids'])} "
          f"activities={len(o['activities'])} items={len(o['items'])}")
    for m in o["meals"]:
        print(f"    meal slot={m['meal_type']} status={m['status']}")
    for i in o["items"]:
        print(f"    item {i['display_name']!r} grams={i['grams']} "
              f"ml={i['volume_ml']} bev={i['beverage_category']} "
              f"kcal={meal_kcal(i['nutrients_json'])}")
    for l in o["liquids"]:
        print(f"    liquid {l['metric_type']} {l['value_json']}")
    for a in o["activities"]:
        print(f"    activity {a['title']!r} kcal={a['calories_burned']}")


# --------------------------------------------------------------------------- #
# Suite A - meal slots
# --------------------------------------------------------------------------- #
SLOT_CASES = [
    ("A1", "eggs and toast", "snack"),
    ("A2", "breakfast scrambled eggs", "breakfast"),
    ("A3", "lunch grilled chicken 200g", "lunch"),
    ("A4", "dinner salmon and rice", "dinner"),
    ("A5", "snack banana", "snack"),
    ("A6", "I ate a banana", "snack"),
]


@pytest.mark.parametrize("cid,text_,slot", SLOT_CASES, ids=[c[0] for c in SLOT_CASES])
def test_suite_a_meal_slots(client, user, auth, cid, text_, slot):
    log(client, auth, text_, message_id=10000 + int(cid[1:]))
    o = observe(user["id"])
    show(f"{cid} {text_!r}", o)
    assert len(o["meals"]) == 1, f"{cid}: expected 1 meal, got {len(o['meals'])}"
    assert o["meals"][0]["meal_type"] == slot, (
        f"{cid}: slot {o['meals'][0]['meal_type']!r} != {slot!r}")
    assert len(o["liquids"]) == 0, f"{cid}: expected 0 liquids"
    assert len(o["activities"]) == 0, f"{cid}: expected 0 activities"


# --------------------------------------------------------------------------- #
# Suite B - liquids only
# --------------------------------------------------------------------------- #
LIQUID_CASES = [
    ("B1", "water 500ml", 500, "water"),
    ("B2", "sparkling water 300 ml", 300, "water"),
    ("B3", "tea 250ml", 250, "tea"),
    ("B4", "black coffee 200ml", 200, "coffee"),
]


@pytest.mark.parametrize("cid,text_,ml,cat", LIQUID_CASES,
                         ids=[c[0] for c in LIQUID_CASES])
def test_suite_b_liquid_only(client, user, auth, cid, text_, ml, cat):
    log(client, auth, text_, message_id=10100 + int(cid[1:]))
    o = observe(user["id"])
    show(f"{cid} {text_!r}", o)
    assert len(o["liquids"]) == 1, f"{cid}: expected 1 liquid, got {len(o['liquids'])}"
    v = o["liquids"][0]["value_json"] or {}
    assert v.get("amount_ml") == ml, f"{cid}: ml {v.get('amount_ml')} != {ml}"
    assert len(o["meals"]) == 0, f"{cid}: expected 0 meals, got {len(o['meals'])}"


# --------------------------------------------------------------------------- #
# Suite C - caloric drinks (dual write)
# --------------------------------------------------------------------------- #
DRINK_CASES = [
    ("C1", "300ml orange juice", 300, "juice", "snack"),
    ("C2", "breakfast latte 250ml", 250, "coffee", "breakfast"),
    ("C3", "beer 500ml", 500, "alcohol", "snack"),
    ("C4", "coke 330ml", 330, "soda", "snack"),
    ("C5", "glass of milk", 250, "milk", "snack"),
]


@pytest.mark.parametrize("cid,text_,ml,cat,slot", DRINK_CASES,
                         ids=[c[0] for c in DRINK_CASES])
def test_suite_c_dual_write(client, user, auth, cid, text_, ml, cat, slot):
    log(client, auth, text_, message_id=10200 + int(cid[1:]))
    o = observe(user["id"])
    show(f"{cid} {text_!r}", o)
    assert len(o["meals"]) == 1, (
        f"{cid}: expected 1 meal (dual-write), got {len(o['meals'])}")
    assert o["meals"][0]["meal_type"] == slot, (
        f"{cid}: slot {o['meals'][0]['meal_type']!r} != {slot!r}")
    assert len(o["liquids"]) == 1, (
        f"{cid}: expected 1 liquid (dual-write), got {len(o['liquids'])}")
    v = o["liquids"][0]["value_json"] or {}
    assert v.get("amount_ml") == ml, f"{cid}: ml {v.get('amount_ml')} != {ml}"
    assert v.get("category") == cat, (
        f"{cid}: category {v.get('category')!r} != {cat!r}")


def test_suite_c6_no_volume(client, user, auth):
    log(client, auth, "orange juice", message_id=10299)
    o = observe(user["id"])
    show("C6 'orange juice' (no volume)", o)
    assert len(o["meals"]) == 1, f"C6: expected 1 meal, got {len(o['meals'])}"
    assert len(o["liquids"]) == 1, (
        f"C6: must not omit the liquid when volume is unknown, got {len(o['liquids'])}")


# --------------------------------------------------------------------------- #
# Suite D - activities
# --------------------------------------------------------------------------- #
ACT_CASES = [
    ("D1", "walk 180 kcal", 180),
    ("D2", "gym 400", 400),
    ("D3", "yoga 30 min 120 kcal", 120),
]


@pytest.mark.parametrize("cid,text_,kcal", ACT_CASES, ids=[c[0] for c in ACT_CASES])
def test_suite_d_activities(client, user, auth, cid, text_, kcal):
    log(client, auth, text_, message_id=10300 + int(cid[1:]))
    o = observe(user["id"])
    show(f"{cid} {text_!r}", o)
    assert len(o["activities"]) == 1, (
        f"{cid}: expected 1 activity, got {len(o['activities'])}")
    assert float(o["activities"][0]["calories_burned"]) == kcal, (
        f"{cid}: kcal {o['activities'][0]['calories_burned']} != {kcal}")
    assert len(o["meals"]) == 0, f"{cid}: activity must not create a meal"
    assert len(o["liquids"]) == 0, f"{cid}: expected 0 liquids"


def test_suite_c1_transaction_is_atomic(client, user, auth, monkeypatch):
    """C txn-atomic: if the liquid write fails, the meal must NOT survive.

    A meal-without-liquid partial write is the production defect; the line must be
    all-or-nothing.
    """
    import drhiro_api.services.log_intents as li

    class LiquidInsertFailed(Exception):
        pass

    class FailingMeasurement:                      # stands in for the liquid row
        def __init__(self, *a, **k):
            raise LiquidInsertFailed("liquid insert failed (injected)")

    monkeypatch.setattr(li, "Measurement", FailingMeasurement)
    log(client, auth, "300ml orange juice", message_id=10298)
    o = observe(user["id"])
    show("C1 txn-atomic (liquid write injected failure)", o)
    assert len(o["meals"]) == 0, (
        f"partial write: {len(o['meals'])} meal row(s) survived a failed liquid write")
    assert len(o["items"]) == 0, "partial write: meal items survived"
    assert len(o["liquids"]) == 0


# --------------------------------------------------------------------------- #
# Suite E - mixed message, one Telegram line, three ledgers
# --------------------------------------------------------------------------- #
E1 = "lunch: grilled chicken 200g, 300ml orange juice, walk 40 min 180 kcal"


def test_suite_e1_mixed_line_three_ledgers(client, user, auth):
    log(client, auth, E1, message_id=10401)
    o = observe(user["id"])
    show(f"E1 {E1!r}", o)
    assert len(o["meals"]) == 1, f"E1: expected 1 meal, got {len(o['meals'])}"
    assert o["meals"][0]["meal_type"] == "lunch", o["meals"][0]["meal_type"]
    names = [i["display_name"].lower() for i in o["items"]]
    assert any("chicken" in n for n in names), names
    assert any("juice" in n for n in names), names
    ch = [i for i in o["items"] if "chicken" in i["display_name"].lower()][0]
    assert ch["grams"] == 200.0, f"E1: chicken grams {ch['grams']} != 200"
    jc = [i for i in o["items"] if "juice" in i["display_name"].lower()][0]
    assert jc["volume_ml"] == 300.0, f"E1: juice volume_ml {jc['volume_ml']} != 300"
    assert jc["beverage_category"] == "juice", jc["beverage_category"]
    assert len(o["liquids"]) == 1, f"E1: expected 1 liquid, got {len(o['liquids'])}"
    v = o["liquids"][0]["value_json"] or {}
    assert v.get("category") == "juice" and v.get("amount_ml") == 300.0, v
    assert len(o["activities"]) == 1, f"E1: expected 1 activity"
    assert o["activities"][0]["title"] == "walk", o["activities"][0]["title"]
    assert float(o["activities"][0]["calories_burned"]) == 180.0
    # no extra rows beyond the single commit
    assert len(o["items"]) == 2, f"E1: expected 2 items, got {len(o['items'])}"


def test_suite_e2_water_and_food(client, user, auth):
    log(client, auth, "water 400ml and a banana", message_id=10402)
    o = observe(user["id"])
    show("E2 'water 400ml and a banana'", o)
    assert len(o["meals"]) == 1, f"E2: expected 1 meal, got {len(o['meals'])}"
    assert o["meals"][0]["meal_type"] == "snack", o["meals"][0]["meal_type"]
    names = [i["display_name"].lower() for i in o["items"]]
    assert names == ["banana"], f"E2: items {names} != ['banana'] (water must not be a meal item)"
    assert len(o["liquids"]) == 1, f"E2: expected 1 liquid, got {len(o['liquids'])}"
    v = o["liquids"][0]["value_json"] or {}
    assert v.get("category") == "water" and v.get("amount_ml") == 400.0, v
    assert len(o["activities"]) == 0, "E2: expected 0 activities"


def test_suite_e3_slot_word_does_not_force_a_meal(client, user, auth):
    """Guard: a slot word must not turn a liquid-only drink into a meal."""
    log(client, auth, "lunch water 300ml", message_id=10403)
    o = observe(user["id"])
    show("E3 'lunch water 300ml'", o)
    assert len(o["meals"]) == 0, f"E3: expected 0 meals, got {len(o['meals'])}"
    assert len(o["liquids"]) == 1, f"E3: expected 1 liquid, got {len(o['liquids'])}"
    v = o["liquids"][0]["value_json"] or {}
    assert v.get("category") == "water" and v.get("amount_ml") == 300.0, v


def test_suite_e4_slot_word_does_not_force_a_meal_for_activity(client, user, auth):
    """Guard: a slot word must not turn an activity into a meal."""
    log(client, auth, "snack walk 100 kcal", message_id=10404)
    o = observe(user["id"])
    show("E4 'snack walk 100 kcal'", o)
    assert len(o["activities"]) == 1, f"E4: expected 1 activity, got {len(o['activities'])}"
    assert len(o["meals"]) == 0, f"E4: expected 0 meals, got {len(o['meals'])}"
    assert len(o["liquids"]) == 0, "E4: expected 0 liquids"


# --------------------------------------------------------------------------- #
# Suite F - idempotency (same message_id)
# --------------------------------------------------------------------------- #
CORRECT = "/api/v1/tools/correct_log"
DELETE = "/api/v1/tools/delete_log"


def ids_of(resp):
    d = (resp.json() or {}).get("data") or {}
    return d


def test_suite_f1_same_message_id_is_idempotent(client, user, auth):
    r1 = log(client, auth, "300ml orange juice", message_id=10501)
    a = ids_of(r1)
    r2 = log(client, auth, "300ml orange juice", message_id=10501)
    b = ids_of(r2)
    o = observe(user["id"])
    show("F1 C1 twice, same message_id", o)
    assert r2.status_code == 200, f"F1: second call {r2.status_code}"
    assert len(o["meals"]) == 1 and len(o["liquids"]) == 1, (
        f"F1: {len(o['meals'])} meals / {len(o['liquids'])} liquids")
    assert a.get("meal_id") == b.get("meal_id"), f"F1: meal id changed {a} -> {b}"
    assert a.get("liquid_ids") == b.get("liquid_ids"), "F1: liquid ids changed"


def test_suite_f2_different_message_id_is_a_new_log(client, user, auth):
    log(client, auth, "300ml orange juice", message_id=10502)
    log(client, auth, "300ml orange juice", message_id=10503)
    o = observe(user["id"])
    show("F2 C1 twice, different message_id", o)
    assert len(o["meals"]) == 2 and len(o["liquids"]) == 2, (
        f"F2: {len(o['meals'])} meals / {len(o['liquids'])} liquids")


def test_suite_f3_meal_idempotent(client, user, auth):
    log(client, auth, "eggs and toast", message_id=10504)
    log(client, auth, "eggs and toast", message_id=10504)
    o = observe(user["id"])
    show("F3 A1 twice, same message_id", o)
    assert len(o["meals"]) == 1, f"F3: {len(o['meals'])} meals"


def test_suite_f4_activity_idempotent(client, user, auth):
    log(client, auth, "walk 180 kcal", message_id=10505)
    log(client, auth, "walk 180 kcal", message_id=10505)
    o = observe(user["id"])
    show("F4 D1 twice, same message_id", o)
    assert len(o["activities"]) == 1, f"F4: {len(o['activities'])} activities"
    assert float(o["activities"][0]["calories_burned"]) == 180.0


def test_suite_f5_concurrent_double_submit(client, user, auth):
    import threading
    results = []

    def go():
        results.append(log(client, auth, "300ml orange juice", message_id=10506))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    o = observe(user["id"])
    show("F5 concurrent C1, same message_id", o)
    assert len(o["meals"]) == 1, f"F5: {len(o['meals'])} meals"
    assert len(o["liquids"]) == 1, f"F5: {len(o['liquids'])} liquids"
    assert all(r.status_code != 500 for r in results), (
        f"F5: 500 from a concurrent submit: {[r.status_code for r in results]}")


# --------------------------------------------------------------------------- #
# Suite G - edits
# --------------------------------------------------------------------------- #
def test_suite_g1_edit_replaces_items_same_meal(client, user, auth):
    log(client, auth, "pizza", message_id=10510)
    before = observe(user["id"])
    log(client, auth, "salad", message_id=10510)
    after = observe(user["id"])
    show("G1 pizza -> salad", after)
    assert len(after["meals"]) == 1, f"G1: {len(after['meals'])} meals"
    assert after["meals"][0]["id"] == before["meals"][0]["id"], "G1: meal id changed"
    names = [i["display_name"].lower() for i in after["items"]]
    assert any("salad" in n for n in names), names
    assert not any("pizza" in n for n in names), f"G1: pizza survived: {names}"


def test_suite_g2_edit_adds_food_keeps_liquid(client, user, auth):
    log(client, auth, "300ml orange juice", message_id=10511)
    before = observe(user["id"])
    log(client, auth, "breakfast 300ml orange juice and a banana", message_id=10511)
    after = observe(user["id"])
    show("G2 C1 -> breakfast juice + banana", after)
    assert len(after["meals"]) == 1, f"G2: {len(after['meals'])} meals"
    assert after["meals"][0]["id"] == before["meals"][0]["id"], "G2: meal id changed"
    assert after["meals"][0]["meal_type"] == "breakfast", after["meals"][0]["meal_type"]
    names = [i["display_name"].lower() for i in after["items"]]
    assert any("banana" in n for n in names), names
    assert len(after["liquids"]) == 1, f"G2: {len(after['liquids'])} liquids"
    v = after["liquids"][0]["value_json"] or {}
    assert v.get("category") == "juice" and v.get("amount_ml") == 300.0, v


def test_suite_g3_edit_liquid_volume(client, user, auth):
    log(client, auth, "water 500ml", message_id=10512)
    log(client, auth, "water 300ml", message_id=10512)
    o = observe(user["id"])
    show("G3 water 500 -> 300", o)
    assert len(o["liquids"]) == 1, f"G3: {len(o['liquids'])} liquids"
    assert (o["liquids"][0]["value_json"] or {}).get("amount_ml") == 300.0, o["liquids"]
    assert len(o["meals"]) == 0, f"G3: {len(o['meals'])} meals"


def test_suite_g4_edit_activity_kcal(client, user, auth):
    log(client, auth, "walk 180 kcal", message_id=10513)
    log(client, auth, "walk 200 kcal", message_id=10513)
    o = observe(user["id"])
    show("G4 walk 180 -> 200", o)
    assert len(o["activities"]) == 1, f"G4: {len(o['activities'])} activities"
    assert float(o["activities"][0]["calories_burned"]) == 200.0


def test_suite_g5_edit_before_original(client, user, auth):
    r = log(client, auth, "salad", message_id=10514)
    o = observe(user["id"])
    show("G5 edit with no prior row", o)
    assert r.status_code == 200, f"G5: {r.status_code} {r.text[:120]}"
    assert len(o["meals"]) == 1, f"G5: {len(o['meals'])} meals"


def test_suite_g6_edit_to_water_drops_the_juice_meal(client, user, auth):
    log(client, auth, "300ml orange juice", message_id=10515)
    log(client, auth, "water 300ml", message_id=10515)
    o = observe(user["id"])
    show("G6 juice -> water", o)
    assert len(o["meals"]) == 0 or all(
        m["status"] == "deleted" for m in o["meals"]), o["meals"]
    assert len(o["liquids"]) == 1, f"G6: {len(o['liquids'])} liquids"
    v = o["liquids"][0]["value_json"] or {}
    assert v.get("category") == "water" and v.get("amount_ml") == 300.0, v


# --------------------------------------------------------------------------- #
# Suite H - delete / correct (soft only)
# --------------------------------------------------------------------------- #
def test_suite_h1_soft_delete_message(client, user, auth):
    log(client, auth, "300ml orange juice", message_id=10520)
    r = client.post(DELETE, json={"telegram_chat_id": CHAT_ID,
                                  "telegram_message_id": "10520"}, headers=auth)
    o = observe(user["id"])
    show("H1 delete the message", o)
    assert r.status_code == 200, f"H1: {r.status_code} {r.text[:120]}"
    assert len(o["meals"]) == 0, f"H1: {len(o['meals'])} visible meals"
    assert len(o["liquids"]) == 0, f"H1: {len(o['liquids'])} visible liquids"
    deleted_meal = q("SELECT status FROM meals WHERE user_id = :u", u=user["id"])
    assert any(m["status"] == "deleted" for m in deleted_meal), (
        f"H1: no soft-deleted meal; meals.status={deleted_meal}")
    liq = q("SELECT deleted_at FROM measurements WHERE user_id = :u", u=user["id"])
    assert all(l["deleted_at"] is not None for l in liq), f"H1: liquid not soft-deleted: {liq}"


def test_suite_h2_delete_named_item(client, user, auth):
    log(client, auth, E1, message_id=10521)
    r = client.post(DELETE, json={"telegram_chat_id": CHAT_ID,
                                  "telegram_message_id": "10521",
                                  "item_name": "juice"}, headers=auth)
    o = observe(user["id"])
    show("H2 delete 'the juice'", o)
    assert r.status_code == 200, f"H2: {r.status_code} {r.text[:160]}"
    names = [i["display_name"].lower() for i in o["items"]]
    assert any("chicken" in n for n in names), f"H2: chicken lost: {names}"
    assert not any("juice" in n for n in names), f"H2: juice survived: {names}"
    assert len(o["liquids"]) == 0, f"H2: juice liquid survived: {o['liquids']}"
    assert len(o["activities"]) == 1, "H2: walk should remain"


def test_suite_h3_correct_slot_same_row(client, user, auth):
    log(client, auth, "eggs and toast", message_id=10522)
    before = observe(user["id"])
    r = client.post(CORRECT, json={"telegram_chat_id": CHAT_ID,
                                   "telegram_message_id": "10522",
                                   "slot": "lunch"}, headers=auth)
    after = observe(user["id"])
    show("H3 'that was lunch'", after)
    assert r.status_code == 200, f"H3: {r.status_code} {r.text[:160]}"
    assert len(after["meals"]) == 1, f"H3: {len(after['meals'])} meals"
    assert after["meals"][0]["id"] == before["meals"][0]["id"], "H3: meal id changed"
    assert after["meals"][0]["meal_type"] == "lunch", after["meals"][0]["meal_type"]


def test_suite_h4_correct_volume_same_row(client, user, auth):
    log(client, auth, "300ml orange juice", message_id=10523)
    before = observe(user["id"])
    r = client.post(CORRECT, json={"telegram_chat_id": CHAT_ID,
                                   "telegram_message_id": "10523",
                                   "amount_ml": 200}, headers=auth)
    after = observe(user["id"])
    show("H4 'that was 200 ml not 300'", after)
    assert r.status_code == 200, f"H4: {r.status_code} {r.text[:160]}"
    assert len(after["liquids"]) == 1, f"H4: {len(after['liquids'])} liquids"
    assert after["liquids"][0]["id"] == before["liquids"][0]["id"], "H4: liquid id changed"
    assert (after["liquids"][0]["value_json"] or {}).get("amount_ml") == 200.0


def test_suite_h5_ambiguous_delete_changes_nothing(client, user, auth):
    log(client, auth, "coffee 200ml and coffee 200ml", message_id=10524)
    before = observe(user["id"])
    r = client.post(DELETE, json={"telegram_chat_id": CHAT_ID,
                                  "telegram_message_id": "10524",
                                  "item_name": "coffee"}, headers=auth)
    after = observe(user["id"])
    show("H5 two coffees + 'delete the coffee'", after)
    assert r.status_code == 409, f"H5: expected 409, got {r.status_code}"
    assert len(after["liquids"]) == len(before["liquids"]), (
        f"H5: rows changed on an ambiguous delete: {len(before['liquids'])} -> "
        f"{len(after['liquids'])}")


# --------------------------------------------------------------------------- #
# Suite J - daily summary contract (the endpoint the owner actually sees)
# --------------------------------------------------------------------------- #
SUMMARY = "/api/v1/tools/get_my_today_summary"


def summary(client, auth):
    r = client.get(SUMMARY, headers=auth)
    assert r.status_code == 200, f"summary {r.status_code}: {r.text[:200]}"
    return (r.json() or {}).get("data") or {}


def test_suite_j_daily_summary_contract(client, user, auth):
    """A clean day: breakfast eggs + 300ml juice + 500ml water + walk 180 kcal."""
    log(client, auth, "breakfast scrambled eggs", message_id=10601)
    log(client, auth, "300ml orange juice", message_id=10602)
    log(client, auth, "water 500ml", message_id=10603)
    log(client, auth, "walk 180 kcal", message_id=10604)

    s = summary(client, auth)
    print("\n--- J daily summary ---")
    print("  meals_logged_today :", s.get("meals_logged_today"))
    print("  calories_kcal_today:", s.get("calories_kcal_today"))
    print("  water_ml_today     :", s.get("water_ml_today"))
    print("  liquids_today      :", s.get("liquids_today"))
    print("  activity_kcal_today:", s.get("activity_kcal_today"))

    # meals: breakfast + the juice's snack meal = 2 (not 3; water is not a meal)
    assert s.get("meals_logged_today") == 2, (
        f"J: meals_logged_today={s.get('meals_logged_today')} != 2")

    # liquids: 300 juice + 500 water = 800, split BY CATEGORY, not by metric_type
    liqu = s.get("liquids_today") or {}
    assert liqu.get("total_ml") == 800, f"J: total_ml={liqu.get('total_ml')} != 800"
    assert liqu.get("juice") == 300, f"J: juice={liqu.get('juice')} != 300"
    assert liqu.get("water") == 500, f"J: water={liqu.get('water')} != 500"

    # activity burn for the day
    assert s.get("activity_kcal_today") == 180.0, (
        f"J: activity_kcal_today={s.get('activity_kcal_today')} != 180")

    # the juice must be in the MEAL ledger (this is the production defect) --
    # either with a kcal number, or present and flagged when the lookup misses.
    o = observe(user["id"])
    names = [i["display_name"].lower() for i in o["items"]]
    assert any("juice" in n for n in names), f"J: juice missing from the meal ledger: {names}"
    assert not any("water" in n for n in names), "J: water must not be a meal item"
    kcal = s.get("calories_kcal_today")
    if kcal:
        assert kcal > 0, "J: calories_kcal_today should include the juice"


def test_suite_j_excludes_soft_deleted_rows(client, user, auth):
    """H1-deleted rows must not appear in any total."""
    log(client, auth, "water 500ml", message_id=10605)
    log(client, auth, "walk 180 kcal", message_id=10606)
    before = summary(client, auth)
    client.post(DELETE, json={"telegram_chat_id": CHAT_ID,
                              "telegram_message_id": "10605"}, headers=auth)
    client.post(DELETE, json={"telegram_chat_id": CHAT_ID,
                              "telegram_message_id": "10606"}, headers=auth)
    after = summary(client, auth)
    print("\n--- J soft-delete exclusion ---")
    print("  before:", before.get("liquids_today"), before.get("activity_kcal_today"))
    print("  after :", after.get("liquids_today"), after.get("activity_kcal_today"))
    assert (before.get("liquids_today") or {}).get("total_ml") == 500
    assert (after.get("liquids_today") or {}).get("total_ml") == 0, (
        f"J: soft-deleted liquid still counted: {after.get('liquids_today')}")
    assert after.get("activity_kcal_today") == 0.0, (
        f"J: soft-deleted activity still counted: {after.get('activity_kcal_today')}")


def test_suite_h4_echo_meal_item_volume_matches_liquid(client, user, auth):
    """After 'that was 200 ml', the meal item must echo 200 as well as the liquid."""
    log(client, auth, "300ml orange juice", message_id=10607)
    client.post(CORRECT, json={"telegram_chat_id": CHAT_ID,
                               "telegram_message_id": "10607",
                               "amount_ml": 200}, headers=auth)
    o = observe(user["id"])
    show("H4 echo: meal item volume_ml after 300 -> 200", o)
    assert len(o["liquids"]) == 1
    assert (o["liquids"][0]["value_json"] or {}).get("amount_ml") == 200.0
    juice = [i for i in o["items"] if "juice" in i["display_name"].lower()]
    assert juice, "H4 echo: juice meal item missing"
    assert juice[0]["volume_ml"] == 200.0, (
        f"H4 echo: meal item volume_ml={juice[0]['volume_ml']} != 200 "
        "(the two ledgers disagree)")


def test_suite_h6_delete_food_with_multiple_drinks_preserves_drinks(client, user, auth):
    """Deleting a food item when multiple drinks are logged must NOT delete any drink.

    Regression for Qodo #11: the old code fell back to the first liquid id when
    Measurement.meal_item_id was empty, so deleting a food alongside multiple
    drinks could delete an unrelated beverage measurement.
    """
    log(client, auth, "chicken 200g, water 500ml, coffee 200ml", message_id=10610)
    before = observe(user["id"])
    show("H6 before: chicken + water + coffee", before)
    assert len(before["liquids"]) == 2, f"H6: expected 2 liquids, got {len(before['liquids'])}"
    names_before = [i["display_name"].lower() for i in before["items"]]
    assert any("chicken" in n for n in names_before), f"H6: chicken missing: {names_before}"

    r = client.post(DELETE, json={"telegram_chat_id": CHAT_ID,
                                  "telegram_message_id": "10610",
                                  "item_name": "chicken"}, headers=auth)
    after = observe(user["id"])
    show("H6 after: delete chicken, drinks preserved", after)
    assert r.status_code == 200, f"H6: {r.status_code} {r.text[:160]}"
    assert len(after["liquids"]) == 2, (
        f"H6: a drink was deleted with the chicken: {len(after['liquids'])} != 2 "
        f"liquids={after['liquids']}")
    names_after = [i["display_name"].lower() for i in after["items"]]
    assert not any("chicken" in n for n in names_after), f"H6: chicken survived: {names_after}"


def test_suite_h7_delete_one_drink_with_food_and_another_drink(client, user, auth):
    """Deleting one drink by name (food + 2 drinks present) must delete only that drink."""
    log(client, auth, "chicken 200g, water 500ml, coffee 200ml", message_id=10611)
    before = observe(user["id"])
    show("H7 before: chicken + water + coffee", before)
    assert len(before["liquids"]) == 2, f"H7: expected 2 liquids, got {len(before['liquids'])}"

    r = client.post(DELETE, json={"telegram_chat_id": CHAT_ID,
                                  "telegram_message_id": "10611",
                                  "item_name": "coffee"}, headers=auth)
    after = observe(user["id"])
    show("H7 after: delete coffee only", after)
    assert r.status_code == 200, f"H7: {r.status_code} {r.text[:160]}"
    assert len(after["liquids"]) == 1, (
        f"H7: expected 1 liquid after deleting coffee, got {len(after['liquids'])}")
    remaining_cat = (after["liquids"][0]["value_json"] or {}).get("category")
    assert remaining_cat == "water", f"H7: remaining liquid is {remaining_cat!r}, expected 'water'"
    names_after = [i["display_name"].lower() for i in after["items"]]
    assert any("chicken" in n for n in names_after), f"H7: chicken was lost: {names_after}"
