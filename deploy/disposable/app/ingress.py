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

OUT OF PROVEN SCOPE (explicit limitations, not claims):
  * (RESOLVED in review round 8) POLLING OFFSET - acknowledgement now uses the REAL
    Bot API offset contract: `getUpdates(offset=N)` confirms updates below N, and the
    offset is DURABLE in `telegram_consumer_offset`. The old bespoke `/ack` route is
    gone. The offset is written AFTER processing, so a crash re-fetches rather than
    skips; the durable receipt remains the correctness boundary.
  * SENDER IDENTITY - a verified bot identity authenticates the BOT, not the SENDER.
    This slice maps every incoming message to one fixed disposable user
    (USER_UUID derived from the bot id) and does NOT resolve the Telegram sender id
    to an internal user. Sender identity resolution is NOT implemented or proven.
  * EDITS / CONTENT CONFLICTS - an edited message sharing the same message_id is
    treated as a duplicate of the original receipt, not as a revision; conflicting
    payload reuse is not reconciled here. Not implemented or proven.
None of these are claimed to be covered; they are listed so the evidence is not
over-read.
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
# Administrator identity bound to the shared admin token (review #3): the audited
# actor is ALWAYS this principal, never a caller-supplied string.
ADMIN_IDENTITY = os.environ.get("INGRESS_ADMIN_IDENTITY", "admin")
# Observability: the main loop CATCHES consume exceptions, so a broken duplicate or
# recovery path can leave the suite green while the queue never drains. These counters
# make the failure observable instead of log-only.
_COUNTERS: dict[str, int] = {
    "consume_error": 0,
    "receipt_duplicate_completed": 0,
    "receipt_duplicate_unfinished": 0,
    "receipt_duplicate_row_missing": 0,
    "receipt_attempt_budget_exhausted": 0,
    "consume_failed_claim_released": 0,
    "receipt_recovered": 0,
}
_COUNTERS_LOCK = threading.Lock()


def _count(name: str, n: int = 1) -> None:
    with _COUNTERS_LOCK:
        _COUNTERS[name] = _COUNTERS.get(name, 0) + n


def counters() -> dict[str, int]:
    with _COUNTERS_LOCK:
        return dict(_COUNTERS)


LEASE_S = int(os.environ.get("RECEIPT_LEASE_S", "60"))
RECEIPT_MAX_ATTEMPTS = int(os.environ.get("RECEIPT_MAX_ATTEMPTS", "10"))
RECEIPT_RECOVERY_LIMIT = int(os.environ.get("RECEIPT_RECOVERY_LIMIT", "20"))
INGRESS_ADMIN_BIND = os.environ.get("INGRESS_ADMIN_BIND", "127.0.0.1")
IN_FLIGHT_GRACE_S = int(os.environ.get("REPLY_IN_FLIGHT_GRACE_S", "8"))
POLL_TIMEOUT_S = int(os.environ.get("SEND_TIMEOUT_S", "5"))
RECOVERY_INTERVAL_S = int(os.environ.get("RECOVERY_INTERVAL_S", "5"))

_process_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

# Test-only crash/fail hooks (B1), gated on marker files inside the trusted spool
# volume. The markers let a test deterministically exercise "crash immediately after
# receipt commit" and "failure before consumption persistence" without timing races.
# They are absent in normal operation; only a test (via the trusted side) creates them.
CRASH_MARKER = "/var/spool/telegram/crash_after_receipt.marker"
FAIL_MARKER = "/var/spool/telegram/fail_before_consume.marker"
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


class TelegramAPIError(Exception):
    """Telegram returned an application-level error (`ok` != true).

    Callers must not treat this as success: an `ok=false` response means Telegram
    processed the request and returned an error, so the delivery outcome is unknown
    (the message may still have been sent).
    """


def _api(method: str, payload: dict | None = None):
    url = f"{TELEGRAM_API}/bot{BOT_TOKEN}/{method}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=POLL_TIMEOUT_S) as resp:
        doc = json.loads(resp.read())
    if not doc.get("ok"):
        raise TelegramAPIError(
            f"telegram ok=false: {doc.get('description')} (outcome ambiguous)"
        )
    return doc


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
                  digest, raw_text, kind="created") -> bool:
    """Durable receipt. Returns True when this call created it.

    The trusted input (`raw_text`) is stored so that an unfinished receipt can be
    re-driven from PostgreSQL after a crash - WITHOUT relying on Telegram to
    redeliver the update (review #1).
    """
    cur.execute(
        """
        INSERT INTO telegram_receipts
            (event_key, bot_id, chat_id, message_id, update_id, content_digest,
             raw_text, kind)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_key) DO NOTHING
        RETURNING id
        """,
        (event_key, bot_id, chat_id, message_id, update_id, digest, raw_text, kind),
    )
    return cur.fetchone() is not None


