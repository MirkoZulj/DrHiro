"""Medications JWT API (0.9.0, spec §5.6).

Every route is tenant-scoped by user_id. Doses are logged with server-side
slot matching (±4 h); a second dose in the same slot is ad-hoc
(scheduled_for NULL) and the partial unique index enforces one event per slot.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from datetime import date as _date

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import AliasChoices, BaseModel, Field
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import DoseEvent, MedicationItem, MedicationItemEvent, User
from drhiro_api.security import audit
from drhiro_api.services import medications as medsvc
from drhiro_api.services.alerts import recompute_medication_alerts
from drhiro_api.services.schemas_meds import normalise_ingredients, per_unit_amount, short_name, IngredientEntry

router = APIRouter(prefix="/medications", tags=["medications"])

MED_FIELDS = (
    "kind", "name", "short_name", "brand", "form", "ingredients_json", "dose_amount", "dose_unit",
    "schedule_json", "timezone", "start_date", "end_date", "reason", "prescribed_by", "unit_label",
    "medical_record_id", "label_image_path", "stock_quantity", "notes",
)


class MedicationCreateRequest(BaseModel):
    kind: str = "supplement"
    name: str = Field(min_length=1, max_length=255)
    short_name: str | None = Field(default=None, max_length=32)
    brand: str | None = None
    form: str | None = None
    ingredients_json: list | None = None
    dose_amount: float | None = None
    dose_unit: str | None = None
    unit_label: str | None = None
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
    short_name: str | None = Field(default=None, max_length=32)
    brand: str | None = None
    form: str | None = None
    # Alias "ingredients" accepted too: agents send label facts under
    # either name and a silently-ignored extra field hid composition.
    ingredients_json: list | None = Field(default=None, validation_alias=AliasChoices("ingredients_json", "ingredients"))
    dose_amount: float | None = None
    dose_unit: str | None = None
    unit_label: str | None = None
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
        "short_name": getattr(item, "short_name", None),
        "brand": item.brand,
        "form": item.form,
        "ingredients_json": item.ingredients_json,
        "dose_amount": item.dose_amount,
        "dose_unit": item.dose_unit,
        "unit_label": getattr(item,"unit_label",None),
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
    warnings=[]
    if req.create_reminder: warnings.append("create_reminder is ignored")
    if req.schedule_json is not None: warnings.append("schedule_json is ignored")
    data={f: getattr(req, f) for f in MED_FIELDS}
    data["schedule_json"]=None
    if data.get("ingredients_json"):
        try: data["ingredients_json"]=normalise_ingredients(data["ingredients_json"])
        except Exception as e: raise HTTPException(status_code=422, detail=str(e))
    item = MedicationItem(user_id=user.id, source="web", status="active", **data)
    db.add(item)
    db.flush()
    medsvc.record_change(db, item, "started", _date_or(req.start_date) or date.today(), None, {"status": "active"})
    _after_medication_write(db, user, item, req.create_reminder)
    audit(db, "user", str(user.id), user.id, "medications.create", "medication_item", str(item.id), {"name": item.name})
    db.commit()
    db.refresh(item)
    out=_item_out(item)
    if warnings: out["warnings"]=warnings
    return out


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
def adherence(response: Response,
    granularity: str = "day",
    offset: int = 0,
    item_id: uuid.UUID | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Per bucket: expected, taken, skipped, not_logged, pct_taken|null
    (None = unknown — never 0)."""
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "0.12"
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


# --- Intake v2 (§3.4) ---
import hashlib
from drhiro_api.services.day_boundary import nutrition_date

def _color_idx(item_id) -> int:
    return int(hashlib.sha1(str(item_id).encode()).hexdigest()[:4], 16) % 8

def _intake_window(view: str, offset: int, tz_name: str):
    from drhiro_api.services.day_boundary import resolve_tz, nutrition_date as nd
    from datetime import datetime, timezone as _tz
    tz = resolve_tz(tz_name)
    today_local = datetime.now(_tz.utc).astimezone(tz).date()
    if view == "month":
        m = today_local.month - 1 + offset
        y = today_local.year + m // 12
        m = m % 12 + 1
        start = date(y, m, 1)
        end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        end -= timedelta(days=1)
    else:
        d = today_local + timedelta(days=offset * 7)
        start = d - timedelta(days=d.weekday())
        end = start + timedelta(days=6)
    return start, end, tz_name

