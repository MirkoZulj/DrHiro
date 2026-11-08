"""OpenClaw agent tools — supplements & medications (0.9.0, spec §5.7).

New module per the standing rule: NEVER grow openclaw_tools.py. Imports the
shared helpers from it and mounts the same /tools prefix.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import AliasChoices, BaseModel, Field
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.models import DoseEvent, MedicationItem, MedicationItemEvent, User
from drhiro_api.routers.medical import store_scan
from drhiro_api.routers.openclaw_tools import ToolResponse, _resolve_user, _tool_audit
from drhiro_api.services import medications as medsvc
from drhiro_api.services.alerts import recompute_medication_alerts
from drhiro_api.services.schemas_meds import normalise_ingredients
from drhiro_api.services.day_boundary import nutrition_date

router = APIRouter(prefix="/tools", tags=["openclaw-tools-meds"])

MED_IDEMPOTENCY_MINUTES = 10


class AddMedRequest(BaseModel):
    kind: str = "supplement"
    name: str = Field(min_length=1, max_length=255)
    brand: str | None = None
    form: str | None = None
    ingredients_json: list | None = Field(default=None, validation_alias=AliasChoices("ingredients_json", "ingredients"))
    dose_amount: float | None = None
    dose_unit: str | None = None
    unit_label: str | None = None
    schedule_json: dict | None = None
    create_reminder: bool = False
    start_date: str | None = None
    end_date: str | None = None
    reason: str | None = None
    prescribed_by: str | None = None
    medical_record_id: str | None = None
    label_image_path: str | None = None


class UpdateMedRequest(BaseModel):
    item_id: str
    effective_date: str | None = None
    dose_amount: float | None = None
    dose_unit: str | None = None
    unit_label: str | None = None
    schedule_json: dict | None = None
    brand: str | None = None
    form: str | None = None
    ingredients_json: list | None = Field(default=None, validation_alias=AliasChoices("ingredients_json", "ingredients"))
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


class LogDoseEntry(BaseModel):
    item_id: str | None = None
    name: str | None = None
    amount: float | None = None

class LogDoseRequest(BaseModel):
    item_id: str | None = None
    name: str | None = None
    taken_at: datetime | None = None
    status: str = "taken"
    amount: float | None = None
    occurrence_id: str | None = None
    note: str | None = None
    unarchive: bool = False
    entries: list[LogDoseEntry] | None = None


class ListDosesRequest(BaseModel):
    date: str | None = None
    item_id: str | None = None
    view: str = "week"
    offset: int = 0

class ListNutrientCodesRequest(BaseModel):
    pass

class GetMyIntakeRequest(BaseModel):
    date: str | None = None
    view: str = "day"
    offset: int = 0


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
        "unit_label": getattr(item,"unit_label",None),
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
    warnings=[]
    if req.create_reminder: warnings.append("create_reminder is ignored")
    if req.schedule_json is not None: warnings.append("schedule_json is ignored")
    try:
        ingredients = normalise_ingredients(req.ingredients_json) if req.ingredients_json else None
    except Exception as e:
        return ToolResponse(ok=False, message=str(e))
    item = MedicationItem(
        user_id=user.id,
        kind=req.kind,
        name=req.name.strip(),
        brand=req.brand,
        form=req.form,
        ingredients_json=ingredients,
        dose_amount=req.dose_amount,
        dose_unit=req.dose_unit,
        unit_label=req.unit_label,
        schedule_json=None,
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
    recompute_medication_alerts(db, user.id)
    _tool_audit(db, user, "tools.add_medication_item", "medication_item", str(item.id), {"name": item.name})
    db.commit()
    db.refresh(item)
    return ToolResponse(ok=True, data={"item": _item_out(item), "already_exists": False, "warnings": warnings})


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
    if req.unit_label is not None:
        item.unit_label = req.unit_label
    if req.schedule_json is not None:
        pass  # legacy ignored
    if getattr(req, "ingredients_json", None) is not None:
        try: item.ingredients_json = normalise_ingredients(req.ingredients_json)
        except Exception as e: return ToolResponse(ok=False, message=str(e))
    for f in ("brand", "form", "start_date", "end_date", "reason", "notes"):
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
    if req.status == "paused":
        return ToolResponse(ok=False, message="paused is retired; archive with stopped, re-add with resume")
    if req.status not in ("stopped",):
        return ToolResponse(ok=False, message="status must be stopped (archive)")
    effective = _date(req.effective_date) or datetime.now(timezone.utc).date()
    before = {"status": item.status}
    item.status = req.status
    medsvc.record_change(db, item, req.status if req.status == "stopped" else "paused",
                         effective, before, {"status": req.status})
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
    """Log one or many doses. Archived match returns archived_match (no write) unless unarchive=true."""
    entries = req.entries or [{"item_id": req.item_id, "name": req.name, "amount": req.amount}]
    taken_at = req.taken_at or datetime.now(timezone.utc)
    if taken_at.tzinfo is None: taken_at = taken_at.replace(tzinfo=timezone.utc)
    results=[]; already=[]
    for ent in entries:
        item=None
        iid = ent.get("item_id") if isinstance(ent, dict) else getattr(ent, "item_id", None)
        nm = ent.get("name") if isinstance(ent, dict) else getattr(ent, "name", None)
        amt_in = ent.get("amount") if isinstance(ent, dict) else getattr(ent, "amount", None)
        amt_in = amt_in if amt_in is not None else req.amount
        if iid:
            item = db.query(MedicationItem).filter(MedicationItem.id == uuid.UUID(iid), MedicationItem.user_id == user.id).first()
            if not item: return ToolResponse(ok=False, message="Medication not found")
        elif nm:
            matches = db.query(MedicationItem).filter(MedicationItem.user_id == user.id, MedicationItem.status != "stopped", MedicationItem.name.ilike(f"%{nm.strip()}%")).all()
            if len(matches) > 1:
                return ToolResponse(ok=True, data={"ambiguous": True, "candidates": [{"item_id": str(m.id), "name": m.name} for m in matches]})
            item = matches[0] if matches else None
            if item is None:
                # check archived for archived_match
                arch = db.query(MedicationItem).filter(MedicationItem.user_id == user.id, MedicationItem.status == "stopped", MedicationItem.name.ilike(f"%{nm.strip()}%")).first()
                if arch:
                    if not req.unarchive:
                        return ToolResponse(ok=True, data={"archived_match": True, "item": _item_out(arch)})
                    item = arch
                else:
                    return ToolResponse(ok=False, message=f"No active medication matches '{nm}'. List your items first.")
        else:
            return ToolResponse(ok=False, message="item_id or name required")
        if item.status == "stopped" and not req.unarchive:
            return ToolResponse(ok=True, data={"archived_match": True, "item": _item_out(item)})
        if item.status == "stopped" and req.unarchive:
            item.status="active"
            medsvc.record_change(db, item, "resumed", taken_at.date(), {"status":"stopped"}, {"status":"active"})
        recent = db.query(DoseEvent).filter(DoseEvent.item_id == item.id,
            DoseEvent.taken_at >= taken_at - timedelta(minutes=MED_IDEMPOTENCY_MINUTES),
            DoseEvent.taken_at <= taken_at + timedelta(minutes=MED_IDEMPOTENCY_MINUTES),
            DoseEvent.status == req.status).first()
        if recent:
            already.append(str(item.id)); results.append({"dose": _dose_out(recent), "already_logged": True}); continue
        amount = amt_in if amt_in is not None else (item.dose_amount if item.dose_amount is not None else 1.0)
        dose = DoseEvent(user_id=user.id, item_id=item.id, scheduled_for=None, taken_at=taken_at,
                         status=req.status, amount=amount, unit=item.dose_unit, source="telegram", note=req.note)
        db.add(dose); db.flush()
        if req.status == "taken": medsvc.decrement_stock(item, amount or 1.0)
        _tool_audit(db, user, "tools.log_dose", "dose_event", str(dose.id), {"status": req.status, "item": item.name})
        results.append({"dose": _dose_out(dose), "already_logged": False, "item_id": str(item.id)})
    db.commit()
    return ToolResponse(ok=True, data={"doses": [r["dose"] for r in results], "results": results, "already_logged": already})

@router.post("/list_doses", response_model=ToolResponse)
def tool_list_doses(req: ListDosesRequest, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Intake shape (§3.5): days with intake only, same as GET /medications/intake."""
    from drhiro_api.routers.medications import get_intake
    # reuse logic via direct call with view/offset (date param maps to day granularity fallback)
    if req.date:
        day = _date(req.date)
        q = db.query(DoseEvent).filter(DoseEvent.user_id == user.id, DoseEvent.status == "taken").all()
        items = {i.id: i for i in db.query(MedicationItem).filter(MedicationItem.user_id == user.id).all()}
        day_doses=[]
        for d in q:
            if nutrition_date(d.taken_at, user.timezone) == day:
                it=items.get(d.item_id)
                if it: day_doses.append({"dose_id": str(d.id), "item_id": str(it.id), "name": it.name, "amount": d.amount, "taken_at": d.taken_at})
        _tool_audit(db, user, "tools.list_doses"); db.commit()
        return ToolResponse(ok=True, data={"date": day.isoformat(), "days": [{"date": day.isoformat(), "doses": day_doses}] if day_doses else [], "doses": day_doses})
    data = get_intake(view=req.view if req.view in ("week","month") else "week", offset=req.offset, kind="all", user=user, db=db)
    _tool_audit(db, user, "tools.list_doses"); db.commit()
    return ToolResponse(ok=True, data=data)

