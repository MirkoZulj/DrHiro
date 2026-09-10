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
ADMIN = f"http://localhost:{os.environ.get('INGRESS_ADMIN_PORT', '8082')}"


def _call(url: str, payload: dict | None = None):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _get(url: str):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


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
        print(json.dumps(_get(f"{ADMIN}/admin/status")))
    elif cmd == "recover":
        print(json.dumps(_get(f"{ADMIN}/admin/recover")))
    elif cmd == "counts":
        print(json.dumps(counts()))
    else:
        print(json.dumps({"error": f"unknown command {cmd}"}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
