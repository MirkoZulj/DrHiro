"""Instance settings — read/write the runtime settings store.

Security:
- Restricted to the authorized user (the one whose linked Telegram identity
  matches the configured ``telegram_allowed_username``; on a fresh install
  before any telegram pairing, any authenticated user is accepted so the first
  admin can configure).
- Secret fields (ai_api_key, telegram_bot_token) are WRITE-ONLY: GET returns a
  masked ``{"set": bool}`` indicator; the full secret is never returned.
- Secret values are never logged.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import ExternalIdentity, User
from drhiro_api.security import audit
from drhiro_api.services.settings_store import (
    EDITABLE_FIELDS,
    apply_updates,
    get_row,
    to_masked_dict,
)

router = APIRouter(prefix="/settings", tags=["settings"])


class SecretValue(BaseModel):
    set: bool
    value: str | None = Field(default=None, max_length=4096)


# For each editable field the allowed payload type: str for non-secret,
# SecretValue for secret.
class SettingsUpdate(BaseModel):
    ai_backend_url: str | None = None
    model_name: str | None = None
    ai_api_key: SecretValue | None = None
    telegram_bot_token: SecretValue | None = None
    telegram_allowed_username: str | None = None


def _authorized(db: Session, user: User) -> bool:
    """A user may manage settings if they are the configured authorized user.

    Authorized = has a linked Telegram identity whose subject equals
    ``telegram_allowed_username``. When no username is configured yet (fresh
    install, nothing paired), any authenticated user is allowed so the first
    admin can set things up.
    """
    row = get_row(db)
    allowed = row.telegram_allowed_username or ""
    if not allowed:
        # Nothing configured yet -> first authenticated user is the admin.
        return True
    identity = (
        db.query(ExternalIdentity)
        .filter(
            ExternalIdentity.provider == "telegram",
            ExternalIdentity.user_id == user.id,
        )
        .first()
    )
    if identity and identity.provider_subject == allowed:
        return True
    # Also accept a telegram identity whose subject matches the allowed
    # username even if recorded against the user in another form.
    return False


def _require_admin(db: Session, user: User) -> None:
    if not _authorized(db, user):
        raise HTTPException(status_code=403, detail="Only the authorized user may change settings.")


@router.get("")
def get_settings(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(db, user)
    row = get_row(db)
    return to_masked_dict(row)


@router.put("")
def update_settings(
    updates: SettingsUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(db, user)
    payload = updates.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(status_code=400, detail="Nothing to update.")
    row = apply_updates(db, payload)
    audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        user_id_affected=user.id,
        action="settings.update",
        resource_type="app_settings",
        resource_id=str(row.id),
        metadata={"fields": sorted(payload.keys())},  # field names only — never values
    )
    db.commit()
    return to_masked_dict(row)
