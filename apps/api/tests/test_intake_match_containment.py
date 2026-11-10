"""Token-containment matcher + AI-estimated FDC search tests (0.12.0-d).

Covers: short/generic names matching long FDC names via token containment
(beer -> Beer, regular; Cashews -> Cashews, raw), non-English items matching
on the English parenthetical (sušena govedina), the '(AI-estimated)' marker
being stripped, and the single-token FDC search tier for compound
AI-estimated names (homemade pizza -> Pizza, cheese). Fake fetchers, no network.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from drhiro_api.models import DataSource, Food, Nutrient, FoodNutrient, Meal, MealItem, User
from drhiro_api.services.intake_match import (
    base_name, contains, match_item_to_food, parenthetical,
)
from drhiro_api.services.food_micros import _best_fdc_match

TZ = ZoneInfo("Europe/Zagreb")
NOSLEEP = lambda s: None  # noqa: E731


def _energy(db, food_id, kcal):
    n = db.query(Nutrient).filter_by(nutrient_code="energy").first()
    if n is None:
        n = Nutrient(nutrient_code="energy", nutrient_label="Energy", unit="kcal", category="macro")
        db.add(n); db.flush()
    db.add(FoodNutrient(food_id=food_id, nutrient_id=n.id, amount_per_100g=float(kcal)))
    db.commit()
    return n


def _food(db, name, kcal=None):
    import uuid
    ds = db.query(DataSource).filter_by(source_key="usda").first()
    if ds is None:
        ds = DataSource(source_key="usda", source_label="USDA")
        db.add(ds); db.flush()
    f = Food(data_source_id=ds.id, external_id=uuid.uuid4().hex[:12], display_name=name)
    db.add(f); db.flush()
    if kcal is not None:
        _energy(db, f.id, kcal)
    return f


def _item(db, name, kcal=None, grams: float = 100):
    u = User(display_name="U", timezone="Europe/Zagreb")
    db.add(u); db.flush()
    m = Meal(user_id=u.id, eaten_at=datetime(2026, 11, 4, 12, tzinfo=TZ).astimezone(timezone.utc),
             meal_type="lunch", status="confirmed")
    db.add(m); db.flush()
    db.add(MealItem(meal_id=m.id, display_name=name, quantity=1, grams=grams,
                    nutrients_json={"kcal": kcal} if kcal else {}))
    db.commit()
    return db.query(MealItem).filter_by(display_name=name).first()


def test_base_name_strips_ai_marker():
    assert base_name("homemade pizza (AI-estimated)") == "homemade pizza"
    assert base_name("roasted corn snack, chili (AI-estimated)") == "roasted corn snack, chili"


def test_base_name_strips_quantity_suffix():
    assert base_name("Kefir, 150 G") == "Kefir"
    assert base_name("Water, Plain, 0 G") == "Water, Plain"
    assert base_name("Beer, regular, 330 ml") == "Beer, regular"
    assert base_name("Beef, 80% lean, raw") == "Beef, 80% lean, raw"


def test_base_name_prefers_english_parenthetical():
    assert base_name("sušena govedina (Beef, cured, dried)") == "Beef, cured, dried"


def test_parenthetical():
    assert parenthetical("beer (AI-estimated)") == "AI-estimated"
    assert parenthetical("plain beer") is None


def test_contains_basic():
    assert contains({"beer"}, "Beer, regular")
    assert contains({"cashews"}, "Cashews, raw")
    assert not contains({"homemade", "pizza"}, "Pizza, cheese")


def test_local_match_beer_containment(db):
    _food(db, "Beer, regular", kcal=43)
    _food(db, "Beef, ground, raw", kcal=155)
    food, score = match_item_to_food(db, "beer")
    assert food is not None and food.display_name == "Beer, regular"
    assert score > 0


def test_local_match_cashews(db):
    _food(db, "Cashews, raw")
    food, _ = match_item_to_food(db, "Cashews")
    assert food is not None and food.display_name == "Cashews, raw"


def test_local_match_plural_fold(db):
    """'cashews' must match 'Nuts, cashew nuts, raw' (plural vs stem)."""
    _food(db, "Nuts, cashew nuts, raw")
    food, _ = match_item_to_food(db, "cashews")
    assert food is not None and food.display_name == "Nuts, cashew nuts, raw"


def test_beer_does_not_match_beer_mustard(db):
    """'beer' must not latch onto a candidate that adds a different food."""
    _food(db, "beer mustard")
    _food(db, "Beer, regular")
    food, _ = match_item_to_food(db, "beer")
    assert food is not None and food.display_name == "Beer, regular"


def test_kefir_matches_despite_tiny_basis(db):
    """grams=1.5 makes kcal/100g noise; the gate must not block the match."""
    _food(db, "Kefir, lowfat, plain, LIFEWAY", kcal=60)
    it = _item(db, "Kefir, 150 G", kcal=2.08, grams=1.5)
    food, _ = match_item_to_food(db, it.display_name, item=it)
    assert food is not None and food.display_name == "Kefir, lowfat, plain, LIFEWAY"


def test_beeF_kcal_gated(db):
    _food(db, "Beef, ground, raw", kcal=155)
    # item claims 23 kcal/100g (like spinach) -> beef must be gated out
    it = _item(db, "BEEF", kcal=23)
    food, _ = match_item_to_food(db, "BEEF", item=it)
    assert food is None


def test_local_match_beeF_without_kcal(db):
    _food(db, "Beef, ground, raw", kcal=155)
    food, _ = match_item_to_food(db, "BEEF")
    assert food is not None and food.display_name == "Beef, ground, raw"


def test_local_match_croatian_parenthetical(db):
    _food(db, "Beef, dried, cured")
    food, score = match_item_to_food(db, "sušena govedina (Beef, cured, dried)")
    assert food is not None and food.display_name == "Beef, dried, cured"
    assert score > 0  # matched via containment tier (word-order swap)


def test_fdc_token_tier_ai_estimated_pizza():
    def fake_search(q):
        if "pizza" in q.lower():
            return [{"fdc_id": 19175, "description": "Pizza, cheese"}]
        return []
    best = _best_fdc_match("homemade pizza (AI-estimated)",
                           fdc_search=fake_search, sleep=NOSLEEP)
    assert best is not None and best[0] == 19175


def test_fdc_croatian_parenthetical_search():
    def fake_search(q):
        if "beef" in q.lower():
            return [{"fdc_id": 11246, "description": "Beef, dried, cured"}]
        return []
    best = _best_fdc_match("sušena govedina (Beef, cured, dried)",
                           fdc_search=fake_search, sleep=NOSLEEP)
    assert best is not None and best[0] == 11246


def test_fdc_no_match_returns_none():
    best = _best_fdc_match("Sudzuk (smoked beef salami, Gavrilovic)",
                           fdc_search=lambda q: [], sleep=NOSLEEP)
    assert best is None


def test_fdc_token_tier_kcal_gate():
    def fake_search(q):
        if "wine" in q.lower():
            return [{"fdc_id": 177111, "description": "Wine, sparkling"}]
        return []
    class It:
        nutrients_json = {"kcal": 200}  # 200/100g claim
        grams = 100
        volume_ml = None
    # sparkling wine ~70-90 kcal/100g: 200 claim must be gated out
    best = _best_fdc_match("sparkling wine (AI-estimated)", item=It,
                           fdc_search=fake_search,
                           fdc_fetcher=lambda ids: {177111: {1008: 73.0}}, sleep=NOSLEEP)
    assert best is None
    # realistic claim passes
    It.nutrients_json = {"kcal": 78}
    best = _best_fdc_match("sparkling wine (AI-estimated)", item=It,
                           fdc_search=fake_search,
                           fdc_fetcher=lambda ids: {177111: {1008: 73.0}}, sleep=NOSLEEP)
    assert best is not None and best[0] == 177111
