"""Concurrent-writer probe against the REAL T1 service path (disposable stack).

Runs N processes that simultaneously attempt to persist the SAME Telegram identity
(bot_id, chat_id, message_id) through the real `get_or_create_operation` +
`write_consumption`. This tests concurrency at the PERSISTENCE layer.

It deliberately does NOT start a second Telegram poller: the architecture keeps
exactly one consumer, so spawning a second getUpdates loop would test something we
do not intend to support. Concurrency here is about two writers racing on the same
identity - which is what happens if a claim is stolen after lease expiry, or if a
worker is restarted while an old one is still finishing.

Prints JSON: how many consumptions, meals, meal_items and measurements were created,
plus the per-process result. A correct implementation yields exactly one meal and
one measurement no matter how many writers race.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import uuid

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/drhiro_src")

IDENTITY = {
    "bot_id": "8677922871",
    "chat_id": "555000111",
    "message_id": "800042",
}
TEXT = "200 g steak and 0.5 l beer"


def writer(idx: int, out: mp.Queue) -> None:
    import ingress  # noqa: E402  (imported per-process, after fork)

    try:
        op_id, created = ingress.persist_consumption(
            event_key=f"{IDENTITY['bot_id']}:{IDENTITY['chat_id']}:{IDENTITY['message_id']}",
            bot_id=IDENTITY["bot_id"],
            chat_id=IDENTITY["chat_id"],
            message_id=IDENTITY["message_id"],
            text=TEXT,
            digest="c" * 64,
        )
        out.put({"idx": idx, "ok": True, "operation_id": str(op_id), "created": created})
    except Exception as exc:  # pragma: no cover - surfaced to the test
        out.put({"idx": idx, "ok": False, "error": repr(exc)})


def main() -> int:
    import psycopg2
    import psycopg2.extras

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    with conn.cursor() as cur:
        for table in ("meals", "meal_items", "measurements", "beverage_measurements",
                      "consumption_operations", "reply_outbox", "reply_audit",
                      "telegram_receipts"):
            cur.execute(f"TRUNCATE {table} CASCADE")
        cur.execute(
            "INSERT INTO reply_owners (chat_id, owner_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (IDENTITY["chat_id"], IDENTITY["chat_id"]),
        )
        # The durable receipt the consumer writes before claiming. reply_outbox has a
        # foreign key to it, so the probe must reproduce that row: this is the
        # persistence layer racing, with the receipt stage assumed already done.
        # Seeded 'completed' (not 'processing') so the LIVE ingress's periodic
        # recover_receipts() does not treat it as unfinished and re-drive it
        # concurrently with this probe's own writers - the probe must own this
        # identity exclusively to test the persistence-layer race.
        cur.execute(
            """
            INSERT INTO telegram_receipts (event_key, bot_id, chat_id, message_id,
                                           update_id, content_digest, status)
            VALUES (%s, %s, %s, %s, 1, %s, 'completed')
            ON CONFLICT (event_key) DO NOTHING
            """,
            (f"{IDENTITY['bot_id']}:{IDENTITY['chat_id']}:{IDENTITY['message_id']}",
             IDENTITY["bot_id"], IDENTITY["chat_id"], IDENTITY["message_id"],
             "c" * 64),
        )

    out: mp.Queue = mp.Queue()
    procs = [mp.Process(target=writer, args=(i, out)) for i in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)

    results = [out.get() for _ in range(n)]

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT count(*) AS n FROM consumption_operations")
        ops = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM meals")
        meals = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM meal_items")
        items = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM measurements WHERE source_provider='consumption'")
        measurements = cur.fetchone()["n"]
        cur.execute("SELECT totals_json FROM meals LIMIT 1")
        row = cur.fetchone()
        totals = dict(row["totals_json"]) if row else None
    conn.close()

    print(json.dumps({
        "writers": n,
        "results": results,
        "creations": sum(1 for r in results if r.get("created")),
        "duplicates_reported": sum(1 for r in results if r.get("ok") and not r.get("created")),
        "errors": [r["error"] for r in results if not r.get("ok")],
        "state": {"consumption_operations": ops, "meals": meals,
                  "meal_items": items, "measurements": measurements,
                  "totals": totals},
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
