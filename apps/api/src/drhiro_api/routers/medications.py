"""Medications JWT API (0.9.0, spec §5.6).

Every route is tenant-scoped by user_id. Doses are logged with server-side
slot matching (±4 h); a second dose in the same slot is ad-hoc
(scheduled_for NULL) and the partial unique index enforces one event per slot.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import DoseEvent, MedicationItem, MedicationItemEvent, User
from drhiro_api.security import audit
from drhiro_api.services import medications as medsvc
from drhiro_api.services.alerts import recompute_medication_alerts
from drhiro_api.services.medication_reminders import set_reminders_enabled, sync_reminders_for_item

router = APIRouter(prefix="/medications", tags=["medications"])

MED_FIELDS = (
    "kind", "name", "brand", "form", "ingredients_json", "dose_amount", "dose_unit",
    "schedule_json", "timezone", "start_date", "end_date", "reason", "prescribed_by",
    "medical_record_id", "label_image_path", "stock_quantity", "notes",
)


class MedicationCreateRequest(BaseModel):
    kind: str = "supplement"
    name: str = Field(min_length=1, max_length=255)
    brand: str | None = None
    form: str | None = None
    ingredients_json: list | None = None
    dose_amount: float | None = None
    dose_unit: str | None = None
    schedule_json: dict | None = None
    timezone: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    reason: str | None = None
    prescribed_by: str | None = None
    medical_record_id: uuid.UUID | None = None
    label_image_path: str | None = None
    stock_quantity: float | None = None
    notes: str | None = None
    create_reminder: bool = False


class MedicationPatchRequest(BaseModel):
    kind: str | None = None
    name: str | None = None
    brand: str | None = None
    form: str | None = None
    ingredients_json: list | None = None
    dose_amount: float | None = None
    dose_unit: str | None = None
    schedule_json: dict | None = None
    start_date: date | None = None
    end_date: date | None = None
    reason: str | None = None
    prescribed_by: str | None = None
    stock_quantity: float | None = None
    notes: str | None = None
    effective_date: date | None = None
    create_reminder: bool = False


class DoseCreateRequest(BaseModel):
    taken_at: datetime | None = None
    status: str = "taken"
    amount: float | None = None
    note: str | None = None


def _date_or(d) -> date | None:
    if d is None:
        return None
    return d.date() if isinstance(d, datetime) else d


def _item_out(item: MedicationItem) -> dict:
    return {
        "id": str(item.id),
        "kind": item.kind,
        "name": item.name,
        "brand": item.brand,
        "form": item.form,
        "ingredients_json": item.ingredients_json,
        "dose_amount": item.dose_amount,
        "dose_unit": item.dose_unit,
        "schedule_json": item.schedule_json,
        "timezone": item.timezone,
        "start_date": item.start_date,
        "end_date": item.end_date,
        "status": item.status,
        "reason": item.reason,
        "prescribed_by": item.prescribed_by,
        "medical_record_id": str(item.medical_record_id) if item.medical_record_id else None,
        "source": item.source,
        "label_image_path": item.label_image_path,
        "reminder_ids_json": item.reminder_ids_json,
        "stock_quantity": item.stock_quantity,
        "notes": item.notes,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _dose_out(d: DoseEvent) -> dict:
    return {
        "id": str(d.id),
        "item_id": str(d.item_id),
        "scheduled_for": d.scheduled_for,
        "taken_at": d.taken_at,
        "status": d.status,
        "amount": d.amount,
        "unit": d.unit,
        "source": d.source,
        "note": d.note,
    }


def _get_item(db: Session, user: User, item_id: str) -> MedicationItem:
    try:
        uid = uuid.UUID(item_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Medication not found")
    item = db.query(MedicationItem).filter(MedicationItem.id == uid, MedicationItem.user_id == user.id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Medication not found")
    return item


def _after_medication_write(db: Session, user: User, item: MedicationItem, create_reminder: bool):
    if create_reminder:
        sync_reminders_for_item(db, user, item)
    if item.status != "active":
        set_reminders_enabled(db, item, False)
    db.flush()
    recompute_medication_alerts(db, user.id)


@router.post("/label-scan")
async def label_scan(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Store a label photo under medical_scans_dir/labels/<user>/ and return the path."""
    from drhiro_api.routers.medical import store_scan

    data = await file.read()
    rel = store_scan(user, data, file.content_type, subdir="labels")
    audit(db, "user", str(user.id), user.id, "medications.label_scan", "medication_item", None, {"path": rel})
    db.commit()
    return {"path": rel}


