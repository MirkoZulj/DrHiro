"""OpenClaw agent tools — supplements & medications (0.9.0, spec §5.7).

New module per the standing rule: NEVER grow openclaw_tools.py. Imports the
shared helpers from it and mounts the same /tools prefix.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.models import DoseEvent, MedicationItem, MedicationItemEvent, User
from drhiro_api.routers.medical import store_scan
from drhiro_api.routers.openclaw_tools import ToolResponse, _resolve_user, _tool_audit
from drhiro_api.services import medications as medsvc
from drhiro_api.services.alerts import recompute_medication_alerts
from drhiro_api.services.medication_reminders import set_reminders_enabled, sync_reminders_for_item

router = APIRouter(prefix="/tools", tags=["openclaw-tools-meds"])

MED_IDEMPOTENCY_MINUTES = 10


class AddMedRequest(BaseModel):
    kind: str = "supplement"
    name: str = Field(min_length=1, max_length=255)
    brand: str | None = None
    form: str | None = None
    ingredients_json: list | None = None
    dose_amount: float | None = None
    dose_unit: str | None = None
    schedule_json: dict | None = None
    start_date: str | None = None
    end_date: str | None = None
    reason: str | None = None
    prescribed_by: str | None = None
    medical_record_id: str | None = None
    label_image_path: str | None = None
    create_reminder: bool = False


class UpdateMedRequest(BaseModel):
    item_id: str
    effective_date: str | None = None
    dose_amount: float | None = None
    dose_unit: str | None = None
    schedule_json: dict | None = None
    brand: str | None = None
    form: str | None = None
    ingredients_json: list | None = None
    start_date: str | None = None
    end_date: str | None = None
    reason: str | None = None
    notes: str | None = None


class StopMedRequest(BaseModel):
    item_id: str
    effective_date: str | None = None
    status: str = "stopped"  # stopped | paused


class ResumeMedRequest(BaseModel):
    item_id: str
    effective_date: str | None = None


class ListMedsRequest(BaseModel):
    status: str = "active"  # active | all
    query: str | None = None


class LogDoseRequest(BaseModel):
    item_id: str | None = None
    name: str | None = None
    taken_at: datetime | None = None
    status: str = "taken"
    amount: float | None = None
    occurrence_id: str | None = None
    note: str | None = None


class ListDosesRequest(BaseModel):
    date: str | None = None
    item_id: str | None = None


class DeleteDoseRequest(BaseModel):
    dose_id: str


def _date(s: str | None):
    return datetime.fromisoformat(s).date() if s else None


def _item_out(item: MedicationItem) -> dict:
    return {
        "item_id": str(item.id),
        "kind": item.kind,
        "name": item.name,
        "brand": item.brand,
        "form": item.form,
        "dose_amount": item.dose_amount,
        "dose_unit": item.dose_unit,
        "schedule_json": item.schedule_json,
        "start_date": item.start_date,
        "end_date": item.end_date,
        "status": item.status,
        "reason": item.reason,
        "ingredients_json": item.ingredients_json,
        "stock_quantity": item.stock_quantity,
        "notes": item.notes,
    }


def _dose_out(d: DoseEvent) -> dict:
    return {
        "dose_id": str(d.id),
        "item_id": str(d.item_id),
        "scheduled_for": d.scheduled_for,
        "taken_at": d.taken_at,
        "status": d.status,
        "amount": d.amount,
        "note": d.note,
    }


@router.post("/add_medication_item", response_model=ToolResponse)
def tool_add_medication(req: AddMedRequest, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    if req.kind not in ("supplement", "medication"):
        return ToolResponse(ok=False, message="kind must be supplement or medication")
    # Idempotent on (user, lower(name), brand, start_date) with status active.
    q = db.query(MedicationItem).filter(
        MedicationItem.user_id == user.id,
        MedicationItem.status.in_(["active", "paused"]),
        MedicationItem.name.ilike(req.name.strip()),
    )
    if req.brand:
        q = q.filter(MedicationItem.brand == req.brand)
    start = _date(req.start_date)
    if start:
        q = q.filter(MedicationItem.start_date == start)
    existing = q.first()
    if existing:
        _tool_audit(db, user, "tools.add_medication_item", "medication_item", str(existing.id), {"already_exists": True})
        db.commit()
        return ToolResponse(ok=True, data={"item": _item_out(existing), "already_exists": True})
    item = MedicationItem(
        user_id=user.id,
        kind=req.kind,
        name=req.name.strip(),
        brand=req.brand,
        form=req.form,
        ingredients_json=req.ingredients_json,
        dose_amount=req.dose_amount,
        dose_unit=req.dose_unit,
        schedule_json=req.schedule_json,
        start_date=start,
        end_date=_date(req.end_date),
        status="active",
        reason=req.reason,
        prescribed_by=req.prescribed_by,
        medical_record_id=uuid.UUID(req.medical_record_id) if req.medical_record_id else None,
        label_image_path=req.label_image_path,
        source="label_photo" if req.label_image_path else "manual",
    )
    db.add(item)
    db.flush()
    medsvc.record_change(db, item, "started", start or datetime.now(timezone.utc).date(), None, {"status": "active"})
    if req.create_reminder:
        sync_reminders_for_item(db, user, item)
    recompute_medication_alerts(db, user.id)
    _tool_audit(db, user, "tools.add_medication_item", "medication_item", str(item.id), {"name": item.name})
    db.commit()
    db.refresh(item)
    return ToolResponse(ok=True, data={"item": _item_out(item), "already_exists": False})


@router.post("/update_medication_item", response_model=ToolResponse)
def tool_update_medication(req: UpdateMedRequest, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    item = db.query(MedicationItem).filter(
        MedicationItem.id == uuid.UUID(req.item_id), MedicationItem.user_id == user.id
    ).first()
    if not item:
        return ToolResponse(ok=False, message="Medication not found")
    effective = _date(req.effective_date) or datetime.now(timezone.utc).date()
    if req.dose_amount is not None or req.dose_unit is not None:
        before = {"dose_amount": item.dose_amount, "dose_unit": item.dose_unit}
        if req.dose_amount is not None:
            item.dose_amount = req.dose_amount
        if req.dose_unit is not None:
            item.dose_unit = req.dose_unit
        medsvc.record_change(db, item, "dose_changed", effective, before,
                             {"dose_amount": item.dose_amount, "dose_unit": item.dose_unit})
    if req.schedule_json is not None:
        before = {"schedule_json": item.schedule_json}
        item.schedule_json = req.schedule_json
        medsvc.record_change(db, item, "schedule_changed", effective, before, {"schedule_json": item.schedule_json})
        sync_reminders_for_item(db, user, item)
    for f in ("brand", "form", "ingredients_json", "start_date", "end_date", "reason", "notes"):
        val = getattr(req, f)
        if val is not None:
            setattr(item, f, {"start_date": _date, "end_date": _date}.get(f, lambda x: x)(val))
    recompute_medication_alerts(db, user.id)
    _tool_audit(db, user, "tools.update_medication_item", "medication_item", str(item.id), {})
    db.commit()
    db.refresh(item)
    return ToolResponse(ok=True, data={"item": _item_out(item)})


@router.post("/stop_medication_item", response_model=ToolResponse)
def tool_stop_medication(req: StopMedRequest, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    item = db.query(MedicationItem).filter(
        MedicationItem.id == uuid.UUID(req.item_id), MedicationItem.user_id == user.id
    ).first()
    if not item:
        return ToolResponse(ok=False, message="Medication not found")
    if req.status not in ("stopped", "paused"):
        return ToolResponse(ok=False, message="status must be stopped or paused")
    effective = _date(req.effective_date) or datetime.now(timezone.utc).date()
    before = {"status": item.status}
    item.status = req.status
    medsvc.record_change(db, item, req.status if req.status == "stopped" else "paused",
                         effective, before, {"status": req.status})
    set_reminders_enabled(db, item, False)
    _tool_audit(db, user, "tools.stop_medication_item", "medication_item", str(item.id), {"status": req.status})
    db.commit()
    db.refresh(item)
    return ToolResponse(ok=True, data={"item": _item_out(item)})


@router.post("/resume_medication_item", response_model=ToolResponse)
def tool_resume_medication(req: ResumeMedRequest, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    item = db.query(MedicationItem).filter(
        MedicationItem.id == uuid.UUID(req.item_id), MedicationItem.user_id == user.id
    ).first()
    if not item:
        return ToolResponse(ok=False, message="Medication not found")
    effective = _date(req.effective_date) or datetime.now(timezone.utc).date()
    item.status = "active"
    medsvc.record_change(db, item, "resumed", effective, {"status": "paused"}, {"status": "active"})
    set_reminders_enabled(db, item, True)
    _tool_audit(db, user, "tools.resume_medication_item", "medication_item", str(item.id), {})
    db.commit()
    db.refresh(item)
    return ToolResponse(ok=True, data={"item": _item_out(item)})


@router.post("/list_medication_items", response_model=ToolResponse)
def tool_list_medications(req: ListMedsRequest, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    q = db.query(MedicationItem).filter(MedicationItem.user_id == user.id)
    if req.status != "all":
        q = q.filter(MedicationItem.status == req.status)
    if req.query:
        q = q.filter(MedicationItem.name.ilike(f"%{req.query}%"))
    items = q.order_by(MedicationItem.created_at).all()
    _tool_audit(db, user, "tools.list_medication_items")
    db.commit()
    return ToolResponse(ok=True, data={"items": [_item_out(i) for i in items], "count": len(items)})


@router.post("/log_dose", response_model=ToolResponse)
def tool_log_dose(req: LogDoseRequest, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    item = None
    if req.item_id:
        item = db.query(MedicationItem).filter(
            MedicationItem.id == uuid.UUID(req.item_id), MedicationItem.user_id == user.id
        ).first()
        if not item:
            return ToolResponse(ok=False, message="Medication not found")
    elif req.name:
        matches = (
            db.query(MedicationItem)
            .filter(MedicationItem.user_id == user.id, MedicationItem.status == "active", MedicationItem.name.ilike(f"%{req.name.strip()}%"))
            .all()
        )
        if len(matches) > 1:
            return ToolResponse(ok=True, data={
                "ambiguous": True,
                "candidates": [{"item_id": str(m.id), "name": m.name, "dose_amount": m.dose_amount,
                                 "dose_unit": m.dose_unit, "schedule_json": m.schedule_json} for m in matches],
            })
        item = matches[0] if matches else None
        if item is None:
            return ToolResponse(ok=False, message=f"No active medication matches '{req.name}'. List your items first.")
    else:
        return ToolResponse(ok=False, message="item_id or name required")

    taken_at = req.taken_at or datetime.now(timezone.utc)
    if taken_at.tzinfo is None:
        taken_at = taken_at.replace(tzinfo=timezone.utc)

    # Telegram redelivery idempotency: same item within 10 min -> return it.
    recent = (
        db.query(DoseEvent)
        .filter(
            DoseEvent.item_id == item.id,
            DoseEvent.taken_at >= taken_at - timedelta(minutes=MED_IDEMPOTENCY_MINUTES),
            DoseEvent.taken_at <= taken_at + timedelta(minutes=MED_IDEMPOTENCY_MINUTES),
            DoseEvent.status == req.status,
        )
        .first()
    )
    if recent:
        _tool_audit(db, user, "tools.log_dose", "dose_event", str(recent.id), {"already_logged": True})
        db.commit()
        return ToolResponse(ok=True, data={"dose": _dose_out(recent), "already_logged": True})

    slot = medsvc.match_slot(item, taken_at, user.timezone, db)
    dose = DoseEvent(
        user_id=user.id,
        item_id=item.id,
        scheduled_for=slot,
        taken_at=taken_at,
        status=req.status,
        amount=req.amount if req.amount is not None else item.dose_amount,
        unit=item.dose_unit,
        source="telegram",
        note=req.note,
    )
    db.add(dose)
    try:
        db.flush()
    except Exception:
        db.rollback()
        return ToolResponse(ok=True, data={"already_logged": True, "dose": None, "message": "Already logged for this slot."})
    if req.status == "taken":
        medsvc.decrement_stock(item, dose.amount or 1.0)
    _tool_audit(db, user, "tools.log_dose", "dose_event", str(dose.id), {"status": req.status, "item": item.name})
    db.commit()
    db.refresh(dose)
    return ToolResponse(ok=True, data={"dose": _dose_out(dose), "already_logged": False, "slot_matched": slot})


@router.post("/list_doses", response_model=ToolResponse)
def tool_list_doses(req: ListDosesRequest, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    day = _date(req.date) or datetime.now(timezone.utc).date()
    q = db.query(DoseEvent).filter(
        DoseEvent.user_id == user.id,
        DoseEvent.taken_at >= datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
        DoseEvent.taken_at < datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
    )
    if req.item_id:
        q = q.filter(DoseEvent.item_id == uuid.UUID(req.item_id))
    doses = q.order_by(DoseEvent.taken_at).all()
    # Expected slots per active item for the day view (taken / skipped / not logged / upcoming).
    view = []
    items = db.query(MedicationItem).filter(
        MedicationItem.user_id == user.id, MedicationItem.status.in_(["active", "paused"])
    ).all()
    for it in items:
        if req.item_id and str(it.id) != req.item_id:
            continue
        slots = medsvc.expected_slots(it, day, day, user.timezone, db)
        item_doses = [d for d in doses if d.item_id == it.id]
        view.append({
            "item": _item_out(it),
            "slots": [
                {"scheduled_for": s,
                 "state": next((d.status for d in item_doses if d.scheduled_for == s), "not_logged")}
                for s in slots
            ],
            "ad_hoc_doses": [_dose_out(d) for d in item_doses if d.scheduled_for is None],
        })
    _tool_audit(db, user, "tools.list_doses")
    db.commit()
    return ToolResponse(ok=True, data={"date": day, "items": view, "doses": [_dose_out(d) for d in doses]})


@router.post("/delete_dose", response_model=ToolResponse)
def tool_delete_dose(req: DeleteDoseRequest, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    dose = db.query(DoseEvent).filter(
        DoseEvent.id == uuid.UUID(req.dose_id), DoseEvent.user_id == user.id
    ).first()
    if not dose:
        return ToolResponse(ok=False, message="Dose not found")
    db.delete(dose)
    _tool_audit(db, user, "tools.delete_dose", "dose_event", req.dose_id, {})
    db.commit()
    return ToolResponse(ok=True, data={"deleted": req.dose_id})


@router.post("/upload_medication_label", response_model=ToolResponse)
async def tool_upload_medication_label(
    file: UploadFile = File(...),
    user: User = Depends(_resolve_user),
    db: Session = Depends(get_db),
):
    data = await file.read()
    rel = store_scan(user, data, file.content_type, subdir="labels")
    _tool_audit(db, user, "tools.upload_medication_label", "medication_item", None, {"path": rel})
    db.commit()
    return ToolResponse(ok=True, data={"label_image_path": rel})
