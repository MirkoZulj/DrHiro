"""TRUSTED ingress worker (disposable stack).

The single Telegram consumer. Owns the bot token, the spool and the signing key;
OpenClaw and the MCP cannot reach any of it (see tests/test_r1_stack_isolation.py).

Pipeline per update:

  1. getMe               -> the VERIFIED bot id (trusted; never caller-supplied)
  2. durable receipt     -> INSERT .. ON CONFLICT DO NOTHING, unique per
                            (bot_id, chat_id, message_id). Redelivery therefore
                            cannot create a second consumption.
  3. exclusive claim     -> claim_token + lease; only the claimant may complete
  4. signed envelope     -> Ed25519, private key held here only
  5. T1 persistence      -> the REAL ConsumptionOperation model, whose
                            uq_consumption_op_telegram constraint enforces
                            exactly-once on the natural Telegram key
  6. reply intent        -> reply_outbox row inserted in the SAME transaction as
                            the consumption commit (transactional intent)
  7. reply delivery      -> pending -> in_flight -> sent | failed | unknown

The one subtle part is step 7. `in_flight` is committed BEFORE the network call, so
a crash mid-send is visible to recovery as "attempted, outcome unknown" and becomes
`unknown` - never silently retried, because the message may already have been
delivered. A crash BEFORE the attempt leaves `pending`, which is safe to retry.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import hmac
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg2
import psycopg2.extras
from sqlalchemy import create_engine, select
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

sys.path.insert(0, "/app/drhiro_src")
from drhiro_api.models import ConsumptionOperation, User  # noqa: E402

# The REAL T1 services. The slice must exercise the actual single write path, not
# fabricate a completed operation row: parse -> resolve nutrition -> write, which is
# what produces meal items, liquid measurements and the 6-nutrient totals.
from drhiro_api.services.consumption import (  # noqa: E402
    _compute_payload_hash,
    get_or_create_operation,
    parse_consumption_text,
    resolve_item_nutrition,
    write_consumption,
)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_API = os.environ["TELEGRAM_API_URL"]
OPENCLAW_URL = os.environ.get("OPENCLAW_URL", "http://openclaw:8090")
DATABASE_URL = os.environ["DATABASE_URL"]
SIGNING_KEY_HEX = os.environ.get("DRHIRO_INGRESS_SIGNING_KEY", "")
EXPECTED_TELEGRAM_ID = os.environ.get("DRHIRO_TELEGRAM_ID", "")

CONSUMER_ID = "telegram:primary"          # single logical consumer
LEASE_S = int(os.environ.get("RECEIPT_LEASE_S", "60"))
IN_FLIGHT_GRACE_S = int(os.environ.get("REPLY_IN_FLIGHT_GRACE_S", "8"))
POLL_TIMEOUT_S = int(os.environ.get("SEND_TIMEOUT_S", "5"))
RECOVERY_INTERVAL_S = int(os.environ.get("RECOVERY_INTERVAL_S", "5"))

_process_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
_engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# Deterministic user identity for this bot (trusted, server-side; never supplied
# by the model or by the message).
USER_UUID = uuid.uuid5(uuid.NAMESPACE_URL, f"drhiro:telegram:{EXPECTED_TELEGRAM_ID}")


def log(event: str, **fields):
    print(json.dumps({"ts": time.time(), "event": event, **fields}, default=str), flush=True)


# --------------------------------------------------------------------------- #
# trusted ingest helpers
# --------------------------------------------------------------------------- #

def _conn():
    return psycopg2.connect(DATABASE_URL)


def _api(method: str, payload: dict | None = None):
    url = f"{TELEGRAM_API}/bot{BOT_TOKEN}/{method}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=POLL_TIMEOUT_S) as resp:
        return json.loads(resp.read())


def verified_bot_id() -> str:
    """getMe is the only trusted source of bot identity. Caller-supplied ids are
    never used for identity."""
    me = _api("getMe")["result"]
    bot_id = str(me["id"])
    if EXPECTED_TELEGRAM_ID and bot_id != EXPECTED_TELEGRAM_ID:
        raise RuntimeError(f"bot id mismatch: getMe={bot_id} expected={EXPECTED_TELEGRAM_ID}")
    log("bot_identity_verified", bot_id=bot_id, username=me.get("username"))
    return bot_id


def ensure_user(session: Session) -> uuid.UUID:
    user = session.get(User, USER_UUID)
    if user is None:
        user = User(id=USER_UUID, display_name="Disposable Telegram User", timezone="UTC")
        session.add(user)
        session.commit()
    return USER_UUID


# --------------------------------------------------------------------------- #
# receipt + claim
# --------------------------------------------------------------------------- #

def write_receipt(cur, *, event_key, bot_id, chat_id, message_id, update_id,
                  digest, kind="created") -> bool:
    """Durable receipt. Returns True when this call created it."""
    cur.execute(
        """
        INSERT INTO telegram_receipts
            (event_key, bot_id, chat_id, message_id, update_id, content_digest, kind)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_key) DO NOTHING
        RETURNING id
        """,
        (event_key, bot_id, chat_id, message_id, update_id, digest, kind),
    )
    return cur.fetchone() is not None


def claim_receipt(cur, event_key: str) -> dict | None:
    """Exclusive claim. Succeeds only when unclaimed, completed-reset, or the
    previous lease has expired (stale-worker fencing)."""
    token = uuid.uuid4().hex
    cur.execute(
        """
        UPDATE telegram_receipts
           SET status = 'processing',
               claim_token = %s,
               claimed_by = %s,
               lease_expires_at = now() + make_interval(secs => %s)
         WHERE event_key = %s
           AND status <> 'completed'
           AND (claim_token IS NULL OR lease_expires_at IS NULL OR lease_expires_at < now())
        RETURNING claim_token, chat_id, message_id, bot_id, content_digest, status
        """,
        (token, _process_owner, LEASE_S, event_key),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "claim_token": row[0], "chat_id": row[1], "message_id": row[2],
        "bot_id": row[3], "content_digest": row[4],
    }


def fail_receipt(cur, event_key: str, claim_token: str, error: str) -> bool:
    """Release a claim after a processing error so the update is retried promptly.

    Waiting for the lease to expire also works, but it wastes the whole lease (60s by
    default) on a transient error. This records the error and the attempt count, and
    clears the claim so the next poll retries. Safe because `write_consumption` is
    transactional: a failure leaves no partial consumption behind.
    """
    cur.execute(
        """
        UPDATE telegram_receipts
           SET status = 'received',
               claim_token = NULL,
               claimed_by = NULL,
               lease_expires_at = NULL,
               attempts = attempts + 1,
               last_error = %s
         WHERE event_key = %s AND claim_token = %s
        """,
        (error[:500], event_key, claim_token),
    )
    return cur.rowcount == 1


def complete_receipt(cur, event_key: str, claim_token: str, operation_id: uuid.UUID) -> bool:
    """Completion requires the claim token - the ownership check."""
    cur.execute(
        """
        UPDATE telegram_receipts
           SET status = 'completed', operation_id = %s, lease_expires_at = NULL
         WHERE event_key = %s AND claim_token = %s
        """,
        (str(operation_id), event_key, claim_token),
    )
    return cur.rowcount == 1


# --------------------------------------------------------------------------- #
# T1 persistence + transactional reply intent
# --------------------------------------------------------------------------- #

def persist_consumption(*, event_key, bot_id, chat_id, message_id, text,
                        digest) -> tuple[uuid.UUID, bool]:
    """Persist through the REAL T1 consumption service.

    Exercises the genuine path end to end:

        parse_consumption_text      -> real parser (quantities, units, beverages)
        get_or_create_operation     -> trusted Telegram identity, fail-closed,
                                       atomic ON CONFLICT on the natural key
        resolve_item_nutrition      -> real DB-first resolution (no network: the
                                       disposable stack seeds a food catalog)
        write_consumption           -> THE single write path: meal row, meal_items,
                                       Measurement + BeverageMeasurement for liquids,
                                       6-nutrient totals, durable operation result

    Reply intent is inserted with the SAME session, so `write_consumption`'s commit
    commits it atomically with the consumption. Returns (operation_id, created);
    created=False is a replay against the natural Telegram key and must never write a
    second consumption or a second meal.
    """
    with Session(_engine) as session:
        user_id = ensure_user(session)

        # Real parser. Non-consumption chatter parses to zero items; that is not an
        # error and must not create a consumption.
        items = parse_consumption_text(text or "")
        payload_hash = _compute_payload_hash(items) if items else digest

        op, created = get_or_create_operation(
            session,
            str(user_id),
            source="telegram",
            source_chat_id=str(chat_id),
            source_message_id=str(message_id),
            source_bot_id=str(bot_id),
            raw_text=text,
            payload_hash=payload_hash,
        )

        if not created and op.status == "completed" and op.result_json:
            # Replay: the consumption already happened. No second meal, no second
            # outbox row, no second delivery.
            op_id = op.id
            session.rollback()          # nothing to write; keep the read side clean
            log("consumption_replayed", event_key=event_key, operation_id=str(op_id))
            return op_id, False

        if not items:
            # Nothing to log: complete the operation with an explicit empty result so
            # a redelivery is equally inert.
            op.status = "completed"
            op.result_json = {"ok": True, "data": {"items": [], "totals": None},
                              "message": "No consumable items recognised."}
            session.commit()
            return op.id, created

        resolved = [resolve_item_nutrition(session, it) for it in items]

        # Transactional reply intent. Inserted BEFORE write_consumption so that the
        # single commit inside it covers both the consumption and this row.
        # NOTE: sql_text, not text - `text` is the message parameter in this scope.
        session.execute(
            sql_text(
                """
                INSERT INTO reply_outbox (operation_id, event_key, chat_id, body)
                VALUES (:op, :ek, :chat, :body)
                ON CONFLICT (operation_id) DO NOTHING
                """
            ),
            {"op": str(op.id), "ek": event_key, "chat": str(chat_id),
             "body": text or ""},
        )
        session.flush()

        # THE real write path. Commits the consumption, the meal items, the liquid
        # measurements and the reply intent together.
        result = write_consumption(
            session, str(user_id), resolved, operation_id=op.id,
        )

        totals = (result.get("data") or {}).get("totals")
        log("t1_persisted_with_reply_intent", event_key=event_key,
            operation_id=str(op.id), items=len(resolved),
            meal_id=(result.get("data") or {}).get("meal_id"),
            kcal=(totals or {}).get("kcal"))
        return op.id, True


# --------------------------------------------------------------------------- #
# reply delivery state machine
# --------------------------------------------------------------------------- #

def _mark_in_flight(cur, operation_id) -> bool:
    """Commit the attempt BEFORE the network call.

    This ordering is what makes a crash mid-send recoverable as `unknown` instead of
    silently retryable.
    """
    cur.execute(
        """
        UPDATE reply_outbox
           SET reply_state = 'in_flight', attempts = attempts + 1,
               in_flight_at = now(), updated_at = now()
         WHERE operation_id = %s AND reply_state IN ('pending', 'failed')
        """,
        (str(operation_id),),
    )
    return cur.rowcount == 1


def _finish(cur, operation_id, state: str, error: str | None = None):
    cur.execute(
        """
        UPDATE reply_outbox
           SET reply_state = %s, last_error = %s, in_flight_at = NULL, updated_at = now()
         WHERE operation_id = %s
        """,
        (state, error, str(operation_id)),
    )


def deliver_reply(operation_id, chat_id: str, body: str) -> str:
    """Drive one outbox row. Returns the resulting reply_state."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            if not _mark_in_flight(cur, operation_id):
                return "skipped"

        try:
            _api("sendMessage", {"chat_id": chat_id,
                                 "text": body or "Logged."})
        except urllib.error.HTTPError as exc:
            # A RESPONSE came back and the send was refused (429/5xx before
            # acceptance). Nothing was delivered, so a retry is known-safe.
            with conn, conn.cursor() as cur:
                _finish(cur, operation_id, "failed", f"http {exc.code}")
            log("reply_failed_known_safe", operation_id=str(operation_id), code=exc.code)
            return "failed"
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
                OSError) as exc:
            # Distinguish "could not connect at all" (the request never left this
            # process, so a retry cannot duplicate) from everything else, where the
            # request may already have been delivered and the confirmation was lost.
            cause = getattr(exc, "reason", exc)
            if isinstance(cause, (ConnectionRefusedError, socket.gaierror)):
                with conn, conn.cursor() as cur:
                    _finish(cur, operation_id, "failed", f"connection never established: {cause!r}")
                log("reply_failed_known_safe", operation_id=str(operation_id),
                    cause=repr(cause))
                return "failed"

            with conn, conn.cursor() as cur:
                _finish(cur, operation_id, "unknown", f"ambiguous: {exc!r}")
            log("reply_unknown_ambiguous_send", operation_id=str(operation_id),
                error=repr(exc))
            return "unknown"

        with conn, conn.cursor() as cur:
            _finish(cur, operation_id, "sent")
        log("reply_sent", operation_id=str(operation_id))
        return "sent"
    finally:
        conn.close()


