"""Ledger classifier tests (0.1.26).

Covers the spec acceptance list:
  * high-confidence agreement (target >= threshold) -> proceed silently
  * confident disagreement -> block with a which-did-you-mean naming both ledgers
  * the 0.5-0.88 band -> proceed flagged needs_review
  * all-low chatter -> no_ledger, nothing written
  * Jev unavailable -> fail open, warning exactly once
  * the guard is disableable by ONE config flag
  * "500 ml water" still lands as a liquid meal via the existing is_liquid /
    beverage_category path -- the classifier never decides food vs drink

The Jev client (``_classify_scores``) is STUBBED in every test: there are no live
Jev calls in this suite. Runs against the DEV database only (see conftest).
"""

from __future__ import annotations

import logging

import pytest

import drhiro_api.services.ledger_classifier as lc
from drhiro_api.config import get_settings
from drhiro_api.models import Activity, Meal, Measurement
from drhiro_api.security import create_service_token
from tests.conftest import make_user


def tool_headers(telegram_id: str) -> dict:
    return {"X-Service-Token": create_service_token(), "X-Telegram-Id": str(telegram_id)}


def _scores(**kw) -> dict:
    """A six-label score map; unspecified labels default to 0.01."""
    base = {label: 0.01 for label in lc.LEDGERS}
    base.update({k: float(v) for k, v in kw.items()})
    return base


