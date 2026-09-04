"""Bulk CSV import endpoints (Section 8.2 extension).

Currently: OMRON Connect CSV export import (blood pressure history).

The OMRON Connect app exports BP history via:
  History tab -> "..." -> Export measurement data -> CSV

This endpoint parses that CSV, validates every row, and creates
measurements with provenance (source_provider="omron_csv",
recording_method="automatic" — device-measured, not manual, not
estimated). Import is idempotent per row (source_record_id derived from
the OMRON record key or a stable row hash), returns a per-row result,
and NEVER silently drops a row: rejected rows are listed with reasons.
"""

from __future__ import annotations

import csv
import hashlib
import io
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user_optional, get_user_by_telegram_id
from drhiro_api.models import Measurement, User
from drhiro_api.security import audit, validate_service_token
from drhiro_schema.metrics import MetricType
from drhiro_schema.values import BloodPressureValue

router = APIRouter(prefix="/import", tags=["import"])

MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB


def _resolve_importer(
    db: Session = Depends(get_db),
    x_service_token: str | None = Header(default=None),
    x_telegram_id: str | None = Header(default=None),
    user: User | None = Depends(get_current_user_optional),
) -> User:
    """Resolve the importing user.

    Two auth paths:
    - Web/Mini App bearer token (get_current_user_optional).
    - OpenClaw service token + X-Telegram-Id (agent sending a file).
    """
    if user is not None:
        return user
    if x_service_token and validate_service_token(x_service_token) and x_telegram_id:
        u = get_user_by_telegram_id(db, x_telegram_id)
        if u and u.status == "active":
            return u
    raise HTTPException(status_code=401, detail="Not authenticated")


class ImportRowResult(BaseModel):
    status: str  # accepted | duplicate | rejected
    reason: str | None = None
    recorded_at: datetime | None = None
    values: dict | None = None


class ImportResult(BaseModel):
    accepted: int
    duplicates: int
    rejected: list[dict]
    rows: list[ImportRowResult]


def _parse_omron_datetime(value: str) -> datetime | None:
    """Parse OMRON CSV datetime fields.

    Handles: '2022-12-07T19:05:52', '2022-12-07 19:05:52',
    '2022-12-07 19:05', with optional timezone suffix. Returns UTC.
    """
    if not value or not value.strip():
        return None
    value = value.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _parse_float(value: str) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_int(value: str) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _row_key(row: dict, row_idx: int) -> str:
    """Stable source_record_id: prefer an OMRON key column, else hash the row."""
    for key in ("OmronBloodPressureKey", "ID", "RecordID", "record_id"):
        if row.get(key):
            return f"omron:{row[key]}"
    canonical = "|".join(f"{k}={row.get(k, '')}" for k in sorted(row))
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
    return f"omron:{row_idx}:{digest}"


@router.post("/omron-csv", response_model=ImportResult)
async def import_omron_csv(
    file: UploadFile = File(...),
    importer: User = Depends(_resolve_importer),
    db: Session = Depends(get_db),
):
    """Import an OMRON Connect CSV export of blood pressure history.

    Authenticated via bearer token (web/Mini App) OR the OpenClaw service
    token + X-Telegram-Id headers (agent file import).
    """
    user = importer
    content = await file.read()
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="CSV too large (max 5 MB)")
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("latin-1")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Unrecognized CSV encoding")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV has no header row")

    # Column detection: find systolic/diastolic/date columns by name (case-insensitive).
    cols = {c.strip().lower(): c for c in reader.fieldnames if c}
    sys_col = next((cols[k] for k in ("systolic", "systolic_mmhg", "sys", "blood pressure systolic") if k in cols), None)
    dia_col = next((cols[k] for k in ("diastolic", "diastolic_mmhg", "dia", "blood pressure diastolic") if k in cols), None)
    pulse_col = next((cols[k] for k in ("pulse", "pulse_bpm", "pulse rate") if k in cols), None)
    dt_col = next((cols[k] for k in ("datetime", "date time", "date_time", "date", "time", "recorded_at", "measured_at", "reading time") if k in cols), None)

    if not sys_col or not dia_col or not dt_col:
        raise HTTPException(
            status_code=400,
            detail="Could not find systolic/diastolic/datetime columns. "
            "Expected OMRON Connect export (Systolic, Diastolic, Date/Time) — "
            f"found columns: {reader.fieldnames}",
        )

    accepted = duplicates = 0
    rejected: list[dict] = []
    rows: list[ImportRowResult] = []

    for row_idx, row in enumerate(reader, start=2):  # 1-based, header = row 1
        if not any(row.values()):
            continue  # skip blank lines

        try:
            systolic = _parse_int(row.get(sys_col))
            diastolic = _parse_int(row.get(dia_col))
            pulse = _parse_int(row.get(pulse_col)) if pulse_col else None
            measured_at = _parse_omron_datetime(row.get(dt_col))

            if systolic is None or diastolic is None:
                rejected.append({"row": row_idx, "reason": "missing systolic/diastolic"})
                rows.append(ImportRowResult(status="rejected", reason="missing systolic/diastolic"))
                continue
            if measured_at is None:
                rejected.append({"row": row_idx, "reason": f"unparseable datetime: {row.get(dt_col)!r}"})
                rows.append(ImportRowResult(status="rejected", reason="unparseable datetime"))
                continue

            values = {"systolic_mmhg": systolic, "diastolic_mmhg": diastolic}
            if pulse is not None:
                values["pulse_bpm"] = pulse
            try:
                BloodPressureValue.model_validate(values)
            except Exception as e:
                rejected.append({"row": row_idx, "reason": f"invalid values: {e}"})
                rows.append(ImportRowResult(status="rejected", reason="invalid values"))
                continue

            source_record_id = _row_key(row, row_idx)
            existing = (
                db.query(Measurement)
                .filter(
                    Measurement.user_id == user.id,
                    Measurement.source_provider == "omron_csv",
                    Measurement.source_record_id == source_record_id,
                )
                .first()
            )
            if existing:
                duplicates += 1
                rows.append(ImportRowResult(status="duplicate", recorded_at=existing.start_at, values=existing.value_json))
                continue

            m = Measurement(
                user_id=user.id,
                metric_type=MetricType.BLOOD_PRESSURE,
                start_at=measured_at,
                end_at=measured_at,
                value_json=values,
                unit="mmHg",
                source_provider="omron_csv",
                source_record_id=source_record_id,
                source_device_id="omron-csv-import",
                recording_method="automatic",
                confidence=1.0,
                metadata_json={"import_file": file.filename, "source": "omron_connect_csv"},
            )
            db.add(m)
            accepted += 1
            rows.append(ImportRowResult(status="accepted", recorded_at=measured_at, values=values))
        except Exception as e:
            rejected.append({"row": row_idx, "reason": f"unexpected error: {e}"})
            rows.append(ImportRowResult(status="rejected", reason="unexpected error"))

    if accepted:
        audit(db, "user", str(user.id), user.id, "import.omron_csv",
              "measurement", file.filename or "omron.csv",
              {"accepted": accepted, "duplicates": duplicates, "rejected": len(rejected)})
    db.commit()
    return ImportResult(accepted=accepted, duplicates=duplicates, rejected=rejected, rows=rows)