def claim_receipt(cur, event_key: str) -> dict | None:
    """Exclusive claim. Succeeds only when unclaimed, completed-reset, or the
    previous lease has expired (stale-worker fencing).

    REVIEW #1 (retry budget): the attempt budget is enforced HERE, at claim time,
    and CONSUMED on acquisition. `attempts` therefore counts every claim taken -
    including one abandoned by a hard crash that never reached fail_receipt - so a
    receipt cannot be redriven forever by redelivery or lease recovery. Filtering
    only inside recover_receipts() left every other entry path unbounded.
    """
    token = uuid.uuid4().hex
    cur.execute(
        """
        UPDATE telegram_receipts
           SET status = 'processing',
               claim_token = %s,
               claimed_by = %s,
               lease_expires_at = now() + make_interval(secs => %s),
               attempts = attempts + 1
         WHERE event_key = %s
           AND status <> 'completed'
           AND attempts < %s
           AND (claim_token IS NULL OR lease_expires_at IS NULL OR lease_expires_at < now())
        RETURNING claim_token, chat_id, message_id, bot_id, content_digest, status
        """,
        (token, _process_owner, LEASE_S, event_key, RECEIPT_MAX_ATTEMPTS),
    )
    row = cur.fetchone()
    if row:
        return {
            "claim_token": row["claim_token"], "chat_id": row["chat_id"],
            "message_id": row["message_id"], "bot_id": row["bot_id"],
            "content_digest": row["content_digest"],
        }

    # Claim denied. Distinguish "budget exhausted" from "another worker holds it",
    # and make the exhausted condition OPERATOR-VISIBLE (status becomes 'exhausted')
    # instead of an invisible no-op that silently drops the update.
    cur.execute(
        """
        UPDATE telegram_receipts
           SET status = 'exhausted',
               claim_token = NULL,
               claimed_by = NULL,
               lease_expires_at = NULL,
               last_error = COALESCE(NULLIF(last_error, ''), '') ||
                            CASE WHEN COALESCE(last_error, '') = '' THEN '' ELSE ' | ' END ||
                            'attempt budget exhausted at ' || attempts || ' claims'
         WHERE event_key = %s
           AND status <> 'completed'
           AND attempts >= %s
           AND (claim_token IS NULL OR lease_expires_at IS NULL OR lease_expires_at < now())
        RETURNING attempts
        """,
        (event_key, RECEIPT_MAX_ATTEMPTS),
    )
    if cur.fetchone() is not None:
        _count("receipt_attempt_budget_exhausted")
        log("receipt_attempt_budget_exhausted", event_key=event_key)
    return None


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

def _mark_in_flight(cur, operation_id) -> uuid.UUID | None:
    """Commit the attempt BEFORE the network call; return the NEW attempt id.

    This ordering is what makes a crash mid-send recoverable as `unknown` instead of
    silently retryable. The unique attempt id (review #4) is stored on the row so a
    completion is fenced to THIS attempt only. Returns None when the row is not in a
    retryable state (a newer attempt or resolution already owns it).
    """
    # String form of the id: psycopg2 must adapt it identically in every process
    # (ingress main loop, stack_ctl subprocess, resolution) with no uuid adapter
    # ambiguity. UUID-typed column: Postgres coerces the text literal on compare.
    attempt_id = str(uuid.uuid4())
    cur.execute(
        """
        UPDATE reply_outbox
           SET reply_state = 'in_flight', attempts = attempts + 1,
               current_attempt_id = %s, in_flight_at = now(), updated_at = now()
         WHERE operation_id = %s AND reply_state IN ('pending', 'failed')
        """,
        (attempt_id, str(operation_id)),
    )
    return attempt_id if cur.rowcount == 1 else None


def _finish(cur, operation_id, attempt_id, state: str,
            error: str | None = None) -> bool:
    """Record the outcome ONLY if `attempt_id` is still the current attempt.

    Review #4: a delayed result from an OLD attempt must not overwrite a newer send
    or resolution. The WHERE guards on current_attempt_id; if a newer attempt or a
    resolution has replaced this one, the update touches no row and returns False.
    """
    cur.execute(
        """
        UPDATE reply_outbox
           SET reply_state = %s, last_error = %s, in_flight_at = NULL,
               current_attempt_id = NULL, updated_at = now()
         WHERE operation_id = %s AND current_attempt_id = %s
        """,
        (state, error, str(operation_id), attempt_id),
    )
    return cur.rowcount == 1


