"""parse_log_text / commit_intents — the live logging path for one Telegram line.

The meal-only writer could not satisfy the liquid and activity ledgers, so one
message is parsed into INTENTS and committed in ONE transaction:

    parse_log_text(db, text) -> list[LogIntent]
    commit_intents(db, user, intents, ...)  # single transaction, all three ledgers

Ledgers:
  meal     -> meals + meal_items
  liquid   -> measurements (value_json: amount_ml, category) (+ beverage_measurements
              link when a meal item owns the drink)
  activity -> activities

Drink policy (locked):
  * kcal <= 5 and category in {water, tea, coffee} -> LIQUID ONLY (no meal row)
  * any other drink, or a caloric drink whose kcal is unknown -> meal + liquid
  * a drink is NEVER stored as grams
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text as _sql
from sqlalchemy.orm import Session

from drhiro_api.models import Activity, BeverageMeasurement, Meal, MealItem, Measurement, Nutrient
from drhiro_api.services.consumption import _delete_beverage_projection_for_meal_items
from drhiro_api.food_search import resolve_food, nutrient_map

# --------------------------------------------------------------------------- #
# vocabulary
# --------------------------------------------------------------------------- #
LIQUID_CATEGORIES = (
    "water", "coffee", "tea", "juice", "soda", "milk", "alcohol", "smoothie",
    "broth", "other",
)

#: Drinks that are liquid-only when their energy is <= 5 kcal / 100 ml.
LOW_CALORIE_CATEGORIES = {"water", "tea", "coffee"}

#: phrase -> (category, is_low_calorie_by_default)
DRINK_WORDS: dict[str, tuple[str, bool]] = {}
for _w in ("water", "sparkling water", "mineral water", "tap water", "voda"):
    DRINK_WORDS[_w] = ("water", True)
for _w in ("black coffee", "espresso", "coffee", "kava"):
    DRINK_WORDS[_w] = ("coffee", True)
for _w in ("green tea", "herbal tea", "tea", "caj", "čaj"):
    DRINK_WORDS[_w] = ("tea", True)

#: These are caloric (or unknown-caloric): they must reach BOTH ledgers.
DRINK_WORDS.update({
    "latte": ("coffee", False),
    "cappuccino": ("coffee", False),
    "caffe latte": ("coffee", False),
    "frappe": ("coffee", False),
    "orange juice": ("juice", False),
    "juice": ("juice", False),
    "sok": ("juice", False),
    "smoothie": ("smoothie", False),
    "coke": ("soda", False),
    "coca cola": ("soda", False),
    "cola": ("soda", False),
    "soda": ("soda", False),
    "fanta": ("soda", False),
    "sprite": ("soda", False),
    "milk": ("milk", False),
    "mlijeko": ("milk", False),
    "beer": ("alcohol", False),
    "pivo": ("alcohol", False),
    "wine": ("alcohol", False),
    "vino": ("alcohol", False),
    "whiskey": ("alcohol", False),
    "vodka": ("alcohol", False),
    "gin": ("alcohol", False),
    "shot": ("alcohol", False),
    "broth": ("broth", True),
})

#: container/unit -> millilitres (locked conversion table).
UNIT_ML = {
    "ml": 1.0, "millilitre": 1.0, "millilitres": 1.0,
    "milliliter": 1.0, "milliliters": 1.0,
    "l": 1000.0, "litre": 1000.0, "litres": 1000.0,
    "liter": 1000.0, "liters": 1000.0,
    "cup": 240.0, "cups": 240.0,
    "glass": 250.0, "glasses": 250.0,
    "shot": 30.0, "shots": 30.0,
    "can": 330.0, "cans": 330.0,
    "bottle": 500.0, "bottles": 500.0,
}

ACTIVITY_WORDS = {
    "walk": "walk", "walked": "walk", "walking": "walk",
    "run": "run", "ran": "run", "running": "run",
    "gym": "gym", "workout": "workout",
    "yoga": "yoga", "cycle": "cycle", "cycled": "cycle",
    "cycling": "cycle", "bike": "cycle", "swim": "swim", "swam": "swim",
}

_BARE_CONTAINER_RE = re.compile(
    r"\b(?:a\s+|an\s+|one\s+)?(glass|glasses|cup|cups|shot|shots|can|cans|"
    r"bottle|bottles)\b\s*(?:of\b)?", re.I)
_ML_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(ml|millilitres?|milliliters?|l|litres?|liters?|cups?|"
    r"glasses?|glass|shots?|cans?|bottles?)\b", re.I)
_ACT_RE = re.compile(
    r"\b(walk(?:ed|ing)?|ran|run(?:ning)?|gym|workout|yoga|cycled?|cycling|bike|"
    r"swam|swim)\b", re.I)
_MIN_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:min|mins|minutes?)\b", re.I)
_KCAL_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:kcal|calories?|cal)\b", re.I)
_LEAD_VERB_RE = re.compile(
    r"^\s*(?:i\s+)?(?:ate|eat|had|have|drank|drink|did|do|took|take|logged|log)"
    r"(?:\s+up)?\s+", re.I)
_LEAD_SLOT_RE = re.compile(r"^\s*(?:breakfast|lunch|dinner|snack|supper)\b\s*[:\-]?\s*",
                           re.I)
_SLOT_WORDS = {"breakfast": "breakfast", "lunch": "lunch", "dinner": "dinner",
               "supper": "dinner", "snack": "snack"}


def _num(raw: str) -> float:
    return float(str(raw).replace(",", "."))


@dataclass
class LogIntent:
    kind: str                      # meal | liquid | activity
    display_name: str = ""
    grams: Optional[float] = None
    volume_ml: Optional[float] = None
    category: Optional[str] = None
    quantity: float = 1.0
    is_drink: bool = False
    kcal: Optional[float] = None
    duration_min: Optional[float] = None
    needs_review: bool = False
    food_catalog_item_id: Optional[str] = None
    raw: str = ""


@dataclass
class ParsedLog:
    intents: list[LogIntent] = field(default_factory=list)
    meal_slot: Optional[str] = None


def _strip_lead_verbs(text: str) -> str:
    out = text or ""
    while True:
        nxt = _LEAD_VERB_RE.sub("", out)
        if nxt == out:
            return out
        out = nxt


def _slot(text: str) -> Optional[str]:
    m = _LEAD_SLOT_RE.match(text or "")
    if m:
        return _SLOT_WORDS.get(m.group(0).strip().rstrip(":- ").lower())
    low = (text or "").lower()
    for word, slot in _SLOT_WORDS.items():
        if re.search(rf"\b{word}\b", low):
            return slot
    return None


def _strip_slot(text: str) -> str:
    t = _LEAD_SLOT_RE.sub("", text or "")
    return re.sub(r"\b(?:breakfast|lunch|dinner|snack|supper)\b\s*[:\-]?", " ", t,
                  flags=re.I)


def _find_volume(text: str):
    """Return (volume_ml|None, text_without_volume, unit_word|None).

    Handles a numeric figure with a unit ("300ml", "0.5 l", "2 cans") and a bare
    container with no number ("glass of milk" -> 250 ml) using the locked table.
    """
    m = _ML_RE.search(text)
    if m:
        unit = m.group(2).lower()
        return (_num(m.group(1)) * UNIT_ML.get(unit, 1.0),
                text[:m.start()] + " " + text[m.end():], unit)
    m = _BARE_CONTAINER_RE.search(text)
    if m:
        unit = m.group(1).lower()
        return (UNIT_ML.get(unit, 0.0) or None,
                text[:m.start()] + " " + text[m.end():], unit)
    return None, text, None


def _find_drink(text: str):
    """Return (category, low_calorie_hint, matched_phrase) or (None, None, None)."""
    low = (text or "").lower()
    best = None
    for phrase in sorted(DRINK_WORDS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(phrase)}\b", low):
            best = phrase
            break
    if not best:
        return None, None, None
    cat, low_cal = DRINK_WORDS[best]
    # An explicit caloric sweetener/qualifier overrides the "black coffee" shortcut.
    if cat == "coffee" and re.search(r"\b(?:latte|cappuccino|milk|sugar|frappe)\b", low):
        low_cal = False
    return cat, low_cal, best


def parse_log_text(db: Session, text: str, *, meal_slot: str | None = None):
    """Parse one free-text log line into intents. Never raises, never drops input."""
    from drhiro_api.services.text_meal_parser import (
        _clean, _extract_amount, _extract_count, _extract_size, _food_vocabulary,
        _portion_grams, _resolve,
    )

    raw = "" if text is None else str(text).strip()
    if not raw:
        return ParsedLog(intents=[], meal_slot=None)

    working = _strip_lead_verbs(raw)
    slot = meal_slot or _slot(raw)

    intents: list[LogIntent] = []
    remaining_parts: list[str] = []

    # ---- 1. ACTIVITIES first: "walk 180 kcal" must not become a meal ----
    for part in re.split(r"\s*(?:,|;|\band\b)\s*", working):
        if not part.strip():
            continue
        am = _ACT_RE.search(part)
        if not am:
            remaining_parts.append(part.strip())
            continue
        name = ACTIVITY_WORDS.get(am.group(1).lower(), am.group(1).lower())
        mm = _MIN_RE.search(part)
        km = _KCAL_RE.search(part)
        kcal = _num(km.group(1)) if km else None
        if kcal is None:
            # "gym 400" -> the bare figure is the burn, not a duration.
            for bare in re.finditer(r"(\d+(?:[.,]\d+)?)", part):
                tail = part[bare.end():bare.end() + 8].lower()
                if re.match(r"\s*(?:min|mins|minutes)", tail):
                    continue
                kcal = _num(bare.group(1))
                break
        intents.append(LogIntent(
            kind="activity", display_name=name, kcal=kcal,
            duration_min=_num(mm.group(1)) if mm else None,
            needs_review=kcal is None, raw=part.strip(),
        ))

    # ---- 2. drinks and foods from what is left ----
    for part in remaining_parts:
        cat, low_cal, phrase = _find_drink(part)
        volume, rest, _unit = _find_volume(part)

        if cat is None:
            # _extract_amount returns (remaining_text, grams, volume_ml)
            no_amount, grams, vol2 = _extract_amount(part)
            if volume is None:
                volume = vol2
            work = _strip_slot(no_amount)
            work, qty, unit = _extract_count(work)
            work, size_factor = _extract_size(work)
            cleaned = _clean(work)
            if not cleaned and volume is None:
                continue
            try:
                vocab = _food_vocabulary(db)
            except Exception:
                vocab = []
            food, matched, canon_key = _resolve(db, cleaned, vocab)
            if grams is None and volume is None:
                try:
                    grams = _portion_grams(cleaned, food, qty, unit, size_factor,
                                           canon_key)
                except Exception:
                    grams = 100.0
            intents.append(LogIntent(
                kind="meal",
                display_name=matched or cleaned or "unknown",
                grams=float(grams) if grams is not None else None,
                volume_ml=volume,
                quantity=float(qty) if qty else 1.0,
                needs_review=food is None,
                food_catalog_item_id=(str(getattr(food, "id", "")) or None)
                if food is not None else None,
                raw=part.strip(),
            ))
            continue

        # A drink. Water / tea / black coffee (<=5 kcal) are LIQUID ONLY.
        # Everything else, including an unknown-calorie drink, reaches BOTH ledgers.
        intents.append(LogIntent(
            kind="liquid" if low_cal else "meal",
            display_name=phrase or _clean(rest) or "drink",
            volume_ml=volume,
            category=cat,
            is_drink=True,
            needs_review=volume is None or not low_cal,
            raw=part.strip(),
        ))

    return ParsedLog(intents=intents, meal_slot=slot)


# --------------------------------------------------------------------------- #
# identity: consumption_operations is THE identity table
# --------------------------------------------------------------------------- #
def _local_date(user, now: datetime):
    """The user's LOCAL calendar date.

    `activity_date` is read back by the daily summary, which works in whole LOCAL
    days. Writing it as `now.date()` (UTC) made an activity logged at 00:30 local
    land on the previous day, and the summary then reported zero burn.
    """
    try:
        tz = ZoneInfo(getattr(user, "timezone", None) or "UTC")
    except Exception:
        tz = timezone.utc
    return now.astimezone(tz).date()


def _payload_hash(parsed: "ParsedLog") -> str:
    """Stable hash of the PARSED intent (not the raw text).

    Re-sending the same message must hash identically; an edit that changes what
    is logged must not.
    """
    import hashlib
    import json as _json
    payload = {
        "slot": parsed.meal_slot,
        "intents": [
            {"k": i.kind, "n": (i.display_name or "").lower(), "g": i.grams,
             "v": i.volume_ml, "c": i.category, "d": i.is_drink, "kc": i.kcal,
             "m": i.duration_min}
            for i in parsed.intents
        ],
    }
    return hashlib.sha256(
        _json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


_CAPS_CACHE: dict = {}
_WARNED: set = set()


def schema_capabilities(db: Session) -> dict:
    """What this database can actually do. Cached per engine, never raises.

    A deployment of the parser-only behaviour (slices 1-3) must run against a
    database WITHOUT `consumption_operations` and WITHOUT the ledger `deleted_at`
    columns -- that is production at d5e6f7a8b9c0. This probe decides which
    behaviour is available instead of assuming the migration ran.
    """
    try:
        key = str(db.get_bind().engine.url)
    except Exception:
        key = "unknown"
    if key in _CAPS_CACHE:
        return _CAPS_CACHE[key]
    caps = {"identity_table": False, "ledger_deleted_at": False,
            "activities_table": False}
    try:
        caps["identity_table"] = db.execute(
            _sql("SELECT to_regclass('public.consumption_operations')")).scalar() is not None
        caps["activities_table"] = db.execute(
            _sql("SELECT to_regclass('public.activities')")).scalar() is not None
        n = db.execute(_sql(
            "SELECT count(*) FROM information_schema.columns"
            " WHERE column_name = 'deleted_at'"
            " AND table_name IN ('measurements', 'activities')")).scalar()
        caps["ledger_deleted_at"] = int(n or 0) >= 2
    except Exception:
        pass
    _CAPS_CACHE[key] = caps
    return caps


def _warn_once(key: str, message: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        import logging
        logging.getLogger(__name__).warning(message)


def _soft_delete_meal(db: Session, meal_id) -> None:
    from drhiro_api.models import Meal
    m = db.get(Meal, uuid.UUID(str(meal_id)))
    if m is not None and m.status != "deleted":
        m.status = "deleted"


def _soft_delete_measurement(db: Session, measurement_id) -> None:
    """No-op when the ledger has no soft-delete column (production today).

    The column is NEVER referenced unless the probe says it exists, so a
    parser-only deployment cannot emit `UPDATE ... SET deleted_at` against a
    schema that lacks it.
    """
    if not schema_capabilities(db)["ledger_deleted_at"]:
        _warn_once("nodeletedat",
                   "soft-delete unavailable: ledger deleted_at columns absent; "
                   "idempotency_disabled_missing_schema")
        return
    # Raw, guarded UPDATE: the column is not ORM-mapped, so a schema without it
    # is never touched.
    db.execute(_sql("UPDATE measurements SET deleted_at = now() WHERE id = :i"
                    " AND deleted_at IS NULL"), {"i": str(measurement_id)})


def _soft_delete_activity(db: Session, activity_id) -> None:
    if not schema_capabilities(db)["ledger_deleted_at"]:
        _warn_once("nodeletedat",
                   "soft-delete unavailable: ledger deleted_at columns absent; "
                   "idempotency_disabled_missing_schema")
        return
    db.execute(_sql("UPDATE activities SET deleted_at = now() WHERE id = :i"
                    " AND deleted_at IS NULL"), {"i": str(activity_id)})


def _merge_liquid_into_existing(db: Session, m: Measurement, intent: LogIntent) -> None:
    """Update a liquid row IN PLACE (H4) without changing its id."""
    m.value_json = {"amount_ml": intent.volume_ml, "category": intent.category,
                    "needs_review": bool(intent.volume_ml is None)}
    if schema_capabilities(db)["ledger_deleted_at"]:
        db.execute(_sql("UPDATE measurements SET deleted_at = NULL WHERE id = :i"),
                   {"i": str(m.id)})


def _resolve_intent_nutrition(db: Session, it: LogIntent, user_id: str) -> dict | None:
    """Resolve scaled nutrition for one parsed meal intent.

    Mirrors the confident branch of ``_lookup_nutrients`` in ``meals.py``:
    resolve via the ranked ``resolve_food`` match, scale per-100g values by
    grams, apply the Atwater kcal fallback when the food has no explicit
    energy nutrient, and return a normalized 6-key payload. Returns the
    unresolved payload (with ``unresolved=True``) when no match is found.
    """
    from drhiro_api.models import Food
    from drhiro_nutrition.catalog import NutrientTotals

    code_by_id = {n.id: n.nutrient_code for n in db.query(Nutrient).all()}
    res = resolve_food(db, it.display_name, limit=5, user_id=user_id)
    food = res.best if res else None

    if food is None:
        return {
            "kcal": None, "protein_g": None, "carbs_g": None,
            "fat_g": None, "fiber_g": None, "sodium_mg": None,
            "sources": [], "resolved_food": None, "unresolved": True,
        }

    nmap = nutrient_map(food, code_by_id)
    grams = it.grams or food.serving_grams or 100.0
    energy_per_100g = nmap.get("energy")
    if not energy_per_100g:
        energy_per_100g = (
            4.0 * (nmap.get("protein") or 0)
            + 4.0 * (nmap.get("carbs") or 0)
            + 9.0 * (nmap.get("fat") or 0)
        )
    totals = NutrientTotals(
        kcal=(energy_per_100g or 0) * grams / 100,
        protein_g=(nmap.get("protein") or 0) * grams / 100,
        carbs_g=(nmap.get("carbs") or 0) * grams / 100,
        fat_g=(nmap.get("fat") or 0) * grams / 100,
        fiber_g=(nmap.get("fiber") or 0) * grams / 100,
        sodium_mg=(nmap.get("sodium") or 0) * grams / 100,
        sources=["usda:fdc-v1"],
    )
    return {
        "kcal": totals.kcal,
        "protein_g": totals.protein_g,
        "carbs_g": totals.carbs_g,
        "fat_g": totals.fat_g,
        "fiber_g": totals.fiber_g,
        "sodium_mg": totals.sodium_mg,
        "sources": totals.sources,
        "resolved_food": food.display_name,
    }


def _write_ledgers(db: Session, user, parsed: "ParsedLog", now, existing,
                   source: str, order_id: str) -> dict:
    """Write/refresh all three ledgers. `existing` is the previous result_json."""
    from drhiro_api.models import Meal

    meal_intents = [i for i in parsed.intents if i.kind == "meal"]
    liquid_intents = [i for i in parsed.intents if i.kind in ("liquid",)]
    act_intents = [i for i in parsed.intents if i.kind == "activity"]
    for i in parsed.intents:                      # caloric drinks reach BOTH ledgers
        if i.is_drink and i.kind == "meal" and not any(i is x for x in liquid_intents):
            liquid_intents.append(i)

    prev = existing or {}
    prev_meal = prev.get("meal_id")
    prev_liquids = list(prev.get("liquid_ids") or [])
    prev_acts = list(prev.get("activity_ids") or [])

    result = {"meal_id": None, "item_ids": [], "liquid_ids": [],
              "activity_ids": [], "slot": parsed.meal_slot, "edited": bool(prev)}

    # ---- meal ----
    meal = None
    if meal_intents:
        if prev_meal:
            meal = db.get(Meal, uuid.UUID(str(prev_meal)))     # SAME parent id (G1/G2)
            if meal is not None:
                meal.meal_type = parsed.meal_slot or "snack"
                meal.status = "needs_review"
                meal.eaten_at = now
        if meal is None:
            meal = Meal(user_id=user.id, eaten_at=now,
                        meal_type=parsed.meal_slot or "snack",
                        status="needs_review", input_method="text",
                        notes=None, totals_json=None, confidence=1.0)
            db.add(meal)
        db.flush()
        # Delete the full beverage projection (BeverageMeasurement + Measurement)
        # for all items before bulk-deleting items so no stale hydration survives
        # and no orphaned link can be reused by copy logic.
        existing_item_ids = [mi.id for mi in db.query(MealItem).filter(MealItem.meal_id == meal.id).all()]
        _delete_beverage_projection_for_meal_items(db, str(user.id), existing_item_ids)
        # Replace the item set: an edit removes what the new text no longer implies.
        db.query(MealItem).filter(MealItem.meal_id == meal.id).delete()
        db.flush()
        result["meal_id"] = str(meal.id)
        meal_nutrients = []  # collect per-item nutrient dicts for totals
        for it in meal_intents:
            # Resolve nutrition for this intent (matches _lookup_nutrients)
            nutrients = _resolve_intent_nutrition(db, it, str(user.id))
            mi = MealItem(meal_id=meal.id,
                          food_catalog_item_id=it.food_catalog_item_id,
                          display_name=it.display_name,
                          quantity=it.quantity or 1.0, unit=None,
                          grams=it.grams,                 # volume never becomes grams
                          volume_ml=it.volume_ml,
                          beverage_category=it.category,
                          nutrients_json=nutrients, source="text", confidence=1.0)
            db.add(mi)
            db.flush()
            result["item_ids"].append(str(mi.id))
            it.__dict__["_meal_item_id"] = mi.id
            if nutrients and not nutrients.get("unresolved"):
                meal_nutrients.append(nutrients)
        # Compute and set meal.totals_json from resolved item nutrients
        if meal_nutrients:
            totals = {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0, "sodium_mg": 0.0, "estimated": False}
            for nj in meal_nutrients:
                for k in totals:
                    if k == "estimated":
                        continue
                    try:
                        totals[k] += float(nj.get(k) or 0)
                    except (TypeError, ValueError):
                        pass
            totals = {k: round(v, 2) if isinstance(v, float) else v for k, v in totals.items()}
            meal.totals_json = totals
    elif prev_meal:
        # The new text no longer implies a meal (G6: juice -> water).
        _soft_delete_meal(db, prev_meal)

    # ---- liquids (reuse ids in place where possible) ----
    for idx, it in enumerate(liquid_intents):
        reuse = prev_liquids[idx] if idx < len(prev_liquids) else None
        row = db.get(Measurement, uuid.UUID(str(reuse))) if reuse else None
        if row is not None:
            _merge_liquid_into_existing(db, row, it)
            m = row
        else:
            cat = it.category or "water"
            m = Measurement(
                user_id=user.id, metric_type="water", start_at=now, end_at=now,
                value_json={"amount_ml": it.volume_ml, "category": cat,
                            "needs_review": bool(it.volume_ml is None)},
                unit="ml", source_provider=source,
                # Identity-derived, NOT a per-request uuid (slice 4).
                source_record_id=f"{order_id}-{idx}-{cat}",
                recording_method="text", confidence=1.0)
            db.add(m)
        db.flush()
        result["liquid_ids"].append(str(m.id))
        mi_id = it.__dict__.get("_meal_item_id")
        if mi_id:
            # On an EDIT the measurement is reused, so its old link must be
            # replaced -- uq_bev_measurement is unique on measurement_id.
            db.query(BeverageMeasurement).filter(
                BeverageMeasurement.measurement_id == m.id).delete()
            db.flush()
            db.add(BeverageMeasurement(user_id=user.id, meal_item_id=mi_id,
                                       measurement_id=m.id))
            db.flush()
    for stale in prev_liquids[len(liquid_intents):]:
        _soft_delete_measurement(db, stale)

    # ---- activities ----
    for idx, it in enumerate(act_intents):
        reuse = prev_acts[idx] if idx < len(prev_acts) else None
        row = db.get(Activity, uuid.UUID(str(reuse))) if reuse else None
        if row is not None:
            row.title = it.display_name
            row.calories_burned = it.kcal if it.kcal is not None else 0.0
            row.activity_date = _local_date(user, now)
            a = row
        else:
            a = Activity(user_id=user.id, activity_date=_local_date(user, now),
                         title=it.display_name, description=None,
                         calories_burned=it.kcal if it.kcal is not None else 0.0)
            db.add(a)
        db.flush()
        result["activity_ids"].append(str(a.id))
    for stale in prev_acts[len(act_intents):]:
        _soft_delete_activity(db, stale)

    return result


def commit_intents(db: Session, user, parsed: "ParsedLog", *, eaten_at=None,
                   telegram_chat_id=None, telegram_message_id=None,
                   source: str = "telegram", raw_text: str | None = None) -> dict:
    """Idempotent, editable write of one log line into all three ledgers.

    Key = (source, source_chat_id, source_message_id) in `consumption_operations`.
      * miss                -> insert the operation, write the ledgers
      * hit, same payload   -> write NOTHING, return the SAME ids (retry)
      * hit, new payload    -> EDIT in place, same parent ids; ledgers the new
                               text no longer implies are soft-deleted
    Without telegram ids (manual UI) the operations table is skipped entirely.
    """
    import json as _json

    from drhiro_api.models import ConsumptionOperation

    now = eaten_at or datetime.now(timezone.utc)
    payload_hash = _payload_hash(parsed)
    caps = schema_capabilities(db)
    has_identity = bool(telegram_message_id and telegram_chat_id)
    if has_identity and not caps["identity_table"]:
        # Slices 1-3 behaviour: insert only. No identity table on this schema.
        _warn_once("noidtable",
                   "consumption_operations absent; idempotency_disabled_missing_schema")
        has_identity = False

    op = None
    existing = None
    if has_identity:
        op = (db.query(ConsumptionOperation)
              .filter(ConsumptionOperation.user_id == user.id,
                      ConsumptionOperation.source == source,
                      ConsumptionOperation.source_chat_id == str(telegram_chat_id),
                      ConsumptionOperation.source_message_id == str(telegram_message_id))
              .with_for_update()
              .first())
        if op is not None:
            if op.payload_hash == payload_hash:
                # F1: identical retry -> no writes at all, same ids.
                existing = _json.loads(op.result_json or "{}")
                existing["replayed"] = True
                db.rollback()
                return existing
            try:
                existing = _json.loads(op.result_json or "{}")
            except Exception:
                existing = None

    order_id = (f"{source}-{telegram_chat_id}-{telegram_message_id}"
                if has_identity else f"anon-{uuid.uuid4().hex}")

    result = _write_ledgers(db, user, parsed, now, existing, source, order_id)

    if has_identity:
        if op is None:
            db.add(ConsumptionOperation(
                user_id=user.id, source=source,
                source_chat_id=str(telegram_chat_id),
                source_message_id=str(telegram_message_id),
                # Stored as '' rather than NULL so uq_consumption_op_telegram
                # actually enforces the key (NULLs are distinct in PostgreSQL).
                source_bot_id="",
                idempotency_key=f"{telegram_chat_id}:{telegram_message_id}",
                raw_text=raw_text, result_json=_json.dumps(result),
                status="applied", payload_hash=payload_hash))
        else:
            op.raw_text = raw_text
            op.result_json = _json.dumps(result)
            op.status = "applied"
            op.payload_hash = payload_hash
        db.flush()

    result["payload_hash"] = payload_hash
    return result


# --------------------------------------------------------------------------- #
# corrections
# --------------------------------------------------------------------------- #
def correct_log(db: Session, user, *, telegram_message_id, telegram_chat_id, patch,
                source: str = "telegram") -> dict:
    """Patch the rows a previous message produced. Soft-delete only."""
    import json as _json
    from drhiro_api.models import ConsumptionOperation, Meal

    op = (db.query(ConsumptionOperation)
          .filter(ConsumptionOperation.user_id == user.id,
                  ConsumptionOperation.source == source,
                  ConsumptionOperation.source_chat_id == str(telegram_chat_id),
                  ConsumptionOperation.source_message_id == str(telegram_message_id))
          .with_for_update().first())
    if op is None:
        return {"ok": False, "reason": "no_such_message"}

    ids = _json.loads(op.result_json or "{}")
    changed = []

    slot = patch.get("slot") if patch else None
    if slot and ids.get("meal_id"):
        m = db.get(Meal, uuid.UUID(str(ids["meal_id"])))
        if m is not None:
            m.meal_type = slot
            changed.append(f"slot={slot}")

    amount = patch.get("amount_ml") if patch else None
    if amount is not None and ids.get("liquid_ids"):
        if len(ids["liquid_ids"]) != 1:
            return {"ok": False, "reason": "ambiguous_liquid",
                    "message": "More than one drink on that message; be specific."}
        m = db.get(Measurement, uuid.UUID(str(ids["liquid_ids"][0])))
        if m is not None:
            v = dict(m.value_json or {})
            v["amount_ml"] = float(amount)
            v["needs_review"] = False
            m.value_json = v
            changed.append(f"amount_ml={amount}")
            # Keep the meal-item echo in sync: the drink's own meal item carries
            # volume_ml too, and a correction that updated only the liquid left the
            # two ledgers disagreeing (meal item 300 vs liquid 200).
            link = (db.query(BeverageMeasurement)
                    .filter(BeverageMeasurement.measurement_id == m.id).first())
            if link is not None:
                mi = db.get(MealItem, link.meal_item_id)
                if mi is not None:
                    mi.volume_ml = float(amount)
                    changed.append("meal_item_volume_ml")

    drop = patch.get("drop_item") if patch else None
    if drop:
        kept, removed = [], []
        for item_id in ids.get("item_ids") or []:
            mi = db.get(MealItem, uuid.UUID(str(item_id)))
            if mi is not None and drop.lower() in (mi.display_name or "").lower():
                removed.append(mi)
            else:
                kept.append(item_id)
        # H5: more than one match is ambiguous -> change NOTHING.
        if len(removed) > 1:
            db.rollback()
            return {"ok": False, "reason": "ambiguous_target",
                    "message": f"More than one {drop!r}; be specific."}
        if removed:
            db.query(MealItem).filter(
                MealItem.id == removed[0].id).delete()
            # a drink that is dropped takes its liquid row with it
            for lid in ids.get("liquid_ids") or []:
                m = db.get(Measurement, uuid.UUID(str(lid)))
                if m is not None and drop.lower() in (
                        (m.value_json or {}).get("category", "")).lower():
                    _soft_delete_measurement(db, lid)
            ids["item_ids"] = kept
            changed.append(f"dropped={drop}")

    op.result_json = _json.dumps(ids)
    db.flush()
    return {"ok": True, "changed": changed, "ids": ids}


def delete_log(db: Session, user, *, telegram_message_id, telegram_chat_id,
               item_name=None, source: str = "telegram") -> dict:
    """Soft-delete what a message logged, or one named item within it."""
    import json as _json
    from drhiro_api.models import ConsumptionOperation

    op = (db.query(ConsumptionOperation)
          .filter(ConsumptionOperation.user_id == user.id,
                  ConsumptionOperation.source == source,
                  ConsumptionOperation.source_chat_id == str(telegram_chat_id),
                  ConsumptionOperation.source_message_id == str(telegram_message_id))
          .with_for_update().first())
    if op is None:
        return {"ok": False, "reason": "no_such_message"}

    ids = _json.loads(op.result_json or "{}")

    if not item_name:
        if ids.get("meal_id"):
            _soft_delete_meal(db, ids["meal_id"])
        for lid in ids.get("liquid_ids") or []:
            _soft_delete_measurement(db, lid)
        for aid in ids.get("activity_ids") or []:
            _soft_delete_activity(db, aid)
        op.status = "deleted"
        db.flush()
        return {"ok": True, "deleted": "message"}

    # Named item. A drink and the liquid it produced are ONE logical target, so a
    # meal item match takes its paired liquid with it; H5 ambiguity changes nothing.
    item_matches = []
    for item_id in ids.get("item_ids") or []:
        mi = db.get(MealItem, uuid.UUID(str(item_id)))
        if mi is not None and item_name.lower() in (mi.display_name or "").lower():
            item_matches.append(mi)

    if len(item_matches) > 1:
        db.rollback()
        return {"ok": False, "reason": "ambiguous_target", "count": len(item_matches),
                "message": f"More than one {item_name!r} on that message; be specific."}

    if len(item_matches) == 1:
        target = item_matches[0]
        # the liquid written for this same item, if any — resolve the pairing
        # through the authoritative BeverageMeasurement link (the new writer
        # records the meal-item relationship there, not on Measurement.meal_item_id).
        paired = None
        bev_link = (db.query(BeverageMeasurement)
                    .filter(BeverageMeasurement.meal_item_id == target.id)
                    .first())
        if bev_link is not None:
            paired = db.get(Measurement, bev_link.measurement_id)
        else:
            # Fallback: legacy writers set Measurement.meal_item_id directly.
            # Only consider measurements recorded by this operation.
            op_liquid_ids = set(str(l) for l in (ids.get("liquid_ids") or []))
            for lid in op_liquid_ids:
                row = db.get(Measurement, uuid.UUID(lid))
                if row is not None and (
                        str(getattr(row, "meal_item_id", "") or "")
                        == str(target.id)):
                    paired = row
                    break
        # Never guess by taking the first liquid id: if the pairing cannot be
        # resolved unambiguously, the beverage measurement is left untouched.
        kept = [i for i in (ids.get("item_ids") or []) if str(target.id) != str(i)]
        db.query(MealItem).filter(MealItem.id == target.id).delete()
        ids["item_ids"] = kept
        if paired is not None:
            _soft_delete_measurement(db, paired.id)
            ids["liquid_ids"] = [l for l in (ids.get("liquid_ids") or [])
                                 if str(paired.id) != str(l)]
        op.result_json = _json.dumps(ids)
        db.flush()
        return {"ok": True, "deleted": item_name, "with_liquid": bool(paired)}

    liquid_matches = []
    for lid in ids.get("liquid_ids") or []:
        row = db.get(Measurement, uuid.UUID(str(lid)))
        if row is not None and item_name.lower() in (
                (row.value_json or {}).get("category", "")).lower():
            liquid_matches.append(row)

    if len(liquid_matches) > 1:
        db.rollback()
        return {"ok": False, "reason": "ambiguous_target",
                "count": len(liquid_matches),
                "message": f"More than one {item_name!r} on that message; be specific."}
    if not liquid_matches:
        db.rollback()
        return {"ok": False, "reason": "not_found"}

    _soft_delete_measurement(db, liquid_matches[0].id)
    ids["liquid_ids"] = [l for l in (ids.get("liquid_ids") or [])
                         if str(liquid_matches[0].id) != str(l)]
    op.result_json = _json.dumps(ids)
    db.flush()
    return {"ok": True, "deleted": item_name}