@router.get("")
def list_medications(status: str = "active", user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(MedicationItem).filter(MedicationItem.user_id == user.id)
    if status != "all":
        q = q.filter(MedicationItem.status == status)
    items = q.order_by(MedicationItem.created_at).all()
    return {"items": [_item_out(i) for i in items], "count": len(items)}


@router.post("")
def create_medication(req: MedicationCreateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if req.kind not in ("supplement", "medication"):
        raise HTTPException(status_code=400, detail="kind must be supplement or medication")
    item = MedicationItem(
        user_id=user.id,
        source="web",
        status="active",
        **{f: getattr(req, f) for f in MED_FIELDS},
    )
    db.add(item)
    db.flush()
    medsvc.record_change(db, item, "started", _date_or(req.start_date) or date.today(), None, {"status": "active"})
    _after_medication_write(db, user, item, req.create_reminder)
    audit(db, "user", str(user.id), user.id, "medications.create", "medication_item", str(item.id), {"name": item.name})
    db.commit()
    db.refresh(item)
    return _item_out(item)


@router.get("/doses")
def list_doses(
    frm: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    item_id: uuid.UUID | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(DoseEvent).filter(DoseEvent.user_id == user.id)
    if frm:
        q = q.filter(DoseEvent.taken_at >= datetime.combine(frm, time.min, tzinfo=timezone.utc))
    if to:
        q = q.filter(DoseEvent.taken_at < datetime.combine(to + timedelta(days=1), time.min, tzinfo=timezone.utc))
    if item_id:
        q = q.filter(DoseEvent.item_id == item_id)
    doses = q.order_by(DoseEvent.taken_at.desc()).limit(500).all()
    return {"doses": [_dose_out(d) for d in doses], "count": len(doses)}


def _adherence_buckets(granularity: str, offset: int) -> list[tuple[date, date]]:
    """Same window semantics as /trends/bucketed: day = trailing 7 days,
    week = trailing 12 ISO weeks, month = trailing 12 30-day buckets,
    `offset` shifts the whole window back."""
    today = date.today()
    if granularity == "day":
        return [(today - timedelta(days=offset + i), today - timedelta(days=offset + i)) for i in range(7)][::-1]
    size = 7 if granularity == "week" else 30
    n = 12
    out = []
    for i in range(n):
        b_end = today - timedelta(days=offset * size + i * size)
        b_start = b_end - timedelta(days=size - 1)
        out.append((b_start, b_end))
    return out[::-1]


@router.get("/adherence")
def adherence(
    granularity: str = "day",
    offset: int = 0,
    item_id: uuid.UUID | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Per bucket: expected, taken, skipped, not_logged, pct_taken|null
    (None = unknown — never 0)."""
    if granularity not in ("day", "week", "month"):
        raise HTTPException(status_code=400, detail="granularity must be day, week or month")
    items = db.query(MedicationItem).filter(MedicationItem.user_id == user.id)
    if item_id:
        items = items.filter(MedicationItem.id == item_id)
    items = items.filter(MedicationItem.status != "stopped").all()
    buckets = _adherence_buckets(granularity, offset)
    out = []
    for b_start, b_end in buckets:
        per_item = []
        totals = {"expected": 0, "taken": 0, "skipped": 0, "not_logged": 0}
        for item in items:
            a = medsvc.adherence(db, user, item, b_start, b_end)
            per_item.append({"item_id": str(item.id), "name": item.name, **a})
            for k in totals:
                totals[k] += a[k]
        logged = totals["taken"] + totals["skipped"]
        out.append({
            "start": b_start,
            "end": b_end,
            "pct_taken": (totals["taken"] / totals["expected"] * 100) if totals["expected"] and logged else None,
            "items": per_item,
            **totals,
        })
    return {"buckets": out, "granularity": granularity, "offset": offset}


@router.get("/{item_id}")
def get_medication(item_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = _get_item(db, user, item_id)
    events = (
        db.query(MedicationItemEvent)
        .filter(MedicationItemEvent.item_id == item.id)
        .order_by(MedicationItemEvent.effective_date)
        .all()
    )
    a30 = medsvc.adherence(db, user, item, date.today() - timedelta(days=29), date.today())
    return {
        "item": _item_out(item),
        "events": [
            {
                "id": str(e.id), "kind": e.kind, "effective_date": e.effective_date,
                "before_json": e.before_json, "after_json": e.after_json, "created_at": e.created_at,
            }
            for e in events
        ],
        "adherence_30d": a30,
    }


@router.patch("/{item_id}")
def patch_medication(item_id: str, req: MedicationPatchRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = _get_item(db, user, item_id)
    effective = req.effective_date or date.today()
    before = {f: getattr(item, f) for f in ("dose_amount", "dose_unit", "schedule_json")}
    for f in ("kind", "name", "brand", "form", "ingredients_json", "dose_amount", "dose_unit",
              "schedule_json", "start_date", "end_date", "reason", "prescribed_by",
              "stock_quantity", "notes"):
        val = getattr(req, f)
        if val is not None:
            setattr(item, f, val)
    if req.dose_amount is not None or req.dose_unit is not None:
        medsvc.record_change(db, item, "dose_changed", effective, before,
                             {"dose_amount": item.dose_amount, "dose_unit": item.dose_unit})
    if req.schedule_json is not None:
        medsvc.record_change(db, item, "schedule_changed", effective,
                             {"schedule_json": before["schedule_json"]}, {"schedule_json": item.schedule_json})
    _after_medication_write(db, user, item, req.create_reminder)
    audit(db, "user", str(user.id), user.id, "medications.update", "medication_item", str(item.id), {})
    db.commit()
    db.refresh(item)
    return _item_out(item)


def _transition(item_id: str, new_status: str, event_kind: str, body: dict | None, user: User, db: Session):
    item = _get_item(db, user, item_id)
    effective = (body or {}).get("effective_date") or date.today()
    if isinstance(effective, str):
        effective = date.fromisoformat(effective)
    before = {"status": item.status}
    item.status = new_status
    medsvc.record_change(db, item, event_kind, effective, before, {"status": new_status})
    # Stop/pause disables the item's reminders; resume re-enables them.
    set_reminders_enabled(db, item, enabled=(new_status == "active"))
    if new_status == "active":
        sync_reminders_for_item(db, user, item)
    audit(db, "user", str(user.id), user.id, f"medications.{event_kind}", "medication_item", str(item.id), {"status": new_status})
    db.commit()
    db.refresh(item)
    return _item_out(item)


@router.post("/{item_id}/stop")
def stop_medication(item_id: str, body: dict | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _transition(item_id, "stopped", "stopped", body, user, db)


@router.post("/{item_id}/pause")
def pause_medication(item_id: str, body: dict | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _transition(item_id, "paused", "paused", body, user, db)


@router.post("/{item_id}/resume")
def resume_medication(item_id: str, body: dict | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _transition(item_id, "active", "resumed", body, user, db)


@router.delete("/{item_id}")
def delete_medication(item_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = _get_item(db, user, item_id)
    set_reminders_enabled(db, item, False)
    db.delete(item)  # cascades events
    audit(db, "user", str(user.id), user.id, "medications.delete", "medication_item", item_id, {})
    db.commit()
    return {"ok": True}


@router.post("/{item_id}/doses")
def log_dose(item_id: str, req: DoseCreateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = _get_item(db, user, item_id)
    if req.status not in ("taken", "skipped"):
        raise HTTPException(status_code=400, detail="status must be taken or skipped")
    taken_at = req.taken_at or datetime.now(timezone.utc)
    if taken_at.tzinfo is None:
        taken_at = taken_at.replace(tzinfo=timezone.utc)
    slot = medsvc.match_slot(item, taken_at, user.timezone, db)
    dose = DoseEvent(
        user_id=user.id,
        item_id=item.id,
        scheduled_for=slot,
        taken_at=taken_at,
        status=req.status,
        amount=req.amount if req.amount is not None else item.dose_amount,
        unit=item.dose_unit,
        source="web",
        note=req.note,
    )
    db.add(dose)
    try:
        db.flush()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=409, detail="A dose for this slot already exists")
    if req.status == "taken":
        medsvc.decrement_stock(item, dose.amount or 1.0)
    audit(db, "user", str(user.id), user.id, "medications.dose_log", "dose_event", str(dose.id), {"status": req.status})
    db.commit()
    db.refresh(dose)
    return _dose_out(dose)


@router.delete("/doses/{dose_id}")
def delete_dose(dose_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        did = uuid.UUID(dose_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Dose not found")
    dose = db.query(DoseEvent).filter(DoseEvent.id == did, DoseEvent.user_id == user.id).first()
    if not dose:
        raise HTTPException(status_code=404, detail="Dose not found")
    db.delete(dose)
    audit(db, "user", str(user.id), user.id, "medications.dose_delete", "dose_event", dose_id, {})
    db.commit()
    return {"ok": True}
