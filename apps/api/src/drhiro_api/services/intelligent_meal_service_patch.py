# Proposed replacement for the /opt/apps/intelligent-meal/service.py CONFIRM handler.
#
# This is a PREVIEW / PREPARATION artifact on the feature/meal-liquid-idempotency
# branch. It is NOT deployed and NOT wired to any live service. It shows how the
# /meals/from-text-intelligent/confirm endpoint (and /meals/from-text alias)
# should be rewritten to route through the unified consumption domain
# (consumption.py) so that:
#   (a) text is parsed ONCE via consumption.py\'s parser,
#   (b) the unified write path (write_consumption / confirm_consumption) writes
#       meal items AND linked beverage measurements in ONE transaction,
#   (c) the Telegram update_id (or caller idempotency key) flows through so
#       consumption_operations dedupes,
#   (d) the completed operation result is persisted (result_json in
#       consumption_operations) so a retry after Redis draft deletion returns
#       the SAVED result instead of 404,
#   (e) the same saved result is returned on replay.
#
# The rest of the endpoints (DELETE /meals, /meals/learn, recipes, etc.) keep
# their existing behavior; only the confirm flow changes.
"""
Proposed new service.py confirm handler (integration preview).

This module is a REFERENCE for the deploy-time rewrite. The live service
continues to run the version in /opt/apps/intelligent-meal/service.py until
this patch is deployed. To deploy: copy the CONFIRM-RELATED functions below
into the live service.py (adjusting imports/engine/Redis to the deploy
environment\'s) and restart the service.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Optional

import redis
import jwt as pyjwt
from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

# --- unified consumption domain (single write path) ---
from drhiro_api.services.consumption import (
    parse_consumption_text,
    confirm_consumption,
    find_completed_result_by_identity,
    ParsedItem,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("intelligent-meal")

# ---------------------------------------------------------------------------
# Config (deploy environment provides these)
# ---------------------------------------------------------------------------
DB_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg2://<DB_USER>:<DB_PASS>@<DB_HOST>/<DB_NAME>")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
JWT_SECRET = os.environ.get("DRHIRO_JWT_SECRET", "<JWT_SECRET>")
SERVICE_TOKEN = os.environ.get("DRHIRO_SERVICE_TOKEN", "")
TELEGRAM_ID = os.environ.get("DRHIRO_TELEGRAM_ID", "<TELEGRAM_ID>")

engine = create_engine(DB_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

app = FastAPI(title="Intelligent Meal Service (proposed)")
bearer = HTTPBearer(auto_error=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    db: Session = Depends(get_db),
):
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        payload = pyjwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token subject")
    user = db.execute(text("SELECT * FROM users WHERE id = :id AND status = 'active'"), {"id": user_id}).fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return {"id": str(user.id)}


def get_user_from_service_or_bearer(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    x_service_token: Optional[str] = Header(None, alias="x-service-token"),
    x_telegram_id: Optional[str] = Header(None, alias="x-telegram-id"),
    db: Session = Depends(get_db),
):
    """Accept either a user JWT bearer token OR a service token + telegram_id header."""
    if credentials is not None:
        try:
            payload = pyjwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
            user_id = payload.get("sub")
            if user_id:
                user = db.execute(text("SELECT * FROM users WHERE id = :id AND status = 'active'"), {"id": user_id}).fetchone()
                if user:
                    return {"id": str(user.id)}
        except Exception:
            pass

    if x_service_token and x_service_token == SERVICE_TOKEN and x_telegram_id:
        user = db.execute(
            text("SELECT u.* FROM users u JOIN external_identities ei ON u.id = ei.user_id "
                 "WHERE ei.provider = 'telegram' AND ei.provider_subject = :tid AND u.status = 'active'"),
            {"tid": x_telegram_id}
        ).fetchone()
        if user:
            return {"id": str(user.id)}

    raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Draft store (Redis)
# ---------------------------------------------------------------------------
def save_draft(draft_id: str, data: dict, ttl: int = 3600):
    redis_client.set(f"intelligent_draft:{draft_id}", json.dumps(data), ex=ttl)


def get_draft(draft_id: str) -> Optional[dict]:
    data = redis_client.get(f"intelligent_draft:{draft_id}")
    return json.loads(data) if data else None


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class MealIn(BaseModel):
    text: str
    meal_type: Optional[str] = None
    eaten_at: Optional[str] = None
    match_all: Optional[bool] = None


class ConfirmMealRequest(BaseModel):
    draft_id: str
    selections: list[int] = []


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/meals/from-text")
async def create_meal_from_text_alias(
    req: MealIn,
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    """Legacy alias -> intelligent path."""
    return await create_meal_from_text_intelligent(req=req, user=user, db=db)


@app.post("/meals/from-text-intelligent")
async def create_meal_from_text_intelligent(
    req: MealIn,
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    """Parse text once, return a Redis draft."""
    text = req.text
    parsed_items = parse_consumption_text(text)

    draft_items = []
    for pi in parsed_items:
        draft_items.append({
            "display_name": pi.display_name,
            "grams": pi.grams,
            "volume_ml": pi.volume_ml,
            "beverage_category": pi.beverage_category,
            "is_beverage": pi.is_beverage,
            "quantity": pi.quantity,
            "unit": pi.unit,
            "nutrients_per_100": pi.nutrients_per_100,
            "nutrients_scaled": pi.nutrients_scaled,
            "source": pi.source,
            "confidence": pi.confidence,
        })

    draft_id = uuid.uuid4().hex
    save_draft(draft_id, {
        "user_id": user["id"],
        "text": text,
        "eaten_at": req.eaten_at,
        "meal_type": req.meal_type,
        "items": draft_items,
    })

    return {"ok": True, "data": {"draft_id": draft_id, "items": draft_items}}


@app.post("/meals/from-text-intelligent/confirm")
async def confirm_meal(
    req: ConfirmMealRequest,
    user: dict = Depends(get_user_from_service_or_bearer),
    db: Session = Depends(get_db),
):
    """Confirm a draft and write via the unified consumption domain.

    B2 — Durable replay WITHOUT the Redis draft:
      1. If the Redis draft exists, parse items from it and call
         confirm_consumption (which resolves the ConsumptionOperation by
         Telegram source identity BEFORE writing).
      2. If the Redis draft is gone (expired / deleted after a prior
         successful confirm), look up a COMPLETED ConsumptionOperation by
         the caller's Telegram source identity. If found, return its saved
         result_json (the original meal) — no 404, no duplicate meal.
      3. If no draft AND no completed operation exist, return an explicit
         error (NOT an empty meal).
    """
    draft = get_draft(req.draft_id)

    if draft:
        if draft["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Not your draft")

        # Rebuild ParsedItems from the draft (parsed ONCE at draft time).
        items = [ParsedItem(**it) for it in draft["items"]]

        # Unified write with durable replay + idempotency via Telegram source identity.
        result = confirm_consumption(
            db=db,
            user_id=user["id"],
            items=items,
            meal_type=draft.get("meal_type"),
            eaten_at=_parse_eaten_at(draft.get("eaten_at")),
            notes=draft.get("text"),
            source="telegram",
            source_chat_id=user.get("telegram_chat_id"),
            source_message_id=user.get("telegram_message_id"),
            source_bot_id=user.get("telegram_bot_id"),
            raw_text=draft.get("text"),
        )

        # Delete the Redis draft (best-effort; durable replay makes this safe).
        redis_client.delete(f"intelligent_draft:{req.draft_id}")
        return result

    # B2: Draft is gone. Resolve the completed operation WITHOUT the draft.
    completed_result = find_completed_result_by_identity(
        db=db,
        user_id=user["id"],
        source="telegram",
        source_chat_id=user.get("telegram_chat_id"),
        source_message_id=user.get("telegram_message_id"),
        source_bot_id=user.get("telegram_bot_id"),
    )
    if completed_result is not None:
        # Durable replay: return the saved result from the prior successful confirm.
        return completed_result

    # No draft and no completed operation → explicit error (never an empty meal).
    raise HTTPException(
        status_code=404,
        detail="No draft and no completed operation found for this identity. "
               "The draft may have expired before confirmation.",
    )


def _parse_eaten_at(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


@app.get("/healthz")
async def health():
    return {"ok": True, "service": "intelligent-meal", "mode": "proposed-unified-confirm"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
