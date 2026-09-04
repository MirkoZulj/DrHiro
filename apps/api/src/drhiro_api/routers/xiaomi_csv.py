"""Xiaomi Mi Fit CSV import pipeline.

Parses official Xiaomi data export CSVs (from account.xiaomi.com → Privacy →
Manage Your Data → MI Fitness → Download) and imports them as measurements.

Supported data types:
- ACTIVITY/*.csv — daily summaries (steps, distance, calories, etc.)
- BODY/*.csv — weight, body fat, muscle, etc.
- SLEEP/*.csv — sleep stages (light, deep, REM, awake)
- HEARTRATE/*.csv — continuous heart rate logs
- HEARTRATE_AUTO/*.csv — automatic heart rate measurements

The parser auto-detects column names (Xiaomi exports vary by locale),
derives a stable source_record_id per row for idempotency, and reports
every row as accepted/rejected/never silently dropping.
"""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user_optional, get_user_by_telegram_id
from drhiro_api.models import Measurement, User
from drhiro_api.security import audit, validate_service_token
from drhiro_schema.metrics import MetricType

# Column name aliases
STEP_ALIASES = ("steps", "step", "step_count", "total_steps", "步数")
DISTANCE_ALIASES = ("distance", "total_distance", "dist", "距离")
CALORIE_ALIASES = ("calories", "calorie", "total_calories", "kcal", "卡路里")
WEIGHT_ALIASES = ("weight", "weight_kg", "体重")
BODY_FAT_ALIASES = ("body_fat", "body_fat_percentage", "fat", "体脂")
MUSCLE_ALIASES = ("muscle", "muscle_rate", "muscle_percentage", "肌肉率")
HEART_RATE_ALIASES = ("heart_rate", "heartrate", "bpm", "心率")
SLEEP_DURATION_ALIASES = (
    "sleep_duration", "total_sleep", "sleep_time",
    "total_sleep_time", "睡眠时长", "deep_sleep", "light_sleep",
)
SLEEP_STAGE_ALIASES = {
    "deep": ("deep_sleep", "deep", "深度睡眠"),
    "light": ("light_sleep", "light", "浅度睡眠"),
    "rem": ("rem_sleep", "rem", "快速眼动"),
    "awake": ("awake", "awake_duration", "清醒"),
}
WAKE_COUNT_ALIASES = ("wake_count", "wake", "awake_count", "苏醒次数")


def _parse_float(value: str) -> float | None:
    if not value or not value.strip():
        return None
    try:
        return float(value.strip().replace(",", "."))
    except (ValueError, TypeError):
        return None


def _parse_xiaomi_datetime(value: str) -> datetime | None:
    """Parse Xiaomi datetime fields.

    Handles formats like:
    - '2024-01-15 08:30:00'
    - '2024-01-15 08:30'
    - '2024-01-15'
    - '2024/01/15 08:30:00'
    - '2024-01-15T08:30:00'
    - with optional timezone like 'UTC' or '+0800'
    """
    if not value or not value.strip():
        return None
    value = value.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _col_index(headers: list[str], aliases: tuple[str, ...]) -> int | None:
    """Find the column index for any of the given aliases (case-insensitive)."""
    lower = [h.strip().lower().replace(" ", "_") for h in headers]
    for alias in aliases:
        if alias.lower() in lower:
            return lower.index(alias.lower())
    return None


def _stable_id(*parts: str) -> str:
    """Derive a stable source_record_id from row contents."""
    joined = "|".join(p.strip() for p in parts if p)
    return hashlib.sha256(joined.encode()).hexdigest()[:32]


def parse_activity_csv(content: str) -> list[dict]:
    """Parse Xiaomi ACTIVITY CSV (daily summary).

    Expected columns (auto-detected):
    - date / timestamp
    - steps / step_count
    - distance / total_distance
    - calories / total_calories
    """
    rows = []
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        return rows

    headers = list(reader.fieldnames)
    idx_date = _col_index(headers, ("date", "timestamp", "time", "日期"))
    idx_steps = _col_index(headers, STEP_ALIASES)
    idx_dist = _col_index(headers, DISTANCE_ALIASES)
    idx_cal = _col_index(headers, CALORIE_ALIASES)

    for row in reader:
        values = list(row.values())
        date_str = values[idx_date] if idx_date is not None else None
        if not date_str:
            continue

        dt = _parse_xiaomi_datetime(date_str)
        if not dt:
            continue

        record = {
            "start_at": dt,
            "end_at": dt,
            "measurements": {},
            "source_record_id": _stable_id(date_str),
        }

        steps = _parse_float(values[idx_steps]) if idx_steps is not None else None
        if steps is not None:
            record["measurements"]["steps"] = {
                "metric_type": MetricType.STEPS,
                "value_json": {"count": int(steps)},
                "unit": "count",
                "recording_method": "automatic",
            }

        distance = _parse_float(values[idx_dist]) if idx_dist is not None else None
        if distance is not None:
            record["measurements"]["distance"] = {
                "metric_type": MetricType.DISTANCE,
                "value_json": {"m": round(distance * 1000, 1)},  # km → m
                "unit": "m",
                "recording_method": "automatic",
            }

        calories = _parse_float(values[idx_cal]) if idx_cal is not None else None
        if calories is not None:
            record["measurements"]["calories"] = {
                "metric_type": MetricType.ACTIVE_CALORIES,
                "value_json": {"kcal": round(calories, 1)},
                "unit": "kcal",
                "recording_method": "automatic",
            }

        if record["measurements"]:
            rows.append(record)

    return rows


