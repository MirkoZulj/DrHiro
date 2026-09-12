"""Auth endpoints: Telegram Mini App, Telegram link, Android device-code,
refresh, logout. Section 8.1."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import DeviceConnection, ExternalIdentity, User
from drhiro_api.security import (
    audit,
    create_access_token,
    create_device_code,
    create_installation_id,
    create_refresh_token,
    decode_token,
    hash_refresh_token,
    validate_telegram_init_data,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: str


class TelegramMiniappRequest(BaseModel):
    init_data: str = Field(min_length=10)


class TelegramLinkStartRequest(BaseModel):
    telegram_id: str


class TelegramLinkStartResponse(BaseModel):
    link_code: str


class TelegramLinkCompleteRequest(BaseModel):
    link_code: str


class DeviceCodeRequest(BaseModel):
    device_name: str | None = None
    device_model: str | None = None


class DeviceCodeResponse(BaseModel):
    installation_id: str
    device_code: str
    expires_in_seconds: int = 600


class DeviceExchangeRequest(BaseModel):
    installation_id: str
    device_code: str
    device_name: str | None = None
    device_model: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str | None = None


# In-memory device codes and link codes (single-instance; production uses Redis)
_DEVICE_CODES: dict[str, dict] = {}
_LINK_CODES: dict[str, dict] = {}
# In-memory refresh-token revocation store: {token_hash: {"expires_at": float, "revoked": bool}}
# Production should use a TTL-backed store (Redis) or a DB table.
_REVOKED_REFRESH_TOKENS: dict[str, dict] = {}


def mint_web_login_code(telegram_id: str) -> str:
    """Mint a magic-link code bound to a Telegram identity.

    The code is stored in the shared `_LINK_CODES` store consumed by
    POST /auth/telegram-link/complete. Unlike /telegram-link/start this does
    NOT require an unpaired identity — it is used by the OpenClaw bot to give
    an already-paired user a one-click dashboard link. Returns the code.
    """
    link_code = uuid.uuid4().hex[:10]
    _LINK_CODES[link_code] = {"telegram_id": telegram_id, "expires": 1800}
    return link_code


@router.post("/telegram-miniapp", response_model=TokenResponse)
def auth_telegram_miniapp(req: TelegramMiniappRequest, db: Session = Depends(get_db)):
    """Validate Telegram initData server-side; create or pair a user."""
    user_data = validate_telegram_init_data(req.init_data)
    if not user_data:
        raise HTTPException(status_code=401, detail="Invalid Telegram initData")

    telegram_id = user_data["telegram_id"]
    identity = (
        db.query(ExternalIdentity)
        .filter(ExternalIdentity.provider == "telegram", ExternalIdentity.provider_subject == telegram_id)
        .first()
    )
    if identity:
        user = db.get(User, identity.user_id)
        if not user or user.status != "active":
            raise HTTPException(status_code=403, detail="User inactive")
    else:
        display = user_data.get("first_name") or user_data.get("username") or "drHiro user"
        user = User(display_name=display, timezone="UTC")
        db.add(user)
        db.flush()
        db.add(ExternalIdentity(provider="telegram", provider_subject=telegram_id, user_id=user.id, verified_at=datetime.now(timezone.utc)))
        db.commit()
        db.refresh(user)

    access = create_access_token(user.id)
    refresh, _ = create_refresh_token(user.id)
    audit(db, "user", telegram_id, user.id, "auth.miniapp_login", "user", str(user.id))
    db.commit()
    return TokenResponse(access_token=access, refresh_token=refresh, user_id=str(user.id))


@router.post("/telegram-link/start", response_model=TelegramLinkStartResponse)
def telegram_link_start(req: TelegramLinkStartRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Start pairing a Telegram identity to the authenticated requesting user.

    Requires authentication. The requesting user already owns the web
    session; this just creates a code they can hand to the Telegram bot
    to bind the two identities. Do not accept arbitrary telegram_id from
    unauthenticated callers.
    """
    # Only allow pairing a telegram identity to the already-authenticated user
    # they're currently logged in as.
    identity = (
        db.query(ExternalIdentity)
        .filter(ExternalIdentity.provider == "telegram", ExternalIdentity.provider_subject == req.telegram_id)
        .first()
    )
    if identity:
        raise HTTPException(status_code=409, detail="Telegram identity already paired")
    link_code = uuid.uuid4().hex[:10]
    _LINK_CODES[link_code] = {"telegram_id": req.telegram_id, "requesting_user_id": str(user.id), "expires_at": datetime.now(timezone.utc).timestamp() + 1800}
    return TelegramLinkStartResponse(link_code=link_code)


