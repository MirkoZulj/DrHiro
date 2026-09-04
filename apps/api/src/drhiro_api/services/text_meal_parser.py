"""Free-text meal parser.

Turns messy human text ("3 whole egs, 2 slices of tost and a large tomatoe")
into structured items that `MealItemIn` accepts and `_lookup_nutrients` can
price, so nutrition actually attaches instead of landing as a 0 kcal draft.

Design notes
------------
The LLM upstream is only asked to hand over cleaned free text -- a single
string, the one shape small models reliably produce. All structure is derived
here, deterministically, so a model that cannot emit nested JSON is never in
the critical path.

`_lookup_nutrients` re-resolves `display_name` through `resolve_food`, so the
name we emit MUST be the canonical catalogue name (e.g. "Bread, white,
commercially prepared") rather than the user's word ("tost"). It also ignores
`quantity` when scaling the local-DB path, therefore this parser always emits
an explicit `grams` value.

pg_trgm is not installed on this deployment, so fuzzy matching uses stdlib
`difflib` against the (small, ~370 row) food-name vocabulary.
"""

from __future__ import annotations

import difflib
import re
from functools import lru_cache

from drhiro_api.food_search import resolve_food
from drhiro_api.models import Food

# ---------------------------------------------------------------------------
# Vocabulary / normalisation tables
# ---------------------------------------------------------------------------

# USDA names are comma-inverted ("Bread, white, commercially prepared"), and
# some everyday words have no substring match at all -- "toast" returns zero
# rows. These map colloquial terms onto the EXACT catalogue display_name, which
# is looked up directly so resolve_food's ranking cannot drift the choice.
CANONICAL = {
    "toast": "Bread, white, commercially prepared",
    "bread": "Bread, white, commercially prepared",
    "white bread": "Bread, white, commercially prepared",
    "egg": "Egg, whole, raw, fresh",
    "eggs": "Egg, whole, raw, fresh",
    "whole egg": "Egg, whole, raw, fresh",
    "whole eggs": "Egg, whole, raw, fresh",
    "chicken breast": "Chicken, breast, boneless, skinless, raw",
    "chicken": "Chicken, breast, boneless, skinless, raw",
    "rice": "Rice, white, long grain, unenriched, raw",
    "white rice": "Rice, white, long grain, unenriched, raw",
    "broccoli": "Broccoli, raw",
    "tomato": "Tomatoes, red, ripe, raw, year round average",
    "tomatoes": "Tomatoes, red, ripe, raw, year round average",
    "espresso": "Coffee, espresso, restaurant-prepared",
    "milk": "Milk, whole, 3.25% milkfat, with added vitamin D",
    "whole milk": "Milk, whole, 3.25% milkfat, with added vitamin D",
    "potato": "Potatoes, russet, without skin, raw",
    "potatoes": "Potatoes, russet, without skin, raw",
    "banana": "Bananas, ripe and slightly ripe, raw",
    "bananas": "Bananas, ripe and slightly ripe, raw",
    "apple": "Apples, gala, with skin, raw",
    "apples": "Apples, gala, with skin, raw",
    "big mac": "McDONALD'S, BIG MAC",
    "bigmac": "McDONALD'S, BIG MAC",
}

# Typo-tolerance vocabulary: the colloquial keys above are what users actually
# type, so fuzzy correction targets these before touching catalogue names.
SYNONYMS = CANONICAL

# Portion weights (grams) for countable / unmeasured foods, so "3 eggs" and
# "a large tomato" become real numbers rather than a flat 100 g guess.
UNIT_GRAMS = {
    "egg": 50.0,
    "eggs": 50.0,
    "whole egg": 50.0,
    "whole eggs": 50.0,
    "slice": 28.0,
    "slice_bread": 28.0,
    "toast": 28.0,
    "bread": 28.0,
    "tomato": 123.0,
    "tomatoes": 123.0,
    "banana": 118.0,
    "bananas": 118.0,
    "apple": 182.0,
    "apples": 182.0,
    "potato": 213.0,
    "potatoes": 213.0,
    "cup": 240.0,
    "tbsp": 15.0,
    "tsp": 5.0,
    "shot": 30.0,
    "espresso": 30.0,
    "big mac": 210.0,
    "bigmac": 210.0,
}

SIZE_FACTORS = {
    "extra large": 1.5,
    "xl": 1.5,
    "large": 1.3,
    "big": 1.3,
    "medium": 1.0,
    "regular": 1.0,
    "small": 0.7,
    "mini": 0.55,
}

# Words that carry no food meaning; stripped before matching.
NOISE = {
    "a", "an", "the", "some", "of", "my", "for", "please", "portion",
    "serving", "servings", "piece", "pieces", "plate", "bowl", "glass",
    "ate", "had", "eat", "eaten", "log", "logged", "with", "and", "plus",
    "today", "lunch", "breakfast", "dinner", "snack", "meal",
}

