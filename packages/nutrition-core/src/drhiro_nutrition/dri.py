"""NASEM Dietary Reference Intakes, adults (0.12.0 Part B, spec §4.2).

RDA where one exists, otherwise AI. EPA+DHA: EFSA 250 mg AI.
Bands: 19-30, 31-50, 51-70, 71+. Source per row: NIH ODS fact sheets.
Changing a value means bumping DRI_VERSION.
"""
from __future__ import annotations
from datetime import date

DRI_VERSION = "nasem-2024.1"

# code: {"m": base, "f": base, (sex, band): override}
DRI = {
  "vitamin_a":   {"m": 900,  "f": 700},
  "vitamin_c":   {"m": 90,   "f": 75},
  "vitamin_d":   {"m": 15,   "f": 15,  ("m","71+"): 20, ("f","71+"): 20},
  "vitamin_e":   {"m": 15,   "f": 15},
  "vitamin_k":   {"m": 120,  "f": 90},
  "thiamin":     {"m": 1.2,  "f": 1.1},
  "riboflavin":  {"m": 1.3,  "f": 1.1},
  "niacin":      {"m": 16,   "f": 14},
  "pantothenic": {"m": 5,    "f": 5},
  "vitamin_b6":  {"m": 1.3,  "f": 1.3, ("m","51-70"): 1.7, ("m","71+"): 1.7, ("f","51-70"): 1.5, ("f","71+"): 1.5},
  "biotin":      {"m": 30,   "f": 30},
  "folate":      {"m": 400,  "f": 400},
  "vitamin_b12": {"m": 2.4,  "f": 2.4},
  "choline":     {"m": 550,  "f": 425},
  "calcium":     {"m": 1000, "f": 1000, ("m","71+"): 1200, ("f","51-70"): 1200, ("f","71+"): 1200},
  "phosphorus":  {"m": 700,  "f": 700},
  "magnesium":   {"m": 420,  "f": 320, ("m","19-30"): 400, ("f","19-30"): 310},
  "potassium":   {"m": 3400, "f": 2600},
  "sodium":      {"m": 1500, "f": 1500},
  "iron":        {"m": 8,    "f": 18, ("f","51-70"): 8, ("f","71+"): 8},
  "zinc":        {"m": 11,   "f": 8},
  "copper":      {"m": 900,  "f": 900},
  "manganese":   {"m": 2.3,  "f": 1.8},
  "selenium":    {"m": 55,   "f": 55},
  "iodine":      {"m": 150,  "f": 150},
  "chromium":    {"m": 35,   "f": 25, ("m","51-70"): 30, ("m","71+"): 30, ("f","51-70"): 20, ("f","71+"): 20},
  "molybdenum":  {"m": 45,   "f": 45},
  "epa_dha":     {"m": 250,  "f": 250},
  "ala":         {"m": 1.6,  "f": 1.1},
}
REF_KIND = {code: "AI" for code in ("vitamin_k","pantothenic","biotin","choline","potassium","sodium","manganese","chromium","ala")} | {"epa_dha": "EFSA AI"}

# code -> (value, scope). None = not established.
UL = {
  "vitamin_a":  (3000, "preformed retinol only"), "vitamin_c": (2000, "total"), "vitamin_d": (100, "total"),
  "vitamin_e":  (1000, "supplemental alpha-tocopherol"), "niacin": (35, "supplemental/fortified"),
  "vitamin_b6": (100, "total"), "folate": (1000, "supplemental folic acid"), "choline": (3500, "total"),
  "calcium":    (2500, "total; 2000 at 51+"), "phosphorus": (4000, "total; 3000 at 71+"),
  "magnesium":  (350, "supplemental only"), "iron": (45, "total"), "zinc": (40, "total"),
  "copper":     (10000, "total"), "manganese": (11, "total"), "selenium": (400, "total"),
  "iodine":     (1100, "total"), "molybdenum": (2000, "total"), "sodium": (2300, "CDRR – reduce above"),
}
# Age-specific UL adjustments for scoped entries.
_UL_AGE = {"calcium": {"51+": 2000}, "phosphorus": {"71+": 3000}}

def age_band(age: int | None) -> str:
    if age is None:
        return "31-50"
    if age < 31:
        return "19-30"
    if age <= 50:
        return "31-50"
    if age <= 70:
        return "51-70"
    return "71+"

def age_from_dob(dob, today: date | None = None) -> int | None:
    if not dob:
        return None
    try:
        born = date.fromisoformat(str(dob)[:10])
    except ValueError:
        return None
    ref = today or date.today()
    return max(0, ref.year - born.year - ((ref.month, ref.day) < (born.month, born.day)))

def reference_for(code, sex: str | None, age: int | None) -> dict:
    s = (sex or "").strip().lower()
    sex_used = {"male": "m", "m": "m", "female": "f", "f": "f"}.get(s, "m")
    assumed = s not in ("male", "m", "female", "f") or age is None
    band = age_band(age)
    table = DRI.get(code, {})
    value = table.get((sex_used, band), table.get(sex_used))
    ul, ul_scope = (None, None)
    if code in UL:
        ul, ul_scope = UL[code]
        adj = _UL_AGE.get(code, {})
        if "51+" in adj and age is not None and age >= 51:
            ul = adj["51+"]
        if "71+" in adj and age is not None and age >= 71:
            ul = adj["71+"]
    kind = REF_KIND.get(code, "RDA")
    return {"value": value, "kind": kind, "ul": ul, "ul_scope": ul_scope, "band": band,
            "sex_used": sex_used, "assumed": assumed, "version": DRI_VERSION}