@router.post("/telegram-link/complete", response_model=TokenResponse)
def telegram_link_complete(req: TelegramLinkCompleteRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Complete pairing: the authenticated web user enters the code shown
    by the bot. Binds the Telegram identity to the CURRENTLY authenticated
    user, never to the identity encoded in a stolen code."""
    code = _LINK_CODES.get(req.link_code)
    if not code:
        raise HTTPException(status_code=404, detail="Link code not found or expired")
    # Enforce code expiry
    if datetime.now(timezone.utc).timestamp() > code["expires_at"]:
        _LINK_CODES.pop(req.link_code, None)
        raise HTTPException(status_code=404, detail="Link code expired")
    telegram_id = code["telegram_id"]
    # Bind the Telegram identity to the AUTHENTICATED requesting user,
    # not to whatever user the code was originally intended for. This
    # prevents an attacker from claiming another user's telegram id.
    identity = (
        db.query(ExternalIdentity)
        .filter(ExternalIdentity.provider == "telegram", ExternalIdentity.provider_subject == telegram_id)
        .first()
    )
    if identity:
        # The telegram identity is already paired to some user.
        if str(identity.user_id) == str(user.id):
            # Already paired to the requesting user -- nothing to do, just return tokens
            pass
        else:
            raise HTTPException(status_code=409, detail="Telegram identity already paired to another account")
    else:
        db.add(ExternalIdentity(provider="telegram", provider_subject=telegram_id, user_id=user.id, verified_at=datetime.now(timezone.utc)))
        db.flush()
    _LINK_CODES.pop(req.link_code, None)
    access = create_access_token(user.id)
    refresh, _ = create_refresh_token(user.id)
    audit(db, "user", telegram_id, user.id, "auth.telegram_link_complete", "user", str(user.id))
    db.commit()
    return TokenResponse(access_token=access, refresh_token=refresh, user_id=str(user.id))


@router.post("/android/device-code", response_model=DeviceCodeResponse)
def android_device_code(req: DeviceCodeRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Web/Mini App user starts device linking; gets code + installation id."""
    installation_id = create_installation_id()
    device_code = create_device_code()
    _DEVICE_CODES[device_code] = {
        "installation_id": installation_id,
        "user_id": str(user.id),
        "expires_at": datetime.now(timezone.utc).timestamp() + 600,  # absolute UTC timestamp
    }
    audit(db, "user", str(user.id), user.id, "auth.device_code_issued", "device", installation_id)
    db.commit()
    return DeviceCodeResponse(installation_id=installation_id, device_code=device_code)


@router.post("/android/exchange", response_model=TokenResponse)
def android_exchange(req: DeviceExchangeRequest, db: Session = Depends(get_db)):
    """Android bridge swaps code+installation id for tokens.

    The device code is user-bound (issued by the web app or by the drHiro
    bot tool /tools/issue_device_code). The bridge presents its OWN
    installation_id (device identity), which becomes the DeviceConnection
    hash. This makes linking one-time: tokens persist and auto-refresh.
    """
    entry = _DEVICE_CODES.get(req.device_code)
    if not entry:
        raise HTTPException(status_code=401, detail="Invalid device code")
    # Enforce absolute expiry timestamp
    if datetime.now(timezone.utc).timestamp() > entry["expires_at"]:
        _DEVICE_CODES.pop(req.device_code, None)
        raise HTTPException(status_code=401, detail="Device code expired")
    user_id = uuid.UUID(entry["user_id"])
    user = db.get(User, user_id)
    if not user or user.status != "active":
        raise HTTPException(status_code=403, detail="User inactive")
    _DEVICE_CODES.pop(req.device_code, None)

    # Upsert the device connection by installation id (idempotent re-link).
    conn = (
        db.query(DeviceConnection)
        .filter(DeviceConnection.user_id == user.id, DeviceConnection.external_device_id_hash == req.installation_id)
        .first()
    )
    if conn:
        conn.status = "active"
        conn.device_name = req.device_name or conn.device_name
        conn.device_model = req.device_model or conn.device_model
    else:
        db.add(
            DeviceConnection(
                user_id=user.id,
                provider="health_connect",
                device_name=req.device_name,
                device_model=req.device_model,
                external_device_id_hash=req.installation_id,
                status="active",
            )
        )
    access = create_access_token(user.id)
    refresh, _ = create_refresh_token(user.id)
    audit(db, "android", req.installation_id, user.id, "auth.android_linked", "device", req.installation_id)
    db.commit()
    return TokenResponse(access_token=access, refresh_token=refresh, user_id=str(user.id))


@router.post("/refresh", response_model=TokenResponse)
def refresh(req: RefreshRequest, db: Session = Depends(get_db)):
    payload = decode_token(req.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid refresh token subject")
    # Check if this refresh token has been revoked (e.g. on logout)
    token_hash = hash_refresh_token(req.refresh_token)
    revoked_entry = _REVOKED_REFRESH_TOKENS.get(token_hash)
    if revoked_entry and revoked_entry.get("revoked"):
        raise HTTPException(status_code=401, detail="Refresh token has been revoked")
    user = db.get(User, user_id)
    if not user or user.status != "active":
        raise HTTPException(status_code=403, detail="User inactive")
    # Revoke the old refresh token (rotation) and issue a new one
    _REVOKED_REFRESH_TOKENS[token_hash] = {"revoked": True, "expires_at": payload.get("exp", 0)}
    access = create_access_token(user.id)
    new_refresh, new_hash = create_refresh_token(user.id)
    return TokenResponse(access_token=access, refresh_token=new_refresh, user_id=str(user.id))


@router.post("/logout")
def logout(req: LogoutRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Revoke the supplied refresh token so it cannot be replayed
    if req.refresh_token:
        token_hash = hash_refresh_token(req.refresh_token)
        _REVOKED_REFRESH_TOKENS[token_hash] = {"revoked": True, "expires_at": 0}
    audit(db, "user", str(user.id), user.id, "auth.logout", "user", str(user.id))
    db.commit()
    return {"ok": True}
