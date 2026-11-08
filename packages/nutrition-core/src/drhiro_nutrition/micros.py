"""Nutrient catalogue (0.12.0 Part B, spec §4.1). Single source for API, web,
agent and importers. All values in canonical unit (mg | mcg | g).

FDC ids/units verified against recorded fixtures in
packages/nutrition-core/tests/fixtures/ (see test_micros_catalogue.py).
"""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class NutrientDef:
    code: str; label_en: str; label_hr: str; unit: str
    group: str
    fdc_ids: tuple[int, ...]
    fdc_unit: str
    off_key: str | None

NUTRIENT_CATALOGUE: tuple[NutrientDef, ...] = (
    NutrientDef("vitamin_a",   "Vitamin A (RAE)", "Vitamin A (RAE)", "mcg", "vitamin",  (1106,), "mcg", "vitamin-a"),
    NutrientDef("vitamin_c",   "Vitamin C", "Vitamin C",            "mg",  "vitamin",  (1162,), "mg",  "vitamin-c"),
    NutrientDef("vitamin_d",   "Vitamin D", "Vitamin D",            "mcg", "vitamin",  (1114,), "mcg", "vitamin-d"),
    NutrientDef("vitamin_e",   "Vitamin E", "Vitamin E",            "mg",  "vitamin",  (1109,), "mg",  "vitamin-e"),
    NutrientDef("vitamin_k",   "Vitamin K", "Vitamin K",            "mcg", "vitamin",  (1185,), "mcg", "vitamin-k"),
    NutrientDef("thiamin",     "B1 Thiamin", "B1 Tiamin",           "mg",  "vitamin",  (1165,), "mg",  "vitamin-b1"),
    NutrientDef("riboflavin",  "B2 Riboflavin", "B2 Riboflavin",    "mg",  "vitamin",  (1166,), "mg",  "vitamin-b2"),
    NutrientDef("niacin",      "B3 Niacin", "B3 Niacin",            "mg",  "vitamin",  (1167,), "mg",  "vitamin-pp"),
    NutrientDef("pantothenic", "B5 Pantothenic acid", "B5 Pantotenska kis.", "mg", "vitamin", (1170,), "mg", "pantothenic-acid"),
    NutrientDef("vitamin_b6",  "B6", "B6",                          "mg",  "vitamin",  (1175,), "mg",  "vitamin-b6"),
    NutrientDef("biotin",      "B7 Biotin", "B7 Biotin",            "mcg", "vitamin",  (1176,), "mcg", "biotin"),
    NutrientDef("folate",      "B9 Folate (DFE)", "B9 Folat (DFE)", "mcg", "vitamin",  (1190, 1177), "mcg", "vitamin-b9"),
    NutrientDef("vitamin_b12", "B12", "B12",                        "mcg", "vitamin",  (1178,), "mcg", "vitamin-b12"),
    NutrientDef("choline",     "Choline", "Kolin",                  "mg",  "vitamin",  (1180,), "mg",  "choline"),
    NutrientDef("calcium",     "Calcium", "Kalcij",                 "mg",  "mineral",  (1087,), "mg",  "calcium"),
    NutrientDef("phosphorus",  "Phosphorus", "Fosfor",              "mg",  "mineral",  (1091,), "mg",  "phosphorus"),
    NutrientDef("magnesium",   "Magnesium", "Magnezij",             "mg",  "mineral",  (1090,), "mg",  "magnesium"),
    NutrientDef("potassium",   "Potassium", "Kalij",                "mg",  "mineral",  (1092,), "mg",  "potassium"),
    NutrientDef("sodium",      "Sodium", "Natrij",                  "mg",  "mineral",  (1093,), "mg",  "sodium"),
    NutrientDef("iron",        "Iron", "Željezo",                   "mg",  "trace",    (1089,), "mg",  "iron"),
    NutrientDef("zinc",        "Zinc", "Cink",                      "mg",  "trace",    (1095,), "mg",  "zinc"),
    NutrientDef("copper",      "Copper", "Bakar",                   "mcg", "trace",    (1098,), "mg",  "copper"),
    NutrientDef("manganese",   "Manganese", "Mangan",               "mg",  "trace",    (1101,), "mg",  "manganese"),
    NutrientDef("selenium",    "Selenium", "Selen",                 "mcg", "trace",    (1103,), "mcg", "selenium"),
    NutrientDef("iodine",      "Iodine", "Jod",                     "mcg", "trace",    (1100,), "mcg", "iodine"),
    NutrientDef("chromium",    "Chromium", "Krom",                  "mcg", "trace",    (1096,), "mcg", "chromium"),
    NutrientDef("molybdenum",  "Molybdenum", "Molibden",            "mcg", "trace",    (1102,), "mcg", "molybdenum"),
    NutrientDef("epa_dha",     "Omega-3 EPA+DHA", "Omega-3 EPA+DHA", "mg", "fatty_acid", (1278, 1272), "g", None),
    NutrientDef("ala",         "Omega-3 ALA", "Omega-3 ALA",        "g",   "fatty_acid", (1404, 1270), "g", "alpha-linolenic-acid"),
)
SUM_IDS = {"epa_dha"}
_BY_CODE = {c.code: c for c in NUTRIENT_CATALOGUE}
def by_code(code: str) -> NutrientDef | None:
    return _BY_CODE.get(code)

