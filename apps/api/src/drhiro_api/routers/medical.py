"""Encounter-centric medical records: dashboard-facing JWT routes + scan storage.

Contract (stable JSON shapes for the Records page):

GET  /api/v1/medical/encounters
     -> {"encounters": [MedicalEncounterOut...], "count": int}
GET  /api/v1/medical/encounters/{id}
     -> {"encounter": MedicalEncounterOut, "records": [MedicalRecordOut...]}

Every encounter object carries record_count (all records) and scan_count
(records with a non-null source_image_path) in both list and detail.
POST /api/v1/medical/scan            (multipart file=; auth)
     -> {"path": "<relative path>", "record_id": null}
GET  /api/v1/medical/scan/{record_id} (auth; streams the image, owner only)

A scan can also be attached when creating a record by re-POSTing the scan with
?record_id=... (or via the tools router), which stamps
medical_records.source_image_path.
"""

from __future__ import annotations

import os
import uuid as uuid_mod
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from drhiro_api.config import get_settings
from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import MedicalEncounter, MedicalRecord, User

router = APIRouter(tags=["medical"])

ALLOWED_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
}
# 25 MB is generous for a document photo; keeps abuse bounded.
MAX_SCAN_BYTES = 25 * 1024 * 1024


def store_scan(user: User, data: bytes, content_type: str | None) -> str:
    """Persist scan bytes under <medical_scans_dir>/<user>/<uuid>.<ext> and
    return the RELATIVE path stored in medical_records.source_image_path."""
    ext = ALLOWED_MIME.get((content_type or "").split(";")[0].strip().lower())
    if not ext:
        raise HTTPException(status_code=415, detail="Unsupported image type — jpg, png, webp or heic expected")
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="Empty upload")
    if len(data) > MAX_SCAN_BYTES:
        raise HTTPException(status_code=413, detail="Image too large (max 25 MB)")
    settings = get_settings()
    rel = os.path.join("scans", str(user.id), f"{uuid_mod.uuid4()}{ext}")
    abs_dir = os.path.join(settings.medical_scans_dir, os.path.dirname(rel))
    abs_path = os.path.join(settings.medical_scans_dir, rel)
    os.makedirs(abs_dir, exist_ok=True)
    with open(abs_path, "wb") as fh:
        fh.write(data)
    return rel


def resolve_scan_file(user: User, rel_path: str | None) -> str | None:
    """Absolute path of a stored scan IF it belongs to the user, else None."""
    if not rel_path:
        return None
    settings = get_settings()
    base = os.path.realpath(settings.medical_scans_dir)
    abs_path = os.path.realpath(os.path.join(base, rel_path))
    if not abs_path.startswith(base + os.sep):
        return None  # path traversal guard
    # Ownership: the per-user directory must match.
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) >= 3 and parts[1] == str(user.id) and os.path.isfile(abs_path):
        return abs_path
    return None


def _encounter_out(
    e: MedicalEncounter, record_count: int | None = None, scan_count: int | None = None
) -> dict:
    out = {
        "id": str(e.id),
        "encounter_date": e.encounter_date.isoformat() if e.encounter_date else None,
        "facility": e.facility,
        "title": e.title,
        "notes": e.notes,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "updated_at": e.updated_at.isoformat() if e.updated_at else None,
    }
    if record_count is not None:
        out["record_count"] = record_count
    if scan_count is not None:
        out["scan_count"] = scan_count
    return out


def _record_out(r: MedicalRecord) -> dict:
    return {
        "id": str(r.id),
        "title": r.title,
        "content": r.content,
        "source_type": r.source_type,
        "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None,
        "encounter_id": str(r.encounter_id) if r.encounter_id else None,
        "source_image_path": r.source_image_path,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/medical/encounters")
def list_encounters(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """List the caller's encounters, newest first, each with its record count."""
    from sqlalchemy import func

    rows = (
        db.query(
            MedicalEncounter,
            func.count(MedicalRecord.id),
            func.count(MedicalRecord.source_image_path),
        )
        .outerjoin(MedicalRecord, MedicalRecord.encounter_id == MedicalEncounter.id)
        .filter(MedicalEncounter.user_id == user.id)
        .group_by(MedicalEncounter.id)
        .order_by(MedicalEncounter.encounter_date.desc(), MedicalEncounter.created_at.desc())
        .all()
    )
    encounters = [_encounter_out(e, int(cnt or 0), int(scnt or 0)) for e, cnt, scnt in rows]
    return {"encounters": encounters, "count": len(encounters)}


@router.get("/medical/encounters/{encounter_id}")
def get_encounter(encounter_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """One encounter plus its records (values/findings/conclusion live inside
    each record's title/content as transcribed)."""
    try:
        eid = uuid_mod.UUID(encounter_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Encounter not found")
    e = db.query(MedicalEncounter).filter(MedicalEncounter.id == eid, MedicalEncounter.user_id == user.id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Encounter not found")
    records = (
        db.query(MedicalRecord)
        .filter(MedicalRecord.encounter_id == e.id)
        .order_by(MedicalRecord.recorded_at.desc(), MedicalRecord.created_at.desc())
        .all()
    )
    scan_count = sum(1 for r in records if r.source_image_path)
    return {
        "encounter": _encounter_out(e, len(records), scan_count),
        "records": [_record_out(r) for r in records],
    }


@router.post("/medical/scan")
async def upload_scan(
    file: UploadFile = File(...),
    record_id: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Store a document scan (multipart). Optionally bind it to an existing
    record with record_id, stamping medical_records.source_image_path."""
    data = await file.read()
    rel = store_scan(user, data, file.content_type)
    rid = None
    if record_id:
        try:
            rid_u = uuid_mod.UUID(record_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Record not found")
        rec = db.query(MedicalRecord).filter(MedicalRecord.id == rid_u, MedicalRecord.user_id == user.id).first()
        if not rec:
            raise HTTPException(status_code=404, detail="Record not found")
        rec.source_image_path = rel
        db.add(rec)
        db.commit()
        rid = str(rec.id)
    return {"path": rel, "record_id": rid}


@router.get("/medical/scan/{record_id}")
def serve_scan(record_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Stream the scan image of one of the caller's records. Owner-only."""
    try:
        rid = uuid_mod.UUID(record_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Record not found")
    rec = db.query(MedicalRecord).filter(MedicalRecord.id == rid, MedicalRecord.user_id == user.id).first()
    if not rec or not rec.source_image_path:
        raise HTTPException(status_code=404, detail="Record not found")
    abs_path = resolve_scan_file(user, rec.source_image_path)
    if not abs_path:
        raise HTTPException(status_code=404, detail="Scan image missing")
    return FileResponse(abs_path)
