"""Generated-file helper for worker jobs (spec §2.2).

begin(file_id) -> marks running
write_bytes(file_id, data, filename, mime) -> writes under
    generated/<user_id>/<uuid>.<ext> inside generated_files_dir, computes
    sha256 + size, sets status ready
fail(file_id, err) -> marks failed

PDF rendering lives in jobs_reports; this module only owns the file lifecycle.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone

from drhiro_api.config import get_settings
from drhiro_api.db import SessionLocal
from drhiro_api.models import GeneratedFile


def begin(file_id: str) -> None:
    db = SessionLocal()
    try:
        f = db.get(GeneratedFile, uuid.UUID(file_id))
        if f:
            f.status = "running"
            db.commit()
    finally:
        db.close()


def fail(file_id: str, err: str) -> None:
    db = SessionLocal()
    try:
        f = db.get(GeneratedFile, uuid.UUID(file_id))
        if f:
            f.status = "failed"
            f.error = str(err)[:2000]
            db.commit()
    finally:
        db.close()


def write_bytes(file_id: str, data: bytes, filename: str, mime: str) -> str:
    """Write the artefact atomically and mark it ready. Returns rel_path."""
    settings = get_settings()
    db = SessionLocal()
    try:
        f = db.get(GeneratedFile, uuid.UUID(file_id))
        if not f:
            raise ValueError(f"generated_file {file_id} not found")
        ext = os.path.splitext(filename)[1].lstrip(".") or "bin"
        rel = os.path.join("generated", str(f.user_id), f"{uuid.uuid4()}.{ext}")
        abs_dir = os.path.join(settings.generated_files_dir, os.path.dirname(rel))
        os.makedirs(abs_dir, exist_ok=True)
        abs_path = os.path.join(settings.generated_files_dir, rel)
        tmp = abs_path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, abs_path)  # atomic move into place
        f.rel_path = rel
        f.filename = filename
        f.mime = mime
        f.size_bytes = len(data)
        f.sha256 = hashlib.sha256(data).hexdigest()
        f.status = "ready"
        f.error = None
        db.commit()
        return rel
    finally:
        db.close()


def expire_files(now: datetime | None = None) -> int:
    """Retention sweep (spec §2.4): data_export rows older than 7 days expire;
    any file past its expires_at is marked expired (file kept on disk until the
    user deletes or a future sweep removes expired artefacts)."""
    now = now or datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        n = 0
        rows = (
            db.query(GeneratedFile)
            .filter(GeneratedFile.status.in_(("ready", "queued", "failed", "running")))
            .all()
        )
        for f in rows:
            hit = False
            if f.expires_at is not None and f.expires_at <= now and f.status != "expired":
                f.status, hit = "expired", True
            elif (
                f.kind == "data_export" and f.status == "ready" and f.created_at is not None
                and (now - f.created_at).days >= 7
            ):
                f.status, hit = "expired", True
            if hit:
                n += 1
        db.commit()
        return n
    finally:
        db.close()