def _classify_delivery_exc(exc: BaseException) -> tuple[str, str]:
    """Classify a delivery exception CONSERVATIVELY (review #5).

    Only a connection-level failure that provably never reached Telegram - connection
    refused or DNS failure (the request never left this process) - is KNOWN-SAFE to
    retry ('failed'). Once the request reached Telegram - ANY HTTP response (4xx or
    5xx), an application-level ok=false, a connection reset after connect, or a
    timeout - the outcome is AMBIGUOUS ('unknown') and must NEVER be auto-resent,
    because the message may already have been delivered. A 5xx alone does NOT prove
    nothing was delivered.
    """
    cause = getattr(exc, "reason", exc)
    if isinstance(exc, urllib.error.HTTPError):
        # A 429 (Too Many Requests) is a documented PRE-ACCEPTANCE rate-limit
        # rejection: Telegram returns it before sending, so a retry provably cannot
        # duplicate. Any other HTTP response (especially 5xx) is AMBIGUOUS - a 5xx
        # alone does not prove nothing was delivered.
        if exc.code == 429:
            return "failed", f"http {exc.code} (rate-limited before acceptance; retry safe)"
        return "unknown", f"http {exc.code} (response received; delivery ambiguous)"
    if isinstance(exc, TelegramAPIError):
        return "unknown", f"{exc} (application-level error; delivery ambiguous)"
    if isinstance(cause, (ConnectionRefusedError, socket.gaierror)):
        return "failed", f"connection never established: {cause!r}"
    return "unknown", f"ambiguous: {exc!r}"


