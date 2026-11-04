"""Personal data export bundle (0.10.x wave 3, spec §4.1).

POST /exports/bundle creates a GeneratedFile(kind="data_export") and enqueues
the worker build. DASHBOARD-ONLY per standing amendment A2: bundles are
downloaded from the Files surface and are NEVER delivered over Telegram.
The legacy synchronous POST /exports (routers/privacy.py) stays unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import GeneratedFile, User
from drhiro_api.security import audit

router = APIRouter(prefix="/exports", tags=["exports"])

DATA_EXPORT_TTL_DAYS = 7


class BundleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # reject unknown fields (e.g. password) with 422

    include_files: bool = True
    format: list[str] = ["json", "csv"]
    fhir: bool = False


def _gf_out(gf: GeneratedFile) -> dict:
    return {
        "id": str(gf.id), "kind": gf.kind, "status": gf.status, "filename": gf.filename,
        "mime": gf.mime, "size_bytes": gf.size_bytes, "sha256": gf.sha256,
        "created_at": gf.created_at, "expires_at": gf.expires_at, "error": gf.error,
    }


@router.post("/bundle")
def create_bundle(req: BundleRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from datetime import datetime, timedelta, timezone

    fmt = [f for f in req.format if f in ("json", "csv")]
    if not fmt:
        raise HTTPException(status_code=400, detail="format must contain 'json' and/or 'csv'")
    if req.fhir:
        raise HTTPException(status_code=400, detail="FHIR export is not available yet (planned v2)")
    from drhiro_api.services.task_queue import enqueue
    gf = GeneratedFile(
        user_id=user.id,
        kind="data_export",
        status="queued",
        params_json={
            "include_files": req.include_files,
            "format": fmt,
            "encrypted_requested": False,  # password-protected exports not available yet
        },
        expires_at=datetime.now(timezone.utc) + timedelta(days=DATA_EXPORT_TTL_DAYS),
    )
    db.add(gf)
    db.flush()
    job_id = enqueue("drhiro", "drhiro_worker.jobs_export.build_export", str(gf.id))
    if job_id is None:
        # F9: enqueue() swallows failures — mark failed instead of stranding queued.
        gf.status, gf.error = "failed", "queue unavailable — export not enqueued"
        db.commit()
        return {"file": _gf_out(gf)}
    gf.job_id = job_id
    audit(db, "user", str(user.id), user.id, "privacy.export_bundle", "generated_file", str(gf.id),
          {"include_files": req.include_files, "format": fmt})
    db.commit()
    db.refresh(gf)
    return {"file": _gf_out(gf)}
