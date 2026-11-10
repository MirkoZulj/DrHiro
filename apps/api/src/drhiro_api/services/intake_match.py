"""Meal-item -> canonical food matcher (0.12.0 Part B, spec B2).

match_item_to_food: name similarity >= 0.85 (SequenceMatcher on normalised
names) and kcal per 100 g within ±25% when both sides know it.
"""
from __future__ import annotations
import re
import unicodedata
from difflib import SequenceMatcher

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()

def parenthetical(name: str) -> str | None:
    """Content inside trailing parentheses, e.g. 'sušena govedina (Beef, cured,
    dried)' -> 'Beef, cured, dried'. Non-English items carry the canonical
    English name in parentheses — match on that, not the foreign name."""
    m = re.search(r"\(([^()]+)\)\s*$", (name or "").strip())
    return m.group(1) if m else None

def base_name(display_name: str) -> str:
    """Name used for matching: strip the '(AI-estimated)' marker, then prefer
    the English parenthetical when present.
    'homemade pizza (AI-estimated)' -> 'homemade pizza'
    'sušena govedina (Beef, cured, dried)' -> 'Beef, cured, dried'
    """
    n = re.sub(r"\s*\([^()]*ai[-\s]estimated[^()]*\)\s*$", "",
               (display_name or "").strip(), flags=re.I)
    p = parenthetical(n)
    return p if p else n

def _tokens(s: str) -> set:
    return {t for t in _norm(s).split() if len(t) >= 3}

def contains(name_tokens: set, cand_name: str) -> bool:
    """Token-containment: every (len>=3) token of the short item name appears
    in the candidate name. Lets 'beer' match 'Beer, regular' and 'cashews'
    match 'Cashews, raw' where SequenceMatcher ratios stay < 0.85."""
    return bool(name_tokens) and name_tokens <= _tokens(cand_name)


# Filler words that carry no matching signal ('homemade pizza' -> 'pizza').
_GENERIC = {
    "homemade", "house", "plain", "fresh", "sweet", "light", "full",
    "extra", "aged", "young", "mixed", "whole", "regular", "strong",
    "roasted", "baked", "cooked", "freshly", "local",
}


def _fold_all(*token_sets):
    """Fold plural/prefixed variants to a shared stem.

    'cashews' and 'cashew' both fold to 'cashew' when they appear on the two
    sides of a comparison; 'glasses' and 'glass' fold to 'glass'. A token is
    folded to the shortest other token (>=4 chars) that is a prefix of it.
    """
    allt = set().union(*token_sets)
    folded = []
    for s in token_sets:
        out = set()
        for t in s:
            cands = [u for u in allt if u != t and 4 <= len(u) < len(t) and t.startswith(u)]
            out.add(min(cands, key=len) if cands else t)
        folded.append(out)
    return folded


def containment_tier(name_tokens, cand_tokens) -> bool:
    """Token-containment acceptance for short / generic names.

    Passes when:
    - content tokens (non-generic, 1-2 of them) are all present in the
      candidate tokens — 'homemade pizza' vs 'Pizza, cheese', 'beer' vs
      'Beer, regular'; or
    - symmetric subset, both sides <=3 tokens — word-order swaps like
      'Beef, cured, dried' vs 'Beef, dried, cured' (similarity 0.62).
    Plurals fold first: 'cashews' vs 'Nuts, cashew nuts, raw'.
    """
    nt_raw, ct_raw = _fold_all(set(name_tokens), set(cand_tokens))
    nt = nt_raw - _GENERIC
    ct = ct_raw - _GENERIC
    if 1 <= len(nt) <= 2 and nt <= ct:
        return True
    if (len(nt_raw) <= 3 and len(ct_raw) <= 3
            and (nt_raw <= ct_raw or ct_raw <= nt_raw)):
        return True
    return False

def _food_kcal_per100(db, food):
    from drhiro_api.models import FoodNutrient, Nutrient
    row = (db.query(FoodNutrient).join(Nutrient, Nutrient.id == FoodNutrient.nutrient_id)
           .filter(FoodNutrient.food_id == food.id, Nutrient.nutrient_code == "energy").first())
    return row.amount_per_100g if row else None

def _item_kcal_per100(item):
    n = item.nutrients_json or {}
    if isinstance(n, dict) and isinstance(n.get("kcal"), (int, float)):
        basis = item.grams or item.volume_ml
        if basis:
            return float(n["kcal"]) * 100.0 / basis
    return None

def _candidate_foods(db, display_name: str, limit: int = 1000):
    """Full-catalogue candidates by name token (no arbitrary first-N cap).

    The old matcher scanned ``Food.limit(200)`` — an arbitrary slice of a
    ~663k-row catalogue — so most foods were never even scored. Search the
    whole catalogue by the longest name token via the display_name index,
    falling back to shorter tokens only if the longest finds nothing.
    """
    from drhiro_api.models import Food
    from sqlalchemy import or_
    tokens = [t for t in _norm(base_name(display_name)).split() if len(t) >= 3]
    tokens.sort(key=len, reverse=True)
    # Query each longest token, plus its singular form when plural
    # ('cashews' -> also 'cashew') — the catalogue stores stems.
    queries: list[str] = []
    for t in tokens[:3]:
        queries.append(t)
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            queries.append(t[:-1])
    for tok in queries:
        rows = (db.query(Food)
                .filter(or_(Food.display_name.ilike(f"%{tok}%"),
                            Food.display_name_en.ilike(f"%{tok}%")))
                .limit(limit).all())
        if rows:
            return rows
    return []

def match_item_to_food(db, display_name: str, item=None, limit: int = 1000):
    """Return (food, confidence) or (None, score).

    Two tiers: (1) name similarity >= 0.85; (2) token-containment for short
    generic names ('beer' in 'Beer, regular'). Both kcal-gated ±25%.
    """
    foods = _candidate_foods(db, display_name, limit=limit)
    item_kcal = _item_kcal_per100(item) if item is not None else None
    mname = base_name(display_name)
    name_tokens = _tokens(mname)
    best, best_score = None, 0.0
    for f in foods:
        score = max(similarity(display_name, f.display_name),
                    similarity(mname, f.display_name))
        if score < 0.85 and not containment_tier(name_tokens, _tokens(f.display_name)):
            continue
        if item_kcal is not None:
            fk = _food_kcal_per100(db, f)
            if fk is not None and fk > 0:
                if abs(item_kcal - fk) / fk > 0.25:
                    continue
        if score > best_score:
            best, best_score = f, score
    return best, best_score

def run_matcher(db, days: int = 365, limit: int = 500) -> dict:
    """Backfill micro_food_id on confirmed meal items (365d window)."""
    from datetime import date, timedelta
    from drhiro_api.models import Meal, MealItem
    from drhiro_api.services.day_boundary import nutrition_date
    cutoff = date.today() - timedelta(days=days)
    matched = skipped = 0
    items = (db.query(MealItem).join(Meal, Meal.id == MealItem.meal_id)
             .filter(Meal.status == "confirmed", MealItem.micro_food_id.is_(None)).all())
    for it in items[:limit]:
        meal = db.get(Meal, it.meal_id)
        if meal is None:
            continue
        # nutrition_date needs user tz; approximate with meal's user via eaten_at date
        if meal.eaten_at.date() < cutoff:
            skipped += 1; continue
        food, score = match_item_to_food(db, it.display_name, item=it)
        if food is not None:
            it.micro_food_id = food.id
            it.micro_match_confidence = round(score, 4)
            matched += 1
    db.commit()
    return {"matched": matched, "skipped_old": skipped}
