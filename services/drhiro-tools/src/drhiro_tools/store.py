"""
drHiro tools — synthetic, non-diagnostic care-preparation fixtures and helpers.

This module intentionally contains ONLY synthetic, clearly-labelled demo data.
It never reads real health data, a database, or user input beyond the structured
arguments the agent passes to a tool. Everything here is fictional and explicitly
marked SYNTHETIC so it cannot be mistaken for a real patient record.
"""
from __future__ import annotations

# Every fixture is labelled SYNTHETIC. None of these are real people or records.
SYNTHETIC_DEMO_CASES = [
    {
        "case_id": "demo-001",
        "label": "SYNTHETIC",
        "summary": "Synthetic scenario: a 45-year-old adult preparing for a routine "
                   "general-practice visit about exercise habits and sleep.",
        "presenting_concern": "feeling more tired than usual and wanting to talk "
                              "about sleep and activity",
        "known_metrics": {
            "steps_today": 4800,
            "sleep_hours_last_night": 6.0,
            "weight_kg": 78.0,
        },
        "note": "Fictional data for demonstration only. Not a real patient.",
    },
    {
        "case_id": "demo-002",
        "label": "SYNTHETIC",
        "summary": "Synthetic scenario: a 32-year-old adult preparing questions about "
                   "balanced eating and hydration for a wellness check-in.",
        "presenting_concern": "asking how to structure a balanced day of meals and "
                              "water intake",
        "known_metrics": {
            "steps_today": 7200,
            "sleep_hours_last_night": 7.5,
            "weight_kg": 65.0,
        },
        "note": "Fictional data for demonstration only. Not a real patient.",
    },
]


def get_demo_case(case_id: str | None = None) -> dict:
    """Read-only retrieval of a synthetic demo case from the bundled fixture store."""
    if not case_id:
        return {
            "ok": True,
            "cases": SYNTHETIC_DEMO_CASES,
            "notice": "All demo cases are SYNTHETIC and for demonstration only.",
        }
    for case in SYNTHETIC_DEMO_CASES:
        if case["case_id"] == case_id:
            return {"ok": True, "case": case, "notice": "SYNTHETIC demo data only."}
    return {"ok": False, "error": f"No synthetic demo case found for {case_id!r}."}


def create_visit_brief(
    case_id: str,
    focus: str,
    user_notes: str = "",
) -> dict:
    """
    Produce a structured, NON-DIAGNOSTIC care-preparation brief.

    This is an informational pre-visit organizer: it summarizes what the user wants
    to discuss and what they may want to ask. It makes NO diagnosis, NO prescription,
    NO triage recommendation, and does not replace a healthcare professional.
    """
    case = None
    for c in SYNTHETIC_DEMO_CASES:
        if c["case_id"] == case_id:
            case = c
    if not case:
        return {"ok": False, "error": f"No synthetic demo case found for {case_id!r}."}

    # Deliberately non-clinical wording. The agent's system prompt reinforces this.
    brief = {
        "title": f"Care-preparation brief — {case_id}",
        "synthetic": True,
        "case_id": case_id,
        "focus": focus,
        "user_notes": user_notes,
        "discussion_points": [
            "Current sleep pattern and how tiredness affects the day",
            "Usual activity level and steps trend",
            "Any questions the user wants to raise at the visit",
        ],
        "sample_questions": [
            "What is a sensible next step for improving sleep habits?",
            "How much activity is reasonable to aim for each week?",
        ],
        "disclaimer": (
            "This is a care-PREPARATION organizer, not medical advice. It does not "
            "diagnose, prescribe, provide emergency triage, or replace a healthcare "
            "professional. If this is an emergency, contact local emergency services."
        ),
    }
    return {"ok": True, "brief": brief}
