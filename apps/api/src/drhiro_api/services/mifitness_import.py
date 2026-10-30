"""Mi Fitness manual-export importer (gap-filling, additive, idempotent).

Implements P1 of specs/mifitness-import.md.

Principles (non-negotiable, from the spec):

* **Health Connect leads.**  The export fills gaps only; it never replaces a
  value we already hold.
* **Never overwrite.**  No existing row is modified, retagged or deleted.
  Additive only.
* **Idempotent.**  Every derived ``source_record_id`` is deterministic
  (``mf:<Sid>:<Key>:<Time>``) so a re-run inserts nothing new.
* **Heart rate is the exception.**  ``heart_rate`` is a *sample* metric and is
  imported by union, not gap-fill, because the user explicitly does not want
  any HR input discarded.

The export ZIP contains two CSVs we consume:

* ``*_hlth_center_fitness_data.csv``            — the raw sample stream
  (``Uid,Sid,Key,Time,Value,UpdateTime``).
* ``*_hlth_center_aggregated_fitness_data.csv`` — the daily aggregates
  (``Uid,Sid,Tag,Key,Time,Value,UpdateTime``); ``Time`` is the 00:00-UTC day
  anchor, so the UTC date equals the local date.

The reading path is streaming (the decompressed raw CSV is ~394 MB); nothing
larger than one batch is held in memory at a time.  The archive password is
supplied per run and is never written to any file, log line or DB row.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import insert, select

from drhiro_api.models import DailyAggregate, Measurement, User
from drhiro_schema.metrics import MetricType

SOURCE_PROVIDER = "mifitness_export"
SOURCE_DEVICE_ID = "mifitness-export"
RECORD_ID_PREFIX = "mf"

# Raw keys that are pure sensor telemetry — never imported (spec §4).
TELEMETRY_KEYS = frozenset({"light_sensitivity_value", "body_momentum"})

# Raw keys that are counted but skipped per-sample: the daily aggregate above
# carries the day (spec §4).
SKIP_SAMPLE_KEYS = frozenset(
    {"calories", "steps", "stress", "intensity", "valid_stand", "vitality"}
)

# Raw keys that map to a measurement row.
IMPORT_KEYS = frozenset(
    {
        "heart_rate",
        "single_heart_rate",
        "resting_heart_rate",
        "spo2",
        "single_spo2",
        "weight",
        "sleep",
    }
)

# Daily-aggregate keys we map to `daily_aggregates.metric_type` (spec §5).
# `weight` carries no value in the export and is skipped.
DAILY_SKIP_KEYS = frozenset({"weight"})

INSERT_BATCH = 5_000

_RAW_SUFFIX = "hlth_center_fitness_data.csv"
_DAILY_SUFFIX = "aggregated_fitness_data.csv"


# ──────────────────────────────────────────────────────────────────────────────
# Mapping helpers
# ──────────────────────────────────────────────────────────────────────────────


def _utc_from_unix(ts: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _local_date_from_unix(ts: Any) -> str | None:
    dt = _utc_from_unix(ts)
    return dt.date().isoformat() if dt else None


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def map_raw_sample(key: str, value: dict, time_unix: Any) -> dict | None:
    """Map one raw-stream row to a measurement spec, or None if not imported.

    Returns ``{"metric_type", "value_json", "unit", "recording_method",
    "end_at"}``.
    """
    if key == "heart_rate":
        bpm = value.get("bpm")
        if bpm is None:
            return None
        return {
            "metric_type": MetricType.HEART_RATE,
            "value_json": {"bpm": int(round(bpm))},
            "unit": "bpm",
            "recording_method": "automatic",
            "end_at": None,
        }
    if key == "single_heart_rate":
        bpm = value.get("bpm")
        if bpm is None:
            return None
        # Manual spot reading — the user's requirement is that no HR input is
        # discarded, so this is union-imported like continuous HR.
        return {
            "metric_type": MetricType.HEART_RATE,
            "value_json": {"bpm": int(round(bpm))},
            "unit": "bpm",
            "recording_method": "manual",
            "end_at": None,
        }
    if key == "resting_heart_rate":
        bpm = value.get("bpm")
        if bpm is None:
            return None
        return {
            "metric_type": MetricType.RESTING_HEART_RATE,
            "value_json": {"bpm": int(round(bpm))},
            "unit": "bpm",
            "recording_method": "automatic",
            "end_at": None,
        }
    if key in ("spo2", "single_spo2"):
        v = value.get("spo2")
        if v is None:
            return None
        return {
            "metric_type": MetricType.SPO2,
            "value_json": {"spo2": int(round(v))},
            "unit": "%",
            "recording_method": "automatic",
            "end_at": None,
        }
    if key == "weight":
        w = value.get("weight")
        if w is None:
            return None
        vj = {"weight_kg": float(w)}
        if value.get("bmi") is not None:
            vj["bmi"] = value["bmi"]
        return {
            "metric_type": MetricType.WEIGHT,
            "value_json": vj,
            "unit": "kg",
            "recording_method": "manual",
            "end_at": None,
        }
    if key == "sleep":
        duration = value.get("duration")
        vj = _drop_none(
            {
                "duration_min": duration,
                "deep_min": value.get("sleep_deep_duration"),
                "light_min": value.get("sleep_light_duration"),
                "rem_min": value.get("sleep_rem_duration"),
                "awake_min": value.get("sleep_awake_duration"),
                "avg_hr": value.get("avg_hr"),
                "max_hr": value.get("max_hr"),
                "min_hr": value.get("min_hr"),
                "avg_breath": value.get("avg_breath"),
                "bedtime": value.get("bedtime"),
                "wake_up_time": value.get("wake_up_time"),
            }
        )
        end_at = None
        start = _utc_from_unix(time_unix)
        if start is not None and duration:
            end_at = start + timedelta(minutes=int(duration))
        return {
            "metric_type": MetricType.SLEEP,
            "value_json": vj,
            "unit": "min",
            "recording_method": "automatic",
            "end_at": end_at,
        }
    return None


def _daily_sleep(value: dict) -> dict:
    return _drop_none(
        {
            "duration_min": value.get("total_duration"),
            "deep_min": value.get("sleep_deep_duration"),
            "light_min": value.get("sleep_light_duration"),
            "score": value.get("sleep_score"),
            "avg_hr": value.get("avg_hr"),
            "awake_min": value.get("sleep_awake_duration"),
        }
    )


def _daily_hr(value: dict) -> dict:
    return _drop_none(
        {
            "avg_bpm": value.get("avg_hr"),
            "max_bpm": value.get("max_hr"),
            "min_bpm": value.get("min_hr"),
            "zones": _drop_none(
                {
                    "warm_up": value.get("warm_up_hr_zone_duration"),
                    "fat_burning": value.get("fat_burning_hr_zone_duration"),
                    "aerobic": value.get("aerobic_hr_zone_duration"),
                    "anaerobic": value.get("anaerobic_hr_zone_duration"),
                    "extreme": value.get("extreme_hr_zone_duration"),
                }
            ),
            "abnormal_count": value.get("abnormal_hr_count"),
        }
    )


def _daily_stress(value: dict) -> dict:
    return _drop_none(
        {
            "avg": value.get("avg_stress"),
            "max": value.get("max_stress"),
            "min": value.get("min_stress"),
            "scale": value.get("stress_scale"),
        }
    )


def _daily_goal(value: dict) -> dict:
    items = []
    for it in value.get("goal_items") or []:
        items.append(
            _drop_none(
                {
                    "field": it.get("field"),
                    "achieved": it.get("achieved_value"),
                    "target": it.get("target_value"),
                }
            )
        )
    return {"items": items}


def map_daily_aggregate(key: str, value: dict) -> tuple[str, dict] | None:
    """Map one daily-aggregate row to ``(metric_type, value_json)``."""
    if key == "steps":
        return MetricType.STEPS, _drop_none(
            {
                "steps": value.get("steps"),
                "distance_m": value.get("distance"),
                "calories": value.get("calories"),
            }
        )
    if key == "calories":
        return MetricType.ACTIVE_CALORIES, _drop_none({"kcal": value.get("calories")})
    if key == "sleep":
        return MetricType.SLEEP, _daily_sleep(value)
    if key == "intensity":
        return MetricType.ACTIVE_MINUTES, _drop_none({"minutes": value.get("duration")})
    if key == "heart_rate":
        return MetricType.HEART_RATE, _daily_hr(value)
    if key == "stress":
        return MetricType.STRESS, _daily_stress(value)
    if key == "valid_stand":
        return MetricType.STAND_HOURS, _drop_none({"count": value.get("count")})
    if key == "goal":
        return MetricType.GOAL_ACHIEVEMENT, _daily_goal(value)
    if key == "spo2":
        return MetricType.SPO2, _drop_none({"avg": value.get("avg_spo2")})
    return None


# ──────────────────────────────────────────────────────────────────────────────
# ZIP reader
# ──────────────────────────────────────────────────────────────────────────────


def _open_zip(source, password: str) -> zipfile.ZipFile:
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    zf = zipfile.ZipFile(source)
    # Detect outright wrong passwords early (the real archive is ZipCrypto).
    if password:
        zf.setpassword(password.encode("utf-8"))
    return zf


def _find_entry(zf: zipfile.ZipFile, suffix: str) -> str:
    matches = [n for n in zf.namelist() if n.endswith(suffix) and not n.endswith("/")]
    if not matches:
        raise ValueError(f"ZIP does not contain an entry ending with {suffix!r}")
    # Prefer the shortest name (avoids the non-aggregated match when both end
    # the same way).
    return sorted(matches, key=len)[0]


def _iter_csv(zf: zipfile.ZipFile, name: str):
    # ZipCrypto raises RuntimeError on the first read when the password is
    # wrong; surface that as a ValueError for callers.
    try:
        with zf.open(name) as fh:
            yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))
    except RuntimeError as exc:  # bad password
        raise ValueError(f"Could not read {name!r} from the ZIP: {exc}") from exc


# ──────────────────────────────────────────────────────────────────────────────
# Import
# ──────────────────────────────────────────────────────────────────────────────


def _blank_report(dry_run: bool, user_id: str) -> dict:
    return {
        "ok": True,
        "dry_run": dry_run,
        "source_provider": SOURCE_PROVIDER,
        "user_id": user_id,
        "raw": {
            "rows_read": 0,
            "imported": 0,
            "skipped_telemetry": 0,
            "skipped_per_sample": 0,
            "inserted": 0,
            "duplicates": 0,
            "errors": 0,
            "by_key": {},
        },
        "daily": {
            "rows_read": 0,
            "mapped": 0,
            "skipped": 0,
            "distinct_keys": 0,
            "revisions_collapsed": 0,
            "inserted": 0,
            "skipped_gapfill": 0,
            "errors": 0,
            "by_key": {},
        },
    }


def run_import(
    db,
    *,
    user: User,
    source,
    password: str,
    dry_run: bool = False,
    import_filename: str | None = None,
) -> dict:
    """Import a Mi Fitness export ZIP for ``user``.

    ``source`` is a path, bytes or file-like of the ZIP.  When ``dry_run`` is
    true NOTHING is written to the database.
    """
    started = time.monotonic()
    uid = user.id
    report = _blank_report(dry_run, str(uid))
    raw = report["raw"]
    daily = report["daily"]

    zf = _open_zip(source, password)
    raw_name = _find_entry(zf, _RAW_SUFFIX)
    daily_name = _find_entry(zf, _DAILY_SUFFIX)

    # Pre-load the idempotency sets.
    existing_sample_ids = set(
        db.execute(
            select(Measurement.source_record_id).where(
                Measurement.user_id == uid,
                Measurement.source_provider == SOURCE_PROVIDER,
            )
        ).scalars()
    )
    existing_daily_keys = set(
        db.execute(
            select(DailyAggregate.local_date, DailyAggregate.metric_type).where(
                DailyAggregate.user_id == uid,
            )
        ).all()
    )
    seen_sample_ids: set[str] = set()

    # ── raw sample stream ────────────────────────────────────────────────────
    pending: list[dict] = []

    def _flush_samples():
        if not pending or dry_run:
            pending.clear()
            return
        db.execute(insert(Measurement), list(pending))
        db.commit()
        pending.clear()

    for row in _iter_csv(zf, raw_name):
        raw["rows_read"] += 1
        key = (row.get("Key") or "").strip()
        raw["by_key"][key] = raw["by_key"].get(key, 0) + 1

        if key in TELEMETRY_KEYS:
            raw["skipped_telemetry"] += 1
            continue
        if key in SKIP_SAMPLE_KEYS or key not in IMPORT_KEYS:
            raw["skipped_per_sample"] += 1
            continue

        raw["imported"] += 1
        try:
            value = json.loads(row.get("Value") or "{}")
        except (ValueError, TypeError):
            raw["errors"] += 1
            continue

        mapped = map_raw_sample(key, value, row.get("Time"))
        start_at = _utc_from_unix(row.get("Time"))
        if mapped is None or start_at is None:
            raw["errors"] += 1
            continue

        sid = (row.get("Sid") or "").strip()
        record_id = f"{RECORD_ID_PREFIX}:{sid}:{key}:{row.get('Time')}"
        if record_id in existing_sample_ids or record_id in seen_sample_ids:
            raw["duplicates"] += 1
            continue

        seen_sample_ids.add(record_id)
        raw["inserted"] += 1
        now = datetime.now(timezone.utc)
        pending.append(
            {
                "id": uuid.uuid4(),
                "user_id": uid,
                "metric_type": str(mapped["metric_type"]),
                "start_at": start_at,
                "end_at": mapped["end_at"],
                "value_json": mapped["value_json"],
                "unit": mapped["unit"],
                "source_provider": SOURCE_PROVIDER,
                "source_record_id": record_id,
                "source_device_id": SOURCE_DEVICE_ID,
                "recording_method": mapped["recording_method"],
                "confidence": 1.0,
                "metadata_json": {
                    "import_file": import_filename,
                    "export_key": key,
                },
                "created_at": now,
                "updated_at": now,
            }
        )
        if len(pending) >= INSERT_BATCH:
            _flush_samples()
    _flush_samples()

    # ── daily aggregates (gap-fill) ──────────────────────────────────────────
    #
    # The export contains several *revisions* of the same (local_date, key) —
    # a mid-day partial write and a later final one (UpdateTime differs).  The
    # unique key is (user_id, local_date, metric_type), so we keep ONE row per
    # day/metric: the revision with the greatest UpdateTime.  Only days that no
    # other source already owns are inserted (Health Connect leads).
    daily_best: dict[tuple[str, str], tuple[int, dict]] = {}

    def _flush_daily():
        if not daily_pending or dry_run:
            daily_pending.clear()
            return
        db.execute(insert(DailyAggregate), list(daily_pending))
        db.commit()
        daily_pending.clear()

    for row in _iter_csv(zf, daily_name):
        daily["rows_read"] += 1
        key = (row.get("Key") or "").strip()
        daily["by_key"][key] = daily["by_key"].get(key, 0) + 1

        if key in DAILY_SKIP_KEYS:
            daily["skipped"] += 1
            continue

        try:
            value = json.loads(row.get("Value") or "{}")
        except (ValueError, TypeError):
            daily["errors"] += 1
            continue

        mapped = map_daily_aggregate(key, value)
        local_date = _local_date_from_unix(row.get("Time"))
        if mapped is None or local_date is None:
            daily["errors"] += 1
            continue

        metric_type, value_json = mapped
        daily["mapped"] += 1
        mt = str(metric_type)
        try:
            update_time = int(row.get("UpdateTime") or 0)
        except (TypeError, ValueError):
            update_time = 0
        payload = {
            "local_date": local_date,
            "metric_type": mt,
            "value_json": value_json,
            "source_record_id": f"{RECORD_ID_PREFIX}:{row.get('Sid')}:{key}:{row.get('Time')}",
        }
        prev = daily_best.get((local_date, mt))
        if prev is None or update_time > prev[0]:
            daily_best[(local_date, mt)] = (update_time, payload)

    daily["distinct_keys"] = len(daily_best)
    daily["revisions_collapsed"] = daily["mapped"] - len(daily_best)

    # Day range covered by the export's daily aggregates (additive report field
    # for the Telegram bot's summary). Purely derived; nothing is written.
    if daily_best:
        _days = sorted({str(k[0]) for k in daily_best})
        report["day_range"] = {"from": _days[0], "to": _days[-1]}

    daily_pending: list[dict] = []
    now = datetime.now(timezone.utc)
    for gap_key in sorted(daily_best):
        if gap_key in existing_daily_keys:
            daily["skipped_gapfill"] += 1
            continue
        payload = daily_best[gap_key][1]
        daily["inserted"] += 1
        daily_pending.append(
            {
                "id": uuid.uuid4(),
                "user_id": uid,
                "local_date": payload["local_date"],
                "metric_type": payload["metric_type"],
                "value_json": payload["value_json"],
                "coverage_score": None,
                "source_count": 1,
                "source_provider": SOURCE_PROVIDER,
                "source_record_id": payload["source_record_id"],
                "computed_at": now,
                "created_at": now,
                "updated_at": now,
            }
        )
        if len(daily_pending) >= INSERT_BATCH:
            _flush_daily()
    _flush_daily()

    report["elapsed_s"] = round(time.monotonic() - started, 2)
    report["import_filename"] = import_filename
    return report


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def _read_password(args: argparse.Namespace) -> str:
    if args.password_file:
        with open(args.password_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    if args.password_env:
        val = os.environ.get(args.password_env)
        if not val:
            raise SystemExit(f"Environment variable {args.password_env!r} is empty")
        return val
    if args.password:
        return args.password
    raise SystemExit("No password supplied (use --password-file or --password-env)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import a Mi Fitness manual-export ZIP (gap-filling)."
    )
    parser.add_argument("--zip", required=True, help="Path to the export ZIP")
    parser.add_argument("--user-id", required=True, help="Target user UUID")
    parser.add_argument("--password-file", help="File containing the archive password")
    parser.add_argument(
        "--password-env",
        help="Name of an env var holding the archive password (safer than argv)",
    )
    parser.add_argument("--password", help="Password (discouraged: visible in ps)")
    parser.add_argument("--dry-run", action="store_true", help="Reconcile only; write nothing")
    parser.add_argument("--report-out", help="Write the JSON report to this path")
    args = parser.parse_args(argv)

    password = _read_password(args)

    from drhiro_api.db import SessionLocal

    db = SessionLocal()
    try:
        user = db.get(User, uuid.UUID(args.user_id))
        if user is None:
            raise SystemExit(f"User {args.user_id} not found")
        report = run_import(
            db,
            user=user,
            source=args.zip,
            password=password,
            dry_run=args.dry_run,
            import_filename=os.path.basename(args.zip),
        )
    finally:
        db.close()

    out = json.dumps(report, indent=2, default=str)
    if args.report_out:
        with open(args.report_out, "w", encoding="utf-8") as fh:
            fh.write(out)
    print(out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())