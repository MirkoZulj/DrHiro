"""Health card JWT API (0.10.0 wave 3, spec §6.2).

One row per user (health_profiles). JSON sections validated by
services.schemas_health_card; every write gets an audit event `health_card.<op>`
with the section and entry id — never the content. Dashboard-only: the public
share link is explicitly NOT in v1 (spec §6.4), and PDFs are generated via the
worker (`POST /health-card/pdf`) for download from the Files surface.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import GeneratedFile, HealthProfile, MedicationItem, User
from drhiro_api.security import audit
from drhiro_api.services.schemas_health_card import LIST_SECTIONS, validate_entry, validate_full_card

router = APIRouter(prefix="/health-card", tags=["health-card"])

STALE_AFTER = timedelta(days=365)


def _get_or_create(db: Session, user: User) -> HealthProfile:
    row = db.query(HealthProfile).filter(HealthProfile.user_id == user.id).first()
    if not row:
        row = HealthProfile(user_id=user.id)
        db.add(row)
        db.flush()
    return row


def _age(dob: str | None) -> int | None:
    if not dob:
        return None
    try:
        b = date.fromisoformat(dob)
    except ValueError:
        return None
    today = date.today()
    return today.year - b.year - ((today.month, today.day) < (b.month, b.day))


def _latest_weight(db: Session, user: User) -> float | None:
    from drhiro_api.services.series import daily_series

    try:
        end = date.today()
        vals = daily_series(db, user, "weight_kg", end - timedelta(days=90), end)
        for d in reversed(vals):
            if d.value is not None:
                return round(d.value, 1)
    except Exception:
        pass
    return None


def _card_out(db: Session, user: User, row: HealthProfile) -> dict:
    meds = (
        db.query(MedicationItem)
        .filter(MedicationItem.user_id == user.id, MedicationItem.status == "active")
        .order_by(MedicationItem.created_at.asc())
        .all()
    )
    reviewed_at = row.reviewed_at
    stale = reviewed_at is None or (datetime.now(timezone.utc) - reviewed_at) > STALE_AFTER
    stale_sections = (list(LIST_SECTIONS) + ["insurance"]) if stale else []
    return {
        "stored": {
            "blood_type": row.blood_type,
            "organ_donor": row.organ_donor,
            "conditions": row.conditions_json or [],
            "allergies": row.allergies_json or [],
            "intolerances": row.intolerances_json or [],
            "implants_devices": row.implants_devices_json or [],
            "vaccinations": row.vaccinations_json or [],
            "surgeries": row.surgeries_json or [],
            "family_history": row.family_history_json or [],
            "emergency_contacts": row.emergency_contacts_json or [],
            "care_team": row.care_team_json or [],
            "insurance": row.insurance_json,
            "notes": row.notes,
        },
        "derived": {
            "display_name": user.display_name,
            "date_of_birth": user.date_of_birth,
            "age": _age(user.date_of_birth),
            "sex": user.sex_for_health_calculations,
            "height_cm": user.height_cm,
            "latest_weight_kg": _latest_weight(db, user),
            "current_medications": [
                {
                    "id": str(m.id),
                    "kind": m.kind,
                    "name": m.name,
                    "dose_amount": m.dose_amount,
                    "dose_unit": m.dose_unit,
                    "schedule": m.schedule_json,
                }
                for m in meds
            ],
        },
        "stale_sections": stale_sections,
        "reviewed_at": reviewed_at,
    }


def _gf_out(gf: GeneratedFile) -> dict:
    return {
        "id": str(gf.id), "kind": gf.kind, "status": gf.status, "filename": gf.filename,
        "mime": gf.mime, "size_bytes": gf.size_bytes, "sha256": gf.sha256,
        "created_at": gf.created_at, "expires_at": gf.expires_at, "error": gf.error,
    }


@router.get("")
def get_card(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = _get_or_create(db, user)
    db.commit()
    return {"card": _card_out(db, user, row)}


class FullCardRequest(BaseModel):
    blood_type: str | None = None
    organ_donor: str | None = None
    conditions: list | None = None
    allergies: list | None = None
    intolerances: list | None = None
    implants_devices: list | None = None
    vaccinations: list | None = None
    surgeries: list | None = None
    family_history: list | None = None
    emergency_contacts: list | None = None
    care_team: list | None = None
    insurance: dict | None = None
    notes: str | None = None


@router.put("")
def put_card(req: FullCardRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = _get_or_create(db, user)
    if req.blood_type is not None:
        validate_full_card({"blood_type": req.blood_type})
        row.blood_type = req.blood_type
    if req.organ_donor is not None:
        validate_full_card({"organ_donor": req.organ_donor})
        row.organ_donor = req.organ_donor
    mapping = {
        "conditions": "conditions_json", "allergies": "allergies_json",
        "intolerances": "intolerances_json", "implants_devices": "implants_devices_json",
        "vaccinations": "vaccinations_json", "surgeries": "surgeries_json",
        "family_history": "family_history_json", "emergency_contacts": "emergency_contacts_json",
        "care_team": "care_team_json",
    }
    for name, col in mapping.items():
        val = getattr(req, name)
        if val is not None:
            for e in val:
                validate_entry(name, e)
            setattr(row, col, val)
    if req.insurance is not None:
        row.insurance_json = req.insurance
    if req.notes is not None:
        row.notes = req.notes
    audit(db, "user", str(user.id), user.id, "health_card.replace", "health_profile", str(row.id), {})
    db.commit()
    db.refresh(row)
    return {"card": _card_out(db, user, row)}


@router.post("/reviewed")
def mark_reviewed(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = _get_or_create(db, user)
    row.reviewed_at = datetime.now(timezone.utc)
    audit(db, "user", str(user.id), user.id, "health_card.reviewed", "health_profile", str(row.id), {})
    db.commit()
    return {"ok": True, "reviewed_at": row.reviewed_at}


class PdfRequest(BaseModel):
    lang: str | None = None


@router.post("/pdf")
def request_pdf(req: PdfRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Queue a health-card PDF (worker job). Dashboard-only download."""
    row = _get_or_create(db, user)
    from drhiro_api.services.task_queue import enqueue

    gf = GeneratedFile(
        user_id=user.id, kind="health_card", status="queued",
        params_json={"lang": req.lang or user.locale or "en", "template": "health_card"},
    )
    db.add(gf)
    db.flush()
    job_id = enqueue("drhiro", "drhiro_worker.jobs_reports.build_report", str(gf.id))
    if job_id is None:
        gf.status, gf.error = "failed", "queue unavailable — job not enqueued"
        db.commit()
        return {"file": _gf_out(gf)}
    gf.job_id = job_id
    audit(db, "user", str(user.id), user.id, "health_card.pdf", "generated_file", str(gf.id), {})
    db.commit()
    db.refresh(gf)
    return {"file": _gf_out(gf)}


