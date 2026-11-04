"""Health-card section validation (spec §6.1 / §6.2).

Pure functions, no DB. Each list section has an entry validator: required
fields, allowed enums, max lengths. Max 50 entries per section.
"""

from __future__ import annotations

from fastapi import HTTPException

LIST_SECTIONS = (
    "conditions", "allergies", "intolerances", "implants_devices",
    "vaccinations", "surgeries", "family_history", "emergency_contacts", "care_team",
)

MAX_ENTRIES = 50
MAX_PHONE = 32

BLOOD_TYPES = {"A+", "A-", "B+", "B-", "AB+", "AB-", "0+", "0-", "unknown"}
YES_NO = {"yes", "no", "unknown"}


def _bad(section: str, msg: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"{section}: {msg}")


def _req(section: str, entry: dict, field: str):
    v = entry.get(field)
    if not v or not isinstance(v, str):
        raise _bad(section, f"'{field}' is required")
    return v


def _enum(section: str, entry: dict, field: str, allowed: set[str]) -> None:
    v = entry.get(field)
    if v is not None and v not in allowed:
        raise _bad(section, f"'{field}' must be one of {sorted(allowed)}")


def _since(section: str, entry: dict) -> None:
    v = entry.get("since")
    if v is None:
        return
    s = str(v)
    if not (len(s) == 4 and s.isdigit() or len(s) == 7 and s[4] == "-" or len(s) == 10):
        raise _bad(section, "'since' must be YYYY, YYYY-MM or YYYY-MM-DD")


def validate_conditions(section: str, e: dict) -> None:
    _req(section, e, "name")
    if not isinstance(e.get("status"), str) or e.get("status") not in ("active", "resolved", "monitoring"):
        raise _bad(section, "'status' must be active|resolved|monitoring")
    _since(section, e)


def validate_allergies(section: str, e: dict) -> None:
    _req(section, e, "substance")
    _enum(section, e, "category", {"drug", "food", "environment", "other"})
    _enum(section, e, "severity", {"mild", "moderate", "severe", "unknown"})
    if e.get("confirmed_by_hcp") is not None and not isinstance(e.get("confirmed_by_hcp"), bool):
        raise _bad(section, "'confirmed_by_hcp' must be a boolean")


def validate_intolerances(section: str, e: dict) -> None:
    _req(section, e, "substance")


def validate_implants(section: str, e: dict) -> None:
    _req(section, e, "name")
    _since(section, e)


def validate_vaccinations(section: str, e: dict) -> None:
    _req(section, e, "vaccine")


def validate_surgeries(section: str, e: dict) -> None:
    _req(section, e, "name")


def validate_family_history(section: str, e: dict) -> None:
    _req(section, e, "relation")
    _req(section, e, "condition")


def validate_emergency_contacts(section: str, e: dict) -> None:
    _req(section, e, "name")
    _req(section, e, "relation")
    phone = e.get("phone")
    if phone is not None and (not isinstance(phone, str) or len(phone) > MAX_PHONE):
        raise _bad(section, f"'phone' must be a string of at most {MAX_PHONE} chars")


def validate_care_team(section: str, e: dict) -> None:
    _req(section, e, "role")
    _req(section, e, "name")


VALIDATORS = {
    "conditions": validate_conditions,
    "allergies": validate_allergies,
    "intolerances": validate_intolerances,
    "implants_devices": validate_implants,
    "vaccinations": validate_vaccinations,
    "surgeries": validate_surgeries,
    "family_history": validate_family_history,
    "emergency_contacts": validate_emergency_contacts,
    "care_team": validate_care_team,
}


def validate_entry(section: str, entry: dict) -> None:
    if section not in VALIDATORS:
        raise HTTPException(status_code=404, detail="Unknown section")
    if not isinstance(entry, dict):
        raise _bad(section, "entry must be an object")
    VALIDATORS[section](section, entry)


def validate_full_card(fields: dict) -> None:
    if "blood_type" in fields and fields["blood_type"] is not None:
        if fields["blood_type"] not in BLOOD_TYPES:
            raise _bad("blood_type", f"must be one of {sorted(BLOOD_TYPES)}")
    if "organ_donor" in fields and fields["organ_donor"] is not None:
        if fields["organ_donor"] not in YES_NO:
            raise _bad("organ_donor", f"must be one of {sorted(YES_NO)}")
