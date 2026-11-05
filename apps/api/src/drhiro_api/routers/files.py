"""Generated-file download/list/delete API (0.10.0 wave 3, spec §2.2).

Owner-only access to the user's own artefacts (PDFs, export bundles).
Files live under <generated_files_dir>/generated/<user_id>/<uuid>.<ext>;
system backups live under backups_dir and are served by routers/backups.py
for admins only. Expired files return 410.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from drhiro_api.config import get_settings
from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import GeneratedFile, User
from drhiro_api.security import audit

router = APIRouter(prefix="/files", tags=["files"])


def _file_out(f: GeneratedFile) -> dict:
    return {
        "id": str(f.id), "kind": f.kind, "status": f.status, "filename": f.filename,
        "mime": f.mime, "size_bytes": f.size_bytes, "sha256": f.sha256,
        "params": f.params_json, "created_at": f.created_at, "expires_at": f.expires_at,
        "error": f.error,
    }


def _get_own(db: Session, user: User, file_id: str) -> GeneratedFile:
    try:
        fid = uuid.UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="File not found")
    f = db.query(GeneratedFile).filter(GeneratedFile.id == fid, GeneratedFile.user_id == user.id).first()
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    return f


def resolve_generated_file(f: GeneratedFile) -> str:
    """Absolute path for a ready GeneratedFile, with realpath + traversal guard
    (same pattern as medical.resolve_scan_file). Raises 404/410."""
    settings = get_settings()
    if f.status == "expired" or (f.expires_at is not None and f.expires_at <= datetime.now(timezone.utc)):
        raise HTTPException(status_code=410, detail="File expired")
    if f.status != "ready" or not f.rel_path:
        raise HTTPException(status_code=404, detail="File not ready")
    if f.kind == "system_backup":
        base = os.path.realpath(settings.backups_dir)
    else:
        base = os.path.realpath(settings.generated_files_dir)
    abs_path = os.path.realpath(os.path.join(base, f.rel_path))
    if not abs_path.startswith(base + os.sep):
        raise HTTPException(status_code=404, detail="File not found")  # traversal guard
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File missing on disk")
    return abs_path


@router.get("")
def list_files(kind: str | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(GeneratedFile).filter(GeneratedFile.user_id == user.id)
    if kind:
        kinds = [k.strip() for k in kind.split(",") if k.strip()]
        q = q.filter(GeneratedFile.kind.in_(kinds))
    files = q.order_by(GeneratedFile.created_at.desc()).limit(200).all()
    return {"files": [_file_out(f) for f in files], "count": len(files)}


@router.get("/{file_id}")
def file_detail(file_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    f = _get_own(db, user, file_id)
    return {"file": _file_out(f)}


@router.get("/{file_id}/download")
def download(file_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    f = _get_own(db, user, file_id)
    path = resolve_generated_file(f)
    return FileResponse(path, media_type=f.mime or "application/octet-stream", filename=f.filename or "download")


@router.delete("/{file_id}")
def delete_file(file_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    f = _get_own(db, user, file_id)
    settings = get_settings()
    if f.rel_path and f.kind != "system_backup":
        base = os.path.realpath(settings.generated_files_dir)
        abs_path = os.path.realpath(os.path.join(base, f.rel_path))
        if abs_path.startswith(base + os.sep) and os.path.isfile(abs_path):
            try:
                os.remove(abs_path)
            except OSError:
                pass  # row removal still succeeds; orphan file is swept by retention
    db.delete(f)
    audit(db, "user", str(user.id), user.id, "files.delete", "generated_file", file_id, {"kind": f.kind})
    db.commit()
    return {"ok": True}
