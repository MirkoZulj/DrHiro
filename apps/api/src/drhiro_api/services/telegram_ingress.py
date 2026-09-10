"""R2 / T1 — trusted Telegram ingress worker.

Owns the trust boundary for consumption events:
  * identity  = versioned canonical (bot_id, chat_id, message_id)  -> event_key
  * content   = separate digest (payload_hash) for conflict detection
  * receipt   = durable PostgreSQL row BEFORE processing
  * model     = UNTRUSTED proposal source, bound to the operation it serves
  * write     = atomic, server-side nutrient resolution, single path

The model may propose quantities/food matches. It never chooses the user, the
event identity, operation ownership, or authorization, and its output is
validated before use and never trusted for nutrition values.

See docs/deliverables/meal-liquid-idempotency/T1_ingress_design_and_writer_ownership.md
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from ..models import (
    BeverageMeasurement,
    ConsumptionItem,
    ConsumptionOperation,
    ExternalIdentity,
    Meal,
    MealItem,
    Measurement,
    User,
)
from .consumption import (
    NUTRIENT_KEYS,
    ParsedItem,
    _normalize_meal_type,
    get_or_create_operation,
    resolve_item_nutrition,
    write_consumption,
)

# ---------------------------------------------------------------------------
# Identity: versioned, canonical, unambiguous
# ---------------------------------------------------------------------------

IDENTITY_VERSION = "v1"
ENVELOPE_VERSION = 1

BEVERAGE_CATEGORIES = {
    "water", "non_alcoholic", "beer", "wine", "spirits", "other_alcohol",
}

# Bounds on untrusted model output.
MAX_ITEMS = 40
MAX_QUANTITY_GRAMS = 20_000.0
MAX_QUANTITY_ML = 20_000.0


def canonical_identity(bot_id: str, chat_id: str, message_id: str) -> str:
    """Versioned canonical identity encoding.

    Canonical JSON (sorted keys, no whitespace) rather than delimiter
    concatenation, so values containing delimiters cannot be confused.
    """
    return json.dumps(
        {
            "v": IDENTITY_VERSION,
            "bot": str(bot_id),
            "chat": str(chat_id),
            "msg": str(message_id),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def event_key(bot_id: str, chat_id: str, message_id: str) -> str:
    """Stable event key derived ONLY from identity — never from content."""
    payload = canonical_identity(bot_id, chat_id, message_id).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def content_digest(text: str) -> str:
    """Digest of the message content, stored separately from identity.

    Used only for conflict detection, never as an identity component.
    """
    normalized = " ".join((text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Trusted transport envelope (bridge -> API)
# ---------------------------------------------------------------------------

def _envelope_message(
    *,
    service: str,
    bot_id: str,
    chat_id: str,
    message_id: str,
    user_id: str,
    digest: str,
    kind: str,
) -> bytes:
    return json.dumps(
        {
            "v": ENVELOPE_VERSION,
            "service": str(service),
            "bot": str(bot_id),
            "chat": str(chat_id),
            "msg": str(message_id),
            "user": str(user_id),
            "digest": str(digest),
            "kind": str(kind),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sign_envelope(secret: str, **kw: str) -> str:
    """Sign the trusted identity fields with the shared secret."""
    return hmac.new(
        secret.encode("utf-8"), _envelope_message(**kw), hashlib.sha256
    ).hexdigest()


def verify_envelope(secret: str, signature: str, **kw: str) -> bool:
    """Constant-time verification. Invalid/absent signature fails closed."""
    if not signature or not secret:
        return False
    expected = sign_envelope(secret, **kw)
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@dataclass
class TrustedEvent:
    """An event whose identity came from authenticated transport.

    `telegram_user_id` is the authenticated Telegram sender. It is NOT the
    internal drHiro user id: the worker resolves it (fail closed) before any
    write, so identity never comes from the model.
    """

    kind: str  # 'message' | 'edit' | 'callback'
    bot_id: str
    chat_id: str
    message_id: str
    telegram_user_id: str = ""
    text: str = ""
    update_id: Optional[str] = None
    callback_data: Optional[str] = None
    user_id: Optional[str] = None  # resolved internal user id

    @property
    def key(self) -> str:
        return event_key(self.bot_id, self.chat_id, self.message_id)


class IngressConflict(Exception):
    """Same event identity, different content, not an authenticated edit."""


class IngressRejected(Exception):
    """The trusted transport envelope is absent, malformed, or invalid."""


def accept_signed_event(secret: str, payload: dict, trusted_bot_id: str = "") -> TrustedEvent:
    """Verify a bridge-signed payload and build a TrustedEvent.

    Fails closed: a missing/blank secret, a missing trusted bot id, a missing
    signature, a malformed payload, a bot id that is not the verified one, or a
    signature that does not match the identity+content actually received is
    rejected. The content digest is recomputed here from the received text, so
    a payload signed for different content does not pass.
    """
    if not secret:
        raise IngressRejected("ingress_secret_not_configured")
    if not trusted_bot_id:
        # The verified getMe.id must be known; otherwise we cannot tell whose
        # account an envelope was minted for.
        raise IngressRejected("trusted_bot_id_not_configured")
    if not isinstance(payload, dict):
        raise IngressRejected("payload_not_an_object")

    required = ("bot_id", "chat_id", "message_id", "user_id", "kind", "service")
    missing = [k for k in required if not str(payload.get(k) or "").strip()]
    if missing:
        raise IngressRejected(f"missing_fields:{','.join(missing)}")

    if str(payload["bot_id"]) != str(trusted_bot_id):
        # An envelope minted for another bot account is not ours to honour.
        raise IngressRejected("cross_account_bot_mismatch")

    kind = str(payload["kind"])
    if kind not in ("message", "edit", "callback"):
        raise IngressRejected(f"unknown_kind:{kind}")

    text = str(payload.get("text") or "")
    received_digest = content_digest(text)
    claimed_digest = str(payload.get("digest") or "")
    if claimed_digest and claimed_digest != received_digest:
        raise IngressRejected("content_digest_mismatch")

    signature = str(payload.get("signature") or "")
    ok = verify_envelope(
        secret,
        signature,
        service=str(payload["service"]),
        bot_id=str(payload["bot_id"]),
        chat_id=str(payload["chat_id"]),
        message_id=str(payload["message_id"]),
        user_id=str(payload["user_id"]),
        digest=received_digest,
        kind=kind,
    )
    if not ok:
        raise IngressRejected("invalid_signature")

    return TrustedEvent(
        kind=kind,
        bot_id=str(payload["bot_id"]),
        chat_id=str(payload["chat_id"]),
        message_id=str(payload["message_id"]),
        telegram_user_id=str(payload["user_id"]),
        text=text,
        update_id=payload.get("update_id"),
        callback_data=payload.get("callback_data") or "",
    )


# ---------------------------------------------------------------------------
# Validation of UNTRUSTED model proposals
# ---------------------------------------------------------------------------

def _finite_positive(value: Any, maximum: float) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if num <= 0 or num > maximum:
        return None
    return num


def validate_proposals(proposals: Any) -> tuple[list[ParsedItem], list[str]]:
    """Turn untrusted proposals into validated ParsedItems.

    Rejects rather than repairs anything malformed. Nutrition is deliberately
    NOT taken from the proposal — it is resolved server-side later.
    """
    rejected: list[str] = []
    accepted: list[ParsedItem] = []

    if not isinstance(proposals, dict):
        return [], ["proposals_not_an_object"]
    raw_items = proposals.get("items")
    if not isinstance(raw_items, list):
        return [], ["items_not_a_list"]
    if len(raw_items) > MAX_ITEMS:
        return [], ["too_many_items"]

    for pos, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            rejected.append(f"item[{pos}]:not_an_object")
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            rejected.append(f"item[{pos}]:missing_name")
            continue
        name = name.strip()[:255]

        category = raw.get("category")
        is_beverage = bool(raw.get("is_beverage")) or (
            isinstance(category, str) and category in BEVERAGE_CATEGORIES
        )

        grams = _finite_positive(raw.get("grams"), MAX_QUANTITY_GRAMS)
        volume_ml = _finite_positive(
            raw.get("volume_ml") if raw.get("volume_ml") is not None else raw.get("ml"),
            MAX_QUANTITY_ML,
        )

        if is_beverage:
            if isinstance(category, str) and category in BEVERAGE_CATEGORIES:
                bev_category = category
            else:
                bev_category = "water"
            if volume_ml is None:
                # A beverage with no volume carries no information.
                rejected.append(f"item[{pos}]:beverage_without_volume")
                continue
            accepted.append(
                ParsedItem(
                    display_name=name,
                    quantity=_finite_positive(raw.get("quantity"), 1000.0) or 1.0,
                    unit="ml",
                    grams=None,
                    volume_ml=volume_ml,
                    beverage_category=bev_category,
                    is_beverage=True,
                    source="model_proposal",
                    confidence=0.8,
                )
            )
            continue

        if grams is None:
            rejected.append(f"item[{pos}]:food_without_grams")
            continue
        accepted.append(
            ParsedItem(
                display_name=name,
                quantity=_finite_positive(raw.get("quantity"), 1000.0) or 1.0,
                unit="g",
                grams=grams,
                volume_ml=None,
                is_beverage=False,
                source="model_proposal",
                confidence=0.8,
            )
        )

    return accepted, rejected


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


class TrustedIngressWorker:
    """Owns receipt, binding, validation, resolution, and the atomic write."""

    def __init__(
        self,
        db: Session,
        proposer: Optional[Callable[[str, str], Any]] = None,
    ) -> None:
        self.db = db
        self._proposer = proposer

    # -- model binding ---------------------------------------------------- #

    def _propose(self, operation_id: str, text: str) -> Any:
        """Ask the model for proposals, BOUND to this operation.

        The operation id is passed as correlation so the caller can supply a
        turn/run-scoped context; the reply is untrusted either way.
        """
        if self._proposer is None:
            return {"items": []}
        return self._proposer(operation_id, text)

    # -- entry points ----------------------------------------------------- #

    def resolve_user(self, event: TrustedEvent) -> str:
        """Resolve the authenticated Telegram sender to the internal user.

        Fails closed: an unknown or inactive sender is rejected. The model can
        never influence this mapping.
        """
        if event.user_id:
            return event.user_id
        if not event.telegram_user_id:
            raise IngressRejected("missing_authenticated_sender")
        user = self._lookup_user(event.telegram_user_id)
        if user is None or getattr(user, "status", "active") != "active":
            raise IngressRejected("unauthenticated_sender")
        event.user_id = str(user.id)
        return event.user_id

    def _lookup_user(self, telegram_id: str):
        """Map an authenticated Telegram sender to the internal user.

        Uses the same linkage the API's auth layer uses (external_identities,
        provider='telegram').
        """
        identity = (
            self.db.query(ExternalIdentity)
            .filter(
                ExternalIdentity.provider == "telegram",
                ExternalIdentity.provider_subject == str(telegram_id),
            )
            .first()
        )
        if identity is None:
            return None
        return self.db.get(User, identity.user_id)

    def handle(self, event: TrustedEvent) -> dict:
        self.resolve_user(event)
        if event.kind == "callback":
            return self.handle_callback(event)
        return self.handle_text(event)

    def handle_text(self, event: TrustedEvent) -> dict:
        """A message (or an authenticated edit) -> receipt, then process."""
        digest = content_digest(event.text)

        try:
            op, created = get_or_create_operation(
                self.db,
                user_id=event.user_id,
                source="telegram",
                source_chat_id=event.chat_id,
                source_message_id=event.message_id,
                source_bot_id=event.bot_id,
                raw_text=event.text,
                payload_hash=digest,
            )
        except ValueError as exc:
            # Different content under the same identity. An edit is an explicit
            # revision; anything else is an unexplained conflict.
            if event.kind == "edit":
                return self._apply_revision(event, digest)
            raise IngressConflict(str(exc)) from exc

        if not created:
            # Redelivery / retry of a known event.
            if event.kind == "edit":
                if op.payload_hash == digest:
                    # An edit that changed nothing is not a revision.
                    return {
                        "status": "replayed",
                        "operation_id": str(op.id),
                        "result": op.result_json,
                    }
                return self._apply_revision(event, digest, existing=op)

            if op.payload_hash and op.payload_hash != digest:
                raise IngressConflict(
                    "conflicting_payload_reuse: same event identity, different content"
                )

            if op.status == "completed" and op.result_json:
                # Durable replay — no second consumption.
                return {
                    "status": "replayed",
                    "operation_id": str(op.id),
                    "result": op.result_json,
                }

        op.status = "processing"
        op.updated_at = _now()
        self.db.flush()

        proposals = self._propose(str(op.id), event.text)
        items, rejected = validate_proposals(proposals)

        if not items:
            if rejected:
                # The model proposed something we could not trust -> ask.
                op.status = "needs_clarification"
                op.result_json = {"ok": False, "rejected": rejected}
                self.db.commit()
                return {
                    "status": "needs_clarification",
                    "operation_id": str(op.id),
                    "rejected": rejected,
                }
            # Nothing consumption-shaped at all: leave this turn to the
            # conversational path. We do NOT guess a consumption.
            op.status = "no_consumption"
            self.db.commit()
            return {
                "status": "no_consumption",
                "operation_id": str(op.id),
            }

        for item in items:
            resolve_item_nutrition(self.db, item)

        meal_type = _normalize_meal_type(
            (proposals or {}).get("meal_type") if isinstance(proposals, dict) else None
        )
        result = write_consumption(
            self.db,
            user_id=event.user_id,
            items=items,
            meal_type=meal_type,
            operation_id=str(op.id),
        )
        return {
            "status": "completed",
            "operation_id": str(op.id),
            "result": result,
        }

    # -- revisions -------------------------------------------------------- #

    def _apply_revision(
        self,
        event: TrustedEvent,
        digest: str,
        existing: Optional[ConsumptionOperation] = None,
    ) -> dict:
        """An authenticated EDIT of the same message id.

        Recorded as an explicit revision of the original operation: the prior
        artifacts are replaced and the revision is counted. Never a second
        consumption.
        """
        op = existing
        if op is None:
            op = self._find_operation_by_identity(event)
        if op is None:
            # No prior operation under this identity: a genuine first write.
            op, _created = get_or_create_operation(
                self.db,
                user_id=event.user_id,
                source="telegram",
                source_chat_id=event.chat_id,
                source_message_id=event.message_id,
                source_bot_id=event.bot_id,
                raw_text=event.text,
                payload_hash=digest,
            )

        revisions = 0
        if isinstance(op.result_json, dict):
            revisions = int(op.result_json.get("revision", 0) or 0)

        self._purge_operation_artifacts(event.user_id, str(op.id))

        op.payload_hash = digest
        op.raw_text = event.text
        op.status = "processing"
        op.result_json = {}
        op.updated_at = _now()
        self.db.flush()

        proposals = self._propose(str(op.id), event.text)
        items, rejected = validate_proposals(proposals)
        if not items:
            op.status = "needs_clarification"
            op.result_json = {"ok": False, "rejected": rejected, "revision": revisions + 1}
            self.db.commit()
            return {
                "status": "needs_clarification",
                "operation_id": str(op.id),
                "revision": revisions + 1,
                "rejected": rejected,
            }

        for item in items:
            resolve_item_nutrition(self.db, item)

        meal_type = _normalize_meal_type(
            (proposals or {}).get("meal_type") if isinstance(proposals, dict) else None
        )
        result = write_consumption(
            self.db,
            user_id=event.user_id,
            items=items,
            meal_type=meal_type,
            operation_id=str(op.id),
        )
        # Mark the durable result as a revision of the original event.
        op = self.db.query(ConsumptionOperation).filter(
            ConsumptionOperation.id == op.id
        ).first()
        if op and isinstance(op.result_json, dict):
            op.result_json = {**op.result_json, "revision": revisions + 1}
            self.db.commit()
            result = op.result_json

        return {
            "status": "revised",
            "operation_id": str(op.id) if op else None,
            "revision": revisions + 1,
            "result": result,
        }

    def _find_operation_by_identity(self, event: TrustedEvent):
        """Look up an existing operation by trusted identity only."""
        return (
            self.db.query(ConsumptionOperation)
            .filter(
                ConsumptionOperation.user_id == event.user_id,
                ConsumptionOperation.source_bot_id == event.bot_id,
                ConsumptionOperation.source_chat_id == event.chat_id,
                ConsumptionOperation.source_message_id == event.message_id,
            )
            .first()
        )

    def _purge_operation_artifacts(self, user_id: str, operation_id: str) -> None:
        """Remove the artifacts written by a previous revision of this operation.

        Order respects foreign keys: links -> items/measurements -> meal ->
        consumption items.
        """
        meal_ids = [
            str(m.id)
            for m in self.db.query(Meal).filter(Meal.source_operation_id == operation_id).all()
        ]
        measurement_ids = [
            str(m.id)
            for m in self.db.query(Measurement)
            .filter(Measurement.source_operation_id == operation_id)
            .all()
        ]
        if meal_ids:
            self.db.query(BeverageMeasurement).filter(
                BeverageMeasurement.meal_item_id.in_(
                    [
                        str(mi.id)
                        for mi in self.db.query(MealItem)
                        .filter(MealItem.meal_id.in_(meal_ids))
                        .all()
                    ]
                )
            ).delete(synchronize_session=False)
            self.db.query(MealItem).filter(
                MealItem.meal_id.in_(meal_ids)
            ).delete(synchronize_session=False)
        if measurement_ids:
            self.db.query(BeverageMeasurement).filter(
                BeverageMeasurement.measurement_id.in_(measurement_ids)
            ).delete(synchronize_session=False)
            self.db.query(Measurement).filter(
                Measurement.id.in_(measurement_ids)
            ).delete(synchronize_session=False)
        if meal_ids:
            self.db.query(Meal).filter(Meal.id.in_(meal_ids)).delete(synchronize_session=False)
        self.db.query(ConsumptionItem).filter(
            ConsumptionItem.operation_id == operation_id,
            ConsumptionItem.user_id == user_id,
        ).delete(synchronize_session=False)
        self.db.flush()

    def recover_incomplete_operations(self, stale_minutes: int = 5) -> dict:
        """Re-drive operations that crashed between receipt and completion.

        A crash can leave an operation in 'pending' or 'processing' with no
        Telegram redelivery (the offset may already have advanced, or the caller
        is not Telegram). This scans the durable consumption_operations table —
        NOT the poll offset — for in-flight operations and re-drives each
        through the same trusted path, idempotently.

        Only operations older than stale_minutes are considered, so a genuinely
        concurrent worker does not double-drive an operation another worker is
        actively processing. 'needs_clarification' is left for the user; only
        'pending'/'processing' are recovered.
        """
        from datetime import timedelta

        cutoff = _now() - timedelta(minutes=stale_minutes)
        stale = (
            self.db.query(ConsumptionOperation)
            .filter(
                ConsumptionOperation.status.in_(["pending", "processing"]),
                ConsumptionOperation.updated_at < cutoff,
            )
            .order_by(ConsumptionOperation.updated_at.asc())
            .limit(200)
            .all()
        )
        recovered = {"replayed": 0, "completed": 0, "no_consumption": 0, "failed": 0, "ids": []}

        for op in stale:
            event = TrustedEvent(
                kind="message",
                bot_id=op.source_bot_id or "",
                chat_id=op.source_chat_id or "",
                message_id=op.source_message_id or "",
                telegram_user_id="",
                text=op.raw_text or "",
                user_id=str(op.user_id),
            )
            # Fresh digest from the stored raw text.
            digest = content_digest(event.text)
            try:
                outcome = self.handle(event)
                status = outcome.get("status")
            except (IngressConflict, IngressRejected) as exc:
                recovered["failed"] += 1
                recovered["ids"].append(str(op.id))
                self.db.rollback()
                self.db.query(ConsumptionOperation).filter(
                    ConsumptionOperation.id == op.id
                ).update({"status": "recovery_failed"}, synchronize_session=False)
                self.db.commit()
                continue

            if status in ("completed", "revised"):
                recovered["completed"] += 1
            elif status == "replayed":
                recovered["replayed"] += 1
            else:
                recovered["no_consumption"] += 1
            recovered["ids"].append(str(op.id))

        return recovered

    # -- confirmation callbacks ------------------------------------------- #

    def handle_callback(self, event: TrustedEvent) -> dict:
        """A confirmation callback must reference the ORIGINAL operation and
        the caller must own it.

        callback_data is 'confirm:<operation_id>' or 'cancel:<operation_id>'.
        The callback's own message identity is NEVER treated as a new
        consumption event.
        """
        data = (event.callback_data or "").strip()
        if ":" not in data:
            return {"status": "ignored", "reason": "malformed_callback_data"}
        action, operation_id = data.split(":", 1)
        if action not in ("confirm", "cancel"):
            return {"status": "ignored", "reason": "unknown_callback_action"}

        op = self.db.query(ConsumptionOperation).filter(
            ConsumptionOperation.id == operation_id
        ).first()
        if op is None:
            return {"status": "ignored", "reason": "unknown_operation"}

        # OwnerShip check: the callback must come from the operation's owner.
        if str(op.user_id) != str(event.user_id):
            return {"status": "rejected", "reason": "callback_not_owner"}

        if action == "cancel":
            if op.status != "completed":
                op.status = "cancelled"
                op.updated_at = _now()
                self.db.commit()
            return {"status": "cancelled", "operation_id": str(op.id)}

        if op.status == "completed" and op.result_json:
            return {
                "status": "replayed",
                "operation_id": str(op.id),
                "result": op.result_json,
            }

        # Re-run the bound proposal for the original operation.
        proposals = self._propose(str(op.id), op.raw_text or "")
        items, rejected = validate_proposals(proposals)
        if not items:
            op.status = "needs_clarification"
            op.result_json = {"ok": False, "rejected": rejected}
            self.db.commit()
            return {
                "status": "needs_clarification",
                "operation_id": str(op.id),
                "rejected": rejected,
            }
        for item in items:
            resolve_item_nutrition(self.db, item)
        meal_type = _normalize_meal_type(
            (proposals or {}).get("meal_type") if isinstance(proposals, dict) else None
        )
        result = write_consumption(
            self.db,
            user_id=event.user_id,
            items=items,
            meal_type=meal_type,
            operation_id=str(op.id),
        )
        return {"status": "completed", "operation_id": str(op.id), "result": result}
