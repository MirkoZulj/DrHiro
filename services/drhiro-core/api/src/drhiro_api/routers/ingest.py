"""Health ingestion endpoints. Section 8.2.

The Health Connect batch endpoint is idempotent by batch_id and by
per-record source ID. Partial-success responses report which records
were accepted and which failed, so the Android bridge can retry only
the failures.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from drhiro_api.config import get_settings
from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import DeviceConnection, IngestBatch, Measurement, User
from drhiro_api.security import audit
from drhiro_schema.metrics import HEALTH_CONNECT_RECORD_MAP, PLAUSIBLE_RANGES, MetricType
from drhiro_schema.values import VALUE_SCHEMAS

router = APIRouter(prefix="/ingest", tags=["ingest"])


class BatchRecord(BaseModel):
    source_record_id: str = Field(min_length=1, max_length=255)
    record_type: str
    start_at: datetime
    end_at: datetime | None = None
    source_timezone: str | None = None
    values: dict
    device: dict | None = None
    client_modified_at: datetime | None = None

    @model_validator(mode="after")
    def _require_end_after_start(self):
        if self.end_at and self.end_at < self.start_at:
            raise ValueError("end_at before start_at")
        return self


class HealthConnectBatchRequest(BaseModel):
    installation_id: str
    batch_id: str = Field(min_length=8, max_length=64)
    records: list[BatchRecord] = Field(max_length=500)


class IngestResult(BaseModel):
    accepted: int
    duplicates: int
    rejected: list[dict]
    errors: list[str] = []


@router.post("/health-connect/batch", response_model=IngestResult)
def ingest_health_connect_batch(
    req: HealthConnectBatchRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ingest a batch of Health Connect records with idempotency.

    - batch_id is the idempotency key: replaying the same batch returns
      the same result without creating duplicate rows.
    - Per-record upsert by (user_id, source_provider, source_record_id).
    - The authenticated installation must belong to the target user.
    """
    settings = get_settings()
    if len(req.records) > settings.max_batch_size:
        raise HTTPException(status_code=413, detail=f"Batch exceeds max size {settings.max_batch_size}")

    # Batch-level idempotency: replaying the same batch returns the stored result.
    existing_batch = (
        db.query(IngestBatch)
        .filter(IngestBatch.user_id == user.id, IngestBatch.batch_id == req.batch_id)
        .first()
    )
    if existing_batch:
        return IngestResult(**existing_batch.result_json)

    # Reject records whose installation is not linked to this user.
    conn = (
        db.query(DeviceConnection)
        .filter(
            DeviceConnection.user_id == user.id,
            DeviceConnection.external_device_id_hash == req.installation_id,
        )
        .first()
    )
    if not conn:
        # A linked device is required for Health Connect uploads.
        raise HTTPException(status_code=403, detail="Installation not linked to this user")

    accepted, duplicates = 0, 0
    rejected: list[dict] = []
    # Records already seen within THIS batch — the pre-query below only sees
    # committed rows, so two copies of the same source_record_id in one batch
    # would both pass it and both get inserted, tripping the unique constraint
    # at commit. Track them to count as duplicates instead of a 500.
    seen: set[str] = set()

    for rec in req.records:
        metric_type = HEALTH_CONNECT_RECORD_MAP.get(rec.record_type)
        if metric_type is None:
            rejected.append({"source_record_id": rec.source_record_id, "reason": f"unknown record_type {rec.record_type}"})
            continue

        # Validate values against the canonical schema + plausibility.
        schema = VALUE_SCHEMAS.get(metric_type)
        try:
            if schema:
                schema.model_validate(rec.values)
        except Exception as e:
            rejected.append({"source_record_id": rec.source_record_id, "reason": f"invalid values: {e}"})
            continue

        if rec.source_record_id in seen:
            duplicates += 1
            continue

        existing = (
            db.query(Measurement)
            .filter(
                Measurement.user_id == user.id,
                Measurement.source_provider == "health_connect",
                Measurement.source_record_id == rec.source_record_id,
            )
            .first()
        )
        if existing:
            duplicates += 1
            continue

        seen.add(rec.source_record_id)
        db.add(
            Measurement(
                user_id=user.id,
                metric_type=metric_type,
                start_at=rec.start_at,
                end_at=rec.end_at or rec.start_at,
                value_json=rec.values,
                unit=_unit_for(metric_type),
                source_provider="health_connect",
                source_record_id=rec.source_record_id,
                source_device_id=(rec.device or {}).get("model") or conn.id.hex,
                recording_method="automatic",
                confidence=1.0,
                metadata_json={"source_timezone": rec.source_timezone, "device": rec.device},
            )
        )
        accepted += 1

    # Idempotency key tracking: a batch_id map would live in Redis in prod.
    if conn.last_sync_at is None or (datetime.now().timestamp() - conn.last_sync_at.timestamp()) > 60:
        conn.last_sync_at = datetime.now()

    if accepted:
        audit(db, "android", req.installation_id, user.id, "ingest.health_connect_batch",
              "measurement", req.batch_id, {"accepted": accepted, "duplicates": duplicates, "rejected": len(rejected)})

    result = IngestResult(accepted=accepted, duplicates=duplicates, rejected=rejected)
    # Persist the idempotency record regardless of partial success.
    db.add(
        IngestBatch(
            user_id=user.id,
            installation_id=req.installation_id,
            batch_id=req.batch_id,
            result_json=result.model_dump(),
        )
    )
    db.commit()
    return result


