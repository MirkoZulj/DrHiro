"""Consumption domain: unified write path for meals + beverages.

This module is the SINGLE write path for all consumption logging:
- intelligent confirm (text → meal + linked beverage)
- legacy meal create
- direct liquid log
- recipe log
- all relevant CRUD (qty change, beverage replacement, delete)

It enforces:
- idempotency via consumption_operations (Telegram update_id or caller key)
- DB-level uniqueness (user_id, source, source_record_id) on measurements
- stable item identity via consumption_items
- atomic meal + beverage writes in ONE transaction
- persisted operation result for replay
- user_id ownership on every read/write
- all 6 nutrients (kcal/protein/carbs/fat/fiber/sodium) in every rebuild
- meal-group validation (breakfast|lunch|dinner|snack, default snack)
- beverage classification via token-aware matching (not substring)
- decimal-comma handling without splitting a number
- per-item quantity (not first-number-of-message)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from drhiro_api.models import (
    ConsumptionItem,
    ConsumptionOperation,
    BeverageMeasurement,
    Meal,
    MealItem,
    Measurement,
    User,
    Food,
    Nutrient,
)
from drhiro_api.food_search import resolve_food, nutrient_map

log = logging.getLogger("drhiro.consumption")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MEAL_GROUPS = frozenset({"breakfast", "lunch", "dinner", "snack"})
DEFAULT_MEAL_TYPE = "snack"

LIQUID_CATEGORIES = ["water", "non_alcoholic", "beer", "wine", "spirits", "other_alcohol"]

# Token-aware beverage classification (word boundary, most specific first).
# Each pattern uses \b so "tea" does NOT match "steak", "vino" does not match "vitamin".
_LIQUID_KEYWORDS: list[tuple[str, re.Pattern]] = [
    ("spirits", re.compile(
        r"\b(?:whiskey|whisky|viski|vodka|votka|rum|gin|brandy|rakija|šljivovica|sljivovica|"
        r"konjak|cognac|tequila|loza|travarica|brandi|brändy)\b", re.I)),
    ("wine", re.compile(
        r"\b(?:wine|vino|rose|rosé|prosecco|šampanjac|sampanjac|champagne|"
        r"crno|bijelo|bjelo)\b", re.I)),
    ("beer", re.compile(
        r"\b(?:beer|pivo|lager|ale|stout|heineken|ozujsko|karlovačko|karlovacko|"
        r"točeno|toceno|radler)\b", re.I)),
    ("other_alcohol", re.compile(
        r"\b(?:cocktail|koktel|cider|jabolčnik|jabolcnik|liqueur|liker|aperol|martini|"
        r"baileys|amaretto|mojito|negroni|spritz)\b", re.I)),
    ("non_alcoholic", re.compile(
        r"\b(?:coffee|kava|cappuccino|latte|tea|čaj|caj|ice\s*tea|juice|sok|soda|cola|coke|"
        r"coca|fanta|sprite|smoothie|shake|milk|mlijeko|mliko|energy|redbull|monster|"
        r"cedevita|limunada|nectar|espresso|americano|mocha)\b", re.I)),
    ("water", re.compile(r"\b(?:water|voda|mineral)\b", re.I)),
]

# Volume unit conversions to ml
_VOLUME_UNITS = {
    "ml": 1.0, "milliliter": 1.0, "millilitre": 1.0, "milliliters": 1.0, "millilitres": 1.0,
    "l": 1000.0, "liter": 1000.0, "litre": 1000.0, "liters": 1000.0, "litres": 1000.0,
    "cl": 10.0, "centiliter": 10.0, "centilitre": 10.0, "centiliters": 10.0, "centilitres": 10.0,
    "dl": 100.0, "deciliter": 100.0, "decilitre": 100.0, "deciliters": 100.0, "decilitres": 100.0,
    "dcl": 100.0,  # alternate abbreviation for deciliter
    "cup": 240.0, "cups": 240.0,
    "glass": 250.0, "glasses": 250.0,
    "bottle": 500.0, "bottles": 500.0,
    "can": 330.0, "cans": 330.0,
    "espresso": 30.0, "espressos": 30.0, "shot": 30.0, "shots": 30.0,
    "mug": 300.0, "mugs": 300.0,
}

# Container gram defaults (for food items)
CONTAINER_GRAMS = {
    "glass": 250, "cup": 240, "bowl": 300, "mug": 300, "can": 330,
    "bottle": 500, "serving": 150, "scoop": 30, "handful": 40, "slice": 28,
}

ITEM_GRAMS = {
    "carrot": 75, "onion": 110, "egg": 50, "tomato": 120, "apple": 180,
    "potato": 170, "pepper": 120, "banana": 120, "orange": 130, "corn": 90,
    "steak": 250, "wine": 150, "beer": 330,
}

NUTRIENT_KEYS = ("kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "sodium_mg")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ParsedItem:
    """One canonical parsed item from free text."""
    display_name: str
    quantity: float = 1.0
    unit: Optional[str] = None
    grams: Optional[float] = None
    volume_ml: Optional[float] = None
    beverage_category: Optional[str] = None
    is_beverage: bool = False
    meal_type: Optional[str] = None
    # Nutrition per 100g or per 100ml
    nutrients_per_100: dict = field(default_factory=dict)
    # Scaled nutrition
    nutrients_scaled: dict = field(default_factory=dict)
    source: str = "manual"
    confidence: float = 0.8
    # Provenance
    explicit_qty_assumption: Optional[str] = None
    # Stage 2: nutrient resolution provenance
    nutrient_basis: Optional[str] = None  # 'per_100_g' | 'per_100_ml'
    resolution_source: Optional[str] = None  # 'db' | 'external' | 'unmatched'
    food_catalog_item_id: Optional[str] = None
    nutrition_complete: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_float(num: str) -> float:
    """Parse a number that may use comma as decimal separator."""
    return float(num.replace(",", "."))


def _strip_nl_prefix(text: str) -> str:
    """Strip natural-language prefixes like 'I drank', 'Yesterday I drank'.

    Returns the text with the prefix removed, or the original text if no
    prefix matched. Also handles date references like 'Yesterday', 'Today'.
    """
    if not text:
        return text
    t = text.strip()
    # Remove leading date references + optional "I drank/ate/had"
    t = re.sub(
        r'^(?:yesterday|today|last\s+\w+day|on\s+\w+day)\s*,?\s*',
        '', t, flags=re.I,
    )
    # Remove leading "I drank/ate/had" etc.
    t = re.sub(
        r'^(?:i\s+(?:drank|ate|had)|i\s+had\s+and\s+ate)\s+',
        '', t, flags=re.I,
    )
    # Remove leading "I had/ate for breakfast/lunch/dinner"
    t = re.sub(
        r'^(?:i\s+(?:had|ate)\s+(?:for\s+)?(?:breakfast|lunch|dinner|snack))\s+',
        '', t, flags=re.I,
    )
    return t.strip()


def _normalize_meal_type(mt: Optional[str]) -> str:
    """Validate meal group. Defaults to 'snack' if unspecified."""
    if not mt:
        return DEFAULT_MEAL_TYPE
    mt = mt.strip().lower()
    return mt if mt in MEAL_GROUPS else DEFAULT_MEAL_TYPE


def _classify_beverage(text: str) -> Optional[str]:
    """Token-aware beverage classification. Returns None if not a beverage."""
    for cat, rx in _LIQUID_KEYWORDS:
        if rx.search(text):
            return cat
    return None


def _stable_item_key(operation_id: str, index: int) -> str:
    """Deterministic item key within an operation."""
    return f"item-{index}"


def _compute_source_record_id(operation_id: str, item_key: str) -> str:
    """Deterministic source_record_id for a consumption item."""
    raw = f"consumption:{operation_id}:{item_key}"
    return raw[:255]


def _compute_payload_hash(items: list, meal_type: Optional[str] = None) -> str:
    """Compute a stable hash of the payload for identity comparison.

    The hash covers the items only (not meal_type). Rationale:
    - The operation identity (telegram chat/msg/bot or idempotency_key) already
      establishes WHICH event this is.
    - The payload hash detects "same event, DIFFERENT content" conflicts.
    - Meal type is contextual metadata (breakfast/lunch/dinner) that does NOT
      change the drink's identity — the same message can be interpreted as
      "lunch" by one caller and left unspecified by another, but the drinks
      are the same. Including it would cause false conflicts between
      confirm_consumption (which passes meal_type) and log_manual_liquid
      (which does not).

    Canonicalization for beverages: when volume_ml is set, unit is normalized
    to "ml" and quantity to 1, because volume_ml is the source of truth for
    beverage volume. This ensures semantically identical drinks produce the
    same hash regardless of whether the caller set unit="ml"/quantity=1
    explicitly or left them as defaults (e.g. _liquid_to_parsed_items vs a
    hand-built ParsedItem).
    """
    def _canonical_unit(it):
        if it.is_beverage and it.volume_ml is not None:
            return "ml"
        return it.unit

    def _canonical_quantity(it):
        if it.is_beverage and it.volume_ml is not None:
            return 1
        return it.quantity

    payload = {
        "items": [
            {
                "display_name": it.display_name,
                "quantity": _canonical_quantity(it),
                "unit": _canonical_unit(it),
                "grams": it.grams,
                "volume_ml": it.volume_ml,
                "beverage_category": it.beverage_category,
                "is_beverage": it.is_beverage,
            }
            for it in items
        ],
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _scale_nutrients(per100: dict, factor: float) -> dict:
    """Scale per-100 nutrients by factor (grams/100 or ml/100)."""
    out = {}
    for k in NUTRIENT_KEYS:
        v = per100.get(k)
        if v is not None:
            try:
                out[k] = round(float(v) * factor, 2)
            except (TypeError, ValueError):
                out[k] = 0.0
    return out


def _sum_nutrients(nutrient_dicts: list[dict]) -> dict:
    """Sum a list of nutrient dicts across all 6 keys."""
    out = {}
    for k in NUTRIENT_KEYS:
        s = 0.0
        for nd in nutrient_dicts:
            try:
                s += float(nd.get(k) or 0)
            except (TypeError, ValueError):
                pass
        out[k] = round(s, 2)
    return out


# ---------------------------------------------------------------------------
# Unified parser (replaces both service.py parse_meal_text AND the MCP liquid block)
# ---------------------------------------------------------------------------

# Regex: number + unit + "of" + food — handles decimal commas without splitting
_NUM_UNIT_RE = re.compile(
    r'(?P<qty>\d+(?:[.,]\d+)?)\s*'
    r'(?P<unit>g|grams?|kg|ml|milliliters?|millilitres?|l|liters?|litres?|'
    r'cl|centiliters?|centilitres?|dl|deciliters?|decilitres?|dcl|'
    r'tbsp|tsp|tablespoons?|teaspoons?|slices?|pieces?|cups?|glass(?:es)?|bottles?|cans?|mugs?|shots?|espressos?)?'
    r'\s+(?:of\s+)?(?P<food>.+)',
    re.IGNORECASE,
)

# Word-number + container + of + food
_CONT_RE = re.compile(
    r'(?P<num>a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s+'
    r'(?:(?P<adj>large|small|big|medium|fresh|whole|plain)\s+)?'
    r'(?P<cont>glass|cup|bowl|mug|can|bottle|serving|scoop|handful|slice|piece)s?\s+'
    r'(?:of\s+)?(?P<food>.+)',
    re.IGNORECASE,
)

# Word-number + food
_WORD_NUM_RE = re.compile(
    r'(?P<num>a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s+'
    r'(?:(?P<adj>large|small|big|medium|fresh|whole|plain)\s+)?'
    r'(?P<food>.+)',
    re.IGNORECASE,
)

WORD_NUM_MAP = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# Separators: comma, "and", "with", "plus" — but NOT inside a number like "0,5"
# Split on comma only when NOT preceded by a digit (negative lookbehind).
# We do NOT require the following char to be non-digit, so "milk,330ml" splits.
_ITEM_SPLIT_RE = re.compile(r'(?<!\d)\s*[,،]\s*|\band\b|\bwith\b|\bplus\b|\+|;', re.I)


def parse_consumption_text(text: str) -> list[ParsedItem]:
    """Parse free-text meal description into canonical ParsedItems.

    Handles:
    - "250ml milk" → one beverage item, 250ml
    - "0,5 l beer" → one beverage item, 500ml (decimal comma NOT split)
    - "1 cup coffee" and "a cup of coffee" → consistent 240ml
    - "200g steak and 250ml water" → two items, water correctly associated
    - "tea" inside "steak" → NOT classified as beverage (word boundary)
    """
    if not text or not text.strip():
        return []

    items: list[ParsedItem] = []
    parts = _ITEM_SPLIT_RE.split(text)

    for part in parts:
        part = part.strip()
        if not part:
            continue
        item = _parse_single_item(part)
        if item:
            items.append(item)

    return items


def _parse_single_item(part: str) -> Optional[ParsedItem]:
    """Parse a single item fragment into a ParsedItem."""

    # Strip NL prefixes (e.g. "I drank", "Yesterday I drank")
    part = _strip_nl_prefix(part)
    if not part:
        return None

    # Try numeric: "500 g of steak", "250ml milk", "0,5 l beer"
    m = _NUM_UNIT_RE.match(part)
    if m:
        qty = _to_float(m.group("qty"))
        unit = (m.group("unit") or "").lower().strip()
        food = m.group("food").strip()
        return _build_item(qty, unit, food)

    # Try container: "a glass of wine", "a cup of coffee"
    m = _CONT_RE.match(part)
    if m:
        cnt = WORD_NUM_MAP.get(m.group("num").lower(), 1)
        cont = m.group("cont").lower()
        food = m.group("food").strip()
        grams = cnt * CONTAINER_GRAMS.get(cont, 100)
        # Check if the container implies a liquid
        bev_cat = _classify_beverage(food)
        if bev_cat:
            vol = cnt * _VOLUME_UNITS.get(cont, 250.0)
            return ParsedItem(
                display_name=food, quantity=cnt, unit=cont,
                volume_ml=vol, beverage_category=bev_cat, is_beverage=True,
                grams=grams,
            )
        return ParsedItem(display_name=food, quantity=cnt, unit=cont, grams=grams)

    # Try word-number: "two eggs", "one large pepper"
    m = _WORD_NUM_RE.match(part)
    if m and m.group("num").lower() in WORD_NUM_MAP:
        cnt = WORD_NUM_MAP[m.group("num").lower()]
        food = m.group("food").strip()
        grams = cnt * ITEM_GRAMS.get(food.lower().rstrip("s"), 100)
        # Check if the food is a beverage
        bev_cat = _classify_beverage(food)
        if bev_cat:
            vol = cnt * 250  # default beverage volume
            return ParsedItem(
                display_name=food, quantity=cnt, grams=grams,
                volume_ml=vol, beverage_category=bev_cat, is_beverage=True,
            )
        return ParsedItem(display_name=food, quantity=cnt, grams=grams)

    # Bare food word
    if part.strip():
        food = part.strip()
        grams = ITEM_GRAMS.get(food.lower().rstrip("s"), 100)
        # Check if the bare word is a beverage
        bev_cat = _classify_beverage(food)
        if bev_cat:
            return ParsedItem(
                display_name=food, quantity=1, grams=grams,
                volume_ml=250, beverage_category=bev_cat, is_beverage=True,
            )
        return ParsedItem(display_name=food, quantity=1, grams=grams)

    return None


def _build_item(qty: float, unit: str, food: str) -> ParsedItem:
    """Build a ParsedItem from qty+unit+food."""
    gram_units = {"g", "gram", "grams", "kg"}
    volume_units = {"ml", "milliliter", "millilitre", "milliliters", "millilitres",
                    "l", "liter", "litres", "liters",
                    "cl", "centiliter", "centilitre", "centiliters", "centilitres",
                    "dl", "deciliter", "decilitre", "deciliters", "decilitres", "dcl",
                    "cup", "cups", "glass", "glasses", "bottle", "bottles",
                    "can", "cans", "espresso", "espressos", "shot", "shots",
                    "mug", "mugs"}
    count_units = {"slice", "slices", "piece", "pieces", "tbsp", "tsp",
                   "tablespoon", "tablespoons", "teaspoon", "teaspoons"}

    # Determine if this is a liquid by food name
    bev_cat = _classify_beverage(food)

    if unit in gram_units:
        if unit == "kg":
            grams = qty * 1000
        else:
            grams = qty
        # Mass unit: preserve beverage identity (e.g. '250 g milk' is still a beverage)
        if bev_cat:
            return ParsedItem(
                display_name=food, quantity=qty, unit=unit, grams=grams,
                beverage_category=bev_cat, is_beverage=True,
                volume_ml=grams,  # 1g ≈ 1ml for beverages
            )
        return ParsedItem(display_name=food, quantity=qty, unit=unit, grams=grams)
    elif unit in volume_units:
        ml = qty * _VOLUME_UNITS.get(unit, 1.0)
        # Volume unit + beverage food name → beverage
        # Volume unit + non-beverage food name → food measured by volume (e.g. "1 cup rice")
        if bev_cat:
            return ParsedItem(
                display_name=food, quantity=qty, unit=unit,
                volume_ml=ml, beverage_category=bev_cat, is_beverage=True,
                grams=ml,  # 1ml ≈ 1g for water-like
            )
        else:
            # Non-beverage measured by volume (e.g. "1 cup rice")
            return ParsedItem(
                display_name=food, quantity=qty, unit=unit,
                grams=ml,  # approximate using 1ml ≈ 1g
            )
    elif unit in count_units:
        gram_map = {
            "slice": 28, "slices": 28, "piece": 5, "pieces": 5,
            "tbsp": 15, "tablespoon": 15, "tablespoons": 15,
            "tsp": 5, "teaspoon": 5, "teaspoons": 5,
        }
        grams = qty * gram_map.get(unit, 10)
        return ParsedItem(display_name=food, quantity=qty, unit=unit, grams=grams)
    else:
        # Unitless count: "2 boiled eggs", "1 glass wine"
        # Check for container word in the food
        container_match = re.match(
            r"^(large|small|big|medium|fresh|whole|plain|glass|cup|bottle)\s+(.+)$",
            food, re.I,
        )
        if container_match:
            food = container_match.group(2).strip()

        # Check if it's a countable item
        food_key = food.lower().rstrip("s")
        grams = ITEM_GRAMS.get(food_key, 0)
        if grams:
            grams = qty * grams
        elif bev_cat:
            # Default beverage volume
            grams = qty * 250
        else:
            grams = qty * 100  # fallback

        return ParsedItem(display_name=food, quantity=qty, grams=grams,
                          volume_ml=grams if bev_cat else None,
                          beverage_category=bev_cat, is_beverage=bool(bev_cat))


# ---------------------------------------------------------------------------
# Stage 2 B1 — Nutrient resolution
# ---------------------------------------------------------------------------

def _external_nutrition_search(food_name: str, limit: int = 3) -> list[dict]:
    """Search for food nutrition via external providers (USDA API, DDG).

    This is the BOUNDARY for external lookups. Callers can mock this function
    to inject fake responses for testing without pre-enriching items.

    Returns a list of candidate dicts with keys:
        display_name, kcal_per_100g, protein_g_per_100g, carbs_g_per_100g,
        fat_g_per_100g, fiber_g_per_100g, sodium_mg_per_100g, source,
        confidence, food_id
    """
    # Try USDA API first (structured, authoritative)
    try:
        candidates = _usda_search(food_name, limit=limit)
        if candidates:
            return candidates
    except Exception:
        pass
    # Fall back to DDG via the VPS host service
    try:
        return _ddg_nutrition_search(food_name)
    except Exception:
        return []


def _usda_search(query: str, limit: int = 3) -> list[dict]:
    """Query USDA FoodData Central for structured per-100g nutrition.

    Restricted to Foundation/SR Legacy/Survey data types to avoid branded
    products (which often have misleading descriptions).
    """
    import os
    import time
    import httpx

    key = query.strip().lower()
    now = time.time()
    if key in _USDA_CACHE and now - _USDA_CACHE[key][0] < 900:
        return _USDA_CACHE[key][1]

    try:
        api_key = os.environ.get("USDA_API_KEY", "")
        if not api_key:
            return []
        r = httpx.get(
            "https://api.nal.usda.gov/fdc/v1/foods/search",
            params={
                "query": query,
                "api_key": api_key,
                "pageSize": limit,
                "dataType": ["Foundation", "SR Legacy", "Survey (FNDDS)"],
            },
            timeout=30,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        foods = data.get("foods", [])
        out = []
        for f in foods:
            desc = f.get("description", "")
            if not desc:
                continue
            nuts = {n.get("nutrientName"): n.get("value") for n in f.get("foodNutrients", [])}
            out.append({
                "display_name": desc.title(),
                "kcal_per_100g": _norm_usda_val(nuts.get("Energy")),
                "protein_g_per_100g": _norm_usda_val(nuts.get("Protein")),
                "carbs_g_per_100g": _norm_usda_val(nuts.get("Carbohydrate, by difference")),
                "fat_g_per_100g": _norm_usda_val(nuts.get("Total lipid (fat)")),
                "fiber_g_per_100g": _norm_usda_val(nuts.get("Fiber, total dietary")),
                "sodium_mg_per_100g": _norm_usda_val(nuts.get("Sodium, Na")),
                "source": "usda",
                "confidence": 0.85,
            })
        _USDA_CACHE[key] = (now, out)
        return out
    except Exception:
        return []


_USDA_CACHE: dict = {}


def _norm_usda_val(value):
    """Coerce a possibly-str value to float or None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ddg_nutrition_search(query: str) -> list[dict]:
    """DuckDuckGo nutrition fallback via the VPS-host ddg-http service.

    Env: DDG_HTTP_URL. The host service runs Camoufox egressing through a
    residential tunnel. Returns [] if unreachable.
    """
    import os
    import httpx

    url = os.environ.get("DDG_HTTP_URL", "")
    if not url:
        return []
    try:
        r = httpx.post(f"{url}/lookup", json={"query": query}, timeout=75)
        if r.status_code != 200:
            return []
        data = r.json()
        cands = data.get("candidates", [])
        for c in cands:
            c["display_name"] = query.strip().title()
            c.setdefault("source", "duckduckgo")
        return cands
    except Exception:
        return []


