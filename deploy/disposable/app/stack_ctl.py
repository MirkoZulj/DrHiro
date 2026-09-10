"""Test control surface, run INSIDE the stack (via docker compose exec).

Lets the tests drive the stack and read trusted state without publishing any port or
putting credentials on the host. Executed as:

    docker compose exec -T ingress python /app/stack_ctl.py <command> [args]

Commands:
    enqueue <chat_id> <text>      queue a Telegram update in the fake API
    mode <normal|accept_then_hang|refuse>
    sent                          what the fake API has actually delivered
    reset                         clear the fake API and its delivery log
    status                        receipts + outbox + consumption op count
    recover                       run the ingress recovery pass now
    counts                        compact counts for assertions

Every command prints JSON.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

import psycopg2
import psycopg2.extras

TELEGRAM_API = os.environ.get("TELEGRAM_API_URL", "http://fake-telegram:8081")
INGRESS_ADMIN = f"http://localhost:{os.environ.get('INGRESS_ADMIN_PORT', '8082')}"
ADMIN = INGRESS_ADMIN


def _call(url: str, payload: dict | None = None, token: str | None = None):
    """POST JSON. Non-2xx responses are returned as data so tests can assert on the
    error code (401 unauthorised, 403 not_owner, 409 state_changed) instead of
    crashing."""
    data = json.dumps(payload or {}).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {**json.loads(resp.read()), "http_status": resp.status}
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except Exception:
            body = {}
        return {**body, "http_status": exc.code}


def _get(url: str, token: str | None = None):
    """GET. Admin routes require the Bearer token (review #2); the fake-API control
    routes do not. Non-2xx responses are returned as data so tests can assert on the
    error code instead of crashing."""
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {**json.loads(resp.read()), "http_status": resp.status}
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except Exception:
            body = {}
        return {**body, "http_status": exc.code}


_ADMIN_TOKEN = os.environ.get("INGRESS_ADMIN_TOKEN", "")


def counts() -> dict:
    # Read lazily: this script also runs in containers that have no database
    # (e.g. the fake Telegram container) for API-only commands.
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            out = {}
            for table, key in (
                ("telegram_receipts", "receipts"),
                ("reply_outbox", "outbox"),
                ("consumption_operations", "consumption_operations"),
            ):
                cur.execute(f"SELECT count(*) AS n FROM {table}")
                out[key] = cur.fetchone()["n"]
            cur.execute(
                "SELECT status, count(*) AS n FROM telegram_receipts GROUP BY status"
            )
            out["receipt_status"] = {r["status"]: r["n"] for r in cur.fetchall()}
            cur.execute(
                "SELECT reply_state, count(*) AS n FROM reply_outbox GROUP BY reply_state"
            )
            out["reply_state"] = {r["reply_state"]: r["n"] for r in cur.fetchall()}
            cur.execute(
                "SELECT count(*) AS n FROM consumption_operations "
                "WHERE source_bot_id IS NOT NULL AND source_message_id IS NOT NULL"
            )
            out["telegram_consumptions"] = cur.fetchone()["n"]
            # Operation ids per state, so a test can act on a specific reply.
            for label, state in (("unknown", "unknown"), ("sent", "sent"),
                                 ("failed", "failed")):
                cur.execute(
                    "SELECT operation_id FROM reply_outbox WHERE reply_state = %s "
                    "ORDER BY created_at DESC LIMIT 1",
                    (state,),
                )
                row = cur.fetchone()
                out[f"{label}_operation_id"] = str(row["operation_id"]) if row else None
            return out
    finally:
        conn.close()


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: stack_ctl.py <command> [args]"}))
        return 2
    cmd = sys.argv[1]

    if cmd == "enqueue":
        payload = {"chat_id": sys.argv[2], "text": sys.argv[3]}
        if len(sys.argv) > 4:
            payload["message_id"] = sys.argv[4]
        print(json.dumps(_call(f"{TELEGRAM_API}/_control/enqueue", payload)))
    elif cmd == "mode":
        print(json.dumps(_call(f"{TELEGRAM_API}/_control/mode", {"mode": sys.argv[2]})))
    elif cmd == "sent":
        print(json.dumps(_get(f"{TELEGRAM_API}/_control/sent")))
    elif cmd == "reset":
        print(json.dumps(_call(f"{TELEGRAM_API}/_control/reset", {})))
    elif cmd == "status":
        print(json.dumps(_get(f"{ADMIN}/admin/status", token=_ADMIN_TOKEN)))
    elif cmd == "recover":
        print(json.dumps(_get(f"{ADMIN}/admin/recover", token=_ADMIN_TOKEN)))
    elif cmd == "real-output":
        print(json.dumps(_get(f"{INGRESS_ADMIN}/admin/real-output", token=_ADMIN_TOKEN)))
    elif cmd == "resolve":
        # resolve <operation_id> <actor> <action> <claim_chat_id> [ack]
        payload = {
            "operation_id": sys.argv[2], "actor": sys.argv[3],
            "action": sys.argv[4], "claim_chat_id": sys.argv[5],
        }
        if len(sys.argv) > 6:
            payload["duplicate_risk_ack"] = sys.argv[6] == "ack"
        print(json.dumps(_call(f"{INGRESS_ADMIN}/admin/reply/resolve", payload,
                               token=os.environ.get("INGRESS_ADMIN_TOKEN"))))
    elif cmd == "resolve_noauth":
        # Same request with NO Authorization header, to prove auth is enforced.
        payload = {
            "operation_id": sys.argv[2], "actor": "anonymous",
            "action": sys.argv[3], "claim_chat_id": sys.argv[4],
        }
        print(json.dumps(_call(f"{INGRESS_ADMIN}/admin/reply/resolve", payload,
                               token=None)))
    elif cmd == "audit":
        print(json.dumps(_get(f"{INGRESS_ADMIN}/admin/audit", token=_ADMIN_TOKEN)))
    elif cmd == "noauth_get":
        # noauth_get <path>  - GET an admin route with NO Authorization header, to
        # prove the GET surface is also authenticated (review #2).
        print(json.dumps(_get(f"{ADMIN}{sys.argv[2]}", token=None)))
    elif cmd == "late-result":
        # late-result <operation_id> <attempt_id> <state>
        # Simulate a DELAYED completion callback from an OLD delivery attempt, to
        # prove it cannot overwrite a newer resolution (review #4).
        import ingress as _I
        conn = _I._conn()
        try:
            with conn, conn.cursor() as cur:
                applied = _I._finish(cur, sys.argv[2], sys.argv[3], sys.argv[4])
        finally:
            conn.close()
        print(json.dumps({"applied": applied}))
    elif cmd == "counts":
        print(json.dumps(counts()))
    else:
        print(json.dumps({"error": f"unknown command {cmd}"}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