# NOTE: the dynamic POST /{section} route is registered AFTER the static
# /reviewed and /pdf routes so it cannot shadow them.
@router.post("/{section}")
def add_entry(section: str, body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = _get_or_create(db, user)
    validate_entry(section, body)
    entry = dict(body)
    entry["id"] = str(uuid.uuid4())
    col = f"{section}_json"
    if section == "insurance":
        raise HTTPException(status_code=400, detail="insurance is a single object, use PUT")
    lst = list(getattr(row, col) or [])
    lst.append(entry)
    setattr(row, col, lst)
    audit(db, "user", str(user.id), user.id, "health_card.add", "health_profile", str(row.id),
          {"section": section, "entry_id": entry["id"]})
    db.commit()
    return {"entry": entry}


@router.patch("/{section}/{entry_id}")
def update_entry(section: str, entry_id: str, body: dict,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = _get_or_create(db, user)
    col = f"{section}_json"
    if not hasattr(row, col):
        raise HTTPException(status_code=404, detail="Unknown section")
    lst = list(getattr(row, col) or [])
    for i, e in enumerate(lst):
        if e.get("id") == entry_id:
            merged = {**e, **{k: v for k, v in body.items() if k != "id"}}
            validate_entry(section, merged)
            lst[i] = merged
            setattr(row, col, lst)
            audit(db, "user", str(user.id), user.id, "health_card.update", "health_profile",
                  str(row.id), {"section": section, "entry_id": entry_id})
            db.commit()
            return {"entry": merged}
    raise HTTPException(status_code=404, detail="Entry not found")


@router.delete("/{section}/{entry_id}")
def delete_entry(section: str, entry_id: str,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = _get_or_create(db, user)
    col = f"{section}_json"
    if not hasattr(row, col):
        raise HTTPException(status_code=404, detail="Unknown section")
    lst = list(getattr(row, col) or [])
    new = [e for e in lst if e.get("id") != entry_id]
    if len(new) == len(lst):
        raise HTTPException(status_code=404, detail="Entry not found")
    setattr(row, col, new)
    audit(db, "user", str(user.id), user.id, "health_card.remove", "health_profile",
          str(row.id), {"section": section, "entry_id": entry_id})
    db.commit()
    return {"ok": True}
