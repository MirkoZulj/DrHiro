#!/usr/bin/env python3
"""Suite K — deploy smokes and the production-shaped schema guard.

Part 1 (K1-K4, K6) runs against the disposable database at c4e5f6a7b8c9.
Part 2 (K7-K10) runs against a scratch database shaped like PRODUCTION:
no `consumption_operations`, no `deleted_at` columns.

Nothing here touches the VPS. No production connection is made.
"""
from __future__ import annotations

import logging
import os
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1] if "__file__" in dir() else Path.cwd()
sys.path.insert(0, "/home/mirko/work/DrHiro/apps/api/src")

DISPOSABLE = "postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_logtest"
PRODSHAPE = "postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_prodshape"

os.environ["DRHIRO_REDIS_URL"] = "redis://localhost:6379/0"
os.environ.pop("DRHIRO_TELEGRAM_INGRESS_SECRET", None)
os.environ.pop("DRHIRO_TELEGRAM_BOT_ID", None)
os.environ.pop("DRHIRO_TELEGRAM_INGRESS_ENABLED", None)

CHAT = "1001"
RESULTS: list[tuple[str, bool, str]] = []


def record(kid: str, ok: bool, detail: str) -> None:
    RESULTS.append((kid, ok, detail))
    print(f"{kid}: {'PASS' if ok else 'FAIL'}  {detail}")


def make_user(dbmod, telegram_id: str) -> str:
    from drhiro_api.models import ExternalIdentity, User
    with dbmod.SessionLocal() as db:
        u = User(display_name="K Probe", status="active", timezone="Europe/Zagreb")
        db.add(u)
        db.flush()
        db.add(ExternalIdentity(user_id=u.id, provider="telegram",
                                provider_subject=telegram_id))
        db.commit()
        return str(u.id)


def counts(engine, uid):
    from sqlalchemy import text
    with engine.connect() as c:
        def n(sql):
            return c.execute(text(sql), {"u": uid}).scalar()
        return {
            "meals": n("SELECT count(*) FROM meals WHERE user_id = :u AND status <> 'deleted'"),
            "items": n("SELECT count(*) FROM meal_items mi JOIN meals m ON m.id = mi.meal_id WHERE m.user_id = :u"),
            "liquids": n("SELECT count(*) FROM measurements WHERE user_id = :u"),
            "slots": [r[0] for r in c.execute(text("SELECT meal_type FROM meals WHERE user_id = :u"), {"u": uid})],
        }


