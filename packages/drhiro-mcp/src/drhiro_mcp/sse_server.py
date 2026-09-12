"""
drHiro Health MCP Server — Raw JSON-RPC over HTTP.
Returns JSON (not SSE) for maximum client compatibility.
"""
import os, json, re, asyncio, httpx, uvicorn
from urllib.parse import quote
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware

API_BASE = os.environ.get("DRHIRO_API_URL", "http://localhost:8010/api/v1")
VISION_BASE_URL = os.environ.get("VISION_BASE_URL", "http://desktop-33cidmi:8000")
VISION_MODEL = os.environ.get("VISION_MODEL", 'E:\\Models\\Qwen3.8-27B-UD-Q4_K_M.gguf')
MEDIA_ROOT = "/openclaw-state/workspace/drhiro/media/inbound"

INTELLIGENT_MEAL_URL = os.environ.get("INTELLIGENT_MEAL_URL", "http://intelligent-meal:8090")
TOKEN = os.environ.get("DRHIRO_MCP_TOKEN", "")
SERVICE_TOKEN = os.environ.get("DRHIRO_SERVICE_TOKEN", "")
TELEGRAM_ID = os.environ.get("DRHIRO_TELEGRAM_ID", "")
REDIS_URL = os.environ.get("REDIS_URL", "")
_LIQUID_WRITER_MODE = os.environ.get("DRHIRO_LIQUID_WRITER", "legacy")


def get_liquid_writer_mode() -> str:
    """Return the active liquid-writer mode.

    Fail-closed: any value other than 'legacy' or 'unified' raises at call sites
    that depend on it. Defaults to 'legacy' (old side effect active) so that a
    missing env var does NOT silently enable the new writer.
    """
    mode = _LIQUID_WRITER_MODE.strip().lower()
    if mode not in ("legacy", "unified"):
        raise ValueError(
            f"Invalid DRHIRO_LIQUID_WRITER={_LIQUID_WRITER_MODE!r}: "
            f"must be 'legacy' or 'unified'"
        )
    return mode


def is_unified_writer() -> bool:
    """True only when DRHIRO_LIQUID_WRITER is explicitly set to 'unified'."""
    return get_liquid_writer_mode() == "unified"


# --- T1 trusted-ingress writer gate ---------------------------------------
# When DRHIRO_TRUSTED_INGRESS_WRITERS_DISABLED is truthy, the conversational
# model's consumption-writing tools are disabled: the trusted worker owns
# Telegram consumption. The model cannot independently log the same drink.
# This is the DECISIVE gate for the model's tool surface; an API header gate
# cannot distinguish a model-presented user JWT from a real user.
_TRUSTED_INGRESS_WRITERS_DISABLED = (
    os.environ.get("DRHIRO_TRUSTED_INGRESS_WRITERS_DISABLED", "false").lower() == "true"
)

# Consumption-writing tool names that the trusted worker must own exclusively.
_CONSUMPTION_WRITER_TOOLS = {
    "log_meal",
    "log_meal_intelligent",
    "confirm_intelligent_meal",
    "log_water",
    "log_liquid",
    "log_recipe_meal",
    "build_recipe",
    "delete_meal",
    "correct_meal_item",
}


def consumption_writers_disabled() -> bool:
    return _TRUSTED_INGRESS_WRITERS_DISABLED


def _writer_disabled_response(tool_name: str):
    return {
        "ok": False,
        "error": (
            f"model_writer_disabled: {tool_name} is owned by the trusted "
            "Telegram ingress; the conversational model cannot log consumption."
        ),
    }


def _headers(path=""):
    h = {"Content-Type": "application/json"}
    # Service token paths: MCP service endpoints that don't require user JWT
    service_paths = ["/meals/", "/tools/"]
    if any(path.startswith(p) for p in service_paths) and SERVICE_TOKEN:
        h["x-service-token"] = SERVICE_TOKEN
        if TELEGRAM_ID:
            h["x-telegram-id"] = TELEGRAM_ID
        return h
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h

async def call_api(method, path, body=None):
    # Route intelligent meal requests to the new service. /meals/from-text is
    # included: the old drhiro-api-1 behind API_BASE was removed, so the
    # non-intelligent path must land on intelligent-meal too (it is a superset
    # and always auto-confirms via the MCP wrapper).
    if (path.startswith("/meals/from-text-intelligent")
            or path == "/meals/from-text"
            or path.startswith("/meals/recipes")
            or path.startswith("/meals/foods/intelligent-search")
            or path.startswith("/meals/foods/ddg-lookup")):
        url = f"{INTELLIGENT_MEAL_URL}{path}"
    else:
        url = f"{API_BASE}{path}"
    async with httpx.AsyncClient(timeout=90) as c:
        try:
            r = await c.request(method, url, headers=_headers(path), content=json.dumps(body) if body else None)
            return r.text
        except Exception as e:
            return json.dumps({"error": str(e)})

