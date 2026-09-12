"""R2 / T1 — trusted ingress client for the Telegram bridge.

The bridge is the SINGLE owner of the bot's update stream (long polling). This
module turns a raw Telegram update into a signed trusted event and delivers it
to the API's ingress worker.

Trust boundary:
  * identity (bot/chat/message/user) is taken from the AUTHENTIC update here,
    never from model output;
  * it is signed with the shared ingress secret so the API can verify it
    arrived via authenticated transport;
  * the model never sees or supplies it.

Adding a second poller or a webhook receiver would break single-owner, so this
deliberately hooks into the existing poll loop instead.

See the T1 ingress design note.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

IDENTITY_VERSION = "v1"
ENVELOPE_VERSION = 1


def canonical_identity(bot_id: str, chat_id: str, message_id: str) -> str:
    return json.dumps(
        {"v": IDENTITY_VERSION, "bot": str(bot_id), "chat": str(chat_id), "msg": str(message_id)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def event_key(bot_id: str, chat_id: str, message_id: str) -> str:
    return hashlib.blake2b(
        canonical_identity(bot_id, chat_id, message_id).encode("utf-8"), digest_size=32
    ).hexdigest()


def content_digest(text: str) -> str:
    return hashlib.sha256(" ".join((text or "").split()).encode("utf-8")).hexdigest()


def _envelope_message(**kw: Any) -> bytes:
    fields = {
        "v": ENVELOPE_VERSION,
        "service": str(kw["service"]),
        "bot": str(kw["bot_id"]),
        "chat": str(kw["chat_id"]),
        "msg": str(kw["message_id"]),
        "user": str(kw["user_id"]),
        "digest": str(kw["digest"]),
        "kind": str(kw["kind"]),
    }
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_event(secret: str, **kw: Any) -> str:
    return hmac.new(secret.encode("utf-8"), _envelope_message(**kw), hashlib.sha256).hexdigest()


def verify_event(secret: str, signature: str, **kw: Any) -> bool:
    if not signature or not secret:
        return False
    return hmac.compare_digest(sign_event(secret, **kw), signature)


@dataclass
class RawEvent:
    """Trusted identity extracted from an authentic Telegram update."""

    kind: str  # 'message' | 'edit' | 'callback'
    bot_id: str
    chat_id: str
    message_id: str
    user_id: str
    text: str = ""
    update_id: Optional[str] = None
    callback_data: Optional[str] = None

    @property
    def key(self) -> str:
        return event_key(self.bot_id, self.chat_id, self.message_id)

    def to_payload(self) -> dict:
        return {
            "v": ENVELOPE_VERSION,
            "service": "telegram-bridge",
            "kind": self.kind,
            "bot_id": self.bot_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "user_id": self.user_id,
            "text": self.text,
            "update_id": self.update_id,
            "callback_data": self.callback_data,
            "digest": content_digest(self.text),
        }


def extract_event(update: dict, bot_id: str) -> Optional[RawEvent]:
    """Extract trusted identity from an authentic Telegram update.

    Handles:
      * message          -> kind='message'
      * edited_message   -> kind='edit'  (a REVISION of the same message id)
      * callback_query   -> kind='callback' (references the original operation)

    Returns None for updates that do not carry a consumption-relevant event.
    """
    if not isinstance(update, dict):
        return None

    update_id = update.get("update_id")
    update_id_s = str(update_id) if update_id is not None else None

    cb = update.get("callback_query")
    if isinstance(cb, dict):
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        from_id = (cb.get("from") or {}).get("id")
        if chat_id is None or from_id is None:
            return None
        # The callback's own message id is NOT a consumption identity; the
        # callback_data references the original operation instead.
        return RawEvent(
            kind="callback",
            bot_id=str(bot_id),
            chat_id=str(chat_id),
            message_id=str(cb.get("id") or msg.get("message_id") or ""),
            user_id=str(from_id),
            text="",
            update_id=update_id_s,
            callback_data=cb.get("data") or "",
        )

    for message_key, kind in (("message", "message"), ("edited_message", "edit")):
        message = update.get(message_key)
        if not isinstance(message, dict):
            continue
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        from_id = (message.get("from") or {}).get("id")
        if chat_id is None or message_id is None or from_id is None:
            return None
        text = (message.get("text") or message.get("caption") or "").strip()
        return RawEvent(
            kind=kind,
            bot_id=str(bot_id),
            chat_id=str(chat_id),
            message_id=str(message_id),
            user_id=str(from_id),
            text=text,
            update_id=update_id_s,
        )

    return None


class IngressClient:
    """Delivers signed trusted events to the API ingress worker."""

    def __init__(
        self,
        base_url: str,
        secret: str,
        service_token: str = "",
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.service_token = service_token
        self.timeout = timeout

    def deliver(self, event: RawEvent) -> dict:
        payload = event.to_payload()
        signature = sign_event(
            self.secret,
            service=payload["service"],
            bot_id=event.bot_id,
            chat_id=event.chat_id,
            message_id=event.message_id,
            user_id=event.user_id,
            digest=payload["digest"],
            kind=event.kind,
        )
        headers = {
            "X-DrHiro-Ingress-Signature": signature,
            "Content-Type": "application/json",
        }
        if self.service_token:
            headers["Authorization"] = f"Bearer {self.service_token}"

        resp = httpx.post(
            f"{self.base_url}/api/v1/ingest/telegram/event",
            json=payload,
            headers=headers,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()
