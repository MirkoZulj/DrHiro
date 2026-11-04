"""System backup admin API (0.10.x wave 3, spec §4.3).

All routes require the authorized admin (_require_admin pattern from
routers/settings.py). The worker runs infra/backup/backup.sh and
restore_test.sh; this router creates GeneratedFile rows and enqueues the jobs.
Redis lock drhiro:backup:lock gives a 409 on concurrent runs.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from drhiro_api.config import get_settings
from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import GeneratedFile, User
from drhiro_api.security import audit
from drhiro_api.routers.settings import _authorized

router = APIRouter(prefix="/backups", tags=["backups"])

BACKUP_LOCK = "drhiro:backup:lock"


def _require_admin(db: Session, user: User) -> None:
    if not _authorized(db, user):
        raise HTTPException(status_code=403, detail="Only the authorized admin may manage backups.")


def _gf_out(gf: GeneratedFile) -> dict:
    return {
        "id": str(gf.id), "kind": gf.kind, "status": gf.status, "filename": gf.filename,
        "mime": gf.mime, "size_bytes": gf.size_bytes, "sha256": gf.sha256,
        "created_at": gf.created_at, "expires_at": gf.expires_at, "error": gf.error,
        "params": gf.params_json,
    }


def _lock_redis():
    import redis as redis_mod

    r = redis_mod.Redis.from_url(get_settings().redis_url)
    if not r.set(BACKUP_LOCK, str(uuid.uuid4()), nx=True, ex=3600):
        raise HTTPException(status_code=409, detail="A backup job is already running")
    return r


def _enqueue_locked(db: Session, user: User, kind: str, job: str, fn: str):
    r = _lock_redis()
    try:
        gf = GeneratedFile(user_id=user.id, kind=kind, status="queued",
                           params_json={"started_by": str(user.id)})
        db.add(gf)
        db.flush()
        from drhiro_api.services.task_queue import enqueue

        job_id = enqueue("drhiro", fn, str(gf.id))
        if job_id is None:
            gf.status, gf.error = "failed", "queue unavailable — job not enqueued"
            db.commit()
            return gf
        gf.job_id = job_id
        audit(db, "user", str(user.id), user.id, "backup.run", "generated_file", str(gf.id), {"kind": kind})
        db.commit()
        db.refresh(gf)
        return gf
    finally:
        try:
            r.delete(BACKUP_LOCK)
        except Exception:
            pass


@router.get("/status")
def backup_status(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(db, user)
    settings = get_settings()
    last_backup = last_restore = None
    files = (
        db.query(GeneratedFile)
        .filter(GeneratedFile.kind.in_(("system_backup", "restore_test")))
        .order_by(GeneratedFile.created_at.desc())
        .limit(50)
        .all()
    )
    for f in files:
        if f.kind == "system_backup" and last_backup is None and f.status == "ready":
            last_backup = {
                "at": f.created_at, "size_bytes": f.size_bytes,
                "encrypted": bool((f.params_json or {}).get("encrypted")),
                "path_name": f.rel_path,
            }
        if f.kind == "restore_test" and last_restore is None:
            last_restore = {
                "at": f.created_at,
                "passed": f.status == "ready",
                "summary": (f.params_json or {}).get("summary"),
            }
    # Host cron detection: newest drhiro-*.sql.gz* in backups_dir newer than the
    # newest worker-run backup means the host cron is active.
    host_cron_detected = False
    try:
        newest_host = 0.0
        newest_worker_ts = None
        for name in os.listdir(settings.backups_dir):
            if name.startswith("drhiro-") and (".sql.gz" in name):
                newest_host = max(newest_host, os.path.getmtime(os.path.join(settings.backups_dir, name)))
        for f in files:
            if f.kind == "system_backup" and f.created_at:
                ts = f.created_at.timestamp() if f.created_at.tzinfo else f.created_at.timestamp()
                newest_worker_ts = max(newest_worker_ts or 0, ts)
        host_cron_detected = bool(newest_host and newest_host > (newest_worker_ts or 0))
    except OSError:
        pass
    offsite = bool(os.environ.get("DRHIRO_OFFSITE_REMOTE"))
    return {
        "last_backup": last_backup,
        "last_restore_test": last_restore,
        "retention": {"daily": 14},
        "host_cron_detected": host_cron_detected,
        "offsite": "configured" if offsite else "not configured",
    }


@router.post("/run")
def run_backup(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(db, user)
    gf = _enqueue_locked(db, user, "system_backup", "backup",
                         "drhiro_worker.jobs_backup.run_backup")
    return {"file": _gf_out(gf)}


@router.post("/restore-test")
def run_restore_test(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(db, user)
    gf = _enqueue_locked(db, user, "restore_test", "backup",
                         "drhiro_worker.jobs_backup.run_restore_test")
    return {"file": _gf_out(gf)}


@router.get("")
def list_backups(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(db, user)
    files = (
        db.query(GeneratedFile)
        .filter(GeneratedFile.kind.in_(("system_backup", "restore_test")))
        .order_by(GeneratedFile.created_at.desc())
        .limit(100)
        .all()
    )
    return {"files": [_gf_out(f) for f in files], "count": len(files)}
