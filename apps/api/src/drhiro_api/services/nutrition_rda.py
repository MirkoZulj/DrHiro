"""Nutrition Coverage (0.10.3 wave 4): RDA reference + daily coverage.

RDA/AI reference values are curated constants for a healthy adult male,
sourced per-row from NIH Office of Dietary Supplements (ODS) Dietary
Reference Intakes fact sheets unless noted. Age bands: values are the
31-50y male DRI where the DRI differs by adult age band; a couple of
nutrients (magnesium, vitamin B6) differ between 19-30 and 31-50 and are
handled by ``rda_for_age``.

Supplement intake is derived from today's taken dose_events joined to
medication_items.ingredients_json (label facts, flexible keys — see
``supplement_intake_for_day``). Food intake comes from meal_items'
nutrients_json; the food DB currently stores only kcal/macros/fiber/
sodium, so micronutrient food values are None ('—' in the UI) rather
than fabricated.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

# key -> (display name, RDA/AI adult male, canonical unit, source)
# Canonical units: mcg for fat-soluble vitamins & selenium/iodine,
# mg for water-soluble vitamins & zinc/iron/omega-3, mg for minerals.
RDA_TABLE: dict[str, tuple[str, float, str, str]] = {
    # NIH ODS: RDA 900 mcg RAE/day, adult males
    "vitamin_a": ("Vitamin A", 900, "mcg", "NIH ODS Vitamin A RDA, adult male"),
    # NIH ODS: RDA 90 mg/day, adult males
    "vitamin_c": ("Vitamin C", 90, "mg", "NIH ODS Vitamin C RDA, adult male"),
    # NIH ODS: RDA 15 mcg (600 IU)/day, adults <=70y
    "vitamin_d": ("Vitamin D", 15, "mcg", "NIH ODS Vitamin D RDA, adult male"),
    # NIH ODS: RDA 15 mg/day, adults
    "vitamin_e": ("Vitamin E", 15, "mg", "NIH ODS Vitamin E RDA, adult male"),
    # NIH ODS: AI 120 mcg/day, adult males
    "vitamin_k": ("Vitamin K", 120, "mcg", "NIH ODS Vitamin K AI, adult male"),
    # NIH ODS: RDA 1.2 mg/day, adult males
    "vitamin_b1": ("Vitamin B1 (thiamine)", 1.2, "mg", "NIH ODS Thiamin RDA, adult male"),
    # NIH ODS: RDA 1.3 mg/day, adult males
    "vitamin_b2": ("Vitamin B2 (riboflavin)", 1.3, "mg", "NIH ODS Riboflavin RDA, adult male"),
    # NIH ODS: RDA 16 mg NE/day, adult males
    "vitamin_b3": ("Vitamin B3 (niacin)", 16, "mg", "NIH ODS Niacin RDA, adult male"),
    # NIH ODS: RDA 1.7 mg/day, adult males 31-50y (1.3 mg at 19-30y)
    "vitamin_b6": ("Vitamin B6", 1.7, "mg", "NIH ODS Vitamin B6 RDA, adult male 31-50y"),
    # NIH ODS: RDA 400 mcg DFE/day, adults
    "folate": ("Folate (B9)", 400, "mcg", "NIH ODS Folate RDA, adult male"),
    # NIH ODS: RDA 2.4 mcg/day, adults
    "vitamin_b12": ("Vitamin B12", 2.4, "mcg", "NIH ODS Vitamin B12 RDA, adult male"),
    # NIH ODS: RDA 1000 mg/day, adult males 31-50y
    "calcium": ("Calcium", 1000, "mg", "NIH ODS Calcium RDA, adult male"),
    # NIH ODS: RDA 420 mg/day, adult males 31-50y (400 mg at 19-30y)
    "magnesium": ("Magnesium", 420, "mg", "NIH ODS Magnesium RDA, adult male 31-50y"),
    # NIH ODS: RDA 11 mg/day, adult males
    "zinc": ("Zinc", 11, "mg", "NIH ODS Zinc RDA, adult male"),
    # NIH ODS: RDA 8 mg/day, adult males
    "iron": ("Iron", 8, "mg", "NIH ODS Iron RDA, adult male"),
    # NIH ODS: RDA 55 mcg/day, adults
    "selenium": ("Selenium", 55, "mcg", "NIH ODS Selenium RDA, adult male"),
    # EFSA: 250 mg/day EPA+DHA adequate intake (adult guideline used in
    # place of an NIH ODS RDA, which does not set one for EPA/DHA)
    "omega3_epa_dha": ("Omega-3 (EPA+DHA)", 250, "mg", "EFSA EPA+DHA adequate intake"),
    # NIH ODS: AI 3400 mg/day, adult males (2019 NASEM AI)
    "potassium": ("Potassium", 3400, "mg", "NIH ODS Potassium AI, adult male"),
    # NIH ODS: RDA 150 mcg/day, adults
    "iodine": ("Iodine", 150, "mcg", "NIH ODS Iodine RDA, adult male"),
}

# Age-band overrides: key -> (max_age_inclusive, value) applied while age <= bound.
_AGE_OVERRIDES: dict[str, list[tuple[int, float]]] = {
    "magnesium": [(30, 400)],
    "vitamin_b6": [(30, 1.3)],
}

# Ingredient component/name keywords -> RDA key (first match wins).
_INGREDIENT_KEYWORDS: list[tuple[str, str]] = [
    ("epa", "omega3_epa_dha"), ("dha", "omega3_epa_dha"),
    ("omega", "omega3_epa_dha"), ("fish oil", "omega3_epa_dha"),
    ("magnes", "magnesium"), ("calcium", "calcium"), ("zinc", "zinc"),
    ("iron", "iron"), ("ferrous", "iron"), ("selenium", "selenium"),
    ("potassium", "potassium"), ("iodine", "iodine"), ("iodide", "iodine"),
    ("vitamin a", "vitamin_a"), ("retinol", "vitamin_a"), ("beta-carotene", "vitamin_a"),
    ("vitamin c", "vitamin_c"), ("ascorb", "vitamin_c"),
    ("vitamin d", "vitamin_d"), ("cholecalciferol", "vitamin_d"),
    ("ergocalciferol", "vitamin_d"), ("vitamin e", "vitamin_e"),
    ("tocopher", "vitamin_e"), ("vitamin k", "vitamin_k"), ("menaquinone", "vitamin_k"),
    ("phylloquinone", "vitamin_k"),
    ("thiamin", "vitamin_b1"), ("vitamin b1", "vitamin_b1"),
    ("riboflavin", "vitamin_b2"), ("vitamin b2", "vitamin_b2"),
    ("niacin", "vitamin_b3"), ("vitamin b3", "vitamin_b3"),
    ("pyridoxine", "vitamin_b6"), ("vitamin b6", "vitamin_b6"),
    ("folate", "folate"), ("folic", "folate"), ("vitamin b9", "folate"),
    ("cobalamin", "vitamin_b12"), ("vitamin b12", "vitamin_b12"),
]

# Meal-item nutrients_json keys -> RDA key (food DB may gain these later;
# today only kcal/macros/fiber/sodium exist, so food values stay None).
_FOOD_KEY_MAP: dict[str, str] = {
    "vitamin_a_mcg": "vitamin_a", "vitamin_c_mg": "vitamin_c",
    "vitamin_d_mcg": "vitamin_d", "vitamin_e_mg": "vitamin_e",
    "vitamin_k_mcg": "vitamin_k", "vitamin_b1_mg": "vitamin_b1",
    "vitamin_b2_mg": "vitamin_b2", "vitamin_b3_mg": "vitamin_b3",
    "vitamin_b6_mg": "vitamin_b6", "folate_mcg": "folate",
    "vitamin_b12_mcg": "vitamin_b12", "calcium_mg": "calcium",
    "magnesium_mg": "magnesium", "zinc_mg": "zinc", "iron_mg": "iron",
    "selenium_mcg": "selenium", "omega3_epa_dha_mg": "omega3_epa_dha",
    "potassium_mg": "potassium", "iodine_mcg": "iodine",
}

_IU_TO_CANONICAL = {  # 1 IU in mcg (or mg for E, handled by unit check)
    "vitamin_a": 0.3, "vitamin_d": 0.025,
}


def rda_for_age(key: str, age: int) -> float:
    _name, base, _unit, _src = RDA_TABLE[key]
    for max_age, val in _AGE_OVERRIDES.get(key, []):
        if age <= max_age:
            return val
    return base


def age_from_profile(user, today: date | None = None) -> int:
    """Age from users.date_of_birth when present, else default 45."""
    dob = getattr(user, "date_of_birth", None)
    if dob:
        try:
            born = date.fromisoformat(str(dob)[:10])
            ref = today or date.today()
            return max(0, ref.year - born.year - ((ref.month, ref.day) < (born.month, born.day)))
        except ValueError:
            pass
    return 45


def _to_canonical(value: float, unit: str | None, key: str, canon_unit: str) -> float:
    u = (unit or "").strip().lower()
    if u in ("iu",):
        if key == "vitamin_e":
            return value * 0.67  # mg per IU (natural alpha-tocopherol)
        return value * _IU_TO_CANONICAL.get(key, 0.0)
    factor = {"g": 1000.0, "mg": 1.0, "mcg": 0.001, "ug": 0.001, "µg": 0.001}
    if u not in factor:
        return value  # unknown unit: assume already canonical
    in_mg = value * factor[u]
    if canon_unit == "mg":
        return in_mg
    return in_mg * 1000.0  # mcg


def _ingredient_amount(entry: dict, canon_unit: str, key: str):
    """Extract (per-1-dose amount in canonical unit) from one
    ingredients_json entry. Flexible label-fact keys: prefers an explicit
    ``amount`` (+ optional ``unit``), then ``per_1_*`` keys, then
    ``per_2_*`` keys (halved to per-1), then any *_mg/_mcg key."""
    def unit_of(k: str) -> str | None:
        kl = k.lower()
        for suf, u in (("_mg", "mg"), ("_mcg", "mcg"), ("_ug", "mcg"),
                       ("_g", "g"), ("_iu", "iu"), ("mg", "mg"), ("mcg", "mcg")):
            if kl.endswith(suf):
                return u
        return None

    def num(k):
        v = entry.get(k)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    # 1) explicit amount + unit
    amt = num("amount")
    if amt is not None:
        return _to_canonical(amt, entry.get("unit"), key, canon_unit)
    # 2) per_1 keys (per-1-dose basis)
    for k, v in entry.items():
        if isinstance(v, (int, float)) and "per_1" in k.lower():
            u = unit_of(k)
            if u:
                return _to_canonical(float(v), u, key, canon_unit)
    # 3) per_2 keys -> halve to per-1
    for k, v in entry.items():
        if isinstance(v, (int, float)) and "per_2" in k.lower():
            u = unit_of(k)
            if u:
                return _to_canonical(float(v) / 2.0, u, key, canon_unit)
    # 4) any amount-looking key
    for k, v in entry.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            u = unit_of(k)
            if u:
                return _to_canonical(float(v), u, key, canon_unit)
    return None


def ingredient_to_rda(entry: dict) -> tuple[str, float] | None:
    """Map one ingredients_json entry to (rda_key, per-1 amount canonical)."""
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("component") or entry.get("name")
               or entry.get("ingredient") or "").lower()
    if not name:
        return None
    key = next((k for kw, k in _INGREDIENT_KEYWORDS if kw in name), None)
    if key is None or key not in RDA_TABLE:
        return None
    amt = _ingredient_amount(entry, RDA_TABLE[key][2], key)
    if amt is None:
        return None
    return key, amt


def supplement_intake_for_day(db, user, day: date, tz) -> dict[str, float]:
    """Today's taken doses scaled by dose amount, per RDA key."""
    from drhiro_api.models import DoseEvent, MedicationItem

    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    doses = (
        db.query(DoseEvent)
        .filter(DoseEvent.user_id == user.id, DoseEvent.status == "taken",
                DoseEvent.taken_at >= start, DoseEvent.taken_at < end)
        .all()
    )
    totals: dict[str, float] = {}
    for dose in doses:
        item = db.query(MedicationItem).filter(MedicationItem.id == dose.item_id).first()
        if item is None or not item.ingredients_json:
            continue
        amount = dose.amount if dose.amount is not None else (item.dose_amount or 1.0)
        for entry in item.ingredients_json:
            mapped = ingredient_to_rda(entry)
            if mapped is None:
                continue
            key, per_one = mapped
            totals[key] = totals.get(key, 0.0) + per_one * float(amount or 1.0)
    return totals


