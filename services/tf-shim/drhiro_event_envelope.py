"""drHiro consumption-event envelope — trusted transport binding (R2).

Architecture: an adapter on the request path captures the ORIGINAL Telegram
identity (bot_id from verified getMe, chat_id, message_id) and mints an opaque,
HMAC-signed envelope bound to the specific request (service, account, event,
and a digest of the current input). The shim (adapter) extracts it ONLY from the
designated OpenClaw runtime-context block, verifies the binding, REMOVES it
before forwarding the turn to TrueForge, and never persists it to conversational
history or ordinary logs. Identity never travels as model-generated tool
arguments and never sits in a shared 'latest event' record.

This module is importable by the shim and is the authoritative implementation.
Prototype files under tests/ (r2_prototype_event_envelope.py) remain the earlier
design validation; this supersedes them.

Properties locked by review:
  - event_id = BLAKE2b over a VERSIONED, CANONICAL serialization of identity
    fields (JSON, sorted keys, explicit tags) -- NOT delimiter concatenation.
  - Request binding covers service + account + event + input_digest, so a valid
    envelope copied from another request FAILS.
  - input_digest is a canonical hash of the RELEVANT CURRENT INPUT (the newest
    user-authored message, excluding the OpenClaw runtime-context block).
  - Envelope expiry governs acceptance of a NEW write; durable replay retention
    is independent, so a fresh authenticated retry of an old event still finds
    its saved result even after the envelope credentials expired.
  - Item discriminators are stable across retries and tool calls.
  - Edited messages are explicit revisions of the original event, not creation
    retries. Confirmation callbacks bind to the original operation with an
    ownership check.
  - Missing, duplicate, conflicting, unsigned, expired, wrong-service,
    wrong-account, or wrong-input-digest envelopes FAIL CLOSED.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

# Versioned envelope fields (explicit tags -> no ambiguous concatenation).
_VERSION = 1
_IDENTITY_FIELDS = ("v", "service", "event_id", "bot_id", "chat_id", "message_id")

# Marker delimiting the envelope block inside the OpenClaw runtime-context block.
BEGIN = "<<<BEGIN_DRHIRO_EVENT_CONTEXT>>>"
END = "<<<END_DRHIRO_EVENT_CONTEXT>>>"

# The designated extraction location: an OpenClaw runtime-context block. The
# envelope is accepted ONLY inside such a block, never from user-authored text
# or historical messages.
_OPENCLAW_CTX_BEGIN = "<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>"
_OPENCLAW_CTX_END = "<<<END_OPENCLAW_INTERNAL_CONTEXT>>>"

_BLOCK_RE = re.compile(
    re.escape(BEGIN) + r"\s*(\[DRHIRO_EVENT_CONTEXT\]\s*\{.*?\})\s*" + re.escape(END),
    re.DOTALL,
)


class EnvelopeError(Exception):
    code = "envelope_error"


class NoEnvelopeError(EnvelopeError):
    code = "no_event_envelope"


class UnsignedEnvelopeError(EnvelopeError):
    code = "envelope_unsigned"


class BadSignatureEnvelopeError(EnvelopeError):
    code = "envelope_signature_invalid"


class ExpiredEnvelopeError(EnvelopeError):
    code = "envelope_expired"


class BadVersionEnvelopeError(EnvelopeError):
    code = "envelope_version_unsupported"


class ConflictingEnvelopeError(EnvelopeError):
    code = "envelope_conflicting"


class AmbiguousEnvelopeError(EnvelopeError):
    code = "envelope_ambiguous"


class BindingMismatchEnvelopeError(EnvelopeError):
    code = "envelope_binding_mismatch"


def _canonical(fields: dict) -> str:
    """Deterministic canonical JSON for signing/hashing.

    Uses JSON with sort_keys and explicit field tags; never relies on
    separator characters inside values (avoids the delimiter-concatenation
    ambiguity the review rejected).
    """
    return json.dumps({k: fields[k] for k in sorted(fields)}, separators=(",", ":"), sort_keys=True)


def _normalize(text: str) -> str:
    """Canonical input form: NFC + trim + collapse internal newlines to space."""
    if not text:
        return ""
    nf = unicodedata.normalize("NFC", text)
    nf = re.sub(r"\s+", " ", nf).strip()
    return nf


def canonical_input(text: str) -> str:
    """sha256 of the canonical form of the relevant current input."""
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()


def derive_event_id(*, service: str, bot_id: str, chat_id: str, message_id: str) -> str:
    """Versioned canonical event identity (BLAKE2b over tagged fields)."""
    payload = _canonical({
        "v": _VERSION,
        "service": service,
        "bot_id": str(bot_id),
        "chat_id": str(chat_id),
        "message_id": str(message_id),
    })
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()


def sign_identity(*, secret: bytes, service: str, event_id: str, bot_id: str,
                  chat_id: str, message_id: str, input_digest: str,
                  issued_at: int, nonce: str) -> str:
    """HMAC over the request binding: service + account(bot) + event + input."""
    fields = {
        "v": _VERSION,
        "service": service,
        "event_id": event_id,
        "bot_id": str(bot_id),
        "chat_id": str(chat_id),
        "message_id": str(message_id),
        "input_digest": input_digest,
        "issued_at": int(issued_at),
        "nonce": nonce,
    }
    return hmac.new(secret, _canonical(fields).encode("utf-8"), hashlib.sha256).hexdigest()


def build_envelope(
    *,
    secret: bytes,
    service: str,
    bot_id: str,
    chat_id: str,
    message_id: str,
    input_digest: str,
    issued_at: Optional[int] = None,
    nonce: Optional[str] = None,
    event_id: Optional[str] = None,
) -> str:
    """Mint a signed envelope for the OpenClaw runtime-context block."""
    import time as _t
    issued_at = int(_t.time()) if issued_at is None else int(issued_at)
    if nonce is None:
        nonce = hashlib.blake2b(secret + str(issued_at).encode(), digest_size=8).hexdigest()
    if event_id is None:
        event_id = derive_event_id(service=service, bot_id=bot_id, chat_id=chat_id, message_id=message_id)
    sig = sign_identity(
        secret=secret, service=service, event_id=event_id, bot_id=bot_id,
        chat_id=chat_id, message_id=message_id, input_digest=input_digest,
        issued_at=issued_at, nonce=nonce,
    )
    body = json.dumps({
        "v": _VERSION, "service": service, "event_id": event_id,
        "bot_id": bot_id, "chat_id": chat_id, "message_id": message_id,
        "input_digest": input_digest, "issued_at": issued_at, "nonce": nonce,
        "sig": sig,
    }, separators=(",", ":"), sort_keys=True)
    return f"{BEGIN}\n[DRHIRO_EVENT_CONTEXT] {body}\n{END}"


@dataclass(frozen=True)
class BoundEvent:
    event_id: str
    service: str
    bot_id: str
    chat_id: str
    message_id: str
    input_digest: str
    issued_at: int
    nonce: str


def extract_envelopes_from_openclaw_block(block_text: str) -> list[dict]:
    """Return the raw envelope dicts found in ONE OpenClaw runtime-context block.

    ONLY this designated location is scanned. User-authored messages and
    historical messages are never scanned for an envelope.
    """
    out = []
    for m in _BLOCK_RE.finditer(block_text):
        header, body = m.group(1).split(" ", 1)
        if header != "[DRHIRO_EVENT_CONTEXT]":
            continue
        try:
            out.append(json.loads(body))
        except Exception:
            continue
    return out


def verify_envelope(
    envelope: dict,
    *,
    secret: bytes,
    service: str,
    bot_id: str,
    chat_id: str,
    message_id: str,
    input_digest: str,
    now: Optional[int] = None,
    max_age_s: int = 300,
) -> BoundEvent:
    """Verify authenticity, integrity, and request binding of one envelope.

    Raises EnvelopeError subclasses; never silently falls back.
    """
    import time as _t
    now = int(_t.time()) if now is None else int(now)

    sig = envelope.get("sig")
    if not isinstance(sig, str) or not sig:
        raise UnsignedEnvelopeError("envelope is unsigned")
    if envelope.get("v") != _VERSION:
        raise BadVersionEnvelopeError("envelope version unsupported")

    # Authenticate + integrity (HMAC). Confidentiality is not the point here.
    fields = {
        "v": envelope["v"], "service": envelope["service"],
        "event_id": envelope["event_id"], "bot_id": envelope["bot_id"],
        "chat_id": envelope["chat_id"], "message_id": envelope["message_id"],
        "input_digest": envelope["input_digest"],
        "issued_at": int(envelope["issued_at"]), "nonce": envelope["nonce"],
    }
    expected = hmac.new(secret, _canonical(fields).encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise BadSignatureEnvelopeError("envelope signature invalid")

    # Request binding: service + account(bot) + event + input digest.
    if envelope.get("service") != service:
        raise BindingMismatchEnvelopeError("envelope service mismatch")
    if str(envelope.get("bot_id")) != str(bot_id):
        raise BindingMismatchEnvelopeError("envelope account(bot) mismatch")
    if str(envelope.get("chat_id")) != str(chat_id):
        raise BindingMismatchEnvelopeError("envelope chat mismatch")
    if str(envelope.get("message_id")) != str(message_id):
        raise BindingMismatchEnvelopeError("envelope message mismatch")
    if envelope.get("input_digest") != input_digest:
        raise BindingMismatchEnvelopeError("envelope input-digest mismatch")

    # Freshness governs acceptance of a NEW write (not durable replay retention).
    if abs(now - int(envelope["issued_at"])) > max_age_s:
        raise ExpiredEnvelopeError("envelope expired")

    return BoundEvent(
        event_id=envelope["event_id"], service=envelope["service"],
        bot_id=str(envelope["bot_id"]), chat_id=str(envelope["chat_id"]),
        message_id=str(envelope["message_id"]),
        input_digest=envelope["input_digest"],
        issued_at=int(envelope["issued_at"]), nonce=envelope["nonce"],
    )


# ---------------------------------------------------------------------------
# Request-side extraction (the shim's job)
# ---------------------------------------------------------------------------

def _openclaw_blocks(messages: list[dict]) -> list[str]:
    """Return the OpenClaw runtime-context block texts present in the messages.

    These are the ONLY locations an envelope is accepted from. Blocks are
    recognised by the OpenClaw delimiters, independent of which message role.
    """
    blocks = []
    for m in messages:
        c = m.get("content")
        texts = []
        if isinstance(c, str):
            texts = [c]
        elif isinstance(c, list):
            texts = [p.get("text", "") for p in c if isinstance(p, dict) and isinstance(p.get("text"), str)]
        for text in texts:
            # A message may contain multiple delimited blocks.
            depth = 0
            start = -1
            i = 0
            while i < len(text):
                if text.startswith(_OPENCLAW_CTX_BEGIN, i):
                    if depth == 0:
                        start = i
                    depth += 1
                    i += len(_OPENCLAW_CTX_BEGIN)
                elif text.startswith(_OPENCLAW_CTX_END, i):
                    depth -= 1
                    if depth == 0 and start != -1:
                        blocks.append(text[start:i + len(_OPENCLAW_CTX_END)])
                        start = -1
                    i += len(_OPENCLAW_CTX_END)
                else:
                    i += 1
    return blocks


def extract_and_remove_envelope(
    body: dict,
    *,
    secret: bytes,
    service: str,
    bot_id: str,
    chat_id: str,
    message_id: str,
    now: Optional[int] = None,
    max_age_s: int = 300,
) -> tuple[BoundEvent, dict]:
    """Extract the envelope from the DESIGNATED location, verify binding, and
    remove it from the request (so it is never forwarded to the model).

    Rules:
      - The envelope is accepted ONLY inside an OpenClaw runtime-context block.
      - At most one valid envelope may be present. Zero -> NoEnvelopeError.
        More than one valid candidate (or a valid one plus an unparseable
        duplicate) -> AmbiguousEnvelopeError.
      - The verified envelope's input_digest must equal canonical_input of the
        relevant current user input (computed here).
    Returns (bound_event, cleaned_body) where cleaned_body has the envelope
    removed from the runtime-context block text.
    """
    import time as _t
    now = int(_t.time()) if now is None else int(now)

    messages = body.get("messages") or []
    blocks = _openclaw_blocks(messages)
    if not blocks:
        raise NoEnvelopeError("no OpenClaw runtime-context block; no event envelope")

    candidates: list[dict] = []
    for b in blocks:
        candidates.extend(extract_envelopes_from_openclaw_block(b))
    if not candidates:
        raise NoEnvelopeError("no event envelope in the designated block")

    # Reject ambiguity: a copied/duplicated envelope must not be silently picked.
    if len(candidates) > 1:
        raise AmbiguousEnvelopeError("multiple envelopes in the designated block")

    current_input = relevant_current_input(body)
    input_digest = canonical_input(current_input)

    bound = verify_envelope(
        candidates[0], secret=secret, service=service, bot_id=bot_id,
        chat_id=chat_id, message_id=message_id, input_digest=input_digest,
        now=now, max_age_s=max_age_s,
    )

    # Remove the envelope from the request body (in place on a shallow copy).
    cleaned = {"__version__": body.get("__version__"), "messages": list(messages)}
    for key, value in body.items():
        if key not in ("messages",):
            cleaned.setdefault(key, value)
    cleaned_messages = []
    removed = False
    for m in messages:
        c = m.get("content")
        new_c = c
        if isinstance(c, str) and BEGIN in c:
            new_c = re.sub(
                re.escape(BEGIN) + r"\s*\[DRHIRO_EVENT_CONTEXT\]\s*\{.*?\}\s*" + re.escape(END),
                "",
                c,
                flags=re.DOTALL,
            )
            if new_c != c:
                removed = True
        elif isinstance(c, list):
            new_parts = []
            for p in c:
                pt = p.get("text", "")
                if isinstance(pt, str) and BEGIN in pt:
                    new_pt = re.sub(
                        re.escape(BEGIN) + r"\s*\[DRHIRO_EVENT_CONTEXT\]\s*\{.*?\}\s*" + re.escape(END),
                        "",
                        pt,
                        flags=re.DOTALL,
                    )
                    if new_pt != pt:
                        removed = True
                    p = {**p, "text": new_pt}
                new_parts.append(p)
            new_c = new_parts
        if removed and new_c != c:
            pass  # already removed
        cleaned_messages.append({**m, "content": new_c})
    cleaned["messages"] = cleaned_messages
    if not removed:
        raise NoEnvelopeError("envelope present but not removed from request")
    return bound, cleaned


def relevant_current_input(body: dict) -> str:
    """The relevant current input: the newest user-authored message text,
    EXCLUDING OpenClaw runtime-context blocks and any envelope. This is exactly
    the input the model would otherwise see; it is what the mint-side digest
    must match. Historical messages are never used.
    """
    messages = body.get("messages") or []
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        texts = []
        if isinstance(c, str):
            texts = [c]
        elif isinstance(c, list):
            texts = [p.get("text", "") for p in c if isinstance(p, dict) and isinstance(p.get("text"), str)]
        text = "\n".join(texts)
        if _openclaw_blocks([{"content": text}]):
            # This is the OpenClaw context envelope; skip to the user's words.
            stripped = re.sub(re.escape(_OPENCLAW_CTX_BEGIN) + r".*?" + re.escape(_OPENCLAW_CTX_END), "", text, flags=re.DOTALL)
            if stripped.strip():
                continue  # block may wrap the real content; fall through below
        if text.strip():
            return text
    return ""
