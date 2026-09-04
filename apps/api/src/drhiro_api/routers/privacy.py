"""Privacy endpoints: consents, exports, account deletion. Section 8.6.

Default is private. Spouse access is read-only unless explicitly granted.
Revoking a grant blocks subsequent reads immediately.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import (
    AuditEvent,
    ConsentGrant,
    Goal,
    Meal,
    Measurement,
    Reminder,
    ReminderOccurrence,
    User,
)
from drhiro_api.security import audit

router = APIRouter(tags=["privacy"])


class ConsentCreateRequest(BaseModel):
    grantee_user_id: str
    scope: str = Field(pattern="^(activity|sleep|weight|bp|nutrition|summaries)$")
    access_level: str = Field(default="read", pattern="^(read|write)$")
    expires_at: datetime | None = None


def _consent_out(c: ConsentGrant) -> dict:
    return {
        "id": str(c.id),
        "grantor_user_id": str(c.grantor_user_id),
        "grantee_user_id": str(c.grantee_user_id),
        "scope": c.scope,
        "access_level": c.access_level,
        "expires_at": c.expires_at,
        "revoked_at": c.revoked_at,
    }


@router.get("/consents")
def list_consents(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    granted = db.query(ConsentGrant).filter(ConsentGrant.grantor_user_id == user.id).all()
    received = db.query(ConsentGrant).filter(ConsentGrant.grantee_user_id == user.id).all()
    return {
        "granted_by_me": [_consent_out(c) for c in granted],
        "granted_to_me": [_consent_out(c) for c in received],
    }


@router.post("/consents")
def create_consent(req: ConsentCreateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    grantee = db.get(User, uuid.UUID(req.grantee_user_id))
    if not grantee:
        raise HTTPException(status_code=404, detail="Grantee user not found")
    if str(grantee.id) == str(user.id):
        raise HTTPException(status_code=400, detail="Cannot grant consent to yourself")
    # Deactivate any prior active grant for the same scope.
    existing = (
        db.query(ConsentGrant)
        .filter(
            ConsentGrant.grantor_user_id == user.id,
            ConsentGrant.grantee_user_id == grantee.id,
            ConsentGrant.scope == req.scope,
            ConsentGrant.revoked_at.is_(None),
        )
        .all()
    )
    for e in existing:
        e.revoked_at = datetime.now(timezone.utc)
    grant = ConsentGrant(
        grantor_user_id=user.id,
        grantee_user_id=grantee.id,
        scope=req.scope,
        access_level=req.access_level,
        expires_at=req.expires_at,
    )
    db.add(grant)
    audit(db, "user", str(user.id), user.id, "consent.create", "consent", str(grant.id), {"scope": req.scope, "grantee": req.grantee_user_id})
    db.commit()
    db.refresh(grant)
    return _consent_out(grant)


@router.delete("/consents/{consent_id}")
def revoke_consent(consent_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    grant = (
        db.query(ConsentGrant)
        .filter(ConsentGrant.id == uuid.UUID(consent_id), ConsentGrant.grantor_user_id == user.id)
        .first()
    )
    if not grant:
        raise HTTPException(status_code=404, detail="Consent grant not found")
    grant.revoked_at = datetime.now(timezone.utc)
    audit(db, "user", str(user.id), user.id, "consent.revoke", "consent", str(grant.id))
    db.commit()
    return {"ok": True, "revoked_at": grant.revoked_at}


def _active_grants_for(db: Session, viewer: User, subject: User) -> list[ConsentGrant]:
    """Active grants subject -> viewer (what viewer may see of subject)."""
    now = datetime.now(timezone.utc)
    return (
        db.query(ConsentGrant)
        .filter(
            ConsentGrant.grantor_user_id == subject.id,
            ConsentGrant.grantee_user_id == viewer.id,
            ConsentGrant.revoked_at.is_(None),
            (ConsentGrant.expires_at.is_(None) | (ConsentGrant.expires_at > now)),
        )
        .all()
    )


class ExportRequest(BaseModel):
    include_measurements: bool = True
    include_meals: bool = True
    include_reminders: bool = True
    include_goals: bool = True


@router.post("/exports")
def create_export(req: ExportRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Create a machine-readable export of the user's own data."""
    export: dict = {"user_id": str(user.id), "exported_at": datetime.now(timezone.utc).isoformat(), "version": "1.0"}
    if req.include_measurements:
        measurements = db.query(Measurement).filter(Measurement.user_id == user.id).all()
        export["measurements"] = [
            {
                "id": str(m.id),
                "metric_type": m.metric_type,
                "start_at": m.start_at.isoformat(),
                "end_at": m.end_at.isoformat() if m.end_at else None,
                "value_json": m.value_json,
                "unit": m.unit,
                "source_provider": m.source_provider,
                "source_record_id": m.source_record_id,
                "recording_method": m.recording_method,
                "confidence": m.confidence,
            }
            for m in measurements
        ]
    if req.include_meals:
        meals = db.query(Meal).filter(Meal.user_id == user.id, Meal.status != "deleted").all()
        export["meals"] = [
            {
                "id": str(m.id),
                "eaten_at": m.eaten_at.isoformat(),
                "meal_type": m.meal_type,
                "status": m.status,
                "input_method": m.input_method,
                "totals_json": m.totals_json,
                "confidence": m.confidence,
                "items": [
                    {"display_name": i.display_name, "quantity": i.quantity, "unit": i.unit,
                     "grams": i.grams, "nutrients_json": i.nutrients_json, "source": i.source}
                    for i in m.items
                ],
            }
            for m in meals
        ]
    if req.include_reminders:
        reminders = db.query(Reminder).filter(Reminder.user_id == user.id).all()
        export["reminders"] = [
            {"id": str(r.id), "type": r.type, "schedule_json": r.schedule_json, "enabled": r.enabled}
            for r in reminders
        ]
    if req.include_goals:
        goals = db.query(Goal).filter(Goal.user_id == user.id).all()
        export["goals"] = [
            {"id": str(g.id), "goal_type": g.goal_type, "target_json": g.target_json, "status": g.status}
            for g in goals
        ]
    audit(db, "user", str(user.id), user.id, "privacy.export", "export", str(user.id), {"size": len(json.dumps(export))})
    db.commit()
    return {"export_id": str(uuid.uuid4()), "export": export}


class DeletionRequest(BaseModel):
    confirm: bool = False


@router.post("/account/deletion-request")
def request_deletion(req: DeletionRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Request account deletion. With confirm=true, purge all user data."""
    if not req.confirm:
        audit(db, "user", str(user.id), user.id, "privacy.deletion_requested", "user", str(user.id))
        db.commit()
        return {"ok": True, "message": "Deletion requested. Confirm to purge."}
    # Purge tenant data (measurements, meals, reminders, goals, grants, audit)
    for model in (Measurement, Meal, Reminder, ReminderOccurrence, Goal, ConsentGrant):
        db.query(model).filter(
            model.user_id == user.id
        ).delete(synchronize_session=False)
    db.query(ConsentGrant).filter(ConsentGrant.grantor_user_id == user.id).delete(synchronize_session=False)
    # Anonymize the user rather than hard-delete (keeps audit referential integrity)
    user.display_name = "deleted-user"
    user.status = "deleted"
    audit(db, "system", "deletion_job", user.id, "privacy.deletion_completed", "user", str(user.id))
    db.commit()
    return {"ok": True, "message": "Account data purged."}