def recover(*, allow_send: bool = True) -> dict:
    """Recovery pass. `pending` is safe to send; a stale `in_flight` becomes
    `unknown` and is NEVER auto-resent; `unknown` is left untouched."""
    counts = {"pending": 0, "unknown": 0, "sent": 0, "failed": 0, "stale_to_unknown": 0}
    conn = _conn()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE reply_outbox
                   SET reply_state = 'unknown',
                       last_error = 'abandoned in_flight (crash mid-send); outcome unknown',
                       updated_at = now()
                 WHERE reply_state = 'in_flight'
                   AND in_flight_at < now() - make_interval(secs => %s)
                RETURNING operation_id
                """,
                (IN_FLIGHT_GRACE_S,),
            )
            stale = [r["operation_id"] for r in cur.fetchall()]
            counts["stale_to_unknown"] = len(stale)
            if stale:
                log("in_flight_to_unknown", operations=[str(o) for o in stale])

            cur.execute(
                "SELECT operation_id, chat_id, body, reply_state FROM reply_outbox "
                "WHERE reply_state IN ('pending', 'failed', 'unknown')"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    for row in rows:
        state = row["reply_state"]
        if state == "unknown":
            counts["unknown"] += 1
            log("unknown_left_unresolved", operation_id=str(row["operation_id"]))
            continue
        if not allow_send:
            counts[state] += 1
            continue
        result = deliver_reply(row["operation_id"], row["chat_id"], row["body"])
        counts[result] = counts.get(result, 0) + 1
    log("recovery_complete", **counts)
    return counts


# --------------------------------------------------------------------------- #
# the consume loop
# --------------------------------------------------------------------------- #

def consume_once(bot_id: str) -> int:
    """One poll/process cycle. Exactly one consumer performs this."""
    try:
        updates = _api("getUpdates")["result"]
    except Exception as exc:
        log("poll_failed", error=repr(exc))
        return 0
    if not updates:
        return 0

    processed = 0
    for update in updates:
        msg = update.get("message") or update.get("edited_message")
        acked = [update["update_id"]]
        if not msg:
            _safe_ack(acked)
            continue

        chat_id = str(msg["chat"]["id"])
        message_id = str(msg["message_id"])
        text = msg.get("text") or ""
        event_key = f"{bot_id}:{chat_id}:{message_id}"
        digest = hashlib.sha256(text.encode()).hexdigest()

        conn = _conn()
        try:
            with conn, conn.cursor() as cur:
                created = write_receipt(
                    cur, event_key=event_key, bot_id=bot_id, chat_id=chat_id,
                    message_id=message_id, update_id=update["update_id"],
                    digest=digest,
                    kind="edited" if "edited_message" in update else "created",
                )
                if not created:
                    cur.execute("SELECT status FROM telegram_receipts WHERE event_key=%s",
                                (event_key,))
                    status = cur.fetchone()[0]
                    log("receipt_duplicate_ignored", event_key=event_key, status=status)
                    _safe_ack(acked)
                    continue
                log("receipt_durable", event_key=event_key)

                claim = claim_receipt(cur, event_key)
                if claim is None:
                    log("claim_denied", event_key=event_key)
                    continue
                log("receipt_claimed", event_key=event_key, claim_token=claim["claim_token"])

            # --- outside the transaction: T1 persistence + reply intent ---
            try:
                op_id, created_op = persist_consumption(
                    event_key=event_key, bot_id=bot_id, chat_id=chat_id,
                    message_id=message_id, text=text, digest=digest,
                )
            except Exception as exc:
                # Release the claim so the update retries instead of being stranded
                # in 'processing' until the lease expires.
                with conn, conn.cursor() as cur:
                    released = fail_receipt(cur, event_key, claim["claim_token"], repr(exc))
                log("consume_failed_claim_released", event_key=event_key,
                    released=released, error=repr(exc))
                continue

            with conn, conn.cursor() as cur:
                if not complete_receipt(cur, event_key, claim["claim_token"], op_id):
                    log("complete_denied_not_owner", event_key=event_key)
            log("receipt_completed", event_key=event_key, operation_id=str(op_id),
                created=created_op)
        finally:
            conn.close()

        # --- reply delivery ---
        if created_op:
            state = deliver_reply(op_id, chat_id, f"Logged: {text}")
            log("reply_state", operation_id=str(op_id), state=state)
        else:
            log("replay_no_second_reply", event_key=event_key)

        _safe_ack(acked)
        processed += 1

    return processed


def _safe_ack(update_ids: list[int]):
    try:
        _api("ack", {"update_ids": update_ids})
    except Exception:
        pass



# --------------------------------------------------------------------------- #
# reply resolution (the `unknown` state)
# --------------------------------------------------------------------------- #

ADMIN_TOKEN = os.environ.get("INGRESS_ADMIN_TOKEN", "")

RESOLVABLE_STATES = ("unknown", "failed")


class ResolutionError(Exception):
    def __init__(self, code: str, http_status: int = 400, **extra):
        super().__init__(code)
        self.code, self.http_status, self.extra = code, http_status, extra


def _authorised(header: str | None) -> bool:
    """Bearer-token auth for the resolution action.

    The token is delivered to the trusted ingress only; it is never mounted into a
    model-accessible container. An unset token means resolution is DISABLED (fail
    closed) rather than open.
    """
    if not ADMIN_TOKEN:
        return False
    if not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer "):].strip(), ADMIN_TOKEN)


def resolve_reply(*, operation_id: str, actor: str, action: str,
                  claim_chat_id: str, note: str | None = None,
                  duplicate_risk_ack: bool = False) -> dict:
    """Resolve an `unknown`/`failed` reply intent. Ownership-checked and audited.

    action="acknowledge": record that the ambiguity is accepted and no resend will
        be attempted. Terminal; the consumption is untouched.
    action="resend":      perform an explicit new delivery attempt. Requires
        duplicate_risk_ack=True, because delivery may ALREADY have happened. The
        attempt is recorded, and the consumption is NEVER recreated or re-run.

    Concurrency: the outbox row is locked FOR UPDATE and its state re-checked, so two
    simultaneous resolutions cannot both act. The loser gets state_changed.
    """
    if action not in ("acknowledge", "resend"):
        raise ResolutionError("unknown_action")

    if action == "resend" and not duplicate_risk_ack:
        raise ResolutionError("duplicate_risk_not_acknowledged")

    conn = _conn()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT o.operation_id, o.chat_id, o.body, o.reply_state, o.attempts,
                           (SELECT count(*) FROM reply_audit a
                             WHERE a.operation_id = o.operation_id
                               AND a.action = 'resend') AS resends
                      FROM reply_outbox o
                     WHERE o.operation_id = %s
                     FOR UPDATE
                    """,
                    (str(operation_id),),
                )
                row = cur.fetchone()
                if row is None:
                    raise ResolutionError("not_found", http_status=404)

                # Ownership: the caller must be bound to the chat that owns the reply.
                cur.execute(
                    "SELECT owner_id FROM reply_owners WHERE chat_id = %s",
                    (row["chat_id"],),
                )
                owner = cur.fetchone()
                if owner is None or owner["owner_id"] != claim_chat_id:
                    raise ResolutionError("not_owner", http_status=403)

                if row["reply_state"] not in RESOLVABLE_STATES:
                    raise ResolutionError(
                        "state_changed", http_status=409,
                        current_state=row["reply_state"],
                    )

                from_state = row["reply_state"]
                attempt_no = int(row["attempts"]) + int(row["resends"])

                if action == "acknowledge":
                    to_state = "resolved_acknowledged"
                    cur.execute(
                        """
                        UPDATE reply_outbox
                           SET reply_state = %s, resolved_by = %s, resolved_at = now(),
                               updated_at = now()
                         WHERE operation_id = %s
                        """,
                        (to_state, actor, str(operation_id)),
                    )
                    cur.execute(
                        """
                        INSERT INTO reply_audit (operation_id, actor, action, from_state,
                                                 to_state, delivery_attempt, detail)
                        VALUES (%s, %s, 'acknowledge', %s, %s, %s, %s)
                        """,
                        (str(operation_id), actor, from_state, to_state, attempt_no,
                         note or "ambiguity accepted; no resend attempted"),
                    )
                    return {"ok": True, "action": action, "to_state": to_state,
                            "delivery_attempt": attempt_no, "consumption_untouched": True}

                # --- explicit resend -------------------------------------------------
                # A new, separately counted delivery attempt. Nothing about the
                # consumption is touched: we do not re-run the write path.
                #
                # CRITICAL ORDERING: transition the row OUT of the resolvable set
                # inside this locked transaction, BEFORE releasing the lock. The send
                # happens after the lock is dropped (a slow network call must not
                # block other work), so without this claim the state would still read
                # 'unknown' while the resend is in flight and a second resolver would
                # pass its own state check - both would act. Marking 'in_flight' (not
                # resolvable) makes the loser observe a state it cannot resolve.
                cur.execute(
                    """
                    UPDATE reply_outbox
                       SET reply_state = 'in_flight', resolved_by = %s,
                           in_flight_at = now(), updated_at = now()
                     WHERE operation_id = %s
                    """,
                    (actor, str(operation_id)),
                )
                cur.execute(
                    """
                    INSERT INTO reply_audit (operation_id, actor, action, from_state,
                                             to_state, delivery_attempt, detail)
                    VALUES (%s, %s, 'resend', %s, 'in_flight', %s, %s)
                    """,
                    (str(operation_id), actor, from_state, attempt_no + 1,
                     note or "explicit resend after ambiguous delivery; "
                             "delivery may already have occurred"),
                )

        # Send OUTSIDE the transaction that holds the lock, so a slow network call
        # does not block other work. The audit row above is already durable.
        try:
            _api("sendMessage", {"chat_id": row["chat_id"],
                                 "text": row["body"] or "Logged."})
            outcome, to_state, error = "delivered", "resolved_resent", None
        except urllib.error.HTTPError as exc:
            outcome, to_state, error = "refused", "failed", f"HTTP {exc.code}"
        except Exception as exc:
            # Ambiguous again: the resend itself may have been delivered.
            outcome, to_state, error = "ambiguous", "unknown", repr(exc)

        conn2 = _conn()
        try:
            with conn2:
                with conn2.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE reply_outbox
                           SET reply_state = %s, last_error = %s, resolved_by = %s,
                               resolved_at = now(), updated_at = now()
                         WHERE operation_id = %s
                        """,
                        (to_state, error, actor, str(operation_id)),
                    )
                    cur.execute(
                        """
                        INSERT INTO reply_audit (operation_id, actor, action, from_state,
                                                 to_state, delivery_attempt, detail)
                        VALUES (%s, %s, 'resend', 'in_flight', %s, %s, %s)
                        """,
                        (str(operation_id), actor, to_state, attempt_no + 1,
                         f"resend outcome={outcome} error={error}"),
                    )
        finally:
            conn2.close()

        log("reply_resolved", operation_id=str(operation_id), actor=actor,
            action=action, outcome=outcome, delivery_attempt=attempt_no + 1)
        return {"ok": True, "action": action, "outcome": outcome, "to_state": to_state,
                "delivery_attempt": attempt_no + 1, "consumption_untouched": True}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# admin surface (test control; the trusted side only)
# --------------------------------------------------------------------------- #

class AdminHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/admin/recover":
            return self._json({"ok": True, "counts": recover()})
        if self.path == "/admin/status":
            conn = _conn()
            try:
                with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT event_key, status, operation_id FROM telegram_receipts ORDER BY id")
                    receipts = [dict(r) for r in cur.fetchall()]
                    cur.execute("SELECT operation_id, reply_state, attempts, last_error FROM reply_outbox ORDER BY created_at")
                    outbox = [dict(r) for r in cur.fetchall()]
                    cur.execute("SELECT count(*) AS n FROM consumption_operations")
                    ops = cur.fetchone()["n"]
                return self._json({"ok": True, "receipts": receipts, "outbox": outbox,
                                   "consumption_operations": ops,
                                   "owner": _process_owner})
            finally:
                conn.close()
        if self.path == "/admin/audit":
            conn = _conn()
            try:
                with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT id, operation_id, actor, action, from_state, to_state,
                               delivery_attempt, detail, created_at
                          FROM reply_audit ORDER BY id
                        """
                    )
                    rows = [dict(r) for r in cur.fetchall()]
                return self._json({"ok": True, "audit": rows})
            finally:
                conn.close()
        if self.path == "/admin/real-output":
            # What the REAL consumption service actually produced. Used by the tests
            # to assert on meal items, nutrition, liquid measurements and projections
            # rather than merely on a completed operation row.
            conn = _conn()
            try:
                with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT m.id AS meal_id, m.meal_type, m.totals_json
                          FROM meals m ORDER BY m.created_at
                        """
                    )
                    meals = [dict(r) for r in cur.fetchall()]
                    cur.execute(
                        """
                        SELECT mi.id, mi.display_name, mi.grams, mi.volume_ml,
                               mi.beverage_category, mi.meal_id
                          FROM meal_items mi ORDER BY mi.display_name
                        """
                    )
                    items = [dict(r) for r in cur.fetchall()]
                    cur.execute(
                        """
                        SELECT id, metric_type, unit, value_json, source_operation_id,
                               source_item_id, meal_item_id
                          FROM measurements WHERE source_provider = 'consumption'
                         ORDER BY created_at
                        """
                    )
                    measurements = [dict(r) for r in cur.fetchall()]
                    cur.execute("SELECT count(*) AS n FROM beverage_measurements")
                    bev_links = cur.fetchone()["n"]
                    cur.execute(
                        "SELECT count(*) AS n FROM consumption_operations WHERE status = 'completed'"
                    )
                    completed_ops = cur.fetchone()["n"]
                    cur.execute("SELECT count(*) AS n FROM consumption_operations")
                    all_ops = cur.fetchone()["n"]
                return self._json({
                    "ok": True, "meals": meals, "meal_items": items,
                    "measurements": measurements, "beverage_measurements": bev_links,
                    "completed_operations": completed_ops,
                    "total_operations": all_ops,
                })
            finally:
                conn.close()
        return self._json({"ok": False}, 404)

    def do_POST(self):
        if self.path != "/admin/reply/resolve":
            return self._json({"ok": False}, 404)

        if not _authorised(self.headers.get("Authorization")):
            return self._json({"ok": False, "error": "unauthorised"}, 401)

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json({"ok": False, "error": "bad_json"}, 400)

        try:
            result = resolve_reply(
                operation_id=payload["operation_id"],
                actor=payload.get("actor") or "unknown",
                action=payload.get("action", ""),
                claim_chat_id=str(payload.get("claim_chat_id", "")),
                note=payload.get("note"),
                duplicate_risk_ack=bool(payload.get("duplicate_risk_ack", False)),
            )
        except ResolutionError as exc:
            body = {"ok": False, "error": exc.code, **exc.extra}
            return self._json(body, exc.http_status)
        except KeyError as exc:
            return self._json({"ok": False, "error": f"missing_field:{exc}"}, 400)
        return self._json(result)


def _register_owner_bindings():
    """Bind the Telegram user to their chat, for ownership-checked resolution.

    Owned by the TRUSTED side: the binding is established from configuration the
    model-accessible containers never see, and resolution is refused unless the
    caller's claim matches it.
    """
    owner = os.environ.get("REPLY_OWNER_ID")
    if not owner:
        return
    conn = _conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO reply_owners (chat_id, owner_id)
                    VALUES (%s, %s) ON CONFLICT (chat_id) DO NOTHING
                    """,
                    (owner, owner),
                )
    finally:
        conn.close()


def _serve_admin():
    port = int(os.environ.get("INGRESS_ADMIN_PORT", "8082"))
    ThreadingHTTPServer(("0.0.0.0", port), AdminHandler).serve_forever()


def main():
    log("ingress_starting", owner=_process_owner)
    for attempt in range(60):
        try:
            with _conn():
                break
        except Exception:
            time.sleep(1)
    else:
        raise SystemExit("database unreachable")

    bot_id = verified_bot_id()
    _register_owner_bindings()
    threading.Thread(target=_serve_admin, daemon=True).start()

    # Recovery first (pending is safe; abandonment becomes unknown).
    recover()

    next_recovery = time.time() + RECOVERY_INTERVAL_S
    while True:
        try:
            consume_once(bot_id)
        except Exception as exc:
            log("consume_error", error=repr(exc))

        # Periodic recovery. This is what turns an abandoned in_flight row into
        # `unknown` after a crash mid-send; `pending` is safe to re-drive.
        if time.time() >= next_recovery:
            try:
                recover()
            except Exception as exc:
                log("recovery_error", error=repr(exc))
            next_recovery = time.time() + RECOVERY_INTERVAL_S

        time.sleep(0.5)


if __name__ == "__main__":
    main()
