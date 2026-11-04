"""Deterministic sentence templates (spec §3.2 render.py). No LLM.

D3: facts-only wording. The BANNED_WORDS list in thresholds.py is asserted
against every rendered finding in run.py — a template drift that introduces
causal wording fails loudly rather than silently.
"""

from __future__ import annotations

from datetime import date

from drhiro_analytics.thresholds import BANNED_WORDS
from drhiro_analytics.types import Finding, TimelineEvent, Unknown

HR_MONTHS = ["sij", "vlj", "ožu", "tra", "svi", "lip", "srp", "kol", "ruj", "lis", "stu", "pro"]
EN_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

LABELS = {
    "en": {
        "sleep_duration_min": "Sleep duration",
        "sleep_midpoint_min": "Sleep midpoint",
        "steps": "Steps",
        "active_minutes": "Active minutes",
        "active_kcal": "Active kcal",
        "activity_kcal_logged": "Logged activity kcal",
        "weight_kg": "Weight",
        "bp_sys": "Systolic BP",
        "bp_dia": "Diastolic BP",
        "bp_pulse": "Pulse",
        "resting_hr": "Resting heart rate",
        "heart_rate_avg": "Heart rate",
        "spo2_avg": "SpO2",
        "stress_avg": "Stress",
        "kcal_in": "Calories in",
        "protein_g": "Protein",
        "carbs_g": "Carbs",
        "fat_g": "Fat",
        "liquids_total_ml": "Liquids",
        "water_ml": "Water",
        "alcohol_ml": "Alcohol",
    },
    "hr": {
        "sleep_duration_min": "Trajanje sna",
        "sleep_midpoint_min": "Sredina sna",
        "steps": "Koraci",
        "active_minutes": "Aktivne minute",
        "active_kcal": "Aktivne kcal",
        "activity_kcal_logged": "Zabilježene aktivne kcal",
        "weight_kg": "Težina",
        "bp_sys": "Gornji tlak",
        "bp_dia": "Donji tlak",
        "bp_pulse": "Puls",
        "resting_hr": "Srce u mirovanju",
        "heart_rate_avg": "Broj otkucaja",
        "spo2_avg": "SpO2",
        "stress_avg": "Stres",
        "kcal_in": "Unešene kalorije",
        "protein_g": "Proteini",
        "carbs_g": "Ugljikohidrati",
        "fat_g": "Masti",
        "liquids_total_ml": "Tekućina",
        "water_ml": "Voda",
        "alcohol_ml": "Alkohol",
    },
}


def label(key: str, locale: str) -> str:
    return LABELS.get(locale, LABELS["en"]).get(key, key)


def fmt_date(d: date | None, locale: str) -> str:
    if d is None:
        return ""
    months = HR_MONTHS if locale == "hr" else EN_MONTHS
    if locale == "hr":
        return f"{d.day}. {months[d.month - 1]} {d.year}."
    return f"{d.day} {months[d.month - 1]} {d.year}"


def fmt_num(v: float | None, unit: str) -> str:
    """minutes ≥ 60 → 7h10; kg 1 dp; mmHg/bpm/steps integers; ml → 1.2 L if ≥ 1000."""
    if v is None:
        return "—"
    if unit == "min" and abs(v) >= 60:
        sign = "-" if v < 0 else ""
        a = abs(v)
        return f"{sign}{int(a // 60)}h{int(round(a % 60)):02d}"
    if unit == "kg":
        return f"{v:.1f}"
    if unit == "ml" and abs(v) >= 1000:
        return f"{v / 1000:.1f} L"
    if unit in ("mmHg", "bpm", "steps", "kcal", "g", "%", "score"):
        return f"{round(v)}"
    return f"{v:g}"


def _cov_pct(f: Finding) -> str:
    cov = (f.stats or {}).get("coverage_after")
    return f"{cov:.0%}" if isinstance(cov, (int, float)) else ""


