"""Relative date/time parsing for free-text meal logging.

Users write when they ate in ordinary language: "yesterday for breakfast",
"on Monday for dinner", "last Friday", "2 days ago", "this morning". This
module extracts that phrase and resolves it to a concrete instant.

Two deliberate design choices
-----------------------------
1. The LLM never does date arithmetic. It is unreliable at "what date was last
   Monday", and a wrong date silently corrupts the health record. It only
   forwards the user's words; resolution happens here, deterministically.

2. Everything resolves in the USER's timezone (Europe/Zagreb), not the
   container's (UTC). Between 00:00 and 02:00 Zagreb these fall on different
   calendar days, so "yesterday" computed in UTC would be wrong for two hours
   every night. The returned datetime is timezone-aware and converted to UTC
   only at the end, for storage.

Bare weekday names always resolve BACKWARDS ("on Monday" said on Wednesday
means the Monday 2 days ago, never the coming Monday) because a meal log is a
record of the past. "next Monday" is explicitly rejected rather than guessed.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

DEFAULT_TZ = "Europe/Zagreb"

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "weds": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

# Default clock time per meal, so a date-only phrase still lands sensibly.
MEAL_HOUR = {
    "breakfast": 8,
    "lunch": 13,
    "dinner": 19,
    "snack": 16,
    "drink": 12,
}

# Phrases that imply a meal type as well as (sometimes) a time of day.
MEAL_WORDS = {
    "breakfast": "breakfast",
    "brekfast": "breakfast",
    "brekky": "breakfast",
    "brunch": "breakfast",
    "lunch": "lunch",
    "luch": "lunch",
    "dinner": "dinner",
    "diner": "dinner",
    "supper": "dinner",
    "tea": "dinner",
    "snack": "snack",
    "dessert": "snack",
}

TIME_OF_DAY = {
    "morning": 8,
    "midday": 12,
    "noon": 12,
    "afternoon": 15,
    "evening": 19,
    "night": 21,
    "midnight": 0,
}

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


class AmbiguousDate(ValueError):
    """Raised when a phrase refers to the future or cannot be resolved safely."""


def _tz(tz_name):
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(tz_name or DEFAULT_TZ)
    except Exception:
        return timezone.utc


def _now_local(tz_name, now=None):
    tz = _tz(tz_name)
    if now is not None:
        return now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    return datetime.now(tz)


def detect_meal_type(text: str):
    """Return the meal type named in the text, if any."""
    low = (text or "").lower()
    for word, mt in sorted(MEAL_WORDS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(word)}\b", low):
            return mt
    return None


def _explicit_clock(text: str):
    """Find an explicit clock time. Returns (hour, minute) or None."""
    low = text.lower()

    m = re.search(r"\b(?:at\s+)?(\d{1,2}):(\d{2})\s*(am|pm)?\b", low)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        ampm = m.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute

    m = re.search(r"\b(?:at\s+)?(\d{1,2})\s*(am|pm)\b", low)
    if m:
        hour = int(m.group(1))
        if m.group(2) == "pm" and hour < 12:
            hour += 12
        elif m.group(2) == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23:
            return hour, 0

    for word, hour in TIME_OF_DAY.items():
        if re.search(rf"\b{re.escape(word)}\b", low):
            return hour, 0
    return None


def _last_weekday(now_local, target_idx, force_previous_week=False):
    """Most recent occurrence of a weekday, strictly in the past.

    Said on Wednesday, "Monday" means 2 days ago. "Last Monday" with
    force_previous_week steps back a further 7 days.
    """
    delta = (now_local.weekday() - target_idx) % 7
    if delta == 0:
        delta = 7  # "on Wednesday" said on Wednesday means a week ago
    if force_previous_week and delta < 7:
        delta += 7
    return now_local - timedelta(days=delta)


def resolve_when(text: str, tz_name: str = DEFAULT_TZ, meal_type=None, now=None):
    """Resolve a date/time phrase in free text.

    Returns (utc_datetime | None, matched_phrase | None, meal_type | None).
    A None datetime means "no date mentioned" -- the caller should default to
    now. Raises AmbiguousDate for future references.
    """
    if not text:
        return None, None, None

    low = str(text).lower()
    now_local = _now_local(tz_name, now)
    mt = meal_type or detect_meal_type(low)

    # Guard: never invent a date for a future reference.
    if re.search(r"\b(?:next|coming|tomorrow|tmrw)\b", low):
        raise AmbiguousDate(
            "That refers to a future time. A meal log records what was already "
            "eaten -- confirm the actual date you mean."
        )

    target = None
    phrase = None

    # --- explicit ISO / numeric dates -------------------------------------
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", low)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        target = now_local.replace(year=y, month=mo, day=d)
        phrase = m.group(0)

    # --- "25 August" / "August 25" / "25th of August" ---------------------
    if target is None:
        month_alt = "|".join(sorted(_MONTHS, key=len, reverse=True))
        m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({month_alt})\b", low)
        if not m:
            m2 = re.search(rf"\b({month_alt})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", low)
            if m2:
                day, mon = int(m2.group(2)), _MONTHS[m2.group(1)]
                phrase = m2.group(0)
            else:
                day = mon = None
        else:
            day, mon = int(m.group(1)), _MONTHS[m.group(2)]
            phrase = m.group(0)
        if day and mon:
            year = now_local.year
            cand = now_local.replace(month=mon, day=day)
            if cand.date() > now_local.date():
                cand = cand.replace(year=year - 1)  # a past date, not next year
            target = cand

    # --- "N days/weeks ago" ----------------------------------------------
    if target is None:
        m = re.search(r"\b(\d+)\s+(day|days|week|weeks)\s+ago\b", low)
        if m:
            n = int(m.group(1))
            days = n * (7 if m.group(2).startswith("week") else 1)
            target = now_local - timedelta(days=days)
            phrase = m.group(0)

    # --- "the day before yesterday" --------------------------------------
    if target is None and re.search(r"\bday before yesterday\b", low):
        target = now_local - timedelta(days=2)
        phrase = "day before yesterday"

    # --- yesterday / today / tonight -------------------------------------
    if target is None:
        if re.search(r"\b(?:yesterday|yday|ystrday)\b", low):
            target = now_local - timedelta(days=1)
            phrase = "yesterday"
        elif re.search(r"\b(?:today|this morning|this afternoon|this evening|tonight)\b", low):
            target = now_local
            phrase = "today"

    # --- weekday names, always backwards ---------------------------------
    if target is None:
        force_prev = bool(re.search(r"\blast\b", low))
        for name in sorted(WEEKDAYS, key=len, reverse=True):
            if re.search(rf"\b{re.escape(name)}\b", low):
                target = _last_weekday(now_local, WEEKDAYS[name], force_prev)
                phrase = f"{'last ' if force_prev else ''}{name}"
                break

    # --- "last week" (no weekday named) ----------------------------------
    if target is None and re.search(r"\blast week\b", low):
        target = now_local - timedelta(days=7)
        phrase = "last week"

    if target is None:
        return None, None, mt

    # --- attach a clock time ---------------------------------------------
    clock = _explicit_clock(low)
    if clock:
        hour, minute = clock
    else:
        hour, minute = MEAL_HOUR.get(mt or "", 12), 0

    target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # If a DEFAULT meal hour lands in the future (logging "today for dinner" at
    # 07:00), clamp to now rather than silently moving the meal to yesterday --
    # the user named today, so the date must stay today. An explicit clock time
    # or an explicitly-dated past day is left exactly as given.
    if target > now_local:
        if phrase in ("today",) or clock:
            target = now_local.replace(second=0, microsecond=0)
        else:
            target = target - timedelta(days=1)

    return target.astimezone(timezone.utc), phrase, mt


def strip_when(text: str) -> str:
    """Remove date/time and meal-type words so only the food remains.

    "yesterday for breakfast I had 3 eggs" -> "I had 3 eggs"
    """
    if not text:
        return ""
    out = str(text)
    patterns = [
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:" + "|".join(_MONTHS) + r")\b",
        r"\b(?:" + "|".join(_MONTHS) + r")\s+\d{1,2}(?:st|nd|rd|th)?\b",
        r"\b\d+\s+(?:day|days|week|weeks)\s+ago\b",
        r"\bday before yesterday\b",
        r"\b(?:yesterday|yday|ystrday|today|tonight)\b",
        r"\bthis (?:morning|afternoon|evening)\b",
        r"\blast week\b",
        r"\b(?:last|on)?\s*(?:" + "|".join(WEEKDAYS) + r")\b",
        r"\b(?:at\s+)?\d{1,2}:\d{2}\s*(?:am|pm)?\b",
        r"\b(?:at\s+)?\d{1,2}\s*(?:am|pm)\b",
        r"\b(?:" + "|".join(TIME_OF_DAY) + r")\b",
        r"\bfor\s+(?:" + "|".join(MEAL_WORDS) + r")\b",
        r"\b(?:" + "|".join(MEAL_WORDS) + r")\b",
        r"\bi (?:had|ate|have)\b",
        r"\bfor my\b",
    ]
    for pat in patterns:
        out = re.sub(pat, " ", out, flags=re.I)
    # Drop articles/filler left dangling once the date words are gone, so
    # "yesterday at 21:30 I had a snack" doesn't reduce to a bare "a".
    out = re.sub(r"\b(?:a|an|the|some|of|my|for)\b", " ", out, flags=re.I)
    out = re.sub(r"\s+", " ", out).strip(" ,.;:-")
    return out
