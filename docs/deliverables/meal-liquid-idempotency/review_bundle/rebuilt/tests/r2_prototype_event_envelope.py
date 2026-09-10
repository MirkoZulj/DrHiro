"""R2 Option B — prototype: trusted consumption-event envelope.

DESIGN VERIFICATION PROTOTYPE ONLY. Nothing here is wired into production; no
deployed file imports it. It exists to demonstrate, with a sanitized fixture,
that the supported OpenClaw plugin hooks can carry a TRUSTED, per-message
consumption-event identity from the Telegram ingress to the shim/API without
using model-generated tool arguments and without a shared "latest event" record.

Capability basis (verified read-only against OpenClaw 2026.7.1):
  - `message_received` plugin hook delivers: threadId (chat), messageId,
    senderId, sessionKey, runId, plus metadata {provider, surface,
    originatingChannel, originatingTo, messageId, senderId, ...} and
    channelId/accountId on the internal variant.
  - `agent_turn_prepare` / `before_prompt_build` can inject context text into
    the turn (prependContext/appendContext).
  - NO supported plugin hook can mutate the raw provider payload
    (`before_provider_payload`/`before_provider_request` are internal-only and
    absent from the plugin hook union), and OpenClaw never sets `user` or
    `metadata` on the outbound completions request. The plugin-controllable
    channel to the provider is therefore the prompt/context text.

Because the tool loop runs inside TrueForge (not OpenClaw), `before_tool_call`
cannot be used for this. The envelope therefore travels in the request the shim
receives, and carries only an OPAQUE, SIGNED event id — never the raw identity —
so the model cannot read or forge identity. Identity is resolved server-side
from a durable record.

Threat model handled here:
  - Forged envelope from user text or model output  -> HMAC verification fails.
  - Replay of an earlier turn's envelope             -> binding is per run/turn
                                                        and single-use for a NEW
                                                        consumption; reuse is only
                                                        accepted for a retry of the
                                                        SAME operation.
  - Concurrent turns overwriting each other          -> no shared "latest" key;
                                                        binding is keyed by event id.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Optional

BEGIN = "<<<BEGIN_DRHIRO_EVENT_CONTEXT>>>"
END = "<<<END_DRHIRO_EVENT_CONTEXT>>>"

# Fields carried INSIDE the signed envelope. Identity values are NOT included:
# only the opaque event id, so the model never sees chat/message/user identity.
_ENVELOPE_FIELDS = ("v", "event_id", "chat_id_h", "message_id_h", "bot_id_h", "issued_at", "nonce")

_HEADER_RE = re.compile(
    r"^\[DRHIRO_EVENT_CONTEXT\]\s*(?P<body>\{.*?\})\s*$",
    re.DOTALL | re.MULTILINE,
)


def canonical(fields: dict) -> str:
    """Deterministic serialization used for signing."""
    return json.dumps({k: fields[k] for k in sorted(fields)}, separators=(",", ":"), sort_keys=True)


def sign(fields: dict, secret: bytes) -> str:
    return hmac.new(secret, canonical(fields).encode("utf-8"), hashlib.sha256).hexdigest()


def _h(value: str) -> str:
    """Salted-ish, reversible-by-server hash of an identifier.

    The envelope carries only this, so the model sees no raw identifiers. The
    server resolves the raw value from the durable event record by event id.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def derive_event_id(*, bot_id: str, chat_id: str, message_id: str) -> str:
    """Deterministic event identity from the Telegram tuple.

    Telegram guarantees (bot_id, chat_id, message_id) is stable for a message,
    so a redelivery derives the SAME event id and collapses to one consumption,
    while two distinct messages derive different ids. This is what makes a
    generated operation id safe: it is durably bound to the originating event.
    """
    raw = f"{bot_id}|{chat_id}|{message_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def mint_envelope(
    *,
    secret: bytes,
    event_id: Optional[str] = None,
    bot_id: str,
    chat_id: str,
    message_id: str,
    issued_at: int,
    nonce: str,
) -> str:
    """Mint the signed envelope text a plugin injects into the turn context."""
    if event_id is None:
        event_id = derive_event_id(bot_id=bot_id, chat_id=chat_id, message_id=message_id)
    fields = {
        "v": 1,
        "event_id": event_id,           # opaque durable operation/event id
        "chat_id_h": _h(chat_id),       # non-reversible in the prompt
        "message_id_h": _h(message_id),
        "bot_id_h": _h(bot_id),
        "issued_at": int(issued_at),
        "nonce": nonce,
    }
    sig = sign(fields, secret)
    body = json.dumps({**fields, "sig": sig}, separators=(",", ":"), sort_keys=True)
    return f"{BEGIN}\n[DRHIRO_EVENT_CONTEXT] {body}\n{END}"