def render_finding(f: Finding, locale: str) -> str:
    lbl = label(f.metric.split(",")[0], locale)
    up = f.direction == "up"
    if f.kind == "level_change":
        d = fmt_date(f.anchor_date, locale)
        b, a = fmt_num(f.before, f.unit), fmt_num(f.after, f.unit)
        delta = fmt_num(abs(f.delta), f.unit) if f.delta is not None else ""
        arrow = "povećanje" if up else "smanjenje"
        if locale == "hr":
            txt = (f"{lbl} {arrow} od {d}: {b} → {a} medijan ({delta}), "
                   f"{(f.stats or {}).get('n_after', '?')} dana nakon, pokrivenost {_cov_pct(f)}.")
        else:
            verb = "increased" if up else "decreased"
            txt = (f"{lbl} {verb} from {d}: {b} → {a} median ({delta}), "
                   f"{(f.stats or {}).get('n_after', '?')} days after, coverage {_cov_pct(f)}.")
        if (f.stats or {}).get("partial"):
            n = (f.stats or {}).get("n_after", 0)
            txt += (f" (still settling — {n} days of data after)" if locale != "hr"
                    else f" (još se utvrđuje — {n} dana podataka nakon)")
        return txt
    if f.kind == "trend":
        slope = f.delta  # per 30 days, signed
        per30 = fmt_num(abs(slope), f.unit)
        d = fmt_date(f.anchor_date, locale)
        if locale == "hr":
            verb = "raste" if up else "pada"
            return f"{lbl} {verb} od {d}: {per30} u 30 dana (Theil–Sen)."
        verb = "risen steadily" if up else "fallen steadily"
        return f"{lbl} has {verb} since {d}: {per30} per 30 days (Theil–Sen)."
    if f.kind == "variability_change":
        d = fmt_date(f.anchor_date, locale)
        b, a = fmt_num(f.before, f.unit), fmt_num(f.after, f.unit)
        more = f.direction == "up"
        if locale == "hr":
            txt = f"{lbl} {'manje' if more else 'više'} redovito od {d} (raspon {b} → {a})."
        else:
            txt = f"{lbl} became {'less' if more else 'more'} regular from {d} (spread {b} → {a})."
        return txt
    if f.kind == "period_delta":
        b, a = fmt_num(f.before, f.unit), fmt_num(f.after, f.unit)
        delta = fmt_num(abs(f.delta), f.unit) if f.delta is not None else ""
        arrow = "viši" if up else "niži"
        if locale == "hr":
            return f"{lbl} je {arrow} u ovom razdoblju nego prethodnom: {b} → {a} medijan ({delta})."
        return f"{lbl} was {'higher' if up else 'lower'} this period than the previous one: {b} → {a} median ({delta})."
    if f.kind == "cooccurrence":
        d = fmt_date(f.anchor_date, locale)
        items = ", ".join(f"{label(m, locale)} {'↑' if dirn == 'up' else '↓'}"
                          for m, dirn in _member_dirs(f))
        if locale == "hr":
            return f"Ok oko {d} (±7 dana) nekoliko se mjera promijenilo zajedno: {items}."
        return f"Around {d} (±7 days) several metrics changed together: {items}."
    return ""


def _member_dirs(f: Finding) -> list[tuple[str, str]]:
    # members' (metric, direction) are encoded in the summary at run time
    return getattr(f, "_members", [])


def render_unknown(u: Unknown, locale: str) -> str:
    return u.text


def render_timeline_suffix(events: list[TimelineEvent], locale: str) -> str:
    if not events:
        return ""
    labels = ", ".join(ev.label for ev in events)
    if locale == "hr":
        return f" Blizu na vremenskoj liniji: {labels}."
    return f" Nearby on your timeline: {labels}."


def assert_no_banned_words(text: str) -> bool:
    low = text.lower()
    return not any(w in low for w in BANNED_WORDS)