# Unit tokens recognised as countable measures rather than food words.
COUNT_UNITS = {
    "slice": "slice", "slices": "slice",
    "cup": "cup", "cups": "cup",
    "tbsp": "tbsp", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "tsp": "tsp", "teaspoon": "tsp", "teaspoons": "tsp",
    "shot": "shot", "shots": "shot",
}

SPLIT_RE = re.compile(r",|\band\b|\bwith\b|\bplus\b|\+|\n|;|&", flags=re.I)
GRAMS_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(kg|kilograms?|kilos?|g|gr|gram|grams|ml|l|litres?|liters?)\b",
    flags=re.I,
)
LEADING_QTY_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(?:x\s*)?", flags=re.I)
WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "half": 0.5, "couple": 2, "dozen": 12,
}


@lru_cache(maxsize=1)
def _noise_pattern():
    return re.compile(r"\b(?:" + "|".join(sorted(NOISE, key=len, reverse=True)) + r")\b", flags=re.I)


def _food_vocabulary(db):
    """All catalogue names, lowercased, for fuzzy matching."""
    return [r[0] for r in db.query(Food.display_name).all() if r[0]]


def _to_float(raw: str) -> float:
    return float(raw.replace(",", "."))


# ---------------------------------------------------------------------------
# Per-fragment parsing
# ---------------------------------------------------------------------------

