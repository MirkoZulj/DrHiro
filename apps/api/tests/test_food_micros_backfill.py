"""Food-micros backfill tests (spec §4.3.2 / §6) — complement to
test_intake_backfill.py (which covers FDC conversion + matcher kcal gate).

Covers: OFF barcode upsert + g->canonical conversion, HTTP 429 retry in the
real FDC fetch path, no-source foods marked no_source, idempotency (a food
with a backfill row is skipped on re-run). Fake fetchers, no network.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from drhiro_api.models import (
    DataSource, Food, FoodMicroBackfill, FoodNutrient, Meal, MealItem, Nutrient, User,
)
from drhiro_api.services.food_micros import backfill_food_micros

TZ = ZoneInfo("Europe/Zagreb")
NOSLEEP = lambda s: None  # noqa: E731


def _nuts(db):
    from drhiro_nutrition.micros import NUTRIENT_CATALOGUE
    out = {}
    for c in NUTRIENT_CATALOGUE:
        n = Nutrient(nutrient_code=c.code, nutrient_label=c.label_en, unit=c.unit, category=c.group)
        db.add(n); db.flush(); out[c.code] = n
    return out


def _setup(db, *, source_key="usda", external_id="168462", barcode=None):
    _nuts(db)
    u = User(display_name="U", timezone="Europe/Zagreb")
    db.add(u); db.flush()
    ds = DataSource(source_key=source_key, source_label=source_key.upper())
    db.add(ds); db.flush()
    food = Food(data_source_id=ds.id, external_id=external_id, display_name="Spinach, raw", barcode=barcode)
    db.add(food); db.flush()
    m = Meal(user_id=u.id, eaten_at=datetime(2026, 11, 4, 12, tzinfo=TZ).astimezone(timezone.utc),
             meal_type="lunch", status="confirmed")
    db.add(m); db.flush()
    db.add(MealItem(meal_id=m.id, food_catalog_item_id=str(food.id), display_name="Spinach",
                    quantity=1, grams=100, nutrients_json={"kcal": 23}))
    db.commit()
    return food


def _rows(db, food):
    return {db.query(Nutrient).get(r.nutrient_id).nutrient_code: r.amount_per_100g
            for r in db.query(FoodNutrient).filter_by(food_id=food.id).all()}


def test_backfill_off_barcode_upsert_and_convert(db):
    food = _setup(db, source_key="off", external_id="", barcode="8718900000001")

    def fake_off(barcode):
        assert barcode == "8718900000001"
        return {"nutriments": {"iron_100g": 0.00271, "magnesium_100g": 0.079}}

    report = backfill_food_micros(db, batch=10, off_fetcher=fake_off, sleep=NOSLEEP, run_matcher_first=False)
    assert report["done"] == 1, report
    rows = _rows(db, food)
    assert rows["iron"] == pytest.approx(2.71)       # OFF g/100g -> mg
    assert rows["magnesium"] == pytest.approx(79.0)
    assert db.query(FoodMicroBackfill).filter_by(food_id=food.id, status="done").count() == 1


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_backfill_retries_on_429(db, monkeypatch):
    food = _setup(db)
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 2:
            return _Resp(429)   # one 429 -> one retry (spec §4.3.2)
        return _Resp(200, [{"fdcId": 168462, "foodNutrients": [
            {"nutrient": {"id": 1090, "unitName": "MG"}, "amount": 79.0}]}])

    monkeypatch.setattr("httpx.post", flaky)
    report = backfill_food_micros(db, batch=10, sleep=NOSLEEP, run_matcher_first=False)
    assert calls["n"] == 2
    assert report["done"] == 1, report
    assert _rows(db, food)["magnesium"] == pytest.approx(79.0)


def test_backfill_food_without_source(db):
    food = _setup(db, source_key="custom", external_id="")
    report = backfill_food_micros(db, batch=10, sleep=NOSLEEP, run_matcher_first=False,
                                  fdc_search=lambda q: [])
    assert report["no_source"] == 1, report
    row = db.query(FoodMicroBackfill).filter_by(food_id=food.id).one()
    assert row.status == "no_source" and row.source == "fdc_search"

def test_backfill_unknown_food_fdc_search_fallback(db):
    # drhiro_private-style food (no usda/off identity) -> live FDC search
    food = _setup(db, source_key="custom", external_id="")
    fake_search = lambda q: [{"fdc_id": 168462, "description": "Spinach, raw"}]  # noqa: E731
    fake_fdc = lambda ids: {168462: {1090: 79.0}}  # noqa: E731
    report = backfill_food_micros(db, batch=10, fdc_fetcher=fake_fdc,
                                  fdc_search=fake_search, sleep=NOSLEEP,
                                  run_matcher_first=False)
    assert report["done"] == 1 and report["no_source"] == 0, report
    assert _rows(db, food)["magnesium"] == pytest.approx(79.0)
    row = db.query(FoodMicroBackfill).filter_by(food_id=food.id).one()
    assert row.status == "done" and row.source == "fdc_search"


def test_backfill_rerun_skips_done_foods(db):
    food = _setup(db)
    fake = lambda ids: {168462: {1090: 79.0}}  # noqa: E731
    first = backfill_food_micros(db, batch=10, fdc_fetcher=fake, sleep=NOSLEEP, run_matcher_first=False)
    assert first["done"] == 1
    second = backfill_food_micros(db, batch=10, fdc_fetcher=fake, sleep=NOSLEEP, run_matcher_first=False)
    assert second["attempted"] == 0, second
