"""Ingredient entry schemas & normalisation (0.11.0-a, spec §§2.1, 3.4).

Canonical entry: {name, nutrient_code, amount, unit, per, serving_units,
form, mapping}. Legacy keys accepted on input, normalised via
normalise_ingredient(). nutrient_code must be a catalogue code or None.
"""
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Any

NUTRIENT_CODES: tuple[str, ...] = (
    "vitamin_a","vitamin_c","vitamin_d","vitamin_e","vitamin_k",
    "thiamin","riboflavin","niacin","pantothenic","vitamin_b6","biotin",
    "folate","vitamin_b12","choline",
    "calcium","phosphorus","magnesium","potassium","sodium",
    "iron","zinc","copper","manganese","selenium","iodine","chromium","molybdenum",
    "epa_dha","ala",
)
# RDA keys used by legacy nutrition_rda (for composition ref fallback)
LEGACY_RDA_ALIASES = {"thiamin":"vitamin_b1","riboflavin":"vitamin_b2","niacin":"vitamin_b3","epa_dha":"omega3_epa_dha"}
# Reverse: pre-0.11 rows may carry the legacy RDA key as nutrient_code.
LEGACY_CODE_MAP = {v: k for k, v in LEGACY_RDA_ALIASES.items()}

class IngredientEntry(BaseModel):
    name: str = Field(min_length=1)
    nutrient_code: str | None = None
    amount: float | None = None
    unit: str | None = None
    per: str = "unit"
    serving_units: float = 1
    form: str | None = None
    mapping: str = "confirmed"
    # % of the label's daily value (printed on the supplement facts panel).
    # Distinct from the computed %REF vs NASEM RDA shown in composition.
    pct_dv: float | None = None

    def model_post_init(self, __context: Any) -> None:  # noqa
        if self.pct_dv is not None and self.pct_dv < 0:
            raise ValueError("pct_dv must be >= 0")
        if self.nutrient_code is not None and self.nutrient_code not in NUTRIENT_CODES:
            raise ValueError(f"unknown nutrient_code '{self.nutrient_code}'; choose from list_nutrient_codes")
        if self.per not in ("unit","serving"):
            raise ValueError("per must be 'unit' or 'serving'")
        if self.mapping not in ("confirmed","suggested","none"):
            raise ValueError("mapping must be confirmed|suggested|none")
        if self.form not in (None,"elemental","compound"):
            raise ValueError("form must be elemental|compound|null")

def _num(v):
    return float(v) if isinstance(v,(int,float)) and not isinstance(v,bool) else None

def _unit_of(k: str):
    kl=k.lower()
    for suf,u in (("_mg","mg"),("_mcg","mcg"),("_ug","mcg"),("_µg","mcg"),("_g","g"),("_iu","iu")):
        if kl.endswith(suf): return u
    return None

# Legacy keyword suggester with exclusions (§4.4)
_EXCLUSIONS = ("pantothen","stearate","omega-6","omega-9","fish oil","total omega")
_KEYWORDS: list[tuple[str,str]] = [
    ("epa","epa_dha"),("dha","epa_dha"),("omega-3","epa_dha"),
    ("magnes","magnesium"),("calcium","calcium"),("zinc","zinc"),("iron","iron"),("ferrous","iron"),
    ("selenium","selenium"),("potassium","potassium"),("iodine","iodine"),("iodide","iodine"),
    ("vitamin a","vitamin_a"),("retinol","vitamin_a"),("beta-carotene","vitamin_a"),
    ("vitamin c","vitamin_c"),("ascorb","vitamin_c"),
    ("vitamin d","vitamin_d"),("cholecalciferol","vitamin_d"),("ergocalciferol","vitamin_d"),
    ("vitamin e","vitamin_e"),("tocopher","vitamin_e"),
    ("vitamin k","vitamin_k"),("menaquinone","vitamin_k"),("phylloquinone","vitamin_k"),
    ("vitamin b12","vitamin_b12"),("cobalamin","vitamin_b12"),
    ("thiamin","thiamin"),("vitamin b1","thiamin"),
    ("riboflavin","riboflavin"),("vitamin b2","riboflavin"),
    ("niacin","niacin"),("vitamin b3","niacin"),
    ("pyridoxine","vitamin_b6"),("vitamin b6","vitamin_b6"),
    ("folate","folate"),("folic","folate"),("vitamin b9","folate"),
    ("pantothenic","pantothenic"),("biotin","biotin"),("choline","choline"),
    ("phosphorus","phosphorus"),("sodium","sodium"),("copper","copper"),("manganese","manganese"),
    ("chromium","chromium"),("molybdenum","molybdenum"),("ala","ala"),("alpha-linolenic","ala"),
]

def suggest_nutrient_code(name: str) -> str | None:
    n=(name or "").lower()
    if any(x in n for x in _EXCLUSIONS): return None
    return next((k for kw,k in _KEYWORDS if kw in n), None)

def normalise_ingredient(entry: dict) -> IngredientEntry:
    if isinstance(entry, IngredientEntry): return entry
    if not isinstance(entry, dict): raise ValueError("ingredient entry must be an object")
    name = str(entry.get("name") or entry.get("component") or entry.get("ingredient") or "").strip()
    if not name: raise ValueError("ingredient entry missing name")
    amt = _num(entry.get("amount"))
    unit = entry.get("unit")
    if amt is None:
        for k,v in entry.items():
            if isinstance(v,(int,float)) and not isinstance(v,bool) and "per_1" in k.lower() and _unit_of(k):
                amt=float(v); unit=_unit_of(k); break
    if amt is None:
        for k,v in entry.items():
            if isinstance(v,(int,float)) and not isinstance(v,bool) and "per_2" in k.lower() and _unit_of(k):
                amt=float(v)/2.0; unit=_unit_of(k); break
    if amt is None:
        for k,v in entry.items():
            if isinstance(v,(int,float)) and not isinstance(v,bool) and _unit_of(k):
                amt=float(v); unit=_unit_of(k); break
    code = entry.get("nutrient_code")
    if code is not None:
        code = LEGACY_CODE_MAP.get(code, code)
    if code is None and entry.get("mapping") in ("suggested",None) and "nutrient_code" not in entry:
        # Only suggest for legacy entries lacking the canonical key
        code = suggest_nutrient_code(name)
        mapping = entry.get("mapping") or ("suggested" if code else "none")
    else:
        mapping = entry.get("mapping") or ("confirmed" if code else "none")
    if mapping=="none": code=None
    per = entry.get("per") or "unit"
    # legacy per_2 implies serving? keep explicit; default unit
    return IngredientEntry(name=name, nutrient_code=code, amount=amt, unit=unit, per=per,
        serving_units=float(entry.get("serving_units") or 1), form=entry.get("form"), mapping=mapping,
        pct_dv=_num(entry.get("pct_dv")))

def normalise_ingredients(raw: list | None) -> list[dict]:
    if not raw: return []
    out=[]
    for e in raw:
        out.append(normalise_ingredient(e).model_dump())
    return out

def validate_ingredients(raw: list | None) -> list[dict]:
    return normalise_ingredients(raw)

def per_unit_amount(entry: dict) -> float | None:
    n=normalise_ingredient(entry)
    if n.amount is None: return None
    return n.amount/(n.serving_units if n.per=="serving" else 1)

def short_name(name: str) -> str:
    if len(name)<=12: return name
    cut=name[:12]
    if " " in cut: cut=cut.rsplit(" ",1)[0]
    return cut.strip() or name[:12]
