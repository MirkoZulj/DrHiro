"""drHiro intelligent-meal service - the live Telegram meal writer.

Deployed as the `intelligent-meal` container on the VPS from /opt/apps/
intelligent-meal (this repo copy is the source of record, not the deploy
source).

REQUIRED ENVIRONMENT (no defaults are baked in - credentials belong in the
environment, never in source):
  DATABASE_URL, REDIS_URL, DRHIRO_JWT_SECRET, DRHIRO_TELEGRAM_ID,
  USDA_API_KEY, PI_HOST, PI_PASS, DDG_HTTP_URL,
  INTELLIGENT_MEAL_URL, DRHIRO_API_URL, DRHIRO_SERVICE_TOKEN

PI_HOST / PI_PASS are consumed by the Pi ingest helper; DDG_HTTP_URL by the
DuckDuckGo fallback. If a feature is unused, leaving its variable unset is
harmless; leaving a real credential in source is not.
"""
# Standalone Intelligent Meal Service
# Runs on port 8090, handles intelligent meal logging directly.
# Features:
#   - Natural language meal parsing
#   - Fuzzy-ranked DB food search (closest matches, scored)
#   - Camoufox fallback: 0 DB matches -> google "<food> nutritional information"
#   - Recipe builder: multi-ingredient dish -> total weight + nutrition -> scale by eaten
import os
import re
import json
import uuid
import time
import difflib
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
import redis
import jwt as pyjwt
from fastapi import FastAPI, Depends, HTTPException, Header, Query, Request, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text as sa_text
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker, Session

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("intelligent-meal")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_URL = os.environ.get("DATABASE_URL", "")
REDIS_URL = os.environ.get("REDIS_URL", "")
JWT_SECRET = os.environ.get("DRHIRO_JWT_SECRET", "")
SERVICE_TOKEN = os.environ.get("DRHIRO_SERVICE_TOKEN", "")
TELEGRAM_ID = os.environ.get("DRHIRO_TELEGRAM_ID", "")

# Camoufox / Pi SSH config
PI_HOST = os.environ.get("PI_HOST", "")
PI_USER = os.environ.get("PI_USER", "mirko")
PI_PASS = os.environ.get("PI_PASS", "")
CAMOUFOX_SCRIPT = "/home/mirko/camoufox_nutrition.py"

