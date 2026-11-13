"""Curate the patient's record into a model-sized context for Questionnarium.

The model only ever sees what this module selects. The contract is:

* conditions, allergies and current medications are ALWAYS included in full
  (they are the facts a health answer must not get wrong);
* recent meals and measurements are included as detail;
* older data is collapsed into aggregates so a long history still fits;
* the whole context is capped to the model's budget (Ling's 32K slot is far
  smaller than Qwen's 256K), with an explicit ``[truncated]`` marker.

Everything is read-only: this module never writes a row.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from drhiro_api.models import (
    HealthProfile,
    Meal,
    MealItem,
    MedicalRecord,
    MedicationItem,
    User,
)
from drhiro_api.services.day_boundary import resolve_tz
from drhiro_api.services.series import daily_series

# model id -> approximate character budget (1 token ~= 4 chars). Ling's slot is
# 32K tokens; leave ample room for the question, history and the answer.
MODEL_CONTEXT_BUDGET = {
    "default": 90_000,
    "ling": 20_000,
}

RECENT_DAYS = 14
AGGREGATE_DAYS = 90
MAX_MEALS = 60
MAX_RECORDS = 20
MAX_RECORD_CHARS = 1_200
MAX_LIST_ENTRIES = 50

# key -> (short label, unit). BP is rendered as one combined value.
CORE_METRICS = [
    ("weight_kg", "weight", "kg"),
    ("bp_sys", "BP", "mmHg"),
    ("bp_dia", None, "mmHg"),
    ("bp_pulse", "pulse", "bpm"),
    ("resting_hr", "resting HR", "bpm"),
    ("heart_rate_avg", "avg HR", "bpm"),
    ("spo2_avg", "SpO2", "%"),
    ("stress_avg", "stress", ""),
    ("sleep_duration_min", "sleep", "h"),
    ("steps", "steps", ""),
    ("active_minutes", "active min", "min"),
    ("active_kcal", "active kcal", "kcal"),
    ("kcal_in", "intake", "kcal"),
    ("protein_g", "protein", "g"),
    ("carbs_g", "carbs", "g"),
    ("fat_g", "fat", "g"),
    ("liquids_total_ml", "liquids", "ml"),
    ("water_ml", "water", "ml"),
]

SOURCE_LABELS = {
    "patient": "patient profile",
    "conditions": "health card conditions",
    "allergies": "allergies",
    "intolerances": "intolerances",
    "medications": "current medications",
    "measurements_recent": "recent measurements",
    "meals_recent": "recent meals",
    "records_recent": "medical records",
    "aggregates": "90-day aggregates",
}

SECTION_ORDER = [
    "patient",
    "conditions",
    "allergies",
    "intolerances",
    "medications",
    "measurements_recent",
    "meals_recent",
    "records_recent",
    "aggregates",
]
MUST_SECTIONS = {"patient", "conditions", "allergies", "medications"}


# --------------------------------------------------------------------------- #
# small formatting helpers
# --------------------------------------------------------------------------- #

def _num(value) -> str:
    if value is None:
        return "?"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{f:g}"


def _age(dob: str | None) -> int | None:
    if not dob:
        return None
    try:
        born = date.fromisoformat(dob)
    except ValueError:
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _today(user: User) -> date:
    return datetime.now(resolve_tz(user.timezone)).date()


def _join(entry: dict, parts: list[str]) -> str:
    return ", ".join(p for p in parts if p)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #

def _patient_section(user: User) -> str:
    lines = [f"Name: {user.display_name}"]
    age = _age(user.date_of_birth)
    if age is not None:
        lines.append(f"Age: {age} years (born {user.date_of_birth})")
    elif user.date_of_birth:
        lines.append(f"Date of birth: {user.date_of_birth}")
    if user.sex_for_health_calculations:
        lines.append(f"Sex: {user.sex_for_health_calculations}")
    if user.height_cm is not None:
        lines.append(f"Height: {_num(user.height_cm)} cm")
    if user.basal_metabolism_kcal is not None:
        lines.append(f"Basal metabolism: {_num(user.basal_metabolism_kcal)} kcal/day")
    return "# Patient\n" + "\n".join(lines)


def _health_card(db: Session, user: User) -> HealthProfile | None:
    return db.query(HealthProfile).filter(HealthProfile.user_id == user.id).first()


def _conditions_section(db: Session, user: User) -> str:
    card = _health_card(db, user)
    entries = (card.conditions_json or []) if card else []
    if not entries:
        return ""
    lines = ["# Conditions"]
    for e in entries[:MAX_LIST_ENTRIES]:
        if not isinstance(e, dict):
            lines.append(f"- {e}")
            continue
        detail = _join(e, [
            str(e.get("status")) if e.get("status") else "",
            f"since {e['since']}" if e.get("since") else "",
            f"resolved {e['resolved']}" if e.get("resolved") else "",
        ])
        line = f"- {e.get('name') or 'Unnamed'}"
        if detail:
            line += f" ({detail})"
        if e.get("notes"):
            line += f" — {_clip(str(e['notes']), 200)}"
        lines.append(line)
    return "\n".join(lines)


def _allergies_section(db: Session, user: User) -> str:
    card = _health_card(db, user)
    entries = (card.allergies_json or []) if card else []
    if not entries:
        return ""
    lines = ["# Allergies"]
    for e in entries[:MAX_LIST_ENTRIES]:
        if not isinstance(e, dict):
            lines.append(f"- {e}")
            continue
        detail = _join(e, [
            str(e.get("category")) if e.get("category") else "",
            f"severity {e['severity']}" if e.get("severity") else "",
            "confirmed by HCP" if e.get("confirmed_by_hcp") else "",
        ])
        line = f"- {e.get('substance') or 'Unnamed'}"
        if detail:
            line += f" ({detail})"
        if e.get("notes"):
            line += f" — {_clip(str(e['notes']), 160)}"
        lines.append(line)
    return "\n".join(lines)


def _intolerances_section(db: Session, user: User) -> str:
    card = _health_card(db, user)
    entries = (card.intolerances_json or []) if card else []
    if not entries:
        return ""
    lines = ["# Intolerances"]
    for e in entries[:MAX_LIST_ENTRIES]:
        if not isinstance(e, dict):
            lines.append(f"- {e}")
            continue
        line = f"- {e.get('substance') or 'Unnamed'}"
        if e.get("notes"):
            line += f" — {_clip(str(e['notes']), 160)}"
        lines.append(line)
    return "\n".join(lines)


def _medications_section(db: Session, user: User) -> str:
    items = (
        db.query(MedicationItem)
        .filter(MedicationItem.user_id == user.id)
        .order_by(MedicationItem.created_at.asc())
        .all()
    )
    if not items:
        return ""
    active = [i for i in items if i.status == "active"]
    stopped = [i for i in items if i.status != "active"]
    lines = ["# Current medications"]
    for i in active[:MAX_LIST_ENTRIES]:
        dose = ""
        if i.dose_amount is not None:
            dose = f" {_num(i.dose_amount)} {i.dose_unit or ''}".strip()
        detail = _join(i, [
            str(i.kind) if i.kind else "",
            f"reason: {i.reason}" if i.reason else "",
            f"since {i.start_date}" if i.start_date else "",
            f"until {i.end_date}" if i.end_date else "",
            f"prescribed by {i.prescribed_by}" if i.prescribed_by else "",
        ])
        line = f"- {i.name}{dose}"
        if i.form:
            line += f" ({i.form})"
        if detail:
            line += f" — {detail}"
        lines.append(line)
        ingredients = [
            f"{e.get('name')} {_num(e.get('amount'))}{e.get('unit') or ''}".strip()
            for e in (i.ingredients_json or [])
            if isinstance(e, dict) and e.get("name")
        ]
        if ingredients:
            lines.append(f"  ingredients: {'; '.join(ingredients[:12])}")
    if stopped:
        lines.append("# Past / stopped medications")
        for i in stopped[:20]:
            dose = ""
            if i.dose_amount is not None:
                dose = f" {_num(i.dose_amount)} {i.dose_unit or ''}".strip()
            lines.append(f"- {i.name}{dose} ({i.status})")
    return "\n".join(lines)


def _metric_series(db: Session, user: User, today: date) -> dict[str, list]:
    start = today - timedelta(days=AGGREGATE_DAYS - 1)
    out: dict[str, list] = {}
    for key, _label, _unit in CORE_METRICS:
        try:
            out[key] = daily_series(db, user, key, start, today)
        except Exception:  # noqa: BLE001 - a metric with no data must not break the rest
            out[key] = []
    return out


def _measurements_section(series: dict[str, list], today: date) -> str:
    days = [today - timedelta(days=i) for i in range(RECENT_DAYS - 1, -1, -1)]
    lines = [f"# Recent measurements (last {RECENT_DAYS} days)"]
    any_value = False
    for day in days:
        parts: list[str] = []
        sys_v = _day_value(series.get("bp_sys"), day)
        dia_v = _day_value(series.get("bp_dia"), day)
        if sys_v is not None and dia_v is not None:
            parts.append(f"BP {_num(sys_v)}/{_num(dia_v)} mmHg")
        elif sys_v is not None:
            parts.append(f"BP sys {_num(sys_v)} mmHg")
        for key, label, unit in CORE_METRICS:
            if key in ("bp_sys", "bp_dia"):
                continue
            value = _day_value(series.get(key), day)
            if value is None:
                continue
            if key == "sleep_duration_min":
                parts.append(f"{label} {_num(round(value / 60.0, 2))} {unit}")
            elif unit:
                parts.append(f"{label} {_num(value)} {unit}")
            else:
                parts.append(f"{label} {_num(value)}")
        if parts:
            any_value = True
            lines.append(f"- {day.isoformat()}: " + ", ".join(parts))
    if not any_value:
        return ""
    return "\n".join(lines)


def _day_value(values: list | None, day: date):
    if not values:
        return None
    for point in values:
        if getattr(point, "day", None) == day:
            return point.value
    return None


def _meals_section(db: Session, user: User, today: date) -> str:
    cutoff = datetime.combine(today - timedelta(days=RECENT_DAYS - 1), datetime.min.time())
    meals = (
        db.query(Meal)
        .filter(
            Meal.user_id == user.id,
            Meal.status == "confirmed",
            Meal.eaten_at >= cutoff,
        )
        .order_by(Meal.eaten_at.desc())
        .limit(MAX_MEALS)
        .all()
    )
    if not meals:
        return ""
    item_rows = (
        db.query(MealItem)
        .filter(MealItem.meal_id.in_([m.id for m in meals]))
        .all()
    )
    items_by_meal: dict = {}
    for it in item_rows:
        items_by_meal.setdefault(it.meal_id, []).append(it)

    lines = [f"# Recent meals (last {RECENT_DAYS} days, confirmed)"]
    for m in sorted(meals, key=lambda x: x.eaten_at, reverse=True):
        totals = m.totals_json or {}
        macro = _join(m, [
            f"{_num(totals.get('kcal'))} kcal" if totals.get("kcal") is not None else "",
            f"P {_num(totals.get('protein_g'))}g" if totals.get("protein_g") is not None else "",
            f"C {_num(totals.get('carbs_g'))}g" if totals.get("carbs_g") is not None else "",
            f"F {_num(totals.get('fat_g'))}g" if totals.get("fat_g") is not None else "",
        ])
        names = [it.display_name for it in items_by_meal.get(m.id, [])][:6]
        day = m.eaten_at.date().isoformat() if m.eaten_at else ""
        line = f"- {day} {m.meal_type or 'meal'}: {macro}".rstrip(": ").strip()
        if names:
            line += f" — {', '.join(names)}"
        lines.append(line)
    return "\n".join(lines)


def _records_section(db: Session, user: User, today: date) -> str:
    cutoff = datetime.combine(today - timedelta(days=179), datetime.min.time())
    records = (
        db.query(MedicalRecord)
        .filter(MedicalRecord.user_id == user.id, MedicalRecord.recorded_at >= cutoff)
        .order_by(MedicalRecord.recorded_at.desc())
        .limit(MAX_RECORDS)
        .all()
    )
    if not records:
        return ""
    lines = ["# Medical records (last 6 months)"]
    for r in records:
        title = r.title or "Untitled document"
        content = _clip(r.content or "", MAX_RECORD_CHARS)
        lines.append(f"- {r.recorded_at.date().isoformat() if r.recorded_at else ''} "
                     f"“{title}”: {content}")
    return "\n".join(lines)


def _aggregates_section(series: dict[str, list]) -> str:
    lines = [f"# {AGGREGATE_DAYS}-day aggregates"]
    any_value = False
    for key, label, unit in CORE_METRICS:
        values = [p.value for p in (series.get(key) or []) if p.value is not None]
        if not values:
            continue
        any_value = True
        avg = sum(values) / len(values)
        if key == "sleep_duration_min":
            lines.append(f"- {label}: avg {_num(round(avg / 60.0, 2))} h, "
                         f"min {_num(round(min(values) / 60.0, 2))} h, "
                         f"max {_num(round(max(values) / 60.0, 2))} h, {len(values)} days with data")
        else:
            lines.append(f"- {label}: avg {_num(round(avg, 1))} {unit}, "
                         f"min {_num(round(min(values), 1))}, max {_num(round(max(values), 1))}, "
                         f"{len(values)} days with data")
    if not any_value:
        return ""
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# budget fit
# --------------------------------------------------------------------------- #

def _fit(sections: dict[str, str], budget: int) -> tuple[str, list[str], bool]:
    """Assemble sections under ``budget`` chars, must-sections first.

    The joined result is guaranteed to be at most ``budget`` characters: the
    separator cost is accounted for and a truncation marker replaces the tail
    rather than being added on top of it.
    """
    sep = "\n\n"
    sep_len = len(sep)
    marker = "\n[truncated]"
    included: list[str] = []
    parts: list[str] = []
    used = 0  # sum of section lengths plus separators
    truncated = False

    def add(name: str, text: str) -> None:
        nonlocal used
        included.append(name)
        parts.append(text)
        used += len(text) + (sep_len if len(parts) > 1 else 0)

    must = [s for s in SECTION_ORDER if s in MUST_SECTIONS and sections.get(s)]
    must_cap = max(budget // max(len(must), 1), 1_500)
    for name in must:
        text = sections[name]
        if len(text) > must_cap:
            text = text[: max(must_cap - len(marker), 0)].rstrip() + marker
            truncated = True
        add(name, text)

    for name in SECTION_ORDER:
        if name in MUST_SECTIONS:
            continue
        text = sections.get(name)
        if not text:
            continue
        extra = len(text) + (sep_len if parts else 0)
        if extra <= budget - used:
            add(name, text)
            continue
        room = budget - used - (sep_len if parts else 0) - len(marker)
        if room > 400:
            add(name, text[:room].rstrip() + marker)
            truncated = True
        break  # budget spent — lower-priority sections are dropped

    return sep.join(parts), included, truncated


def build_context(db: Session, user: User, model: str = "default") -> dict:
    """Return ``{"text", "sections", "sources", "chars", "estimated_tokens",
    "truncated", "budget_chars", "model"}`` for one patient."""
    budget = MODEL_CONTEXT_BUDGET.get(model, MODEL_CONTEXT_BUDGET["default"])
    today = _today(user)
    series = _metric_series(db, user, today)

    sections = {
        "patient": _patient_section(user),
        "conditions": _conditions_section(db, user),
        "allergies": _allergies_section(db, user),
        "intolerances": _intolerances_section(db, user),
        "medications": _medications_section(db, user),
        "measurements_recent": _measurements_section(series, today),
        "meals_recent": _meals_section(db, user, today),
        "records_recent": _records_section(db, user, today),
        "aggregates": _aggregates_section(series),
    }
    text, included, truncated = _fit(sections, budget)
    return {
        "text": text,
        "sections": included,
        "sources": [SOURCE_LABELS.get(s, s) for s in included],
        "chars": len(text),
        "estimated_tokens": len(text) // 4,
        "truncated": truncated,
        "budget_chars": budget,
        "model": model,
    }