class IntakeEntry(BaseModel):
    item_id: uuid.UUID
    amount: float | None = None
    note: str | None = None

class IntakeCreateRequest(BaseModel):
    taken_at: datetime | None = None
    entries: list[IntakeEntry] = Field(min_length=1, max_length=20)

class DosePatchRequest(BaseModel):
    amount: float | None = None
    taken_at: datetime | None = None
    note: str | None = None

@router.get("/intake")
def get_intake(view: str = "week", offset: int = 0, kind: str = "all",
               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if view not in ("week", "month"): raise HTTPException(400, "view must be week|month")
    if kind not in ("all", "supplement", "medication"): raise HTTPException(400, "kind must be all|supplement|medication")
    start, end, tz_name = _intake_window(view, offset, user.timezone)
    from drhiro_api.services.day_boundary import local_calendar_date_window
    # query generously: nutrition days around window (boundary 4am)
    qstart = datetime.combine(start, time.min, tzinfo=timezone.utc) - timedelta(days=2)
    qend = datetime.combine(end, time.max, tzinfo=timezone.utc) + timedelta(days=2)
    doses = db.query(DoseEvent).filter(DoseEvent.user_id == user.id, DoseEvent.status == "taken",
        DoseEvent.taken_at >= qstart, DoseEvent.taken_at <= qend).order_by(DoseEvent.taken_at).all()
    items = {i.id: i for i in db.query(MedicationItem).filter(MedicationItem.user_id == user.id).all()}
    days: dict = {}
    agg: dict = {}
    for d in doses:
        dd = nutrition_date(d.taken_at, tz_name)
        if dd < start or dd > end: continue
        item = items.get(d.item_id)
        if not item: continue
        if kind != "all" and item.kind != kind: continue
        amt = d.amount if d.amount is not None else (item.dose_amount or 1.0)
        days.setdefault(dd, []).append({"dose_id": str(d.id), "item_id": str(item.id),
            "name": item.name, "short_name": getattr(item, "short_name", None) or short_name(item.name), "kind": item.kind,
            "color_idx": _color_idx(item.id), "amount": amt,
            "unit_label": getattr(item, "unit_label", None) or item.dose_unit,
            "taken_at": d.taken_at, "note": d.note})
        a = agg.setdefault(item.id, {"item_id": str(item.id), "name": item.name, "kind": item.kind,
            "short_name": getattr(item, "short_name", None) or short_name(item.name),
            "color_idx": _color_idx(item.id), "days": set(), "total": 0.0,
            "unit_label": getattr(item, "unit_label", None) or item.dose_unit, "last": d.taken_at})
        a["days"].add(dd); a["total"] += float(amt or 0)
        if d.taken_at > a["last"]: a["last"] = d.taken_at
    return {"view": view, "start": start.isoformat(), "end": end.isoformat(), "tz": tz_name,
        "days": [{"date": d.isoformat(), "doses": days[d]} for d in sorted(days)],
        "items": [{"item_id": v["item_id"], "name": v["name"], "short_name": v.get("short_name"), "kind": v["kind"], "color_idx": v["color_idx"],
                   "days_taken": len(v["days"]), "total_amount": round(v["total"], 3),
                   "unit_label": v["unit_label"], "last_taken_at": v["last"]} for v in sorted(agg.values(), key=lambda x: -len(x["days"]))]}

@router.post("/intake")
def post_intake(req: IntakeCreateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    taken_at = req.taken_at or datetime.now(timezone.utc)
    if taken_at.tzinfo is None: taken_at = taken_at.replace(tzinfo=timezone.utc)
    out_doses=[]; already=[]
    for e in req.entries:
        item = db.query(MedicationItem).filter(MedicationItem.id == e.item_id, MedicationItem.user_id == user.id).first()
        if not item: raise HTTPException(404, f"Item {e.item_id} not found")
        # 10-min idempotency per item
        recent = db.query(DoseEvent).filter(DoseEvent.item_id == item.id,
            DoseEvent.taken_at >= taken_at - timedelta(minutes=10),
            DoseEvent.taken_at <= taken_at + timedelta(minutes=10), DoseEvent.status == "taken").first()
        if recent:
            already.append(str(item.id)); continue
        amt = e.amount if e.amount is not None else (item.dose_amount if item.dose_amount is not None else 1.0)
        dose = DoseEvent(user_id=user.id, item_id=item.id, scheduled_for=None, taken_at=taken_at,
                         status="taken", amount=amt, unit=item.dose_unit, source="web", note=e.note)
        db.add(dose); db.flush()
        medsvc.decrement_stock(item, amt or 1.0)
        out_doses.append(dose)
    audit(db, "user", str(user.id), user.id, "medications.intake_bulk", "dose_event", None, {"count": len(out_doses)})
    db.commit()
    for d in out_doses: db.refresh(d)
    return {"doses": [_dose_out(d) for d in out_doses], "already_logged": already}

@router.patch("/doses/{dose_id}")
def patch_dose(dose_id: str, req: DosePatchRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try: did = uuid.UUID(dose_id)
    except ValueError: raise HTTPException(404, "Dose not found")
    dose = db.query(DoseEvent).filter(DoseEvent.id == did, DoseEvent.user_id == user.id).first()
    if not dose: raise HTTPException(404, "Dose not found")
    if req.amount is not None: dose.amount = req.amount
    if req.taken_at is not None:
        dose.taken_at = req.taken_at if req.taken_at.tzinfo else req.taken_at.replace(tzinfo=timezone.utc)
    if req.note is not None: dose.note = req.note
    db.commit(); db.refresh(dose)
    return _dose_out(dose)


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


def _composition_entries(raw):
    """Per-entry fallback: never 500 on legacy rows.

    Try normalise_ingredient (legacy RDA codes are mapped in schemas_meds);
    on ValueError (unknown code, per='capsule', …) degrade to an unmapped
    entry preserving name/amount/unit, per='unit'.
    """
    from drhiro_api.services.schemas_meds import LEGACY_CODE_MAP, _num, _unit_of, normalise_ingredient
    out = []
    for e in raw or []:
        try:
            out.append(normalise_ingredient(e).model_dump())
            continue
        except ValueError:
            pass
        # Salvage retry: legacy code / invalid per etc. cleaned, code preserved
        if isinstance(e, dict):
            c = dict(e)
            if c.get("nutrient_code") in LEGACY_CODE_MAP:
                c["nutrient_code"] = LEGACY_CODE_MAP[c["nutrient_code"]]
            if c.get("per") not in (None, "unit", "serving"):
                c["per"] = "unit"
            if c.get("mapping") not in (None, "confirmed", "suggested", "none"):
                c["mapping"] = "none"
            if c.get("form") not in (None, "elemental", "compound"):
                c["form"] = None
            try:
                out.append(normalise_ingredient(c).model_dump())
                continue
            except ValueError:
                pass
        try:
            if isinstance(e, dict):
                name = str(e.get("name") or e.get("component") or e.get("ingredient") or "").strip() or "Unknown"
                amt = _num(e.get("amount"))
                unit = e.get("unit")
                if amt is None:
                    for k, v in e.items():
                        if isinstance(v, (int, float)) and not isinstance(v, bool) and _unit_of(k):
                            amt = float(v); unit = _unit_of(k); break
            else:
                name, amt, unit = str(e), None, None
            out.append({"name": name, "nutrient_code": None, "mapping": "none",
                        "amount": amt, "unit": unit, "per": "unit",
                        "serving_units": 1, "form": None, "pct_dv": None})
        except Exception:
            out.append({"name": "Unknown", "nutrient_code": None, "mapping": "none",
                        "amount": None, "unit": None, "per": "unit",
                        "serving_units": 1, "form": None, "pct_dv": None})
    return out


@router.get("/{item_id}/composition")
def get_composition(item_id: str, scope: str = "unit", date_str: str | None = Query(default=None, alias="date"), start: str | None = None, end: str | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """§3.3/3.4 composition: per-unit × amount taken (day/period), with RDA ref.
    Returns both new shape (rows/taken) and legacy keys for old clients."""
    item = _get_item(db, user, item_id)
    if scope not in ("unit", "day", "period"): raise HTTPException(400, "scope must be unit|day|period")
    from drhiro_nutrition.dri import reference_for, age_from_dob
    from drhiro_api.services.schemas_meds import LEGACY_RDA_ALIASES
    from drhiro_api.services.day_boundary import nutrition_date
    from drhiro_api.services.schemas_meds import per_unit_amount
    age = age_from_dob(getattr(user, "date_of_birth", None))
    sex = getattr(user, "sex_for_health_calculations", None)
    entries = _composition_entries(item.ingredients_json or [])
    # taken totals
    taken_amt = 0.0; doses_n=0; days_taken=0
    if scope in ("day", "period"):
        q = db.query(DoseEvent).filter(DoseEvent.user_id == user.id, DoseEvent.item_id == item.id, DoseEvent.status == "taken")
        if scope == "day":
            if not date_str: raise HTTPException(400, "date is required for day scope")
            try: dd = _date.fromisoformat(date_str)
            except ValueError: raise HTTPException(400, "date must be ISO format YYYY-MM-DD")
            rows = [d for d in q.all() if nutrition_date(d.taken_at, user.timezone) == dd]
        else:
            if not start or not end: raise HTTPException(400, "start and end are required for period scope")
            try: s, e = _date.fromisoformat(start), _date.fromisoformat(end)
            except ValueError: raise HTTPException(400, "start/end must be ISO format YYYY-MM-DD")
            rows = [d for d in q.all() if s <= nutrition_date(d.taken_at, user.timezone) <= e]
        doses_n = len(rows)
        taken_amt = sum(float(d.amount if d.amount is not None else (item.dose_amount or 1.0)) for d in rows)
        days_taken = len({nutrition_date(d.taken_at, user.timezone) for d in rows})
    rows_out=[]; legacy=[]
    for entry in entries:
        per_u = per_unit_amount(entry)
        code = entry.get("nutrient_code")
        legacy_key = LEGACY_RDA_ALIASES.get(code, code) if code else None
        ref=None; unit=None; ref_kind="RDA"
        if code:
            _r = reference_for(code, sex, age)
            ref = _r["value"]; ref_kind = _r["kind"]; unit = entry.get("unit")
        taken_total = round(per_u * taken_amt, 3) if (per_u is not None and scope != "unit") else None
        avg = round(taken_total / days_taken, 3) if (taken_total is not None and days_taken) else None
        pct = round(taken_total / ref * 100, 1) if (taken_total is not None and ref) else None
        row={"name": entry.get("name"), "nutrient_code": code, "mapping": entry.get("mapping"),
             "per_unit": per_u, "unit": entry.get("unit") or unit, "taken_total": taken_total,
             "avg_per_day_taken": avg, "ref": ref, "ref_kind": ref_kind, "ul": None,
             "pct_dv": entry.get("pct_dv"), "per": entry.get("per"), "serving_units": entry.get("serving_units"),
             "form": entry.get("form"),
             "pct_ref": pct, "above_ul": False}
        rows_out.append(row)
        legacy.append({"component": entry.get("name"), "nutrient_key": legacy_key,
            "per_capsule": per_u, "unit": entry.get("unit") or unit, "per_dose_total": row["taken_total"],
            "rda": ref, "pct_per_capsule": pct, "raw": entry})
    taken = {"amount_units": round(taken_amt,3), "days_taken": days_taken, "doses": doses_n} if scope != "unit" else None
    return {"item_id": str(item.id), "item": _item_out(item), "name": item.name, "kind": item.kind,
            "dose_amount": item.dose_amount, "dose_unit": item.dose_unit, "unit_label": getattr(item,"unit_label",None),
            "age": age, "scope": scope, "taken": taken, "rows": rows_out, "legacy_rows": legacy}

class ConfirmMappingRequest(BaseModel):
    name: str | None = None
    nutrient_code: str | None = None

@router.post("/{item_id}/confirm-mapping")
def confirm_mapping(item_id: str, req: ConfirmMappingRequest,
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Flip one ingredient's mapping to 'confirmed' in place.

    Server-side, single-row update: every other entry — and every
    amount/unit/per field of the confirmed entry — is preserved verbatim
    from storage. This replaces the web client's former GET+PATCH of the
    whole ingredients_json list (2026-11-06: it read ingredients_json from
    the top level of GET /{id}, which wraps the item under "item", so the
    list came back empty and the PATCH wiped all but one amount-less row).
    Never creates an entry: unknown ingredient -> 404, nothing written.
    """
    from drhiro_api.services.schemas_meds import LEGACY_CODE_MAP
    item = _get_item(db, user, item_id)
    if not req.name and not req.nutrient_code:
        raise HTTPException(status_code=400, detail="name or nutrient_code is required")
    entries = list(item.ingredients_json or [])

    def _canon(e: dict):
        c = e.get("nutrient_code")
        return LEGACY_CODE_MAP.get(c, c) if isinstance(c, str) else c

    want_code = LEGACY_CODE_MAP.get(req.nutrient_code, req.nutrient_code) if req.nutrient_code else None
    idx = -1
    for i, e in enumerate(entries):
        if isinstance(e, dict) and req.name and str(
            e.get("name") or e.get("component") or e.get("ingredient") or ""
        ).casefold() == req.name.casefold():
            idx = i
            break
    if idx < 0 and want_code:
        for i, e in enumerate(entries):
            if isinstance(e, dict) and _canon(e) == want_code:
                idx = i
                break
    if idx < 0:
        raise HTTPException(status_code=404, detail="ingredient not found for this item")
    updated = dict(entries[idx])
    updated["mapping"] = "confirmed"
    entries[idx] = updated
    item.ingredients_json = entries
    audit(db, "user", str(user.id), user.id, "medications.confirm_mapping", "medication_item", str(item.id),
          {"name": updated.get("name") or updated.get("component"), "nutrient_code": updated.get("nutrient_code")})
    db.commit()
    db.refresh(item)
    return _item_out(item)

@router.patch("/{item_id}")
def patch_medication(item_id: str, req: MedicationPatchRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = _get_item(db, user, item_id)
    effective = req.effective_date or date.today()
    before = {f: getattr(item, f) for f in ("dose_amount", "dose_unit", "schedule_json")}
    warnings=[]
    if req.create_reminder: warnings.append("create_reminder is ignored")
    if getattr(req,"schedule_json",None) is not None: warnings.append("schedule_json is ignored")
    for f in ("kind", "name", "brand", "form", "dose_amount", "dose_unit", "unit_label",
              "start_date", "end_date", "reason", "prescribed_by",
              "stock_quantity", "notes"):
        val = getattr(req, f)
        if val is not None:
            setattr(item, f, val)
    # short_name: empty string clears back to the computed fallback.
    if req.short_name is not None:
        item.short_name = req.short_name.strip() or None
    if getattr(req,"ingredients_json",None) is not None:
        try: item.ingredients_json=normalise_ingredients(req.ingredients_json)
        except Exception as e: raise HTTPException(status_code=422, detail=str(e))
    if req.dose_amount is not None or req.dose_unit is not None:
        medsvc.record_change(db, item, "dose_changed", effective, before,
                             {"dose_amount": item.dose_amount, "dose_unit": item.dose_unit})
    # schedule_json ignored (legacy column retained)
    _after_medication_write(db, user, item, req.create_reminder)
    audit(db, "user", str(user.id), user.id, "medications.update", "medication_item", str(item.id), {})
    db.commit()
    db.refresh(item)
    out=_item_out(item)
    if warnings: out["warnings"]=warnings
    return out


def _transition(item_id: str, new_status: str, event_kind: str, body: dict | None, user: User, db: Session):
    item = _get_item(db, user, item_id)
    effective = (body or {}).get("effective_date") or date.today()
    if isinstance(effective, str):
        effective = date.fromisoformat(effective)
    before = {"status": item.status}
    item.status = new_status
    medsvc.record_change(db, item, event_kind, effective, before, {"status": new_status})
    # 0.11.0-a: reminders decoupled; archive/unarchive only
    audit(db, "user", str(user.id), user.id, f"medications.{event_kind}", "medication_item", str(item.id), {"status": new_status})
    db.commit()
    db.refresh(item)
    return _item_out(item)


@router.post("/{item_id}/stop")
def stop_medication(item_id: str, body: dict | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _transition(item_id, "stopped", "stopped", body, user, db)


@router.post("/{item_id}/pause")
def pause_medication(item_id: str, body: dict | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    raise HTTPException(status_code=410, detail="paused is retired; use archive (stop) / unarchive (resume)")


@router.post("/{item_id}/resume")
def resume_medication(item_id: str, body: dict | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _transition(item_id, "active", "resumed", body, user, db)

@router.post("/{item_id}/unarchive")
def unarchive_medication(item_id: str, body: dict | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _transition(item_id, "active", "resumed", body, user, db)


@router.delete("/{item_id}")
def delete_medication(item_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = _get_item(db, user, item_id)
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
    dose = DoseEvent(
        user_id=user.id,
        item_id=item.id,
        scheduled_for=None,
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
    # 0.11.0-a: reminder enqueue removed
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