def deliver_reply(operation_id, chat_id: str, body: str) -> str:
    """Drive one outbox row. Returns the resulting reply_state."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            attempt_id = _mark_in_flight(cur, operation_id)
            if attempt_id is None:
                return "skipped"

        try:
            _api("sendMessage", {"chat_id": chat_id,
                                 "text": body or "Logged."})
            outcome, error = "sent", None
        except Exception as exc:
            # Conservative: connection-refused is known-safe; every other failure
            # (HTTP 4xx/5xx, ok=false, reset, timeout) is ambiguous.
            outcome, error = _classify_delivery_exc(exc)

        with conn, conn.cursor() as cur:
            applied = _finish(cur, operation_id, attempt_id, outcome, error)
        if not applied:
            log("delivery_result_stale_ignored", operation_id=str(operation_id),
                state=outcome)
        else:
            log("reply_delivery", operation_id=str(operation_id), state=outcome)
        return outcome
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
                   SET reply_state = 'unknown', current_attempt_id = NULL,
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
# the consume loop + receipt recovery
# --------------------------------------------------------------------------- #

def process_update(*, event_key, bot_id, chat_id, message_id, text, update_id,
                   digest, kind) -> None:
    """Claim + persist + complete + reply for ONE event.

    Shared by the fresh-poll path AND by receipt recovery, so unfinished work is
    re-driven from the stored payload without Telegram redelivery (review #1). The
    claim ownership, the transactional write, and the reply delivery are unchanged;
    this is purely the same pipeline invoked from two call sites.
    """
    conn = _conn()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            claim = claim_receipt(cur, event_key)
            if claim is None:
                # Completed under us, or another worker holds the (unexpired) lease.
                log("claim_denied_or_completed", event_key=event_key)
                return
            log("receipt_claimed", event_key=event_key, claim_token=claim["claim_token"])

        # Test-only crash hook (B1): deterministic "crash immediately after receipt
        # commit" - the receipt is durably 'processing'; the process dies before any
        # consumption work. Recovery re-drives it from the stored raw_text.
        if os.path.exists(CRASH_MARKER):
            os.remove(CRASH_MARKER)
            log("test_crash_after_receipt_commit")
            os._exit(1)

        try:
            # Test-only failure hook (B1): deterministic "failure before consumption
            # persistence" - fail_receipt releases the claim; recovery re-drives.
            if os.path.exists(FAIL_MARKER):
                os.remove(FAIL_MARKER)
                raise RuntimeError("test: simulated transient failure before consumption")
            op_id, created_op = persist_consumption(
                event_key=event_key, bot_id=bot_id, chat_id=chat_id,
                message_id=message_id, text=text, digest=digest,
            )
        except Exception as exc:
            # Release the claim so the update retries instead of being stranded
            # in 'processing' until the lease expires.
            with conn, conn.cursor() as cur:
                released = fail_receipt(cur, event_key, claim["claim_token"], repr(exc))
            _count("consume_failed_claim_released")
            log("consume_failed_claim_released", event_key=event_key,
                released=released, error=repr(exc))
            return

        with conn, conn.cursor() as cur:
            if not complete_receipt(cur, event_key, claim["claim_token"], op_id):
                log("complete_denied_not_owner", event_key=event_key)
        log("receipt_completed", event_key=event_key, operation_id=str(op_id),
            created=created_op)

        # --- reply delivery ---
        if created_op:
            state = deliver_reply(op_id, chat_id, f"Logged: {text}")
            log("reply_state", operation_id=str(op_id), state=state)
        else:
            log("replay_no_second_reply", event_key=event_key)
    finally:
        conn.close()


def recover_receipts(*, limit: int | None = None) -> dict:
    """Re-drive unfinished receipts from PostgreSQL WITHOUT Telegram redelivery.

    Review #1: a crash after the receipt commit, or a failure before consumption
    persistence, leaves a receipt that is not 'completed'. This scans those receipts
    (lease expired = stale-worker fencing, or free), and re-runs the SAME pipeline
    from the stored raw_text. A permanent failure eventually hits the attempts cap and
    is left for an operator rather than spinning forever.
    """
    conn = _conn()
    rows = []
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT event_key, bot_id, chat_id, message_id, update_id,
                       content_digest, raw_text, kind, attempts
                  FROM telegram_receipts
                 WHERE status <> 'completed'
                   AND attempts < %s
                   AND (lease_expires_at IS NULL OR lease_expires_at < now())
                 ORDER BY received_at
                 LIMIT %s
                """,
                (RECEIPT_MAX_ATTEMPTS, limit or RECEIPT_RECOVERY_LIMIT),
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    counts = {"redriven": 0, "claim_denied": 0, "failed": 0, "exhausted": 0}
    for r in rows:
        try:
            process_update(
                event_key=r["event_key"], bot_id=r["bot_id"], chat_id=r["chat_id"],
                message_id=r["message_id"], update_id=r["update_id"],
                digest=r["content_digest"], text=r["raw_text"], kind=r["kind"],
            )
            counts["redriven"] += 1
        except SystemExit:
            raise
        except Exception as exc:  # pragma: no cover - surfaced in the log
            counts["failed"] += 1
            log("receipt_recovery_error", event_key=r["event_key"], error=repr(exc))

    # REVIEW #1: a receipt at or over the retry budget is EXCLUDED by the scan's
    # `attempts < cap` filter, so without this sweep it silently stalls in 'received'
    # forever - neither redriven nor reported. Mark it operator-visible instead.
    conn2 = _conn()
    try:
        with conn2, conn2.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE telegram_receipts
                   SET status = 'exhausted',
                       claim_token = NULL,
                       claimed_by = NULL,
                       lease_expires_at = NULL,
                       last_error = COALESCE(NULLIF(last_error, ''), '') ||
                           CASE WHEN COALESCE(last_error, '') = '' THEN '' ELSE ' | ' END ||
                           'attempt budget exhausted after ' || attempts || ' claims'
                 WHERE status NOT IN ('completed', 'exhausted')
                   AND attempts >= %s
                RETURNING event_key, attempts
                """,
                (RECEIPT_MAX_ATTEMPTS,),
            )
            newly = [dict(r) for r in cur.fetchall()]
    finally:
        conn2.close()
    if newly:
        counts["exhausted"] = len(newly)
        _count("receipt_attempt_budget_exhausted", len(newly))
        log("receipts_marked_exhausted",
            receipts=[r["event_key"] for r in newly])
    if rows or newly:
        log("receipt_recovery_pass", **counts)
    return counts


def consume_once(bot_id: str) -> int:
    """One poll/process cycle. Exactly one consumer performs this."""
    offset = read_offset()
    try:
        updates = _api("getUpdates", {"offset": offset, "timeout": 0})["result"]
    except Exception as exc:
        log("poll_failed", error=repr(exc))
        return 0
    if not updates:
        return 0

    processed = 0
    highest = None
    for update in updates:
        msg = update.get("message") or update.get("edited_message")
        highest = update["update_id"] if highest is None else max(highest, update["update_id"])
        if not msg:
            continue

        chat_id = str(msg["chat"]["id"])
        message_id = str(msg["message_id"])
        text = msg.get("text") or ""
        event_key = f"{bot_id}:{chat_id}:{message_id}"
        digest = hashlib.sha256(text.encode()).hexdigest()
        kind = "edited" if "edited_message" in update else "created"

        conn = _conn()
        stored = None
        created = False
        try:
            # RealDictCursor for NAMED row access. psycopg2's cursor.execute()
            # returns None, so a chained `cur.execute(...).fetchone()` raises
            # AttributeError before any status is inspected - and positional
            # indexing into a mis-listed column order silently reads the wrong
            # field or runs off the end. Named access removes both failure modes.
            with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                created = write_receipt(
                    cur, event_key=event_key, bot_id=bot_id, chat_id=chat_id,
                    message_id=message_id, update_id=update["update_id"],
                    digest=digest, raw_text=text, kind=kind,
                )
                if created:
                    log("receipt_durable", event_key=event_key)
                else:
                    cur.execute(
                        "SELECT status, raw_text, bot_id, chat_id, message_id, "
                        "update_id, content_digest, kind, attempts "
                        "FROM telegram_receipts WHERE event_key=%s",
                        (event_key,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        # Unreachable in practice (the INSERT conflicted), but never
                        # silently continue on a missing row.
                        _count("receipt_duplicate_row_missing")
                        log("receipt_duplicate_row_missing", event_key=event_key)
                        process_update(event_key=event_key, bot_id=bot_id,
                                       chat_id=chat_id, message_id=message_id,
                                       text=text, update_id=update["update_id"],
                                       digest=digest, kind=kind)
                        processed += 1
                        continue

                    status = row["status"]
                    if status == "completed":
                        # Replay: this event already fully processed. Inert.
                        _count("receipt_duplicate_completed")
                        log("receipt_duplicate_completed", event_key=event_key)
                        processed += 1
                        continue
                    if status == "exhausted":
                        # Retry budget spent. Operator-visible; do not spin.
                        log("receipt_duplicate_exhausted", event_key=event_key,
                            attempts=row["attempts"])
                        processed += 1
                        continue
                    # UNFINISHED work from a previous crash/failure. Re-drive it from
                    # the STORED payload - do not discard it just because the receipt
                    # already exists (review #1). NOTE: the STORED text, not the
                    # newly delivered text, is authoritative.
                    _count("receipt_duplicate_unfinished")
                    log("receipt_duplicate_unfinished_redriven", event_key=event_key,
                        status=status)
                    stored = {
                        "bot_id": row["bot_id"], "chat_id": row["chat_id"],
                        "message_id": row["message_id"], "update_id": row["update_id"],
                        "digest": row["content_digest"], "text": row["raw_text"],
                        "kind": row["kind"],
                    }
        finally:
            conn.close()

        if created:
            process_update(event_key=event_key, bot_id=bot_id, chat_id=chat_id,
                           message_id=message_id, text=text,
                           update_id=update["update_id"], digest=digest, kind=kind)
        else:
            process_update(event_key=event_key,
                           bot_id=stored["bot_id"], chat_id=stored["chat_id"],
                           message_id=stored["message_id"], update_id=stored["update_id"],
                           digest=stored["digest"], text=stored["text"],
                           kind=stored["kind"])

        processed += 1

    # Confirm everything we just fetched, once, after processing.
    if highest is not None:
        commit_offset(highest + 1)

    return processed


# --------------------------------------------------------------------------- #
# durable polling offset (review #7 item 7: real polling-offset contract)
# --------------------------------------------------------------------------- #
# Acknowledgement is NOT a separate endpoint call. The Bot API confirms updates by
# OFFSET: `getUpdates(offset=N)` discards everything below N and returns N onwards.
# The previous code posted to a bespoke `/ack` route that no longer exists, so acks
# silently failed and the same updates were re-served forever. The offset is now
# DURABLE in PostgreSQL, so a restart resumes rather than replaying the queue.
#
# The offset is written AFTER processing, deliberately: a crash re-fetches the
# unconfirmed updates instead of skipping them. Re-processing is safe because the
# durable receipt (unique per bot/chat/message) makes it idempotent - the offset is
# an efficiency measure, never the correctness boundary.
CONSUMER = "telegram"


def read_offset() -> int:
    """Next offset to request (i.e. highest confirmed update_id + 1)."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT last_update_id FROM telegram_consumer_offset WHERE consumer = %s",
                (CONSUMER,),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def commit_offset(next_offset: int) -> None:
    """Confirm every update below `next_offset` (monotonic; never moves backwards)."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO telegram_consumer_offset
                    (consumer, last_update_id, owner_id, lease_expires_at)
                VALUES (%s, %s, %s, now() + make_interval(secs => %s))
                ON CONFLICT (consumer) DO UPDATE
                   SET last_update_id = GREATEST(
                           telegram_consumer_offset.last_update_id,
                           EXCLUDED.last_update_id),
                       owner_id = EXCLUDED.owner_id,
                       lease_expires_at = EXCLUDED.lease_expires_at
                """,
                (CONSUMER, next_offset, _process_owner, LEASE_S),
            )
    finally:
        conn.close()