def run(label: str, url: str, telegram_id: str, do_k6: bool, do_ledger: bool):
    print(f"\n{'=' * 70}\n{label}  ({url.rsplit('/', 1)[-1]})\n{'=' * 70}")
    os.environ["DRHIRO_DATABASE_URL"] = url
    for mod in [m for m in list(sys.modules) if m.startswith("drhiro_api")]:
        del sys.modules[mod]

    from fastapi.testclient import TestClient
    from drhiro_api import db as dbmod
    from drhiro_api.main import app
    from drhiro_api.security import create_service_token

    engine = dbmod.engine
    uid = make_user(dbmod, telegram_id)
    headers = {"X-Service-Token": create_service_token(), "X-Telegram-Id": telegram_id}
    client = TestClient(app, raise_server_exceptions=False)

    from sqlalchemy import text
    with engine.connect() as c:
        has_conv = c.execute(text("SELECT to_regclass('public.consumption_operations')")).scalar()
        has_del = c.execute(text(
            "SELECT count(*) FROM information_schema.columns WHERE column_name='deleted_at'"
            " AND table_name IN ('measurements','activities')")).scalar()
    print(f"schema: consumption_operations={'present' if has_conv else 'ABSENT'}, "
          f"deleted_at columns={has_del}")

    # ---- capture the logger for K9 ----
    records: list[str] = []

    class Cap(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())

    cap = Cap()
    logging.getLogger("drhiro_api.services.log_intents").addHandler(cap)
    logging.getLogger("drhiro_api.services.log_intents").setLevel(logging.WARNING)

    if do_k6:
        # K6: starting the API must not create the identity table.
        h = client.get("/health")
        with engine.connect() as c:
            after = c.execute(text("SELECT to_regclass('public.consumption_operations')")).scalar()
        record("K6", after is None,
               f"API start did not create consumption_operations "
               f"(health={h.status_code}, to_regclass=None)")

    if do_ledger:
      # K7 / K8 / K9 / K10 — the drink write on THIS schema (prod-shaped only).
      r1 = client.post("/api/v1/tools/create_meal_from_text",
                     json={"text": "300ml orange juice", "telegram_chat_id": CHAT,
                           "telegram_message_id": "70001"}, headers=headers)
      c1 = counts(engine, uid)
      record("K7", r1.status_code == 200 and c1["meals"] == 1 and c1["liquids"] == 1,
           f"status={r1.status_code} meals={c1['meals']} liquids={c1['liquids']}")

      body = r1.text
      err = r1.status_code >= 500 or "deleted_at" in body.lower()
      record("K8", not err,
           f"no 5xx and no 'deleted_at' in the response ({r1.status_code})")

      n_log = sum(1 for m in records if "idempotency_disabled_missing_schema" in m)
      record("K9", n_log >= 1, f"idempotency_disabled_missing_schema logged {n_log}x")

      r2 = client.post("/api/v1/tools/create_meal_from_text",
                     json={"text": "300ml orange juice", "telegram_chat_id": CHAT,
                           "telegram_message_id": "70001"}, headers=headers)
      c2 = counts(engine, uid)
      dup = c2["meals"] == 2 and c2["liquids"] == 2
      record("K10", r2.status_code == 200 and dup,
           f"EXPECTED duplicate when the schema is absent: status={r2.status_code} "
           f"meals={c2['meals']} liquids={c2['liquids']} (de-dup unavailable "
           f"without the identity table)")

    # K3 (only meaningful where the snapshot is clean)
    r3 = client.post("/api/v1/tools/create_meal_from_text",
                     json={"text": "eggs and toast", "telegram_chat_id": CHAT,
                           "telegram_message_id": "70002"}, headers=headers)
    c3 = counts(engine, uid)
    slots = [s for s in c3["slots"] if s]
    record("K3", r3.status_code == 200 and "snack" in slots,
           f"status={r3.status_code} slots={c3['slots']}")

    logging.getLogger("drhiro_api.services.log_intents").removeHandler(cap)
    return client


def main() -> int:
    # ---------- Part 1: disposable ----------
    telegram_id = str(uuid.uuid4().int)[:12]
    client = run("PART 1 — disposable (c4e5f6a7b8c9)", DISPOSABLE, telegram_id,
                do_k6=False, do_ledger=False)

    h = client.get("/health")
    record("K1", h.status_code == 200, f"GET /health -> {h.status_code}")

    r = client.post("/api/v1/ingest/telegram/event", json={"update_id": 1})
    ok = r.status_code == 503 and "ingress_secret_not_configured" in r.text
    record("K2", ok, f"POST /api/v1/ingest/telegram/event -> {r.status_code} {r.text[:70]}")

    from sqlalchemy import create_engine, text
    e = create_engine(DISPOSABLE)
    with e.connect() as c:
        v = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    record("K4", v == "c4e5f6a7b8c9" and v != "b7c8d9e0f1a2",
           f"alembic current on disposable = {v}")

    # ---------- Part 2: production-shaped ----------
    ptid = str(uuid.uuid4().int)[:12]
    run("PART 2 — production-shaped scratch DB", PRODSHAPE, ptid,
        do_k6=True, do_ledger=True)

    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    for kid, ok, detail in RESULTS:
        print(f"  {kid:4} {'PASS' if ok else 'FAIL'}")
    failed = [k for k, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed"
          + (f"  FAILED: {failed}" if failed else "  (all pass)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
