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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

sys.path.insert(0, "/app/drhiro_src")
from drhiro_api.models import ConsumptionOperation, User  # noqa: E402

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
    """Persist into the REAL T1 tables and record reply intent in the SAME commit.

    Returns (operation_id, created). `created=False` means the natural Telegram key
    already existed: a replay, which must not produce a second consumption.
    """
    with Session(_engine) as session:
        user_id = ensure_user(session)
        op = session.execute(
            select(ConsumptionOperation).where(
                ConsumptionOperation.user_id == user_id,
                ConsumptionOperation.source_bot_id == bot_id,
                ConsumptionOperation.source_chat_id == chat_id,
                ConsumptionOperation.source_message_id == message_id,
            )
        ).scalar_one_or_none()

        if op is not None:
            return op.id, False

        op = ConsumptionOperation(
            user_id=user_id,
            source="telegram",
            source_bot_id=bot_id,
            source_chat_id=chat_id,
            source_message_id=message_id,
            raw_text=text,
            payload_hash=digest,
            status="completed",
            result_json={"items": 1, "via": "trusted-ingress"},
        )
        session.add(op)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            existing = session.execute(
                select(ConsumptionOperation).where(
                    ConsumptionOperation.user_id == user_id,
                    ConsumptionOperation.source_bot_id == bot_id,
                    ConsumptionOperation.source_chat_id == chat_id,
                    ConsumptionOperation.source_message_id == message_id,
                )
            ).scalar_one()
            log("consumption_conflict_replayed", event_key=event_key)
            return existing.id, False

        # Transactional intent: same transaction as the consumption commit.
        session.execute(
            __import__("sqlalchemy").text(
                """
                INSERT INTO reply_outbox (operation_id, event_key, chat_id, body)
                VALUES (:op, :ek, :chat, :body)
                ON CONFLICT (operation_id) DO NOTHING
                """
            ),
            {"op": str(op.id), "ek": event_key, "chat": chat_id,
             "body": f"Logged: {text}"},
        )
        session.commit()
        log("t1_persisted_with_reply_intent", event_key=event_key, operation_id=str(op.id))
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
            _api("sendMessage", {"chat_id": chat_id, "text": body})
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
            op_id, created_op = persist_consumption(
                event_key=event_key, bot_id=bot_id, chat_id=chat_id,
                message_id=message_id, text=text, digest=digest,
            )

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
        return self._json({"ok": False}, 404)


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