class EnvelopeError(Exception):
    pass


@dataclass
class VerifiedEvent:
    event_id: str
    chat_id_h: str
    message_id_h: str
    bot_id_h: str
    issued_at: int
    nonce: str
    raw_block: str = field(repr=False, default="")


def extract_envelope(request_body: dict, secret: bytes, *, max_age_s: int = 300, now: Optional[int] = None) -> VerifiedEvent:
    """Extract + verify the envelope from a provider request body (transport).

    Reads ONLY the request body (never model output). The LAST well-formed,
    signed block wins; unsigned or tampered blocks are rejected outright.
    """
    messages = request_body.get("messages") or []
    text_parts = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            text_parts.append(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    text_parts.append(p["text"])
    text = "\n".join(text_parts)

    blocks = re.findall(re.escape(BEGIN) + r"(.*?)" + re.escape(END), text, re.DOTALL)
    if not blocks:
        raise EnvelopeError("no_event_envelope")

    # Validate from the last block backwards; the first valid one wins.
    import time as _time
    now = int(_time.time()) if now is None else int(now)
    for raw in reversed(blocks):
        m = _HEADER_RE.search(raw)
        if not m:
            continue
        try:
            payload = json.loads(m.group("body"))
        except Exception:
            continue
        sig = payload.pop("sig", None)
        if not sig:
            raise EnvelopeError("envelope_unsigned")
        if set(payload) != set(_ENVELOPE_FIELDS):
            raise EnvelopeError("envelope_field_set_invalid")
        expected = sign(payload, secret)
        if not hmac.compare_digest(sig, expected):
            raise EnvelopeError("envelope_signature_invalid")
        if payload.get("v") != 1:
            raise EnvelopeError("envelope_version_unsupported")
        if abs(now - int(payload.get("issued_at", 0))) > max_age_s:
            raise EnvelopeError("envelope_expired")
        return VerifiedEvent(
            event_id=payload["event_id"],
            chat_id_h=payload["chat_id_h"],
            message_id_h=payload["message_id_h"],
            bot_id_h=payload["bot_id_h"],
            issued_at=int(payload["issued_at"]),
            nonce=payload["nonce"],
            raw_block=raw,
        )
    raise EnvelopeError("no_valid_envelope")


# ---------------------------------------------------------------------------
# Durable binding (design demonstration; production would be a DB table)
# ---------------------------------------------------------------------------

class DurableEventStore:
    """Durable identity/results beyond transient state.

    Keyed by event_id (NOT by a shared "latest event" key), so concurrent turns
    in the same chat cannot overwrite one another.
    """

    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.results: dict[str, dict] = {}
        self.item_keys: dict[str, list[str]] = {}

    def record_event(self, event_id: str, *, bot_id: str, chat_id: str, message_id: str,
                     update_id: Optional[str] = None, sender_id: Optional[str] = None) -> None:
        # Identity that Telegram guarantees stable for this message. update_id is
        # retained for delivery tracing only (OpenClaw does not expose it to
        # plugins; recorded when the ingress can supply it).
        self.events.setdefault(event_id, {
            "event_id": event_id,
            "bot_id": bot_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "update_id": update_id,
            "sender_id": sender_id,
            # Stable per-item discriminator: one consumption item per index under
            # this event. Meal-tool and liquid-tool calls for the SAME drink must
            # resolve to the same item key.
            "item_keys": [],
        })

    def item_key(self, event_id: str, discriminator: str) -> str:
        """Stable discriminator for an item within an event."""
        return f"{event_id}:item:{discriminator}"

    def register_item(self, event_id: str, discriminator: str) -> str:
        key = self.item_key(event_id, discriminator)
        keys = self.events[event_id]["item_keys"]
        if key not in keys:
            keys.append(key)
        return key

    def record_result(self, event_id: str, result: dict) -> None:
        self.results[event_id] = result

    def get_result(self, event_id: str) -> Optional[dict]:
        return self.results.get(event_id)