def resolve_item_nutrition(db: Session, item: ParsedItem) -> ParsedItem:
    """Resolve nutrition for a ParsedItem through DB → external fallback.

    This is the REAL food resolution path. It:
    1. Searches the local DB via resolve_food (tiered ranking).
    2. If no DB match, calls _external_nutrition_search (mockable boundary).
    3. Scales per-100 nutrients by the item's grams/100 or ml/100.
    4. Sets nutrient_basis (per_100_g vs per_100_ml) based on item type.
    5. Sets nutrition_complete=False if no resolution was found.

    UNKNOWN ≠ KNOWN-ZERO: a lookup failure sets nutrition_complete=False
    and leaves nutrients as empty/zero, so downstream code can distinguish
    "we know this has 0 kcal" from "we couldn't find it".
    """
    if item.nutrients_per_100:
        # Already resolved (e.g. from a saved operation)
        return item

    food_name = item.display_name
    if not food_name:
        item.nutrition_complete = False
        return item

    # 1. Try local DB
    code_by_id = {n.id: n.nutrient_code for n in db.query(Nutrient).all()}
    result = resolve_food(db, food_name, limit=5)
    food = result.best if result else None

    if food is not None:
        # Found in DB
        nmap = nutrient_map(food, code_by_id)
        item.nutrients_per_100 = {
            "kcal": nmap.get("energy"),
            "protein_g": nmap.get("protein"),
            "carbs_g": nmap.get("carbs"),
            "fat_g": nmap.get("fat"),
            "fiber_g": nmap.get("fiber"),
            "sodium_mg": nmap.get("sodium"),
        }
        # Fill missing with None → 0 when scaling
        for k in NUTRIENT_KEYS:
            item.nutrients_per_100.setdefault(k, None)
        item.food_catalog_item_id = str(food.id)
        item.resolution_source = "db"
        item.confidence = 0.9
    else:
        # 2. No DB match — try external
        external = _external_nutrition_search(food_name)
        if external:
            best = external[0]
            item.nutrients_per_100 = {
                "kcal": best.get("kcal_per_100g"),
                "protein_g": best.get("protein_g_per_100g"),
                "carbs_g": best.get("carbs_g_per_100g"),
                "fat_g": best.get("fat_g_per_100g"),
                "fiber_g": best.get("fiber_g_per_100g"),
                "sodium_mg": best.get("sodium_mg_per_100g"),
            }
            item.food_catalog_item_id = best.get("food_id")
            item.resolution_source = best.get("source", "external")
            item.confidence = best.get("confidence", 0.7)
        else:
            # 3. Nothing found — mark incomplete
            item.nutrients_per_100 = {k: None for k in NUTRIENT_KEYS}
            item.resolution_source = "unmatched"
            item.confidence = 0.3
            item.nutrition_complete = False
            # Still scale to 0 for storage (distinguished by nutrition_complete=False)
            item.nutrients_scaled = {k: 0.0 for k in NUTRIENT_KEYS}
            return item

    # Determine nutrient basis
    # Nutrient basis follows the ACTUAL source data, not the item type.
    # If the item was measured by mass (grams set, no volume), use per_100_g.
    # If the item was measured by volume (volume_ml set, no grams), use per_100_ml.
    # If both are present (e.g. "250 g milk"), the mass is the primary measurement
    # and volume is derived — use per_100_g.
    if item.grams is not None and item.volume_ml is not None:
        # Both present: mass is primary, volume derived via documented density
        item.nutrient_basis = "per_100_g"
        factor = item.grams / 100.0
    elif item.grams is not None:
        item.nutrient_basis = "per_100_g"
        factor = item.grams / 100.0
    elif item.volume_ml is not None:
        item.nutrient_basis = "per_100_ml"
        factor = item.volume_ml / 100.0
    else:
        item.nutrient_basis = "per_100_g" if not item.is_beverage else "per_100_ml"
        factor = 1.0

    # Scale nutrients (None → 0)
    item.nutrients_scaled = _scale_nutrients(
        {k: (v if v is not None else 0.0) for k, v in item.nutrients_per_100.items()},
        factor,
    )
    return item


