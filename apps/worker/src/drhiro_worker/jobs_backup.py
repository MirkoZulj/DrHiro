"""Worker jobs: system backup + restore test (0.10.x wave 3, spec §4.3).

run_backup / run_restore_test shell out to infra/backup/backup.sh and
restore_test.sh (the same scripts the host cron uses — logic lives in one
place). Results are recorded on the GeneratedFile row:
- run_backup:        kind=system_backup, ready when the script prints
                     "[backup] done: <path>"; the produced file stays in the
                     backups_dir (NOT copied into generated_files_dir — it may
                     be multi-GB and GPG-encrypted).
- run_restore_test:  kind=restore_test, ready when the script exits 0.
check_backup_staleness (periodic, spec §2.4) is a no-op alert hook here: the
Telegram admin ping is delivered by the notification layer, so this job only
logs — the admin sees staleness on GET /backups/status (host_cron_detected,
last_backup age).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from datetime import datetime, timezone

from drhiro_api.config import get_settings
from drhiro_api.db import SessionLocal
from drhiro_api.models import GeneratedFile

BACKUP_SCRIPT = "/app/infra/backup/backup.sh"
RESTORE_SCRIPT = "/app/infra/backup/restore_test.sh"
TIMEOUT = 1800


def _script(name: str) -> str:
    """Deployed script path; the repo copy when running outside the image."""
    deployed = f"/app/infra/backup/{name}"
    if os.path.exists(deployed):
        return deployed
    return os.path.join(os.getcwd(), "infra", "backup", name)


def _fail(file_id: str, err: str) -> None:
    db = SessionLocal()
    try:
        f = db.get(GeneratedFile, uuid.UUID(file_id))
        if f:
            f.status, f.error = "failed", str(err)[:2000]
            db.commit()
    finally:
        db.close()


def _finish(file_id: str, *, rel_path: str | None, size: int | None,
            sha: str | None, params: dict) -> None:
    db = SessionLocal()
    try:
        f = db.get(GeneratedFile, uuid.UUID(file_id))
        if f:
            f.status = "ready"
            f.rel_path = rel_path
            f.size_bytes = size
            f.sha256 = sha
            f.params_json = {**(f.params_json or {}), **params}
            db.commit()
    finally:
        db.close()


def run_backup(file_id: str) -> dict:
    settings = get_settings()
    script = _script("backup.sh")
    if not os.path.exists(script):
        _fail(file_id, f"backup script not found at {script} (only deployed in the worker image)")
        return {"status": "failed", "reason": "script missing"}
    env = dict(os.environ)
    if settings.backup_gpg_recipient:
        env["DRHIRO_GPG_RECIPIENT"] = settings.backup_gpg_recipient
    try:
        proc = subprocess.run([script], env=env, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        _fail(file_id, "backup timed out after 1800s")
        return {"status": "failed", "reason": "timeout"}
    done_line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("[backup] done:")), None)
    if proc.returncode != 0 or not done_line:
        _fail(file_id, f"backup.sh exit={proc.returncode}: {(proc.stderr or proc.stdout)[-1500:]}")
        return {"status": "failed", "reason": "script failed"}
    produced = done_line.split("[backup] done:", 1)[1].strip()
    size = sha = None
    try:
        size = os.path.getsize(produced)
        h = hashlib.sha256()
        with open(produced, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        sha = h.hexdigest()
    except OSError:
        pass
    settings_dir = os.path.realpath(settings.backups_dir)
    rel = os.path.relpath(os.path.realpath(produced), settings_dir)
    encrypted = produced.endswith(".gpg")
    _finish(file_id, rel_path=rel, size=size, sha=sha,
            params={"encrypted": encrypted, "script": "backup.sh"})
    return {"status": "ready", "path": produced, "encrypted": encrypted}


def run_restore_test(file_id: str) -> dict:
    settings = get_settings()
    script = _script("restore_test.sh")
    if not os.path.exists(script):
        _fail(file_id, f"restore_test script not found at {script} (only deployed in the worker image)")
        return {"status": "failed", "reason": "script missing"}
    env = dict(os.environ)
    try:
        proc = subprocess.run([script], env=env, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        _fail(file_id, "restore test timed out after 1800s")
        return {"status": "failed", "reason": "timeout"}
    tail = "\n".join((proc.stdout or "").splitlines()[-30:])
    if proc.returncode != 0:
        _finish(file_id, rel_path=None, size=None, sha=None,
                params={"passed": False, "summary": f"exit={proc.returncode}", "log": tail[-2000:]})
        # mark failed (a failed restore test is a failed artefact)
        db = SessionLocal()
        try:
            f = db.get(GeneratedFile, uuid.UUID(file_id))
            if f:
                f.status = "failed"
                db.commit()
        finally:
            db.close()
        return {"status": "failed", "exit": proc.returncode}
    _finish(file_id, rel_path=None, size=None, sha=None,
            params={"passed": True, "summary": (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else "ok",
                    "log": tail[-2000:]})
    return {"status": "ready", "passed": True}


def check_backup_staleness() -> dict:
    """Periodic staleness probe (spec §2.4). Returns a report dict; alerting to
    the admin happens in the notification layer / dashboard status endpoint."""
    settings = get_settings()
    newest = None
    try:
        for name in os.listdir(settings.backups_dir):
            if name.startswith("drhiro-") and ".sql.gz" in name:
                mtime = datetime.fromtimestamp(
                    os.path.getmtime(os.path.join(settings.backups_dir, name)), tz=timezone.utc
                )
                newest = max(newest or mtime, mtime)
    except OSError:
        pass
    stale = newest is None or (datetime.now(timezone.utc) - newest).total_seconds() > 26 * 3600
    return {"newest_backup": newest.isoformat() if newest else None, "stale": stale}