class ManualWeightRequest(BaseModel):
    weight_kg: float = Field(ge=20, le=400)
    measured_at: datetime | None = None
    note: str | None = None


class ManualBloodPressureRequest(BaseModel):
    systolic_mmhg: int = Field(ge=40, le=300)
    diastolic_mmhg: int = Field(ge=20, le=200)
    pulse_bpm: int | None = Field(default=None, ge=20, le=250)
    measured_at: datetime | None = None
    body_position: str | None = None
    measurement_location: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _sys_gt_dia(self):
        if self.systolic_mmhg <= self.diastolic_mmhg:
            raise ValueError("systolic must be greater than diastolic")
        return self


class ManualWaterRequest(BaseModel):
    amount_ml: int = Field(ge=0, le=10000)
    measured_at: datetime | None = None
    category: str = "water"  # one of LIQUID_CATEGORIES


class ManualResult(BaseModel):
    id: str
    metric_type: str
    recorded_at: datetime


def _manual_measurement(db: Session, user: User, metric_type: str, values: dict, measured_at: datetime | None, method: str = "manual", note: str | None = None) -> Measurement:
    m = Measurement(
        user_id=user.id,
        metric_type=metric_type,
        start_at=measured_at or datetime.now(),
        end_at=measured_at or datetime.now(),
        value_json=values,
        unit=_unit_for(metric_type),
        source_provider="manual",
        source_record_id=f"manual-{uuid.uuid4().hex}",
        recording_method=method,
        confidence=1.0,
        metadata_json={"note": note} if note else None,
    )
    db.add(m)
    db.flush()
    return m