# --------------------------------------------------------------------------- #
# reply resolution (the `unknown` state)
# --------------------------------------------------------------------------- #

ADMIN_TOKEN = os.environ.get("INGRESS_ADMIN_TOKEN", "")

RESOLVABLE_STATES = ("unknown", "failed")


class ResolutionError(Exception):
    def __init__(self, code: str, http_status: int = 400, **extra):
        super().__init__(code)
        self.code, self.http_status, self.extra = code, http_status, extra


def _authenticate(header: str | None) -> str | None:
    """Resolve the authenticated principal from the Bearer token.

    Review #3: the principal is derived from VERIFIED CREDENTIALS (the shared admin
    token), never from caller-supplied JSON. A valid token yields the fixed
    ADMIN_IDENTITY; anything else yields None (unauthenticated). An unset token
    means authentication is DISABLED (fail closed). This is an ADMINISTRATOR-ONLY
    surface: there is one trusted admin identity bound to the token.
    """
    if not ADMIN_TOKEN:
        return None
    if not header or not header.startswith("Bearer "):
        return None
    if hmac.compare_digest(header[len("Bearer "):].strip(), ADMIN_TOKEN):
        return ADMIN_IDENTITY
    return None


def resolve_reply(*, operation_id: str, principal: str, action: str,
                  claim_chat_id: str, note: str | None = None,
                  duplicate_risk_ack: object = None) -> dict:
    """Resolve an `unknown`/`failed` reply intent. Administrator-only, audited.

    `principal` is the AUTHENTICATED administrator (from the token, review #3), never
    a caller-supplied actor string - so the audit trail records who actually acted.

    action="acknowledge": record that the ambiguity is accepted and no resend will
        be attempted. Terminal; the consumption is untouched.
    action="resend":      perform an explicit new delivery attempt. Requires
        duplicate_risk_ack=True, because delivery may ALREADY have happened. The
        attempt is fenced by a NEW current_attempt_id (review #4) so a late result
        from an OLD attempt cannot overwrite it. The consumption is NEVER recreated.

    Concurrency: the outbox row is locked FOR UPDATE and its state re-checked under
    the lock. A racing resolution observes the already-claimed state and is refused
    (409 state_changed).
    """
    if action not in ("acknowledge", "resend"):
        raise ResolutionError("unknown_action")
    # REVIEW #4 (strict confirmation): a resend requires the LITERAL JSON boolean
    # true. `is not True` rejects false, null, missing, numbers, and strings - the
    # string "false" is truthy, so a falsy coercion check would accept a refusal.
    if action == "resend" and duplicate_risk_ack is not True:
        raise ResolutionError(
            "duplicate_risk_not_acknowledged",
            detail="duplicate_risk_ack must be the JSON boolean true",
        )

    conn = _conn()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT o.operation_id, o.chat_id, o.body, o.reply_state, o.attempts,
                           -- REVIEW #4: count DISTINCT attempt ids, not audit ROWS.
                           -- Each attempt writes a start row and a completion row, so
                           -- count(*) double-counted every resend.
                           (SELECT count(DISTINCT a.attempt_id) FROM reply_audit a
                             WHERE a.operation_id = o.operation_id
                               AND a.action = 'resend'
                               AND a.attempt_id IS NOT NULL) AS resends
                      FROM reply_outbox o
                     WHERE o.operation_id = %s
                     FOR UPDATE
                    """,
                    (str(operation_id),),
                )
                row = cur.fetchone()
                if row is None:
                    raise ResolutionError("not_found", http_status=404)

                # Administrator-only policy: the reply must belong to a chat bound in
                # reply_owners. The claim_chat_id is a SELECTION SCOPE (which reply),
                # not an identity claim - the identity is the authenticated principal.
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
                    # No attempt is in flight; clear any stale fencing id.
                    cur.execute(
                        """
                        UPDATE reply_outbox
                           SET reply_state = %s, resolved_by = %s, resolved_at = now(),
                               current_attempt_id = NULL, updated_at = now()
                         WHERE operation_id = %s
                        """,
                        (to_state, principal, str(operation_id)),
                    )
                    cur.execute(
                        """
                        INSERT INTO reply_audit (operation_id, actor, action, from_state,
                                                 to_state, delivery_attempt, detail)
                        VALUES (%s, %s, 'acknowledge', %s, %s, %s, %s)
                        """,
                        (str(operation_id), principal, from_state, to_state, attempt_no,
                         note or "ambiguity accepted; no resend attempted"),
                    )
                    return {"ok": True, "action": action, "to_state": to_state,
                            "delivery_attempt": attempt_no, "consumption_untouched": True}

                # --- explicit resend -------------------------------------------------
                # A new, separately counted, FENCED delivery attempt. Nothing about
                # the consumption is touched: we do not re-run the write path.
                #
                # CRITICAL ORDERING (review #4): claim the row OUT of the resolvable
                # set inside this locked transaction, BEFORE releasing the lock. The
                # send happens after the lock is dropped (a slow network call must not
                # block other work). Marking 'in_flight' with a NEW current_attempt_id
                # makes a racing resolver observe a state it cannot resolve, and fences
                # the completion to this attempt only.
                resend_attempt_id = str(uuid.uuid4())
                cur.execute(
                    """
                    UPDATE reply_outbox
                       SET reply_state = 'in_flight', resolved_by = %s,
                           current_attempt_id = %s, in_flight_at = now(),
                           updated_at = now()
                     WHERE operation_id = %s
                    """,
                    (principal, resend_attempt_id, str(operation_id)),
                )
                cur.execute(
                    """
                    INSERT INTO reply_audit (operation_id, actor, action, from_state,
                                             to_state, delivery_attempt, attempt_id,
                                             applied, detail)
                    VALUES (%s, %s, 'resend', %s, 'in_flight', %s, %s, true, %s)
                    """,
                    (str(operation_id), principal, from_state, attempt_no + 1,
                     resend_attempt_id,
                     note or "explicit resend after ambiguous delivery; "
                             "delivery may already have occurred"),
                )

        # Send OUTSIDE the transaction that holds the lock, so a slow network call
        # does not block other work. The audit row above is already durable.
        try:
            _api("sendMessage", {"chat_id": row["chat_id"],
                                 "text": row["body"] or "Logged."})
            outcome, error = "delivered", None
        except Exception as exc:
            outcome, error = _classify_delivery_exc(exc)

        if outcome == "delivered":
            to_state, terminal_error = "resolved_resent", None
        elif outcome == "failed":
            to_state, terminal_error = "failed", error       # known-safe, retryable
        else:
            to_state, terminal_error = "unknown", error      # ambiguous again

        conn2 = _conn()
        try:
            with conn2:
                # REVIEW #4: the fenced UPDATE can legitimately affect ZERO rows when a
                # newer attempt has taken over (current_attempt_id has moved on). The
                # outcome is still an OBSERVATION worth recording, but it is NOT an
                # applied state transition, and it must not be reported as one.
                with conn2.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        UPDATE reply_outbox
                           SET reply_state = %s, last_error = %s, resolved_by = %s,
                               resolved_at = now(), current_attempt_id = NULL,
                               updated_at = now()
                         WHERE operation_id = %s AND current_attempt_id = %s
                        """,
                        (to_state, terminal_error, principal,
                         str(operation_id), resend_attempt_id),
                    )
                    applied = cur.rowcount == 1

                    # Read the TRUTHFUL current state, whether or not this attempt
                    # applied. Callers must not infer the row's state from our result.
                    cur.execute(
                        "SELECT reply_state, current_attempt_id FROM reply_outbox "
                        "WHERE operation_id = %s",
                        (str(operation_id),),
                    )
                    now_row = cur.fetchone() or {}
                    current_state = now_row.get("reply_state")
                    current_attempt = now_row.get("current_attempt_id")

                    # Immutable record of WHAT was observed and whether it changed
                    # state. `applied=false` marks a stale observation so the audit
                    # trail cannot be read as a transition that never happened.
                    cur.execute(
                        """
                        INSERT INTO reply_audit (operation_id, actor, action, from_state,
                                                 to_state, delivery_attempt, attempt_id,
                                                 applied, detail)
                        VALUES (%s, %s, 'resend', 'in_flight', %s, %s, %s, %s, %s)
                        """,
                        (str(operation_id), principal,
                         to_state if applied else (current_state or to_state),
                         attempt_no + 1, resend_attempt_id, applied,
                         (f"resend outcome={outcome} error={terminal_error}"
                          if applied else
                          f"STALE observation from attempt {resend_attempt_id}: "
                          f"outcome={outcome} error={terminal_error}; superseded by "
                          f"attempt {current_attempt}; state unchanged")),
                    )
        finally:
            conn2.close()

        log("reply_resolved", operation_id=str(operation_id), actor=principal,
            action=action, outcome=outcome, delivery_attempt=attempt_no + 1,
            applied=applied)

        result = {"ok": True, "action": action, "outcome": outcome, "applied": applied,
                  "delivery_attempt": attempt_no + 1, "attempt_id": resend_attempt_id,
                  "consumption_untouched": True}
        if applied:
            result["to_state"] = to_state
        else:
            # Honest reporting: this attempt did NOT change the state. The observed
            # delivery result is recorded, but the row's state is the newer one.
            result["stale"] = True
            result["to_state"] = current_state
            result["current_state"] = current_state
            result["current_attempt_id"] = current_attempt
            result["detail"] = ("delivery was observed but this attempt was superseded; "
                               "state unchanged")
        return result
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
        # Every admin route is authenticated (review #2/#3): the model-accessible
        # network must not reach an unauthenticated surface that discloses trusted
        # data or changes state. The GET /admin/recover route both changes state and
        # may send replies, so it is no less protected than the POST surface.
        if not self.path.startswith("/admin/"):
            return self._json({"ok": False}, 404)
        principal = _authenticate(self.headers.get("Authorization"))
        if principal is None:
            return self._json({"ok": False, "error": "unauthorised"}, 401)

        if self.path == "/admin/recover":
            # BOTH recovery paths, so a test (or an operator) can drive them
            # deterministically instead of waiting for the periodic interval.
            # Reply recovery: pending/failed are re-sent, abandoned in_flight -> unknown.
            reply_counts = recover()
            receipt_counts = recover_receipts()
            return self._json({"ok": True, "counts": reply_counts,
                               "receipt_counts": receipt_counts})
        if self.path == "/admin/status":
            conn = _conn()
            try:
                with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT event_key, status, operation_id FROM telegram_receipts ORDER BY id")
                    receipts = [dict(r) for r in cur.fetchall()]
                    cur.execute("SELECT operation_id, reply_state, attempts, last_error, "
                                "current_attempt_id FROM reply_outbox ORDER BY created_at")
                    outbox = [dict(r) for r in cur.fetchall()]
                    cur.execute("SELECT count(*) AS n FROM consumption_operations")
                    ops = cur.fetchone()["n"]
                return self._json({"ok": True, "receipts": receipts, "outbox": outbox,
                                   "consumption_operations": ops,
                                   "counters": counters(),
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
                               delivery_attempt, attempt_id, applied, detail, created_at
                          FROM reply_audit ORDER BY id
                        """
                    )
                    rows = [dict(r) for r in cur.fetchall()]
                return self._json({"ok": True, "audit": rows})
            finally:
                conn.close()
        if self.path == "/admin/real-output":
            conn = _conn()
            try:
                with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT m.id AS meal_id, m.meal_type, m.totals_json FROM meals m ORDER BY m.created_at"
                    )
                    meals = [dict(r) for r in cur.fetchall()]
                    cur.execute(
                        "SELECT mi.id, mi.display_name, mi.grams, mi.volume_ml, mi.beverage_category, mi.meal_id FROM meal_items mi ORDER BY mi.display_name"
                    )
                    items = [dict(r) for r in cur.fetchall()]
                    cur.execute(
                        "SELECT id, metric_type, unit, value_json, source_operation_id, source_item_id, meal_item_id FROM measurements WHERE source_provider = 'consumption' ORDER BY created_at"
                    )
                    measurements = [dict(r) for r in cur.fetchall()]
                    cur.execute("SELECT count(*) AS n FROM beverage_measurements")
                    bev_links = cur.fetchone()["n"]
                    cur.execute("SELECT count(*) AS n FROM consumption_operations WHERE status = 'completed'")
                    completed_ops = cur.fetchone()["n"]
                    cur.execute("SELECT count(*) AS n FROM consumption_operations")
                    all_ops = cur.fetchone()["n"]
                return self._json({
                    "ok": True, "meals": meals, "meal_items": items,
                    "measurements": measurements, "beverage_measurements": bev_links,
                    "completed_operations": completed_ops, "total_operations": all_ops,
                })
            finally:
                conn.close()
        return self._json({"ok": False}, 404)

    def do_POST(self):
        if self.path != "/admin/reply/resolve":
            return self._json({"ok": False}, 404)
        principal = _authenticate(self.headers.get("Authorization"))
        if principal is None:
            return self._json({"ok": False, "error": "unauthorised"}, 401)

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json({"ok": False, "error": "bad_json"}, 400)

        try:
            result = resolve_reply(
                operation_id=payload["operation_id"],
                principal=principal,                       # from the token, not the body
                action=payload.get("action", ""),
                claim_chat_id=str(payload.get("claim_chat_id", "")),
                note=payload.get("note"),
                # REVIEW #4 (strict confirmation): pass the RAW value and let
                # resolve_reply require the literal JSON boolean true. Coercing with
                # bool() accepts non-boolean truthy values - the string "false" is
                # truthy - which would let a resend proceed without real consent.
                duplicate_risk_ack=payload.get("duplicate_risk_ack", None),
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
    """Serve the admin surface bound to the trusted loopback (review #2).

    Binding to 127.0.0.1 keeps the surface off the turn-facing interface, so the
    model-accessible containers cannot reach it at all. The trusted harness reaches
    it by exec'ing INTO the ingress and calling localhost. (Routes are ALSO
    authenticated, defence in depth.)
    """
    port = int(os.environ.get("INGRESS_ADMIN_PORT", "8082"))
    ThreadingHTTPServer((INGRESS_ADMIN_BIND, port), AdminHandler).serve_forever()


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

    # Recovery first. Reply recovery: pending is safe, abandonment becomes unknown.
    # Receipt recovery: unfinished receipts are re-driven from the stored payload
    # (review #1), covering a crash immediately after the receipt commit.
    recover()
    recover_receipts()

    next_recovery = time.time() + RECOVERY_INTERVAL_S
    while True:
        try:
            consume_once(bot_id)
        except Exception as exc:
            _count("consume_error")
            log("consume_error", error=repr(exc))

        # Periodic recovery: reply delivery state AND unfinished receipts.
        if time.time() >= next_recovery:
            try:
                recover()
                recover_receipts()
            except Exception as exc:
                log("recovery_error", error=repr(exc))
            next_recovery = time.time() + RECOVERY_INTERVAL_S

        time.sleep(0.5)


if __name__ == "__main__":
    main()