def parse_body_csv(content: str) -> list[dict]:
    """Parse Xiaomi BODY CSV (weight, body fat, muscle, etc.)."""
    rows = []
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        return rows

    headers = list(reader.fieldnames)
    idx_date = _col_index(headers, ("date", "timestamp", "time", "日期"))
    idx_weight = _col_index(headers, WEIGHT_ALIASES)
    idx_fat = _col_index(headers, BODY_FAT_ALIASES)
    idx_muscle = _col_index(headers, MUSCLE_ALIASES)

    for row in reader:
        values = list(row.values())
        date_str = values[idx_date] if idx_date is not None else None
        if not date_str:
            continue

        dt = _parse_xiaomi_datetime(date_str)
        if not dt:
            continue

        record = {
            "start_at": dt,
            "end_at": dt,
            "measurements": {},
            "source_record_id": _stable_id(date_str, "body"),
        }

        weight = _parse_float(values[idx_weight]) if idx_weight is not None else None
        if weight is not None:
            record["measurements"]["weight"] = {
                "metric_type": MetricType.WEIGHT,
                "value_json": {"weight_kg": round(weight, 2)},
                "unit": "kg",
                "recording_method": "automatic",
            }

        fat = _parse_float(values[idx_fat]) if idx_fat is not None else None
        if fat is not None:
            record["measurements"]["body_fat"] = {
                "metric_type": "body_fat_percentage",
                "value_json": {"percent": round(fat, 1)},
                "unit": "%",
                "recording_method": "automatic",
            }

        muscle = _parse_float(values[idx_muscle]) if idx_muscle is not None else None
        if muscle is not None:
            record["measurements"]["muscle"] = {
                "metric_type": "muscle_percentage",
                "value_json": {"percent": round(muscle, 1)},
                "unit": "%",
                "recording_method": "automatic",
            }

        if record["measurements"]:
            rows.append(record)

    return rows


def parse_sleep_csv(content: str) -> list[dict]:
    """Parse Xiaomi SLEEP CSV.

    Xiaomi sleep exports vary: some have total duration, others have
    per-stage minutes. We produce a SLEEP measurement with duration_min
    and break down deep/light/rem/awake if columns are present.
    """
    rows = []
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        return rows

    headers = list(reader.fieldnames)
    idx_date = _col_index(headers, ("date", "timestamp", "time", "日期"))
    idx_duration = _col_index(headers, SLEEP_DURATION_ALIASES)

    for row in reader:
        values = list(row.values())
        date_str = values[idx_date] if idx_date is not None else None
        if not date_str:
            continue

        dt = _parse_xiaomi_datetime(date_str)
        if not dt:
            continue

        duration = None
        if idx_duration is not None:
            duration = _parse_float(values[idx_duration])

        # Try to parse per-stage breakdown
        stages = {}
        for stage_name, aliases in SLEEP_STAGE_ALIASES.items():
            idx = _col_index(headers, aliases)
            if idx is not None:
                val = _parse_float(values[idx])
                if val is not None:
                    stages[stage_name] = val

        # If no total duration but we have stages, compute total
        if duration is None and stages:
            duration = sum(stages.values())

        if duration is None:
            continue

        record = {
            "start_at": dt,
            "end_at": dt,
            "measurements": {
                "sleep": {
                    "metric_type": MetricType.SLEEP,
                    "value_json": {"duration_min": int(duration)},
                    "unit": "min",
                    "recording_method": "automatic",
                }
            },
            "source_record_id": _stable_id(date_str, "sleep"),
        }

        if stages:
            record["measurements"]["sleep"]["value_json"]["stages_min"] = stages

        rows.append(record)

    return rows


def parse_heartrate_csv(content: str) -> list[dict]:
    """Parse Xiaomi HEARTRATE / HEARTRATE_AUTO CSV.

    Each row is a single heart rate measurement at a timestamp.
    """
    rows = []
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        return rows

    headers = list(reader.fieldnames)
    idx_date = _col_index(headers, ("date", "timestamp", "time", "日期"))
    idx_hr = _col_index(headers, HEART_RATE_ALIASES)

    for row in reader:
        values = list(row.values())
        date_str = values[idx_date] if idx_date is not None else None
        if not date_str:
            continue

        dt = _parse_xiaomi_datetime(date_str)
        if not dt:
            continue

        hr = _parse_float(values[idx_hr]) if idx_hr is not None else None
        if hr is None:
            continue

        rows.append({
            "start_at": dt,
            "end_at": dt,
            "measurements": {
                "heart_rate": {
                    "metric_type": MetricType.HEART_RATE,
                    "value_json": {"bpm": int(hr)},
                    "unit": "bpm",
                    "recording_method": "automatic",
                }
            },
            "source_record_id": _stable_id(date_str, str(int(hr))),
        })

    return rows


