"""Thin shim kept for one release (0.12.0 Part B, spec §4.2).

Reference values now live in drhiro_nutrition.dri (sex x age band).
RDA_TABLE is built from the male 31-50 base so legacy callers keep working;
new code should use reference_for() directly.
"""
from __future__ import annotations
from datetime import date
from drhiro_nutrition.dri import DRI, DRI_VERSION, age_from_dob, reference_for
from drhiro_nutrition.micros import NUTRIENT_CATALOGUE

RDA_TABLE = {c.code: (c.label_en, float(DRI[c.code]["m"]), c.unit, f"NIH ODS {c.label_en} (NASEM)")
             for c in NUTRIENT_CATALOGUE}
RDA_TABLE["vitamin_b1"] = ("Vitamin B1 (thiamine)", 1.2, "mg", "NIH ODS Thiamin RDA, adult male")
RDA_TABLE["vitamin_b2"] = ("Vitamin B2 (riboflavin)", 1.3, "mg", "NIH ODS Riboflavin RDA, adult male")
RDA_TABLE["vitamin_b3"] = ("Vitamin B3 (niacin)", 16, "mg", "NIH ODS Niacin RDA, adult male")
RDA_TABLE["omega3_epa_dha"] = ("Omega-3 (EPA+DHA)", 250, "mg", "EFSA EPA+DHA adequate intake")
_LEGACY = {"vitamin_b1": "thiamin", "vitamin_b2": "riboflavin", "vitamin_b3": "niacin", "omega3_epa_dha": "epa_dha"}

def rda_for_age(key: str, age: int) -> float:
    code = _LEGACY.get(key, key)
    return float(reference_for(code, "male", age)["value"])

def age_from_profile(user, today: date | None = None) -> int:
    age = age_from_dob(getattr(user, "date_of_birth", None), today)
    return age if age is not None else 45

__all__ = ["RDA_TABLE", "rda_for_age", "age_from_profile", "reference_for", "DRI_VERSION"]

# ── Legacy shim functions (kept one release for wave-4 callers/tests) ──
from datetime import datetime, time, timedelta, timezone as _tz
from drhiro_nutrition.micros import to_canonical as _to_canonical_new, UnmappedUnit as _Unmapped
from drhiro_api.services.schemas_meds import suggest_nutrient_code as _suggest

_LEGACY_KEY = {"thiamin": "vitamin_b1", "riboflavin": "vitamin_b2", "niacin": "vitamin_b3", "epa_dha": "omega3_epa_dha"}

def _omega3_kind(name: str):
    n = (name or "").lower()
    if "epa" in n or "dha" in n:
        return "specific"
    if "dpa" in n or "docosapentaenoic" in n:
        return "dpa"
    return "generic"

def _ingredient_amount(entry: dict, canon_unit: str, key: str):
    # legacy flexible keys -> (amount, unit)
    def unit_of(k: str):
        kl = k.lower()
        for suf, u in (("_mg", "mg"), ("_mcg", "mcg"), ("_ug", "mcg"), ("_g", "g"), ("_iu", "iu")):
            if kl.endswith(suf):
                return u
        return None
    def num(k):
        v = entry.get(k)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    amt = num("amount")
    if amt is not None:
        return amt, entry.get("unit")
    for k, v in entry.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) and "per_1" in k.lower() and unit_of(k):
            return float(v), unit_of(k)
    for k, v in entry.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) and "per_2" in k.lower() and unit_of(k):
            return float(v) / 2.0, unit_of(k)
    for k, v in entry.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) and unit_of(k):
            return float(v), unit_of(k)
    return None, None

def ingredient_to_rda(entry: dict):
    """Legacy (key, per-1 canonical amount) with old keyword map."""
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("component") or entry.get("name") or entry.get("ingredient") or "").lower()
    if not name:
        return None
    code = _suggest(name)
    if code is None:
        # legacy map included generic omega; suggest() excludes fish oil/total omega.
        if any(w in name for w in ("fish oil", "total omega", "omega")):
            code = "epa_dha"
        else:
            return None
    if code == "epa_dha" and _omega3_kind(name) == "dpa":
        return None
    amt, unit = _ingredient_amount(entry, None, code)
    if amt is None:
        return None
    try:
        canon, _assumed = _to_canonical_new(amt, unit, code, name=name)
    except _Unmapped:
        return None
    return _LEGACY_KEY.get(code, code), canon

def supplement_intake_for_day(db, user, day, tz) -> dict:
    from drhiro_api.models import DoseEvent, MedicationItem
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    doses = db.query(DoseEvent).filter(DoseEvent.user_id == user.id, DoseEvent.status == "taken",
                                       DoseEvent.taken_at >= start, DoseEvent.taken_at < end).all()
    totals = {}
    for dose in doses:
        item = db.query(MedicationItem).filter(MedicationItem.id == dose.item_id).first()
        if item is None or not item.ingredients_json:
            continue
        amount = dose.amount if dose.amount is not None else (item.dose_amount or 1.0)
        mapped = []
        for entry in item.ingredients_json:
            m = ingredient_to_rda(entry)
            if m is None:
                continue
            key, per_one = m
            name = str(entry.get("component") or entry.get("name") or entry.get("ingredient") or "").lower()
            mapped.append((key, per_one, _omega3_kind(name) if key == "omega3_epa_dha" else None))
        has_specific = any(k == "omega3_epa_dha" and kind == "specific" for k, _, kind in mapped)
        for key, per_one, kind in mapped:
            if key == "omega3_epa_dha" and kind == "generic" and has_specific:
                continue
            totals[key] = totals.get(key, 0.0) + per_one * float(amount or 1.0)
    return totals

def food_intake_for_day(db, user, day, tz) -> dict:
    from drhiro_api.services.intake import intake_range
    res = intake_range(db, user, day, day)
    out = {}
    for n in res["nutrients_raw"]:
        d = n["days"][0] if n["days"] else {}
        if d.get("food") is not None:
            out[_LEGACY_KEY.get(n["code"], n["code"])] = d["food"]
    return out

def coverage_rows(db, user, day, tz) -> list:
    age = age_from_profile(user, day)
    supp = supplement_intake_for_day(db, user, day, tz)
    food = food_intake_for_day(db, user, day, tz)
    rows = []
    for key, (name, _base, unit, source) in RDA_TABLE.items():
        rda = rda_for_age(key, age)
        s = supp.get(key, 0.0)
        f = food.get(key)
        have = s + (f or 0.0)
        rows.append({"key": key, "name": name, "rda": rda, "unit": unit,
                     "from_supplements": round(s, 2), "from_food": round(f, 2) if f is not None else None,
                     "coverage_pct": round(have / rda * 100, 1) if rda else None, "source": source, "age": age})
    return rows