# ---------------------------------------------------------------------------
# DB + Redis
# ---------------------------------------------------------------------------
engine = create_engine(DB_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(title="Intelligent Meal Service")

bearer = HTTPBearer(auto_error=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    db: Session = Depends(get_db),
):
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        payload = pyjwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token subject")
    user = db.execute(text("SELECT * FROM users WHERE id = :id AND status = 'active'"), {"id": user_id}).fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return {"id": str(user.id), "telegram_id": TELEGRAM_ID}


def get_user_from_service_or_bearer(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    x_service_token: Optional[str] = Header(None, alias="x-service-token"),
    x_telegram_id: Optional[str] = Header(None, alias="x-telegram-id"),
    db: Session = Depends(get_db),
):
    """Accept either a user JWT bearer token OR a service token + telegram_id header."""
    # Try bearer token first
    if credentials is not None:
        try:
            payload = pyjwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
            user_id = payload.get("sub")
            if user_id:
                user = db.execute(text("SELECT * FROM users WHERE id = :id AND status = 'active'"), {"id": user_id}).fetchone()
                if user:
                    return {"id": str(user.id), "telegram_id": x_telegram_id or TELEGRAM_ID}
        except Exception:
            pass

    # Try service token
    if x_service_token and x_service_token == SERVICE_TOKEN and x_telegram_id:
        # Look up user by telegram_id
        user = db.execute(
            text("SELECT u.* FROM users u JOIN external_identities ei ON u.id = ei.user_id WHERE ei.provider = 'telegram' AND ei.provider_subject = :tid AND u.status = 'active'"),
            {"tid": x_telegram_id}
        ).fetchone()
        if user:
            return {"id": str(user.id), "telegram_id": x_telegram_id}

    raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class MealItemIn(BaseModel):
    display_name: str
    grams: Optional[float] = None


class MealIn(BaseModel):
    text: str
    meal_type: Optional[str] = None
    eaten_at: Optional[str] = None
    match_all: Optional[bool] = None


# Minimum confidence for AUTO-confirmed food matches. Below this the
# item is logged as 'unmatched' (0-kcal placeholder) instead of silently
# writing a wrong food (e.g. 'Fat, chicken' for 'corn bread').
MIN_CONFIDENCE = 0.45


class ConfirmMealRequest(BaseModel):
    draft_id: str
    selections: list[int] = []


class SetWeightRequest(BaseModel):
    text: str
    grams: float




class LearnFoodRequest(BaseModel):
    display_name: str
    kcal_per_100g: float | None = None
    protein_g_per_100g: float | None = None
    carbs_g_per_100g: float | None = None
    fat_g_per_100g: float | None = None
    fiber_g_per_100g: float | None = None
    sodium_mg_per_100g: float | None = None
class RecipeCreateRequest(BaseModel):
    name: str
    text: str  # ingredient list, e.g. "650g of beef, 400g of chickpeas, 200g of peas"


class RecipeLogRequest(BaseModel):
    grams_eaten: float
    meal_type: Optional[str] = None
    eaten_at: Optional[str] = None


class FoodCandidate(BaseModel):
    display_name: str
    kcal_per_100g: Optional[float] = None
    protein_g_per_100g: Optional[float] = None
    carbs_g_per_100g: Optional[float] = None
    fat_g_per_100g: Optional[float] = None
    fiber_g_per_100g: Optional[float] = None
    sodium_mg_per_100g: Optional[float] = None
    source: str = "unknown"
    confidence: float = 0.5


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
WORD_NUM = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
CONTAINER_GRAMS = {
    "glass": 250, "cup": 240, "bowl": 300, "mug": 300, "can": 330,
    "bottle": 500, "serving": 150, "scoop": 30, "handful": 40, "slice": 28,
}
ADJ = {"large": 0, "small": 0, "big": 0, "medium": 0, "fresh": 0, "whole": 0, "plain": 0}
# Approx grams for whole produce when counted by item
ITEM_GRAMS = {
    "carrot": 75, "carrots": 75, "onion": 110, "onions": 110, "egg": 50, "eggs": 50,
    "tomato": 120, "tomatoes": 120, "apple": 180, "apples": 180, "potato": 170, "potatoes": 170,
    "pepper": 120, "peppers": 120, "banana": 120, "bananas": 120, "orange": 130, "oranges": 130,
    "corn": 90, "cob": 90, "wine": 150, "beer": 330, "steak": 250,
}

# Whole/multi-word foods that come as a unit and need a real per-item weight.
# Keyed on the normalized (leading-adjective-stripped) phrase.
WHOLE_ITEM_GRAMS = {
    "corn cob": 150, "corncob": 150, "corn on cob": 150, "corn on the cob": 150,
    "ice cream cone": 100, "icecream cone": 100,
    "chicken thigh": 150, "chicken thighs": 150, "chicken leg": 150, "chicken legs": 150,
    "chicken breast": 170, "chicken breast half": 170, "chicken wing": 45, "chicken wings": 45,
    "potato": 170, "pepper": 120, "bell pepper": 150, "red pepper": 150, "green pepper": 150,
}




_DATE_DAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_STOPWORDS = {
    "the", "a", "an", "and", "of", "to", "with", "in", "on", "for", "at",
    "raw", "fresh", "cooked", "whole", "large", "small", "medium", "big",
    "piece", "pieces", "portion", "serving", "sliced", "diced",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)


def _match_item(items, frag):
    """Find the meal item a user fragment refers to, tolerantly.

    A user says 'red bell pepper' but the DB stores 'Peppers, bell, red, raw'.
    Naive substring ('frag in name or name in frag') misses that. Strategy:
      1. substring match on the lowercased display_name (incl. reversed),
      2. else token-overlap score (shared meaningful tokens), best wins,
         requiring at least one strong shared token.
    Returns the matched row or None."""
    if not frag:
        return None
    frag_l = frag.lower()
    for it in items:
        dn = (it[1] or "").lower()
        if frag_l and (frag_l in dn or dn in frag_l):
            return it
    frag_tok = {t for t in _TOKEN_RE.findall(frag_l) if t not in _STOPWORDS}
    if not frag_tok:
        return None
    best = None
    best_score = 0
    for it in items:
        dn = (it[1] or "").lower()
        dn_tok = {t for t in _TOKEN_RE.findall(dn) if t not in _STOPWORDS}
        if not dn_tok:
            continue
        shared = frag_tok & dn_tok
        if not shared:
            continue
        score = len(shared) / max(1, len(frag_tok))   # recall on the fragment
        if score > best_score:
            best = it
            best_score = score
    if best and best_score >= 0.5:
        return best
    return None


def resolve_eaten_date(text: str, now):
    t = text.lower()
    if re.search(r"\btoday\b", t):
        return now.date().isoformat()
    if re.search(r"\byesterday\b", t):
        return (now - timedelta(days=1)).date().isoformat()
    m = re.search(r"\b(?:on\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t)
    if m:
        target_wd = _DATE_DAYS[m.group(1)]
        days_ago = (now.weekday() - target_wd) % 7
        if days_ago == 0:
            days_ago = 7
        return (now - timedelta(days=days_ago)).date().isoformat()
    if re.search(r"\blast\s+night\b", t):
        return (now - timedelta(days=1)).date().isoformat()
    return None


def _norm_day_prefix(text: str) -> str:
    # Strip a leading bracketed timestamp, e.g. "[Fri 2026-09-11 19:46 UTC] ",
    # which the MCP layer prepends. Requiring a date inside the brackets keeps
    # this from eating ordinary bracketed words. Without this the quantity
    # regex never matches the leading "300ml", the item arrives with
    # grams=None, and confirm_meal's `or 100.0` default stores a flat 100 g.
    text = re.sub(r'^\s*\[[^\]]*\d{4}-\d{2}-\d{2}[^\]]*\]\s*', '', text)
    text = re.sub(r'^(?:on\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|last\s+\w+day|today|yesterday)\s+(?:for\s+)?(?:breakfast|lunch|dinner|snack)\s+(?:i\s+)?(?:had|ate|had\s+and\s+ate)\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^(?:on\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|last\s+\w+day|today|yesterday)\s+(?:for\s+)?(?:breakfast|lunch|dinner|snack)\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^(?:for\s+)?(?:breakfast|lunch|dinner|snack)\s+(?:i\s+)?(?:had|ate)\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^(?:for\s+)?(?:breakfast|lunch|dinner|snack)\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^(?:i\s+)?(?:had|ate)\s+(?:for\s+)?(?:breakfast|lunch|dinner|snack)\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^(?:i\s+)?(?:had|ate)\s*', '', text, flags=re.IGNORECASE)
    return text


def _extract_meal_type(text: str) -> Optional[str]:
    """Pull the meal type from a phrase like 'for dinner I had ...' or 'breakfast'."""
    m = re.search(r'\b(breakfast|lunch|dinner|snack)\b', text, re.IGNORECASE)
    return m.group(1).lower() if m else None


async def parse_meal_text(text: str) -> list[dict]:
    """Parse natural language meal text into structured items."""
    text = _norm_day_prefix(text)
    items = []
    parts = re.split(r',|\band\b', text)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # ---- Numeric: "500 g of steak", "200 g of lettuce"
        mnum = re.match(
            r'(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>g|grams?|kg|ml|dcl|dl|l|liters?|tbsp|tsp|tablespoons?|teaspoons?|slices?|pieces?|of)?\s+(?:of\s+)?(?P<food>.+)',
            part, re.IGNORECASE
        )
        if mnum:
            qty = float(mnum.group("qty"))
            unit = (mnum.group("unit") or "").lower().strip()
            food = mnum.group("food").strip()
            gram_units = {"g", "gram", "grams", "kg", "ml", "dcl", "dl", "l", "liter", "liters"}
            count_units = {"slice", "slices", "piece", "pieces", "tbsp", "tsp", "tablespoon", "tablespoons", "teaspoon", "teaspoons"}
            if unit in gram_units:
                if unit == "kg":
                    grams = qty * 1000
                elif unit in ("dcl", "dl"):
                    grams = qty * 100
                elif unit in ("l", "liter", "liters"):
                    grams = qty * 1000
                elif unit == "ml":
                    grams = qty
                else:
                    grams = qty
            elif unit in count_units:
                gram_per_unit = {
                    "slice": 28, "slices": 28, "piece": 5, "pieces": 5,
                    "tbsp": 15, "tablespoon": 15, "tablespoons": 15,
                    "tsp": 5, "teaspoon": 5, "teaspoons": 5,
                    "egg": 50, "eggs": 50,
                }
                grams = qty * gram_per_unit.get(unit, 10)
            else:
                # unitless count: '2 boiled eggs', '1 large pepper', '1 glass wine'
                # strip leading adjectives/containers so base noun is the food
                adj_m = re.match(r"^(large|small|big|medium|fresh|whole|plain|glass|cup|bottle)\s+(.+)$", food, re.IGNORECASE)
                if adj_m:
                    food = adj_m.group(2).strip()
                # Normalize a TRAILING adjective: 'pepper large' -> 'pepper'
                food_l = food.lower().rstrip("s")
                for t_adj in ("large", "small", "big", "medium", "fresh", "whole", "plain"):
                    if food_l.endswith(" " + t_adj):
                        food = food[: -(len(t_adj) + 1)].strip()
                        food_l = food.lower()
                        break
                # Whole/multi-word food first, else single item.
                whole_g = WHOLE_ITEM_GRAMS.get(food.lower()) or WHOLE_ITEM_GRAMS.get(food_l)
                w = whole_g or ITEM_GRAMS.get(food_l or food.lower()) or 0
                if w:
                    grams = qty * w
                elif "wine" in food_l or food_l=="wine":
                    grams = qty * 150
                elif "beer" in food_l or food_l=="beer":
                    grams = qty * 330
                else:
                    grams = qty
            items.append({"display_name": food, "grams": grams})
            continue

        # ---- Container: "a glass of wine", "a cup of coffee"
        mcont = re.match(
            r'(?P<num>a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s+'
            r'(?:(?P<adj>large|small|big|medium|fresh|whole|plain)\s+)?'
            r'(?P<cont>glass|cup|bowl|mug|can|bottle|serving|scoop|handful|slice|piece)s?\s+'
            r'(?:of\s+)?(?P<food>.+)',
            part, re.IGNORECASE
        )
        if mcont:
            cnt = WORD_NUM[mcont.group("num").lower()]
            cont = mcont.group("cont").lower()
            food = mcont.group("food").strip()
            grams = cnt * CONTAINER_GRAMS.get(cont, 100)
            items.append({"display_name": food, "grams": grams})
            continue

        # ---- Word count: "two eggs", "one large pepper"
        mword = re.match(
            r'(?P<num>a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s+'
            r'(?:(?P<adj>large|small|big|medium|fresh|whole|plain)\s+)?'
            r'(?P<food>.+)',
            part, re.IGNORECASE
        )
        if mword and mword.group("num").lower() in WORD_NUM:
            cnt = WORD_NUM[mword.group("num").lower()]
            food = mword.group("food").strip()
            # Strip leading adjectives so 'large pepper' -> 'pepper', 'glass wine' -> 'wine'
            adj_m = re.match(r"^(large|small|big|medium|fresh|whole|plain|glass|cup|bottle)\s+(.+)$", food, re.IGNORECASE)
            if adj_m:
                food = adj_m.group(2).strip()
            # Normalize a TRAILING adjective: 'pepper large' -> 'pepper', 'corn big' -> 'corn'
            food_l = food.lower().rstrip("s")
            for t_adj in ("large", "small", "big", "medium", "fresh", "whole", "plain"):
                if food_l.endswith(" " + t_adj):
                    food = food[: -(len(t_adj) + 1)].strip()
                    food_l = food.lower()
                    break
            # Prefer a whole-item weight (multi-word foods) first, else single item.
            whole_g = WHOLE_ITEM_GRAMS.get(food.lower()) or WHOLE_ITEM_GRAMS.get(food_l)
            if whole_g:
                grams = cnt * whole_g
            else:
                grams = cnt * ITEM_GRAMS.get(food_l or food.lower(), 100)
            items.append({"display_name": food, "grams": grams})
            continue

        # ---- Bare adj+food
        mbare = re.match(
            r'(?P<adj>large|small|big|medium|fresh|whole|plain|a|an)\s+(?P<food>.+)',
            part, re.IGNORECASE
        )
        if mbare and mbare.group("adj").lower() in ADJ:
            items.append({"display_name": mbare.group("food").strip(), "grams": None})
            continue

        items.append({"display_name": part, "grams": None})

    return items


# ---------------------------------------------------------------------------
# Food search (normalized schema) with fuzzy ranking
# ---------------------------------------------------------------------------
SEARCH_SQL = """
    SELECT
        f.id::text AS food_id,
        f.display_name,
        MAX(CASE WHEN n.nutrient_code = 'energy' THEN fn.amount_per_100g END) as kcal_per_100g,
        MAX(CASE WHEN n.nutrient_code = 'protein' THEN fn.amount_per_100g END) as protein_g_per_100g,
        MAX(CASE WHEN n.nutrient_code = 'carbs' THEN fn.amount_per_100g END) as carbs_g_per_100g,
        MAX(CASE WHEN n.nutrient_code = 'fat' THEN fn.amount_per_100g END) as fat_g_per_100g,
        MAX(CASE WHEN n.nutrient_code = 'fiber' THEN fn.amount_per_100g END) as fiber_g_per_100g,
        MAX(CASE WHEN n.nutrient_code = 'sodium' THEN fn.amount_per_100g END) as sodium_mg_per_100g,
        ds.source_label as source
    FROM foods f
    JOIN data_sources ds ON f.data_source_id = ds.id
    LEFT JOIN food_nutrients fn ON f.id = fn.food_id
    LEFT JOIN nutrients n ON fn.nutrient_id = n.id
    WHERE {where_clause}
    GROUP BY f.id, f.display_name, ds.source_label
    ORDER BY {order_clause}
    LIMIT :limit
"""


def _rank_candidates(rows: list[dict], query: str) -> list[dict]:
    """Score DB candidates by token overlap + substring similarity to find the closest matches."""
    q = query.strip().lower()
    q_words = set(re.findall(r"[a-z0-9]+", q))
    # words/substrings that indicate a chain restaurant or composite food,
    # not the plain ingredient (substring match, so "applebee's" -> "applebee" hits)
    NOISE = ("sauce", "fries", "gravy", " sub ", "sandwich", "burger", "dressing", "spread",
             "cracker", "denny", "subway", "applebee", "banquet", "cooked", "frozen",
             "taco bell", "taco", "restaurant", "mcdonald", "kfc", "wendy", "pizza",
             "chipotle", "starbucks", "panera", "chili", "family style", "house",
             "golden corral", "olive garden", "chili's", "outback", "tgif", "arbys",
             "panda express", "burger king", "dairy queen", "little caesars")
    ranked = []
    for r in rows:
        name = r.get("display_name", "")
        name_l = name.lower()
        name_words = set(re.findall(r"[a-z0-9]+", name_l))
        noise_pen = 0.35 if any(n in f" {name_l} " for n in NOISE) else 0.0
        # token overlap: how much of the QUERY is present
        # light stemming so "greens"~"green", "eggs"~"egg"
        def _stem(w):
            # light stem: strip trailing 's' (eggs->egg).  Diacritics folded
            # separately.  Plural 'cevapi' is handled by substring overlap below.
            return w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w
        def _fold(w):
            # diacritic-fold so č= c, ž= z, š= s, đ= dj, ć= c
            return w.translate(str.maketrans("čžšđć", "czsdc"))
        # Fold diacritics BEFORE tokenising (the [a-z0-9] class would otherwise
        # drop non-ASCII letters like 'č' in 'čevap').
        q_stem = {_fold(_stem(w)) for w in re.findall(r"[a-z0-9]+", _fold(q))}
        n_stem = {_fold(_stem(w)) for w in re.findall(r"[a-z0-9]+", _fold(name_l))}
        matched = q_stem & n_stem
        # substring overlap: a stored token that is a substring/prefix of a
        # query token (e.g. "cevap" vs "cevapi") counts as a match too.
        if not matched:
            for qs in q_stem:
                for ns in n_stem:
                    if len(qs) >= 3 and len(ns) >= 3 and (qs.startswith(ns) or ns.startswith(qs)):
                        matched.add(qs)
                        break
        if q_stem:
            q_coverage = len(matched) / len(q_stem)
        else:
            q_coverage = 0.0
        # MISMATCH PENALTY: salient candidate words the user never mentioned
        # ("Vinegar" in "Vinegar, red wine" when the query was "wine";
        # "feet" in "Chicken, feet" for "boiled eggs"; "peas" for "salad").
        _GENERIC = {"raw", "cooked", "fresh", "whole", "plain", "green", "red",
                    "white", "black", "yellow", "large", "small", "boiled",
                    "baked", "fried", "grilled", "with", "and", "ns", "as",
                    "purchased", "prepared", "from", "refuse", "yield", "meat",
                    "grade", "liquid", "frozen", "canned", "dried", "pasteurized",
                    "babyfood", "junior", "strained", "chunks", "slices", "pieces",
                    "mature", "regular", "sweetened", "unsweetened", "added",
                    "vitamin", "fortified", "enriched", "instant", "ready", "eat",
                    "serve", "mix", "pack", "package", "container", "average",
                    "boneless", "bone", "separable", "lean", "fat", "trimmed",
                    "choice", "select", "prime", "loin", "imported", "grass",
                    "fed", "roast", "lip", "au", "grades"}
        unmatched_salient = {
            w for w in n_stem - q_stem - _GENERIC
            if len(w) > 2 and not any(w in qs or qs in w for qs in q_stem if len(qs) > 2)
        }
        mismatch_pen = min(0.36, 0.12 * len(unmatched_salient))
        # Head-noun bonus: a comma-segment of the candidate name that exactly
        # equals a query token is the food's head noun ("wine" in
        # "Alcoholic beverage, wine, table, red"). Strong positive signal.
        head_bonus = 0.0
        _q_nouns = [w for w in reversed(list(q_stem)) if w not in _GENERIC]
        _q_noun = _q_nouns[0] if _q_nouns else None
        if _q_noun:
            segs = [set(re.findall(r"[a-z0-9]+", s)) for s in re.split(r",", name_l)]
            _CATEGORY = _GENERIC | {"alcoholic", "beverage", "beverages", "food",
                                     "foods", "dish", "drink", "drinks", "product"}
            segs_stem = [
                {w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w
                 for w in sw}
                for sw in segs if sw
            ]
            if segs_stem:
                first = segs_stem[0]
                first_is_category = first <= _CATEGORY
                # exact pure-noun segment match, first segment preferred
                if first == {_q_noun}:
                    head_bonus = 0.45
                elif _q_noun in first and first - {_q_noun} <= _GENERIC:
                    head_bonus = 0.40
                elif first_is_category and any(_q_noun in sw for sw in segs_stem[1:]):
                    head_bonus = 0.45
                elif _q_noun in first:
                    head_bonus = 0.30
                # STRONG whole-phrase bonus: EVERY salient query token (e.g. both
                # 'chicken' AND 'thigh') appears in the candidate's name, and the
                # candidate's head is not a generic category/food. This promotes
                # "Chicken, thigh, ..." above "Fat, chicken" / "Babyfood, chicken"
                # when the user named an explicit cut (thigh/breast/wing/leg).
                salient_q = {w for w in q_stem if w not in _GENERIC}
                if len(salient_q) >= 2 and not first_is_category:
                    all_present = all(
                        any(w in sw for sw in segs_stem) for w in salient_q
                    )
                    if all_present:
                        head_bonus = max(head_bonus, 0.60)
                # different primary food noun (e.g. 'Vinegar' for 'wine') -> cap
                if first != {_q_noun} and not first_is_category and (first - _CATEGORY) and head_bonus <= 0.30:
                    head_bonus -= 0.90
        # Derive kcal from macros (Atwater: P*4 + C*4 + F*9) when energy is
        # missing — many curated rows have macros but no energy value.
        rr = dict(r)
        if rr.get("kcal_per_100g") is None:
            p, c, f = rr.get("protein_g_per_100g"), rr.get("carbs_g_per_100g"), rr.get("fat_g_per_100g")
            if p is not None and f is not None:
                rr["kcal_per_100g"] = round((p or 0) * 4 + (c or 0) * 4 + (f or 0) * 9, 1)
        # penalty proportional to extra words in the food name (composite foods)
        extra_words = len(name_words - q_words)
        # substring / sequence similarity
        seq = difflib.SequenceMatcher(None, q, name_l).ratio()
        # whole-word bonus: the exact query appears as a standalone token
        whole_word = 0.25 if any(re.search(r"\b" + re.escape(w) + r"s?\b", name_l) for w in q_words) else 0.0
        # brevity: prefer plain/short names
        brevity = 0.10 if extra_words == 0 else max(0.0, 0.08 - 0.02 * extra_words)
        score = (0.5 * q_coverage + 0.25 * seq + whole_word + brevity + head_bonus - noise_pen - 0.03 * extra_words - mismatch_pen)
        # Penalize rows missing energy data — they would log as 0 kcal.
        if rr.get("kcal_per_100g") is None:
            score -= 0.45
        rr["confidence"] = round(max(min(score, 1.0), -1.0), 3)
        ranked.append((score, rr))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [rr for _, rr in ranked]




# ---------------------------------------------------------------------------
# Catalog learning (2026-08-30): persist google/user-provided foods for future
# matches. Google results were previously used once and discarded; user-provided
# nutrition went nowhere. Both now upsert into `foods` under the
# 'drHiro Private Catalog' data source so the local DB is searched FIRST.
# ---------------------------------------------------------------------------
_PRIVATE_CACHE = None


def _private_source_id(db) -> str:
    """data_sources.id for 'drHiro Private Catalog' (created once)."""
    global _PRIVATE_CACHE
    if _PRIVATE_CACHE:
        return _PRIVATE_CACHE
    row = db.execute(
        text("SELECT id FROM data_sources WHERE source_label = 'drHiro Private Catalog'")
    ).fetchone()
    if row:
        _PRIVATE_CACHE = str(row[0])
        return _PRIVATE_CACHE
    ds_id = str(uuid.uuid4())
    db.execute(
        text("INSERT INTO data_sources (id, source_label) VALUES (:id, :label) "
             "ON CONFLICT (source_label) DO NOTHING"),
        {"id": ds_id, "label": "drHiro Private Catalog"},
    )
    db.commit()
    row = db.execute(
        text("SELECT id FROM data_sources WHERE source_label = 'drHiro Private Catalog'")
    ).fetchone()
    _PRIVATE_CACHE = str(row[0])
    return _PRIVATE_CACHE


_NUTRIENT_IDS = None


def _nutrient_ids(db) -> dict:
    """nutrient_code -> nutrients.id, cached."""
    global _NUTRIENT_IDS
    if _NUTRIENT_IDS:
        return _NUTRIENT_IDS
    rows = db.execute(text("SELECT nutrient_code, id FROM nutrients")).fetchall()
    _NUTRIENT_IDS = {r[0]: str(r[1]) for r in rows}
    return _NUTRIENT_IDS


def catalog_learn(db, display_name: str, per100g: dict, external_id: str | None = None) -> str:
    """Upsert a food + its per-100g nutrients into the private catalog.

    per100g keys: kcal, protein_g, carbs_g, fat_g, fiber_g, sodium_mg (any subset).
    Returns the food id. Safe to call repeatedly (idempotent per external_id).
    """
    if not display_name:
        return ""
    ds_id = _private_source_id(db)
    ext = external_id or ("user:" + re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")[:80])

    # Upsert food row
    db.execute(
        text("""
            INSERT INTO foods (id, data_source_id, external_id, display_name, is_generic, is_liquid, created_at, updated_at)
            VALUES (:id, :ds, :ext, :name, true, false, NOW(), NOW())
            ON CONFLICT (data_source_id, external_id)
            DO UPDATE SET display_name = EXCLUDED.display_name, updated_at = NOW()
        """),
        {"id": str(uuid.uuid4()), "ds": ds_id, "ext": ext, "name": display_name[:255]},
    )
    db.commit()
    row = db.execute(
        text("SELECT id FROM foods WHERE data_source_id = :ds AND external_id = :ext"),
        {"ds": ds_id, "ext": ext},
    ).fetchone()
    food_id = str(row[0])

    # Upsert nutrients
    code_map = {
        "kcal": ("energy", per100g.get("kcal")),
        "protein_g": ("protein", per100g.get("protein_g")),
        "carbs_g": ("carbs", per100g.get("carbs_g")),
        "fat_g": ("fat", per100g.get("fat_g")),
        "fiber_g": ("fiber", per100g.get("fiber_g")),
        "sodium_mg": ("sodium", per100g.get("sodium_mg")),
    }
    nids = _nutrient_ids(db)
    for key, (code, val) in code_map.items():
        if val is None:
            continue
        nid = nids.get(code)
        if not nid:
            continue
        db.execute(
            text("""
                INSERT INTO food_nutrients (id, food_id, nutrient_id, amount_per_100g)
                VALUES (:id, :fid, :nid, :amt)
                ON CONFLICT (food_id, nutrient_id) DO UPDATE
                SET amount_per_100g = EXCLUDED.amount_per_100g
            """),
            {"id": str(uuid.uuid4()), "fid": food_id, "nid": nid, "amt": float(val)},
        )
    db.commit()
    log.info(f"[catalog-learn] stored '{display_name}' (ext={ext}) with per100g={per100g}")
    return food_id

def _needs_selection(candidates: list[dict], conf_gap: float = 0.08) -> bool:
    """Ambiguity check: ask the user to pick when candidates are close in confidence.

    Single candidate -> no selection needed (auto-confirm top pick).
    Multiple candidates whose top-two confidence differ by more than `conf_gap`
    -> the top pick is clearly best, no need to bother the user.
    Multiple candidates within `conf_gap` of each other -> ambiguous, offer choice.
    """
    if len(candidates) <= 1:
        return False
    confs = sorted([(c.get("confidence") or 0.0) for c in candidates], reverse=True)
    top, second = confs[0], confs[1]
    # If top is clearly dominant, don't nag; otherwise offer selection.
    return (top - second) <= conf_gap


async def search_food(db: Session, query: str, limit: int = 8) -> list[dict]:
    """Search food database for closest-matching candidates using fuzzy ranking."""
    q = query.strip().lower()
    if not q:
        return []

    # Broad candidate pool: any word overlap, ordered by match density
    words = re.findall(r"[a-z0-9]+", q)
    candidates = []

    # 1. Exact
    if q:
        rows = db.execute(
            text(SEARCH_SQL.format(where_clause="LOWER(f.display_name) = :q", order_clause="f.display_name")),
            {"q": q, "limit": limit},
        ).fetchall()
        candidates.extend([dict(r._mapping) for r in rows])

    # 2. Contains whole phrase
    if not candidates and q:
        rows = db.execute(
            text(SEARCH_SQL.format(where_clause="LOWER(f.display_name) LIKE :q", order_clause="length(f.display_name)")),
            {"q": f"%{q}%", "limit": 30},
        ).fetchall()
        candidates.extend([dict(r._mapping) for r in rows])

    # 2b. Reverse-substring with DIACRITIC FOLDING: match when a stored name is
    #     contained in the query after normalising Croatian diacritics.
    #     Handles plural/derived forms and un-accented typing, e.g. stored
    #     "čevap" vs typed "cevapi", or "čaj" vs "caj".  The query is folded the
    #     same way so "cevapi" folds to "cevapi" and "čevap" to "cevap" (match).
    if len(q) <= 48 and len(words) <= 4:
        rows = db.execute(
            text(SEARCH_SQL.format(
                where_clause=(
                    ":q_folded LIKE '%' || translate(LOWER(f.display_name),"
                    " 'čžšđć', 'czsdc') || '%'"),
                order_clause="length(f.display_name) DESC")),
            {"q_folded": q.translate(str.maketrans("čžšđć", "czsdc")),
             "limit": 20},
        ).fetchall()
        new = [dict(r._mapping) for r in rows if not any(
            c.get("display_name") == r._mapping["display_name"] for c in candidates)]
        candidates.extend(new)

    # 3. Word-wise (every word must appear, any order)
    if len(candidates) < limit and len(words) > 1:
        conditions = " AND ".join(f"LOWER(f.display_name) LIKE :w{i}" for i in range(len(words)))
        params = {f"w{i}": f"%{w}%" for i, w in enumerate(words)}
        params["limit"] = 60
        rows = db.execute(
            text(SEARCH_SQL.format(where_clause=conditions, order_clause="length(f.display_name)")),
            params,
        ).fetchall()
        candidates.extend([dict(r._mapping) for r in rows])

    # 4. Any-word match (broadest)
    if len(candidates) < limit and words:
        conditions = " OR ".join(f"LOWER(f.display_name) LIKE :w{i}" for i in range(len(words)))
        params = {f"w{i}": f"%{w}%" for i, w in enumerate(words)}
        params["limit"] = 80
        rows = db.execute(
            text(SEARCH_SQL.format(where_clause=conditions, order_clause="length(f.display_name)")),
            params,
        ).fetchall()
        candidates.extend([dict(r._mapping) for r in rows])

    # Dedupe
    seen = set()
    unique = []
    for c in candidates:
        if c["display_name"] not in seen:
            seen.add(c["display_name"])
            unique.append(c)

    if not unique:
        return []

    # Rank by fuzzy score
    ranked = _rank_candidates(unique, q)
    return ranked[:limit]


# ---------------------------------------------------------------------------
# Structured nutrition fallback (USDA FoodData Central) + Camoufox last resort
# ---------------------------------------------------------------------------
USDA_API_KEY = os.environ.get("USDA_API_KEY", "")
_USDA_CACHE = {}  # query -> (timestamp, candidates); DEMO_KEY is rate-limited


def _norm_nutrient(value):
    """Coerce a possibly-str USDA value to float or None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def usda_search(query: str, limit: int = 3) -> list[dict]:
    """Query USDA FoodData Central for structured per-100g nutrition.

    Accurate (authoritative data), used as the primary fallback when the
    local DB has 0 matches. DEMO_KEY allows ~30 req/hr which is plenty for a
    fallback path. Falls back to camoufox if rate-limited/unavailable.

    Restricted to Foundation/SR Legacy/Survey data types: the default search
    ranks Branded products first, which returns junk (e.g. a "STEAK" seasoning
    mix with 556 kcal from carbs).
    """
    key = query.strip().lower()
    now = time.time()
    if key in _USDA_CACHE and now - _USDA_CACHE[key][0] < 900:
        return _USDA_CACHE[key][1]
    try:
        log.info(f"[usda] searching: {query}")
        r = httpx.get(
            "https://api.nal.usda.gov/fdc/v1/foods/search",
            params={
                "query": query,
                "api_key": USDA_API_KEY,
                "pageSize": limit,
                "dataType": ["Foundation", "SR Legacy", "Survey (FNDDS)"],
            },
            timeout=30,
        )
        if r.status_code == 429:
            log.warning("[usda] rate-limited (429)")
            return _USDA_CACHE.get(key, (0, []))[1]
        if r.status_code != 200:
            log.warning(f"[usda] HTTP {r.status_code}")
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
                "kcal_per_100g": _norm_nutrient(nuts.get("Energy")),
                "protein_g_per_100g": _norm_nutrient(nuts.get("Protein")),
                "carbs_g_per_100g": _norm_nutrient(nuts.get("Carbohydrate, by difference")),
                "fat_g_per_100g": _norm_nutrient(nuts.get("Total lipid (fat)")),
                "fiber_g_per_100g": _norm_nutrient(nuts.get("Fiber, total dietary")),
                "sodium_mg_per_100g": _norm_nutrient(nuts.get("Sodium, Na")),
                "source": "USDA API",
                "confidence": 0.85,
            })
        log.info(f"[usda] got {len(out)} candidates")
        _USDA_CACHE[key] = (now, out)
        return out
    except Exception as e:
        log.warning(f"[usda] error: {e}")
        return []


DDG_HTTP_URL = os.environ.get("DDG_HTTP_URL", "")


def camoufox_search(query: str) -> list[dict]:
    """When 0 DB matches, look the food up via DuckDuckGo on the VPS host.

    Runs entirely on the VPS: an HTTP call to the host-side ddg-http service
    (172.20.0.1:8098), which runs Camoufox egressing through the home-socks
    tunnel to the residential IP. Google was never used by the original Pi
    script (it used DuckDuckGo too); the old SSH-to-Pi path is removed.
    """
    if not query or not query.strip():
        return []
    try:
        import httpx
        log.info(f"[ddg-fallback] searching: {query}")
        r = httpx.post(f"{DDG_HTTP_URL}/lookup",
                       json={"query": query.strip()}, timeout=75)
        if r.status_code != 200:
            log.warning(f"[ddg-fallback] http {r.status_code}: {r.text[:200]}")
            return []
        data = r.json()
        cands = data.get("candidates", [])
        for c in cands:
            c["display_name"] = query.strip().title()
            c.setdefault("source", "duckduckgo")
        log.info(f"[ddg-fallback] got {len(cands)} candidates")
        return cands
    except Exception as e:
        log.warning(f"[ddg-fallback] error: {e}")
        return []


def structured_fallback(query: str) -> list[dict]:
    """Primary structured fallback (USDA API); camoufox as last resort."""
    cands = usda_search(query)
    if cands:
        return cands
    return camoufox_search(query)


# ---------------------------------------------------------------------------
# Recipe builder
# ---------------------------------------------------------------------------
async def parse_ingredients(db: Session, text: str) -> list[dict]:
    """Parse a recipe ingredient list into items with per-ingredient nutrition."""
    items = await parse_meal_text(text)
    resolved = []
    for it in items:
        name = it["display_name"]
        grams = it.get("grams")
        # An ingredient without a quantity defaults to 1 serving of 100g
        if not grams:
            grams = 100.0
        cands = await search_food(db, name)
        c = cands[0] if cands else None
        if c is None:
            # try camoufox for the ingredient
            ccs = structured_fallback(name)
            c = ccs[0] if ccs else None
        resolved.append({
            "name": name,
            "grams": grams,
            "kcal_per_100g": (c or {}).get("kcal_per_100g"),
            "protein_g_per_100g": (c or {}).get("protein_g_per_100g"),
            "carbs_g_per_100g": (c or {}).get("carbs_g_per_100g"),
            "fat_g_per_100g": (c or {}).get("fat_g_per_100g"),
            "fiber_g_per_100g": (c or {}).get("fiber_g_per_100g"),
            "sodium_mg_per_100g": (c or {}).get("sodium_mg_per_100g"),
            "source": (c or {}).get("source", "unmatched"),
        })
    return resolved


def _compute_recipe_totals(ingredients: list[dict]) -> dict:
    """Compute total weight + total nutrition from ingredient list."""
    total_weight = sum(i["grams"] for i in ingredients)
    totals = {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0, "sodium_mg": 0.0}
    for i in ingredients:
        g = i["grams"]
        f = g / 100.0
        totals["kcal"] += (i.get("kcal_per_100g") or 0) * f
        totals["protein_g"] += (i.get("protein_g_per_100g") or 0) * f
        totals["carbs_g"] += (i.get("carbs_g_per_100g") or 0) * f
        totals["fat_g"] += (i.get("fat_g_per_100g") or 0) * f
        totals["fiber_g"] += (i.get("fiber_g_per_100g") or 0) * f
        totals["sodium_mg"] += (i.get("sodium_mg_per_100g") or 0) * f
    # nutrition per 100g for storage
    per100 = {}
    if total_weight > 0:
        per100 = {k: round(v / total_weight * 100, 3) for k, v in totals.items()}
    return {
        "total_weight_g": round(total_weight, 2),
        "totals": {k: round(v, 2) for k, v in totals.items()},
        "nutrition_per_100g": per100,
    }


# ---------------------------------------------------------------------------
# Draft store (Redis)
# ---------------------------------------------------------------------------
def save_draft(draft_id: str, data: dict, ttl: int = 3600):
    redis_client.set(f"intelligent_draft:{draft_id}", json.dumps(data), ex=ttl)


def get_draft(draft_id: str) -> Optional[dict]:
    data = redis_client.get(f"intelligent_draft:{draft_id}")
    return json.loads(data) if data else None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/meals/from-text")
async def create_meal_from_text_alias(
    req: MealIn,
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    """Alias so the legacy log_meal MCP path lands here too (auto-confirmed upstream)."""
    return await create_meal_from_text_intelligent(req=req, user=user, db=db)


@app.post("/meals/from-text-intelligent")
async def create_meal_from_text_intelligent(
    req: MealIn,
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    """Parse text, search DB per item (with camoufox fallback), return draft."""
    text = req.text
    items = await parse_meal_text(text)

    # Extract meal_type from the phrase if the model didn't supply one
    meal_type = req.meal_type
    if not meal_type:
        meal_type = _extract_meal_type(text)

    draft_items = []
    for item in items:
        display_name = item.get("display_name", "")
        grams = item.get("grams")
        if not display_name:
            continue

        candidates = await search_food(db, display_name)
        source = "db"
        # Merge USDA API candidates into the pool and rank together.
        # DB rows get +0.15 home bonus (curated, matched against exact fragment);
        # rows without energy data are dropped (they'd log 0 kcal); USDA fills
        # the gap when the DB has nothing usable.
        try:
            usda_cands = usda_search(display_name, limit=3)
        except Exception:
            usda_cands = []
        pool = [dict(c, _home=True) for c in candidates]
        # Learned private-catalog entries count as "home" too (2026-08-30):
        # the user (or a google fallback) taught us these exact foods, so they
        # must be searchable/prioritised like DB rows.
        private_ids = set()
        try:
            ds_id = _private_source_id(db)
            private_ids = {str(r[0]) for r in db.execute(
                sa_text("SELECT id FROM foods WHERE data_source_id = :ds"),
                {"ds": ds_id}).fetchall()} if ds_id else set()
        except Exception as e:
            log.warning(f"[private-catalog] id lookup failed: {e}")
        if private_ids:
            pool += [dict(c, _home=True, _private=True) for c in usda_cands
                     if str(c.get("food_id")) in private_ids]
            pool += [dict(c, _home=True, _private=True) for c in pool
                     if str(c.get("food_id")) in private_ids]
        pool += [c for c in usda_cands if not any(
            c.get("display_name") == x.get("display_name") for x in pool)]
        if not pool:
            pool = structured_fallback(display_name)
            source = "google"
        candidates = _rank_candidates(pool, display_name)
        # Apply home bonus after ranking by re-sorting: home rows get +0.15
        rescored = []
        for c in candidates:
            is_home = c.pop("_home", False)
            is_private = c.pop("_private", False)
            bonus = 0.0
            if is_private:
                # Learned entries: strong boost, and a match (case-insensitive,
                # diacritic-folded, token-or-substring) with the user's fragment
                # is decisive — the user taught us this exact food.  Handles
                # stored "čevap" vs typed "cevapi".  The boosted score is stored
                # back onto the candidate so confirm_meal's MIN_CONFIDENCE check
                # sees it (not just the ordering).
                bonus = 0.55
                def _fold_token(w):
                    return w.translate(str.maketrans("čžšđć", "czsdc"))
                # Fold diacritics BEFORE tokenising so 'čevap' -> 'cevap' keeps
                # its leading c (the [a-z0-9] class would otherwise drop 'č').
                frag_tokens = {_fold_token(t) for t in re.findall(r"[a-z0-9]+", _fold_token((display_name or "").lower()))}
                name_tokens = {_fold_token(t) for t in re.findall(r"[a-z0-9]+", _fold_token((c.get("display_name") or "").lower()))}
                matched_tok = frag_tokens & name_tokens
                if frag_tokens and (matched_tok or any(
                        len(qs) >= 3 and len(ns) >= 3 and (qs.startswith(ns) or ns.startswith(qs))
                        for qs in frag_tokens for ns in name_tokens)):
                    bonus += 0.15
                    bonus = min(bonus, 0.80)
            elif is_home:
                bonus = 0.30
            boosted = (c.get("confidence") or 0) + bonus
            # Store the boosted score back onto the candidate so the confirm
            # path's confidence floor sees the real, user-learned confidence.
            c["confidence"] = boosted
            rescored.append((boosted, c))
        rescored.sort(key=lambda x: x[0], reverse=True)
        candidates = [c for _, c in rescored]
        for c in candidates:
            c.setdefault("source", source)

        draft_items.append({
            "fragment": display_name,
            "grams": grams,
            "candidates": candidates,
            "needs_user_selection": _needs_selection(candidates) or (
                (not candidates or (candidates[0].get("confidence") or 0.0) < MIN_CONFIDENCE)
            ),
        })

    draft_id = uuid.uuid4().hex
    save_draft(draft_id, {
        "user_id": user["id"],
        "text": text,
        "eaten_at": req.eaten_at or resolve_eaten_date(req.text or "", datetime.now(timezone.utc)) or datetime.now(timezone.utc).isoformat(),
        "meal_type": meal_type,
        "items": draft_items,
    })

    return {
        "ok": True,
        "data": {
            "draft_id": draft_id,
            "items": draft_items,
        },
    }


@app.post("/meals/from-text-intelligent/confirm")
async def confirm_meal(
    req: ConfirmMealRequest,
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    """Confirm a meal draft and write it to the database."""
    draft = get_draft(req.draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found or expired")
    if draft["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Not your draft")

    # Create meal
    meal_id = str(uuid.uuid4())
    eaten_at = draft.get("eaten_at", datetime.now(timezone.utc).isoformat())
    meal_type = draft.get("meal_type") or "snack"
    notes = draft.get("text", "")

    dup = db.execute(
        text("SELECT id FROM meals WHERE notes = :notes AND created_at > NOW() - INTERVAL '2 minutes' LIMIT 1"),
        {"notes": notes},
    ).fetchone()
    if dup:
        # Idempotent success: the meal IS in the database. Returning ok:false made
        # the bot report a failure and retry endlessly for an already-logged meal.
        existing = db.execute(
            text("SELECT totals_json FROM meals WHERE id = :mid"),
            {"mid": str(dup[0])},
        ).fetchone()
        totals = existing[0] if existing else {}
        return {
            "ok": True,
            "duplicate": True,
            "data": {
                "meal_id": str(dup[0]),
                "status": "confirmed",
                "totals": totals,
                "message": "Already logged moments ago — no duplicate created.",
            },
        }
    db.execute(
        text("""
                INSERT INTO meals (id, user_id, eaten_at, meal_type, status, input_method, notes, totals_json, confidence, created_at, updated_at)
            VALUES (:id, :user_id, CAST(:eaten_at AS timestamp with time zone), :meal_type, 'confirmed', 'text_intelligent', :notes, :totals_json, 0.8, NOW(), NOW())
        """),
        {"id": meal_id, "user_id": user["id"], "eaten_at": eaten_at, "meal_type": meal_type, "notes": notes, "totals_json": json.dumps({"kcal": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "fiber_g": 0, "sodium_mg": 0})},
    )

    # Create meal items
    totals = {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0, "sodium_mg": 0.0}
    item_details = []

    for i, item_data in enumerate(draft["items"]):
        sel_idx = req.selections[i] if i < len(req.selections) else 0
        candidates = item_data.get("candidates", [])
        fragment = item_data.get("fragment", "")
        grams = item_data.get("grams") or 100.0

        # Confidence floor: the MCP layer auto-confirms with selections=[0,...]
        # without seeing the candidates. A top candidate below MIN_CONFIDENCE is
        # almost always a wrong food (e.g. 'Fat, chicken' for 'corn bread', both
        # seen in production with negative confidence). Log it as 'unmatched'
        # instead of silently substituting the wrong food. An explicit user
        # selection (non-zero index) is honoured regardless.
        auto_confirmed = sel_idx == 0
        top_conf = (candidates[0].get("confidence") or 0.0) if candidates else 0.0
        if auto_confirmed and candidates and top_conf < MIN_CONFIDENCE:
            log.warning(
                "[confirm] low-confidence auto-match rejected: %r conf=%.3f (min %.2f)",
                candidates[0].get("display_name"), top_conf, MIN_CONFIDENCE,
            )
            # Last resort: try web lookup (USDA + DDG/camoufox) before logging as
            # unmatched. A weak USDA match got us here; DDG may return better data.
            try:
                ddg_cands = structured_fallback(fragment)
                if ddg_cands:
                    ddg_ranked = _rank_candidates(ddg_cands, fragment)
                    best_conf = (ddg_ranked[0].get("confidence") or 0.0) if ddg_ranked else 0.0
                    if best_conf >= MIN_CONFIDENCE:
                        candidates = ddg_ranked
                        log.info("[confirm] DDG fallback rescued: %r conf=%.3f", fragment, best_conf)
            except Exception as e:
                log.warning("[confirm] DDG fallback error: %r", e)
            if not candidates:
                candidates = []

        if candidates and sel_idx < len(candidates):
            c = candidates[sel_idx]
            factor = grams / 100.0
            kcal = (c.get("kcal_per_100g") or 0) * factor
            protein = (c.get("protein_g_per_100g") or 0) * factor
            carbs = (c.get("carbs_g_per_100g") or 0) * factor
            fat = (c.get("fat_g_per_100g") or 0) * factor
            fiber = (c.get("fiber_g_per_100g") or 0) * factor
            sodium = (c.get("sodium_mg_per_100g") or 0) * factor

            item_id = str(uuid.uuid4())
            db.execute(
                text("""
                    INSERT INTO meal_items (id, meal_id, food_catalog_item_id, display_name, quantity, unit, grams, nutrients_json, source, confidence, user_corrected, created_at, updated_at)
                    VALUES (:id, :meal_id, NULL, :display_name, 1, 'g', :grams, :nutrients_json, :source, :confidence, false, NOW(), NOW())
                """),
                {
                    "id": item_id,
                    "meal_id": meal_id,
                    "display_name": c.get("display_name", fragment),
                    "grams": grams,
                    "nutrients_json": json.dumps({
                        "kcal": round(kcal, 2),
                        "protein_g": round(protein, 2),
                        "carbs_g": round(carbs, 2),
                        "fat_g": round(fat, 2),
                        "fiber_g": round(fiber, 2),
                        "sodium_mg": round(sodium, 2),
                    }),
                    "source": c.get("source", "unknown"),
                    "confidence": c.get("confidence", 0.8),
                },
            )

            totals["kcal"] += kcal
            totals["protein_g"] += protein
            totals["carbs_g"] += carbs
            totals["fat_g"] += fat
            totals["fiber_g"] += fiber
            totals["sodium_mg"] += sodium
            item_details.append({
                "display_name": c.get("display_name", fragment),
                "grams": grams,
                "source": c.get("source", "unknown"),
            })
        elif fragment and candidates:
            # Learning hook: if the winning candidate came from the google
            # fallback, persist it into the private catalog so future meals hit
            # the DB first (2026-08-30 pipeline fix).
            try:
                win_src = (c.get("source") or "").lower()
                if "google" in win_src or "camoufox" in win_src:
                    per100g = {
                        "kcal": c.get("kcal_per_100g"),
                        "protein_g": c.get("protein_g_per_100g"),
                        "carbs_g": c.get("carbs_g_per_100g"),
                        "fat_g": c.get("fat_g_per_100g"),
                        "fiber_g": c.get("fiber_g_per_100g"),
                        "sodium_mg": c.get("sodium_mg_per_100g"),
                    }
                    catalog_learn(db, fragment, per100g,
                                  external_id="google:" + re.sub(r"[^a-z0-9]+", "-", fragment.lower()).strip("-")[:80])
            except Exception as e:
                log.warning(f"[catalog-learn] google learn failed: {e}")
        else:
            # No candidate: log raw fragment as 0-kcal placeholder (user adjusts later)
            item_id = str(uuid.uuid4())
            db.execute(
                text("""
                    INSERT INTO meal_items (id, meal_id, food_catalog_item_id, display_name, quantity, unit, grams, nutrients_json, source, confidence, user_corrected, created_at, updated_at)
                    VALUES (:id, :meal_id, NULL, :display_name, 1, 'g', :grams, :nutrients_json, 'unmatched', 0.3, false, NOW(), NOW())
                """),
                {
                    "id": item_id,
                    "meal_id": meal_id,
                    "display_name": fragment,
                    "grams": grams,
                    "nutrients_json": json.dumps({
                        "kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0,
                        "fat_g": 0.0, "fiber_g": 0.0, "sodium_mg": 0.0,
                        "unmatched": True,
                    }),
                },
            )
            item_details.append({
                "display_name": fragment,
                "grams": grams,
                "source": "unmatched",
            })

    db.execute(
        text("UPDATE meals SET totals_json = :totals WHERE id = :id"),
        {"totals": json.dumps({k: round(v, 2) for k, v in totals.items()}), "id": meal_id},
    )
    db.commit()

    # Delete draft
    redis_client.delete(f"intelligent_draft:{req.draft_id}")

    return {
        "ok": True,
        "data": {
            "meal_id": meal_id,
            "status": "confirmed",
            "totals": {k: round(v, 2) for k, v in totals.items()},
            "items": item_details,
            "auto_confirmed": True,
        },
        "message": "Meal logged with best available matches. You can edit items later if needed.",
    }


@app.delete("/meals/{meal_id}")
async def delete_meal_by_id(
    meal_id: str,
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    """Delete a whole meal and its items by id."""
    meal = db.execute(text("SELECT id FROM meals WHERE id = :mid"), {"mid": meal_id}).fetchone()
    if not meal:
        return {"ok": False, "error": "meal_not_found"}
    db.execute(text("DELETE FROM meal_items WHERE meal_id = :mid"), {"mid": meal_id})
    db.execute(text("DELETE FROM meals WHERE id = :mid"), {"mid": meal_id})
    db.commit()
    return {"ok": True, "deleted": meal_id}




@app.post("/meals/learn")
async def learn_food(req: LearnFoodRequest,
                     user: dict = Depends(get_user_from_service_or_bearer),
                     db: Session = Depends(get_db)):
    """Store user-provided nutrition for a food fragment into the private
    catalog so ALL future matches resolve from the local DB first (2026-08-30).
    """
    per100g = {
        "kcal": req.kcal_per_100g,
        "protein_g": req.protein_g_per_100g,
        "carbs_g": req.carbs_g_per_100g,
        "fat_g": req.fat_g_per_100g,
        "fiber_g": req.fiber_g_per_100g,
        "sodium_mg": req.sodium_mg_per_100g,
    }
    per100g = {k: v for k, v in per100g.items() if v is not None}
    if not req.display_name or not per100g:
        raise HTTPException(status_code=422, detail="display_name and at least one nutrient required")
    food_id = catalog_learn(db, req.display_name.strip(), per100g,
                            external_id=("user:" + re.sub(r"[^a-z0-9]+", "-", req.display_name.lower()).strip("-")[:80]))
    return {"ok": True, "data": {"food_id": food_id, "display_name": req.display_name.strip(), "stored": True}}

@app.post("/meals/delete-by-fragment")
async def delete_meal_by_fragment(
    req: MealIn,
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    """Delete meals whose notes match a fragment.
    body: {text: '<fragment>', match_all: true/false}"""
    frag = (req.text or "").strip()
    if not frag:
        return {"ok": False, "error": "missing_text"}
    match_all = bool(req.match_all)
    if match_all:
        rows = db.execute(text(
            "SELECT id, notes FROM meals WHERE notes ILIKE :pat ORDER BY created_at DESC"
        ), {"pat": f"%{frag}%"}).fetchall()
    else:
        rows = db.execute(text(
            "SELECT id, notes FROM meals WHERE notes ILIKE :pat ORDER BY created_at DESC LIMIT 1"
        ), {"pat": f"%{frag}%"}).fetchall()
    if not rows:
        return {"ok": False, "error": "no_match"}
    deleted = []
    for row in rows:
        db.execute(text("DELETE FROM meal_items WHERE meal_id = :mid"), {"mid": row[0]})
        db.execute(text("DELETE FROM meals WHERE id = :mid"), {"mid": row[0]})
        deleted.append(row[0])
    db.commit()
    return {"ok": True, "deleted_count": len(deleted), "deleted_ids": deleted, "matched_notes": [r[1][:80] for r in rows]}


@app.get("/meals/latest")
async def latest_meal(
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    """Most recent meal id + items, for correction flows (raw SQL)."""
    meal = db.execute(text("SELECT id FROM meals ORDER BY created_at DESC LIMIT 1")).fetchone()
    if not meal:
        return {"ok": False, "error": "no_meals"}
    mid = meal[0]
    items = db.execute(text("SELECT display_name FROM meal_items WHERE meal_id = :mid"), {"mid": mid}).fetchall()
    return {"ok": True, "data": {"id": mid, "items": [i[0] for i in items]}}


@app.post("/meals/{meal_id}/items/correct")
async def correct_meal_item(
    meal_id: str,
    req: MealIn,
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    """Replace an item in a logged meal (raw SQL).
    body: {text: '<wrong food fragment>', meal_type: '<right food name>'}"""
    wrong = (req.text or "").strip().lower()
    right = (req.meal_type or "").strip()
    meal = db.execute(text("SELECT id FROM meals WHERE id = :mid"), {"mid": meal_id}).fetchone()
    if not meal:
        return {"ok": False, "error": "meal_not_found"}
    items = db.execute(text("SELECT id, display_name, grams, nutrients_json FROM meal_items WHERE meal_id = :mid"), {"mid": meal_id}).fetchall()
    target = None
    for it in items:
        dn = (it[1] or "").lower()
        if wrong and (wrong in dn or dn in wrong):
            target = it
            break
    if not target:
        return {"ok": False, "error": "item_not_found", "items": [i[1] for i in items]}
    item_id, old_name, grams, old_nutrients = target[0], target[1], target[2] or 0, target[3]
    # User explicitly marked the match wrong: prefer web/USDA fallback over a DB
    # re-match unless a DB candidate is a very strong match (conf >= 0.75).
    candidates = await search_food(db, right)
    best = candidates[0] if candidates else None
    if not best or (best.get("confidence") or 0) < 0.75:
        fallback = structured_fallback(right)
        if fallback and (not best or (fallback[0].get("confidence") or 0) >= (best.get("confidence") or 0) - 0.1):
            candidates = fallback
            best = fallback[0]
    if not best:
        return {"ok": False, "error": "no_candidates"}
    new_name = best["display_name"]
    per100 = {
        "kcal": best.get("kcal_per_100g") or 0,
        "protein_g": best.get("protein_g_per_100g") or 0,
        "carbs_g": best.get("carbs_g_per_100g") or 0,
        "fat_g": best.get("fat_g_per_100g") or 0,
        "fiber_g": best.get("fiber_g_per_100g") or 0,
        "sodium_mg": best.get("sodium_mg_per_100g") or 0,
    }
    scaled = {k: round(v * grams / 100.0, 2) for k, v in per100.items()}
    db.execute(text(
        "UPDATE meal_items SET display_name = :dn, nutrients_json = :nj, user_corrected = TRUE, updated_at = NOW() WHERE id = :iid"
    ), {"dn": new_name, "nj": json.dumps(scaled), "iid": item_id})
    # recompute meal totals from all items
    items2 = db.execute(text("SELECT nutrients_json FROM meal_items WHERE meal_id = :mid"), {"mid": meal_id}).fetchall()
    tot = {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    for row in items2:
        try:
            nj = row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}")
        except Exception:
            continue
        for k in tot:
            try:
                tot[k] += float(nj.get(k) or 0)
            except Exception:
                pass
    tot = {k: round(v, 2) for k, v in tot.items()}
    db.execute(text("UPDATE meals SET totals_json = :tj, updated_at = NOW() WHERE id = :mid"), {"tj": json.dumps(tot), "mid": meal_id})
    db.commit()
    return {"ok": True, "meal_id": meal_id, "replaced": old_name, "replaced_with": new_name, "grams": grams, "totals": tot}


@app.post("/meals/{meal_id}/items/set-custom")
async def set_custom_nutrition(
    meal_id: str,
    request: Request,
    req: MealIn,
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    """Set user-provided per-100g nutrition on a matching meal item (raw SQL).
    body: {text: '<item name fragment>', meal_type: unused,
           match_all: false}
    Custom values arrive via query params on the MCP side; here we accept them in
    req.model_extra as: kcal_per_100g, protein_per_100g, carbs_per_100g, fat_per_100g,
    fiber_per_100g, sodium_per_100g, new_name (optional display name)."""
    frag = (req.text or "").strip().lower()
    if not frag:
        return {"ok": False, "error": "missing_text"}
    qp = dict(request.query_params)
    def _f(key):
        # accept both 'protein_per_100g' and the MCP tool's 'protein_g_per_100g'
        v = qp.get(key) or qp.get(key.replace("_per_100g", "_g_per_100g") if key != "kcal_per_100g" else key)
        try:
            return float(v) if v not in (None, "") else None
        except Exception:
            return None
    kcal100 = _f("kcal_per_100g")
    prot100 = _f("protein_per_100g")
    carb100 = _f("carbs_per_100g")
    fat100 = _f("fat_per_100g")
    fib100 = _f("fiber_per_100g")
    sod100 = _f("sodium_per_100g")
    new_name = qp.get("new_name")
    meal = db.execute(text("SELECT id FROM meals WHERE id = :mid"), {"mid": meal_id}).fetchone()
    if not meal:
        return {"ok": False, "error": "meal_not_found"}
    items = db.execute(text("SELECT id, display_name, grams FROM meal_items WHERE meal_id = :mid"), {"mid": meal_id}).fetchall()
    target = _match_item(items, frag)
    if not target:
        return {"ok": False, "error": "item_not_found", "items": [i[1] for i in items]}
    item_id, old_name, grams = target[0], target[1], target[2] or 0
    scale = grams / 100.0
    nj = {"kcal": round((kcal100 or 0) * scale, 2),
          "protein_g": round((prot100 or 0) * scale, 2),
          "carbs_g": round((carb100 or 0) * scale, 2),
          "fat_g": round((fat100 or 0) * scale, 2),
          "fiber_g": round((fib100 or 0) * scale, 2) if fib100 is not None else 0,
          "sodium_mg": round((sod100 or 0) * scale, 2) if sod100 is not None else 0}
    dn = new_name or old_name
    db.execute(text(
        "UPDATE meal_items SET display_name = :dn, nutrients_json = :nj, user_corrected = TRUE, updated_at = NOW() WHERE id = :iid"
    ), {"dn": dn, "nj": json.dumps(nj), "iid": item_id})
    # recompute meal totals
    items2 = db.execute(text("SELECT nutrients_json FROM meal_items WHERE meal_id = :mid"), {"mid": meal_id}).fetchall()
    tot = {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    for row in items2:
        try:
            n = row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}")
        except Exception:
            continue
        for k in tot:
            try:
                tot[k] += float(n.get(k) or 0)
            except Exception:
                pass
    tot = {k: round(v, 2) for k, v in tot.items()}
    db.execute(text("UPDATE meals SET totals_json = :tj, updated_at = NOW() WHERE id = :mid"), {"tj": json.dumps(tot), "mid": meal_id})
    db.commit()
    return {"ok": True, "item_id": item_id, "display_name": dn, "grams": grams,
            "per_100g": {"kcal": kcal100, "protein_g": prot100, "carbs_g": carb100, "fat_g": fat100},
            "scaled": nj, "meal_totals": tot}


@app.post("/meals/{meal_id}/items/set-weight")
async def set_meal_item_weight(
    meal_id: str,
    req: SetWeightRequest,
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    """Change the weight (grams) of one logged meal item and rescale its nutrition.
    body: {text: '<item name fragment>', grams: <new weight in grams>}
    The displayed name and per-100g nutrition stay the same; only the portion
    and the scaled nutrients change. Meal totals are recomputed. Deterministic —
    no food re-match, so 'change the steak weight to 200g' always works."""
    frag = (req.text or "").strip().lower()
    new_grams = req.grams
    if not frag:
        return {"ok": False, "error": "missing_text"}
    if not new_grams or new_grams <= 0 or new_grams > 10000:
        return {"ok": False, "error": "bad_grams", "message": "grams must be a positive number."}
    meal = db.execute(text("SELECT id FROM meals WHERE id = :mid"), {"mid": meal_id}).fetchone()
    if not meal:
        return {"ok": False, "error": "meal_not_found"}
    items = db.execute(text("SELECT id, display_name, grams, nutrients_json FROM meal_items WHERE meal_id = :mid"), {"mid": meal_id}).fetchall()
    target = _match_item(items, frag)
    if not target:
        return {"ok": False, "error": "item_not_found", "items": [i[1] for i in items]}
    item_id, old_name, old_grams, old_nj = target[0], target[1], target[2] or 0, target[3]
    factor = new_grams / (old_grams if old_grams else 100.0)
    # Rescale the existing per-item nutrients linearly with the weight change.
    nj = {}
    try:
        old = old_nj if isinstance(old_nj, dict) else json.loads(old_nj or "{}")
        for k in ("kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "sodium_mg"):
            try:
                nj[k] = round(float(old.get(k) or 0) * factor, 2)
            except Exception:
                nj[k] = 0
    except Exception:
        nj = {}
    db.execute(text(
        "UPDATE meal_items SET grams = :g, nutrients_json = :nj, user_corrected = TRUE, updated_at = NOW() WHERE id = :iid"
    ), {"g": new_grams, "nj": json.dumps(nj), "iid": item_id})
    # recompute meal totals from all items
    items2 = db.execute(text("SELECT nutrients_json FROM meal_items WHERE meal_id = :mid"), {"mid": meal_id}).fetchall()
    tot = {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    for row in items2:
        try:
            n = row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}")
        except Exception:
            continue
        for k in tot:
            try:
                tot[k] += float(n.get(k) or 0)
            except Exception:
                pass
    tot = {k: round(v, 2) for k, v in tot.items()}
    db.execute(text("UPDATE meals SET totals_json = :tj, updated_at = NOW() WHERE id = :mid"), {"tj": json.dumps(tot), "mid": meal_id})
    db.commit()
    return {"ok": True, "item_id": item_id, "display_name": old_name,
            "old_grams": old_grams, "grams": new_grams, "rescaled": nj,
            "meal_totals": tot}


@app.get("/meals/resolve/{frag}")
async def resolve_meal(frag: str, user: dict = Depends(get_user_from_service_or_bearer), db: Session = Depends(get_db)):
    """Resolve a partial/truncated meal id to the full UUID."""
    if len(frag) >= 32:
        try:
            import uuid as _u
            _u.UUID(frag)
            return {"ok": True, "meal_id": frag}
        except Exception:
            pass
    row = db.execute(text("SELECT id FROM meals WHERE id::text LIKE :pat ORDER BY created_at DESC LIMIT 1"), {"pat": f"{frag}%"}).fetchone()
    if row:
        return {"ok": True, "meal_id": str(row[0])}
    return {"ok": False, "error": "not_found"}


@app.get("/meals/foods/search")
async def foods_search_alias(
    q: str = Query(..., description="food to search"),
    limit: int = 10,
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    """Alias so MCP search_food (legacy path) hits the live service."""
    return await intelligent_search(q=q, user=user, db=db)


@app.get("/meals/foods/ddg-lookup")
async def ddg_nutrition_lookup(
    q: str = Query(..., description="food to search on DuckDuckGo"),
    user: dict = Depends(get_user_from_service_or_bearer),
):
    """Explicit DuckDuckGo nutrition lookup via the VPS camoufox fallback.
    
    Use when the local DB + USDA both return nothing, or when the user
    pushes back on a 0-kcal / unmatched food and asks to search online.
    Hits the host-side ddg-http service (Camoufox -> residential tunnel).
    Returns the parsed candidates with per-100g nutrition, or [] if the
    lookup service is unreachable / blocked.
    """
    cands = structured_fallback(q)
    return {
        "query": q,
        "source": "ddg" if cands else "none",
        "candidates": cands,
        "fallback_available": True,
    }


@app.get("/meals/foods/intelligent-search")
async def intelligent_search(
    q: str = Query(..., description="food to search"),
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    """Search closest DB matches; if 0, use camoufox google fallback."""
    candidates = await search_food(db, q)
    source = "db"
    if not candidates:
        candidates = structured_fallback(q)
        source = "google"
    return {
        "query": q,
        "source": source,
        "candidates": candidates,
        "needs_user_selection": len(candidates) > 1,
    }


# ---------------------------------------------------------------------------
# Recipe endpoints
# ---------------------------------------------------------------------------
@app.post("/meals/recipes")
async def create_recipe(
    req: RecipeCreateRequest,
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    """Build a recipe from an ingredient list; compute total weight + nutrition."""
    ingredients = await parse_ingredients(db, req.text)
    if not ingredients:
        raise HTTPException(status_code=400, detail="No ingredients parsed")
    totals = _compute_recipe_totals(ingredients)

    recipe_id = str(uuid.uuid4())
    db.execute(
        text("""
            INSERT INTO recipes (id, user_id, name, total_weight_g, ingredients_json, nutrition_per_100g, created_at, updated_at)
            VALUES (:id, :user_id, :name, :total_weight_g, :ingredients_json, :nutrition_per_100g, NOW(), NOW())
        """),
        {
            "id": recipe_id,
            "user_id": user["id"],
            "name": req.name.strip(),
            "total_weight_g": totals["total_weight_g"],
            "ingredients_json": json.dumps(ingredients),
            "nutrition_per_100g": json.dumps(totals["nutrition_per_100g"]),
        },
    )
    db.commit()

    return {
        "ok": True,
        "data": {
            "recipe_id": recipe_id,
            "name": req.name.strip(),
            "total_weight_g": totals["total_weight_g"],
            "totals": totals["totals"],
            "nutrition_per_100g": totals["nutrition_per_100g"],
            "ingredients": ingredients,
        },
        "message": f"Recipe '{req.name}' built: {totals['total_weight_g']}g total.",
    }


@app.post("/meals/recipes/{recipe_id}/log")
async def log_recipe_meal(
    recipe_id: str,
    req: RecipeLogRequest,
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    """Log a meal from a recipe; scale nutrients by grams_eaten / total_weight."""
    recipe = db.execute(
        text("SELECT * FROM recipes WHERE id = :id AND user_id = :uid"),
        {"id": recipe_id, "uid": user["id"]},
    ).fetchone()
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    total_weight = recipe.total_weight_g
    if total_weight <= 0:
        raise HTTPException(status_code=400, detail="Recipe has no weight")
    scale = req.grams_eaten / total_weight

    n = recipe.nutrition_per_100g
    nutrition_per_100g = n if isinstance(n, dict) else json.loads(n)
    # scale = grams_eaten/100 * per100g  ==  grams_eaten/100 * (total*100/total_weight) == grams_eaten * total/total_weight
    # Simpler: per-100g value * grams_eaten/100
    factor = req.grams_eaten / 100.0
    totals = {
        "kcal": round((nutrition_per_100g.get("kcal") or 0) * factor, 2),
        "protein_g": round((nutrition_per_100g.get("protein_g") or 0) * factor, 2),
        "carbs_g": round((nutrition_per_100g.get("carbs_g") or 0) * factor, 2),
        "fat_g": round((nutrition_per_100g.get("fat_g") or 0) * factor, 2),
        "fiber_g": round((nutrition_per_100g.get("fiber_g") or 0) * factor, 2),
        "sodium_mg": round((nutrition_per_100g.get("sodium_mg") or 0) * factor, 2),
    }

    meal_id = str(uuid.uuid4())
    eaten_at = req.eaten_at or datetime.now(timezone.utc).isoformat()
    meal_type = req.meal_type or "snack"

    db.execute(
        text("""
            INSERT INTO meals (id, user_id, eaten_at, meal_type, status, input_method, notes, totals_json, confidence, created_at, updated_at)
            VALUES (:id, :user_id, CAST(:eaten_at AS timestamp with time zone), :meal_type, 'confirmed', 'recipe', :notes, :totals_json, 0.9, NOW(), NOW())
        """),
        {
            "id": meal_id, "user_id": user["id"], "eaten_at": eaten_at,
            "meal_type": meal_type, "notes": f"{recipe.name} ({req.grams_eaten}g of {total_weight}g)",
            "totals_json": json.dumps(totals),
        },
    )

    item_id = str(uuid.uuid4())
    db.execute(
        text("""
            INSERT INTO meal_items (id, meal_id, food_catalog_item_id, display_name, quantity, unit, grams, nutrients_json, source, confidence, user_corrected, created_at, updated_at)
            VALUES (:id, :meal_id, NULL, :display_name, 1, 'g', :grams, :nutrients_json, 'recipe', 0.9, false, NOW(), NOW())
        """),
        {
            "id": item_id,
            "meal_id": meal_id,
            "display_name": recipe.name,
            "grams": req.grams_eaten,
            "nutrients_json": json.dumps(totals),
        },
    )
    db.commit()

    return {
        "ok": True,
        "data": {
            "meal_id": meal_id,
            "status": "confirmed",
            "recipe_name": recipe.name,
            "eaten_g": req.grams_eaten,
            "total_recipe_g": total_weight,
            "scale": round(scale, 4),
            "totals": totals,
        },
        "message": f"Logged {req.grams_eaten}g of {recipe.name} (recipe {total_weight}g).",
    }


@app.get("/meals/recipes")
async def list_recipes(
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        text("SELECT id, name, total_weight_g FROM recipes WHERE user_id = :uid ORDER BY created_at DESC"),
        {"uid": user["id"]},
    ).fetchall()
    return {
        "ok": True,
        "data": [{"id": str(r.id), "name": r.name, "total_weight_g": r.total_weight_g} for r in rows],
    }


@app.get("/healthz")
async def health():
    return {"ok": True, "service": "intelligent-meal"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
