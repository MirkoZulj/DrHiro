"""Backfill + matcher tests (0.12.0 Part B, B2 / §4.3.2). Fake fetchers, no network."""
from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from drhiro_api.models import DataSource, Food, FoodNutrient, Meal, MealItem, Nutrient, User
from drhiro_api.services.food_micros import backfill_food_micros
from drhiro_api.services.intake_match import match_item_to_food

TZ = ZoneInfo("Europe/Zagreb")

def _nuts(db):
    from drhiro_nutrition.micros import NUTRIENT_CATALOGUE
    out = {}
    for c in NUTRIENT_CATALOGUE:
        n = Nutrient(nutrient_code=c.code, nutrient_label=c.label_en, unit=c.unit, category=c.group)
        db.add(n); db.flush(); out[c.code] = n
    return out

def _setup(db):
    nuts = _nuts(db)
    u = User(display_name="U", timezone="Europe/Zagreb")
    db.add(u); db.flush()
    ds = DataSource(source_key="usda", source_label="USDA"); db.add(ds); db.flush()
    food = Food(data_source_id=ds.id, external_id="999999", display_name="Cod liver oil")
    db.add(food); db.flush()
    energy = Nutrient(nutrient_code="energy", nutrient_label="Energy", unit="kcal", category="energy")
    db.add(energy); db.flush()
    db.add(FoodNutrient(food_id=food.id, nutrient_id=energy.id, amount_per_100g=900.0))
    m = Meal(user_id=u.id, eaten_at=datetime(2026, 11, 4, 12, 0, tzinfo=TZ).astimezone(__import__("datetime").timezone.utc),
             meal_type="lunch", status="confirmed")
    db.add(m); db.flush()
    db.add(MealItem(meal_id=m.id, food_catalog_item_id=str(food.id), display_name="Cod liver oil",
                    quantity=1, grams=10, nutrients_json={"kcal": 90}))
    db.commit()
    return food

def test_backfill_converts_copper_and_epa_dha(db):
    food = _setup(db)
    def fake_fdc(ids):
        return {999999: {1098: 1.0, 1278: 0.5, 1272: 0.3}}  # copper 1 mg, EPA/DHA g
    report = backfill_food_micros(db, batch=10, fdc_fetcher=fake_fdc, run_matcher_first=False)
    assert report["done"] == 1, report
    rows = {}
    for fn in db.query(FoodNutrient).filter_by(food_id=food.id).all():
        rows[db.query(Nutrient).get(fn.nutrient_id).nutrient_code] = fn.amount_per_100g
    assert rows["copper"] == pytest.approx(1000.0)
    assert rows["epa_dha"] == pytest.approx(800.0)

def test_matcher_kcal_gate(db):
    food = _setup(db)
    item = MealItem(meal_id=None, display_name="Cod liver oil", quantity=1, grams=10, nutrients_json={"kcal": 90})
    got, score = match_item_to_food(db, "Cod liver oil", item=item)
    assert got is not None and score >= 0.85
    item2 = MealItem(meal_id=None, display_name="Cod liver oil", quantity=1, grams=10, nutrients_json={"kcal": 9000})
    got2, _ = match_item_to_food(db, "Cod liver oil", item=item2)
    assert got2 is None

def test_matcher_searches_beyond_first_200(db):
    _setup(db)
    ds = db.query(DataSource).first()
    for i in range(250):
        db.add(Food(data_source_id=ds.id, external_id=f"filler-{i}",
                    display_name=f"Filler food {i}"))
    db.flush()
    target = Food(data_source_id=ds.id, external_id="777001",
                  display_name="Quinoa, cooked")
    db.add(target); db.flush()
    got, score = match_item_to_food(db, "Quinoa, cooked")
    assert got is not None and got.id == target.id and score >= 0.85

def test_log_time_attach_sets_micro_food_via_fdc(db, monkeypatch):
    from datetime import datetime, timezone
    from drhiro_api.services.log_intents import LogIntent, ParsedLog, _write_ledgers
    food = _setup(db)  # seeds nutrients + a usda food + confirmed meal
    user = db.query(User).first()
    monkeypatch.setenv("USDA_API_KEY", "test-key")
    def fake_resolve(db_, display_name, item=None, **k):
        return food, 0.9, 3
    monkeypatch.setattr("drhiro_api.services.food_micros.resolve_via_fdc_search", fake_resolve)
    parsed = ParsedLog(intents=[LogIntent(kind="meal", display_name="Zzunknownquinoa",
                                           grams=100)], meal_slot="lunch")
    result = _write_ledgers(db, user, parsed, datetime.now(timezone.utc), None, "test", "order-1")
    db.commit()
    mi = db.query(MealItem).filter_by(display_name="Zzunknownquinoa").one()
    assert mi.micro_food_id == food.id and mi.micro_match_confidence == 0.9
    assert result["meal_id"] is not None
