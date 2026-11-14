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
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from drhiro_api.config import get_settings
from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import MedicalEncounter, MedicalRecord, User
from drhiro_api.services.day_boundary import local_calendar_date, resolve_tz

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


@router.get("/medical/timeline")
def get_timeline(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
):
    """Horizontal timeline derivation (Part A, 0.12.0-b).

    One event per distinct date per encounter. Dates come from
    ``encounter_date`` plus the local calendar date of each record's
    ``recorded_at`` — EXCEPT records whose ``recorded_at`` is just the upload
    timestamp (``recorded_at == created_at`` within 60s, i.e. no transcription
    dating was ever applied), which are ignored so upload day doesn't leak in
    as a phantom series point. Sort ascending by date; ``?from=&to=`` filter
    inclusive on the event date.
    """
    tz = resolve_tz(user.timezone)
    today = datetime.now(tz).date()

    from_d = _parse_iso_date(from_) if from_ else None
    to_d = _parse_iso_date(to) if to else None

    encounters = (
        db.query(MedicalEncounter)
        .filter(MedicalEncounter.user_id == user.id)
        .order_by(MedicalEncounter.encounter_date.asc())
        .all()
    )
    records = (
        db.query(MedicalRecord)
        .filter(MedicalRecord.user_id == user.id)
        .order_by(MedicalRecord.recorded_at.asc())
        .all()
    )
    recs_by_enc: dict = {}
    for r in records:
        recs_by_enc.setdefault(r.encounter_id, []).append(r)

    events: list[dict] = []
    for enc in encounters:
        recs = recs_by_enc.get(enc.id, [])
        # distinct dates -> record ids (surgery-day dating already landed in
        # recorded_at via 514008f-era tooling; upload-timestamp rows excluded).
        by_date: dict = {}
        # encounter_date is always a candidate.
        by_date.setdefault(enc.encounter_date, [])
        for r in recs:
            if _is_upload_timestamp_only(r):
                continue
            d = local_calendar_date(r.recorded_at, tz)
            by_date.setdefault(d, [])
            by_date[d].append(str(r.id))
        dates = sorted(by_date)
        series_id = (
            str(enc.id) if len(dates) > 1 else None
        )
        kind = _timeline_kind(enc, recs)
        for d in dates:
            if from_d and d < from_d:
                continue
            if to_d and d > to_d:
                continue
            events.append(
                {
                    "id": f"enc:{enc.id}:{d.isoformat()}",
                    "encounter_id": str(enc.id),
                    "date": d.isoformat(),
                    "kind": kind,
                    "title": enc.title or (recs[0].title if recs else None) or "Medical record",
                    "facility": enc.facility,
                    "record_ids": by_date[d],
                    "series_id": series_id,
                    "series_size": len(dates),
                }
            )
    events.sort(key=lambda e: e["date"])
    birth_date = user.date_of_birth
    # A6 handled client-side: null birth_date -> earliest event -1y.
    return {
        "birth_date": birth_date,
        "today": today.isoformat(),
        "timezone": user.timezone,
        "events": events,
        "count": len(events),
    }


def _is_upload_timestamp_only(r: MedicalRecord) -> bool:
    """True when recorded_at carries no transcription dating info: it equals
    created_at within 60s (a fresh upload auto-dated to now, never backdated
    by transcription, surgery-day rule, or explicit recorded_at)."""
    if r.recorded_at is None or r.created_at is None:
        return False
    a = r.recorded_at if r.recorded_at.tzinfo else r.recorded_at.replace(tzinfo=timezone.utc)
    b = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
    return abs((a - b).total_seconds()) <= 60


def _parse_iso_date(s: str):
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


# Clinical-kind derivation for timeline dots. ``source_type`` values in this
# app are transport channels (telegram_photo / manual / ... — see Records.tsx
# SOURCE_LABELS), NOT clinical kinds, so kind is inferred from encounter
# title/facility/notes + record titles. Unknown -> "other".
_KIND_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("surgery", ("operac", "kirur", "surgery", "surgical", "operation", "tonsillectom", "tonsilektom", "uppp", "osas", "aneste")),
    ("imaging", ("ct ", "mri", "mr ", "xray", "x-ray", "rentgen", "ultrazvu", "ultrasound", "pet", "mamograf", "scan", "snim")),
    ("hospital", ("hospital", "bolnic", "clinic", "klinik", "ward", "odjel", "emergency", "hitna", "inpatient", "stay", "boravak")),
    ("lab", ("lab", "blood", "krv", "hemoglobin", "panel", "nalaz", "biochem", "hormone", "glucose", "kolesterol", "cholesterol", "urine", "mokrac")),
    ("rx", ("prescription", "recept", "pharmac", "lek", "lijek", "medication", "terapij", "dose", "doz")),
    ("visit", ("visit", "exam", "pregled", "consult", "checkup", "check-up", "appointment", "doctor", "liječnik", "doktor", "ordinac")),
)


def _timeline_kind(enc, recs) -> str:
    from drhiro_api.routers.openclaw_tools import _is_surgery_encounter

    if _is_surgery_encounter(enc):
        return "surgery"
    hay = " ".join(
        str(x or "")
        for x in [enc.title, enc.facility, enc.notes, *(r.title for r in recs)]
    ).lower()
    for kind, kws in _KIND_KEYWORDS:
        if any(k in hay for k in kws):
            return kind
    return "other"


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
    # Notes/text: the Records detail page edits both title and notes (main's
    # encounter edit UI); without this the notes draft was silently dropped.
    notes: str | None = None

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
    if req.notes is not None: e.notes = req.notes.strip() or None
    db.add(e); db.commit(); db.refresh(e)
    return _encounter_out(e)