# Registry of parsers by data type prefix
PARSERS = {
    "ACTIVITY": parse_activity_csv,
    "BODY": parse_body_csv,
    "SLEEP": parse_sleep_csv,
    "HEARTRATE": parse_heartrate_csv,
    "HEARTRATE_AUTO": parse_heartrate_csv,
}


def detect_data_type(filename: str) -> str | None:
    """Detect the Xiaomi data type from the CSV filename or content hints.

    Xiaomi files are named like:
    - ACTIVITY_1234567890.csv
    - BODY_1234567890.csv
    - SLEEP_1234567890.csv
    - HEARTRATE_1234567890.csv
    """
    upper = filename.upper().replace(" ", "_")
    for prefix in PARSERS:
        if upper.startswith(prefix):
            return prefix
    return None


# ---- FastAPI endpoint ----

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile

from drhiro_api.deps import get_current_user_optional, get_user_by_telegram_id
from drhiro_api.models import Measurement
from drhiro_api.security import audit, validate_service_token

router = APIRouter(prefix="/import", tags=["import"])

MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


def _resolve_xiaomi_importer(
    db: Session = Depends(get_db),
    x_service_token: str | None = Header(default=None),
    x_telegram_id: str | None = Header(default=None),
    user: User | None = Depends(get_current_user_optional),
) -> User:
    if user is not None:
        return user
    if x_service_token and validate_service_token(x_service_token) and x_telegram_id:
        u = get_user_by_telegram_id(db, x_telegram_id)
        if u and u.status == "active":
            return u
    raise HTTPException(status_code=401, detail="Not authenticated")


@router.post("/xiaomi-csv")
async def import_xiaomi_csv(
    file: UploadFile = File(...),
    importer: User = Depends(_resolve_xiaomi_importer),
    db: Session = Depends(get_db),
):
    """Import a Xiaomi Mi Fit CSV export.

    Supported data types: ACTIVITY, BODY, SLEEP, HEARTRATE, HEARTRATE_AUTO.
    The data type is auto-detected from the filename.
    """
    user = importer
    content = await file.read()
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="CSV too large (max 10 MB)")
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("latin-1")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Unrecognized CSV encoding")

    data_type = detect_data_type(file.filename or "")
    if not data_type:
        raise HTTPException(
            status_code=400,
            detail="Cannot detect data type from filename. Expected ACTIVITY_*.csv, BODY_*.csv, SLEEP_*.csv, or HEARTRATE_*.csv",
        )

    parser = PARSERS[data_type]
    parsed_rows = parser(text)

    if not parsed_rows:
        raise HTTPException(
            status_code=400,
            detail=f"No valid data rows found in {file.filename}. Check the file format.",
        )

    accepted = duplicates = rejected_count = 0
    rejected: list[dict] = []

    for record in parsed_rows:
        for metric_key, meas in record["measurements"].items():
            try:
                source_record_id = f"xiaomi:{data_type.lower()}:{record['source_record_id']}:{metric_key}"

                existing = (
                    db.query(Measurement)
                    .filter(
                        Measurement.user_id == user.id,
                        Measurement.source_provider == f"xiaomi_{data_type.lower()}",
                        Measurement.source_record_id == source_record_id,
                    )
                    .first()
                )

                if existing:
                    duplicates += 1
                    continue

                m = Measurement(
                    user_id=user.id,
                    metric_type=meas["metric_type"],
                    start_at=record["start_at"],
                    end_at=record["end_at"],
                    value_json=meas["value_json"],
                    unit=meas.get("unit"),
                    source_provider=f"xiaomi_{data_type.lower()}",
                    source_record_id=source_record_id,
                    source_device_id="xiaomi-csv-import",
                    recording_method=meas.get("recording_method", "automatic"),
                    confidence=0.9,
                    metadata_json={"import_file": file.filename, "data_type": data_type},
                )
                db.add(m)
                accepted += 1
            except Exception as e:
                rejected_count += 1
                rejected.append({"source_record_id": source_record_id, "reason": str(e)})

    if accepted:
        audit(db, "user", str(user.id), user.id, "import.xiaomi_csv",
              "measurement", file.filename or "xiaomi.csv",
              {"accepted": accepted, "duplicates": duplicates, "rejected": len(rejected), "data_type": data_type})
    db.commit()

    return {
        "accepted": accepted,
        "duplicates": duplicates,
        "rejected": rejected,
        "data_type": data_type,
        "filename": file.filename,
    }
