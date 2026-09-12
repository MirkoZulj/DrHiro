"""T1 trusted Telegram ingress endpoint + writer-ownership gate.

Two responsibilities:

1. `POST /ingest/telegram/event` — accepts an event signed by the single polling
   owner (the telegram-bridge), verifies the trusted transport envelope, and
   drives the trusted ingress worker. Fails closed when the secret or the
   verified bot id is not configured.

2. `require_model_writer_allowed` — a dependency for the LEGACY consumption
   writers. When the trusted path is active, the conversational model's service
   identity may no longer log a consumption by itself; manual and other
   authenticated callers are unaffected and keep their explicit idempotency
   contract.

See the T1 ingress design note for writer ownership.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from drhiro_api.config import get_settings
from drhiro_api.db import get_db
from drhiro_api.services.telegram_ingress import (
    IngressConflict,
    IngressRejected,
    TrustedIngressWorker,
    accept_signed_event,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["telegram-ingress"])

SIGNATURE_HEADER = "X-DrHiro-Ingress-Signature"

# The model is asked for PROPOSALS only. It is told explicitly that it does not
# own identity, authorization, or nutrition values.
PROPOSAL_SYSTEM_PROMPT = """You extract structured consumption items from a user's message.

Return ONLY minified JSON of the form:
{"meal_type": "breakfast|lunch|dinner|snack"|null,
 "items": [{"name": string, "grams": number|null, "volume_ml": number|null,
            "category": "water|non_alcoholic|beer|wine|spirits|other_alcohol"|null,
            "is_beverage": boolean}]}

Rules:
- Use grams for solid food, volume_ml for drinks. Never invent a quantity you
  were not told; use null instead.
- Do NOT output calories or any nutrition values. Do NOT output user ids,
  chat ids, message ids, or any identity field.
- If you cannot identify any item, return {"items": []}.
"""


def llm_proposer(operation_id: str, text: str):
    """Model-backed proposal source. Output is UNTRUSTED and validated later."""
    from drhiro_api.services.llm_client import chat_complete_sync

    raw = chat_complete_sync(
        [
            {"role": "system", "content": PROPOSAL_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"operation_id={operation_id}\n"
                    f"message: {text}\n"
                    "Return minified JSON only."
                ),
            },
        ],
        temperature=0.1,
    )
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        return json.loads(raw[start:end])
    except Exception:  # noqa: BLE001
        log.warning("Proposal parsing failed; treating as no proposals")
        return {"items": []}


@router.post("/telegram/event")
def telegram_event(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    """Trusted Telegram event from the bridge (the single polling owner)."""
    settings = get_settings()

    if not settings.telegram_ingress_secret:
        raise HTTPException(status_code=503, detail="ingress_secret_not_configured")
    if not settings.telegram_bot_id:
        raise HTTPException(status_code=503, detail="trusted_bot_id_not_configured")

    signed = dict(payload)
    signed["signature"] = request.headers.get(SIGNATURE_HEADER, "")

    try:
        event = accept_signed_event(
            settings.telegram_ingress_secret,
            signed,
            trusted_bot_id=settings.telegram_bot_id,
        )
    except IngressRejected as exc:
        # Unauthenticated transport: no read, no write.
        raise HTTPException(status_code=401, detail=f"ingress_rejected:{exc}") from exc

    worker = TrustedIngressWorker(db, proposer=llm_proposer)
    try:
        return worker.handle(event)
    except IngressConflict as exc:
        raise HTTPException(status_code=409, detail=f"ingress_conflict:{exc}") from exc
    except IngressRejected as exc:
        raise HTTPException(status_code=403, detail=f"ingress_rejected:{exc}") from exc


def require_model_writer_allowed(request: Request) -> None:
    """Gate for legacy consumption writers.

    The model-driven caller (the OpenClaw/MCP service identity) loses the right
    to create consumption once the trusted path owns writes. A user JWT
    (manual/web/Android callers) is unaffected and keeps its explicit
    idempotency contract.
    """
    settings = get_settings()
    if settings.legacy_consumption_writers_enabled:
        return
    if not settings.telegram_ingress_enabled:
        # Closing the legacy writers without an active trusted path would break
        # logging entirely; refuse rather than silently disable consumption.
        raise HTTPException(
            status_code=503,
            detail="legacy_writers_closed_without_trusted_path",
        )

    service_token = request.headers.get("x-service-token", "")
    if not service_token:
        return  # authenticated non-model caller (user JWT)

    from drhiro_api.security import validate_service_token

    if validate_service_token(service_token, expected_service="openclaw"):
        raise HTTPException(
            status_code=403,
            detail="model_writer_disabled: trusted telegram ingress owns consumption",
        )
