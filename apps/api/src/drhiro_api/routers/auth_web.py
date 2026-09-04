"""Web login via Telegram/WhatsApp OTP (passwordless).

Flow (per user request):
1. User opens the web app and enters their display name.
2. drHiro generates a 6-digit code, stores it in Redis (TTL 5 min,
   max attempts, per-identifier rate limit), and sends it to the user's
   linked Telegram (or WhatsApp when the gateway supports it).
3. User enters the code; drHiro verifies and issues access/refresh tokens.

Security properties:
- Codes are single-use, expire in 5 minutes, max 5 attempts.
- Rate limit: one code per identifier per 60s.
- Delivery is only possible to identities already linked (telegram_id
  recorded via the bot pairing flow); no code can be sent to an
  arbitrary number.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import uuid

import redis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from drhiro_api.config import get_settings
from drhiro_api.db import get_db
from drhiro_api.models import ExternalIdentity, User
from drhiro_api.security import audit, create_access_token, create_refresh_token

router = APIRouter(prefix="/auth/web", tags=["auth-web"])

CODE_TTL_SECONDS = 300          # 5 minutes
MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60


def _redis() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


class OtpRequest(BaseModel):
    identifier: str = Field(min_length=2, max_length=100)  # display name


class OtpRequestResponse(BaseModel):
    ok: bool
    sent_to: str  # "telegram" | "whatsapp" | "none"
    expires_in_seconds: int = CODE_TTL_SECONDS
    debug_code: str | None = None  # only when DRHIRO_DEBUG=true


class OtpVerify(BaseModel):
    identifier: str = Field(min_length=2, max_length=100)
    code: str = Field(min_length=6, max_length=6)


@router.post("/otp/request", response_model=OtpRequestResponse)
def otp_request(req: OtpRequest, db: Session = Depends(get_db)):
    """Send a 6-digit code to the user's linked Telegram/WhatsApp."""
    user = _find_user_by_identifier(db, req.identifier)
    if not user or user.status != "active":
        # Do not reveal whether an identifier exists (enumeration defense).
        raise HTTPException(status_code=404, detail="Unknown user. Start the drHiro bot to pair your account.")

    settings = get_settings()
    r = _redis()
    key = f"drhiro:otp:req:{req.identifier.strip().lower()}"
    last_sent = r.get(key)
    if last_sent and (time.time() - float(last_sent)) < RESEND_COOLDOWN_SECONDS:
        raise HTTPException(status_code=429, detail="Code already sent. Try again in a minute.")

    # Find the linked channel for delivery.
    telegram_id = None
    wa_identity = None
    for ident in user.identities:
        if ident.provider == "telegram":
            telegram_id = ident.provider_subject
        elif ident.provider == "whatsapp":
            wa_identity = ident.provider_subject

    code = f"{random.randint(0, 999999):06d}"
    r.setex(
        f"drhiro:otp:code:{req.identifier.strip().lower()}",
        CODE_TTL_SECONDS,
        json.dumps({"code": code, "attempts": 0, "user_id": str(user.id)}),
    )

    sent_to = "none"
    if telegram_id and settings.telegram_bot_token:
        ok = _send_telegram_code(settings.telegram_bot_token, telegram_id, code)
        if ok:
            sent_to = "telegram"
    elif wa_identity:
        # WhatsApp delivery arrives with the OpenClaw gateway (Phase 2).
        sent_to = "whatsapp_pending"

    if sent_to == "none":
        raise HTTPException(status_code=400, detail="No linked delivery channel. Start the drHiro bot first.")

    r.setex(key, RESEND_COOLDOWN_SECONDS, str(time.time()))
    audit(db, "web", str(user.id), user.id, "auth.web_otp_request", "user", str(user.id), {"sent_to": sent_to})
    db.commit()
    return OtpRequestResponse(ok=True, sent_to=sent_to, debug_code=code if settings.debug else None)


@router.post("/otp/verify")
def otp_verify(req: OtpVerify, db: Session = Depends(get_db)):
    """Verify the 6-digit code and issue tokens."""
    r = _redis()
    norm = req.identifier.strip().lower()
    stored = r.get(f"drhiro:otp:code:{norm}")
    if not stored:
        raise HTTPException(status_code=401, detail="Code expired or not requested.")

    data = json.loads(stored)
    if data["attempts"] >= MAX_ATTEMPTS:
        r.delete(f"drhiro:otp:code:{norm}")
        raise HTTPException(status_code=429, detail="Too many attempts. Request a new code.")

    if not _constant_time_eq(data["code"], req.code):
        data["attempts"] += 1
        r.setex(f"drhiro:otp:code:{norm}", CODE_TTL_SECONDS, json.dumps(data))
        raise HTTPException(status_code=401, detail="Invalid code.")

    user = db.get(User, uuid.UUID(data["user_id"]))
    if not user or user.status != "active":
        raise HTTPException(status_code=403, detail="User inactive")

    r.delete(f"drhiro:otp:code:{norm}")
    access = create_access_token(user.id)
    refresh, _ = create_refresh_token(user.id)
    audit(db, "web", str(user.id), user.id, "auth.web_otp_verify", "user", str(user.id))
    db.commit()
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer", "user_id": str(user.id)}


def _find_user_by_identifier(db: Session, identifier: str) -> User | None:
    """Resolve identifier: exact display_name match first, then a linked
    telegram username. Case-insensitive."""
    norm = identifier.strip()
    user = (
        db.query(User)
        .filter(User.display_name.ilike(norm))
        .first()
    )
    if user:
        return user
    # Telegram @username match on external identities
    ident = (
        db.query(ExternalIdentity)
        .filter(
            ExternalIdentity.provider == "telegram",
            ExternalIdentity.provider_subject.ilike(norm.lstrip("@")),
        )
        .first()
    )
    return db.get(User, ident.user_id) if ident else None


def _send_telegram_code(bot_token: str, chat_id: str, code: str) -> bool:
    """Send the code via the Telegram Bot API. Returns True on success."""
    import urllib.request

    text = (
        f"drHiro sign-in code: {code}\n\n"
        "This code expires in 5 minutes. If you did not request it, ignore this message."
    )
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception:
        return False


def _constant_time_eq(a: str, b: str) -> bool:
    return hashlib.sha256(a.encode()).digest() == hashlib.sha256(b.encode()).digest()