@pytest.fixture
def jev_on(monkeypatch):
    """Configure Jev + neutralise translation. The caller stubs _classify_scores."""
    monkeypatch.setenv("DRHIRO_JEV_API_KEY", "test-key")
    monkeypatch.setenv("DRHIRO_JEV_LEDGER_GUARD_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(lc, "_to_english", lambda t: t)
    lc._WARNED.clear()
    yield
    get_settings.cache_clear()


def stub(monkeypatch, mapping: dict) -> None:
    monkeypatch.setattr(lc, "_classify_scores", lambda text: dict(mapping))


# --------------------------------------------------------------------------- #
# classify_ledger -- the pure classifier
# --------------------------------------------------------------------------- #
def test_confidence_agreement_returns_label(jev_on, monkeypatch):
    stub(monkeypatch, _scores(consumption=0.97))
    v = lc.classify_ledger("2 eggs and a slice of sourdough bread")
    assert v.available is True
    assert v.reason == "classified"
    assert v.label == "consumption"
    assert v.score == pytest.approx(0.97)
    assert v.runner_up_score == pytest.approx(0.01)
    assert v.margin == pytest.approx(0.96)


def test_band_is_no_ledger_label_but_score_kept(jev_on, monkeypatch):
    stub(monkeypatch, _scores(consumption=0.7, activity=0.1))
    v = lc.classify_ledger("something ambiguous")
    assert v.available is True
    assert v.label is None
    assert v.reason == "no_ledger"
    assert v.score == pytest.approx(0.7)


def test_all_low_chatter_is_no_ledger(jev_on, monkeypatch):
    stub(monkeypatch, _scores(consumption=0.01, activity=0.03))
    v = lc.classify_ledger("good morning, how are you")
    assert v.label is None
    assert v.reason == "no_ledger"
    assert max(s for s in v.scores.values() if s is not None) < 0.5


def test_empty_text_short_circuits_without_call(jev_on, monkeypatch):
    called = {"n": 0}

    def boom(text):
        called["n"] += 1
        return _scores(consumption=0.99)

    monkeypatch.setattr(lc, "_classify_scores", boom)
    v = lc.classify_ledger("   ")
    assert v.label is None and v.reason == "no_ledger"
    assert called["n"] == 0


def test_unavailable_when_jev_unconfigured(monkeypatch):
    monkeypatch.delenv("DRHIRO_JEV_API_KEY", raising=False)
    get_settings.cache_clear()
    v = lc.classify_ledger("2 eggs")
    assert v.available is False
    assert v.reason == "unavailable"
    assert v.label is None
    get_settings.cache_clear()


def test_unavailable_when_call_fails(jev_on, monkeypatch):
    monkeypatch.setattr(lc, "_classify_scores", lambda text: None)
    v = lc.classify_ledger("2 eggs")
    assert v.available is False and v.reason == "unavailable"


def test_classifier_never_returns_a_liquid_label(jev_on, monkeypatch):
    # Food vs drink is decided downstream, never here.
    stub(monkeypatch, _scores(consumption=0.94))
    v = lc.classify_ledger("500 ml water")
    assert v.label == "consumption"
    assert v.label not in {"liquid", "water", "drink"}


# --------------------------------------------------------------------------- #
# guard_write -- the pre-write guard, unit level
# --------------------------------------------------------------------------- #
def test_guard_proceeds_on_agreement(jev_on, monkeypatch):
    stub(monkeypatch, _scores(consumption=0.97))
    g = lc.guard_write("2 eggs", "consumption")
    assert g.action == "proceed"


def test_guard_blocks_confident_disagreement(jev_on, monkeypatch):
    stub(monkeypatch, _scores(activity=0.94, consumption=0.08))
    g = lc.guard_write("CrossFit 350 kcal", "consumption")
    assert g.action == "block"
    assert g.which_did_you_mean["target_ledger"] == "consumption"
    assert g.which_did_you_mean["suggested_ledger"] == "activity"
    assert g.which_did_you_mean["suggested_tool"] == "log_activity"


def test_guard_reviews_in_band(jev_on, monkeypatch):
    stub(monkeypatch, _scores(consumption=0.7))
    g = lc.guard_write("maybe food", "consumption")
    assert g.action == "review"


def test_guard_no_ledger_when_all_low(jev_on, monkeypatch):
    stub(monkeypatch, _scores(consumption=0.05, activity=0.03))
    g = lc.guard_write("good morning", "consumption")
    assert g.action == "no_ledger"


def test_guard_fails_open_when_unavailable(monkeypatch, caplog):
    monkeypatch.delenv("DRHIRO_JEV_API_KEY", raising=False)
    get_settings.cache_clear()
    lc._WARNED.clear()
    with caplog.at_level(logging.WARNING, logger=lc.__name__):
        g = lc.guard_write("2 eggs", "consumption")
    assert g.action == "fail_open"
    assert sum("fail-open" in r.message for r in caplog.records) == 1
    get_settings.cache_clear()


def test_guard_disabled_by_flag(jev_on, monkeypatch):
    monkeypatch.setenv("DRHIRO_JEV_LEDGER_GUARD_ENABLED", "false")
    get_settings.cache_clear()
    stub(monkeypatch, _scores(activity=0.94, consumption=0.08))
    g = lc.guard_write("CrossFit 350 kcal", "consumption")
    assert g.action == "proceed" and g.reason == "disabled"
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# the route: meal path blocks, activity path proceeds (spec acceptance #2/#3)
# --------------------------------------------------------------------------- #
def test_meal_path_blocks_workout_and_writes_nothing(client, db, jev_on, monkeypatch):
    user = make_user(db, "GuardBlock", telegram_id="5301")
    stub(monkeypatch, _scores(activity=0.94, consumption=0.08))
    r = client.post("/api/v1/tools/create_meal_from_text",
                    json={"telegram_chat_id": "pgchat-7101", "telegram_message_id": "pg-ledg-0","text": "CrossFit 350 kcal"}, headers=tool_headers("5301"))
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["data"]["error"] == "ledger_disagreement"
    wdym = body["data"]["which_did_you_mean"]
    assert wdym["target_ledger"] == "consumption"
    assert wdym["suggested_ledger"] == "activity"
    # Nothing was written to the meal ledger.
    assert db.query(Meal).filter(Meal.user_id == user.id).count() == 0


def test_activity_path_proceeds(client, db):
    """The same text down the activity path logs to the activities ledger."""
    user = make_user(db, "GuardProceed", telegram_id="5302")
    r = client.post("/api/v1/tools/log_activity",
                    json={"title": "CrossFit", "calories_burned": 350},
                    headers=tool_headers("5302"))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    rows = db.query(Activity).filter(Activity.user_id == user.id).all()
    assert len(rows) == 1 and rows[0].calories_burned == 350
    # And crucially no meal was created by the activity path.
    assert db.query(Meal).filter(Meal.user_id == user.id).count() == 0


def test_good_morning_is_no_ledger_and_writes_nothing(client, db, jev_on, monkeypatch):
    user = make_user(db, "Chatter", telegram_id="5303")
    stub(monkeypatch, _scores(consumption=0.01, activity=0.03))
    r = client.post("/api/v1/tools/create_meal_from_text",
                    json={"telegram_chat_id": "pgchat-7101", "telegram_message_id": "pg-ledg-1","text": "good morning, how are you"}, headers=tool_headers("5303"))
    assert r.status_code == 409, r.text
    assert r.json()["data"]["error"] == "no_ledger"
    assert db.query(Meal).filter(Meal.user_id == user.id).count() == 0
    assert db.query(Measurement).filter(Measurement.user_id == user.id).count() == 0
    assert db.query(Activity).filter(Activity.user_id == user.id).count() == 0


def test_route_reviews_in_band_and_logs(client, db, jev_on, monkeypatch):
    user = make_user(db, "ReviewBand", telegram_id="5304")
    stub(monkeypatch, _scores(consumption=0.7))
    r = client.post("/api/v1/tools/create_meal_from_text",
                    json={"telegram_chat_id": "pgchat-7101", "telegram_message_id": "pg-ledg-2","text": "2 eggs"}, headers=tool_headers("5304"))
    assert r.status_code == 200, r.text
    assert r.json()["data"]["ledger_review"] is True
    assert db.query(Meal).filter(Meal.user_id == user.id).count() == 1


def test_route_reviews_elsewhere_when_parser_finds_nothing(client, db, jev_on, monkeypatch):
    """Router: parser empty + classifier names a ledger -> routing hint, no write."""
    user = make_user(db, "Router", telegram_id="5305")
    stub(monkeypatch, _scores(sleep=0.98, consumption=0.01))
    # consumption < floor, best other (sleep) >= threshold -> block is the guard's
    # decision; either way nothing is written and the payload names the ledger.
    r = client.post("/api/v1/tools/create_meal_from_text",
                    json={"telegram_chat_id": "pgchat-7101", "telegram_message_id": "pg-ledg-3","text": "slept 7 hours"}, headers=tool_headers("5305"))
    assert r.status_code == 409, r.text
    assert r.json()["data"]["which_did_you_mean"]["suggested_ledger"] == "sleep"
    assert db.query(Meal).filter(Meal.user_id == user.id).count() == 0
    assert db.query(Measurement).filter(Measurement.user_id == user.id).count() == 0


# --------------------------------------------------------------------------- #
# fail open end-to-end (acceptance #4)
# --------------------------------------------------------------------------- #
def test_unconfigured_jev_meal_log_succeeds_with_one_warning(client, db, monkeypatch, caplog):
    monkeypatch.delenv("DRHIRO_JEV_API_KEY", raising=False)
    get_settings.cache_clear()
    lc._WARNED.clear()
    user = make_user(db, "FailOpen", telegram_id="5306")
    with caplog.at_level(logging.WARNING, logger=lc.__name__):
        r = client.post("/api/v1/tools/create_meal_from_text",
                        json={"telegram_chat_id": "pgchat-7101", "telegram_message_id": "pg-ledg-4","text": "2 eggs"}, headers=tool_headers("5306"))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert db.query(Meal).filter(Meal.user_id == user.id).count() == 1
    assert sum("fail-open" in rec.message for rec in caplog.records) == 1
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# food/drink untouched (acceptance #5)
# --------------------------------------------------------------------------- #
def test_water_still_becomes_a_liquid_meal(client, db, jev_on, monkeypatch):
    user = make_user(db, "Water", telegram_id="5307")
    stub(monkeypatch, _scores(consumption=0.9))
    r = client.post("/api/v1/tools/create_meal_from_text",
                    json={"telegram_chat_id": "pgchat-7101", "telegram_message_id": "pg-ledg-5","text": "500 ml water"}, headers=tool_headers("5307"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    # Water is liquid-only: no meal row, one measurement row with 500 ml.
    assert db.query(Meal).filter(Meal.user_id == user.id).count() == 0
    rows = db.query(Measurement).filter(Measurement.user_id == user.id).all()
    assert len(rows) == 1
    assert rows[0].metric_type == "water"
    assert (rows[0].value_json or {}).get("category") == "water"
    assert (rows[0].value_json or {}).get("amount_ml") == 500.0
    # The classifier was asked at most once and only about consumption.
    assert body["data"]["ledger_review"] is False