class UnmappedUnit(ValueError):
    """Unknown/unsupported unit for a nutrient — never assume canonical."""

_UNIT_TO_MG = {"g": 1000.0, "mg": 1.0, "mcg": 0.001, "ug": 0.001, "µg": 0.001, "μg": 0.001}

def _mass_to_unit(value_mg: float, target_unit: str) -> float:
    return value_mg if target_unit == "mg" else value_mg * 1000.0 if target_unit == "mcg" else value_mg / 1000.0

def convert_value(value: float, from_unit: str, code: str) -> float:
    """Convert a mass value in from_unit to the nutrient's canonical unit."""
    n = by_code(code)
    if n is None:
        raise ValueError(f"unknown nutrient code {code!r}")
    fu = (from_unit or "").strip().lower()
    if fu not in _UNIT_TO_MG:
        raise UnmappedUnit(f"unsupported unit {from_unit!r} for {code}")
    return _mass_to_unit(float(value) * _UNIT_TO_MG[fu], n.unit)

def to_canonical(value: float, unit: str | None, code: str, name: str = "", form: str | None = None) -> tuple[float, bool]:
    """Supplement label amount -> canonical. Returns (value, assumed).

    Strict: unknown units raise UnmappedUnit (caller lists in unknowns).
    IU / folate rules per spec §4.4.
    """
    n = by_code(code)
    if n is None:
        raise UnmappedUnit(f"unknown nutrient code {code!r}")
    u = (unit or "").strip().lower()
    nm = (name or "").lower()
    if u == "iu":
        if code == "vitamin_d":
            return float(value) * 0.025, False
        if code == "vitamin_a":
            if "beta-carotene" in nm or "beta carotene" in nm:
                return float(value) * 0.15, False
            return float(value) * 0.3, False
        if code == "vitamin_e":
            if "dl-" in nm or "dl " in nm:
                return float(value) * 0.45, False
            if "d-alpha" in nm or "d alpha" in nm or "natural" in nm:
                return float(value) * 0.67, False
            return float(value) * 0.45, True
        raise UnmappedUnit(f"IU not defined for {code}")
    if u in _UNIT_TO_MG:
        val = float(value) * _UNIT_TO_MG[u]
        if code == "folate" and "folic acid" in nm and "dfe" not in nm:
            val *= 1.7
        return _mass_to_unit(val, n.unit), False
    raise UnmappedUnit(f"unsupported unit {unit!r} for {code} ({name})")
