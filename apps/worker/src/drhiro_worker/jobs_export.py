"""Worker job: personal data export bundle (0.10.x wave 3, spec §4.1).

build_export(file_id) streams every owned row into a zip:

  manifest.json          {schema, app_version, alembic_revision, exported_at,
                          user_tz, files: [{path, sha256, rows}]}
  json/<table>.ndjson    one object per row (ISO timestamps, UUIDs as strings)
  csv/<table>.csv        same columns flattened (JSON columns as JSON strings)
  files/scans/...        medical scans (paths mirror source_image_path)
  README.txt

Rows are streamed with yield_per; per-table buffers are flushed into the zip
once per table. DASHBOARD-ONLY per standing amendment A2: the bundle is never
Telegram-delivered; the user downloads it from the Files surface.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import uuid
import zipfile
from datetime import date, datetime, timezone

from sqlalchemy import text

from drhiro_api.config import get_settings
from drhiro_api.db import SessionLocal
from drhiro_api.models import (
    Activity,
    Alert,
    AnalysisRun,
    AuditEvent,
    ConsentGrant,
    DailyAggregate,
    DeviceConnection,
    DoseEvent,
    ExternalIdentity,
    FoodCatalogItem,
    FoodResolutionRule,
    GeneratedFile,
    Goal,
    HealthProfile,
    Meal,
    MealItem,
    MedicalEncounter,
    MedicalRecord,
    MedicationItem,
    MedicationItemEvent,
    Measurement,
    Reminder,
    ReminderOccurrence,
    User,
)
from drhiro_worker import files as gfiles

EXPORT_SCHEMA = "drhiro-export/1"


def _jsonable(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, bytes):
        return None
    return v


def _row_dict(row) -> dict:
    out = {}
    for c in row.__table__.columns:
        v = getattr(row, c.name)
        if isinstance(v, (dict, list)):
            out[c.name] = json.dumps(v, default=_jsonable, sort_keys=True)
        else:
            out[c.name] = _jsonable(v)
    return out


def _queries(db, uid: uuid.UUID) -> list[tuple[str, object, list[str]]]:
    """(table, query, drop_columns) for every exported table, spec §4.1.
    external_identities exports provider + subject only; generated_files and
    consent_grants are metadata-level; audit_events by user_id_affected."""
    return [
        ("users", db.query(User).filter(User.id == uid), []),
        ("external_identities", db.query(ExternalIdentity).filter(ExternalIdentity.user_id == uid), []),
        ("device_connections", db.query(DeviceConnection).filter(DeviceConnection.user_id == uid), []),
        ("measurements", db.query(Measurement).filter(Measurement.user_id == uid), []),
        ("daily_aggregates", db.query(DailyAggregate).filter(DailyAggregate.user_id == uid), []),
        ("meals", db.query(Meal).filter(Meal.user_id == uid), []),
        ("meal_items", db.query(MealItem).join(Meal, MealItem.meal_id == Meal.id).filter(Meal.user_id == uid), []),
        ("activities", db.query(Activity).filter(Activity.user_id == uid), []),
        ("reminders", db.query(Reminder).filter(Reminder.user_id == uid), []),
        ("reminder_occurrences", db.query(ReminderOccurrence).join(Reminder, ReminderOccurrence.reminder_id == Reminder.id).filter(Reminder.user_id == uid), []),
        ("goals", db.query(Goal).filter(Goal.user_id == uid), []),
        ("alerts", db.query(Alert).filter(Alert.user_id == uid), []),
        ("food_resolution_rules", db.query(FoodResolutionRule).filter(FoodResolutionRule.user_id == uid), []),
        ("food_catalog_items", db.query(FoodCatalogItem).filter(FoodCatalogItem.user_id == uid), []),
        ("medical_encounters", db.query(MedicalEncounter).filter(MedicalEncounter.user_id == uid), []),
        ("medical_records", db.query(MedicalRecord).filter(MedicalRecord.user_id == uid), []),
        ("medication_items", db.query(MedicationItem).filter(MedicationItem.user_id == uid), []),
        ("medication_item_events", db.query(MedicationItemEvent).filter(MedicationItemEvent.user_id == uid), []),
        ("dose_events", db.query(DoseEvent).filter(DoseEvent.user_id == uid), []),
        ("health_profiles", db.query(HealthProfile).filter(HealthProfile.user_id == uid), []),
        ("analysis_runs", db.query(AnalysisRun).filter(AnalysisRun.user_id == uid), []),
        ("generated_files", db.query(GeneratedFile).filter(GeneratedFile.user_id == uid), []),
        ("consent_grants", db.query(ConsentGrant).filter(ConsentGrant.grantor_user_id == uid), []),
        ("audit_events", db.query(AuditEvent).filter(AuditEvent.user_id_affected == uid), []),
    ]


def _flush_table(db, uid, zf, table: str, q, drop: list[str], fmts: list[str], manifest: list) -> None:
    nd = io.StringIO()
    cs = io.StringIO()
    writer = None
    cols = None
    n = 0
    for row in q.yield_per(2000):
        d = _row_dict(row)
        for col in drop:
            d.pop(col, None)
        if "json" in fmts:
            nd.write(json.dumps(d, sort_keys=True, default=str) + "\n")
        if "csv" in fmts:
            if writer is None:
                cols = list(d.keys())
                writer = csv.DictWriter(cs, fieldnames=cols, extrasaction="ignore")
                writer.writeheader()
            writer.writerow({k: ("" if v is None else v) for k, v in d.items()})
        n += 1
    if n == 0:
        # Always emit a file for every table, even when empty (importers can
        # rely on presence): empty .ndjson + header-only .csv.
        model = q.column_descriptions[0]["entity"]
        cols = [c.name for c in model.__table__.columns]
        if "json" in fmts:
            data = b""
            zf.writestr(f"json/{table}.ndjson", data)
            manifest.append({"path": f"json/{table}.ndjson", "sha256": hashlib.sha256(data).hexdigest(), "rows": 0})
        if "csv" in fmts:
            writer = csv.DictWriter(cs, fieldnames=cols)
            writer.writeheader()
            data = cs.getvalue().encode()
            zf.writestr(f"csv/{table}.csv", data)
            manifest.append({"path": f"csv/{table}.csv", "sha256": hashlib.sha256(data).hexdigest(), "rows": 0})
        return
    if "json" in fmts:
        data = nd.getvalue().encode()
        zf.writestr(f"json/{table}.ndjson", data)
        manifest.append({"path": f"json/{table}.ndjson", "sha256": hashlib.sha256(data).hexdigest(), "rows": n})
    if "csv" in fmts:
        data = cs.getvalue().encode()
        zf.writestr(f"csv/{table}.csv", data)
        manifest.append({"path": f"csv/{table}.csv", "sha256": hashlib.sha256(data).hexdigest(), "rows": n})


def _scan_files(db, uid: uuid.UUID) -> list[tuple[str, str]]:
    """[(abs_path, zip_arcname)] for the user's scans."""
    from drhiro_api.routers.medical import resolve_scan_file

    out = []
    user = db.get(User, uid)
    for rec in db.query(MedicalRecord).filter(
        MedicalRecord.user_id == uid, MedicalRecord.source_image_path.isnot(None)
    ).yield_per(500):
        abs_path = resolve_scan_file(user, rec.source_image_path)
        if abs_path:
            out.append((abs_path, os.path.join("files", rec.source_image_path)))
    return out


