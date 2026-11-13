"""B0 acceptance (0.12.0 Part B): disposable-DB end to end.

Male, DOB 1985-06-01, Europe/Zagreb. Spinach (USDA per-100g: iron 2.71,
magnesium 79, vitamin K 483, folate 194) 200 g confirmed @12:00 local;
500 g draft ignored; Magnesium citrate elemental 100 mg x2 @23:30 local.
"""
from __future__ import annotations
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from tests.conftest import auth_headers
from drhiro_api.models import DataSource, DoseEvent, Food, FoodNutrient, Meal, MealItem, MedicationItem, Nutrient, User

DAY = "2026-11-04"
TZ = ZoneInfo("Europe/Zagreb")

def _seed_nutrients(db):
    from drhiro_nutrition.micros import NUTRIENT_CATALOGUE
    rows = {}
    for c in NUTRIENT_CATALOGUE:
        n = Nutrient(nutrient_code=c.code, nutrient_label=c.label_en, unit=c.unit, category=c.group)
        db.add(n); db.flush(); rows[c.code] = n
    return rows

def _setup(client, db):
    user = User(display_name="B0", timezone="Europe/Zagreb", date_of_birth="1985-06-01",
                sex_for_health_calculations="male")
    db.add(user); db.flush()
    nuts = _seed_nutrients(db)
    ds = DataSource(source_key="usda", source_label="USDA")
    db.add(ds); db.flush()
    food = Food(data_source_id=ds.id, external_id="168462", display_name="Spinach, raw")
    db.add(food); db.flush()
    for code, val in {"iron": 2.71, "magnesium": 79.0, "vitamin_k": 483.0, "folate": 194.0}.items():
        db.add(FoodNutrient(food_id=food.id, nutrient_id=nuts[code].id, amount_per_100g=val))
    def meal(status, grams, hour):
        m = Meal(user_id=user.id, eaten_at=datetime(2026, 11, 4, hour, 0, tzinfo=TZ).astimezone(timezone.utc),
                 meal_type="lunch", status=status)
        db.add(m); db.flush()
        db.add(MealItem(meal_id=m.id, food_catalog_item_id=str(food.id), display_name="Spinach, raw",
                        quantity=1, grams=grams, nutrients_json={"kcal": 23 * grams / 100}))
        db.flush()
    meal("confirmed", 200, 12)
    meal("draft", 500, 13)
    item = MedicationItem(user_id=user.id, kind="supplement", name="Magnesium citrate", unit_label="capsule",
                          dose_amount=2, dose_unit="capsule", status="active",
                          ingredients_json=[{"name": "Magnesium (as magnesium citrate)", "nutrient_code": "magnesium",
                                             "amount": 100, "unit": "mg", "per": "unit", "form": "elemental",
                                             "mapping": "confirmed"}])
    db.add(item); db.flush()
    db.add(DoseEvent(user_id=user.id, item_id=item.id, taken_at=datetime(2026, 11, 4, 23, 30, tzinfo=TZ).astimezone(timezone.utc),
                     status="taken", amount=2, source="test"))
    db.commit()
    return user

def test_b0_magnesium_iron_vitk_and_unknown_zinc(client, db):
    user = _setup(client, db)
    h = auth_headers(user)
    r = client.get(f"/api/v1/intake/day?date={DAY}", headers=h)
    assert r.status_code == 200, r.text
    by = {n["code"]: n for n in r.json()["nutrients"]}
    mg = by["magnesium"]
    assert abs(mg["total"] - 358) < 1, mg
    assert abs(mg["food"] - 158) < 1 and abs(mg["supp"] - 200) < 1, mg
    assert abs(mg["pct"] - 85.2) < 1.5, mg
    assert abs(by["iron"]["total"] - 5.42) < 0.1 and abs(by["iron"]["pct"] - 68) < 2, by["iron"]
    assert abs(by["vitamin_k"]["total"] - 966) < 1 and abs(by["vitamin_k"]["pct"] - 805) < 5, by["vitamin_k"]
    zinc = by["zinc"]
    assert zinc["status"] in ("partial", "food_missing") or zinc["total"] is None, zinc
    assert zinc["total"] != 0

def test_b0_overview_week_and_no_data_day(client, db):
    user = _setup(client, db)
    h = auth_headers(user)
    r = client.get(f"/api/v1/intake/overview?view=week&date={DAY}", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["days"]) == 7, body["days"]
    mg = next(n for n in body["nutrients"] if n["code"] == "magnesium")
    day_map = {d["date"]: d for d in mg["days"]}
    assert day_map[DAY]["status"] == "ok", day_map[DAY]
    other = next(d for d in mg["days"] if d["date"] != DAY)
    assert other["status"] == "no_data" and other["pct"] is None, other
