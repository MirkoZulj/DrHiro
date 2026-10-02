"""Normalise food/drink input to English before catalogue lookup.

The food catalogue is USDA-derived and English; the user logs in Croatian.
Jev's equivalence model is English-first (it scores "kava" vs "Coffee" at 0.01),
so the raw input is translated before the DB/USDA/Jev cascade. The DuckDuckGo
step and the stored display name keep the ORIGINAL language.

Two layers:
  1. A curated Croatian->English glossary. Deterministic, instant, and it wins
     over the model for the terms it knows -- this is what stops "pasteta"
     becoming "paste" (it is pate) and "gemist" becoming "Gemist".
  2. The local LLM, for anything the glossary does not cover.

Word-by-word translation is the failure mode being avoided: "cajna pasteta"
literally means "tea paste", but the food is a pork pate. The glossary encodes
the food, not the words.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from drhiro_api.services.llm_client import chat_complete_sync

log = logging.getLogger(__name__)

_DIACRITICS = str.maketrans("čćžšđČĆŽŠĐ", "cczsdCCZSD")


def _norm(text: str) -> str:
    """Lowercase and fold Croatian diacritics for glossary matching."""
    return (text or "").strip().lower().translate(_DIACRITICS)


# Curated Croatian (and regional) food/drink -> common English culinary name.
# Keys are diacritic-folded lowercase.
_GLOSSARY: dict[str, str] = {
    # drinks
    "kava": "coffee, brewed",
    "kavica": "coffee, brewed",
    "espreso": "espresso",
    "caj": "tea, brewed",
    "bijeli caj": "tea, brewed",
    "voda": "water",
    "mineralna voda": "mineral water",
    "sok": "fruit juice",
    "sok od narance": "orange juice",
    "pivo": "beer",
    "vino": "wine",
    "bijelo vino": "white wine",
    "crno vino": "red wine",
    "crveno vino": "red wine",
    "gemist": "white wine spritzer",
    "bevanda": "white wine spritzer",
    "rakija": "brandy",
    "sok od jabuke": "apple juice",
    "mlijeko": "milk",
    "kefir": "kefir",
    "jogurt": "yogurt",
    "kakao": "cocoa",
    # dairy
    "sir": "cheese",
    "sir i vrhnje": "cream cheese spread",
    "vrhnje": "sour cream",
    "kiselo vrhnje": "sour cream",
    "slatko vrhnje": "heavy cream",
    "maslac": "butter",
    "puter": "butter",
    "skuta": "cottage cheese",
    "svjezi sir": "cottage cheese",
    "gauda": "gouda cheese",
    "ementaler": "emmental cheese",
    "emmental": "emmental cheese",
    "parmezan": "parmesan cheese",
    "feta": "feta cheese",
    "mozzarella": "mozzarella cheese",
    "brie": "brie cheese",
    "topljeni sir": "processed cheese",
    # meat / fish
    "meso": "meat",
    "govedina": "beef",
    "junetina": "beef",
    "svinjetina": "pork",
    "piletina": "chicken",
    "purecina": "turkey",
    "puretina": "turkey",
    "janjetina": "lamb",
    "teletina": "veal",
    "riba": "fish",
    "tunjevina": "tuna",
    "losos": "salmon",
    "slanina": "bacon",
    "sunka": "ham",
    "panceta": "pancetta",
    "kulen": "cured pork sausage",
    "cajna pasteta": "pork pate",
    "pasteta": "pork pate",
    "jetrena pasteta": "liver pate",
    "gulas": "goulash",
    "cevapi": "grilled minced meat",
    "cevapcici": "grilled minced meat",
    "pljeskavica": "grilled minced meat patty",
    "sarma": "cabbage rolls with minced meat",
    "kotlet": "pork chop",
    "batak": "chicken thigh",
    "prsa": "chicken breast",
    "kobasica": "sausage",
    "salama": "salami",
    "hrenovka": "hot dog sausage",
    "jaje": "egg",
    "jaja": "egg",
    "omlet": "omelette",
    "kuhano jaje": "boiled egg",
    # staples / grains
    "kruh": "bread",
    "kiselo tijesto": "sourdough bread",
    "kvasac": "yeast",
    "tost": "toast bread",
    "pecivo": "bread roll",
    "kifla": "bread roll",
    "burek": "meat pie",
    "riza": "rice, cooked",
    "bijela riza": "white rice, cooked",
    "tjestenina": "pasta, cooked",
    "spageti": "spaghetti, cooked",
    "njoki": "potato gnocchi",
    "krompir": "potato",
    "krumpir": "potato",
    "pire krumpir": "mashed potato",
    "pomfrit": "french fries",
    "brasno": "flour",
    "zob": "oats",
    "zobene pahuljice": "oat flakes",
    "muesli": "muesli",
    "kukuruz": "corn",
    "kukuruzne pahuljice": "corn flakes",
    "leca": "lentils",
    "grah": "beans, cooked",
    "slanutak": "chickpeas",
    # vegetables
    "salata": "lettuce",
    "zelena salata": "lettuce",
    "rajcica": "tomato",
    "paradajz": "tomato",
    "krastavac": "cucumber",
    "paprika": "bell pepper",
    "roga paprika": "banana pepper",
    "luk": "onion",
    "crveni luk": "red onion",
    "cesnjak": "garlic",
    "mrkva": "carrot",
    "tikvica": "zucchini",
    "patlidzan": "eggplant",
    "brokula": "broccoli",
    "cvjetaca": "cauliflower",
    "kupus": "cabbage",
    "kelj": "kale",
    "spinat": "spinach",
    "gljive": "mushrooms",
    "pecurke": "mushrooms",
    "kisele paprike": "pickled peppers",
    "ajvar": "roasted red pepper spread",
    "masline": "olives",
    "avokado": "avocado",
    "batat": "sweet potato",
    # fruit
    "jabuka": "apple",
    "kruska": "pear",
    "banana": "banana",
    "naranca": "orange",
    "mandarina": "tangerine",
    "limun": "lemon",
    "grozdje": "grapes",
    "jagoda": "strawberry",
    "jagode": "strawberry",
    "malina": "raspberry",
    "borovnica": "blueberry",
    "breskva": "peach",
    "kajsija": "apricot",
    "sljiva": "plum",
    "lubenica": "watermelon",
    "dinja": "melon",
    "smokva": "fig",
    "orasi": "walnuts",
    "bademi": "almonds",
    "lesnjaci": "hazelnuts",
    "kikiriki": "peanuts",
    # prepared / other
    "juha": "soup",
    "varivo": "vegetable stew",
    "pizza": "pizza",
    "sendvic": "sandwich",
    "hamburger": "hamburger",
    "palacinke": "pancakes",
    "kolac": "cake",
    "keks": "biscuit",
    "sladoled": "ice cream",
    "cokolada": "chocolate",
    "med": "honey",
    "secer": "sugar",
    "dzem": "jam",
    "maslinovo ulje": "olive oil",
    "ulje": "vegetable oil",
    "ocat": "vinegar",
    "majoneza": "mayonnaise",
    "kecap": "ketchup",
    "senf": "mustard",
    "sol": "salt",
    "papar": "black pepper",
    "zacini": "spices",
}

_SYSTEM = (
    "You translate Croatian food and drink names into the English term a "
    "nutrition database would use.\n"
    "Rules:\n"
    "1. Name the FOOD, never translate word by word. 'cajna pasteta' is a pork "
    "pate, NOT 'tea paste'. 'gemist' is a wine spritzer.\n"
    "2. Use the common English culinary name, lowercase, no brand names.\n"
    "3. For drinks, include how it is served when it changes the calories: "
    "brewed coffee, not dry coffee.\n"
    "4. If the input is already English, repeat it unchanged.\n"
    "5. Reply with ONLY the English name. No quotes, no explanation.\n"
    "Examples:\n"
    "kava -> coffee, brewed\n"
    "cajna pasteta -> pork pate\n"
    "gemist -> white wine spritzer\n"
    "riza -> rice, cooked\n"
    "piletina -> chicken\n"
    "jabuka -> apple\n"
    "sir i vrhnje -> cream cheese spread"
)


@lru_cache(maxsize=4096)
def _llm_translate(text: str) -> str:
    try:
        out = chat_complete_sync(
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": text}],
            temperature=0.0,
        )
    except Exception:
        log.exception("food translation failed; falling back to original text")
        return text
    out = (out or "").strip().strip('"').strip()
    # Guard against a model that answers with a sentence instead of a name.
    if not out or len(out) > 80 or "\n" in out:
        return text
    return out


def _glossary_lookup(text: str) -> str | None:
    """Exact match, then the longest glossary key contained in the phrase."""
    key = _norm(text)
    if not key:
        return None
    if key in _GLOSSARY:
        return _GLOSSARY[key]
    # Longest key first so "bijelo vino" beats "vino".
    for k in sorted(_GLOSSARY, key=len, reverse=True):
        if k in key:
            return _GLOSSARY[k]
    return None


def translate_food_to_english(text: str) -> str:
    """Best-effort English normalisation. Never raises; falls back to input."""
    text = (text or "").strip()
    if not text:
        return text
    hit = _glossary_lookup(text)
    if hit:
        return hit
    return _llm_translate(text)