# ---------------------------------------------------------------------------
# Unified write path
# ---------------------------------------------------------------------------

def get_or_create_operation(
    db: Session,
    user_id: str,
    source: str = "telegram",
    source_chat_id: Optional[str] = None,
    source_message_id: Optional[str] = None,
    source_bot_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    raw_text: Optional[str] = None,
    payload_hash: Optional[str] = None,
) -> tuple[ConsumptionOperation, bool]:
    """Get existing operation or create a new one. Returns (operation, created).

    B3: Trusted identity enforcement:
    - If idempotency_key is provided, it takes precedence (works for any source).
    - Telegram source without idempotency_key REQUIRES (source_chat_id, source_message_id, source_bot_id).
      Missing identity → fail closed with an explicit error.
    - If an existing operation is found but its payload_hash differs from the new
      payload_hash, reject (conflicting payload reuse).
    """
    # --- Fail-closed identity enforcement ---
    # If idempotency_key is provided, use it (works for any source)
    if idempotency_key:
        op = db.query(ConsumptionOperation).filter(
            ConsumptionOperation.user_id == user_id,
            ConsumptionOperation.idempotency_key == idempotency_key,
        ).first()
        if op:
            if payload_hash and op.payload_hash and op.payload_hash != payload_hash:
                raise ValueError(
                    "conflicting_payload_reuse: same idempotency_key but different payload."
                )
            return op, False
        # No existing op found → create new one with this idempotency_key
        # (fall through to creation below)

    # Telegram source requires full identity
    elif source == "telegram":
        if not (source_chat_id and source_message_id and source_bot_id):
            raise ValueError(
                "telegram_source_requires_identity: "
                "source_chat_id, source_message_id, and source_bot_id are required "
                "for telegram source to enable retry protection."
            )
        op = db.query(ConsumptionOperation).filter(
            ConsumptionOperation.user_id == user_id,
            ConsumptionOperation.source_bot_id == source_bot_id,
            ConsumptionOperation.source_chat_id == source_chat_id,
            ConsumptionOperation.source_message_id == source_message_id,
        ).first()
        if op:
            if payload_hash and op.payload_hash and op.payload_hash != payload_hash:
                raise ValueError(
                    "conflicting_payload_reuse: same Telegram identity but different payload."
                )
            return op, False

    else:
        # Non-Telegram source without idempotency_key → fail closed
        raise ValueError(
            "missing_identity: either telegram source identity or idempotency_key is required."
        )

    # --- Atomic first creation (B5) ---
    # Use INSERT ... ON CONFLICT DO NOTHING to handle concurrent first creation
    # safely. If two callers try to create the same operation simultaneously,
    # only one INSERT succeeds; the other gets (existing_op, False).
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    # Build the insert values
    insert_id = uuid.uuid4()
    insert_values = {
        "id": insert_id,
        "user_id": user_id,
        "source": source,
        "source_chat_id": source_chat_id,
        "source_message_id": source_message_id,
        "source_bot_id": source_bot_id,
        "idempotency_key": idempotency_key,
        "raw_text": raw_text,
        "status": "pending",
        "result_json": {},
        "payload_hash": payload_hash,
    }

    # Determine the unique constraint to conflict on
    if idempotency_key:
        # Conflict on (user_id, idempotency_key)
        stmt = pg_insert(ConsumptionOperation).values(insert_values)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["user_id", "idempotency_key"]
        )
        db.execute(stmt)
        db.flush()
        # Re-select the winner (either our insert or the existing one)
        op = db.query(ConsumptionOperation).filter(
            ConsumptionOperation.user_id == user_id,
            ConsumptionOperation.idempotency_key == idempotency_key,
        ).first()
        if op is None:
            raise RuntimeError("Failed to create or find operation after insert")
        # Determine if we created it or found an existing one
        created = op.id == insert_id
        if not created:
            # Check payload hash conflict
            if payload_hash and op.payload_hash and op.payload_hash != payload_hash:
                raise ValueError(
                    "conflicting_payload_reuse: same idempotency_key but different payload."
                )
        return op, created

    elif source == "telegram":
        # Conflict on (user_id, source_bot_id, source_chat_id, source_message_id)
        stmt = pg_insert(ConsumptionOperation).values(insert_values)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["user_id", "source_bot_id", "source_chat_id", "source_message_id"]
        )
        db.execute(stmt)
        db.flush()
        # Re-select the winner
        op = db.query(ConsumptionOperation).filter(
            ConsumptionOperation.user_id == user_id,
            ConsumptionOperation.source_bot_id == source_bot_id,
            ConsumptionOperation.source_chat_id == source_chat_id,
            ConsumptionOperation.source_message_id == source_message_id,
        ).first()
        if op is None:
            raise RuntimeError("Failed to create or find operation after insert")
        created = op.id == insert_id
        if not created:
            if payload_hash and op.payload_hash and op.payload_hash != payload_hash:
                raise ValueError(
                    "conflicting_payload_reuse: same Telegram identity but different payload."
                )
        return op, created
    else:
        # This should never be reached due to the identity check above
        raise RuntimeError("Unreachable: identity check failed")