@router.post("/list_nutrient_codes", response_model=ToolResponse)
def tool_list_nutrient_codes(req: ListNutrientCodesRequest = ListNutrientCodesRequest(), user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Catalogue codes from spec §4.1 (micros.py stub until 0.11.0-b)."""
    try:
        from drhiro_nutrition.micros import NUTRIENT_CATALOGUE
        codes=[{"code": c.code, "label_en": c.label_en, "label_hr": c.label_hr, "unit": c.unit, "group": c.group} for c in NUTRIENT_CATALOGUE]
    except Exception:
        codes=[{"code": c, "label_en": c, "label_hr": c, "unit": None, "group": None} for c in __import__("drhiro_api.services.schemas_meds", fromlist=["NUTRIENT_CODES"]).NUTRIENT_CODES]
    _tool_audit(db, user, "tools.list_nutrient_codes"); db.commit()
    return ToolResponse(ok=True, data={"codes": codes, "count": len(codes)})

@router.post("/get_my_intake", response_model=ToolResponse)
def tool_get_my_intake(req: GetMyIntakeRequest, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """§4.6 payload condensed: per nutrient total, % ref, food/supp split, completeness."""
    from drhiro_api.routers.intake import overview as _overview, day as _day
    from datetime import date as _d
    if req.date:
        body = _day(date=_date(req.date), user=user, db=db)
    else:
        body = _overview(view="week" if req.view == "week" else "day" if req.view == "day" else "week",
                         offset=req.offset or 0, codes=None, source="all", anchor=None, user=user, db=db)
    nutrients = [{"code": n["code"], "label": n["label"], "ref": n["ref"], "unit": n["unit"],
                  "days": [{"date": d["date"], "food": d["food"], "supp": d["supp"], "total": d["total"],
                            "pct": d["pct"], "completeness": d["completeness"], "status": d["status"],
                            "above_ul": d["above_ul"]} for d in n["days"]]} for n in body["nutrients"]]
    _tool_audit(db, user, "tools.get_my_intake"); db.commit()
    return ToolResponse(ok=True, data={"start": body["start"], "end": body["end"], "tz": body["tz"],
        "dri_version": body["dri_version"], "reference_profile": body["reference_profile"],
        "completeness_overall": body["completeness_overall"], "unknowns": body["unknowns"],
        "nutrients": nutrients})

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
