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


def store_scan(user: User, data: bytes, content_type: str | None, subdir: str = "scans") -> str:
    """Persist scan bytes under <medical_scans_dir>/<subdir>/<user>/<uuid>.<ext> and
    return the RELATIVE path stored in medical_records.source_image_path.
    Legacy documents stay in scans/<user>/...; medication labels use subdir="labels"."""
    ext = ALLOWED_MIME.get((content_type or "").split(";")[0].strip().lower())
    if not ext:
        raise HTTPException(status_code=415, detail="Unsupported image type — jpg, png, webp or heic expected")
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="Empty upload")
    if len(data) > MAX_SCAN_BYTES:
        raise HTTPException(status_code=413, detail="Image too large (max 25 MB)")
    settings = get_settings()
    rel = os.path.join(subdir, str(user.id), f"{uuid_mod.uuid4()}{ext}")
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


def _record_out(r: MedicalRecord, user: User | None = None) -> dict:
    paths = r.source_image_paths_json or ([r.source_image_path] if r.source_image_path else [])
    # scan_count/has_scans count only scans that actually resolve on disk
    # (resolve_scan_file: owner dir + isfile). A record whose scan file was
    # deleted/lost therefore reports 0/false and the UI shows no gallery,
    # instead of a broken thumbnail/lightbox for a 404.
    if user is not None:
        scan_count = sum(1 for p in paths if resolve_scan_file(user, p) is not None)
    else:
        scan_count = len([p for p in paths if p])
    return {
        "id": str(r.id),
        "title": r.title,
        "content": r.content,
        "source_type": r.source_type,
        "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None,
        "encounter_id": str(r.encounter_id) if r.encounter_id else None,
        "source_image_path": r.source_image_path,
        "source_image_paths": paths,
        "scan_count": scan_count,
        "has_scans": scan_count > 0,
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
        )
        .outerjoin(MedicalRecord, MedicalRecord.encounter_id == MedicalEncounter.id)
        .filter(MedicalEncounter.user_id == user.id)
        .group_by(MedicalEncounter.id)
        .order_by(MedicalEncounter.encounter_date.desc(), MedicalEncounter.created_at.desc())
        .all()
    )
    # scan_count counts only scans that resolve on disk (resolve_scan_file).
    rec_rows = (
        db.query(MedicalRecord.encounter_id, MedicalRecord.source_image_paths_json, MedicalRecord.source_image_path)
        .filter(MedicalRecord.user_id == user.id)
        .all()
    )
    resolvable: dict = {}
    for enc_id, paths_json, single in rec_rows:
        paths = paths_json or ([single] if single else [])
        resolvable[enc_id] = resolvable.get(enc_id, 0) + sum(1 for p in paths if resolve_scan_file(user, p) is not None)
    encounters = [_encounter_out(e, int(cnt or 0), int(resolvable.get(e.id, 0))) for e, cnt in rows]
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
    scan_count = sum(1 for r in records for p in (r.source_image_paths_json or ([r.source_image_path] if r.source_image_path else [])) if resolve_scan_file(user, p) is not None)
    return {
        "encounter": _encounter_out(e, len(records), scan_count),
        "records": [_record_out(r, user) for r in records],
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
def serve_scan(
    record_id: str,
    index: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream one scan image of a caller's record. Owner-only.

    ``?index=N`` selects the Nth path in ``source_image_paths_json``
    (0-based). Without an index (or when the list is absent) the legacy
    ``source_image_path`` (index 0) is served, preserving prior behavior.
    Out-of-range indexes are rejected with 404.
    """
    try:
        rid = uuid_mod.UUID(record_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Record not found")
    rec = db.query(MedicalRecord).filter(MedicalRecord.id == rid, MedicalRecord.user_id == user.id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="Record not found")
    if not rec.source_image_path:
        raise HTTPException(status_code=404, detail="Scan image missing")
    paths = rec.source_image_paths_json or [rec.source_image_path]
    if index is None:
        rel_path = rec.source_image_path
    else:
        if index < 0 or index >= len(paths):
            raise HTTPException(status_code=404, detail="Scan index out of range")
        rel_path = paths[index]
    abs_path = resolve_scan_file(user, rel_path)
    if not abs_path:
        raise HTTPException(status_code=404, detail="Scan image missing")
    return FileResponse(abs_path)

from pydantic import BaseModel

class MedicalRecordPatch(BaseModel):
    title: str | None = None
    content: str | None = None

class MedicalEncounterPatch(BaseModel):
    title: str | None = None

@router.patch("/medical/records/{record_id}")
def patch_medical_record(record_id: str, req: MedicalRecordPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Edit a record's title/text (Card 8 fix: wrong auto-title e.g. 'Krvna kartica - zapis 2000').
    Title is agent-supplied on create (no server auto-title); this lets owner correct it."""
    try: rid = uuid_mod.UUID(record_id)
    except ValueError: raise HTTPException(status_code=404, detail="Record not found")
    rec = db.query(MedicalRecord).filter(MedicalRecord.id == rid, MedicalRecord.user_id == user.id).first()
    if not rec: raise HTTPException(status_code=404, detail="Record not found")
    if req.title is not None: rec.title = req.title.strip() or None
    if req.content is not None:
        if not req.content.strip(): raise HTTPException(status_code=400, detail="content must not be empty")
        rec.content = req.content
    # Card 8 propagation: Records.tsx renders the ENCOUNTER title as the card
    # headline. Keep it in sync when this record is the encounter's only record.
    if req.title is not None and rec.encounter_id is not None:
        enc = db.query(MedicalEncounter).filter(MedicalEncounter.id == rec.encounter_id, MedicalEncounter.user_id == user.id).first()
        if enc is not None:
            count = db.query(MedicalRecord).filter(MedicalRecord.encounter_id == enc.id).count()
            if count == 1 and enc.title != rec.title:
                enc.title = rec.title
    db.add(rec); db.commit(); db.refresh(rec)
    return _record_out(rec, user)

@router.patch("/medical/encounters/{encounter_id}")
def patch_medical_encounter(encounter_id: str, req: MedicalEncounterPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try: eid = uuid_mod.UUID(encounter_id)
    except ValueError: raise HTTPException(status_code=404, detail="Encounter not found")
    e = db.query(MedicalEncounter).filter(MedicalEncounter.id == eid, MedicalEncounter.user_id == user.id).first()
    if not e: raise HTTPException(status_code=404, detail="Encounter not found")
    if req.title is not None: e.title = req.title.strip() or None
    db.add(e); db.commit(); db.refresh(e)
    return _encounter_out(e)