def get_operation_result(db: Session, operation_id: str) -> Optional[dict]:
    """Return the durable result of a completed operation, if any."""
    op = db.query(ConsumptionOperation).filter(
        ConsumptionOperation.id == operation_id,
    ).first()
    if op and op.status == "completed" and op.result_json:
        return op.result_json
    return None


def find_completed_result_by_identity(
    db: Session,
    user_id: str,
    source: str = "telegram",
    source_chat_id: Optional[str] = None,
    source_message_id: Optional[str] = None,
    source_bot_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Optional[dict]:
    """Look up a COMPLETED operation by source identity and return its saved result.

    B2 (durable replay without Redis draft): when the Redis draft is gone but a
    completed operation already exists for this source identity, return the
    persisted result_json so the caller gets the original meal instead of a 404
    or an empty meal. Returns None if no completed operation exists for the
    given identity.
    """
    if idempotency_key:
        op = db.query(ConsumptionOperation).filter(
            ConsumptionOperation.user_id == user_id,
            ConsumptionOperation.idempotency_key == idempotency_key,
        ).first()
    elif source == "telegram" and source_chat_id and source_message_id and source_bot_id:
        op = db.query(ConsumptionOperation).filter(
            ConsumptionOperation.user_id == user_id,
            ConsumptionOperation.source_bot_id == source_bot_id,
            ConsumptionOperation.source_chat_id == source_chat_id,
            ConsumptionOperation.source_message_id == source_message_id,
        ).first()
    else:
        return None

    if op and op.status == "completed" and op.result_json:
        return op.result_json
    return None


def write_consumption(
    db: Session,
    user_id: str,
    items: list[ParsedItem],
    meal_type: Optional[str] = None,
    eaten_at: Optional[datetime] = None,
    operation_id: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Write a full consumption (meal + linked beverages) atomically.

    This is the SINGLE write path. It:
    1. Creates the meal row
    2. Creates meal_items for each item
    3. For beverage items, creates a Measurement row AND a BeverageMeasurement link
    4. Computes totals from ALL 6 nutrients
    5. Stores the operation result for replay
    6. All in ONE transaction

    Returns the confirm-shaped result dict.
    """
    meal_type = _normalize_meal_type(meal_type)
    now = datetime.now(timezone.utc)
    eaten_at = eaten_at or now

    # Ensure we have an operation
    if operation_id:
        # Lock the operation row so concurrent confirms serialize here: the
        # second writer blocks until the first commits, then observes
        # status='completed' and returns the saved result instead of writing a
        # duplicate meal. This makes replay durable at the DB level.
        op = db.query(ConsumptionOperation).filter(
            ConsumptionOperation.id == operation_id,
            ConsumptionOperation.user_id == user_id,
        ).with_for_update().first()
        if not op:
            raise ValueError("operation_not_found")
        # If already completed, return the stored result (idempotent replay)
        if op.status == "completed" and op.result_json:
            return op.result_json
    else:
        op = ConsumptionOperation(
            id=str(uuid.uuid4()),
            user_id=user_id,
            source="api",
            status="pending",
            result_json={},
        )
        db.add(op)
        db.flush()
        operation_id = op.id

    # Create meal
    meal_id = str(uuid.uuid4())
    meal = Meal(
        id=meal_id,
        user_id=user_id,
        eaten_at=eaten_at,
        meal_type=meal_type,
        status="confirmed",
        input_method="text_intelligent",
        notes=notes,
        totals_json={k: 0.0 for k in NUTRIENT_KEYS},
        confidence=0.8,
        confirmed_at=now,
        source_operation_id=operation_id,
    )
    db.add(meal)
    db.flush()

    item_details = []
    all_nutrient_dicts = []

    for idx, item in enumerate(items):
        item_key = _stable_item_key(operation_id, idx)
        source_record_id = _compute_source_record_id(operation_id, item_key)

        # Create consumption_item record
        ci = ConsumptionItem(
            id=str(uuid.uuid4()),
            operation_id=operation_id,
            user_id=user_id,
            item_key=item_key,
            item_kind="beverage" if item.is_beverage else "food",
            display_name=item.display_name,
            quantity=item.quantity,
            unit=item.unit,
            grams=item.grams,
            volume_ml=item.volume_ml,
            nutrients_per_100=item.nutrients_per_100,
            nutrients_scaled=item.nutrients_scaled,
            beverage_category=item.beverage_category,
            meal_type=meal_type,
            source=item.source,
            confidence=item.confidence,
            # Stage 2: nutrient resolution provenance
            nutrient_basis=item.nutrient_basis,
            resolution_source=item.resolution_source,
            food_catalog_item_id=item.food_catalog_item_id,
            nutrition_complete=item.nutrition_complete,
        )
        db.add(ci)
        db.flush()

        # Create meal_item
        meal_item_id = str(uuid.uuid4())
        mi = MealItem(
            id=meal_item_id,
            meal_id=meal_id,
            display_name=item.display_name,
            quantity=item.quantity,
            unit=item.unit,
            grams=item.grams,
            nutrients_json=item.nutrients_scaled,
            source=item.source,
            confidence=item.confidence,
            source_operation_id=operation_id,
            source_item_id=ci.id,
            volume_ml=item.volume_ml,
            beverage_category=item.beverage_category,
        )
        db.add(mi)
        db.flush()

        ci.meal_item_id = meal_item_id
        all_nutrient_dicts.append(item.nutrients_scaled)

        # For beverages, also create a Measurement row (liquid tracking)
        measurement_id = None
        if item.is_beverage and item.volume_ml:
            measurement_id = str(uuid.uuid4())
            meas = Measurement(
                id=measurement_id,
                user_id=user_id,
                metric_type="water",
                start_at=eaten_at,
                end_at=eaten_at,
                value_json={"amount_ml": item.volume_ml, "category": item.beverage_category or "water"},
                unit="ml",
                source_provider="consumption",
                source_record_id=source_record_id,
                recording_method="automatic",
                confidence=item.confidence,
                source_operation_id=operation_id,
                source_item_id=ci.id,
                meal_item_id=meal_item_id,
            )
            db.add(meas)
            db.flush()

            ci.measurement_id = measurement_id

            # Create the 1:1 link
            bev = BeverageMeasurement(
                id=str(uuid.uuid4()),
                user_id=user_id,
                meal_item_id=meal_item_id,
                measurement_id=measurement_id,
                consumption_item_id=ci.id,
            )
            db.add(bev)

        item_details.append({
            "display_name": item.display_name,
            "grams": item.grams,
            "volume_ml": item.volume_ml,
            "beverage_category": item.beverage_category,
            "source": item.source,
            "meal_item_id": meal_item_id,
            "measurement_id": measurement_id,
        })

    # Compute meal totals from all 6 nutrients
    totals = _sum_nutrients(all_nutrient_dicts)
    meal.totals_json = totals

    # Stage 2: track whether all items have complete nutrition
    meal_nutrition_complete = all(item.nutrition_complete for item in items)

    # Update operation status and store durable result
    op.status = "completed"
    op.updated_at = now
    result = {
        "ok": True,
        "data": {
            "meal_id": meal_id,
            "status": "confirmed",
            "totals": totals,
            "items": item_details,
            "auto_confirmed": True,
            "nutrition_complete": meal_nutrition_complete,
        },
        "message": "Meal logged with best available matches.",
    }
    op.result_json = result

    db.commit()
    return result


# ---------------------------------------------------------------------------
# Confirm handler bridge
# ---------------------------------------------------------------------------

def confirm_consumption(
    db: Session,
    user_id: str,
    items: list[ParsedItem],
    meal_type: Optional[str] = None,
    eaten_at: Optional[datetime] = None,
    notes: Optional[str] = None,
    *,
    source: str = "telegram",
    source_chat_id: Optional[str] = None,
    source_message_id: Optional[str] = None,
    source_bot_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    raw_text: Optional[str] = None,
) -> dict:
    """Single entry point for the /meals/from-text-intelligent/confirm handler.

    Resolves (or creates) the ConsumptionOperation for the caller's source
    identity FIRST — before any write — so that a retry after the Redis draft
    is deleted finds the already-completed operation and returns its saved
    result instead of 404-ing or creating a duplicate meal.

    Flow:
      1. get_or_create_operation — durable replay lookup by Telegram key or
         caller idempotency_key. If an existing operation is already
         'completed', write_consumption returns its saved result.
      2. write_consumption(items, operation_id=...) — atomic meal + beverage
         write in ONE transaction, persisted result_json for replay.

    Returns the confirm-shaped result dict with durable replay semantics:
    calling this twice for the same source identity returns the SAME result
    (same meal_id) and never creates a second meal or beverage measurement.
    """
    # Compute payload hash BEFORE calling get_or_create so we can detect
    # conflicting reuse of the same identity key.
    payload_hash = _compute_payload_hash(items, meal_type) if items else None

    op, created = get_or_create_operation(
        db=db,
        user_id=user_id,
        source=source,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        source_bot_id=source_bot_id,
        idempotency_key=idempotency_key,
        raw_text=raw_text,
        payload_hash=payload_hash,
    )
    # Commit the pending operation row so concurrent callers can see it
    # (the row lock in write_consumption serializes the actual write).
    if created:
        db.flush()

    return write_consumption(
        db=db,
        user_id=user_id,
        items=items,
        meal_type=meal_type,
        eaten_at=eaten_at,
        operation_id=str(op.id),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Unified mutations
# ---------------------------------------------------------------------------

def update_item_quantity(
    db: Session, user_id: str, meal_id: str, item_fragment: str, new_grams: float
) -> dict:
    """Change a meal item's weight and rescale its nutrition + linked volume."""
    meal = db.query(Meal).filter(Meal.id == meal_id, Meal.user_id == user_id).first()
    if not meal:
        return {"ok": False, "error": "meal_not_found"}

    items = db.query(MealItem).filter(MealItem.meal_id == meal_id).all()
    target = _match_item(items, item_fragment)
    if not target:
        return {"ok": False, "error": "item_not_found", "items": [i.display_name for i in items]}

    old_grams = target.grams or 100.0
    factor = new_grams / old_grams

    # Rescale nutrients
    old_nj = target.nutrients_json or {}
    new_nj = {}
    for k in NUTRIENT_KEYS:
        try:
            new_nj[k] = round(float(old_nj.get(k) or 0) * factor, 2)
        except (TypeError, ValueError):
            new_nj[k] = 0.0

    target.grams = new_grams
    target.nutrients_json = new_nj
    target.user_corrected = True

    # If this item has a linked beverage measurement, update its volume too
    bev = db.query(BeverageMeasurement).filter(
        BeverageMeasurement.meal_item_id == target.id
    ).first()
    if bev:
        meas = db.query(Measurement).filter(
            Measurement.id == bev.measurement_id,
            Measurement.user_id == user_id,
        ).first()
        if meas:
            old_vj = dict(meas.value_json or {})
            old_ml = old_vj.get("amount_ml") or 0
            old_vj["amount_ml"] = round(old_ml * factor, 1)
            meas.value_json = old_vj  # assign new dict so SQLAlchemy detects change
            # Also update the meal_item's volume_ml
            target.volume_ml = round(old_ml * factor, 1)

    # Recompute meal totals
    _recompute_meal_totals(db, meal)
    db.commit()
    return {"ok": True, "item_id": target.id, "grams": new_grams, "rescaled": new_nj,
            "meal_totals": meal.totals_json}


def replace_beverage(
    db: Session, user_id: str, meal_id: str, item_fragment: str,
    new_display_name: str, new_beverage_category: Optional[str] = None,
    new_volume_ml: Optional[float] = None,
) -> dict:
    """Replace a beverage item: update classification, nutrition, and liquid projection.
    
    If the new item is NOT a beverage (new_beverage_category is None), the linked
    liquid measurement is removed.
    """
    meal = db.query(Meal).filter(Meal.id == meal_id, Meal.user_id == user_id).first()
    if not meal:
        return {"ok": False, "error": "meal_not_found"}

    items = db.query(MealItem).filter(MealItem.meal_id == meal_id).all()
    target = _match_item(items, item_fragment)
    if not target:
        return {"ok": False, "error": "item_not_found"}

    target.display_name = new_display_name
    target.beverage_category = new_beverage_category
    target.user_corrected = True

    # Check if the linked measurement should be removed (beverage -> solid)
    bev = db.query(BeverageMeasurement).filter(
        BeverageMeasurement.meal_item_id == target.id
    ).first()
    
    if bev and new_beverage_category is None:
        # Beverage replaced with solid: remove the liquid measurement
        db.query(Measurement).filter(
            Measurement.id == bev.measurement_id,
            Measurement.user_id == user_id,
        ).delete()
        db.delete(bev)
        target.volume_ml = None
    elif bev:
        # Still a beverage: update the measurement
        meas = db.query(Measurement).filter(
            Measurement.id == bev.measurement_id,
            Measurement.user_id == user_id,
        ).first()
        if meas:
            if new_beverage_category:
                meas.value_json["category"] = new_beverage_category
            if new_volume_ml:
                meas.value_json["amount_ml"] = new_volume_ml

    _recompute_meal_totals(db, meal)
    db.commit()
    return {"ok": True, "item_id": target.id, "display_name": new_display_name,
            "meal_totals": meal.totals_json}


def delete_beverage(db: Session, user_id: str, meal_id: str, item_fragment: str) -> dict:
    """Delete a beverage item AND its linked liquid measurement."""
    meal = db.query(Meal).filter(Meal.id == meal_id, Meal.user_id == user_id).first()
    if not meal:
        return {"ok": False, "error": "meal_not_found"}

    items = db.query(MealItem).filter(MealItem.meal_id == meal_id).all()
    target = _match_item(items, item_fragment)
    if not target:
        return {"ok": False, "error": "item_not_found"}

    # Find and delete linked measurement
    bev = db.query(BeverageMeasurement).filter(
        BeverageMeasurement.meal_item_id == target.id
    ).first()
    if bev:
        db.query(Measurement).filter(
            Measurement.id == bev.measurement_id,
            Measurement.user_id == user_id,
        ).delete()
        db.delete(bev)

    db.delete(target)
    _recompute_meal_totals(db, meal)
    db.commit()
    return {"ok": True, "deleted_item": target.display_name, "meal_totals": meal.totals_json}


def delete_meal(db: Session, user_id: str, meal_id: str) -> dict:
    """Delete a meal and all its items + linked beverage measurements."""
    meal = db.query(Meal).filter(Meal.id == meal_id, Meal.user_id == user_id).first()
    if not meal:
        return {"ok": False, "error": "meal_not_found"}

    # Delete linked beverage measurements
    bevs = db.query(BeverageMeasurement).filter(
        BeverageMeasurement.meal_item_id.in_(
            db.query(MealItem.id).filter(MealItem.meal_id == meal_id)
        )
    ).all()
    for bev in bevs:
        db.query(Measurement).filter(
            Measurement.id == bev.measurement_id,
            Measurement.user_id == user_id,
        ).delete()

    db.query(BeverageMeasurement).filter(
        BeverageMeasurement.meal_item_id.in_(
            db.query(MealItem.id).filter(MealItem.meal_id == meal_id)
        )
    ).delete(synchronize_session=False)

    db.query(MealItem).filter(MealItem.meal_id == meal_id).delete(synchronize_session=False)
    db.delete(meal)
    db.commit()
    return {"ok": True, "deleted": meal_id}


def _recompute_meal_totals(db: Session, meal: Meal) -> None:
    """Recompute meal totals from all items, across all 6 nutrients."""
    items = db.query(MealItem).filter(MealItem.meal_id == meal.id).all()
    nutrient_dicts = []
    for it in items:
        nj = it.nutrients_json
        if isinstance(nj, dict):
            nutrient_dicts.append(nj)
    meal.totals_json = _sum_nutrients(nutrient_dicts)


def _match_item(items: list[MealItem], frag: str) -> Optional[MealItem]:
    """Find the meal item a user fragment refers to, tolerantly."""
    if not frag:
        return None
    frag_l = frag.lower().strip()
    for it in items:
        dn = (it.display_name or "").lower()
        if frag_l and (frag_l in dn or dn in frag_l):
            return it
    # Token overlap
    frag_tok = set(re.findall(r"[a-z0-9]+", frag_l))
    best = None
    best_score = 0
    for it in items:
        dn = (it.display_name or "").lower()
        dn_tok = set(re.findall(r"[a-z0-9]+", dn))
        shared = frag_tok & dn_tok
        if shared:
            score = len(shared) / max(1, len(frag_tok))
            if score > best_score:
                best = it
                best_score = score
    return best if best_score >= 0.5 else None


# ---------------------------------------------------------------------------
# Manual liquid logging with reconciliation
# ---------------------------------------------------------------------------
#
# Three distinct intents must NOT be collapsed:
#
#   1. SAME-EVENT / IDEMPOTENT REPLAY
#      The same originating Telegram event already logged this drink (e.g. via
#      a meal confirm) OR this is a tool-invocation retry. The call carries a
#      SOURCE IDENTITY (chat_id + message_id + bot_id or idempotency_key).
#      Resolution: look up the ConsumptionOperation by that identity. If it is
#      already `completed`, return the SAVED result. No new row.
#
#   2. EXPLICIT-REFERENCE RECONCILIATION
#      The user says "also count that milk as liquid" — an explicit reference
#      to an EXISTING consumption item (item_id). Resolution: link the new
#      beverage volume to the EXISTING item/measurement. No second row.
#
#   3. GENUINELY NEW DRINK
#      The user says "another glass of milk" — a new drink in a new event.
#      Neither source identity nor existing-item reference. Resolution: write
#      a NEW consumption with volume AND calories (not a bare water row).
#
#   4. AMBIGUOUS
#      Cannot determine existing vs new. Resolution: return a CLARIFY response
#      — do NOT silently duplicate.
#
# Tool names (log_liquid, log_water) must NOT determine consumption identity.
# Identity is determined by source identity + explicit reference only.
# ---------------------------------------------------------------------------


def log_manual_liquid(
    db: Session,
    user_id: str,
    amount_ml: float,
    category: str = "water",
    *,
    # Source identity (idempotent replay / same-event meal+liquid)
    source: str = "telegram",
    source_chat_id: Optional[str] = None,
    source_message_id: Optional[str] = None,
    source_bot_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    # Explicit-reference reconciliation (also count that X as liquid)
    existing_item_id: Optional[str] = None,
    # Genuinely new drink context
    display_name: Optional[str] = None,
    eaten_at: Optional[datetime] = None,
    notes: Optional[str] = None,
    # Intent signaling
    intent: Optional[str] = None,  # "new" for genuinely new drink; None = ambiguous
    # Pre-enriched items (optional — for caloric drinks with real nutrients)
    items: Optional[list] = None,
) -> dict:
    """Reconciliation-aware manual liquid logging.

    Returns a dict with `ok` plus one of:
      - `data`: the saved/linked result (same-event, reconciliation, new drink)
      - `clarify`: True + `message` when intent is ambiguous

    The caller (MCP agent model) signals intent via request shape:
      - Omit both source identity AND existing_item_id → AMBIGUOUS → CLARIFY
        (unless `intent="new"` is set).
      - Provide source identity (chat_id+message_id+bot_id) → SAME-EVENT:
        replay saved result if already completed.
      - Provide existing_item_id → RECONCILIATION: link to existing item.
      - intent="new" (with no source identity / reference) → GENUINELY NEW drink.
    """
    now = datetime.now(timezone.utc)
    eaten_at = eaten_at or now

    # --- 1. Explicit-reference reconciliation --------------------------------
    if existing_item_id:
        return _reconcile_liquid(
            db, user_id, amount_ml, category, existing_item_id, eaten_at
        )

    # --- 2. Source-identity idempotent replay --------------------------------
    if (source == "telegram" and source_chat_id and source_message_id and source_bot_id) or idempotency_key:
        # Compute payload hash for identity comparison
        _payload_items = items if items is not None else _liquid_to_parsed_items(amount_ml, category, display_name)
        ph = _compute_payload_hash(_payload_items, None)
        try:
            op, created = get_or_create_operation(
                db=db,
                user_id=user_id,
                source=source,
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                source_bot_id=source_bot_id,
                idempotency_key=idempotency_key,
                raw_text=notes,
                payload_hash=ph,
            )
        except ValueError as e:
            # Fail-closed: identity missing or conflicting payload
            return {"ok": False, "error": "identity_required", "message": str(e)}
        if not created and op.status == "completed" and op.result_json:
            # Same originating event already logged this drink → replay saved result
            return op.result_json
        # If the operation exists but is not completed (pending), fall through
        # to write a new consumption under that operation. The row lock in
        # write_consumption will serialize concurrent writers.
        built_items = items if items is not None else _liquid_to_parsed_items(amount_ml, category, display_name)
        return write_consumption(
            db=db,
            user_id=user_id,
            items=built_items,
            eaten_at=eaten_at,
            operation_id=str(op.id),
            notes=notes,
        )

    # --- 2b. Telegram source without identity → FAIL CLOSED ------------------
    # If the caller explicitly signals telegram source but omits the required
    # identity, reject immediately. Do NOT fall through to ambiguous/clarify.
    if source == "telegram":
        return {
            "ok": False,
            "error": "identity_required",
            "message": (
                "telegram_source_requires_identity: source_chat_id, source_message_id, "
                "and source_bot_id are required for telegram source to enable retry protection."
            ),
        }

    # --- 3. Ambiguous intent → CLARIFY ---------------------------------------
    # No source identity, no explicit reference. Without an explicit "new"
    # intent, we cannot safely determine whether this is a new drink or a
    # duplicate of an existing one. Return a CLARIFY response — do NOT
    # silently duplicate.
    if intent != "new":
        return {
            "ok": False,
            "clarify": True,
            "message": (
                "Should I log this as a new drink, or add it to an existing one? "
                "Please clarify — e.g. 'another glass' for a new drink, or "
                "'also count that milk as liquid' to add to an existing one."
            ),
        }

    # --- 4. Genuinely new drink (no source identity, no reference) ----------
    # The caller explicitly signals a new drink. We write a full consumption
    # with volume AND calories — NOT a bare water row.
    built_items = items if items is not None else _liquid_to_parsed_items(amount_ml, category, display_name)
    return _write_new_liquid_consumption(
        db, user_id, built_items, eaten_at, source, notes
    )


def _reconcile_liquid(
    db: Session,
    user_id: str,
    amount_ml: float,
    category: str,
    existing_item_id: str,
    eaten_at: datetime,
) -> dict:
    """Link a manual liquid to an EXISTING consumption item.

    Finds the existing item (by ConsumptionItem.id or BeverageMeasurement.id
    or Measurement.id) and updates its volume in place. Does NOT insert a
    second row.
    """
    # Try ConsumptionItem.id first
    ci = db.query(ConsumptionItem).filter(
        ConsumptionItem.id == existing_item_id,
        ConsumptionItem.user_id == user_id,
    ).first()

    if ci and ci.measurement_id:
        meas = db.query(Measurement).filter(
            Measurement.id == ci.measurement_id,
            Measurement.user_id == user_id,
        ).first()
        if meas:
            old_vj = dict(meas.value_json or {})
            old_ml = old_vj.get("amount_ml") or 0
            old_vj["amount_ml"] = round(old_ml + amount_ml, 1)
            meas.value_json = old_vj
            db.commit()
            return {
                "ok": True,
                "data": {
                    "reconciled": True,
                    "measurement_id": str(meas.id),
                    "total_amount_ml": old_vj["amount_ml"],
                    "category": old_vj.get("category", category),
                },
                "message": f"Added {int(amount_ml)} ml to existing drink (now {int(old_vj['amount_ml'])} ml total).",
            }

    # Try BeverageMeasurement.id
    bev = db.query(BeverageMeasurement).filter(
        BeverageMeasurement.id == existing_item_id,
        BeverageMeasurement.user_id == user_id,
    ).first()
    if bev:
        meas = db.query(Measurement).filter(
            Measurement.id == bev.measurement_id,
            Measurement.user_id == user_id,
        ).first()
        if meas:
            old_vj = dict(meas.value_json or {})
            old_ml = old_vj.get("amount_ml") or 0
            old_vj["amount_ml"] = round(old_ml + amount_ml, 1)
            meas.value_json = old_vj
            db.commit()
            return {
                "ok": True,
                "data": {
                    "reconciled": True,
                    "measurement_id": str(meas.id),
                    "total_amount_ml": old_vj["amount_ml"],
                    "category": old_vj.get("category", category),
                },
                "message": f"Added {int(amount_ml)} ml to existing drink (now {int(old_vj['amount_ml'])} ml total).",
            }

    # Try Measurement.id directly
    meas = db.query(Measurement).filter(
        Measurement.id == existing_item_id,
        Measurement.user_id == user_id,
    ).first()
    if meas:
        old_vj = dict(meas.value_json or {})
        old_ml = old_vj.get("amount_ml") or 0
        old_vj["amount_ml"] = round(old_ml + amount_ml, 1)
        meas.value_json = old_vj
        db.commit()
        return {
            "ok": True,
            "data": {
                "reconciled": True,
                "measurement_id": str(meas.id),
                "total_amount_ml": old_vj["amount_ml"],
                "category": old_vj.get("category", category),
            },
            "message": f"Added {int(amount_ml)} ml to existing drink (now {int(old_vj['amount_ml'])} ml total).",
        }

    return {"ok": False, "error": "item_not_found", "message": "No existing drink found to reconcile with."}


def _liquid_to_parsed_items(
    amount_ml: float,
    category: str,
    display_name: Optional[str] = None,
) -> list:
    """Build a ParsedItem list for a standalone liquid."""
    name = display_name or category or "water"
    item = ParsedItem(
        display_name=name,
        quantity=1,
        unit="ml" if amount_ml != int(amount_ml) else "ml",
        grams=amount_ml,
        volume_ml=amount_ml,
        beverage_category=category,
        is_beverage=True,
    )
    # Standalone drinks have zero calories unless the caller enriches them.
    item.nutrients_per_100 = {k: 0 for k in NUTRIENT_KEYS}
    item.nutrients_scaled = {k: 0 for k in NUTRIENT_KEYS}
    return [item]


def _write_new_liquid_consumption(
    db: Session,
    user_id: str,
    items: list,
    eaten_at: datetime,
    source: str = "manual",
    notes: Optional[str] = None,
) -> dict:
    """Write a genuinely new drink consumption with volume AND calories.

    Unlike the legacy /manual/water endpoint (which writes a bare metric_type=water
    Measurement bypassing the consumption domain), this creates a proper
    ConsumptionOperation + BeverageMeasurement link so the drink contributes
    nutrition once and is trackable as a distinct consumption item.
    """
    op = ConsumptionOperation(
        id=str(uuid.uuid4()),
        user_id=user_id,
        source=source,
        status="pending",
        result_json={},
    )
    db.add(op)
    db.flush()

    # For a standalone caloric drink, we write a Measurement row directly
    # (beverage item) under the operation so it contributes nutrition.
    meal_type = DEFAULT_MEAL_TYPE
    meal_id = str(uuid.uuid4())
    meal = Meal(
        id=meal_id,
        user_id=user_id,
        eaten_at=eaten_at,
        meal_type=meal_type,
        status="confirmed",
        input_method="text_manual",
        notes=notes,
        totals_json={k: 0.0 for k in NUTRIENT_KEYS},
        confidence=0.8,
        confirmed_at=eaten_at,
        source_operation_id=op.id,
    )
    db.add(meal)
    db.flush()

    item_details = []
    for idx, item in enumerate(items):
        item_key = _stable_item_key(str(op.id), idx)
        source_record_id = _compute_source_record_id(str(op.id), item_key)

        ci = ConsumptionItem(
            id=str(uuid.uuid4()),
            operation_id=op.id,
            user_id=user_id,
            item_key=item_key,
            item_kind="beverage",
            display_name=item.display_name,
            quantity=item.quantity,
            unit=item.unit,
            grams=item.grams,
            volume_ml=item.volume_ml,
            nutrients_per_100=item.nutrients_per_100,
            nutrients_scaled=item.nutrients_scaled,
            beverage_category=item.beverage_category,
            meal_type=meal_type,
            source=source,
            confidence=item.confidence,
        )
        db.add(ci)
        db.flush()

        meal_item_id = str(uuid.uuid4())
        mi = MealItem(
            id=meal_item_id,
            meal_id=meal_id,
            display_name=item.display_name,
            quantity=item.quantity,
            unit=item.unit,
            grams=item.grams,
            nutrients_json=item.nutrients_scaled,
            source=source,
            confidence=item.confidence,
            source_operation_id=op.id,
            source_item_id=ci.id,
            volume_ml=item.volume_ml,
            beverage_category=item.beverage_category,
        )
        db.add(mi)
        db.flush()

        ci.meal_item_id = mi.id

        measurement_id = None
        if item.is_beverage and item.volume_ml:
            measurement_id = str(uuid.uuid4())
            meas = Measurement(
                id=measurement_id,
                user_id=user_id,
                metric_type="water",
                start_at=eaten_at,
                end_at=eaten_at,
                value_json={"amount_ml": item.volume_ml, "category": item.beverage_category or "water"},
                unit="ml",
                source_provider="consumption",
                source_record_id=source_record_id,
                recording_method="manual",
                confidence=item.confidence,
                source_operation_id=op.id,
                source_item_id=ci.id,
                meal_item_id=meal_item_id,
            )
            db.add(meas)
            db.flush()

            ci.measurement_id = meas.id

            bev = BeverageMeasurement(
                id=str(uuid.uuid4()),
                user_id=user_id,
                meal_item_id=meal_item_id,
                measurement_id=measurement_id,
                consumption_item_id=ci.id,
            )
            db.add(bev)

        item_details.append({
            "display_name": item.display_name,
            "grams": item.grams,
            "volume_ml": item.volume_ml,
            "beverage_category": item.beverage_category,
            "source": source,
            "meal_item_id": meal_item_id,
            "measurement_id": measurement_id,
        })

    totals = _sum_nutrients([item.nutrients_scaled for item in items])
    meal.totals_json = totals

    op.status = "completed"
    op.updated_at = eaten_at
    result = {
        "ok": True,
        "data": {
            "meal_id": meal_id,
            "status": "confirmed",
            "totals": totals,
            "items": item_details,
            "auto_confirmed": True,
        },
        "message": "Drink logged.",
    }
    op.result_json = result

    db.commit()
    return result


# ---------------------------------------------------------------------------
# B7 — Canonical atomic mutations for generic datapoints + timestamp/group
# ---------------------------------------------------------------------------

def update_measurement_value(
    db: Session,
    user_id: str,
    measurement_id: str,
    new_value_json: dict,
) -> dict:
    """Generic measurement update that delegates to beverage logic when the
    measurement is linked to a beverage (via BeverageMeasurement).

    For a beverage measurement, this updates:
      - Measurement.value_json
      - MealItem.volume_ml, MealItem.nutrients_json (rescaled)
      - MealItem.grams
      - Meal.totals_json
    All in one transaction.

    For a non-beverage measurement, updates only the Measurement.value_json.
    """
    meas = db.query(Measurement).filter(
        Measurement.id == measurement_id,
        Measurement.user_id == user_id,
    ).first()
    if not meas:
        return {"ok": False, "error": "measurement_not_found"}

    # Check if this measurement is a beverage
    bev = db.query(BeverageMeasurement).filter(
        BeverageMeasurement.measurement_id == measurement_id,
        BeverageMeasurement.user_id == user_id,
    ).first()

    if bev:
        # Beverage path: update all projections atomically
        mi = db.query(MealItem).filter(MealItem.id == bev.meal_item_id).first()
        if not mi:
            # Orphaned beverage_measurement — clean up and update measurement only
            db.delete(bev)
            meas.value_json = new_value_json
            db.commit()
            return {"ok": True, "measurement_id": measurement_id, "orphan_cleaned": True}

        old_ml = meas.value_json.get("amount_ml", 0) if isinstance(meas.value_json, dict) else 0
        new_ml = new_value_json.get("amount_ml", old_ml)

        # Update measurement
        meas.value_json = new_value_json

        # Rescale meal_item
        if old_ml > 0 and new_ml != old_ml:
            factor = new_ml / old_ml
            old_nj = mi.nutrients_json or {}
            new_nj = {}
            for k in NUTRIENT_KEYS:
                try:
                    new_nj[k] = round(float(old_nj.get(k) or 0) * factor, 2)
                except (TypeError, ValueError):
                    new_nj[k] = 0.0
            mi.nutrients_json = new_nj
            mi.volume_ml = new_ml
            if mi.grams is not None:
                mi.grams = new_ml  # 1ml ≈ 1g for beverages
        elif new_ml == old_ml:
            # Value unchanged, no rescale needed
            pass
        else:
            # old_ml was 0, can't rescale; just set new values
            mi.volume_ml = new_ml

        # Recompute meal totals
        meal = db.query(Meal).filter(Meal.id == mi.meal_id).first()
        if meal:
            _recompute_meal_totals(db, meal)
            db.commit()
            return {"ok": True, "measurement_id": measurement_id,
                    "meal_item_id": str(mi.id), "meal_totals": meal.totals_json}

    # Non-beverage path: just update the value
    meas.value_json = new_value_json
    db.commit()
    return {"ok": True, "measurement_id": measurement_id}


def delete_measurement(
    db: Session,
    user_id: str,
    measurement_id: str,
) -> dict:
    """Generic measurement delete that cascades to beverage projections when
    the measurement is a beverage.

    For a beverage measurement, this deletes:
      - Measurement
      - BeverageMeasurement link
      - MealItem (the beverage item)
      - Recomputes Meal.totals_json

    For a non-beverage measurement, just deletes the Measurement.
    """
    meas = db.query(Measurement).filter(
        Measurement.id == measurement_id,
        Measurement.user_id == user_id,
    ).first()
    if not meas:
        return {"ok": False, "error": "measurement_not_found"}

    bev = db.query(BeverageMeasurement).filter(
        BeverageMeasurement.measurement_id == measurement_id,
        BeverageMeasurement.user_id == user_id,
    ).first()

    if bev:
        mi = db.query(MealItem).filter(MealItem.id == bev.meal_item_id).first()
        meal_id = mi.meal_id if mi else None

        db.delete(bev)
        if mi:
            db.delete(mi)

        if meal_id:
            meal = db.query(Meal).filter(Meal.id == meal_id).first()
            if meal:
                _recompute_meal_totals(db, meal)

        db.delete(meas)
        db.commit()
        return {"ok": True, "measurement_id": measurement_id,
                "cascade": "beverage", "meal_id": meal_id}

    db.delete(meas)
    db.commit()
    return {"ok": True, "measurement_id": measurement_id}


def update_meal_timestamp(
    db: Session,
    user_id: str,
    meal_id: str,
    new_eaten_at: datetime,
) -> dict:
    """Update a meal's timestamp and propagate to all linked measurements
    and consumption items atomically.
    """
    meal = db.query(Meal).filter(Meal.id == meal_id, Meal.user_id == user_id).first()
    if not meal:
        return {"ok": False, "error": "meal_not_found"}

    meal.eaten_at = new_eaten_at

    # Update all linked beverage measurements
    for mi in meal.items:
        bev = db.query(BeverageMeasurement).filter(
            BeverageMeasurement.meal_item_id == mi.id
        ).first()
        if bev:
            meas = db.query(Measurement).filter(
                Measurement.id == bev.measurement_id,
                Measurement.user_id == user_id,
            ).first()
            if meas:
                meas.start_at = new_eaten_at
                meas.end_at = new_eaten_at

    # Update consumption items
    ci_list = db.query(ConsumptionItem).filter(
        ConsumptionItem.operation_id == meal.source_operation_id
    ).all()
    for ci in ci_list:
        ci.updated_at = datetime.now(timezone.utc)

    db.commit()
    return {"ok": True, "meal_id": meal_id, "eaten_at": new_eaten_at.isoformat()}


def update_meal_group(
    db: Session,
    user_id: str,
    meal_id: str,
    new_meal_type: str,
) -> dict:
    """Update a meal's group (breakfast/lunch/dinner/snack) and propagate
    to all linked consumption items atomically.
    """
    meal = db.query(Meal).filter(Meal.id == meal_id, Meal.user_id == user_id).first()
    if not meal:
        return {"ok": False, "error": "meal_not_found"}

    meal_type = _normalize_meal_type(new_meal_type)
    meal.meal_type = meal_type

    # Update consumption items
    ci_list = db.query(ConsumptionItem).filter(
        ConsumptionItem.operation_id == meal.source_operation_id
    ).all()
    for ci in ci_list:
        ci.meal_type = meal_type

    db.commit()
    return {"ok": True, "meal_id": meal_id, "meal_type": meal_type}


# ---------------------------------------------------------------------------
# Meal-item bridge helpers — used by routers/meals.py to delegate add/patch/
# delete/copy through the shared domain so beverages keep their liquid
# projection and mutations update all projections atomically.
# ---------------------------------------------------------------------------

def create_beverage_projection(
    db: Session,
    user_id: str,
    meal_item: MealItem,
    volume_ml: float,
    beverage_category: str,
    eaten_at: datetime,
) -> str | None:
    """Create the liquid Measurement + BeverageMeasurement link for an
    already-persisted beverage MealItem.

    Returns the new measurement_id, or None if volume_ml is not positive.
    """
    if not volume_ml or volume_ml <= 0:
        return None

    measurement_id = str(uuid.uuid4())
    meas = Measurement(
        id=measurement_id,
        user_id=user_id,
        metric_type="water",
        start_at=eaten_at,
        end_at=eaten_at,
        value_json={"amount_ml": volume_ml, "category": beverage_category or "water"},
        unit="ml",
        source_provider="consumption",
        source_record_id=f"meal-item:{meal_item.id}",
        recording_method="automatic",
        confidence=meal_item.confidence,
        meal_item_id=meal_item.id,
    )
    db.add(meas)
    db.flush()

    bev = BeverageMeasurement(
        id=str(uuid.uuid4()),
        user_id=user_id,
        meal_item_id=meal_item.id,
        measurement_id=measurement_id,
    )
    db.add(bev)

    meal_item.volume_ml = volume_ml
    meal_item.beverage_category = beverage_category
    return measurement_id


def propagate_beverage_patch(
    db: Session,
    user_id: str,
    meal_id: str,
    item: MealItem,
    old_grams: float | None,
) -> None:
    """After a PATCH /meals/{id}/items/{id} mutates a beverage item, propagate
    the change to the linked Measurement (volume + category) and recompute
    meal totals.

    Safe to call for non-beverage items (no-op). Handles grams→volume
    proportional scaling and beverage→solid declassification.
    """
    bev = db.query(BeverageMeasurement).filter(
        BeverageMeasurement.meal_item_id == item.id
    ).first()

    new_grams = item.grams
    new_category = item.beverage_category
    new_name = item.display_name

    # Determine if the item is still a beverage after the rename
    still_bev = new_category is not None or (
        new_name is not None and _classify_beverage(new_name) is not None
    ) if new_name else (new_category is not None)

    if bev and not still_bev:
        # Beverage renamed to a solid: remove the liquid projection
        db.query(Measurement).filter(
            Measurement.id == bev.measurement_id,
            Measurement.user_id == user_id,
        ).delete()
        db.delete(bev)
        item.volume_ml = None
        item.beverage_category = None
    elif bev:
        # Still a beverage: update the linked measurement
        meas = db.query(Measurement).filter(
            Measurement.id == bev.measurement_id,
            Measurement.user_id == user_id,
        ).first()
        if meas:
            old_vj = dict(meas.value_json or {})
            old_ml = old_vj.get("amount_ml") or 0
            old_g = old_grams or old_ml  # best-effort basis
            if old_g and new_grams is not None and new_grams != old_g:
                factor = new_grams / old_g
                new_ml = round(old_ml * factor, 1)
                old_vj["amount_ml"] = new_ml
                item.volume_ml = new_ml
            if new_category:
                old_vj["category"] = new_category
            meas.value_json = old_vj
    elif still_bev:
        # Solid renamed to a beverage: create a liquid projection
        meal = db.query(Meal).filter(Meal.id == meal_id).first()
        if meal:
            vol = item.volume_ml
            if vol is None and new_grams is not None:
                vol = float(new_grams)  # 1ml ≈ 1g for beverages
            if vol and vol > 0:
                cat = new_category or _classify_beverage(new_name) or "water"
                create_beverage_projection(db, user_id, item, vol, cat, meal.eaten_at)

    # Recompute totals from current items
    meal = db.query(Meal).filter(Meal.id == meal_id).first()
    if meal:
        _recompute_meal_totals(db, meal)


def delete_beverage_item(
    db: Session,
    user_id: str,
    meal_id: str,
    item_id: str,
) -> dict:
    """Delete a meal_item (by id) and its linked BeverageMeasurement + Measurement.

    Replacement for the router's bare db.delete(item) + _sync_totals so
    beverage deletions don't orphan liquid rows. Safe for non-beverage
    items (equivalent to db.delete + recompute).
    """
    from fastapi import HTTPException
    try:
        item_uuid = uuid.UUID(str(item_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="Item not found")

    mi = db.query(MealItem).filter(
        MealItem.id == item_uuid, MealItem.meal_id == meal_id
    ).first()
    if not mi:
        raise HTTPException(status_code=404, detail="Item not found")

    bev = db.query(BeverageMeasurement).filter(
        BeverageMeasurement.meal_item_id == item_id
    ).first()
    if bev:
        db.query(Measurement).filter(
            Measurement.id == bev.measurement_id,
            Measurement.user_id == user_id,
        ).delete()
        db.delete(bev)

    db.delete(mi)

    meal = db.query(Meal).filter(Meal.id == meal_id).first()
    if meal:
        _recompute_meal_totals(db, meal)

    return {"ok": True, "item_id": item_id, "meal_totals": meal.totals_json if meal else {}}


def copy_beverage_link(
    db: Session,
    user_id: str,
    source_meal_item_id: str,
    new_meal_item_id: str,
    eaten_at: datetime,
) -> str | None:
    """When copying a meal, replicate the BeverageMeasurement + Measurement
    for a copied beverage item so the copy is consistent with the source.

    Returns the new measurement_id, or None if the source item was not a
    beverage.
    """
    bev = db.query(BeverageMeasurement).filter(
        BeverageMeasurement.meal_item_id == source_meal_item_id
    ).first()
    if not bev:
        return None

    source_meas = db.query(Measurement).filter(
        Measurement.id == bev.measurement_id,
        Measurement.user_id == user_id,
    ).first()
    if not source_meas:
        # Orphaned link — clean up
        db.delete(bev)
        return None

    new_measurement_id = str(uuid.uuid4())
    new_value = dict(source_meas.value_json or {})
    new_meas = Measurement(
        id=new_measurement_id,
        user_id=user_id,
        metric_type="water",
        start_at=eaten_at,
        end_at=eaten_at,
        value_json=new_value,
        unit="ml",
        source_provider="consumption",
        source_record_id=f"copy:{source_meas.id}",
        recording_method="automatic",
        confidence=source_meas.confidence,
        meal_item_id=new_meal_item_id,
    )
    db.add(new_meas)
    db.flush()

    new_bev = BeverageMeasurement(
        id=str(uuid.uuid4()),
        user_id=user_id,
        meal_item_id=new_meal_item_id,
        measurement_id=new_measurement_id,
    )
    db.add(new_bev)

    # Sync the new meal_item's liquid fields from the copied measurement
    new_mi = db.query(MealItem).filter(MealItem.id == new_meal_item_id).first()
    if new_mi:
        new_mi.volume_ml = new_value.get("amount_ml")
        new_mi.beverage_category = new_value.get("category")

    return new_measurement_id
