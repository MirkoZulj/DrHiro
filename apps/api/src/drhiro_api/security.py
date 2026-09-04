"""Security primitives: JWT access/refresh, Telegram initData validation,
device-link codes, OpenClaw service identity, audit helper.

Blueprint requirements honored here:
- Short-lived access tokens and rotating refresh tokens.
- Server-side validation of Telegram Mini App initData (never trust client).
- Short-lived device-link codes for the Android bridge.
- OpenClaw tools get a signed service identity; the subject is inferred
  from the token, never accepted from the LLM.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

import jwt

from drhiro_api.config import get_settings

ALGO = "HS256"


# ---------------------------------------------------------------------------
# JWT tokens
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(user_id: uuid.UUID, scope: str = "user") -> str:
    settings = get_settings()
    expires = _now() + timedelta(minutes=settings.jwt_access_ttl_minutes)
    payload = {
        "sub": str(user_id),
        "scope": scope,
        "exp": expires,
        "iat": _now(),
        "jti": secrets.token_hex(8),
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGO)


def create_refresh_token(user_id: uuid.UUID, scope: str = "user") -> tuple[str, str]:
    """Return (opaque_refresh_token, token_hash). The hash is stored in DB."""
    settings = get_settings()
    token = secrets.token_urlsafe(48)
    expires = _now() + timedelta(days=settings.jwt_refresh_ttl_days)
    payload = {
        "sub": str(user_id),
        "scope": scope,
        "exp": expires,
        "iat": _now(),
        "type": "refresh",
        "jti": token,
    }
    jwt_token = jwt.encode(payload, settings.jwt_secret, algorithm=ALGO)
    return jwt_token, token


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, get_settings().jwt_secret, algorithms=[ALGO])
    except jwt.PyJWTError:
        return None


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Telegram Mini App initData validation
# ---------------------------------------------------------------------------

def validate_telegram_init_data(init_data: str) -> dict | None:
    """Validate Telegram Mini App initData and return the user object.

    Per Telegram docs: hash = HMAC-SHA256(data_check_string, secret_key)
    where secret_key = HMAC-SHA256("WebAppData", bot_token).
    The data_check_string is all key=value pairs sorted by key, joined
    with '\\n', EXCLUDING the 'hash' field.
    """
    settings = get_settings()
    if not settings.telegram_bot_token:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    secret_key = hmac.new(b"WebAppData", settings.telegram_bot_token.encode(), hashlib.sha256).digest()
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        return None

    # auth_date within 24h prevents replay
    auth_date = int(parsed.get("auth_date", 0))
    if auth_date and (time.time() - auth_date) > 86400:
        return None

    try:
        user = json.loads(parsed.get("user", "{}"))
    except json.JSONDecodeError:
        user = {}
    return {
        "telegram_id": str(user.get("id")),
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "username": user.get("username"),
        "auth_date": auth_date,
    }


# ---------------------------------------------------------------------------
# Device-link codes (Android bridge)
# ---------------------------------------------------------------------------

def create_device_code() -> str:
    """Short-lived, high-entropy code shown in the Mini App/web for linking."""
    return secrets.token_urlsafe(9)  # ~12 chars


def create_installation_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# OpenClaw service identity
# ---------------------------------------------------------------------------

def create_service_token(service: str = "openclaw", expires_minutes: int = 30) -> str:
    """Signed service token the OpenClaw gateway presents to call tools.

    The token binds a service identity; per-user subject resolution
    happens server-side from the message context (telegram_id), never
    from the LLM.
    """
    settings = get_settings()
    payload = {
        "service": service,
        "exp": _now() + timedelta(minutes=expires_minutes),
        "iat": _now(),
        "type": "service",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGO)


def validate_service_token(token: str, expected_service: str = "openclaw") -> bool:
    payload = decode_token(token)
    if not payload:
        return False
    if payload.get("type") != "service":
        return False
    if payload.get("service") != expected_service:
        return False
    return True


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def audit(db, actor_type: str, actor_id: str | None, user_id_affected: uuid.UUID | None,
          action: str, resource_type: str | None = None, resource_id: str | None = None,
          metadata: dict | None = None) -> None:
    """Write an audit event. Caller commits."""
    from drhiro_api.models import AuditEvent
    db.add(
        AuditEvent(
            actor_type=actor_type,
            actor_id=actor_id,
            user_id_affected=user_id_affected,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata_json=metadata,
        )
    )