def build_export(file_id: str) -> dict:
    db = SessionLocal()
    try:
        gfile = db.get(GeneratedFile, uuid.UUID(file_id))
        if not gfile or gfile.status not in ("queued", "failed"):
            return {"skipped": gfile and gfile.status}
        uid = gfile.user_id
        params = gfile.params_json or {}
        fmts = params.get("format") or ["json"]
        include_files = params.get("include_files", True)
        settings = get_settings()

        gfiles.begin(file_id)

        rev = None
        try:
            rev = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
        except Exception:
            rev = None

        manifest: list[dict] = []
        tmp_path = os.path.join(
            settings.generated_files_dir, "generated", str(uid), f".export-{uuid.uuid4()}.tmp"
        )
        os.makedirs(os.path.dirname(tmp_path), exist_ok=True)

        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for table, q, drop in _queries(db, uid):
                _flush_table(db, uid, zf, table, q, drop, fmts, manifest)

            if include_files:
                for abs_path, arc in _scan_files(db, uid):
                    with open(abs_path, "rb") as fh:
                        data = fh.read()
                    zf.writestr(arc, data)
                    manifest.append({"path": arc, "sha256": hashlib.sha256(data).hexdigest(), "rows": None})

            user = db.get(User, uid)
            manifest_doc = {
                "schema": EXPORT_SCHEMA,
                "app_version": settings.version,
                "alembic_revision": rev,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "user_tz": user.timezone if user else "UTC",
                "files": manifest,
            }
            zf.writestr("manifest.json", json.dumps(manifest_doc, indent=2, sort_keys=True))
            zf.writestr("README.txt", "\n".join([
                "drHiro personal data export",
                "",
                "json/<table>.ndjson — one JSON object per database row (ISO timestamps,",
                "                      UUIDs as strings).",
                "csv/<table>.csv     — the same rows flattened (JSON columns as JSON strings).",
                "files/scans/...     — stored medical document scans; paths mirror",
                "                      medical_records.source_image_path.",
                "manifest.json       — app version, DB schema revision, per-file checksums.",
                "",
                "Generated by drHiro. Self-recorded data; contains no diagnosis.",
            ]))

        with open(tmp_path, "rb") as fh:
            data = fh.read()
        filename = f"drhiro-export-{datetime.now(timezone.utc).strftime('%Y%m%d')}.zip"
        rel = gfiles.write_bytes(file_id, data, filename, "application/zip")
        try:
            os.remove(tmp_path)
        except OSError:
            pass

        db.close()
        db = SessionLocal()
        from drhiro_api.security import audit

        audit(db, "system", None, uid, "privacy.export_bundle.built", "generated_file", file_id,
              {"size_bytes": len(data)})
        db.commit()
        return {"status": "ready", "rel_path": rel, "size_bytes": len(data)}
    except Exception as e:
        gfiles.fail(file_id, repr(e)[:2000])
        raise
    finally:
        db.close()