@router.post("/manual/weight", response_model=ManualResult)
def manual_weight(req: ManualWeightRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    m = _manual_measurement(db, user, MetricType.WEIGHT, {"weight_kg": req.weight_kg}, req.measured_at, note=req.note)
    audit(db, "user", str(user.id), user.id, "ingest.manual_weight", "measurement", str(m.id))
    db.commit()
    return ManualResult(id=str(m.id), metric_type=MetricType.WEIGHT, recorded_at=m.start_at)


@router.post("/manual/blood-pressure", response_model=ManualResult)
def manual_bp(req: ManualBloodPressureRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    values = {
        "systolic_mmhg": req.systolic_mmhg,
        "diastolic_mmhg": req.diastolic_mmhg,
        "pulse_bpm": req.pulse_bpm,
        "body_position": req.body_position,
        "measurement_location": req.measurement_location,
    }
    m = _manual_measurement(db, user, MetricType.BLOOD_PRESSURE, values, req.measured_at, note=req.note)
    audit(db, "user", str(user.id), user.id, "ingest.manual_bp", "measurement", str(m.id))
    db.commit()
    return ManualResult(id=str(m.id), metric_type=MetricType.BLOOD_PRESSURE, recorded_at=m.start_at)


@router.post("/manual/water", response_model=ManualResult)
def manual_water(req: ManualWaterRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cat = req.category if req.category in LIQUID_CATEGORIES else "water"
    m = _manual_measurement(db, user, MetricType.WATER, {"amount_ml": req.amount_ml, "category": cat}, req.measured_at)
    audit(db, "user", str(user.id), user.id, "ingest.manual_water", "measurement", str(m.id))
    db.commit()
    return ManualResult(id=str(m.id), metric_type=MetricType.WATER, recorded_at=m.start_at)


class ManualTextRequest(BaseModel):
    text: str = Field(min_length=1)


class ManualTextMeasurement(BaseModel):
    id: str
    metric_type: str
    value_json: dict
    recorded_at: datetime


class ManualTextResult(BaseModel):
    accepted: int
    measurements: list[ManualTextMeasurement]
    unparsed: str


# Deterministic regex parser for natural-language measurement logging.
# Order matters: blood pressure (slashes/"over") first, then weight, then water.
_WEIGHT_KG_RE = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*(?:kg|kilos?|kilograms?)\b", re.IGNORECASE)
_WEIGHT_WORD_RE = re.compile(r"\b(?:weight|down to|down at)\s+(?:is\s+)?(\d{1,3}(?:[.,]\d+)?)\b", re.IGNORECASE)
_BP_RE = re.compile(r"\b(\d{2,3})\s*(?:/|over)\s*(\d{2,3})\b", re.IGNORECASE)
_PULSE_RE = re.compile(r"\bpulse\s+(\d{2,3})\b", re.IGNORECASE)
_WATER_AMOUNT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(ml|milliliter|millilitre|milliliters|millilitres|l|liter|litre|liters|litres)s?\b", re.IGNORECASE)
_GLASSES_RE = re.compile(r"\b(\d+)\s+glass(?:es)?\b", re.IGNORECASE)

# ---- Liquid categories (for the Liquid tile) ----
# A liquid measurement is stored under MetricType.WATER with
# value_json = {"amount_ml": N, "category": <liquid_category>}.
# Old rows have no category -> treated as "water".
LIQUID_CATEGORIES = [
    "water", "non_alcoholic", "beer", "wine", "spirits", "other_alcohol",
]
# Keyword -> category, evaluated in order (most specific first). Croatian +
# English keywords. Each value is a compiled regex matched as a word boundary
# so "vino" (wine) does not hit "vitamin" etc.
_LIQUID_KEYWORDS: list[tuple[str, re.Pattern]] = [
    # spirits (strong, unmeasured-by-glass liquors)
    ("spirits", re.compile(r"\b(?:whiskey|whisky|viski|vodka|votka|rum|gin|brandy|rakija|šljivovica|sljivovica|konjak|cognac|tequila|loza|travarica|brändy|brandi)\b", re.IGNORECASE)),
    # wine
    ("wine", re.compile(r"\b(?:wine|vino|rose|rosé|prosecco|šampanjac|sampanjac|champagne|crno|bijelo|bjelo)\b", re.IGNORECASE)),
    # beer
    ("beer", re.compile(r"\b(?:beer|pivo|lager|ale|stout|heineken|ozujsko|karlovačko|karlovacko|točeno|toceno|radler)\b", re.IGNORECASE)),
    # other alcoholic (cocktails, liqueurs, cider)
    ("other_alcohol", re.compile(r"\b(?:cocktail|koktel|cider|jabolčnik|jabolcnik|liqueur|liker|aperol|martini|baileys|amaretto|mojito|negroni|spritz)\b", re.IGNORECASE)),
    # non-alcoholic beverages (coffee, tea, juice, soda, milk, energy, ...)
    ("non_alcoholic", re.compile(r"\b(?:coffee|kava|cappuccino|latte|tea|čaj|caj|ice\s*tea|juice|sok|soda|cola|coke|coca|fanta|sprite|smoothie|shake|milk|mlijeko|mliko|energy|redbull|monster|cedevita|limunada|nectar)\b", re.IGNORECASE)),
    # water (lowest priority)
    ("water", re.compile(r"\b(?:water|voda|mineral)\b", re.IGNORECASE)),
]


def _liquid_category(text: str) -> str:
    """Classify a liquid phrase into a category. Defaults to water."""
    for cat, rx in _LIQUID_KEYWORDS:
        if rx.search(text):
            return cat
    return "water"


def _to_float(num: str) -> float:
    return float(num.replace(",", "."))


def _parse_text_measurements(text: str) -> tuple[list[tuple[str, dict]], str]:
    """Parse free text into (metric_type, value_json) pairs.

    Returns (parsed, unparsed_text). Each pattern matched is stripped from
    the working text so the remainder can be reported as unparsed.
    """
    parsed: list[tuple[str, dict]] = []
    rest = text

    # Blood pressure: e.g. "120/80", "bp 120/80", "blood pressure 120 over 80",
    # "128/78 pulse 64". Also captures "120/80 pulse 64".
    bp_matches = list(_BP_RE.finditer(rest))
    for m in bp_matches:
        sys_v, dia_v = int(m.group(1)), int(m.group(2))
        if not (40 <= sys_v <= 300 and 20 <= dia_v <= 200 and sys_v > dia_v):
            continue
        pulse = None
        after = rest[m.end():m.end() + 30]
        pm = _PULSE_RE.search(after)
        if pm:
            pulse = int(pm.group(1))
        parsed.append((MetricType.BLOOD_PRESSURE, {
            "systolic_mmhg": sys_v,
            "diastolic_mmhg": dia_v,
            "pulse_bpm": pulse,
        }))
    rest = _BP_RE.sub("", rest)
    # Strip the pulse annotation tied to a BP reading so it isn't reported as unparsed.
    rest = _PULSE_RE.sub("", rest)

    # Weight with explicit unit: "78.5 kg", "78,5kg"
    for m in _WEIGHT_KG_RE.finditer(rest):
        kg = _to_float(m.group(1))
        if 20 <= kg <= 400:
            parsed.append((MetricType.WEIGHT, {"weight_kg": kg}))
    rest = _WEIGHT_KG_RE.sub("", rest)

    # Weight with a leading keyword: "weight 78.5", "down to 78"
    for m in _WEIGHT_WORD_RE.finditer(rest):
        kg = _to_float(m.group(1))
        if 20 <= kg <= 400:
            parsed.append((MetricType.WEIGHT, {"weight_kg": kg}))
    rest = _WEIGHT_WORD_RE.sub("", rest)

    # Water in glasses: "drank 2 glasses" (1 glass = 250 ml).
    # Category determined from the surrounding phrase (e.g. "2 glasses of wine").
    for m in _GLASSES_RE.finditer(rest):
        ml = int(m.group(1)) * 250
        if 0 <= ml <= 10000:
            cat = _liquid_category(rest[max(0, m.start() - 20):m.end() + 20])
            parsed.append((MetricType.WATER, {"amount_ml": ml, "category": cat}))
    rest = _GLASSES_RE.sub("", rest)

    # Liquid by volume: "2.5 l water", "water 250 ml", "0.5 l beer",
    # "200 ml coffee", "a glass of wine".
    for m in _WATER_AMOUNT_RE.finditer(rest):
        amt = _to_float(m.group(1))
        unit = m.group(2).lower()
        ml = int(amt * 1000) if unit.startswith("l") else int(amt)
        if 0 <= ml <= 10000:
            cat = _liquid_category(rest[max(0, m.start() - 30):m.end() + 30])
            parsed.append((MetricType.WATER, {"amount_ml": ml, "category": cat}))
    rest = _WATER_AMOUNT_RE.sub("", rest)

    return parsed, rest.strip()


@router.post("/manual/text", response_model=ManualTextResult)
def manual_text(req: ManualTextRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Natural-language measurement logging via deterministic parsing.

    Supported: weight ('78.5 kg', 'weight 78.5', '78,5kg', 'down to 78'),
    blood pressure ('120/80', 'bp 120/80', 'blood pressure 120 over 80',
    '128/78 pulse 64'), water ('2.5 l water', 'water 250 ml', 'drank 2 glasses').
    """
    parsed, unparsed = _parse_text_measurements(req.text)
    if not parsed:
        raise HTTPException(
            status_code=422,
            detail=(
                "Could not parse any measurement from the text. Supported formats: "
                "weight ('78.5 kg', 'weight 78.5', 'down to 78'), "
                "blood pressure ('120/80', 'bp 128/78 pulse 64', 'blood pressure 120 over 80'), "
                "water ('2.5 l water', 'water 250 ml', 'drank 2 glasses')."
            ),
        )

    now = datetime.now()
    results: list[ManualTextMeasurement] = []
    for metric_type, values in parsed:
        schema = VALUE_SCHEMAS.get(metric_type)
        if schema is not None:
            try:
                schema.model_validate(values)
            except Exception:
                continue  # drop values that fail canonical validation
        m = _manual_measurement(db, user, metric_type, values, now)
        results.append(ManualTextMeasurement(
            id=str(m.id),
            metric_type=metric_type,
            value_json=values,
            recorded_at=m.start_at,
        ))

    if not results:
        raise HTTPException(
            status_code=422,
            detail="Parsed input was rejected by canonical value validation; nothing was saved.",
        )

    audit(db, "user", str(user.id), user.id, "ingest.manual_text", "measurement", None,
          {"accepted": len(results), "parsed": req.text})
    db.commit()
    return ManualTextResult(accepted=len(results), measurements=results, unparsed=unparsed)


@router.get("/status")
def ingest_status(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    conns = (
        db.query(DeviceConnection)
        .filter(DeviceConnection.user_id == user.id)
        .all()
    )
    last_measurement = (
        db.query(Measurement)
        .filter(Measurement.user_id == user.id)
        .order_by(Measurement.start_at.desc())
        .first()
    )
    return {
        "devices": [
            {
                "provider": c.provider,
                "device_name": c.device_name,
                "status": c.status,
                "last_sync_at": c.last_sync_at,
            }
            for c in conns
        ],
        "last_record_at": last_measurement.start_at if last_measurement else None,
        "note": "Missing wearable data is 'missing', never zero.",
    }


def _unit_for(metric_type: str) -> str | None:
    return {
        MetricType.STEPS: "count",
        MetricType.DISTANCE: "m",
        MetricType.ACTIVE_CALORIES: "kcal",
        MetricType.HEART_RATE: "bpm",
        MetricType.RESTING_HEART_RATE: "bpm",
        MetricType.SLEEP: "min",
        MetricType.SPO2: "%",
        MetricType.WEIGHT: "kg",
        MetricType.BLOOD_PRESSURE: "mmHg",
        MetricType.WATER: "ml",
    }.get(metric_type)