TOOLS = [
    {"name": "get_steps", "description": "Get step counts for the last N days.", "inputSchema": {"type": "object", "properties": {"days": {"type": "integer", "default": 7}}}},
    {"name": "get_daily_metrics", "description": "Get today's health metrics (steps, weight, BP, sleep).", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_weight", "description": "Get latest weight reading.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_blood_pressure", "description": "Get latest blood pressure reading.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_device_code", "description": "Issue a one-time device code so the user can link the drHiro Bridge Android app. Call this when the user asks to pair/link their phone, needs a device code, or is setting up the Android bridge app. The code expires in 10 minutes. Relay the returned code to the user verbatim.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_reminders", "description": "Get active reminders.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "create_reminder", "description": "Create a new reminder. Auto-confirms and returns the logged result immediately.", "inputSchema": {"type": "object", "properties": {"reminder_type": {"type": "string"}, "time": {"type": "string"}, "message": {"type": "string"}, "frequency": {"type": "string", "default": "daily"}}}},
    {"name": "get_trends", "description": "Get health trends for a metric over N days.", "inputSchema": {"type": "object", "properties": {"metric": {"type": "string", "default": "steps"}, "days": {"type": "integer", "default": 30}}}},
    {"name": "log_weight", "description": "Log a manual weight entry. Auto-confirms and returns the logged result immediately.", "inputSchema": {"type": "object", "properties": {"value_kg": {"type": "number"}, "measured_at": {"type": "string"}}}},
    {"name": "log_blood_pressure", "description": "Log a manual blood pressure reading. Auto-confirms and returns the logged result immediately.", "inputSchema": {"type": "object", "properties": {"systolic": {"type": "integer"}, "diastolic": {"type": "integer"}, "pulse": {"type": "integer"}, "measured_at": {"type": "string"}}}},
    {"name": "log_meal", "description": "Log a meal. Set text to the user's food words INCLUDING any day or time they mentioned, copied verbatim - e.g. text=\"on Monday for dinner I had chicken breast 200g\" or text=\"yesterday for breakfast 3 eggs and toast\". Never resolve dates yourself and never drop the day words: the server computes the real date in the user's timezone. Put every food in that one sentence and call this tool once. Auto-confirms and returns the logged result immediately.", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "meal_type": {"type": "string"}, "conversation_id": {"type": "string", "description": "the drhiro_conversation_id from the context block (if provided)"}}, "required": ["text"]}},
    {"name": "ddg_nutrition_lookup", "description": "Search online for nutrition data via DuckDuckGo when the local database has no match. Use when the user pushes back on a 0 kcal / unmatched food and asks to look it up online. Returns per-100g kcal/protein/carbs/fat candidates, or [] if lookup is blocked.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}, {"name": "search_food", "description": "Search the food database for a food item by name.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}}}},
    {"name": "log_meal_intelligent", "description": "Log a meal from the user's words. Picks the best matching foods, logs immediately, and returns what was logged. NEVER ask the user to choose options and NEVER call ask_user_question for meals - just call this tool once with the user's verbatim words, then relay the returned summary as-is.", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "meal_type": {"type": "string"}, "conversation_id": {"type": "string", "description": "the drhiro_conversation_id from the context block (if provided)"}}, "required": ["text"]}},
    {"name": "confirm_intelligent_meal", "description": "Confirm a meal draft with user-selected food candidates. Auto-confirms and returns the logged result immediately.", "inputSchema": {"type": "object", "properties": {"draft_id": {"type": "string"}, "selections": {"type": "array", "items": {"type": "integer"}}}, "required": ["draft_id", "selections"]}},
    {"name": "build_recipe", "description": "Build a recipe (multi-ingredient dish) from an ingredient list. Compute total weight and total nutrition, so the user can later log a portion. Example: name=\"gulash\", text=\"650g of beef, 400g of chickpeas, 200g of peas, 2 carrots, 1 onion, 2 tbsp of olive oil, 20g of butter, 1l of water\". Auto-confirms and returns the logged result immediately.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "text": {"type": "string"}}, "required": ["name", "text"]}},
    {"name": "log_recipe_meal", "description": "Log a meal portion from a previously built recipe, scaling nutrients by (grams eaten / total recipe weight). Auto-confirms and returns the logged result immediately.", "inputSchema": {"type": "object", "properties": {"recipe_id": {"type": "string"}, "grams_eaten": {"type": "number"}, "meal_type": {"type": "string"}}, "required": ["recipe_id", "grams_eaten"]}},
    {"name": "delete_meal", "description": "Delete one or more logged meals by matching a fragment of the original text (e.g. 'goulash', 'Monday dinner', 'peach snack'). Pass match_all=true to delete every match, false/omitted to delete only the newest match.", "inputSchema": {"type": "object", "properties": {"fragment": {"type": "string"}, "match_all": {"type": "boolean"}}, "required": ["fragment"]}},
    {"name": "delete_activity", "description": "Delete a logged activity by its ID. Use to correct an accidentally logged or wrong-calorie activity entry. Returns ok when deleted.", "inputSchema": {"type": "object", "properties": {"activity_id": {"type": "string"}}, "required": ["activity_id"]}},
    {"name": "list_activities", "description": "List logged activities for a date (YYYY-MM-DD, defaults to today). Returns each activity with its ID, title and calories_burned, so you can identify the ID of an entry to correct or delete via delete_activity.", "inputSchema": {"type": "object", "properties": {"date": {"type": "string", "description": "Optional date YYYY-MM-DD. Defaults to today."}}}},
    {"name": "update_activity", "description": "Edit an existing activity by its ID (change title, calories_burned, description, or activity_date). Use to fix a wrong-calorie or mis-typed activity entry in place instead of delete+re-log.", "inputSchema": {"type": "object", "properties": {"activity_id": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"}, "calories_burned": {"type": "number"}, "activity_date": {"type": "string"}}, "required": ["activity_id"]}},
    {"name": "list_data_points", "description": "Find logged data points of any metric (water, weight, steps, sleep, blood_pressure, exercise, heart_rate, ...). Returns each point with its ID and value so you can identify and edit/delete it. Pass metric_type (optional) and/or date (YYYY-MM-DD) to narrow.", "inputSchema": {"type": "object", "properties": {"metric_type": {"type": "string", "description": "e.g. water, weight, steps, sleep, blood_pressure, exercise"}, "date": {"type": "string", "description": "Optional date YYYY-MM-DD to filter by"}}}},
    {"name": "update_data_point", "description": "Edit an existing logged entry IN PLACE. Works for a health measurement (water, weight, steps, sleep, blood_pressure, exercise, heart_rate) — pass id (from list_data_points) and value/measured_at/unit. ALSO works for a MEAL ITEM'S PORTION WEIGHT — pass item (<fragment of the logged food name, e.g. 'steak'>) and grams (<new weight>) to change how much of that food was logged without changing which food; use this for requests like 'make the steak 200g', 'the steak should be 200g', 'change the steak weight to 200 grams'. This is the tool for correcting any logged value in place instead of delete+re-log.", "inputSchema": {"type": "object", "properties": {"id": {"type": "string", "description": "data-point id (from list_data_points) for a health measurement edit"}, "value": {"type": "object", "description": "replacement value payload, e.g. {\"weight_kg\": 87}"}, "measured_at": {"type": "string"}, "unit": {"type": "string"}, "item": {"type": "string", "description": "fragment of a logged meal item's display name, e.g. 'steak' — for changing a meal item's portion weight"}, "grams": {"type": "number", "description": "new portion weight in grams — for changing a meal item's weight"}, "meal_id": {"type": "string", "description": "optional; defaults to latest meal when editing a meal item's weight"}, "conversation_id": {"type": "string", "description": "the drhiro_conversation_id from the context block (if provided)"}}}},
    {"name": "delete_data_point", "description": "Delete a logged data point by its ID (any metric). Use to remove a wrong, duplicate, or accidental measurement.", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "set_custom_nutrition", "description": "Write user-provided nutrition values onto a logged meal item when the database has wrong or missing data. Pass item (fragment of the logged item name), meal_id (optional, defaults to newest matching meal), kcal_per_100g, protein_per_100g, carbs_per_100g, fat_per_100g, optionally fiber_per_100g and sodium_per_100g, optionally new_name.", "inputSchema": {"type": "object", "properties": {"item": {"type": "string"}, "meal_id": {"type": "string"}, "kcal_per_100g": {"type": "number"}, "protein_per_100g": {"type": "number"}, "carbs_per_100g": {"type": "number"}, "fat_per_100g": {"type": "number"}, "fiber_per_100g": {"type": "number"}, "sodium_per_100g": {"type": "number"}, "new_name": {"type": "string"}}, "required": ["item", "kcal_per_100g"]}},
    
    {"name": "learn_food", "description": "Store the user's OWN nutritional facts for a food so ALL future meals use them. Use when the user dictates macros (e.g. 'su\u0161ena vratina is 324 kcal, 34g protein, 21g fat per 100g') or after they provide values for an unmatched food. Pass name + per-100g values; text is optional free form.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "description": "food name as the user calls it"}, "text": {"type": "string", "description": "optional: the user's full sentence containing the numbers"}, "kcal_per_100g": {"type": "number"}, "protein_g_per_100g": {"type": "number"}, "carbs_g_per_100g": {"type": "number"}, "fat_g_per_100g": {"type": "number"}, "fiber_g_per_100g": {"type": "number"}, "sodium_mg_per_100g": {"type": "number"}}, "required": ["name"]}},
    {"name": "correct_meal_item", "description": "Correct one item in the most recent logged meal. Pass wrong=<matched-name fragment> and right=<what the user actually ate>. Re-searches foods and updates totals.", "inputSchema": {"type": "object", "properties": {"wrong": {"type": "string"}, "right": {"type": "string"}, "meal_id": {"type": "string", "description": "optional; defaults to latest meal"}, "conversation_id": {"type": "string", "description": "the drhiro_conversation_id from the context block (if provided)"}}, "required": ["wrong", "right"]}},
    {"name": "list_recipes", "description": "List the user's saved recipes with their ids and total weights.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "analyze_food_photo", "description": "Extract nutrition values from a FOOD LABEL or package photo the user sent. Call this when the user attaches a photo of a label/package instead of typing macros. Pass the photo file path from the [media attached: ...] text.", "inputSchema": {"type": "object", "properties": {"photo_path": {"type": "string", "description": "path of the attached image"}, "learn": {"type": "boolean", "default": True}, "grams": {"type": "number"}}, "required": ["photo_path"]}},
    {"name": "log_water", "description": "Log water intake. Set amount_ml to the volume in millilitres. Auto-confirms and returns the logged result immediately.", "inputSchema": {"type": "object", "properties": {"amount_ml": {"type": "number", "description": "volume in millilitres"}, "conversation_id": {"type": "string", "description": "the drhiro_conversation_id from the context block (if provided)"}}, "required": ["amount_ml"]}},
    {"name": "log_liquid", "description": "Log any liquid/drink intake with a category. Categories: water, non_alcoholic (juice/coffee/tea/soda/milk), beer, wine, spirits (whiskey/vodka/rum/gin/rakija), other_alcohol (cocktails/liqueurs/cider). Set amount_ml to the volume in millilitres and category to one of those values. Auto-confirms and returns the logged result immediately.", "inputSchema": {"type": "object", "properties": {"amount_ml": {"type": "number", "description": "volume in millilitres"}, "category": {"type": "string", "description": "one of: water, non_alcoholic, beer, wine, spirits, other_alcohol", "enum": ["water", "non_alcoholic", "beer", "wine", "spirits", "other_alcohol"]}, "conversation_id": {"type": "string", "description": "the drhiro_conversation_id from the context block (if provided)"}}, "required": ["amount_ml", "category"]}},
    {"name": "log_activity", "description": "Log a physical activity. Auto-confirms and returns the logged result immediately.", "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}, "calories_burned": {"type": "number"}, "activity_date": {"type": "string"}, "conversation_id": {"type": "string", "description": "the drhiro_conversation_id from the context block (if provided)"}}, "required": ["title", "calories_burned"]}}
]

# Simple in-memory session store
_SESSIONS = {}

def _unwrap(v, _depth=0):
    """Strip operator-wrapper objects small models emit around plain values.

    Observed from Qwen via TrueForge:
      {"$expr": {"eq": ["lunch", "lunch"]}}            -> "lunch"
      {"$expr": [{"display_name": {...}},
]}             -> [{"display_name": ...}]
    Any single-key dict whose key starts with "$", and comparison wrappers
    like {"eq": [a, b]}, collapse to the underlying value.
    """
    if _depth > 8:
        return v
    if isinstance(v, dict):
        if len(v) == 1:
            only_key = next(iter(v))
            if isinstance(only_key, str) and only_key.startswith("$"):
                return _unwrap(v[only_key], _depth + 1)
            if isinstance(only_key, str) and only_key.lower() in (
                "eq", "equals", "==", "value", "const", "literal",
            ):
                inner = v[only_key]
                if isinstance(inner, (list, tuple)) and inner:
                    return _unwrap(inner[0], _depth + 1)
                return _unwrap(inner, _depth + 1)
        return {k: _unwrap(val, _depth + 1) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_unwrap(i, _depth + 1) for i in v]
    return v


def _as_text(v, default=""):
    """Coerce a model-supplied value to a plain string.

    Small local models (Qwen) sometimes emit a dict or list where the schema
    asks for a string, e.g. query={"Big Mac": "calories"} instead of
    query="Big Mac". Naive str() yields a mangled query, so recover the
    intended text instead of failing or searching for garbage.
    """
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        # Prefer common key names the model may wrap the value in.
        for k in ("query", "name", "value", "text", "food", "display_name"):
            if k in v and isinstance(v[k], str):
                return v[k].strip()
        # Otherwise the intent is usually a KEY ({"Big Mac": "calories"}).
        # Prefer a key containing letters over a purely numeric one, since models
        # sometimes emit count maps like {"210": "2", "Big Mac": "1"}.
        keys = [k for k in v.keys() if isinstance(k, str) and k.strip()]
        for k in keys:
            if any(ch.isalpha() for ch in k):
                return k.strip()
        for k in keys:
            return k.strip()
        return default
    if isinstance(v, (list, tuple)):
        for item in v:
            got = _as_text(item, "")
            if got:
                return got
        return default
    return str(v).strip()


def _as_number(v, default=0):
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v.replace(",", "."))
        except ValueError:
            return default
    if isinstance(v, dict):
        for cand in v.values():
            got = _as_number(cand, None)
            if got is not None:
                return got
    return default


def _normalize_items(raw):
    """Normalize the log_meal items array into [{display_name, grams, ...}]."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        # Either a single item, or a mapping of {food: grams}.
        if any(k in raw for k in ("display_name", "name", "food")):
            raw = [raw]
        else:
            return [{"display_name": _as_text(k), "grams": _as_number(val, None) or 100}
                    for k, val in raw.items() if _as_text(k)]
    if isinstance(raw, str):
        return [{"display_name": raw.strip(), "grams": 100}] if raw.strip() else []
    out = []
    for it in raw if isinstance(raw, (list, tuple)) else []:
        if isinstance(it, str):
            if it.strip():
                out.append({"display_name": it.strip(), "grams": 100})
            continue
        if not isinstance(it, dict):
            continue
        name = _as_text(it.get("display_name") or it.get("name") or it.get("food"))
        if not name:
            continue
        entry = {"display_name": name}
        grams = it.get("grams")
        if grams is not None:
            entry["grams"] = _as_number(grams, None)
        qty = it.get("quantity")
        if qty is not None:
            entry["quantity"] = _as_number(qty, None)
        unit = _as_text(it.get("unit"), "")
        if unit:
            entry["unit"] = unit
        if entry.get("grams") is None and entry.get("quantity") is None:
            entry["grams"] = 100
        out.append(entry)
    return out


_FIELD_WORDS = {
    "limit", "query", "meal_type", "items", "notes", "eaten_at", "food",
    "grams", "quantity", "unit", "display_name", "name", "value",
}


_SCHEMA_MARKERS = {"$schema", "title", "type", "description", "default", "properties", "required", "items", "enum"}


def _is_schema_echo(v, _depth=0):
    """True when the model echoed the tool's JSON Schema back instead of data.

    Observed: {"$schema": {"type": "string", "title": "Food", ...}, "title": "Food"}
    Such a payload must never be treated as a food name or written to the record.
    """
    if _depth > 5:
        return False
    if isinstance(v, dict):
        keys = {k.lower() for k in v.keys() if isinstance(k, str)}
        if any(k.startswith("$") for k in keys):
            return True
        # A dict that is only schema vocabulary carries no user data.
        if keys and keys <= _SCHEMA_MARKERS:
            return True
        return any(_is_schema_echo(val, _depth + 1) for val in v.values())
    if isinstance(v, (list, tuple)):
        return any(_is_schema_echo(i, _depth + 1) for i in v)
    if isinstance(v, str):
        return v.strip().lower() in ("$schema", "title", "type", "description", "properties")
    return False


def _unwrap_schema_echo(v, _depth=0):
    """Extract real values from schema-echo wrappers.

    Qwen sometimes wraps values as {"type": "string", "description": "<actual>"}.
    This pulls the actual data out.
    """
    if _depth > 5:
        return v
    if isinstance(v, dict):
        keys = {k.lower() for k in v.keys() if isinstance(k, str)}
        if keys & {"type", "description", "default", "__type__", "__type"} and len(keys) <= 3:
            for k in ("description", "default", "value", "example"):
                if k in v:
                    return _unwrap_schema_echo(v[k], _depth + 1)
        return {k: _unwrap_schema_echo(val, _depth + 1) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_unwrap_schema_echo(i, _depth + 1) for i in v]
    return v


def _plausible_food_name(v):
    """True when v yields a real food name, not schema vocabulary or numeric junk."""
    if _is_schema_echo(v):
        return False
    t = _as_text(v)
    if not t:
        return False
    low = t.strip().lower().lstrip("$")
    if low in _FIELD_WORDS or low in _SCHEMA_MARKERS:
        return False
    if low.replace(".", "", 1).replace(",", "", 1).isdigit():
        return False
    return any(ch.isalpha() for ch in t)


def _plausible_food_text(v):
    """True when v looks like a human food description.

    Looser than _plausible_food_name: a full sentence with digits and units
    ("3 whole eggs, 2 slices of toast") is exactly what we want here. Still
    rejects schema echoes and bare schema vocabulary.
    """
    if _is_schema_echo(v):
        return False
    t = _as_text(v)
    if not t:
        return False
    low = t.strip().lower().lstrip("$")
    if low in _FIELD_WORDS or low in _SCHEMA_MARKERS:
        return False
    # Needs at least one run of letters to be a food description.
    return bool(re.search(r"[a-z]{2,}", low))


def _strip_schema_noise(v, _depth=0):
    """Recover any genuine free text hiding inside a schema-echo payload.

    When the model copies its own inputSchema back, the only human-authored
    string present is usually the example inside a description. That is not the
    user's meal, so it must NOT be logged -- this returns "" for pure echoes and
    only yields text that isn't schema vocabulary.
    """
    if _depth > 5:
        return ""
    if isinstance(v, str):
        low = v.strip().lower()
        if low in _FIELD_WORDS or low in _SCHEMA_MARKERS:
            return ""
        if low in ("string", "number", "integer", "object", "array", "boolean"):
            return ""
        return v.strip()
    if isinstance(v, dict):
        # Skip description/title/type keys: those are schema, never user data.
        for k, val in v.items():
            if isinstance(k, str) and k.lower() in _SCHEMA_MARKERS:
                continue
            got = _strip_schema_noise(val, _depth + 1)
            if got:
                return got
        return ""
    if isinstance(v, (list, tuple)):
        for i in v:
            got = _strip_schema_noise(i, _depth + 1)
            if got:
                return got
    return ""


def _valid_meal_type(v):
    return isinstance(v, str) and v.strip().lower() in (
        "breakfast", "lunch", "dinner", "snack", "drink",
    )


def _looks_like_items(v):
    if _is_schema_echo(v):
        return False
    if isinstance(v, list):
        return any(isinstance(i, (dict, str)) for i in v)
    if isinstance(v, dict):
        return any(k in v for k in ("display_name", "name", "food"))
    return False


def _rescue(args, spec, _depth=0):
    """Hoist schema fields out of a mis-nested argument payload.

    Small models frequently bury the whole payload under one schema key, e.g.
      {"meal_type": {"items": [...], "eaten_at": "...", "notes": "..."}}
      {"eaten_at":  {"items": [...], "meal_type": "lunch"}}
    Rather than reject these, search the payload (breadth-first, bounded) for
    each expected field and take the first value that passes its validator.
    Correctly-shaped payloads are returned untouched.

    `spec` maps field name -> validator(value) -> bool.
    """
    found = {}
    queue = [(args, 0)]
    while queue:
        node, depth = queue.pop(0)
        if depth > 6 or not isinstance(node, dict):
            continue
        for field, ok in spec.items():
            if field in found:
                continue
            if field in node and ok(node[field]):
                found[field] = node[field]
        for val in node.values():
            if isinstance(val, dict):
                queue.append((val, depth + 1))
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        queue.append((item, depth + 1))
    return found


_DATE_HINT_RE = re.compile(
    r"\b(?:yesterday|yday|today|tonight|last\s+week|day\s+before\s+yesterday|"
    r"\d+\s+(?:day|days|week|weeks)\s+ago|this\s+(?:morning|afternoon|evening)|"
    r"last\s+\w+day|(?:mon|tues?|wednes|thurs?|fri|satur|sun)day|"
    r"\d{4}-\d{2}-\d{2})\b",
    re.I,
)


async def _recover_date_phrase(text, conversation_id=""):
    """Prepend the user's own date words when the model dropped them.

    Qwen paraphrases on tool calls and often discards the day ("On Monday for
    dinner I had X" -> "X"), which would log the meal against today. The shim
    stashes the raw user turn in Redis; if our text carries no date hint but
    theirs does, restore the original sentence instead of guessing.

    conversation_id is the shim's stable conversation key. When present we read
    the scoped key; when absent we SKIP recovery (never fall back to a global
    key — that would leak state across concurrent users).
    """
    if _DATE_HINT_RE.search(text):
        return text
    if not REDIS_URL or not conversation_id:
        return text
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(REDIS_URL, decode_responses=True)
        raw = await r.get(f"tfshim:last_user_text:{conversation_id}")
        await r.aclose()
    except Exception:
        return text
    if not raw or not _DATE_HINT_RE.search(raw):
        return text
    print(f"[log_meal] recovered date phrase from user turn: {raw[:120]!r}", flush=True)
    return raw




_MACRO_LINE_RE = re.compile(
    r"(?P<key>kcal|calorie[s]?|protein[s]?|carb[s]?|carbohydrate[s]?|fat[s]?|fiber[s]?|fibre[s]?|sodium[mg]*)"
    r"\D{0,20}(?P<val>\d+(?:[.,]\d+)?)", re.I)


def _parse_macros_from_text(text):
    """Extract kcal/protein/carbs/fat/fiber/sodium from a free-text sentence like
    '100 grams of su\u0161ena vratina contains approximately 324 calories ... 34 g protein'.
    Returns (per100g: dict, per_serving_grams: float|None)."""
    if not text:
        return {}, None
    per100 = {}
    grams = None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:g|gram|grams)\b", text, re.I)
    if m and not re.search(r"\bper\s*100", text, re.I):
        g = float(m.group(1).replace(",", "."))
        if 10 <= g <= 2000:
            grams = g
    for m in _MACRO_LINE_RE.finditer(text):
        key = m.group("key").lower()
        val = float(m.group("val").replace(",", "."))
        if key.startswith("kcal") or key.startswith("calorie"):
            per100.setdefault("kcal", val)
        elif key.startswith("protein"):
            per100.setdefault("protein_g", val)
        elif key.startswith("carb"):
            per100.setdefault("carbs_g", val)
        elif key.startswith("fat"):
            per100.setdefault("fat_g", val)
        elif key.startswith("fiber") or key.startswith("fibre"):
            per100.setdefault("fiber_g", val)
        elif key.startswith("sodium"):
            per100.setdefault("sodium_mg", val if val > 5 else val * 1000)
    # Values in the sentence may be per-serving; normalise to per-100g when we
    # know the serving size the user mentioned (e.g. "per 100 g" vs "100 g of").
    if grams and grams != 100 and per100:
        f = 100.0 / grams
        per100 = {k: round(v * f, 1) for k, v in per100.items()}
    return per100, grams


def _looks_like_macro_dump(text):
    """True when a sentence looks like the user dictating nutrition values."""
    if not text:
        return False
    low = text.lower()
    return bool(_MACRO_LINE_RE.search(low)) and any(
        w in low for w in ("kcal", "calorie", "protein", "carb", "fat", "sodium"))


def _learn_food_name_from_text(text):
    """Best-effort food-name extraction from a macro-dump sentence: the subject
    before 'contains/is/has' or after 'for'/'of', e.g.
    'su\u0161ena vratina contains 324 kcal ...' -> 'su\u0161ena vratina'."""
    if not text:
        return ""
    t = re.sub(r"^\[[^\]]*\]\s*", "", text.strip())
    t = re.sub(r"^(?:the\s+)?(?:food\s+)?", "", t, flags=re.I)
    m = re.search(r"^(.{3,60}?)\s+(?:contains|is|has|have)\b", t, flags=re.I)
    if m:
        return m.group(1).strip(" \"'.,").strip()
    m = re.search(r"\bfor\s+(?:100\s*g(?:rams)?\s+of\s+)?([a-z\u0160\u0161\u017d\u017e\u0106\u0107 \-]{3,60})", t, flags=re.I)
    if m:
        return m.group(1).strip(" \"'.,").strip()
    return ""

def _extract_correction_pairs(text):
    """Parse '<wrong> to <right>' / '<wrong> with <right>' pairs from the user's
    correction sentence. Handles a leading '[date]' prefix, curly/smart quotes,
    surrounding quotes, and a leading directive verb, e.g.
    '[Sun 2026-08-30 16:26 UTC] \u201cCorrect Fat, chicken to corn bread and
    ofvementaler to emmental cheese\u201d'
    -> [('Fat, chicken','corn bread'), ('ofvementaler','emmental cheese')]."""
    if not text:
        return []
    # 1. Normalize curly/smart quotes to ASCII.
    t = (text
         .replace("\u201c", "\"")
         .replace("\u201d", "\"")
         .replace("\u2018", "'")
         .replace("\u2019", "'"))
    t = t.strip()
    # 2. Strip a leading '[date]' prefix the shim prepends.
    t = re.sub(r"^\[[^\]]*\]\s*", "", t).strip()
    # 3. Strip any surrounding quotes BEFORE removing the directive verb.
    t = t.strip("\"'")
    # 4. Remove a leading directive verb.
    t = re.sub(r"^(?:please\s+)?(?:correct|replace|change)\b", "", t, flags=re.I).strip()
    pairs = []
    clauses = re.split(r"\s+(?:and|then)\s+|;\s*", t)
    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        m = re.search(r"(.+?)\s+(?:to|with|->)\s+(.+)", clause, flags=re.I)
        if m:
            a = m.group(1).strip().strip("\"'.,")
            b = m.group(2).strip().strip("\"'.,")
            if a and b and a.lower() not in ("the", "this", "it", "item", "one", "meal"):
                pairs.append((a, b))
    return pairs


_WEIGHT_WORDS = {
    "g", "gr", "gram", "grams", "gramme", "grammes",
    "kg", "kilo", "kilos", "kilogram", "kilograms",
}

# Directive verbs that appear in EDIT/instruction sentences (not meal content).
_EDIT_LEAD_RE = re.compile(
    r"^(?:please\s+)?(?:change|update|fix|correct|adjust|make|set|remove|delete|add|log|note)\b",
    re.I,
)
# Correction markers that signal 'fix what I logged', never a fresh meal.
_CORRECTION_MARKER_RE = re.compile(
    r"(weight|portion|grams?|should\s+be|instead\s+of|was\s+wrong|to\s+\d+\s*(?:g|grams?|kg))",
    re.I,
)

_DISALLOWED_FRAG = {
    "the", "this", "it", "its", "item", "weight", "portion", "portion size",
    "the steak", "steak weight", "meal", "portions",
}

# Weight-edit verbs (a strict subset of _EDIT_LEAD_RE: only those that signal
# an EDIT of an existing log, never a fresh meal).
_WEIGHT_EDIT_VERB_RE = re.compile(
    r"^(?:please\s+)?(?:change|update|fix|set|make|correct|adjust)\b", re.I)


def _extract_weight_correction(text):
    """Parse a WEIGHT-EDIT instruction from the user's sentence.

    Returns (item_fragment, grams) or None. Handles a leading '[date]' prefix
    and curly/smart quotes. Examples:
      'Change the steak weight to 200g'  -> ('steak', 200)
      'make the steak 200 grams'         -> ('steak', 200)
      'steak should be 200g'             -> ('steak', 200)
      'set steak weight to 200'          -> ('steak', 200)

    STRICT: it must fire ONLY on clear edit intent. A fresh meal sentence like
    'I had 200g of steak and 3 eggs for lunch' must return None — otherwise the
    shim-Redis recovery would hijack a meal-to-log as a weight edit. Edit intent
    = a leading weight-edit verb (change/update/fix/set/make/correct/adjust) OR
    an explicit weight/portion/should-be phrase."""
    if not text:
        return None
    t = (text
         .replace("\u201c", "\"")
         .replace("\u201d", "\"")
         .replace("\u2018", "'")
         .replace("\u2019", "'"))
    t = re.sub(r"^\[[^\]]*\]\s*", "", t).strip()
    t = t.strip("\"'")
    if not t:
        return None
    had_verb = bool(_WEIGHT_EDIT_VERB_RE.match(t))
    t2 = _WEIGHT_EDIT_VERB_RE.sub("", t, count=1).strip()
    low = t2.lower()
    # Discriminator: is this an edit, not a fresh meal? 'I had 200g of steak'
    # leads with 'I had' (no edit verb) and has no weight/portion/should-be.
    if not (had_verb or re.search(r"\b(weight|portion|should\s+be)\b", low)):
        return None
    grams_str = None
    unit = ""
    item = None
    # A) '<item> weight to Ng' / '<item> portion to Ng'  (item precedes the word)
    m = re.search(
        r"(.+?)\s+(?:weight|portion)(?:\s+size)?\s+to\s+"
        r"(\d+(?:[.,]\d+)?)\s*(g|gr|grams?|grammes?|kg|kilos?|kilograms?)?\b",
        t2, re.I)
    if not m:
        # B) 'weight of <item> to Ng'
        m = re.search(
            r"\bweight\s+of\s+(.+?)\s+to\s+"
            r"(\d+(?:[.,]\d+)?)\s*(g|gr|grams?|grammes?|kg|kilos?|kilograms?)?\b",
            t2, re.I)
    if not m:
        # C) '<item> should be Ng' / '<item> to Ng' / '<item> Ng' (numeric+unit)
        m = re.search(
            r"(.+?)\s+(?:should\s+be|to|at|=|:)?\s*"
            r"(\d+(?:[.,]\d+)?)\s*(g|gr|grams?|grammes?|kg|kilos?|kilograms?)\b",
            t2, re.I)
    if not m:
        return None
    item = m.group(1).strip()
    grams_str = m.group(2)
    unit = (m.group(3) or "").lower()
    try:
        g = float(grams_str.replace(",", "."))
    except Exception:
        return None
    if unit in ("kg", "kilos", "kilo", "kilograms", "kilogram"):
        g = g * 1000
    if not (1 < g <= 10000):
        return None
    # Clean the item fragment down to a food name.
    item = item.strip().strip("\"'.,")
    item = re.sub(r"^(?:the|a|an)\s+", "", item, flags=re.I).strip()
    item = re.sub(r"\s+(?:weight|portion)(?:\s+size)?$", "", item).strip()
    item = re.sub(r"^(?:of|from)\s+", "", item).strip()
    if not item or item.lower() in _DISALLOWED_FRAG:
        return None
    return (item, g)


def _looks_like_edit_instruction(text):
    """True when a user turn is an EDIT instruction, not a fresh meal to log.

    This is the guard against the shim-Redis recovery logging a directive like
    'Change the steak weight to 200g' as a blank 0-kcal meal. A sentence that
    leads with an edit verb AND carries a weight/portion/correction marker is
    an instruction about an EXISTING log, so it must never be fed to the meal
    parser as food text."""
    if not text:
        return False
    t = (text
         .replace("\u201c", "\"")
         .replace("\u201d", "\"")
         .replace("\u2018", "'")
         .replace("\u2019", "'"))
    t = re.sub(r"^\[[^\]]*\]\s*", "", t).strip().strip("\"'")
    if not t:
        return False
    if not _EDIT_LEAD_RE.match(t):
        return False
    # Must be a genuine correction marker, not merely 'log 200g of steak'.
    return bool(_CORRECTION_MARKER_RE.search(t))



def _deep_str(v):
    seen = 0
    while isinstance(v, dict) and seen < 5:
        keys = list(v.keys())
        if len(keys) == 1:
            v = v[keys[0]]
            seen += 1
        else:
            break
    if isinstance(v, str):
        return v.strip()
    return ""


async def _resolve_meal_id(raw_id: str) -> str:
    """If meal_id is truncated/partial, resolve to the full UUID."""
    raw_id = (raw_id or '').strip()
    if len(raw_id) >= 32:
        return raw_id
    try:
        out = await call_api("GET", f"/meals/resolve/{raw_id}")
        data = json.loads(out)
        if data.get("ok"):
            return data["meal_id"]
    except Exception:
        pass
    return raw_id


async def handle_mcp(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400)
    
    method = body.get("method", "")
    req_id = body.get("id")
    params = body.get("params", {})
    
    # Initialize — create session
    if method == "initialize":
        import uuid
        sid = str(uuid.uuid4()).replace("-", "")[:32]
        _SESSIONS[sid] = {"initialized": True}
        headers = {"mcp-session-id": sid}
        return JSONResponse(
            {"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "drhiro-health", "version": "1.0.0"}}},
            headers=headers
        )
    
    # Session is advisory, not enforced — TrueForge may operate statelessly.
    sid = request.headers.get("mcp-session-id", "")
    if sid and sid not in _SESSIONS:
        _SESSIONS[sid] = {"initialized": True}
    
    if method == "notifications/initialized":
        return JSONResponse({"jsonrpc": "2.0"}, status_code=202)
    
    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
    
    if method == "tools/call":
        tool_name = params.get("name", "")
        args = _unwrap(params.get("arguments", {}))
        if not isinstance(args, dict):
            args = {}
        print(f"[tools/call] name={tool_name!r} args={json.dumps(args, default=str)[:300]}", flush=True)
        # T1: the trusted worker owns consumption. Disable the model's
        # consumption-writing tools when active.
        if consumption_writers_disabled() and tool_name in _CONSUMPTION_WRITER_TOOLS:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": req_id, "result": _writer_disabled_response(tool_name)}
            )
        try:
            if tool_name == "get_steps":
                text = await call_api("GET", "/dashboard/today")
            elif tool_name == "log_water":
                args = _unwrap_schema_echo(args)
                amount = args.get("amount_ml") or args.get("amount") or args.get("ml")
                if isinstance(amount, dict) or (isinstance(amount, str) and _is_schema_echo(amount)):
                    amount = None
                if amount is None:
                    text_in = _as_text(args.get("text"))
                    if text_in:
                        import re
                        m = re.search(r'(\d+(?:[.,]\d+)?)\s*(ml|l|glass)', text_in, re.I)
                        if m:
                            val = float(m.group(1).replace(',', '.'))
                            if m.group(2).lower() == 'l':
                                val = val * 1000
                            elif 'glass' in m.group(2).lower():
                                val = val * 200
                            amount = val
                conversation_id = _as_text(args.get("conversation_id"))
                if amount is None and REDIS_URL and conversation_id:
                    try:
                        import redis.asyncio as aioredis
                        rcli = aioredis.from_url(REDIS_URL, decode_responses=True)
                        raw = await rcli.get(f"tfshim:last_user_text:{conversation_id}")
                        await rcli.aclose()
                        if raw:
                            import re
                            t = raw.replace('\u201c', '"').replace('\u201d', '"') \
                                   .replace('\u2018', "'").replace('\u2019', "'")
                            t = re.sub(r"^\[[^\]]*\]\s*", "", t)
                            m = re.search(r"(\d+(?:[.,]\d+)?)\s*(ml|milliliter|millilitre|liter|litre|l)\b", t, re.I)
                            if m:
                                val = float(m.group(1).replace(',', '.'))
                                u = m.group(2).lower()
                                if u == 'l' or u.startswith('liter') or u.startswith('litre'):
                                    val = val * 1000
                                amount = val
                            else:
                                g = re.search(r"(\d+)\s*glass(?:es)?\b", t, re.I)
                                if g:
                                    amount = float(g.group(1)) * 200
                            if amount:
                                print(f"[log_water] recovered: amount={amount} ml", flush=True)
                    except Exception:
                        pass
                if amount is None:
                    text = json.dumps({"ok": False, "error": "need_amount",
                                       "message": "How much water? Tell me the amount in ml or glasses."})
                else:
                    try:
                        amount = float(str(amount).replace(',', '.'))
                    except (TypeError, ValueError):
                        amount = None
                if amount is not None and amount > 0:
                    # Route through the configured API using the shared auth
                    # context (TOKEN / API_BASE) — never mint a hard-coded user JWT.
                    out = await call_api("POST", "/ingest/manual/water", {"amount_ml": amount})
                    try:
                        ok = (json.loads(out) or {}).get("ok")
                    except Exception:
                        ok = out.startswith("{") and "error" not in out
                    if not ok:
                        # Fail closed: surface the backend error, don't fabricate success.
                        try:
                            err = (json.loads(out) or {}).get("error")
                        except Exception:
                            err = None
                        text = json.dumps({"ok": False, "error": err or "log_failed",
                                           "message": "Could not log water right now. Please try again."})
                        print(f"[log_water] api error: {err}", flush=True)
                    else:
                        text = json.dumps({"ok": True,
                                           "message": f"Logged {int(amount)} ml of water."})
                        print(f"[log_water] logged {amount} ml", flush=True)
                else:
                    text = json.dumps({"ok": False, "error": "need_amount",
                                       "message": "How much water? Tell me the amount in ml or glasses."})

            elif tool_name == "log_liquid":
                # Reuse log_water's amount recovery, but add a category.
                args = _unwrap_schema_echo(args)
                amount = args.get("amount_ml") or args.get("amount") or args.get("ml")
                if isinstance(amount, dict) or (isinstance(amount, str) and _is_schema_echo(amount)):
                    amount = None
                cat_raw = args.get("category") or args.get("type") or "water"
                if isinstance(cat_raw, dict):
                    cat_raw = "water"
                category = str(cat_raw).strip().lower()
                _LIQUID_CATS = ["water", "non_alcoholic", "beer", "wine", "spirits", "other_alcohol"]
                if category not in _LIQUID_CATS:
                    category = "water"
                if amount is None:
                    text_in = _as_text(args.get("text"))
                    if text_in:
                        import re
                        # detect category from the phrase
                        for c, pat in [
                            ("spirits", r"whiskey|whisky|viski|vodka|votka|rum|gin|brandy|rakija|konjak|tequila"),
                            ("wine", r"wine|vino|rose|prosecco|champagne|šampanjac"),
                            ("beer", r"beer|pivo|lager|ale|stout|heineken|ozujsko|karlova"),
                            ("other_alcohol", r"cocktail|koktel|cider|liqueur|liker|aperol|martini|mojito"),
                            ("non_alcoholic", r"coffee|kava|tea|čaj|juice|sok|soda|cola|cola|coke|milk|energy|redbull|fanta|sprite"),
                        ]:
                            if re.search(pat, text_in, re.I):
                                category = c
                                break
                        m = re.search(r'(\d+(?:[.,]\d+)?)\s*(ml|l|glass)', text_in, re.I)
                        if m:
                            val = float(m.group(1).replace(',', '.'))
                            if m.group(2).lower() == 'l':
                                val = val * 1000
                            elif 'glass' in m.group(2).lower():
                                val = val * 200
                            amount = val
                conversation_id = _as_text(args.get("conversation_id"))
                if amount is None and REDIS_URL and conversation_id:
                    try:
                        import redis.asyncio as aioredis
                        rcli = aioredis.from_url(REDIS_URL, decode_responses=True)
                        raw = await rcli.get(f"tfshim:last_user_text:{conversation_id}")
                        await rcli.aclose()
                        if raw:
                            import re
                            t = raw.replace('\u201c', '"').replace('\u201d', '"') \
                                   .replace('\u2018', "'").replace('\u2019', "'")
                            t = re.sub(r"^\[[^\]]*\]\s*", "", t)
                            # category from phrase
                            for c, pat in [
                                ("spirits", r"whiskey|whisky|viski|vodka|votka|rum|gin|brandy|rakija|konjak|tequila"),
                                ("wine", r"wine|vino|rose|prosecco|champagne|šampanjac"),
                                ("beer", r"beer|pivo|lager|ale|stout|heineken|ozujsko|karlova"),
                                ("other_alcohol", r"cocktail|koktel|cider|liqueur|liker|aperol|martini|mojito"),
                                ("non_alcoholic", r"coffee|kava|tea|čaj|juice|sok|soda|cola|cola|coke|milk|energy|redbull|fanta|sprite"),
                            ]:
                                if re.search(pat, t, re.I):
                                    category = c
                                    break
                            m = re.search(r"(\d+(?:[.,]\d+)?)\s*(ml|milliliter|millilitre|liter|litre|l)\b", t, re.I)
                            if m:
                                val = float(m.group(1).replace(',', '.'))
                                u = m.group(2).lower()
                                if u == 'l' or u.startswith('liter') or u.startswith('litre'):
                                    val = val * 1000
                                amount = val
                            else:
                                g = re.search(r"(\d+)\s*glass(?:es)?\b", t, re.I)
                                if g:
                                    amount = float(g.group(1)) * 200
                            if amount:
                                print(f"[log_liquid] recovered: amount={amount} ml cat={category}", flush=True)
                    except Exception:
                        pass
                if amount is None:
                    text = json.dumps({"ok": False, "error": "need_amount",
                                       "message": "How much did you drink? Tell me the amount in ml or glasses."})
                else:
                    try:
                        amount = float(str(amount).replace(',', '.'))
                    except (TypeError, ValueError):
                        amount = None
                if amount is not None and amount > 0:
                    # Route through the configured API using the shared auth
                    # context (TOKEN / API_BASE) — never mint a hard-coded user JWT.
                    # The /ingest/manual/water endpoint accepts a category field and
                    # writes a Measurement row with the liquid category. This is the
                    # SAME backend path the meal confirm uses for linked beverages,
                    # so a manual log_liquid for a drink that was also in a meal
                    # resolves to the same consumption item identity (no double count).
                    out = await call_api("POST", "/ingest/manual/water",
                                        {"amount_ml": amount, "category": category})
                    try:
                        ok = (json.loads(out) or {}).get("ok")
                    except Exception:
                        ok = out.startswith("{") and "error" not in out
                    if not ok:
                        try:
                            err = (json.loads(out) or {}).get("error")
                        except Exception:
                            err = None
                        text = json.dumps({"ok": False, "error": err or "log_failed",
                                           "message": "Could not log that right now. Please try again."})
                        print(f"[log_liquid] api error: {err}", flush=True)
                    else:
                        text = json.dumps({"ok": True,
                                           "message": f"Logged {int(amount)} ml of {category}."})
                        print(f"[log_liquid] logged {amount} ml cat={category}", flush=True)
                else:
                    text = json.dumps({"ok": False, "error": "need_amount",
                                       "message": "How much did you drink? Tell me the amount in ml or glasses."})

            elif tool_name == "log_activity":
                args = _unwrap_schema_echo(args)
                title = _as_text(args.get("title")) or _as_text(args.get("activity"))
                desc = _as_text(args.get("description")) or ""
                kcal = args.get("calories_burned") or args.get("calories") or args.get("kcal")
                date = _as_text(args.get("activity_date")) or ""
                if kcal is not None:
                    try:
                        kcal = float(kcal)
                    except (TypeError, ValueError):
                        kcal = None
                
                # Schema-echo recovery: when args are empty, recover from the
                # user's message stored in Redis by the shim.
                # _as_text on {"__type":"string"} returns "__type" (a dict key),
                # which is non-empty and would block recovery -- clear it first.
                if title and title.strip().lower() in ('__type', '__type__', 'string', 'number', 'title', 'type', 'description', 'properties', 'required', 'default', 'example'):
                    title = ''
                conversation_id = _as_text(args.get("conversation_id"))
                if (not title or kcal is None) and REDIS_URL and conversation_id:
                    try:
                        import redis.asyncio as aioredis
                        rcli = aioredis.from_url(REDIS_URL, decode_responses=True)
                        raw = await rcli.get(f"tfshim:last_user_text:{conversation_id}")
                        await rcli.aclose()
                        if raw:
                            import re
                            t = raw.replace('\u201c', '"').replace('\u201d', '"') \
                                   .replace('\u2018', "'").replace('\u2019', "'")
                            t = re.sub(r"^\[[^\]]*\]\s*", "", t)
                            t = re.sub(r"^(?:please\s+)?(?:log|track|add|record|note|save)\s+",
                                       "", t, flags=re.I)
                            kcal_m = re.search(r"(\d{2,5})\s*kcal\b", t, re.I)
                            if kcal_m:
                                kcal = float(kcal_m.group(1))
                            min_m = re.search(r"(\d+)\s*(?:min(?:utes)?|mins|hrs?|hours?)\b", t, re.I)
                            if min_m and kcal is None:
                                kcal = float(min_m.group(1)) * 4
                            if not title:
                                tt = re.sub(r"\d{2,5}\s*kcal\b", " ", t, flags=re.I)
                                tt = re.sub(r"\d+\s*(?:min(?:utes)?|mins|hrs?|hours?)\b", " ", tt, flags=re.I)
                                tt = re.sub(r"[~,:\-\u2013\u2014]+", " ", tt)
                                tt = re.sub(r"\s+", " ", tt).strip()
                                tt = re.sub(r"^(?:of|for)\s+", "", tt)
                                tt = re.sub(r"\s+(?:please|pls|thanks|thank\s+you)\s*$", "", tt, flags=re.I)
                                if tt and tt.lower() not in ('__type', '__type__', 'string', 'number', 'the', 'this', 'it'):
                                    title = tt
                            if title and title.lower() in ('__type', '__type__', 'string', 'number'):
                                title = ''
                            if title or kcal:
                                print(f"[log_activity] recovered: title={title!r} kcal={kcal}", flush=True)
                    except Exception:
                        pass
                if not title or kcal is None or kcal <= 0:
                    text = json.dumps({"ok": False, "error": "need_title_and_kcal",
                                       "message": "I need the activity name and calories burned. e.g. 'Log 30 min gardening, 150 kcal'"})
                else:
                    body = {"title": title, "calories_burned": kcal}
                    if desc: body["description"] = desc
                    if date: body["activity_date"] = date
                    out = await call_api("POST", "/activities", body)
                    try:
                        ok = (json.loads(out) or {}).get("ok")
                    except Exception:
                        ok = out.startswith("{") and "error" not in out
                    if not ok:
                        try:
                            err = (json.loads(out) or {}).get("error")
                        except Exception:
                            err = None
                        text = json.dumps({"ok": False, "error": err or "log_failed",
                                           "message": "Could not log the activity right now."})
                        print(f"[log_activity] api error: {err}", flush=True)
                    else:
                        text = json.dumps({"ok": True,
                                           "message": f"Logged {title}: {int(kcal)} kcal burned."})
                        print(f"[log_activity] logged {title} {kcal} kcal", flush=True)

            elif tool_name == "learn_food":
                args = _unwrap_schema_echo(args)
                name = _as_text(args.get("name")) or _as_text(args.get("food")) or _as_text(args.get("display_name"))
                text_in = _as_text(args.get("text"))
                if text_in and _looks_like_macro_dump(text_in):
                    parsed, grams = _parse_macros_from_text(text_in)
                    if not name:
                        name = _learn_food_name_from_text(text_in)
                    for k, v in parsed.items():
                        args.setdefault(k, v)
                per100 = {}
                for k in ("kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "sodium_mg"):
                    v = args.get(k + "_per_100g", args.get(k))
                    if v is not None:
                        try:
                            per100[k] = float(v)
                        except (TypeError, ValueError):
                            pass
                if not name or not per100:
                    text = json.dumps({"ok": False, "error": "need_name_and_macros",
                                       "message": "Tell me the food name and at least one value (kcal/protein/carbs/fat per 100g) and I will remember it."})
                else:
                    try:
                        out = await call_api("POST", "/meals/learn", {
                            "display_name": name, **{k + "_per_100g": v for k, v in per100.items()}
                        })
                        ok = (json.loads(out) or {}).get("ok")
                    except Exception as e:
                        ok = False
                        print(f"[learn_food] api error: {e}", flush=True)
                    if ok:
                        pretty = ", ".join((f"{v} kcal" if k == "kcal" else f"{k.replace('_g','')}={v}g") for k, v in per100.items())
                        saved_msg = "Saved " + name + " for future matches (" + pretty + " per 100 g). I will use these values from now on."
                        text = json.dumps({"ok": True, "message": saved_msg})
                        print(f"[learn_food] stored {name!r} {per100}", flush=True)
                    else:
                        text = json.dumps({"ok": False, "error": "learn_failed",
                                           "message": "I could not save that right now. Please try again."})

            elif tool_name == "analyze_food_photo":
                import base64 as _b64
                import glob as _glob
                args = _unwrap_schema_echo(args)
                photo_path = _as_text(args.get("photo_path")) or _as_text(args.get("image_path"))
                learn = args.get("learn", True)
                grams = args.get("grams")
                candidates = []
                if photo_path:
                    candidates = _glob.glob(photo_path) or ([photo_path] if str(photo_path).startswith("/home/node/") else [])
                if not candidates:
                    staged = sorted(_glob.glob(MEDIA_ROOT + "/openclaw-staged-*/*.jpg")
                                    + _glob.glob(MEDIA_ROOT + "/openclaw-staged-*/*.jpeg")
                                    + _glob.glob(MEDIA_ROOT + "/openclaw-staged-*/*.png"),
                                    key=lambda p: __import__("os").path.getmtime(p))
                    candidates = staged[-1:] if staged else []
                if not candidates or not __import__("os").path.exists(candidates[0]):
                    text = json.dumps({"ok": False, "error": "no_photo",
                                       "message": "I could not find the photo file. Please resend the image."})
                else:
                    photo = candidates[0]
                    try:
                        img = _b64.b64encode(open(photo, "rb").read()).decode()
                        payload = {
                            "model": VISION_MODEL,
                            "messages": [{"role": "user", "content": [
                                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img}},
                                {"type": "text", "text": "IMPORTANT: Read the nutrition table EXACTLY as printed on the label. Do NOT guess or estimate. Look for the table with headers per 100 g. Read each number character by character. If the label shows kJ, also convert to kcal (divide by 4.184). If the label shows salt, convert to sodium (salt_mg / 2.5 = sodium_mg). Extract: energy kcal, protein g, carbohydrate g, fat g, fiber g, sodium mg. Return ONLY minified JSON. Return ONLY minified JSON: {\"product\": string, \"per100g\": {\"kcal\": number, \"protein_g\": number, \"carbs_g\": number, \"fat_g\": number, \"fiber_g\": number, \"sodium_mg\": number}, \"serving_g\": number|null}. Convert kJ to kcal (divide by 4.184) when kcal is not printed. Convert salt to sodium (x400) when only salt is printed. No markdown."}
                            ]}],
                            "max_tokens": 300, "temperature": 0.1, "stream": False,
                            "chat_template_kwargs": {"enable_thinking": False},
                        }
                        import httpx as _hx
                        vreq = _hx.post(VISION_BASE_URL.rstrip("/") + "/v1/chat/completions",
                                        json=payload, timeout=170)
                        vtxt = (vreq.json()["choices"][0]["message"]["content"] or "").strip()
                        vtxt = re.sub(r"^```(?:json)?|```$", "", vtxt, flags=re.M).strip()
                        vdata = json.loads(vtxt[vtxt.index("{"):vtxt.rindex("}") + 1])
                    except Exception as e:
                        text = json.dumps({"ok": False, "error": "vision_failed",
                                           "message": f"Photo analysis failed: {e}"})
                    else:
                        p100 = vdata.get("per100g") or {}
                        name = (vdata.get("product") or "Unlabeled product").strip()[:120]
                        learned = False
                        if learn and p100:
                            try:
                                lout = await call_api("POST", "/meals/learn", {
                                    "display_name": name,
                                    **{k + "_per_100g": v for k, v in p100.items() if v is not None},
                                })
                                learned = (json.loads(lout) or {}).get("ok")
                            except Exception as e:
                                print(f"[analyze_food_photo] learn failed: {e}", flush=True)
                        g = None
                        try:
                            g = float(grams) if grams is not None else (vdata.get("serving_g") or None)
                        except (TypeError, ValueError):
                            g = vdata.get("serving_g")
                        kcal_portion = round((p100.get("kcal") or 0) * (g or 100) / 100.0, 1) if (p100 and g) else None
                        msg = ("From the label: " + name + " - " + str(p100.get("kcal")) + " kcal, protein "
                               + str(p100.get("protein_g")) + " g, carbs " + str(p100.get("carbs_g")) + " g, fat "
                               + str(p100.get("fat_g")) + " g per 100 g"
                               + (", sodium " + str(p100.get("sodium_mg")) + " mg" if p100.get("sodium_mg") else "")
                               + (". For " + str(g) + " g that is about " + str(kcal_portion) + " kcal." if kcal_portion else ".")
                               + (" Saved for future matches." if learned else ""))
                        text = json.dumps({"ok": True, "product": name, "per100g": p100,
                                           "serving_g": g, "kcal_portion": kcal_portion,
                                           "learned": learned, "photo": photo, "message": msg})

            elif tool_name == "get_daily_metrics":
                text = await call_api("GET", "/dashboard/today")
            elif tool_name == "get_weight":
                text = await call_api("GET", "/dashboard/today")
            elif tool_name == "get_blood_pressure":
                text = await call_api("GET", "/dashboard/today")
            elif tool_name == "get_device_code":
                text = await call_api("POST", "/tools/issue_device_code", {})
            elif tool_name == "get_reminders":
                text = await call_api("GET", "/reminders")
            elif tool_name == "create_reminder":
                text = await call_api("POST", "/reminders", {"type": _as_text(args.get("reminder_type")), "time": _as_text(args.get("time")), "message": _as_text(args.get("message")), "frequency": _as_text(args.get("frequency"), "daily") or "daily"})
            elif tool_name == "get_trends":
                m = quote(_as_text(args.get("metric"), "steps") or "steps")
                text = await call_api("GET", f"/trends?metric={m}&days={int(_as_number(args.get('days'), 30) or 30)}")
            elif tool_name == "log_weight":
                text = await call_api("POST", "/ingest/manual/weight", {"value": _as_number(args.get("value_kg"), 0), "measured_at": args.get("measured_at")})
            elif tool_name == "log_blood_pressure":
                text = await call_api("POST", "/ingest/manual/blood-pressure", {"systolic": int(_as_number(args.get("systolic"), 0)), "diastolic": int(_as_number(args.get("diastolic"), 0)), "pulse": args.get("pulse") and int(_as_number(args.get("pulse"), 0)) or None, "measured_at": args.get("measured_at")})
            elif tool_name == "log_meal":
                args = _unwrap_schema_echo(args)
                rescued = _rescue(args, {
                    "text": _plausible_food_text,
                    "food": _plausible_food_text,
                    "display_name": _plausible_food_text,
                    "items": _looks_like_items,
                    "grams": lambda v: isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().replace(".", "", 1).isdigit()),
                    "meal_type": _valid_meal_type,
                    "eaten_at": lambda v: isinstance(v, str) and v.strip(),
                    "notes": lambda v: isinstance(v, str),
                })

                # Build one plain-text description; the server-side parser does
                # all structuring, so the model never has to emit nested JSON.
                text = _as_text(rescued.get("text") or rescued.get("food") or rescued.get("display_name"))
                # Qwen sometimes SWAPS the values: text={"meal_type":"dinner"},
                # meal_type="<the user's whole sentence>". Recover the sentence
                # from wherever it landed.
                if not text:
                    mt_val = args.get("meal_type") or args.get("text")
                    if isinstance(mt_val, str) and _plausible_food_text(mt_val) and re.search(r"\d|\b(?:had|ate|steak|chicken|egg|bread|rice|salad|cheese|fish|pasta|soup|meat|beef|pork|yogurt|fruit)\b", mt_val, re.I):
                        text = mt_val.strip()
                # Last resort: the shim stashes the user's raw words in Redis.
                conversation_id = _as_text(args.get("conversation_id"))
                if not text and REDIS_URL and conversation_id:
                    try:
                        import redis.asyncio as aioredis
                        rcli = aioredis.from_url(REDIS_URL, decode_responses=True)
                        raw = await rcli.get(f"tfshim:last_user_text:{conversation_id}")
                        await rcli.aclose()
                        if raw and _plausible_food_text(raw) and not _looks_like_edit_instruction(raw):
                            text = raw.strip()
                            print(f"[log_meal] recovered text from shim redis: {text[:120]!r}", flush=True)
                    except Exception:
                        pass
                if not text:
                    salvaged = _normalize_items(rescued.get("items"))
                    if salvaged:
                        parts = []
                        for it in salvaged:
                            nm = it.get("display_name")
                            g = it.get("grams")
                            parts.append(f"{nm} {g:g}g" if nm and g else (nm or ""))
                        text = ", ".join(p for p in parts if p)
                if not text:
                    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps({
                        "error": "no_food_text",
                        "message": (
                            "Nothing was written. You sent the schema instead of a value. "
                            "Call log_meal again where text is the user's own words, verbatim. "
                            "If the user wrote '3 whole egs and tost', send text='3 whole egs and tost'."
                        ),
                        "send_exactly_this_shape": {"text": "<the user's food words here>", "meal_type": "breakfast"},
                    })}], "isError": True}})

                # Append an explicit weight if the model supplied one separately.
                g = _as_number(rescued.get("grams"), None)
                if g and not re.search(r"\d\s*(?:g|gr|gram|grams|kg|ml)\b", text, re.I):
                    text = f"{text} {g:g}g"

                # Restore date words the model dropped while paraphrasing.
                text = await _recover_date_phrase(text, conversation_id)

                payload = {"text": text}
                # Only pass meal_type when the model actually supplied one; an
                # invented default would override the parser's own detection
                # from phrases like "for dinner".
                mt = rescued.get("meal_type")
                if mt:
                    payload["meal_type"] = mt.strip().lower()
                if rescued.get("eaten_at"):
                    payload["eaten_at"] = rescued["eaten_at"]
                print(f"[log_meal] text={text!r} meal_type={payload.get('meal_type')}", flush=True)
                text_out = await call_api("POST", "/meals/from-text", payload)
                # The intelligent service returns a draft; auto-confirm it so
                # the legacy log_meal path also writes the meal immediately.
                try:
                    dd = json.loads(text_out)
                    did = (dd.get("data") or {}).get("draft_id")
                    ditems = (dd.get("data") or {}).get("items") or []
                    if dd.get("ok") and did:
                        sel = [0] * len(ditems)
                        c = await call_api("POST", "/meals/from-text-intelligent/confirm", {"draft_id": did, "selections": sel})
                        try:
                            if json.loads(c).get("ok"):
                                text_out = c
                        except Exception:
                            pass
                except Exception:
                    pass
                # Report back what was actually stored, so the model states facts.
                try:
                    mid = (json.loads(text_out).get("data") or {}).get("meal_id")
                except Exception:
                    mid = None
                if mid:
                    try:
                        detail = await call_api("GET", f"/meals/{mid}")
                        if "detail" not in detail:
                            text_out = detail
                    except Exception:
                        pass
                text = text_out
            elif tool_name == "list_activities":
                _d = _as_text(args.get("date")) or ""
                path = "/activities" + (f"?date={_d}" if _d else "")
                out = await call_api("GET", path)
                try:
                    parsed = json.loads(out)
                    ok = isinstance(parsed, (list, dict)) and "error" not in (parsed if isinstance(parsed, dict) else {})
                except Exception:
                    ok = False
                if ok:
                    text = out
                else:
                    text = json.dumps({"ok": False, "error": "list_failed", "message": "Could not list activities right now."})
            elif tool_name == "update_activity":
                _aid = _as_text(args.get("activity_id")) or _deep_str(args.get("activity_id"))
                if not _aid:
                    text = json.dumps({"ok": False, "error": "missing_activity_id", "message": "Pass activity_id."})
                else:
                    _body = {}
                    for _k in ("title", "description", "calories_burned", "activity_date"):
                        _v = args.get(_k)
                        if _v is None or (isinstance(_v, dict) and (not _v or set(_v.keys()) == {"__type"})):
                            continue
                        if isinstance(_v, dict):
                            _v = _deep_str(_v) or _as_text(_v)
                        if _k == "description" or (_v != ""):
                            _body[_k] = _v
                    out = await call_api("PATCH", f"/data-points/activity/{_aid}", _body)
                    try:
                        ok = (json.loads(out) or {}).get("ok")
                    except Exception:
                        ok = out.startswith("{") and "error" not in out
                    if not ok:
                        try:
                            err = (json.loads(out) or {}).get("error")
                        except Exception:
                            err = None
                        text = json.dumps({"ok": False, "error": err or "update_failed",
                                           "message": "Could not update activity right now."})
                        print(f"[update_activity] api error: {err}", flush=True)
                    else:
                        text = out
            elif tool_name == "list_data_points":
                from datetime import datetime as _dtdt, timezone as _tz
                _q = []
                _mt = _as_text(args.get("metric_type")) or ""
                if _mt:
                    _q.append("metric_type=" + quote(_mt))
                _d = _as_text(args.get("date")) or ""
                if _d:
                    _q.append("from=" + _dtdt.fromisoformat(_d).replace(tzinfo=_tz.utc).isoformat())
                    _q.append("to=" + (_dtdt.fromisoformat(_d).replace(hour=23, minute=59, second=59)).replace(tzinfo=_tz.utc).isoformat())
                path = "/data-points" + (("?" + "&".join(_q)) if _q else "")
                out = await call_api("GET", path)
                try:
                    parsed = json.loads(out)
                    ok = isinstance(parsed, (list, dict)) and "error" not in (parsed if isinstance(parsed, dict) else {})
                except Exception:
                    ok = False
                if ok:
                    text = out
                else:
                    text = json.dumps({"ok": False, "error": "list_failed", "message": "Could not list data points right now."})
            elif tool_name == "update_data_point":
                args = _unwrap_schema_echo(args)
                _pid = _as_text(args.get("id")) or _deep_str(args.get("id"))
                _item = _as_text(args.get("item")) or _deep_str(args.get("item"))
                _grams = args.get("grams")
                if isinstance(_grams, str):
                    try:
                        _grams = float(_grams.replace(",", "."))
                    except Exception:
                        _grams = None
                _meal_id = _as_text(args.get("meal_id")) or _deep_str(args.get("meal_id"))
                conversation_id = _as_text(args.get("conversation_id"))
                # Is this a MEAL ITEM WEIGHT edit (item + grams)? If so route to
                # the meal service's set-weight endpoint (deterministic rescale).
                if _item or _grams:
                    if not _item or not _grams:
                        text = json.dumps({"ok": False, "error": "bad_args",
                                           "message": "For a meal item weight edit pass BOTH item (fragment) and grams."})
                    else:
                        # resolve meal (explicit or latest)
                        meal_id = await _resolve_meal_id(_meal_id)
                        if not meal_id:
                            try:
                                latest = await call_api("GET", "/meals/latest")
                                meal_id = (json.loads(latest).get("data") or {}).get("id") or ""
                            except Exception:
                                meal_id = ""
                        if not meal_id:
                            text = json.dumps({"ok": False, "error": "no_meal", "message": "No logged meal found."})
                        else:
                            text = await call_api("POST", f"/meals/{meal_id}/items/set-weight",
                                                  {"text": _item, "grams": float(_grams)})
                elif not _pid and REDIS_URL and conversation_id:
                    # No id and no item/grams: the model likely emitted a pure
                    # schema echo. Recover a weight-correction sentence
                    # ("change the steak weight to 200g") from the shim Redis key.
                    try:
                        import redis.asyncio as aioredis
                        rcli = aioredis.from_url(REDIS_URL, decode_responses=True)
                        raw = await rcli.get(f"tfshim:last_user_text:{conversation_id}")
                        await rcli.aclose()
                        wc = _extract_weight_correction(raw or "")
                        if wc:
                            _item, _grams = wc
                            meal_id = await _resolve_meal_id(_meal_id)
                            if not meal_id:
                                try:
                                    latest = await call_api("GET", "/meals/latest")
                                    meal_id = (json.loads(latest).get("data") or {}).get("id") or ""
                                except Exception:
                                    meal_id = ""
                            if meal_id:
                                text = await call_api("POST", f"/meals/{meal_id}/items/set-weight",
                                                      {"text": _item, "grams": float(_grams)})
                                print(f"[update_data_point] recovered meal weight edit: {_item!r} -> {_grams}g", flush=True)
                            else:
                                text = json.dumps({"ok": False, "error": "no_meal", "message": "No logged meal found."})
                        else:
                            text = json.dumps({"ok": False, "error": "missing_id", "message": "Pass id, or item+grams for a meal weight edit."})
                    except Exception as e:
                        print(f"[update_data_point] recovery error: {e}", flush=True)
                        text = json.dumps({"ok": False, "error": "missing_id", "message": "Pass id, or item+grams for a meal weight edit."})
                elif not _pid:
                    text = json.dumps({"ok": False, "error": "missing_id", "message": "Pass id, or item+grams for a meal weight edit."})
                else:
                    _body = {}
                    _val = args.get("value")
                    if isinstance(_val, dict) and not (set(_val.keys()) == {"__type"} or _val.get("__type") is not None and set(_val.keys()) == {"__type"}):
                        _body["value"] = _val
                    elif _val not in (None, {}, "", {"__type": None}):
                        _body["value"] = {"value": _val}
                    if args.get("unit") not in (None, "", {"__type": None}):
                        _body["unit"] = _as_text(args.get("unit"))
                    if args.get("measured_at"):
                        _body["measured_at"] = _as_text(args.get("measured_at"))
                    out = await call_api("PATCH", f"/data-points/{_pid}", _body)
                    try:
                        ok = (json.loads(out) or {}).get("ok")
                    except Exception:
                        ok = out.startswith("{") and "error" not in out
                    if not ok:
                        try:
                            err = (json.loads(out) or {}).get("error")
                        except Exception:
                            err = None
                        text = json.dumps({"ok": False, "error": err or "update_failed",
                                           "message": "Could not update data point right now."})
                        print(f"[update_data_point] api error: {err}", flush=True)
                    else:
                        text = out
            elif tool_name == "delete_data_point":
                _pid = _as_text(args.get("id")) or _deep_str(args.get("id"))
                if not _pid:
                    text = json.dumps({"ok": False, "error": "missing_id", "message": "Pass id."})
                else:
                    out = await call_api("DELETE", f"/data-points/{_pid}")
                    try:
                        ok = (json.loads(out) or {}).get("ok")
                    except Exception:
                        ok = "error" not in out
                    if not ok:
                        try:
                            err = (json.loads(out) or {}).get("error")
                        except Exception:
                            err = None
                        text = json.dumps({"ok": False, "error": err or "delete_failed",
                                           "message": "Could not delete data point right now."})
                    else:
                        text = out
            elif tool_name == "delete_activity":
                aid = _as_text(args.get("activity_id")) or _deep_str(args.get("activity_id"))
                if not aid:
                    text = json.dumps({"error": "missing_activity_id", "message": "Pass activity_id to delete."})
                else:
                    out = await call_api("DELETE", f"/activities/{aid}")
                    try:
                        parsed = json.loads(out)
                        ok = parsed.get("ok")
                        if ok:
                            text = out
                        elif parsed.get("error") == "not_found":
                            text = json.dumps({"ok": False, "error": "not_found", "message": "Activity not found or already deleted."})
                        else:
                            err = parsed.get("error")
                            text = json.dumps({"ok": False, "error": err or "delete_failed",
                                               "message": "Could not delete the activity right now."})
                            print(f"[delete_activity] api error: {err}", flush=True)
                    except Exception:
                        text = json.dumps({"ok": False, "error": "delete_failed",
                                           "message": "Could not delete the activity right now."})
            elif tool_name == "delete_meal":
                frag = _as_text(args.get("fragment"))
                match_all = bool(args.get("match_all"))
                if not frag:
                    text = json.dumps({"error": "missing_fragment", "message": "Pass fragment, e.g. 'goulash' or 'Monday dinner'."})
                else:
                    text = await call_api("POST", "/meals/delete-by-fragment", {"text": frag, "match_all": match_all})
            elif tool_name == "set_custom_nutrition":
                item_frag = _as_text(args.get("item")) or _deep_str(args.get("item"))
                mid_val = _deep_str(args.get("meal_id"))
                if mid_val:
                    args["meal_id"] = await _resolve_meal_id(mid_val)
                if not item_frag:
                    text = json.dumps({"error": "missing_item", "message": "Pass item (fragment of logged item name)."})
                else:
                    meal_id = _as_text(args.get("meal_id")) or ""
                    if not meal_id:
                        latest = await call_api("GET", "/meals/latest")
                        try:
                            meal_id = (json.loads(latest).get("data") or {}).get("id") or ""
                        except Exception:
                            meal_id = ""
                    if not meal_id:
                        text = json.dumps({"error": "no_meal", "message": "No logged meal found."})
                    else:
                        # pass per-100g values as query params on the correct path
                        from urllib.parse import urlencode as _ue
                        qp = {k: v for k, v in args.items() if k not in ("item", "meal_id") and v is not None}
                        qs = _ue(qp)
                        text = await call_api("POST", f"/meals/{meal_id}/items/set-custom?item={quote(item_frag)}" + (f"&{qs}" if qs else ""), {"text": item_frag})
            elif tool_name == "correct_meal_item":
                args = _unwrap_schema_echo(args)
                rescued = _rescue(args, {
                    "wrong": _plausible_food_text,
                    "right": _plausible_food_text,
                    "meal_id": lambda v: bool(_as_text(v) or _deep_str(v)),
                })
                wrong = _as_text(rescued.get("wrong")) or _deep_str(rescued.get("wrong"))
                right = _as_text(rescued.get("right")) or _deep_str(rescued.get("right"))
                meal_id = _as_text(rescued.get("meal_id")) or _deep_str(rescued.get("meal_id"))
                conversation_id = _as_text(args.get("conversation_id"))
                meal_id = await _resolve_meal_id(meal_id)
                if not meal_id:
                    try:
                        latest = await call_api("GET", "/meals/latest")
                        meal_id = (json.loads(latest).get("data") or {}).get("id") or ""
                    except Exception:
                        meal_id = ""
                # Pure schema-echo carries no names. Recover the user's
                # correction sentence ("Correct X to Y") from the shim Redis key.
                if (not wrong or not right) and REDIS_URL and conversation_id:
                    try:
                        import redis.asyncio as aioredis
                        rcli = aioredis.from_url(REDIS_URL, decode_responses=True)
                        raw = await rcli.get(f"tfshim:last_user_text:{conversation_id}")
                        await rcli.aclose()
                        pairs = _extract_correction_pairs(raw or "")
                        if pairs:
                            wrong = pairs[0][0]
                            right = pairs[0][1]
                            print(f"[correct_meal_item] recovered from user text: {wrong!r} -> {right!r}", flush=True)
                    except Exception:
                        pass
                if not meal_id or not wrong or not right:
                    text = json.dumps({"error": "bad_args", "message": "need wrong, right, and meal_id (or a latest meal)."})
                else:
                    text = await call_api("POST", f"/meals/{meal_id}/items/correct", {"text": wrong, "meal_type": right})
            elif tool_name == "ddg_nutrition_lookup":
                rescued = _rescue(args, {
                    "query": lambda v: isinstance(_as_text(v), str) and bool(_as_text(v).strip()),
                })
                query = _as_text(rescued.get("query"))
                if not query:
                    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps({
                        "error": "missing_query",
                        "message": "ddg_nutrition_lookup requires 'query' as a plain string, e.g. {\"query\": \"Big Mac\"}.",
                    })}], "isError": True}})
                q = quote(query)
                text = await call_api("GET", f"/meals/foods/ddg-lookup?q={q}")
                print(f"[ddg-lookup] q={q!r} -> {text[:160]}", flush=True)

            elif tool_name == "search_food":
                def _good_query(v):
                    if _is_schema_echo(v):
                        return False
                    t = _as_text(v)
                    return bool(t) and t.strip().lower().lstrip("$") not in (
                        _FIELD_WORDS | _SCHEMA_MARKERS
                    )
                rescued = _rescue(args, {
                    "query": _good_query,
                    "limit": lambda v: isinstance(v, (int, float)),
                })
                query = _as_text(rescued.get("query"))
                if not query:
                    # Last resort: the model may have buried the food name in an items-like blob.
                    salvaged = _rescue(args, {"display_name": _plausible_food_name})
                    query = _as_text(salvaged.get("display_name"))
                if not query:
                    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps({
                        "error": "missing_query",
                        "message": "search_food requires 'query' as a plain string, e.g. {\"query\": \"Big Mac\"}.",
                    })}], "isError": True}})
                q = quote(query)
                text = await call_api("GET", f"/meals/foods/search?q={q}&limit={int(_as_number(rescued.get('limit'), 10) or 10)}")
                print(f"[search_food] q={q!r} -> {text[:160]}", flush=True)

            elif tool_name == "log_meal_intelligent":
                args = _unwrap_schema_echo(args)
                rescued = _rescue(args, {
                    "text": _plausible_food_text,
                    "food": _plausible_food_text,
                    "display_name": _plausible_food_text,
                    "meal_type": _valid_meal_type,
                })
                text = _as_text(rescued.get("text") or rescued.get("food") or rescued.get("display_name"))
                # Qwen sometimes SWAPS the values: text={"meal_type":"dinner"},
                # meal_type="<the user's whole sentence>". Recover the sentence
                # from wherever it landed.
                if not text:
                   mt_val = args.get("meal_type") or args.get("text")
                   if isinstance(mt_val, str) and _plausible_food_text(mt_val) and re.search(r"\d|\b(?:had|ate|steak|chicken|egg|bread|rice|salad|cheese|fish|pasta|soup|meat|beef|pork|yogurt|fruit)\b", mt_val, re.I):
                       text = mt_val.strip()
                # Last resort: the shim stashes the user's raw words in Redis.
                conversation_id = _as_text(args.get("conversation_id"))
                if not text and REDIS_URL and conversation_id:
                   try:
                       import redis.asyncio as aioredis
                       rcli = aioredis.from_url(REDIS_URL, decode_responses=True)
                       raw = await rcli.get(f"tfshim:last_user_text:{conversation_id}")
                       await rcli.aclose()
                       if raw and _plausible_food_text(raw) and not _looks_like_edit_instruction(raw):
                           text = raw.strip()
                           print(f"[log_meal_intelligent] recovered text from shim redis: {text[:120]!r}", flush=True)
                   except Exception:
                       pass
                if not text:
                   return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps({
                       "error": "no_food_text",
                       "message": "log_meal_intelligent requires 'text' as the user's food words.",
                   })}], "isError": True}})
                text = await _recover_date_phrase(text, conversation_id)
                payload = {"text": text}
                mt = rescued.get("meal_type")
                if mt:
                    payload["meal_type"] = mt.strip().lower()
                text_out = await call_api("POST", "/meals/from-text-intelligent", payload)
                # ALWAYS auto-confirm immediately with the best-ranked candidate.
                # Rationale: Qwen/TrueForge cannot hold a conversational draft or
                # relay a selection question reliably (it replies with its canned
                # "could not retrieve" error when handed prose to echo back).
                # So: log the meal now with top picks, and include the ranked
                # alternatives in the confirmation so the user can adjust after.
                try:
                    draft_data = json.loads(text_out)
                    draft_ok = draft_data.get("ok")
                    draft_items = (draft_data.get("data") or {}).get("items", [])
                    draft_id = (draft_data.get("data") or {}).get("draft_id")

                    if draft_ok and draft_items and draft_id:
                        selections = [0] * len(draft_items)
                        confirm_payload = {"draft_id": draft_id, "selections": selections}
                        confirm_out = await call_api("POST", "/meals/from-text-intelligent/confirm", confirm_payload)
                        # Retry once on failure (service may be restarting).
                        try:
                            conf = json.loads(confirm_out)
                        except Exception:
                            conf = {}
                        if not conf.get("ok"):
                            print(f"[log_meal_intelligent] confirm failed, retrying: {confirm_out[:200]}", flush=True)
                            await asyncio.sleep(2)
                            confirm_out = await call_api("POST", "/meals/from-text-intelligent/confirm", confirm_payload)
                            try:
                                conf = json.loads(confirm_out)
                            except Exception:
                                conf = {}
                        if not conf.get("ok"):
                            # Confirm genuinely failed — say so explicitly so the
                            # model reports an error, not a success.
                            text = json.dumps({"ok": False, "error": "meal_logging_failed", "message": "The meal could not be written to the database. Tell the user logging failed and to try again shortly."})
                            print(f"[log_meal_intelligent] confirm FAILED after retry", flush=True)
                        else:
                            # Build a SHORT, human-readable summary. Qwen cannot
                            # reliably relay a giant JSON blob; concise text works.
                            d = conf.get("data", {})
                            lines = []
                            for it in d.get("items", []):
                                lines.append(f"- {it.get('display_name')} {it.get('grams'):g}g")
                            t = d.get("totals", {})
                            summary = (f"Meal logged (#{d.get('meal_id','')[:8]}): "
                                       + "; ".join(lines)
                                       + f". Total: {t.get('kcal', 0):g} kcal, {t.get('protein_g', 0):g}g protein, {t.get('carbs_g', 0):g}g carbs, {t.get('fat_g', 0):g}g fat.")
                            # Note ambiguous items so the user can adjust after.
                            amb = [it.get("fragment") for it in draft_items if it.get("needs_user_selection")]
                            if amb:
                                summary += f" Several options existed for: {', '.join(amb)}. Say which to change if a match is wrong."
                            text = json.dumps({"ok": True, "summary": summary})
                            print(f"[log_meal_intelligent] confirmed: {summary[:120]}", flush=True)
                            # CONTROLLABLE LIQUID AUTO-LOG (gated by DRHIRO_LIQUID_WRITER):
                            # • unified mode → SKIP: the unified backend writer
                            #   (consumption.py) already handled beverages atomically
                            #   as part of the meal confirm. Running the MCP side-effect
                            #   would double-count the drink.
                            # • legacy mode → RUN: preserve the old behavior where the
                            #   MCP scans the user's text for a drink and logs volume.
                            #   This is the pre-cutover state; the unified writer may
                            #   also be writing (dual-write interval during cutover),
                            #   so the runbook must ensure only one writer is active.
                            # Fail-closed: any other flag value raises ValueError at
                            # module load (see get_liquid_writer_mode).
                            if not is_unified_writer():
                                try:
                                    _drink_cats = [
                                        ("spirits", r"whiskey|whisky|viski|vodka|votka|rum|gin|brandy|rakija|šljivovica|sljivovica|konjak|cognac|tequila|loza|travarica"),
                                        ("wine", r"wine|vino|rose|rosé|prosecco|šampanjac|sampanjac|champagne|crno|bijelo|bjelo"),
                                        ("beer", r"beer|pivo|lager|ale|stout|heineken|ozujsko|karlovačko|karlovacko|točeno|toceno|radler"),
                                        ("other_alcohol", r"cocktail|koktel|cider|jabolčnik|jabolcnik|liqueur|liker|aperol|martini|baileys|amaretto|mojito|negroni|spritz"),
                                        ("non_alcoholic", r"coffee|kava|cappuccino|latte|tea|čaj|caj|ice\s*tea|juice|sok|soda|cola|coke|coca|fanta|sprite|smoothie|shake|milk|mlijeko|mliko|energy|redbull|monster|cedevita|limunada|nectar|espresso|americano|mocha"),
                                    ]
                                    _text_lower = text.lower()
                                    _category = None
                                    for _c, _pat in _drink_cats:
                                        if re.search(_pat, _text_lower, re.I):
                                            _category = _c
                                            break
                                    if _category:
                                        _amount = None
                                        _m = re.search(r'(\d+(?:[.,]\d+)?)\s*(ml|milliliter|millilitre|liter|litre|l|cup|cups|glass|glasses|espresso|shot|shots)', _text_lower)
                                        if _m:
                                            _val = float(_m.group(1).replace(',', '.'))
                                            _unit = _m.group(2).lower()
                                            if _unit in ('l', 'liter', 'litre'):
                                                _amount = _val * 1000
                                            elif _unit in ('cup', 'cups'):
                                                _amount = _val * 250
                                            elif _unit in ('glass', 'glasses'):
                                                _amount = _val * 200
                                            elif _unit in ('espresso', 'shot', 'shots'):
                                                _amount = _val * 30
                                            else:
                                                _amount = _val
                                        if _amount and _amount > 0:
                                            _resp = await call_api("POST", "/ingest/manual/water",
                                                                   {"amount_ml": _amount, "category": _category})
                                            try:
                                                _ok = (json.loads(_resp) or {}).get("ok")
                                            except Exception:
                                                _ok = False
                                            if _ok:
                                                print(f"[log_meal_intelligent] liquid auto-logged: {_amount:.0f}ml {_category}", flush=True)
                                                try:
                                                    _summary_obj = json.loads(text)
                                                    _summary_obj["liquid_logged"] = f"{_amount:.0f}ml {_category}"
                                                    text = json.dumps(_summary_obj)
                                                except Exception:
                                                    pass
                                            else:
                                                print(f"[log_meal_intelligent] liquid auto-log failed: {_resp[:120]}", flush=True)
                                except Exception as _e:
                                    print(f"[log_meal_intelligent] liquid detection error: {_e}", flush=True)
                            else:
                                print(f"[log_meal_intelligent] liquid auto-log SKIPPED (DRHIRO_LIQUID_WRITER=unified): backend is authoritative", flush=True)
                    else:
                        text = text_out
                except Exception as e:
                    print(f"[log_meal_intelligent] error: {e}", flush=True)
                    text = text_out
            elif tool_name == "confirm_intelligent_meal":
                args = _unwrap_schema_echo(args)
                # Qwen echoes draft_id as {"<id>": {}} — unwrap any single-key dict
                def _draft_id_ok(v):
                    if isinstance(v, str) and v.strip():
                        return True
                    if isinstance(v, dict) and len(v) == 1:
                        k = next(iter(v))
                        return isinstance(k, str) and bool(k.strip())
                    return False
                rescued = _rescue(args, {
                    "draft_id": _draft_id_ok,
                    "selections": lambda v: isinstance(v, (list, dict)),
                })
                draft_id = _as_text(rescued.get("draft_id"))
                if not draft_id and isinstance(rescued.get("draft_id"), dict):
                    draft_id = next(iter(rescued["draft_id"]), "").strip()
                if not draft_id:
                    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps({
                        "error": "missing_draft_id",
                        "message": "confirm_intelligent_meal requires 'draft_id'.",
                    })}], "isError": True}})
                selections = rescued.get("selections", [])
                if isinstance(selections, dict):
                    selections = list(selections.values())
                if not isinstance(selections, list):
                    selections = []
                payload = {"draft_id": draft_id, "selections": selections}
                text_out = await call_api("POST", "/meals/from-text-intelligent/confirm", payload)
                text = text_out
            elif tool_name == "build_recipe":
                args = _unwrap_schema_echo(args)
                rescued = _rescue(args, {
                    "name": _plausible_food_text,
                    "text": _plausible_food_text,
                    "ingredients": _plausible_food_text,
                })
                name = _as_text(rescued.get("name"))
                text = _as_text(rescued.get("text") or rescued.get("ingredients"))
                if not name or not text:
                    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps({
                        "error": "missing_recipe",
                        "message": "build_recipe requires 'name' (dish name) and 'text' (ingredient list, e.g. '650g of beef, 400g of chickpeas').",
                    })}], "isError": True}})
                payload = {"name": name, "text": text}
                text = await call_api("POST", "/meals/recipes", payload)
            elif tool_name == "log_recipe_meal":
                args = _unwrap_schema_echo(args)
                rescued = _rescue(args, {
                    "recipe_id": lambda v: isinstance(v, str) and v.strip(),
                    "grams_eaten": lambda v: isinstance(v, (int, float)),
                    "meal_type": _valid_meal_type,
                })
                rid = _as_text(rescued.get("recipe_id"))
                grams = _as_number(rescued.get("grams_eaten"), 0)
                if not rid or not grams:
                    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps({
                        "error": "missing_recipe_log",
                        "message": "log_recipe_meal requires 'recipe_id' and 'grams_eaten'.",
                    })}], "isError": True}})
                payload = {"grams_eaten": grams}
                mt = rescued.get("meal_type")
                if mt:
                    payload["meal_type"] = mt.strip().lower()
                text = await call_api("POST", f"/meals/recipes/{rid}/log", payload)
            elif tool_name == "list_recipes":
                text = await call_api("GET", "/meals/recipes")
            else:
                return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {tool_name}"}})
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}]}})
        except Exception as e:
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(e)}})
    
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})

async def handle_mcp_get(request: Request):
    """TrueForge probes GET before POST; answer instead of 405."""
    return JSONResponse({"ok": True, "transport": "streamable-http", "endpoint": "/mcp", "methods": ["POST"]})

app = Starlette(routes=[
    Route("/mcp", endpoint=handle_mcp, methods=["POST"]),
    Route("/healthz", endpoint=lambda r: JSONResponse({"ok": True}), methods=["GET"]),
])
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3100)