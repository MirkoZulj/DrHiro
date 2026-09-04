"""FastAPI dependencies: current user resolution from tokens."""

from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.models import ExternalIdentity, User
from drhiro_api.security import decode_token

bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    payload = decode_token(credentials.credentials)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token subject")
    user = db.get(User, user_id)
    if not user or user.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User inactive or missing")
    return user


def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User | None:
    """Like get_current_user but returns None when no/invalid bearer token.

    Used by dual-auth endpoints (bearer OR OpenClaw service token).
    """
    if credentials is None:
        return None
    payload = decode_token(credentials.credentials)
    if not payload or payload.get("type") != "access":
        return None
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        return None
    user = db.get(User, user_id)
    if not user or user.status != "active":
        return None
    return user


def get_user_by_telegram_id(db: Session, telegram_id: str) -> User | None:
    identity = (
        db.query(ExternalIdentity)
        .filter(ExternalIdentity.provider == "telegram", ExternalIdentity.provider_subject == telegram_id)
        .first()
    )
    if identity:
        return db.get(User, identity.user_id)
    return None
