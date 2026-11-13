"""Intake v2 tests (0.12.0 Part B, spec §4.3-§4.6 / §6).

Legacy normaliser exclusions, food-side rules, day statuses, UL scope,
tenant isolation and the food-micros backfill job. HTTP-level tests target
/api/v1/intake/* (routers/intake.py); they land green once the router does.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from drhiro_api.services.schemas_meds import normalise_ingredient, suggest_nutrient_code
from drhiro_api.models import (
    DataSource, DoseEvent, Food, FoodNutrient, Measurement, Meal, MealItem,
    MedicationItem, Nutrient, User,
)
from tests.conftest import auth_headers  # noqa: E402

DAY = "2026-11-04"
TZ = ZoneInfo("Europe/Zagreb")


# ── Legacy keyword suggester exclusions (§4.4 / §6 R6) ───────────────────────

def test_calcium_pantothenate_suggests_pantothenic_not_calcium():
    assert suggest_nutrient_code("calcium pantothenate") == "pantothenic"


def test_pantothenic_acid_suggests_pantothenic():
    assert suggest_nutrient_code("pantothenic acid") == "pantothenic"


def test_magnesium_stearate_is_not_a_magnesium_source():
    assert suggest_nutrient_code("magnesium stearate") is None


def test_omega_6_and_9_are_not_epa_dha():
    assert suggest_nutrient_code("omega-6") is None
    assert suggest_nutrient_code("omega-9") is None


def test_fish_oil_and_total_omega_excluded():
    assert suggest_nutrient_code("fish oil") is None
    assert suggest_nutrient_code("total omega-3") is None


def test_legit_sources_still_suggest():
    assert suggest_nutrient_code("zinc bisglycinate") == "zinc"
    assert suggest_nutrient_code("EPA 500mg") == "epa_dha"


def test_normalise_legacy_pantothenate():
    entry = normalise_ingredient({"name": "calcium pantothenate", "amount": 100, "unit": "mg"})
    assert entry.nutrient_code == "pantothenic"
    assert entry.mapping == "suggested"


# ── Fixtures for HTTP coverage tests ─────────────────────────────────────────

def _seed_nutrients(db):
    from drhiro_nutrition.micros import NUTRIENT_CATALOGUE
    rows = {}
    for c in NUTRIENT_CATALOGUE:
        n = Nutrient(nutrient_code=c.code, nutrient_label=c.label_en, unit=c.unit, category=c.group)
        db.add(n); db.flush(); rows[c.code] = n
    return rows


def _make_food(db, nuts, name, per100: dict, external_id="1", source_key="usda"):
    ds = DataSource(source_key=source_key, source_label=source_key.upper())
    db.add(ds); db.flush()
    food = Food(data_source_id=ds.id, external_id=external_id, display_name=name)
    db.add(food); db.flush()
    for code, val in per100.items():
        db.add(FoodNutrient(food_id=food.id, nutrient_id=nuts[code].id, amount_per_100g=val))
    return food


def _meal(db, user, food, *, status, grams=None, volume_ml=None, hour=12, day=4):
    m = Meal(user_id=user.id, eaten_at=datetime(2026, 11, day, hour, 0, tzinfo=TZ).astimezone(timezone.utc),
             meal_type="lunch", status=status)
    db.add(m); db.flush()
    db.add(MealItem(meal_id=m.id, food_catalog_item_id=str(food.id), display_name=food.display_name,
                    quantity=1, grams=grams, volume_ml=volume_ml, nutrients_json={"kcal": 100}))
    db.commit()
    return m


def _dose(db, user, *, per_unit_mg=None, amount=1, nutrient="magnesium", name="Magnesium", form="elemental"):
    if per_unit_mg is None:
        per_unit_mg = 100
    item = MedicationItem(user_id=user.id, kind="supplement", name=name, unit_label="capsule",
                          dose_amount=amount, dose_unit="capsule", status="active",
                          ingredients_json=[{"name": name, "nutrient_code": nutrient,
                                             "amount": per_unit_mg, "unit": "mg", "per": "unit",
                                             "form": form, "mapping": "confirmed"}])
    db.add(item); db.flush()
    db.add(DoseEvent(user_id=user.id, item_id=item.id,
                     taken_at=datetime(2026, 11, 4, 12, 0, tzinfo=TZ).astimezone(timezone.utc),
                     status="taken", amount=amount, source="test"))
    db.commit()
    return item


def _get_day(client, user, date=DAY):
    r = client.get(f"/api/v1/intake/day?date={date}", headers=auth_headers(user))
    assert r.status_code == 200, r.text
    return r.json()


def _user(db, name, **kw):
    u = User(display_name=name, timezone="Europe/Zagreb", date_of_birth="1985-06-01",
             sex_for_health_calculations="male", **kw)
    db.add(u); db.commit(); db.refresh(u)
    return u


# ── Food-side rules (§4.3) ────────────────────────────────────────────────────

def test_draft_meal_ignored(client, db):
    user = _user(db, "draft")
    nuts = _seed_nutrients(db)
    food = _make_food(db, nuts, "Spinach", {"zinc": 0.5})
    _meal(db, user, food, status="confirmed", grams=100)
    _meal(db, user, food, status="draft", grams=500)
    by = {n["code"]: n for n in _get_day(client, user)["nutrients"]}
    assert abs(by["zinc"]["food"] - 0.5) < 1e-9, by["zinc"]


def test_food_without_nutrient_row_is_unknown_not_zero(client, db):
    user = _user(db, "unknown")
    nuts = _seed_nutrients(db)
    food = _make_food(db, nuts, "Spinach", {"iron": 2.71})  # no zinc row
    _meal(db, user, food, status="confirmed", grams=200)
    by = {n["code"]: n for n in _get_day(client, user)["nutrients"]}
    zinc = by["zinc"]
    assert zinc["food"] is None and zinc["total"] is None, zinc
    assert zinc["completeness"] is not None and zinc["completeness"] < 1, zinc


def test_beverage_250ml_scales_by_2_5(client, db):
    user = _user(db, "bev")
    nuts = _seed_nutrients(db)
    food = _make_food(db, nuts, "Orange juice", {"vitamin_c": 50.0})
    _meal(db, user, food, status="confirmed", volume_ml=250)
    by = {n["code"]: n for n in _get_day(client, user)["nutrients"]}
    assert abs(by["vitamin_c"]["food"] - 125.0) < 1e-9, by["vitamin_c"]


def test_water_only_measurement_changes_nothing(client, db):
    user = _user(db, "water")
    nuts = _seed_nutrients(db)
    food = _make_food(db, nuts, "Spinach", {"iron": 2.71})
    _meal(db, user, food, status="confirmed", grams=100)
    before = _get_day(client, user)["nutrients"]
    db.add(Measurement(user_id=user.id, metric_type="water", start_at=datetime(2026, 11, 4, 14, 0, tzinfo=TZ).astimezone(timezone.utc),
                       value_json={"ml": 500}, unit="ml", source_provider="test", source_record_id="w1"))
    db.commit()
    after = _get_day(client, user)["nutrients"]
    assert before == after


def test_no_data_day_and_food_missing_status(client, db):
    user = _user(db, "statuses")
    nuts = _seed_nutrients(db)
    _dose(db, user)  # supplement only, no meals
    by = {n["code"]: n for n in _get_day(client, user)["nutrients"]}
    assert by["magnesium"]["status"] == "food_missing", by["magnesium"]
    r = client.get(f"/api/v1/intake/overview?view=week&date={DAY}", headers=auth_headers(user))
    assert r.status_code == 200, r.text
    mg = next(n for n in r.json()["nutrients"] if n["code"] == "magnesium")
    other = next(d for d in mg["days"] if d["date"] != DAY)
    assert other["status"] == "no_data" and other["pct"] is None, other


# ── UL scope (§4.2): magnesium UL 350 applies to supplements only ────────────

def test_mg_ul_scoped_to_supplements(client, db):
    user = _user(db, "ul-ok")
    nuts = _seed_nutrients(db)
    food = _make_food(db, nuts, "Mg-rich", {"magnesium": 200.0})  # 200 mg per 100 g
    _meal(db, user, food, status="confirmed", grams=100)          # 200 mg food
    _dose(db, user, per_unit_mg=300, amount=1)                    # 300 mg supp
    by = {n["code"]: n for n in _get_day(client, user)["nutrients"]}
    assert by["magnesium"]["above_ul"] is False, by["magnesium"]


def test_mg_ul_exceeded_by_supplement(client, db):
    user = _user(db, "ul-over")
    nuts = _seed_nutrients(db)
    _dose(db, user, per_unit_mg=400, amount=1)  # 400 mg supp alone
    by = {n["code"]: n for n in _get_day(client, user)["nutrients"]}
    assert by["magnesium"]["above_ul"] is True, by["magnesium"]


# ── Compound minerals are skipped and land in unknowns (§4.4) ────────────────

def test_compound_magnesium_skipped_to_unknowns(client, db):
    user = _user(db, "compound")
    _seed_nutrients(db)
    _dose(db, user, per_unit_mg=500, amount=1, name="Magnesium citrate", form="compound")
    body = _get_day(client, user)
    by = {n["code"]: n for n in body["nutrients"]}
    assert by["magnesium"]["supp"] in (None, 0.0), by["magnesium"]
    assert any(u.get("code") == "magnesium" for u in body.get("unknowns", [])), body.get("unknowns")


# ── Tenant isolation (§4.6) ───────────────────────────────────────────────────

def test_overview_tenant_isolation(client, db):
    user_a = _user(db, "A")
    user_b = _user(db, "B")
    nuts = _seed_nutrients(db)
    food = _make_food(db, nuts, "Spinach", {"iron": 2.71})
    _meal(db, user_a, food, status="confirmed", grams=200)
    r = client.get(f"/api/v1/intake/overview?view=week&date={DAY}", headers=auth_headers(user_b))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["days"]) == 7
    fe = next(n for n in body["nutrients"] if n["code"] == "iron")
    assert all(d["status"] == "no_data" and d["pct"] is None for d in fe["days"]), fe