def food_intake_for_day(db, user, day: date, tz) -> dict[str, float]:
    """Today's meal_items nutrients_json aggregated per RDA key where the
    food DB actually has the nutrient; keys with no data are absent
    (rendered as '—', never fabricated)."""
    from drhiro_api.models import Meal, MealItem

    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    meals = (
        db.query(Meal)
        .filter(Meal.user_id == user.id, Meal.eaten_at >= start, Meal.eaten_at < end)
        .all()
    )
    totals: dict[str, float] = {}
    for meal in meals:
        items = db.query(MealItem).filter(MealItem.meal_id == meal.id).all()
        for item in items:
            n = item.nutrients_json or {}
            if not isinstance(n, dict):
                continue
            for food_key, rda_key in _FOOD_KEY_MAP.items():
                v = n.get(food_key)
                if isinstance(v, (int, float)):
                    totals[rda_key] = totals.get(rda_key, 0.0) + float(v)
    return totals


def coverage_rows(db, user, day: date, tz) -> list[dict]:
    age = age_from_profile(user, day)
    supp = supplement_intake_for_day(db, user, day, tz)
    food = food_intake_for_day(db, user, day, tz)
    rows = []
    for key, (name, _base, unit, source) in RDA_TABLE.items():
        rda = rda_for_age(key, age)
        s = supp.get(key, 0.0)
        f = food.get(key)
        have = s + (f or 0.0)
        rows.append({
            "key": key, "name": name, "rda": rda, "unit": unit,
            "from_supplements": round(s, 2),
            "from_food": round(f, 2) if f is not None else None,
            "coverage_pct": round(have / rda * 100, 1) if rda else None,
            "source": source, "age": age,
        })
    return rows
