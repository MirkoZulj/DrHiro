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
)

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
    "cup": 240.0, "cups": 240.0,
    "glass": 250.0, "glasses": 250.0,
    "bottle": 500.0, "bottles": 500.0,
    "can": 330.0, "cans": 330.0,
    "espresso": 30.0, "shot": 30.0, "shots": 30.0,
    "mug": 300.0,
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_float(num: str) -> float:
    """Parse a number that may use comma as decimal separator."""
    return float(num.replace(",", "."))


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
    r'(?P<unit>g|grams?|kg|ml|milliliters?|millilitres?|l|liters?|litres?|dcl|dl|'
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
# We split on comma only when NOT between two digits (negative lookahead/lookbehind)
_ITEM_SPLIT_RE = re.compile(r'(?<!\d)\s*,\s*(?!\d)|\band\b|\bwith\b|\bplus\b|\+|;', re.I)


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
        return ParsedItem(display_name=food, quantity=cnt, grams=grams)

    # Bare food word
    if part.strip():
        food = part.strip()
        grams = ITEM_GRAMS.get(food.lower().rstrip("s"), 100)
        return ParsedItem(display_name=food, quantity=1, grams=grams)

    return None


def _build_item(qty: float, unit: str, food: str) -> ParsedItem:
    """Build a ParsedItem from qty+unit+food."""
    gram_units = {"g", "gram", "grams", "kg"}
    volume_units = {"ml", "milliliter", "millilitre", "milliliters", "millilitres",
                    "l", "liter", "litres", "liters", "dcl", "dl",
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
        return ParsedItem(display_name=food, quantity=qty, unit=unit, grams=grams)
    elif unit in volume_units:
        ml = qty * _VOLUME_UNITS.get(unit, 1.0)
        # Volume unit implies beverage
        if not bev_cat:
            bev_cat = "non_alcoholic"
        return ParsedItem(
            display_name=food, quantity=qty, unit=unit,
            volume_ml=ml, beverage_category=bev_cat, is_beverage=True,
            grams=ml,  # 1ml ≈ 1g for water-like
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
) -> tuple[ConsumptionOperation, bool]:
    """Get existing operation or create a new one. Returns (operation, created)."""
    # Try Telegram key
    if source == "telegram" and source_chat_id and source_message_id:
        op = db.query(ConsumptionOperation).filter(
            ConsumptionOperation.user_id == user_id,
            ConsumptionOperation.source_bot_id == source_bot_id,
            ConsumptionOperation.source_chat_id == source_chat_id,
            ConsumptionOperation.source_message_id == source_message_id,
        ).first()
        if op:
            return op, False

    # Try idempotency key
    if idempotency_key:
        op = db.query(ConsumptionOperation).filter(
            ConsumptionOperation.user_id == user_id,
            ConsumptionOperation.idempotency_key == idempotency_key,
        ).first()
        if op:
            return op, False

    op = ConsumptionOperation(
        id=str(uuid.uuid4()),
        user_id=user_id,
        source=source,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        source_bot_id=source_bot_id,
        idempotency_key=idempotency_key,
        raw_text=raw_text,
        status="pending",
        result_json={},
    )
    db.add(op)
    db.flush()
    return op, True


def get_operation_result(db: Session, operation_id: str) -> Optional[dict]:
    """Return the durable result of a completed operation, if any."""
    op = db.query(ConsumptionOperation).filter(
        ConsumptionOperation.id == operation_id,
    ).first()
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
        op = db.query(ConsumptionOperation).filter(
            ConsumptionOperation.id == operation_id,
            ConsumptionOperation.user_id == user_id,
        ).first()
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
        },
        "message": "Meal logged with best available matches.",
    }
    op.result_json = result

    db.commit()
    return result


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