def _extract_amount(text: str):
    """Pull an explicit weight/volume out of the fragment.

    Returns (remaining_text, grams|None).
    """
    m = GRAMS_RE.search(text)
    if not m:
        return text, None
    value = _to_float(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith(("kg", "kilo")):
        grams = value * 1000.0
    elif unit in ("l", "litre", "litres", "liter", "liters"):
        grams = value * 1000.0
    else:
        # g / gr / gram / grams / ml -- ml treated as 1 g for water-like foods.
        grams = value
    return (text[: m.start()] + " " + text[m.end():]), grams


def _extract_count(text: str):
    """Pull a leading count and optional unit. Returns (text, qty, unit)."""
    qty = None
    unit = None

    m = LEADING_QTY_RE.match(text)
    if m:
        qty = _to_float(m.group(1))
        text = text[m.end():]
    else:
        first = text.strip().split(" ")[0].lower() if text.strip() else ""
        if first in WORD_NUMBERS:
            qty = float(WORD_NUMBERS[first])
            text = text.strip()[len(first):]

    tokens = text.strip().split()
    if tokens and tokens[0].lower() in COUNT_UNITS:
        unit = COUNT_UNITS[tokens[0].lower()]
        text = " ".join(tokens[1:])

    return text, qty, unit


def _extract_size(text: str):
    """Pull a size adjective. Returns (text, factor)."""
    low = text.lower()
    for phrase in sorted(SIZE_FACTORS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(phrase)}\b", low):
            text = re.sub(rf"\b{re.escape(phrase)}\b", " ", text, flags=re.I)
            return text, SIZE_FACTORS[phrase]
    return text, 1.0


def _clean(text: str) -> str:
    text = _noise_pattern().sub(" ", text)
    text = re.sub(r"[^\w\s'&-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _by_exact_name(db, canonical_name: str):
    return db.query(Food).filter(Food.display_name == canonical_name).first()


def _canonical_key(phrase: str):
    """Map a cleaned phrase to a CANONICAL key, tolerating typos.

    Tries exact key, then longest contained key, then fuzzy correction of the
    phrase and of each word against the colloquial key vocabulary.
    """
    low = phrase.lower().strip()
    if low in CANONICAL:
        return low

    # Longest key contained in the phrase ("grilled chicken breast" -> key).
    for key in sorted(CANONICAL, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", low):
            return key

    keys = list(CANONICAL)
    # Whole-phrase fuzzy ("tomatoe" -> "tomato", "big mak" -> "big mac").
    for cutoff in (0.85, 0.75, 0.68):
        hit = difflib.get_close_matches(low, keys, n=1, cutoff=cutoff)
        if hit:
            return hit[0]

    # Word-level fuzzy ("egs" -> "eggs", "tost" -> "toast", "brocoli").
    words = low.split()
    for cutoff in (0.85, 0.75, 0.7):
        for word in words:
            if len(word) <= 2:
                continue
            hit = difflib.get_close_matches(word, keys, n=1, cutoff=cutoff)
            if hit:
                return hit[0]
        # Also try adjacent word pairs for two-word keys ("chiken brest").
        for i in range(len(words) - 1):
            pair = f"{words[i]} {words[i+1]}"
            hit = difflib.get_close_matches(pair, keys, n=1, cutoff=cutoff)
            if hit:
                return hit[0]
    return None


def _resolve(db, name: str, vocabulary):
    """Resolve a cleaned food phrase to a catalogue row.

    Order: canonical synonym (typo-tolerant, exact DB lookup) -> resolve_food
    -> difflib against catalogue names. Returns (Food|None, matched_name|None).
    """
    if not name:
        return None, None

    # 1. Canonical map, tolerant of typos. Deterministic and highest priority so
    #    "toast"/"tost" and "rice"/"rise" always land on the same food.
    key = _canonical_key(name)
    if key:
        food = _by_exact_name(db, CANONICAL[key])
        if food is not None:
            return food, food.display_name, key

    # 2. The catalogue's own ranked resolver.
    res = resolve_food(db, name, limit=5)
    best = getattr(res, "best", None)
    if best is None:
        matches = getattr(res, "matches", None) or []
        best = matches[0].food if matches else None
    if best is not None:
        return best, best.display_name, key

    # 3. Fuzzy against full catalogue names (pg_trgm unavailable -> difflib).
    lowered = {v.lower(): v for v in vocabulary}
    for cutoff in (0.82, 0.7, 0.62):
        hit = difflib.get_close_matches(name.lower(), list(lowered), n=1, cutoff=cutoff)
        if hit:
            food = _by_exact_name(db, lowered[hit[0]])
            if food is not None:
                return food, food.display_name, key

    return None, None, key


def _portion_grams(cleaned: str, food, qty, unit, size_factor, canon_key=None):
    """Decide grams when no explicit weight was given.

    Uses the resolved canonical key when available, so "3 eggs" and the typo'd
    "3 egs" produce the same weight rather than diverging on spelling.
    """
    key = (canon_key or cleaned).lower().strip()
    base = None

    if unit and unit in UNIT_GRAMS:
        base = UNIT_GRAMS[unit]
        if unit == "slice" and ("bread" in key or "toast" in key):
            base = UNIT_GRAMS["slice_bread"]
    if base is None:
        for token, grams in sorted(UNIT_GRAMS.items(), key=lambda kv: -len(kv[0])):
            if re.search(rf"\b{re.escape(token)}s?\b", key):
                base = grams
                break
    if base is None and food is not None:
        base = getattr(food, "serving_grams", None)
    if base is None:
        base = 100.0

    return round(base * (qty if qty else 1.0) * size_factor, 1)



_BOUNDARY_UNIT = r"(?:g|gr|gram|grams|kg|ml|l|dl|cl)"
_BOUNDARY_SIZE = (
    r"(?:large|small|medium|big|whole|half|slices?|glass|glasses"
    r"|cup|cups|piece|pieces|clove|cloves)"
)
_BOUNDARY_NUMWORD = r"(?:one|two|three|four|five|six|seven|eight|nine|ten|a|an)"

# A quantity starting mid-fragment marks a new food even with no conjunction:
# "500 g of steak 200 g of lettuce" is two items. The lookahead requires a word
# AFTER the unit so a trailing weight stays attached ("big mac 210g" = one item).
_IMPLICIT_BOUNDARY_RE = re.compile(
    r"(?<=\w)\s+(?="
    rf"\d+(?:[.,]\d+)?\s*{_BOUNDARY_UNIT}\b\s+(?:of\s+)?[a-z]"
    rf"|{_BOUNDARY_NUMWORD}\s+{_BOUNDARY_SIZE}\b"
    rf"|\d+\s+{_BOUNDARY_SIZE}\b"
    r")",
    flags=re.I,
)


def _split_fragments(text: str):
    """Split on explicit separators, then on implicit quantity boundaries."""
    for fragment in SPLIT_RE.split(text):
        if not fragment or not fragment.strip():
            continue
        for piece in _IMPLICIT_BOUNDARY_RE.split(fragment):
            if piece and piece.strip():
                yield piece

def parse_meal_text(db, text: str) -> list[dict]:
    """Parse free text into MealItemIn-compatible dicts.

    Never raises on unparseable input: unresolved fragments are still returned
    with their cleaned name so nothing is silently dropped, and the existing
    pipeline flags the meal for review.
    """
    if not text or not str(text).strip():
        return []

    try:
        vocabulary = _food_vocabulary(db)
    except Exception:
        vocabulary = []

    items: list[dict] = []
    for fragment in _split_fragments(str(text)):
        if not fragment or not fragment.strip():
            continue

        try:
            rest, grams = _extract_amount(fragment)
            rest, qty, unit = _extract_count(rest)
            rest, size_factor = _extract_size(rest)
            cleaned = _clean(rest)
            if not cleaned:
                continue

            food, matched, canon_key = _resolve(db, cleaned, vocabulary)

            if grams is None:
                grams = _portion_grams(cleaned, food, qty, unit, size_factor, canon_key)

            item = {
                "display_name": matched or cleaned,
                "grams": float(grams),
                "quantity": float(qty) if qty else 1.0,
            }
            if unit:
                item["unit"] = unit
            if food is not None:
                fid = getattr(food, "id", None)
                if fid is not None:
                    item["food_catalog_item_id"] = str(fid)
            items.append(item)
        except Exception:
            # One bad fragment must never lose the rest of the meal.
            fallback = _clean(fragment)
            if fallback:
                items.append({"display_name": fallback, "grams": 100.0, "quantity": 1.0})

    return items
