"""OpenClaw tool contract. Section 9.

The OpenClaw gateway presents a signed service token (type=service) and
identifies the user by telegram_id in the X-Telegram-Id header, resolved
server-side. The LLM never passes an arbitrary user_id for ordinary user
tools.

Each tool maps 1:1 to a function the drHiro skill exposes to the agent.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, text
from sqlalchemy.orm import Session, selectinload

from drhiro_api.db import get_db
from drhiro_api.deps import get_user_by_telegram_id
from drhiro_api.models import Activity, Alert, DoseEvent, Goal, IngestBatch, Meal, MealItem, Measurement, MedicalEncounter, MedicalRecord, MedicationItem, Reminder, ReminderOccurrence, User
from drhiro_api.routers.auth import mint_web_login_code
from drhiro_api.routers.dashboard import _measurements_since
from drhiro_api.security import audit, validate_service_token
from drhiro_api.services.day_boundary import (
    day_start_hour,
    local_now,
    nutrition_date,
    parse_timezone,
    resolve_tz,
    utc_offset_str,
)
from drhiro_rules.calculations import weight_trend
from drhiro_schema.metrics import MetricType

router = APIRouter(prefix="/tools", tags=["openclaw-tools"])

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _maybe_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


# Surgery-day dating rule: when a batch/record belongs to a surgery
# encounter, recorded_at must be the surgery day itself (encounter_date,
# noon in the user's timezone so the local date is unambiguous), never
# the day before/after produced by UTC-midnight arithmetic.
_SURGERY_KEYWORDS = (
    "operac", "kirur", "surgery", "surgical", "operation",
    "tonsillectom", "tonsilektom", "uppp", "osas",
)


def _is_surgery_encounter(enc) -> bool:
    if enc is None:
        return False
    hay = " ".join(str(x or "") for x in (enc.title, enc.facility, enc.notes)).lower()
    return any(k in hay for k in _SURGERY_KEYWORDS)


def _surgery_recorded_at(enc, user) -> datetime:
    tz = resolve_tz(user.timezone)
    return datetime(enc.encounter_date.year, enc.encounter_date.month, enc.encounter_date.day, 12, 0, tzinfo=tz)


_PLACEHOLDER_MARKER = "transcription pending"
_BURST_MERGE_WINDOW = timedelta(minutes=15)


def _merge_placeholder_siblings(db: Session, user: User, canonical, explicit_ids=None) -> dict:
    """Completion pass: fold leftover placeholder batches from the same burst
    into the canonical (enriched) record. Single canonical record per burst:
    scans are unioned (deduped by path) onto the canonical record, the
    placeholder records are deleted, and their encounters are deleted when
    they hold no other records. Returns merge stats for the response/audit.

    Sibling selection: explicit ``merge_record_ids`` (caller's burst knowledge,
    owner-scoped) PLUS auto-detected placeholders of the same user whose
    created_at is within +-15 min of the canonical record's created_at.
    """
    canonical_scans = list(canonical.source_image_paths_json or ([canonical.source_image_path] if canonical.source_image_path else []))
    seen = set(canonical_scans)
    siblings = []
    seen_ids = {canonical.id}
    if explicit_ids:
        for raw in explicit_ids:
            rid = _maybe_uuid(raw)
            if rid is None or rid in seen_ids:
                continue
            seen_ids.add(rid)
            sib = db.query(MedicalRecord).filter(MedicalRecord.id == rid, MedicalRecord.user_id == user.id).first()
            if sib is not None:
                siblings.append(sib)
    # Auto-merge same-burst placeholders (Telegram album fragments).
    if canonical.created_at is not None:
        lo = canonical.created_at - _BURST_MERGE_WINDOW
        hi = canonical.created_at + _BURST_MERGE_WINDOW
        auto = (
            db.query(MedicalRecord)
            .filter(
                MedicalRecord.user_id == user.id,
                MedicalRecord.source_type == "telegram_photo_batch",
                MedicalRecord.id != canonical.id,
                MedicalRecord.content.ilike(f"%{_PLACEHOLDER_MARKER}%"),
                MedicalRecord.created_at >= lo,
                MedicalRecord.created_at <= hi,
            )
            .all()
        )
        for sib in auto:
            if sib.id not in seen_ids:
                seen_ids.add(sib.id)
                siblings.append(sib)
    merged_ids: list[str] = []
    merged_scans = 0
    empty_encounters: list = []
    for sib in siblings:
        sib_scans = list(sib.source_image_paths_json or ([sib.source_image_path] if sib.source_image_path else []))
        for p in sib_scans:
            if p and p not in seen:
                seen.add(p)
                canonical_scans.append(p)
                merged_scans += 1
        merged_ids.append(str(sib.id))
        sib_enc_id = sib.encounter_id
        db.delete(sib)
        if sib_enc_id is not None:
            empty_encounters.append(sib_enc_id)
    if merged_ids:
        canonical.source_image_paths_json = canonical_scans
        canonical.source_image_path = canonical_scans[0] if canonical_scans else canonical.source_image_path
        db.flush()
        # Delete now-empty sibling encounters (placeholder encounters only
        # ever held their single placeholder record).
        for enc_id in set(empty_encounters):
            if enc_id == canonical.encounter_id:
                continue
            remaining = db.query(MedicalRecord).filter(MedicalRecord.encounter_id == enc_id).count()
            if remaining == 0:
                enc = db.query(MedicalEncounter).filter(MedicalEncounter.id == enc_id, MedicalEncounter.user_id == user.id).first()
                if enc is not None:
                    db.delete(enc)
        db.flush()
    return {"merged_record_ids": merged_ids, "merged_scan_count": merged_scans, "total_scan_count": len(canonical_scans)}


def _local_preview_kcal(db: Session, item) -> float | None:
    """Best-effort LOCAL-ONLY kcal for a draft meal item.

    The draft is committed with ``nutrients_json=None``; the agent resolves
    nutrition later. So the confirmation message for a freshly-parsed meal has
    no per-item energy yet. This looks the parser's local catalogue match up
    directly (no LLM / no online cascade — deliberately cheap and deterministic)
    and scales it to the item's grams/volume. Purely a preview: nothing is
    persisted. Returns None when nothing local matches.
    """
    try:
        n = item.nutrients_json or {}
        if isinstance(n, dict) and n.get("kcal") is not None:
            return round(float(n["kcal"]), 1)
        if not item.food_catalog_item_id:
            return None
        from sqlalchemy import or_
        from drhiro_api.models import Food, Nutrient
        from drhiro_api.food_search import nutrient_map
        uid = _maybe_uuid(item.food_catalog_item_id)
        q = db.query(Food).filter(or_(Food.id == uid, Food.external_id == str(item.food_catalog_item_id)))
        food = q.first()
        if food is None:
            return None
        code_by_id = {nu.id: nu.nutrient_code for nu in db.query(Nutrient).all()}
        nmap = nutrient_map(food, code_by_id)
        energy = nmap.get("energy")
        if energy is None:
            energy = (4.0 * (nmap.get("protein") or 0)
                      + 4.0 * (nmap.get("carbs") or 0)
                      + 9.0 * (nmap.get("fat") or 0))
        grams = item.grams or item.volume_ml or 100.0
        return round(float(energy) * grams / 100.0, 1)
    except Exception:
        return None


def _meal_confirmation(db: Session, user: User, meal: Meal) -> dict:
    """Everything the bot needs to say 'For Monday dinner I logged …'.

    Reports the day the meal was filed under (``nutrition_date`` + ``weekday``),
    the meal type, the LOCAL wall-clock time it used (from the user's stored
    timezone — never the gateway's UTC clock), every item with quantity/unit and
    kcal, and the meal total. Keys are ADDED alongside the legacy ones.
    """
    tz = resolve_tz(user.timezone)
    eaten_at = meal.eaten_at
    if eaten_at is None:
        eaten_local = local_now(user.timezone)
        nd = nutrition_date(datetime.now(timezone.utc), user.timezone)
    else:
        eaten_local = eaten_at.astimezone(tz)
        nd = nutrition_date(eaten_at, user.timezone)

    items = []
    total_kcal = 0.0
    for it in meal.items:
        n = it.nutrients_json if isinstance(it.nutrients_json, dict) else {}
        kcal = n.get("kcal")
        if kcal is None:
            kcal = _local_preview_kcal(db, it)
        if kcal is not None:
            total_kcal += float(kcal)
        items.append({
            "item_id": str(it.id),
            "display_name": it.display_name,
            "quantity": it.quantity,
            "unit": it.unit,
            "grams": it.grams,
            "volume_ml": it.volume_ml,
            "kcal": round(float(kcal), 1) if kcal is not None else None,
            "protein_g": n.get("protein_g"),
            "carbs_g": n.get("carbs_g"),
            "fat_g": n.get("fat_g"),
            "nutrients": it.nutrients_json,
            "resolved_food": n.get("resolved_food"),
            "unresolved": bool(
                it.source == "unresolved"
                or (isinstance(it.nutrients_json, dict) and it.nutrients_json.get("unresolved"))
                or it.nutrients_json is None
            ),
        })

    totals = meal.totals_json or {}
    return {
        "meal_id": str(meal.id),
        "status": meal.status,
        "meal_type": meal.meal_type,
        "eaten_at": eaten_at.isoformat() if eaten_at else None,
        "timezone": user.timezone,
        "nutrition_date": nd.isoformat(),
        "weekday": WEEKDAY_NAMES[eaten_local.weekday()],
        "local_date": eaten_local.date().isoformat(),
        "local_time": eaten_local.strftime("%H:%M"),
        "utc_offset": utc_offset_str(eaten_local),
        "day_start_hour": day_start_hour(),
        "items": items,
        "total_kcal": round(total_kcal, 1),
        "totals": {
            "kcal": totals.get("kcal", round(total_kcal, 1)),
            "protein_g": totals.get("protein_g"),
            "carbs_g": totals.get("carbs_g"),
            "fat_g": totals.get("fat_g"),
        },
        "totals_json": meal.totals_json,
    }


def _parse_eaten_at(raw: str, meal: Meal, user: User) -> datetime:
    """Parse a correction date/datetime into a tz-aware UTC instant.

    A bare ``YYYY-MM-DD`` is resolved at the CURRENT LOCAL time-of-day in the
    user's timezone (clamped to the past, never in the future) — never at
    midnight. Midnight UTC lands at ~02:00 local, BEFORE the 04:00 day
    boundary, so a bare-date move silently books the meal on the PREVIOUS
    nutrition day. A full ISO datetime is honoured as given (naive -> the
    user's timezone).
    """
    tz = resolve_tz(user.timezone)
    s = str(raw).strip()
    try:
        from datetime import date as _date
        d = _date.fromisoformat(s)
        now_local = local_now(user.timezone)
        local_dt = datetime(d.year, d.month, d.day, now_local.hour,
                            now_local.minute, now_local.second, tzinfo=tz)
        # A bare date equal to TODAY must not be stamped in the future: clamp
        # to the current local instant. A FUTURE bare date is left as-is so the
        # caller's 7-day guard can still reject it (clamping it to now would
        # silently swallow the guard).
        if d == now_local.date() and local_dt > now_local:
            local_dt = now_local
        return local_dt.astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="eaten_at must be YYYY-MM-DD or an ISO datetime")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def _resolve_user(
    db: Session = Depends(get_db),
    x_telegram_id: str | None = Header(default=None),
    x_service_token: str | None = Header(default=None),
) -> User:
    if not x_service_token or not validate_service_token(x_service_token):
        raise HTTPException(status_code=401, detail="Invalid service token")
    if not x_telegram_id:
        raise HTTPException(status_code=401, detail="Missing X-Telegram-Id")
    user = get_user_by_telegram_id(db, x_telegram_id)
    if not user or user.status != "active":
        raise HTTPException(status_code=404, detail="User not paired")
    return user


def _tool_audit(db: Session, user: User, action: str, resource_type: str | None = None, resource_id: str | None = None, metadata: dict | None = None):
    audit(db, "openclaw", str(user.id), user.id, action, resource_type, resource_id, metadata)


class ToolResponse(BaseModel):
    ok: bool
    data: dict | list | None = None
    message: str | None = None


@router.get("/get_my_today_summary", response_model=ToolResponse)
def tool_today_summary(user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    from drhiro_api.routers.dashboard import dashboard_today
    summary = dashboard_today(user, db)
    _tool_audit(db, user, "tools.today_summary")
    db.commit()
    return ToolResponse(ok=True, data=summary)


class TrendRequest(BaseModel):
    metric: str
    period: str = "30d"


@router.post("/get_my_metric_trend", response_model=ToolResponse)
def tool_metric_trend(req: TrendRequest, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    measurements = _measurements_since(db, user.id, 90)
    if req.metric == "weight":
        data = weight_trend(measurements, 30)
    elif req.metric == "steps":
        data = {"raw_count": sum(1 for m in measurements if m["metric_type"] == MetricType.STEPS)}
    elif req.metric == "blood_pressure":
        data = {"raw_count": sum(1 for m in measurements if m["metric_type"] == MetricType.BLOOD_PRESSURE)}
    else:
        data = {"raw_count": sum(1 for m in measurements if m["metric_type"] == req.metric)}
    _tool_audit(db, user, "tools.metric_trend", "measurement", req.metric)
    db.commit()
    return ToolResponse(ok=True, data=data)


class ManualWeightTool(BaseModel):
    value: float = Field(ge=20, le=400)
    unit: str = "kg"
    measured_at: datetime | None = None


@router.post("/create_manual_weight", response_model=ToolResponse)
def tool_create_weight(req: ManualWeightTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    m = Measurement(
        user_id=user.id,
        metric_type=MetricType.WEIGHT,
        start_at=req.measured_at or datetime.now(timezone.utc),
        end_at=req.measured_at or datetime.now(timezone.utc),
        value_json={"weight_kg": req.value},
        unit="kg",
        source_provider="manual",
        source_record_id=f"tool-{uuid.uuid4().hex}",
        recording_method="manual",
        confidence=1.0,
    )
    db.add(m)
    db.flush()
    _tool_audit(db, user, "tools.manual_weight", "measurement", str(m.id))
    db.commit()
    return ToolResponse(ok=True, message=f"Weight {req.value} kg recorded.", data={"id": str(m.id)})


class ManualBpTool(BaseModel):
    systolic: int = Field(ge=40, le=300)
    diastolic: int = Field(ge=20, le=200)
    pulse: int | None = None
    measured_at: datetime | None = None
    context: str | None = None


@router.post("/create_manual_bp", response_model=ToolResponse)
def tool_create_bp(req: ManualBpTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    m = Measurement(
        user_id=user.id,
        metric_type=MetricType.BLOOD_PRESSURE,
        start_at=req.measured_at or datetime.now(timezone.utc),
        end_at=req.measured_at or datetime.now(timezone.utc),
        value_json={
            "systolic_mmhg": req.systolic,
            "diastolic_mmhg": req.diastolic,
            "pulse_bpm": req.pulse,
            "body_position": req.context,
        },
        unit="mmHg",
        source_provider="manual",
        source_record_id=f"tool-{uuid.uuid4().hex}",
        recording_method="manual",
        confidence=1.0,
    )
    db.add(m)
    db.flush()
    _tool_audit(db, user, "tools.manual_bp", "measurement", str(m.id))
    db.commit()
    return ToolResponse(ok=True, message=f"BP {req.systolic}/{req.diastolic} recorded.", data={"id": str(m.id)})


class MealFromTextTool(BaseModel):
    text: str
    eaten_at: datetime | None = None
    meal_type: str | None = None
    # Telegram identity of the message that produced this line. Present -> the
    # write is idempotent and editable; absent (manual UI) -> plain insert.
    telegram_chat_id: str | None = None
    telegram_message_id: str | None = None
    # >0 marks an edit of an existing message.
    edit_date: int | None = None


@router.post("/create_meal_from_text", response_model=ToolResponse)
def tool_meal_from_text(req: MealFromTextTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Log ONE Telegram line into all three ledgers in ONE transaction.

    The meal-only writer could not satisfy the liquid or activity ledgers, so the
    line is parsed into intents (meal / liquid / activity) and committed together:
    a partial write is impossible, and a failure rolls the whole line back.
    """
    # Phantom-meal guard: a meal write MUST carry the originating Telegram
    # message identity. Without it we cannot tie the write to a real user
    # message, nor reject a duplicate via idempotency. This is the hard
    # backstop that makes an identity-less (phantom) meal write impossible:
    # missing either id -> HTTP 400, nothing is written.
    if not req.telegram_chat_id or not req.telegram_message_id:
        raise HTTPException(
            status_code=400,
            detail="message identity required: pass telegram_chat_id and telegram_message_id",
        )
    from drhiro_api.services.log_intents import commit_intents, parse_log_text
    from drhiro_api.services.date_parser import AmbiguousDate, resolve_when, strip_when

    # Resolve any relative date ("yesterday", "on Monday") in the USER's
    # timezone, server-side. The LLM never does date arithmetic: a wrong date
    # silently corrupts the record, so only its words are forwarded here.
    food_text = req.text
    resolved_when = None
    resolved_meal_type = None
    try:
        resolved_when, phrase, resolved_meal_type = resolve_when(
            req.text,
            getattr(user, "timezone", None) or "Europe/Zagreb",
            meal_type=req.meal_type,
        )
        if phrase:
            stripped = strip_when(req.text)
            if stripped:
                food_text = stripped
    except AmbiguousDate as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        pass

    # A named slot wins; otherwise SNACK. None is never a stored slot.
    slot = req.meal_type or resolved_meal_type or "snack"

    # ---- ledger guard (0.1.26): never file a workout/etc. as a meal -------
    # Fail-open by construction: an unconfigured or unreachable Jev leaves
    # logging exactly as it was. A confident disagreement returns a structured
    # "which did you mean?" payload and writes NOTHING; a low-confidence target
    # proceeds flagged for review; no confident ledger at all asks the user.
    guard = None
    ledger_review = False
    try:
        from drhiro_api.services.ledger_classifier import guard_write
        guard = guard_write(food_text, "consumption")
    except Exception:
        guard = None
    if guard is not None:
        if guard.action == "block":
            return JSONResponse(status_code=409, content={
                "ok": False,
                "message": guard.message,
                "data": {"error": "ledger_disagreement", **guard.as_dict()},
            })
        if guard.action == "no_ledger":
            return JSONResponse(status_code=409, content={
                "ok": False,
                "message": guard.message,
                "data": {"error": "no_ledger", **guard.as_dict()},
            })
        ledger_review = guard.action == "review"

    try:
        parsed = parse_log_text(db, food_text, meal_slot=slot)
    except Exception:
        parsed = None
    if parsed is None or not parsed.intents:
        # Router: the deterministic parser found nothing. If the classifier
        # names a ledger, tell the bot which one and its writer instead of
        # failing or letting the model guess. Never writes a measurement here.
        verdict = guard.verdict if guard is not None else None
        if verdict is not None and verdict.label:
            from drhiro_api.services.ledger_classifier import LEDGER_TOOLS
            return JSONResponse(status_code=409, content={
                "ok": False,
                "message": (
                    f"This looks like {verdict.label}; log it via the "
                    f"{LEDGER_TOOLS.get(verdict.label, verdict.label)} tool."
                ),
                "data": {
                    "error": "routed_elsewhere",
                    "routed_to": verdict.label,
                    "routed_tool": LEDGER_TOOLS.get(verdict.label),
                    "verdict": verdict.as_dict(),
                },
            })
        raise HTTPException(status_code=400, detail="nothing_loggable_in_text")

    # No LLM gate here — agent verifies quality. Commit with status=needs_review
    # so the agent can inspect parsed items before confirming.

    # The raw text is preserved; the parser derives the structure so nutrition
    # attaches. Identity comes from the body when the caller supplies it (the
    # manual UI does not). On a schema without consumption_operations the
    # identity is ignored and this is a plain insert -- see schema_capabilities.
    result = commit_intents(
        db, user, parsed,
        # Fallback stamp is LOCAL now, not UTC now: between 00:00 and ~02:00
        # local the UTC clock is still on the previous calendar day.
        eaten_at=req.eaten_at or resolved_when or local_now(user.timezone).astimezone(timezone.utc),
        telegram_chat_id=req.telegram_chat_id,
        telegram_message_id=req.telegram_message_id,
        raw_text=req.text,
    )
    _tool_audit(db, user, "tools.log_text", "meal", result.get("meal_id"))
    db.commit()

    # Enrich the response so the bot can confirm the log with the day, meal
    # type, LOCAL time, items (with kcal) and the total — the feedback loop.
    meal_intents = [i for i in parsed.intents if i.kind == "meal"]
    meal = None
    if result.get("meal_id"):
        meal = (
            db.query(Meal)
            .options(selectinload(Meal.items))
            .filter(Meal.id == uuid.UUID(str(result["meal_id"])), Meal.user_id == user.id)
            .first()
        )

    # A suppressed repeat must be visible to the agent: reporting success for a
    # write that never happened is silent data loss, which is worse than the
    # duplicate the guard prevents.
    suppressed = bool(result.get("duplicate_suppressed"))
    data: dict = {
        "meal_id": result.get("meal_id"),
        "meal_type": slot,
        "status": "needs_review",
        "duplicate_suppressed": suppressed,
        "ledger_review": ledger_review,
    }
    if meal is not None:
        conf = _meal_confirmation(db, user, meal)
        # Keep the legacy per-item keys (item_id/display_name/grams/original_text)
        # and add the enriched ones (kcal, unit, quantity, resolved_food, ...).
        items = []
        for idx, entry in enumerate(conf["items"]):
            entry = dict(entry)
            if idx < len(meal_intents):
                entry["original_text"] = meal_intents[idx].raw
            items.append(entry)
        data["items"] = items
        for key in ("eaten_at", "timezone", "nutrition_date", "weekday",
                    "local_date", "local_time", "utc_offset", "day_start_hour",
                    "total_kcal", "totals"):
            data[key] = conf[key]
    else:
        # Liquid-only line (no meal row): keep the historical item shape, add kcal.
        data["items"] = [
            {
                "item_id": None,
                "display_name": item.display_name,
                "grams": item.grams,
                "volume_ml": item.volume_ml,
                "kcal": item.kcal,
                "kind": item.kind,
                "original_text": item.raw,
            }
            for item in parsed.intents
        ]
        _now_local = local_now(user.timezone)
        data["timezone"] = user.timezone
        data["nutrition_date"] = nutrition_date(datetime.now(timezone.utc), user.timezone).isoformat()
        data["local_date"] = _now_local.date().isoformat()
        data["local_time"] = _now_local.strftime("%H:%M")
        data["utc_offset"] = utc_offset_str(_now_local)
        data["day_start_hour"] = day_start_hour()

    return ToolResponse(
        ok=True,
        message=(
            "This message was already logged by your earlier call in this "
            "turn — the duplicate write was suppressed, so nothing was "
            "written twice. Do NOT tell the user it was 'already logged'; "
            "confirm the meal from your first response."
            if suppressed
            else "Parsed. Status: needs_review. Verify and confirm."
        ),
        data=data,
    )


class MealItemPatchTool(BaseModel):
    display_name: str | None = None
    quantity: float | None = None
    unit: str | None = None
    grams: float | None = None
    # Explicit food re-pointing: same identifier shape search_food returns
    # ("external_id"); "food_catalog_item_id" accepted as alias so a search
    # result can be piped straight in. Forwarded to meals.MealItemPatch,
    # which re-resolves nutrition FROM that food.
    food_catalog_item_id: str | None = None
    external_id: str | None = None
    # Custom per-100g nutrition override — forwarded to MealItemPatch so the
    # agent can store exact values the user provides (e.g. "100g bread = 281
    # kcal") that no catalog food matches. Scaled by grams/100 on the server.
    kcal_per_100g: float | None = None
    protein_per_100g: float | None = None
    carbs_per_100g: float | None = None
    fat_per_100g: float | None = None
    fiber_per_100g: float | None = None
    sugar_per_100g: float | None = None
    salt_g_per_100g: float | None = None


class UpdateMealItemTool(BaseModel):
    meal_id: str
    item_id: str
    patch: MealItemPatchTool


@router.post("/update_meal_item", response_model=ToolResponse)
def tool_update_meal_item(req: UpdateMealItemTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    from drhiro_api.routers.meals import MealItemPatch, patch_meal_item
    meal = db.query(Meal).filter(Meal.id == uuid.UUID(req.meal_id), Meal.user_id == user.id).first()
    if not meal:
        raise HTTPException(status_code=404, detail="Meal not found")
    patch = MealItemPatch(**req.patch.model_dump(exclude_none=True))
    out = patch_meal_item(req.meal_id, req.item_id, patch, user, db)
    _tool_audit(db, user, "tools.meal_item_update", "meal_item", req.item_id,
                metadata={"patch": req.patch.model_dump(exclude_none=True)})
    db.commit()
    # Enrich with per-item post-write state
    item = db.query(MealItem).filter(MealItem.id == uuid.UUID(req.item_id)).first()
    item_state = None
    if item is not None:
        n = item.nutrients_json if isinstance(item.nutrients_json, dict) else {}
        kcal = n.get("kcal")
        if kcal is None:
            kcal = _local_preview_kcal(db, item)
        item_state = {
            "item_id": str(item.id),
            "display_name": item.display_name,
            "resolved_food": n.get("resolved_food"),
            "kcal": round(float(kcal), 1) if kcal is not None else None,
            "unresolved": bool(
                item.source == "unresolved"
                or (isinstance(item.nutrients_json, dict) and item.nutrients_json.get("unresolved"))
                or item.nutrients_json is None
            ),
        }
    return ToolResponse(ok=True, data={"meal_id": str(out.id), "status": out.status, "item": item_state})


class ConfirmMealTool(BaseModel):
    meal_id: str


@router.post("/confirm_meal", response_model=ToolResponse)
def tool_confirm_meal(req: ConfirmMealTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    from drhiro_api.routers.meals import confirm_meal
    meal = (
        db.query(Meal)
        .options(selectinload(Meal.items))
        .filter(Meal.id == uuid.UUID(req.meal_id), Meal.user_id == user.id)
        .first()
    )
    if not meal:
        raise HTTPException(status_code=404, detail="Meal not found")
    out = confirm_meal(req.meal_id, user, db)
    _tool_audit(db, user, "tools.meal_confirm", "meal", req.meal_id)
    db.commit()
    confirmed = (
        db.query(Meal)
        .options(selectinload(Meal.items))
        .filter(Meal.id == uuid.UUID(req.meal_id), Meal.user_id == user.id)
        .first()
    )
    conf = _meal_confirmation(db, user, confirmed)
    return ToolResponse(ok=True, message="Meal confirmed.", data=conf)




class GetPendingMealTool(BaseModel):
    meal_id: str


@router.post("/get_pending_meal", response_model=ToolResponse)
def tool_get_pending_meal(req: GetPendingMealTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Get the parsed items of a meal with status=needs_review for agent verification."""
    meal = (
        db.query(Meal)
        .options(selectinload(Meal.items))
        .filter(Meal.id == uuid.UUID(req.meal_id), Meal.user_id == user.id)
        .first()
    )
    if not meal:
        raise HTTPException(status_code=404, detail="Meal not found")
    # Enriched payload: the day/weekday/local time it was filed under plus each
    # item with kcal and the meal total — everything the bot needs to confirm.
    data = _meal_confirmation(db, user, meal)
    return ToolResponse(ok=True, data=data)


class UpdateMealTool(BaseModel):
    meal_id: str
    eaten_at: str | None = None      # YYYY-MM-DD (keeps local time-of-day) or ISO datetime
    meal_type: str | None = None


@router.post("/update_meal", response_model=ToolResponse)
def tool_update_meal(req: UpdateMealTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Correct an EXISTING meal's day (``eaten_at``) and/or ``meal_type``.

    Closes the feedback loop when the user says "that was Sunday's dinner":
    ``update_meal_item`` edits an item's contents, this moves the whole meal to
    another day/slot. Scoped to the caller's own meal and audited. A date more
    than ~7 days from now (either direction) is rejected — a larger jump is far
    more likely a mis-parse than a real correction.
    """
    meal = (
        db.query(Meal)
        .options(selectinload(Meal.items))
        .filter(Meal.id == uuid.UUID(req.meal_id), Meal.user_id == user.id)
        .first()
    )
    if not meal:
        raise HTTPException(status_code=404, detail="Meal not found")

    before = _meal_confirmation(db, user, meal)
    changed: list[str] = []
    if req.meal_type is not None:
        if not req.meal_type.strip():
            raise HTTPException(status_code=400, detail="meal_type must not be empty")
        meal.meal_type = req.meal_type.strip()
        changed.append("meal_type")
    if req.eaten_at is not None:
        new_dt = _parse_eaten_at(req.eaten_at, meal, user)
        delta_days = abs((new_dt - datetime.now(timezone.utc)).total_seconds()) / 86400.0
        if delta_days > 7:
            raise HTTPException(
                status_code=400,
                detail=(
                    "eaten_at is more than 7 days from now — refusing to move the "
                    "meal that far. Confirm the date with the user."
                ),
            )
        meal.eaten_at = new_dt
        changed.append("eaten_at")
    if not changed:
        raise HTTPException(status_code=400, detail="Provide eaten_at and/or meal_type")

    _tool_audit(db, user, "tools.meal_update", "meal", str(meal.id),
                metadata={"changed": changed,
                          "from": {"eaten_at": before["eaten_at"], "meal_type": before["meal_type"]},
                          "to": {"eaten_at": meal.eaten_at.isoformat(), "meal_type": meal.meal_type}})
    db.commit()
    db.refresh(meal)
    after = _meal_confirmation(db, user, meal)
    return ToolResponse(
        ok=True,
        message=(
            f"Moved meal to {after['weekday']} {after['local_date']} "
            f"({after['meal_type']}, local time {after['local_time']})."
        ),
        data={"changed": changed, "previous": before, **after},
    )


class ListMealsTool(BaseModel):
    date: str | None = None        # YYYY-MM-DD in the user's timezone; default = today
    meal_type: str | None = None   # optional filter: breakfast/lunch/dinner/snack/other


@router.post("/list_meals", response_model=ToolResponse)
def tool_list_meals(req: ListMealsTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """List the user's meals for ONE local day, oldest first (default today).

    This is the READ side of a correction. Without it the agent can only act on
    a ``meal_id`` it already holds, so "fix last night's dinner" degrades into
    asking the user to dig ids out of the dashboard. The day window follows the
    same boundary as ``nutrition_date`` (``day_start_hour`` local), so a 23:44
    dinner and a 01:30 snack on the same nutritional day land together.
    """
    tz = resolve_tz(user.timezone)
    dsh = day_start_hour()
    if req.date and req.date.strip():
        try:
            target = datetime.strptime(req.date.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    else:
        target = nutrition_date(datetime.now(timezone.utc), user.timezone)

    start_local = datetime(target.year, target.month, target.day, dsh, 0, 0)
    start_utc = start_local.replace(tzinfo=tz).astimezone(timezone.utc)
    end_utc = (start_local + timedelta(days=1)).replace(tzinfo=tz).astimezone(timezone.utc)

    meals = (
        db.query(Meal)
        .options(selectinload(Meal.items))
        .filter(
            Meal.user_id == user.id,
            Meal.status != "deleted",
            Meal.eaten_at >= start_utc,
            Meal.eaten_at < end_utc,
        )
        .order_by(Meal.eaten_at)
        .all()
    )
    if req.meal_type and req.meal_type.strip():
        want = req.meal_type.strip().lower()
        meals = [m for m in meals if (m.meal_type or "").lower() == want]

    entries = [_meal_confirmation(db, user, m) for m in meals]
    _tool_audit(db, user, "tools.list_meals", metadata={"date": target.isoformat(),
                                                       "meal_type": req.meal_type,
                                                       "count": len(entries)})
    db.commit()
    return ToolResponse(
        ok=True,
        message=f"{len(entries)} meal(s) on {WEEKDAY_NAMES[target.weekday()]} {target.isoformat()}.",
        data={
            "date": target.isoformat(),
            "weekday": WEEKDAY_NAMES[target.weekday()],
            "timezone": user.timezone,
            "day_start_hour": dsh,
            "count": len(entries),
            "meals": entries,
            "total_kcal": round(sum(e.get("total_kcal") or 0.0 for e in entries), 1),
        },
    )


class SetMyTimezoneTool(BaseModel):
    timezone: str


@router.post("/set_my_timezone", response_model=ToolResponse)
def tool_set_my_timezone(req: SetMyTimezoneTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Set the user's timezone from an IANA name or a UTC offset.

    The user travels, so the clock must follow the DEVICE. Telegram cannot
    supply a zone, so the bot calls this when the user says where they are
    ("I'm in Tokyo now" -> ``Asia/Tokyo``; "UTC+9" / "+09:00" also accepted).
    Every day boundary, meal date and reminder then resolves in that zone.
    """
    try:
        tzname = parse_timezone(req.timezone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid timezone: {exc}")
    previous = user.timezone
    user.timezone = tzname
    _tool_audit(db, user, "tools.set_timezone", "user", str(user.id),
                metadata={"from": previous, "to": tzname})
    db.commit()
    db.refresh(user)
    now_local = local_now(tzname)
    return ToolResponse(
        ok=True,
        message=(
            f"Timezone set to {tzname}. Your local time is now "
            f"{now_local.strftime('%Y-%m-%d %H:%M')} ({utc_offset_str(now_local)})."
        ),
        data={
            "timezone": tzname,
            "previous_timezone": previous,
            "local_date": now_local.date().isoformat(),
            "local_time": now_local.strftime("%H:%M:%S"),
            "utc_offset": utc_offset_str(now_local),
            "nutrition_date": nutrition_date(datetime.now(timezone.utc), tzname).isoformat(),
            "day_start_hour": day_start_hour(),
        },
    )


class LogActivityTool(BaseModel):
    title: str
    calories_burned: float = Field(ge=0)
    activity_date: str | None = None  # ISO date; defaults to today (user tz)
    description: str | None = None


def _activity_date_or_today(raw: str | None, user: User):
    """Parse a tool-supplied ISO date, defaulting to the user's nutrition day."""
    from datetime import date as _date
    if raw is None or str(raw).strip() == "":
        return nutrition_date(datetime.now(timezone.utc), resolve_tz(user.timezone))
    try:
        return _date.fromisoformat(str(raw).strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")


def _activity_day_kcal(db: Session, user: User, d) -> float:
    """Sum of the user's LOGGED activity kcal for one nutrition date.

    Mirrors dashboard_today's ``activity_kcal_today``: only the user-logged
    ``activities`` table, soft-deleted rows excluded when the schema carries the
    column. Device exercise sessions are deliberately NOT included — this is the
    number the activity ledger owns, and it never touches the meal/food totals.
    """
    q = db.query(Activity).filter(Activity.user_id == user.id,
                                  Activity.activity_date == d)
    try:
        from drhiro_api.routers.dashboard import _ledger_soft_delete_available
        if _ledger_soft_delete_available(db):
            q = q.filter(text("activities.deleted_at IS NULL"))
    except Exception:
        pass
    return round(sum(float(a.calories_burned or 0) for a in q.all()), 1)


@router.post("/log_activity", response_model=ToolResponse)
def tool_log_activity(req: LogActivityTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Log a workout / physical activity the user DID, with calories BURNED.

    This is the ONLY correct way to record activity energy. An activity is NEVER
    a meal: filing it through the meal path adds BURNED calories to the day's
    EATEN total and corrupts the food ledger (a real incident logged 850 kcal of
    gardening as food and lost two real logs). BURNED energy lives in the
    ``activities`` table, never in ``meals``.
    """
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title must not be empty")
    d = _activity_date_or_today(req.activity_date, user)
    a = Activity(user_id=user.id, activity_date=d, title=title,
                 description=req.description, calories_burned=req.calories_burned)
    db.add(a)
    db.flush()
    _tool_audit(db, user, "tools.log_activity", "activity", str(a.id),
                metadata={"title": a.title, "calories_burned": a.calories_burned,
                          "date": d.isoformat()})
    db.commit()
    db.refresh(a)
    day_total = _activity_day_kcal(db, user, d)
    return ToolResponse(
        ok=True,
        message=(f"Logged activity '{a.title}' ({a.calories_burned:g} kcal burned) "
                 f"on {d.isoformat()}. Day total activity: {day_total:g} kcal burned."),
        data={"id": str(a.id), "date": d.isoformat(), "title": a.title,
              "description": a.description, "calories_burned": a.calories_burned,
              "activity_kcal_today": day_total},
    )


class ListActivitiesTool(BaseModel):
    date: str | None = None  # ISO date; defaults to today (user tz)


@router.post("/list_activities", response_model=ToolResponse)
def tool_list_activities(req: ListActivitiesTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """List the user's LOGGED activities for one day (default: today, user tz).

    Returns each activity (id, date, title, calories_burned) plus the day's
    total burned kcal. Use the returned ``id`` with ``delete_activity``.
    """
    d = _activity_date_or_today(req.date, user)
    q = db.query(Activity).filter(Activity.user_id == user.id,
                                  Activity.activity_date == d)
    try:
        from drhiro_api.routers.dashboard import _ledger_soft_delete_available
        if _ledger_soft_delete_available(db):
            q = q.filter(text("activities.deleted_at IS NULL"))
    except Exception:
        pass
    rows = q.order_by(Activity.created_at).all()
    items = [{"id": str(a.id), "date": a.activity_date.isoformat(), "title": a.title,
              "description": a.description, "calories_burned": a.calories_burned}
             for a in rows]
    total = round(sum(float(a.calories_burned or 0) for a in rows), 1)
    _tool_audit(db, user, "tools.list_activities", "activity", d.isoformat())
    db.commit()
    return ToolResponse(ok=True, data={"date": d.isoformat(), "items": items,
                                       "count": len(items), "total_burned": total})


class DeleteActivityTool(BaseModel):
    activity_id: str


@router.post("/delete_activity", response_model=ToolResponse)
def tool_delete_activity(req: DeleteActivityTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Delete one of the user's LOGGED activities by id (from list_activities)."""
    a = (db.query(Activity)
         .filter(Activity.id == _maybe_uuid(req.activity_id), Activity.user_id == user.id)
         .first())
    if not a:
        raise HTTPException(status_code=404, detail="Activity not found")
    d = a.activity_date
    info = {"id": str(a.id), "date": d.isoformat(), "title": a.title,
            "calories_burned": a.calories_burned}
    db.delete(a)
    _tool_audit(db, user, "tools.delete_activity", "activity", str(a.id), metadata=info)
    db.commit()
    day_total = _activity_day_kcal(db, user, d)
    return ToolResponse(
        ok=True,
        message=f"Deleted activity '{info['title']}' ({info['calories_burned']:g} kcal) on {info['date']}.",
        data={**info, "deleted": True, "activity_kcal_today": day_total},
    )


class AddMedicalRecordTool(BaseModel):
    title: str | None = None
    content: str
    recorded_at: datetime | None = None
    encounter_id: str | None = None  # optional: group under a medical encounter


@router.post("/add_medical_record", response_model=ToolResponse)
def tool_add_medical_record(req: AddMedicalRecordTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Store the user's transcribed medical document (lab report, prescription, letter).

    ``content`` is the faithful transcription the agent read from the photo —
    original language, exact numbers/units/dates. The agent confirms the
    transcription with the user BEFORE calling this. A naive ``recorded_at``
    is resolved in the user's timezone.
    """
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content must not be empty — store the document's transcription, not a summary")
    encounter = None
    if req.encounter_id:
        encounter = (
            db.query(MedicalEncounter)
            .filter(MedicalEncounter.id == _maybe_uuid(req.encounter_id), MedicalEncounter.user_id == user.id)
            .first()
        )
        if not encounter:
            raise HTTPException(status_code=404, detail="Encounter not found (encounter_id must come from list_medical_encounters)")
    recorded_at = req.recorded_at
    if recorded_at is not None and recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=resolve_tz(user.timezone))
    # Surgery-day rule: a record under a surgery encounter is dated the
    # surgery day itself, regardless of any off-by-one supplied date.
    if encounter is not None and _is_surgery_encounter(encounter):
        recorded_at = _surgery_recorded_at(encounter, user)
    rec = MedicalRecord(
        user_id=user.id,
        title=((req.title or "").strip() or None),
        content=content,
        source_type="telegram_photo",
        recorded_at=recorded_at or datetime.now(timezone.utc),
        encounter_id=encounter.id if encounter else None,
    )
    db.add(rec)
    db.flush()
    _tool_audit(db, user, "tools.add_medical_record", "medical_record", str(rec.id))
    db.commit()
    return ToolResponse(
        ok=True,
        message="Medical record stored.",
        data=_medical_record_out(rec),
    )


class ListMedicalRecordsTool(BaseModel):
    query: str | None = None  # substring search over title + content
    limit: int = Field(default=20, ge=1, le=50)


@router.post("/list_medical_records", response_model=ToolResponse)
def tool_list_medical_records(req: ListMedicalRecordsTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """List the user's stored medical records, newest first, newest ``limit``.

    Pass ``query`` to search a fragment across title AND content (e.g. a lab
    name, a doctor, a medicine). Use the returned ``id`` with
    ``delete_medical_record``.
    """
    q = db.query(MedicalRecord).filter(MedicalRecord.user_id == user.id)
    if req.query and req.query.strip():
        like = f"%{req.query.strip()}%"
        q = q.filter(or_(MedicalRecord.title.ilike(like), MedicalRecord.content.ilike(like)))
    rows = q.order_by(MedicalRecord.recorded_at.desc(), MedicalRecord.created_at.desc()).limit(req.limit).all()
    _tool_audit(db, user, "tools.list_medical_records", metadata={"query": req.query, "count": len(rows)})
    db.commit()
    return ToolResponse(
        ok=True,
        message=f"{len(rows)} medical record(s) found." if rows else "No medical records found.",
        data={
            "count": len(rows),
            "records": [_medical_record_out(r) for r in rows],
        },
    )


class DeleteMedicalRecordTool(BaseModel):
    record_id: str


@router.post("/delete_medical_record", response_model=ToolResponse)
def tool_delete_medical_record(req: DeleteMedicalRecordTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Delete one of the user's stored medical records by id (from list_medical_records)."""
    rec = (
        db.query(MedicalRecord)
        .filter(MedicalRecord.id == _maybe_uuid(req.record_id), MedicalRecord.user_id == user.id)
        .first()
    )
    if not rec:
        raise HTTPException(status_code=404, detail="Medical record not found")
    info = _medical_record_out(rec)
    db.delete(rec)
    _tool_audit(db, user, "tools.delete_medical_record", "medical_record", str(info["id"]),
                metadata={"title": info["title"]})
    db.commit()
    return ToolResponse(
        ok=True,
        message="Medical record deleted.",
        data={**info, "deleted": True},
    )


def _medical_record_out(rec: MedicalRecord) -> dict:
    return {
        "id": str(rec.id),
        "title": rec.title,
        "content": rec.content,
        "source_type": rec.source_type,
        "recorded_at": rec.recorded_at.isoformat() if rec.recorded_at else None,
        "encounter_id": str(rec.encounter_id) if rec.encounter_id else None,
        "source_image_path": rec.source_image_path,
        "source_image_paths": rec.source_image_paths_json or ([rec.source_image_path] if rec.source_image_path else []),
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
    }


# ---------------------------------------------------------------------------
# Medical encounters (group records of one visit / lab draw / prescription)
# ---------------------------------------------------------------------------


def _encounter_out(e: MedicalEncounter, record_count: int | None = None) -> dict:
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
    return out


class AddMedicalEncounterTool(BaseModel):
    encounter_date: str  # YYYY-MM-DD
    facility: str | None = None
    title: str | None = None
    notes: str | None = None


@router.post("/add_medical_encounter", response_model=ToolResponse)
def tool_add_medical_encounter(req: AddMedicalEncounterTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Create a clinical encounter that groups one or more medical records.

    ``encounter_date`` is the date of the visit/lab draw (YYYY-MM-DD).
    Default grouping proposal: facility + date taken from the documents —
    confirm with the user before storing.
    """
    try:
        d = date.fromisoformat((req.encounter_date or "").strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="encounter_date must be YYYY-MM-DD")
    e = MedicalEncounter(
        user_id=user.id,
        encounter_date=d,
        facility=((req.facility or "").strip() or None),
        title=((req.title or "").strip() or None),
        notes=((req.notes or "").strip() or None),
    )
    db.add(e)
    db.flush()
    _tool_audit(db, user, "tools.add_medical_encounter", "medical_encounter", str(e.id))
    db.commit()
    return ToolResponse(
        ok=True,
        message=f"Medical encounter stored ({d.isoformat()}). Add records with add_medical_record + encounter_id.",
        data=_encounter_out(e, 0),
    )


class ListMedicalEncountersTool(BaseModel):
    limit: int = Field(default=20, ge=1, le=50)


@router.post("/list_medical_encounters", response_model=ToolResponse)
def tool_list_medical_encounters(req: ListMedicalEncountersTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """List the user's medical encounters, newest first, with record counts.

    Use the returned ``id`` with ``get_medical_encounter`` (details + records)
    or ``delete_medical_encounter``.
    """
    from sqlalchemy import func

    rows = (
        db.query(MedicalEncounter, func.count(MedicalRecord.id))
        .outerjoin(MedicalRecord, MedicalRecord.encounter_id == MedicalEncounter.id)
        .filter(MedicalEncounter.user_id == user.id)
        .group_by(MedicalEncounter.id)
        .order_by(MedicalEncounter.encounter_date.desc(), MedicalEncounter.created_at.desc())
        .limit(req.limit)
        .all()
    )
    items = [_encounter_out(e, int(cnt or 0)) for e, cnt in rows]
    _tool_audit(db, user, "tools.list_medical_encounters", metadata={"count": len(items)})
    db.commit()
    return ToolResponse(
        ok=True,
        message=f"{len(items)} encounter(s) found." if items else "No medical encounters found.",
        data={"count": len(items), "encounters": items},
    )


class GetMedicalEncounterTool(BaseModel):
    encounter_id: str


@router.post("/get_medical_encounter", response_model=ToolResponse)
def tool_get_medical_encounter(req: GetMedicalEncounterTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """One encounter plus the records stored inside it (id from list_medical_encounters)."""
    e = (
        db.query(MedicalEncounter)
        .filter(MedicalEncounter.id == _maybe_uuid(req.encounter_id), MedicalEncounter.user_id == user.id)
        .first()
    )
    if not e:
        raise HTTPException(status_code=404, detail="Encounter not found")
    records = (
        db.query(MedicalRecord)
        .filter(MedicalRecord.encounter_id == e.id)
        .order_by(MedicalRecord.recorded_at.desc(), MedicalRecord.created_at.desc())
        .all()
    )
    _tool_audit(db, user, "tools.get_medical_encounter", "medical_encounter", str(e.id))
    db.commit()
    return ToolResponse(
        ok=True,
        data={"encounter": _encounter_out(e, len(records)), "records": [_medical_record_out(r) for r in records]},
    )


class DeleteMedicalEncounterTool(BaseModel):
    encounter_id: str


@router.post("/delete_medical_encounter", response_model=ToolResponse)
def tool_delete_medical_encounter(req: DeleteMedicalEncounterTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Delete an encounter AND every medical record stored inside it.

    Confirm with the user before calling — this removes all the documents
    grouped under the encounter. Refuses unknown ids.
    """
    e = (
        db.query(MedicalEncounter)
        .filter(MedicalEncounter.id == _maybe_uuid(req.encounter_id), MedicalEncounter.user_id == user.id)
        .first()
    )
    if not e:
        raise HTTPException(status_code=404, detail="Encounter not found")
    records = db.query(MedicalRecord).filter(MedicalRecord.encounter_id == e.id).all()
    info = _encounter_out(e, len(records))
    for r in records:
        db.delete(r)
    db.delete(e)
    _tool_audit(db, user, "tools.delete_medical_encounter", "medical_encounter", str(e.id),
                metadata={"record_count": len(records)})
    db.commit()
    return ToolResponse(
        ok=True,
        message=f"Deleted encounter and {len(records)} record(s).",
        data={**info, "deleted": True, "deleted_record_count": len(records)},
    )


class UploadMedicalScanTool(BaseModel):
    record_id: str | None = None  # optional: bind scan to an existing record


@router.post("/upload_medical_scan", response_model=ToolResponse)
async def tool_upload_medical_scan(
    file: UploadFile = File(...),
    record_id: str | None = Form(default=None),
    user: User = Depends(_resolve_user),
    db: Session = Depends(get_db),
):
    """Store a document scan image (multipart) and optionally bind it to a
    medical record. Used by the MCP bridge, which reads the inbound Telegram
    photo from the gateway's media directory server-side and relays the bytes —
    the model never handles binary itself."""
    from drhiro_api.routers.medical import store_scan

    data = await file.read()
    rel = store_scan(user, data, file.content_type)
    rid = None
    if record_id:
        rec = (
            db.query(MedicalRecord)
            .filter(MedicalRecord.id == _maybe_uuid(record_id), MedicalRecord.user_id == user.id)
            .first()
        )
        if not rec:
            raise HTTPException(status_code=404, detail="Medical record not found")
        rec.source_image_path = rel
        db.add(rec)
        rid = str(rec.id)
    _tool_audit(db, user, "tools.upload_medical_scan", "medical_record", rid,
                metadata={"path": rel, "bound": bool(rid)})
    db.commit()
    return ToolResponse(
        ok=True,
        message="Scan stored." + (" Bound to record." if rid else ""),
        data={"path": rel, "record_id": rid},
    )


class UpdateMedicalRecordTool(BaseModel):
    record_id: str
    title: str | None = None
    content: str | None = None
    recorded_at: datetime | None = None
    encounter_id: str | None = None  # re-group under another encounter (owner-scoped)
    merge_record_ids: list[str] | None = None  # same-burst placeholder ids to fold in


@router.post("/update_medical_record", response_model=ToolResponse)
def tool_update_medical_record(req: UpdateMedicalRecordTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Enrich an EXISTING medical record in place (title/content/recorded_at and/or
    encounter grouping).

    This is the deterministic counterpart of add_medical_record: a photo batch
    is first staged by ingest_medical_batch (encounter + ONE record with scans
    bound, placeholder content), then the agent replaces the placeholder with
    the faithful transcription via this tool — never by creating a second
    record. At least one field must be provided; other users' records 404.
    """
    rec = (
        db.query(MedicalRecord)
        .filter(MedicalRecord.id == _maybe_uuid(req.record_id), MedicalRecord.user_id == user.id)
        .first()
    )
    if not rec:
        raise HTTPException(status_code=404, detail="Medical record not found")
    changed: list[str] = []
    if req.title is not None:
        rec.title = (req.title.strip() or None)
        changed.append("title")
    if req.content is not None:
        content = req.content.strip()
        if not content:
            raise HTTPException(status_code=400, detail="content must not be empty")
        rec.content = content
        changed.append("content")
    if req.recorded_at is not None:
        ra = req.recorded_at
        if ra.tzinfo is None:
            ra = ra.replace(tzinfo=resolve_tz(user.timezone))
        rec.recorded_at = ra
        changed.append("recorded_at")
    if req.encounter_id is not None:
        encounter = (
            db.query(MedicalEncounter)
            .filter(MedicalEncounter.id == _maybe_uuid(req.encounter_id), MedicalEncounter.user_id == user.id)
            .first()
        )
        if not encounter:
            raise HTTPException(status_code=404, detail="Encounter not found (encounter_id must come from list_medical_encounters)")
        rec.encounter_id = encounter.id
        changed.append("encounter_id")
    if not changed:
        raise HTTPException(status_code=400, detail="Provide at least one of title, content, recorded_at, encounter_id")
    # Surgery-day rule + completion pass (merge same-burst placeholders).
    effective_enc = None
    if rec.encounter_id is not None:
        effective_enc = db.query(MedicalEncounter).filter(MedicalEncounter.id == rec.encounter_id, MedicalEncounter.user_id == user.id).first()
    if effective_enc is not None and _is_surgery_encounter(effective_enc):
        rec.recorded_at = _surgery_recorded_at(effective_enc, user)
        if "recorded_at" not in changed:
            changed.append("recorded_at(surgery-day)")
    # Card 8 title propagation (Kim qa-title-bug.md): Records.tsx renders the
    # ENCOUNTER title as the card headline. When this record is the encounter's
    # only record, keep the encounter headline in sync with the record title
    # (e.g. stale 'Krvna kartica - zapis 2000' → 'Eye exam' on enrichment).
    if req.title is not None and effective_enc is not None:
        count = db.query(MedicalRecord).filter(MedicalRecord.encounter_id == effective_enc.id).count()
        if count == 1 and effective_enc.title != rec.title:
            effective_enc.title = rec.title
            changed.append("encounter_title")
    merge_info = {"merged_record_ids": [], "merged_scan_count": 0, "total_scan_count": len(rec.source_image_paths_json or [])}
    # Run the completion pass when enriching content (transcription) or when
    # the caller explicitly names same-burst placeholders to fold in.
    if req.content is not None or req.merge_record_ids:
        merge_info = _merge_placeholder_siblings(db, user, rec, explicit_ids=req.merge_record_ids)
    _tool_audit(db, user, "tools.update_medical_record", "medical_record", str(rec.id),
                metadata={"changed": changed, **merge_info})
    db.commit()
    db.refresh(rec)
    return ToolResponse(ok=True, message="Medical record updated.", data={**_medical_record_out(rec), **merge_info})


class DedupeRecordScansTool(BaseModel):
    record_id: str
    mode: str = "bytes"
    hamming_threshold: int = 4
    dry_run: bool = False


def _ahash_64(abs_path) -> int | None:
    """64-bit average hash of an image file. None if unreadable (caller keeps it)."""
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(abs_path) as im:
            gray = im.convert("L").resize((8, 8))
            pixels = [int(v) for v in gray.getdata()]
        avg = sum(pixels) / len(pixels)
        h = 0
        for i, p in enumerate(pixels):
            if p > avg:
                h |= 1 << i
        return h
    except Exception:
        return None


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


@router.post("/dedupe_record_scans", response_model=ToolResponse)
def tool_dedupe_record_scans(req: DedupeRecordScansTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Content-dedupe a medical record's bound scans (owner-scoped).

    mode='bytes' (default, legacy): sha256 of bytes, keep first per hash.
    mode='perceptual': 64-bit aHash per scan, greedy keep-first clustering
    with Hamming distance <= hamming_threshold (default 4, range 0-16),
    single-linkage (connected components, so stepwise recompression drift chains together). Keeps the earliest-uploaded path per
    cluster; deletes only files clustered with a kept image. Files whose
    aHash cannot be computed are NEVER deleted (kept, reported in
    ``unhashed``). Missing files are reported in ``missing``. Idempotent.
    dry_run=True computes clusters without deleting files or rewriting
    source_image_paths_json.
    """
    from drhiro_api.routers.medical import resolve_scan_file
    rec = (
        db.query(MedicalRecord)
        .filter(MedicalRecord.id == _maybe_uuid(req.record_id), MedicalRecord.user_id == user.id)
        .first()
    )
    if not rec:
        raise HTTPException(status_code=404, detail="Medical record not found")
    mode = (req.mode or "bytes").lower()
    if mode not in ("bytes", "perceptual"):
        raise HTTPException(status_code=400, detail="mode must be 'bytes' or 'perceptual'")
    threshold = int(req.hamming_threshold)
    if not (0 <= threshold <= 16):
        raise HTTPException(status_code=400, detail="hamming_threshold must be 0..16")
    paths = list(rec.source_image_paths_json or ([rec.source_image_path] if rec.source_image_path else []))
    before = len(paths)
    if mode == "perceptual":
        missing: list[str] = []
        unhashed: list[str] = []
        items = []  # (rel, hash, abs_path) in original order
        for rel in paths:
            if not rel:
                continue
            abs_path = resolve_scan_file(user, rel)
            if abs_path is None:
                missing.append(rel)
                continue
            ah = _ahash_64(abs_path)
            if ah is None:
                # Never delete a file we cannot hash.
                unhashed.append(rel)
                items.append((rel, None, abs_path))
                continue
            items.append((rel, ah, abs_path))
        # Single-linkage clustering (union-find): two scans link if Hamming
        # <= threshold. Chaining handles recompressed variants that drift
        # stepwise from the original; representative-only clustering misses
        # those. Unhashed files are always their own cluster (never deleted).
        parent = list(range(len(items)))
        def _find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        def _union(a, b):
            ra, rb = _find(a), _find(b)
            if ra != rb:
                parent[rb] = ra
        for i in range(len(items)):
            if items[i][1] is None:
                continue
            for j in range(i + 1, len(items)):
                if items[j][1] is None:
                    continue
                if _hamming(items[i][1], items[j][1]) <= threshold:
                    _union(i, j)
        groups: dict[int, list[int]] = {}
        for idx in range(len(items)):
            groups.setdefault(_find(idx), []).append(idx)
        kept: list[str] = []
        removed_paths: list[str] = []
        cluster_summary = []
        for root, members in sorted(groups.items(), key=lambda kv: min(kv[1])):
            members_sorted = sorted(members)
            kept_rel, kept_hash, kept_abs = items[members_sorted[0]]
            kept.append(kept_rel)
            removed_here = []
            max_dist = 0
            for m in members_sorted[1:]:
                rel_m, hash_m, abs_m = items[m]
                removed_here.append(rel_m)
                removed_paths.append(rel_m)
                if hash_m is not None and kept_hash is not None:
                    max_dist = max(max_dist, _hamming(hash_m, kept_hash))
                if not req.dry_run:
                    try:
                        os.unlink(abs_m)
                    except FileNotFoundError:
                        pass
            cluster_summary.append({"kept": kept_rel, "kept_hash": (f"{kept_hash:016x}" if kept_hash is not None else None), "removed": removed_here, "max_distance": max_dist})
        removed = len(removed_paths)
        if not req.dry_run and kept != paths:
            rec.source_image_paths_json = kept
            rec.source_image_path = kept[0] if kept else rec.source_image_path
        _tool_audit(db, user, "tools.dedupe_record_scans", "medical_record", str(rec.id),
                    metadata={"before": before, "after": len(kept), "mode": mode, "clusters": len(cluster_summary), "removed": removed, "missing": missing, "unhashed": unhashed, "dry_run": bool(req.dry_run), "hamming_threshold": threshold})
        db.commit()
        return ToolResponse(ok=True, message=f"Perceptual dedupe record scans: {before} -> {len(kept)} ({len(cluster_summary)} clusters, removed {removed}).",
                            data={"record_id": str(rec.id), "mode": mode, "before": before, "after": len(kept), "clusters": len(cluster_summary), "cluster_summary": cluster_summary, "removed_files": removed, "removed_paths": removed_paths, "kept_paths": kept, "missing": missing, "unhashed": unhashed, "dry_run": bool(req.dry_run), "hamming_threshold": threshold})

    kept: list[str] = []
    seen_hashes: set[str] = set()
    groups = 0
    removed = 0
    missing: list[str] = []
    for rel in paths:
        if not rel:
            continue
        abs_path = resolve_scan_file(user, rel)
        if abs_path is None:
            missing.append(rel)
            continue
        h = hashlib.sha256()
        with open(abs_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        digest = h.hexdigest()
        if digest in seen_hashes:
            groups += 1
            try:
                os.unlink(abs_path)
                removed += 1
            except FileNotFoundError:
                pass
            continue
        seen_hashes.add(digest)
        kept.append(rel)
    if kept != paths:
        rec.source_image_paths_json = kept
        rec.source_image_path = kept[0] if kept else rec.source_image_path
    _tool_audit(db, user, "tools.dedupe_record_scans", "medical_record", str(rec.id),
                metadata={"before": before, "after": len(kept), "groups": groups, "removed": removed, "missing": missing})
    db.commit()
    return ToolResponse(ok=True, message=f"Deduped record scans: {before} -> {len(kept)} (removed {removed}).",
                        data={"record_id": str(rec.id), "before": before, "after": len(kept), "groups": groups, "removed_files": removed, "kept_paths": kept, "missing": missing})

@router.post("/ingest_medical_batch", response_model=ToolResponse)
async def tool_ingest_medical_batch(
    files: list[UploadFile] = File(...),
    encounter_date: str | None = Form(default=None),
    facility: str | None = Form(default=None),
    title: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    caption: str | None = Form(default=None),
    batch_key: str | None = Form(default=None),
    user: User = Depends(_resolve_user),
    db: Session = Depends(get_db),
):
    """Deterministically stage a medical photo batch as ONE encounter + ONE record.

    This is the structural guarantee that a medical photo batch is ALWAYS
    stored, independent of whether the LLM ever calls a storage tool: the
    gateway/bridge calls this BEFORE the LLM turn (or on the first tool call)
    with every photo of the batch. The endpoint stores ALL scans, creates the
    encounter and a single record whose content is a placeholder
    ("transcription pending"); the agent later replaces the placeholder with
    the faithful transcription via update_medical_record. No 'Done' can be
    fabricated because the record provably exists before the model speaks.

    Idempotent: callers pass ``batch_key`` (stable per batch, e.g. a hash of
    the staged media paths); a replay returns the original ids with
    deduplicated=true and stores nothing new.
    """
    from drhiro_api.routers.medical import store_scan

    if not files:
        raise HTTPException(status_code=400, detail="files must contain at least one photo")
    if len(files) > 50:
        raise HTTPException(status_code=400, detail="batch too large (max 50 photos)")

    key = (batch_key or "").strip() or None
    if key:
        existing = (
            db.query(IngestBatch)
            .filter(IngestBatch.user_id == user.id, IngestBatch.batch_id == key)
            .first()
        )
        if existing is not None:
            prior = existing.result_json or {}
            return ToolResponse(
                ok=True,
                message="Batch already staged — returning the original encounter/record.",
                data={**prior, "deduplicated": True},
            )

    # Encounter date defaults to the user's LOCAL day (never the UTC clock).
    if encounter_date and encounter_date.strip():
        try:
            enc_date = date.fromisoformat(encounter_date.strip()[:10])
        except ValueError:
            raise HTTPException(status_code=400, detail="encounter_date must be YYYY-MM-DD")
    else:
        enc_date = nutrition_date(datetime.now(timezone.utc), user.timezone)

    # Store first (all scans), then create encounter + ONE record atomically.
    scan_paths: list[str] = []
    for f in files:
        data = await f.read()
        scan_paths.append(store_scan(user, data, f.content_type))

    encounter = MedicalEncounter(
        user_id=user.id,
        encounter_date=enc_date,
        facility=((facility or "").strip() or None),
        title=((title or "").strip() or None),
        notes=((notes or "").strip() or None),
    )
    db.add(encounter)
    db.flush()

    n = len(scan_paths)
    rec_title = ((title or "").strip() or f"Medical photo batch ({n} photo{'s' if n != 1 else ''})")
    placeholder = (
        f"[Medical photo batch — {n} photo(s) staged automatically; "
        f"transcription pending. Original scans preserved.]"
    )
    if caption and caption.strip():
        placeholder += f"\nCaption: {caption.strip()}"
    rec = MedicalRecord(
        user_id=user.id,
        title=rec_title,
        content=placeholder,
        source_type="telegram_photo_batch",
        # Anchor the staged record to the encounter day (noon, user tz) so
        # dating never drifts to the UTC day boundary.
        recorded_at=datetime(enc_date.year, enc_date.month, enc_date.day, 12, 0, tzinfo=resolve_tz(user.timezone)),
        encounter_id=encounter.id,
        source_image_path=scan_paths[0],
        source_image_paths_json=scan_paths,
    )
    db.add(rec)
    db.flush()

    result = {
        "encounter_id": str(encounter.id),
        "record_id": str(rec.id),
        "photo_count": n,
        "scan_paths": scan_paths,
        "source_image_path": scan_paths[0],
        "encounter_date": enc_date.isoformat(),
        "deduplicated": False,
        "status": "staged_transcription_pending",
    }
    if key:
        db.add(IngestBatch(
            user_id=user.id,
            installation_id="medical_batch_ingest",
            batch_id=key,
            result_json=result,
        ))
    _tool_audit(db, user, "tools.ingest_medical_batch", "medical_encounter", str(encounter.id),
                metadata={"record_id": str(rec.id), "photo_count": n, "batch_key": key})
    db.commit()
    return ToolResponse(
        ok=True,
        message=(
            f"Staged {n} photo(s) as ONE record under a new encounter. "
            f"record_id={rec.id} encounter_id={encounter.id}. "
            "Enrich it with update_medical_record (transcription), then verify "
            "with get_medical_encounter before confirming."
        ),
        data=result,
    )


@router.post("/undo_last_user_action", response_model=ToolResponse)
def tool_undo_last_action(user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Undo the user's last action: delete the most recent measurement or
    activity, or revert the most recent meal to draft."""
    last_measurement = (
        db.query(Measurement)
        .filter(Measurement.user_id == user.id)
        .order_by(Measurement.created_at.desc())
        .first()
    )
    last_meal = (
        db.query(Meal)
        .filter(Meal.user_id == user.id)
        .order_by(Meal.created_at.desc())
        .first()
    )
    last_activity = (
        db.query(Activity)
        .filter(Activity.user_id == user.id)
        .order_by(Activity.created_at.desc())
        .first()
    )
    candidates = []
    if last_measurement:
        candidates.append((last_measurement.created_at, "measurement", last_measurement))
    if last_meal:
        candidates.append((last_meal.created_at, "meal", last_meal))
    if last_activity:
        candidates.append((last_activity.created_at, "activity", last_activity))
    # 0.9.0 (spec §5.7): dose events are always undoable; a medication item
    # only within 10 minutes of creation.
    last_dose = (
        db.query(DoseEvent)
        .filter(DoseEvent.user_id == user.id)
        .order_by(DoseEvent.created_at.desc())
        .first()
    )
    if last_dose:
        candidates.append((last_dose.created_at, "dose", last_dose))
    last_item = (
        db.query(MedicationItem)
        .filter(
            MedicationItem.user_id == user.id,
            MedicationItem.created_at >= datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        .order_by(MedicationItem.created_at.desc())
        .first()
    )
    if last_item:
        candidates.append((last_item.created_at, "medication_item", last_item))
    if not candidates:
        return ToolResponse(ok=False, message="Nothing to undo.")
    _, kind, obj = max(candidates, key=lambda c: c[0])
    if kind in ("measurement", "activity", "dose", "medication_item"):
        db.delete(obj)
        _tool_audit(db, user, "tools.undo", kind, str(obj.id))
        db.commit()
        return ToolResponse(ok=True, message=f"Last {kind.replace('_', ' ')} undone.")
    obj.status = "deleted"
    _tool_audit(db, user, "tools.undo", "meal", str(obj.id))
    db.commit()
    return ToolResponse(ok=True, message="Last meal undone.")


@router.get("/list_my_reminders", response_model=ToolResponse)
def tool_list_reminders(user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    reminders = db.query(Reminder).filter(Reminder.user_id == user.id, Reminder.enabled.is_(True)).all()
    _tool_audit(db, user, "tools.list_reminders")
    db.commit()
    return ToolResponse(
        ok=True,
        data=[{"id": str(r.id), "type": r.type, "schedule_json": r.schedule_json, "next_due_at": r.next_due_at} for r in reminders],
    )


class SnoozeTool(BaseModel):
    occurrence_id: str
    duration_minutes: int = 15


@router.post("/snooze_reminder", response_model=ToolResponse)
def tool_snooze_reminder(req: SnoozeTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    occ = (
        db.query(ReminderOccurrence)
        .join(Reminder)
        .filter(ReminderOccurrence.id == uuid.UUID(req.occurrence_id), Reminder.user_id == user.id)
        .first()
    )
    if not occ:
        raise HTTPException(status_code=404, detail="Occurrence not found")
    occ.status = "snoozed"
    occ.due_at = datetime.now(timezone.utc) + timedelta(minutes=req.duration_minutes)
    _tool_audit(db, user, "tools.snooze_reminder", "reminder_occurrence", req.occurrence_id)
    db.commit()
    return ToolResponse(ok=True, message=f"Snoozed {req.duration_minutes} minutes.")


class CreateReminderTool(BaseModel):
    type: str  # bp, weight, meal, water, sync, activity, bedtime, weekly
    schedule_json: dict  # {"cron": "0 8 * * 1"} or {"days": ["mon"], "time": "08:00"}
    timezone: str = "UTC"
    quiet_hours_json: dict | None = None
    escalation_policy_json: dict | None = None


@router.post("/issue_device_code", response_model=ToolResponse)
def tool_issue_device_code(user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Issue a one-time device code for the current user.

    The user enters this code in the drHiro Android Bridge app to link
    their phone. The code is user-bound, expires in 10 minutes, and the
    link persists after exchange (the bridge stores its tokens and
    auto-refreshes).
    """
    from drhiro_api.routers.auth import _DEVICE_CODES, create_device_code

    code = create_device_code()
    _DEVICE_CODES[code] = {"user_id": str(user.id), "expires": 600}
    _tool_audit(db, user, "tools.issue_device_code", "device")
    db.commit()
    return ToolResponse(
        ok=True,
        message=f"Your device code is: {code}\n\nOpen the drHiro Bridge app, enter this code once, and grant Health Connect permissions. It expires in 10 minutes.",
        data={"device_code": code, "expires_in_seconds": 600},
    )


@router.post("/create_reminder", response_model=ToolResponse)
def tool_create_reminder(req: CreateReminderTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Create a reminder for the current user.

    schedule_json supports:
      - {"cron": "0 8 * * 1"}         (5-field cron: minute hour dom month dow)
      - {"days": ["mon", "wed"], "time": "08:00"}
    The API computes next_due_at in the user's timezone.
    """
    from drhiro_api.routers.reminders import _compute_next_due

    if "cron" not in req.schedule_json and "days" not in req.schedule_json:
        raise HTTPException(status_code=400, detail="schedule_json needs 'cron' or 'days'")
    reminder = Reminder(
        user_id=user.id,
        type=req.type,
        schedule_json=req.schedule_json,
        timezone=req.timezone,
        enabled=True,
        quiet_hours_json=req.quiet_hours_json,
        escalation_policy_json=req.escalation_policy_json,
    )
    db.add(reminder)
    db.flush()
    reminder.next_due_at = _compute_next_due(reminder)
    _tool_audit(db, user, "tools.create_reminder", "reminder", str(reminder.id))
    db.commit()
    return ToolResponse(
        ok=True,
        message=f"Reminder set: {req.type} with schedule {req.schedule_json} (next: {reminder.next_due_at}).",
        data={"reminder_id": str(reminder.id), "next_due_at": reminder.next_due_at},
    )


class SetGoalTool(BaseModel):
    goal_type: str
    target_json: dict
    period: str | None = None


@router.post("/set_user_goal", response_model=ToolResponse)
def tool_set_goal(req: SetGoalTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    goal = Goal(user_id=user.id, goal_type=req.goal_type, target_json=req.target_json, source="user")
    if req.period and req.period.lower().endswith("d"):
        try:
            days = int(req.period[:-1])
            goal.start_date = datetime.now(timezone.utc).date().isoformat()
            goal.end_date = (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()
        except ValueError:
            pass
    db.add(goal)
    db.flush()
    _tool_audit(db, user, "tools.set_goal", "goal", str(goal.id))
    db.commit()
    return ToolResponse(ok=True, message="Goal set.", data={"goal_id": str(goal.id)})


@router.get("/get_my_active_alerts", response_model=ToolResponse)
def tool_active_alerts(user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    alerts = db.query(Alert).filter(Alert.user_id == user.id, Alert.status == "open").order_by(Alert.created_at.desc()).all()
    _tool_audit(db, user, "tools.active_alerts")
    db.commit()
    return ToolResponse(
        ok=True,
        data=[
            {
                "id": str(a.id),
                "rule_code": a.rule_code,
                "severity": a.severity,
                "explanation_template": a.explanation_template,
                "params_json": a.params_json,
                "created_at": a.created_at,
            }
            for a in alerts
        ],
    )


class AcknowledgeAlertTool(BaseModel):
    alert_id: str


@router.post("/acknowledge_alert", response_model=ToolResponse)
def tool_acknowledge_alert(req: AcknowledgeAlertTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    alert = db.query(Alert).filter(Alert.id == uuid.UUID(req.alert_id), Alert.user_id == user.id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.status = "acknowledged"
    alert.acknowledged_at = datetime.now(timezone.utc)
    _tool_audit(db, user, "tools.acknowledge_alert", "alert", req.alert_id)
    db.commit()
    return ToolResponse(ok=True, message="Alert acknowledged.")


@router.post("/issue_web_login_link", response_model=ToolResponse)
def tool_issue_web_login_link(
    user: User = Depends(_resolve_user),
    db: Session = Depends(get_db),
    x_telegram_id: str | None = Header(default=None),
):
    """Mint a one-click dashboard login link for the user's Telegram identity.

    Returns a URL the bot can DM; tapping it exchanges the code for real
    tokens (POST /auth/telegram-link/complete) and lands on the dashboard
    with no OTP entry. Works for already-paired users.
    """
    link_code = mint_web_login_code(x_telegram_id or "")
    web_base = os.getenv(
        "DRHIRO_WEB_BASE",
        "https://vmi3413468.contaboserver.net/drhiro-app",
    ).rstrip("/")
    url = f"{web_base}/auth/link?link={link_code}"
    _tool_audit(db, user, "tools.issue_web_login_link", "user", str(user.id))
    db.commit()
    return ToolResponse(
        ok=True,
        message="Dashboard login link minted.",
        data={"url": url, "link_code": link_code, "expires_in": 1800},
    )


class SearchFoodTool(BaseModel):
    query: str
    limit: int = 5


@router.post("/search_food", response_model=ToolResponse)
def tool_search_food(req: SearchFoodTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Search for foods by name. Returns ranked candidates with nutrition per 100g.

    Local database resolution first; if it is thin/absent, falls back to a live
    USDA FoodData Central lookup (USDA_API_KEY env var) so the agent can verify
    a parser match against real online values.
    """
    import os as _os
    from drhiro_api.food_search import resolve_food, nutrient_map
    from drhiro_api.models import Nutrient
    result = resolve_food(db, req.query, limit=req.limit, user_id=user.id)
    code_by_id = {n.id: n.nutrient_code for n in db.query(Nutrient).all()}
    candidates = []
    for match in result.matches:
        f = match.food
        nmap = nutrient_map(f, code_by_id)
        candidates.append({
            "external_id": str(f.external_id),
            "display_name": f.display_name,
            "kcal_per_100g": nmap.get("energy"),
            "protein_g_per_100g": nmap.get("protein"),
            "carbs_g_per_100g": nmap.get("carbs"),
            "fat_g_per_100g": nmap.get("fat"),
            "fiber_g_per_100g": nmap.get("fiber"),
            "sodium_mg_per_100g": nmap.get("sodium"),
            "match_tier": match.tier,
            "is_generic": bool(f.is_generic),
            "source": "local",
        })
    online = _usda_online_candidates(req.query, req.limit)
    seen = {c["display_name"].lower() for c in candidates}
    # Persist online hits (USDA + DDG) into the local foods table so a later
    # update_meal_item(display_name / external_id) can resolve them instead of
    # falling back to 0 kcal. Best-effort: failure never fails the search.
    for c in online:
        key = c["display_name"].lower()
        try:
            _persist_food(db, c)
            db.commit()
        except Exception:
            db.rollback()
        if key not in seen:
            # brand_owner/data_type are internal provenance for _persist_food;
            # the consumer payload key set must not change.
            candidates.append({k: v for k, v in c.items()
                               if k not in ("brand_owner", "data_type")})
            seen.add(key)
    # DuckDuckGo web fallback only when nothing credible found yet.
    if not candidates and not result.ambiguous:
        ddg = _ddg_online_candidates(req.query, req.limit)
        for c in ddg:
            key = c["display_name"].lower()
            if key not in seen:
                candidates.append(c)
                seen.add(key)
                try:
                    _persist_food(db, c)
                    db.commit()
                except Exception:
                    db.rollback()
    return ToolResponse(ok=True, data={
        "candidates": candidates,
        "ambiguous": result.ambiguous,
        "online_lookup": bool(online),
        "ddg_lookup": any(c.get("source") == "duckduckgo" for c in candidates),
    })


def _usda_online_candidates(query: str, limit: int):
    """Live USDA FoodData Central lookup. Returns list of candidate dicts."""
    import os as _os
    key = _os.environ.get("USDA_API_KEY") or ""
    if not key:
        return []
    try:
        from drhiro_nutrition.usda import USDACatalog
    except Exception:
        return []
    try:
        cat = USDACatalog(api_key=key, timeout=8.0)
        try:
            items = cat.search(query, limit=limit)
        finally:
            cat.close()
    except Exception:
        return []
    out = []
    for it in items:
        out.append({
            "external_id": it.external_id,
            "display_name": it.display_name,
            "kcal_per_100g": it.kcal_per_100g,
            "protein_g_per_100g": it.protein_g_per_100g,
            "carbs_g_per_100g": it.carbs_g_per_100g,
            "fat_g_per_100g": it.fat_g_per_100g,
            "fiber_g_per_100g": it.fiber_g_per_100g,
            "sodium_mg_per_100g": it.sodium_mg_per_100g,
            "source": "usda-online",
            "brand_owner": getattr(it, "brand_owner", None),
            "data_type": getattr(it, "data_type", None),
        })
    return out

def _ddg_online_candidates(query: str, limit: int):
    """DuckDuckGo nutrition fallback via the VPS ddg_http service.

    A bare food name often returns no parseable nutrition, so if the first
    query yields nothing we retry once with 'nutrition per 100g' appended.
    """
    import json as _j
    import re as _re
    import urllib.request as _ur

    def _fetch(q):
        try:
            body = _ur.urlopen(
                _ur.Request(
                    "http://172.20.0.1:8098/lookup",
                    data=_j.dumps({"query": q}).encode(),
                    headers={"Content-Type": "application/json"},
                ),
                timeout=75,
            ).read()
            return _j.loads(body or b"{}")
        except Exception:
            return {}

    data = _fetch(query)
    if not data.get("candidates"):
        data = _fetch(query + " nutrition per 100g")
    out = []
    for c in (data.get("candidates") or [])[:limit]:
        if c.get("kcal_per_100g") is None and c.get("protein_g_per_100g") is None:
            continue
        slug = _re.sub(r"[^a-z0-9]+", "-", (c.get("display_name") or query).lower().strip())[:60]
        out.append({
            "external_id": "ddg:" + slug,
            "display_name": c.get("display_name") or query,
            "kcal_per_100g": c.get("kcal_per_100g"),
            "protein_g_per_100g": c.get("protein_g_per_100g"),
            "carbs_g_per_100g": c.get("carbs_g_per_100g"),
            "fat_g_per_100g": c.get("fat_g_per_100g"),
            "fiber_g_per_100g": None,
            "sodium_mg_per_100g": None,
            "source": "duckduckgo",
        })
    return out


def _persist_food(db: Session, c: dict):
    """Save an online-found food locally (drhiro_private source) if new. Best-effort.

    Handles both USDA-online and DuckDuckGo candidates: the external_id
    namespace is preserved (USDA keeps its real FDC id, DDG keeps 'ddg:<slug>'),
    so a later resolution by external_id finds the row.
    """
    import uuid
    from drhiro_api.models import Food, FoodNutrient, Nutrient, DataSource
    ds = db.query(DataSource).filter(DataSource.source_key == "drhiro_private").first()
    if ds is None:
        return None
    ex = db.query(Food).filter(Food.data_source_id == ds.id, Food.external_id == c["external_id"]).first()
    if ex is not None:
        return ex
    # Store the candidate's ORIGINAL name (spec Task 5). Storing the translation
    # collapsed specific names ("Beef, tenderloin steak, raw") to their generic
    # word ("beef") and minted the 35 duplicate-name groups cleaned up in
    # 0.1.16. The candidate dict carries the name the online source returned.
    _stored_name = (c.get("display_name") or "").strip() or str(c.get("display_name") or "")
    # A candidate that carries a brand is NOT a generic food. When the online
    # source gave no brand information we keep the historical default (True) --
    # the backfill script reconciles the pre-existing rows from FDC directly.
    _brand = c.get("brand_owner")
    _is_generic = not _brand
    # A DIFFERENT FDC id for the same concept must not mint a second row with an
    # identical display_name. Identical names can never be ranked
    # deterministically: whichever row the heap returned first won. Reuse it.
    by_name = db.query(Food).filter(Food.data_source_id == ds.id,
                                    Food.display_name == _stored_name).first()
    if by_name is not None:
        return by_name
    f = Food(id=uuid.uuid4(), data_source_id=ds.id, external_id=c["external_id"],
             display_name=_stored_name, is_generic=_is_generic, is_liquid=False)
    db.add(f)
    db.flush()
    # nutrient codes in this schema are the NAME strings search_food reads back.
    vals = {
        "energy": c.get("kcal_per_100g"),
        "protein": c.get("protein_g_per_100g"),
        "carbs": c.get("carbs_g_per_100g"),
        "fat": c.get("fat_g_per_100g"),
    }
    rows = {n.nutrient_code: n for n in db.query(Nutrient).filter(Nutrient.nutrient_code.in_(list(vals))).all()}
    for code, val in vals.items():
        r = rows.get(code)
        if r is None or val is None:
            continue
        db.add(FoodNutrient(food_id=f.id, nutrient_id=r.id, amount_per_100g=val, amount_per_serving=None))
    db.flush()
    return f


# ──────────────────────────────────────────────────────────────────────────────
# Mi Fitness export import (VPS inbox flow)
# ──────────────────────────────────────────────────────────────────────────────
#
# A Telegram bot cannot download a file larger than 20 MB and a real Mi
# Fitness export is ~23 MB, so the archive is staged by hand in a VPS inbox
# directory and the bot imports it BY NAME.  Two hard rules:
#
#   * the archive password is read from a sibling ``.zip_pw`` file (mode 600)
#     in the inbox.  It is NEVER a tool argument, never travels through
#     Telegram, and is never echoed in a response, log line or audit row.
#   * ``filename`` must be a bare name resolved strictly inside the inbox —
#     any path separator, ``..``, absolute path or symlink escape is rejected.
#
# The import is long (~3 min: the importer streams a ~394 MB CSV).  The
# gateway's MCP request timeout defaults to 60 s (mcp-transport-config:
# DEFAULT_REQUEST_TIMEOUT_MS), so a synchronous tool call WOULD time out.
# The tool therefore starts a BACKGROUND job and returns a job_id at once; the
# bot polls ``get_mifitness_import_status``.  The finished report is also
# persisted into ``ingest_batches`` (batch_id ``mifitness-tool-<job_id>``) so
# the result survives an api restart.

IMPORTS_INCOMING_DIR = os.environ.get("DRHIRO_IMPORTS_INCOMING", "/imports/incoming")
ZIP_PASSWORD_FILENAME = ".zip_pw"

# In-process job registry.  The api runs a SINGLE uvicorn worker (see
# Dockerfile.api CMD), so a module-level dict is the live state; the durable
# copy of a finished report is the ingest_batches row.
_MF_JOBS: dict[str, dict] = {}
_MF_LATEST_JOB: dict[str, str] = {}  # user_id -> most recent job_id
_MF_LOCK = threading.Lock()


def _imports_dir() -> Path:
    return Path(IMPORTS_INCOMING_DIR)


def _safe_archive_path(filename: str) -> Path:
    """Resolve ``filename`` to a real file strictly inside the inbox.

    Rejects anything that is not a bare filename living directly in the
    inbox: path separators, ``..``, absolute paths, hidden/dot files and
    symlinks (which would let a name escape the directory).  Raises
    ``ValueError`` with a bot-readable message.
    """
    name = (filename or "").strip()
    if not name:
        raise ValueError("filename is required")
    if name != os.path.basename(name):
        raise ValueError("filename must be a bare name (no directory part)")
    if "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("filename must not contain a path separator")
    if name in (".", "..") or name.startswith("."):
        raise ValueError("filename must not be '.' or a hidden file")
    base = _imports_dir()
    try:
        base = base.resolve(strict=True)
    except OSError:
        raise ValueError(f"inbox directory {IMPORTS_INCOMING_DIR!r} is not available")
    candidate = base / name
    if candidate.is_symlink():
        raise ValueError("filename must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise ValueError(f"no staged archive named {name!r}")
    if resolved.parent != base:
        raise ValueError("filename escapes the inbox directory")
    if not resolved.is_file():
        raise ValueError(f"{name!r} is not a regular file")
    return resolved


def _read_archive_password() -> str | None:
    """Read the sibling ``.zip_pw``; return None when it is missing/empty.

    The value is only ever passed to the importer service in-process.
    """
    pw_path = _imports_dir() / ZIP_PASSWORD_FILENAME
    try:
        if not pw_path.is_file():
            return None
        secret = pw_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return secret or None


def _mf_summary(report: dict) -> dict:
    """Compact, bot-readable digest of an importer report."""
    raw = report.get("raw") or {}
    daily = report.get("daily") or {}
    errors = int(raw.get("errors") or 0) + int(daily.get("errors") or 0)
    return {
        "dry_run": bool(report.get("dry_run")),
        "imported": int(raw.get("imported") or 0),
        "inserted": int(raw.get("inserted") or 0) + int(daily.get("inserted") or 0),
        "raw_inserted": int(raw.get("inserted") or 0),
        "daily_inserted": int(daily.get("inserted") or 0),
        "duplicates": int(raw.get("duplicates") or 0),
        "skipped": (
            int(raw.get("skipped_per_sample") or 0)
            + int(raw.get("skipped_telemetry") or 0)
            + int(daily.get("skipped") or 0)
            + int(daily.get("skipped_gapfill") or 0)
        ),
        "skipped_gapfill": int(daily.get("skipped_gapfill") or 0),
        "errors": errors,
        "day_range": report.get("day_range"),
        "elapsed_s": report.get("elapsed_s"),
        "import_filename": report.get("import_filename"),
        "batch_id": report.get("batch_id"),
    }


def _mf_redact(value: str, secret: str | None) -> str:
    """Guarantee a secret can never surface in an error we return."""
    text = str(value)
    if secret and secret in text:
        text = text.replace(secret, "[redacted]")
    return text


def _mf_run_job(job_id: str, user_id, filename: str, path: Path, password: str, dry_run: bool) -> None:
    """Background worker: run the shared importer and persist the report."""
    from drhiro_api.db import SessionLocal
    from drhiro_api.services import mifitness_import

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            raise RuntimeError("user vanished before the import ran")
        with open(path, "rb") as fh:
            source = fh.read()
        report = mifitness_import.run_import(
            db,
            user=user,
            source=source,
            password=password,
            dry_run=dry_run,
            import_filename=filename,
        )
        batch_id = f"mifitness-tool-{job_id}"
        db.add(
            IngestBatch(
                user_id=user_id,
                installation_id="mifitness_import",
                batch_id=batch_id,
                result_json=report,
            )
        )
        audit(
            db, "openclaw", str(user_id), user_id, "tools.import_mifitness", "import",
            filename,
            {
                "job_id": job_id,
                "dry_run": dry_run,
                "raw_inserted": report["raw"]["inserted"],
                "raw_duplicates": report["raw"]["duplicates"],
                "batch_id": batch_id,
            },
        )
        db.commit()
        report["batch_id"] = batch_id
        with _MF_LOCK:
            _MF_JOBS[job_id].update(
                status="done",
                finished_at=datetime.now(timezone.utc).isoformat(),
                summary=_mf_summary(report),
            )
    except Exception as exc:  # noqa: BLE001 - surfaced to the bot, redacted
        try:
            db.rollback()
        except Exception:
            pass
        with _MF_LOCK:
            _MF_JOBS[job_id].update(
                status="error",
                finished_at=datetime.now(timezone.utc).isoformat(),
                error=_mf_redact(str(exc), password)[:500],
            )
    finally:
        db.close()


class ImportMifitnessTool(BaseModel):
    filename: str = Field(description="bare filename of the staged archive")
    dry_run: bool = False


class ImportStatusTool(BaseModel):
    job_id: str | None = None
    wait_seconds: int = Field(default=0, ge=0, le=50)


@router.post("/list_mifitness_archives", response_model=ToolResponse)
def tool_list_mifitness_archives(
    user: User = Depends(_resolve_user), db: Session = Depends(get_db)
):
    """List archives staged in the VPS inbox (newest first).

    Reports name, size, mtime for each staged archive and whether the sibling
    ``.zip_pw`` password file is present.  The password value is never read
    or returned here.
    """
    base = _imports_dir()
    archives: list[dict] = []
    if base.is_dir():
        for entry in sorted(base.iterdir()):
            if entry.name.startswith(".") or not entry.is_file() or entry.is_symlink():
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            archives.append(
                {
                    "filename": entry.name,
                    "size_bytes": st.st_size,
                    "size_mb": round(st.st_size / (1024 * 1024), 1),
                    "modified_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                }
            )
    archives.sort(key=lambda a: a["modified_utc"], reverse=True)
    has_password = _read_archive_password() is not None
    _tool_audit(db, user, "tools.list_mifitness_archives")
    db.commit()
    message = (
        "Staged archives are ready."
        if archives and has_password
        else (
            "No archives staged. Ask the user to drop the export ZIP into the inbox directory."
            if not archives
            else "Archives are staged but the password file is missing — tell the user to write it to .zip_pw (mode 600) in the inbox directory."
        )
    )
    return ToolResponse(
        ok=True,
        message=message,
        data={
            "directory": str(base),
            "password_file_present": has_password,
            "archives": archives,
        },
    )


@router.post("/import_mifitness_export", response_model=ToolResponse)
def tool_import_mifitness_export(
    req: ImportMifitnessTool,
    user: User = Depends(_resolve_user),
    db: Session = Depends(get_db),
):
    """Start importing a staged Mi Fitness export by bare filename.

    Reuses the shared importer service (same code path as
    ``POST /import/mifitness``).  Gap-filling only: it never overwrites a day
    another source already owns.  Runs as a background job — poll
    ``get_mifitness_import_status`` for the report.
    """
    try:
        path = _safe_archive_path(req.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    password = _read_archive_password()
    if password is None:
        return ToolResponse(
            ok=False,
            message=(
                "No archive password staged. The user must place the password in "
                f"{ZIP_PASSWORD_FILENAME} in the inbox directory (mode 600) — never "
                "send it in chat. Then retry the import."
            ),
            data={"filename": path.name, "password_file_present": False},
        )

    job_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat()
    with _MF_LOCK:
        _MF_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "filename": path.name,
            "dry_run": bool(req.dry_run),
            "started_at": started_at,
            "finished_at": None,
            "summary": None,
            "error": None,
        }
        _MF_LATEST_JOB[str(user.id)] = job_id

    _tool_audit(
        db, user, "tools.import_mifitness_start", "import", path.name,
        {"job_id": job_id, "dry_run": bool(req.dry_run)},
    )
    db.commit()

    thread = threading.Thread(
        target=_mf_run_job,
        args=(job_id, user.id, path.name, path, password, bool(req.dry_run)),
        name=f"mifitness-import-{job_id[:8]}",
        daemon=True,
    )
    thread.start()

    mode = "dry run" if req.dry_run else "import"
    return ToolResponse(
        ok=True,
        message=(
            f"Mi Fitness {mode} started for {path.name}. It runs in the background "
            "(~3 min); poll get_mifitness_import_status with this job_id. Nothing is "
            "ever overwritten — existing days keep their data."
        ),
        data={
            "job_id": job_id,
            "status": "running",
            "filename": path.name,
            "dry_run": bool(req.dry_run),
            "started_at": started_at,
        },
    )


@router.post("/get_mifitness_import_status", response_model=ToolResponse)
def tool_get_mifitness_import_status(
    req: ImportStatusTool,
    user: User = Depends(_resolve_user),
    db: Session = Depends(get_db),
):
    """Report the state of a Mi Fitness import job (default: the latest one).

    Runs a bounded wait (``wait_seconds``) so the bot need not spam polls.
    """
    job_id = req.job_id or _MF_LATEST_JOB.get(str(user.id))

    if req.wait_seconds and job_id:
        deadline = time.monotonic() + req.wait_seconds
        while time.monotonic() < deadline:
            with _MF_LOCK:
                state = (_MF_JOBS.get(job_id) or {}).get("status")
            if state and state != "running":
                break
            time.sleep(1.0)

    with _MF_LOCK:
        job = dict(_MF_JOBS.get(job_id)) if job_id else None

    if job is None:
        # Fall back to the durable report (survives an api restart).
        q = db.query(IngestBatch).filter(
            IngestBatch.user_id == user.id,
            IngestBatch.batch_id.like("mifitness-tool-%"),
        )
        if job_id:
            q = q.filter(IngestBatch.batch_id == f"mifitness-tool-{job_id}")
        batch = q.order_by(IngestBatch.created_at.desc()).first()
        if batch is None:
            return ToolResponse(
                ok=True,
                message="No Mi Fitness import job found. Start one with import_mifitness_export.",
                data={"job_id": job_id, "status": "none"},
            )
        report = batch.result_json or {}
        return ToolResponse(
            ok=True,
            message="Import finished.",
            data={
                "job_id": str(batch.batch_id).replace("mifitness-tool-", ""),
                "status": "done",
                "summary": _mf_summary(report),
            },
        )

    if job.get("status") == "done":
        summary = job.get("summary") or {}
        message = (
            f"{'Dry run' if summary.get('dry_run') else 'Import'} complete: "
            f"{summary.get('inserted', 0)} inserted, {summary.get('duplicates', 0)} duplicate, "
            f"{summary.get('errors', 0)} errors in {summary.get('elapsed_s')}s."
        )
    elif job.get("status") == "error":
        message = f"Import failed: {job.get('error')}"
    else:
        message = "Import still running."
    return ToolResponse(ok=True, message=message, data=job)




