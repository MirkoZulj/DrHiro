"""End-to-end check of the activities migration on DISPOSABLE databases.

Exercises both required paths through the real Alembic CLI:
  * FRESH   - a chain-built database with no activities table
  * ADOPT   - a database that already has the production-shaped table

Read-only with respect to production. Creates throwaway databases only.
"""
import os
import subprocess
import sys
import time

import psycopg2
from sqlalchemy import create_engine, inspect, text

REPO = "/home/mirko/work/DrHiro"
VENV = f"{REPO}/.venv/bin"
ADMIN = "postgresql://drhiro:drhiro@localhost:5435/postgres"
BASE = "postgresql://drhiro:drhiro@localhost:5435"
TS = int(time.time())
FRESH_DB = f"drhiro_act_fresh_{TS}"
ADOPT_DB = f"drhiro_act_adopt_{TS}"

PROD_SHAPE = """
CREATE TABLE activities (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    activity_date date NOT NULL,
    title character varying(255) NOT NULL,
    description text,
    calories_burned double precision NOT NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT activities_pkey PRIMARY KEY (id),
    CONSTRAINT activities_calories_burned_check CHECK (calories_burned >= 0::double precision),
    CONSTRAINT activities_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX idx_activities_user_date ON activities (user_id, activity_date);
"""


def make_db(name):
    c = psycopg2.connect(ADMIN)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    c.close()


def alembic(db, *args):
    env = dict(os.environ)
    env["DRHIRO_DATABASE_URL"] = f"{BASE}/{db}"
    env["PYTHONPATH"] = f"{REPO}/apps/api/src"
    r = subprocess.run(
        [f"{VENV}/alembic", *args],
        cwd=f"{REPO}/apps/api", env=env, capture_output=True, text=True,
    )
    return r.returncode, (r.stdout + r.stderr)


def snapshot(db):
    eng = create_engine(f"{BASE}/{db}")
    insp = inspect(eng)
    out = {
        "exists": "activities" in insp.get_table_names(),
        "comment": None,
        "indexes": sorted(i["name"] for i in insp.get_indexes("activities")) if False else None,
        "cols": [],
    }
    if out["exists"]:
        out["comment"] = (insp.get_table_comment("activities") or {}).get("text")
        out["indexes"] = sorted(i["name"] for i in insp.get_indexes("activities"))
        out["cols"] = sorted(c["name"] for c in insp.get_columns("activities"))
    eng.dispose()
    return out


def main():
    results = {}
    make_db(FRESH_DB)
    make_db(ADOPT_DB)
    print(f"created disposable DBs: {FRESH_DB}, {ADOPT_DB}")

    # ---------------- FRESH path ----------------
    rc, log = alembic(FRESH_DB, "upgrade", "head")
    print(f"\n[FRESH] alembic upgrade head -> rc={rc}")
    if rc != 0:
        print(log[-2000:])
        sys.exit(1)
    snap = snapshot(FRESH_DB)
    print(f"[FRESH] activities exists={snap['exists']} comment={snap['comment']!r}")
    print(f"[FRESH] indexes={snap['indexes']}")
    print(f"[FRESH] columns={snap['cols']}")
    results["fresh"] = snap

    # ---------------- ADOPT path ----------------
    rc, log = alembic(ADOPT_DB, "upgrade", "9a1b2c3d4e5f")
    print(f"\n[ADOPT] alembic upgrade 9a1b2c3d4e5f -> rc={rc}")
    if rc != 0:
        print(log[-2000:])
        sys.exit(1)
    eng = create_engine(f"{BASE}/{ADOPT_DB}")
    with eng.begin() as c:
        for stmt in PROD_SHAPE.split(";"):
            if stmt.strip():
                c.execute(text(stmt))
    eng.dispose()
    before = snapshot(ADOPT_DB)
    print(f"[ADOPT] pre-existing production-shaped table indexes={before['indexes']}")

    rc, log = alembic(ADOPT_DB, "upgrade", "head")
    print(f"[ADOPT] alembic upgrade head (adoption) -> rc={rc}")
    print("[ADOPT] log tail:", log.strip().splitlines()[-3:])
    if rc != 0:
        print(log[-2000:])
        sys.exit(1)
    after = snapshot(ADOPT_DB)
    print(f"[ADOPT] comment={after['comment']!r} (must be None = not owned)")
    print(f"[ADOPT] indexes={after['indexes']}")
    results["adopt"] = after

    # ---------------- downgrade policy ----------------
    rc, log = alembic(ADOPT_DB, "downgrade", "-1")
    print(f"\n[ADOPT] alembic downgrade -1 -> rc={rc} (non-zero expected)")
    blocked = "downgrade unsupported" in log
    print(f"[ADOPT] refused with 'downgrade unsupported': {blocked}")
    still_there = snapshot(ADOPT_DB)["exists"]
    print(f"[ADOPT] table still present after refused downgrade: {still_there}")

    rc2, log2 = alembic(FRESH_DB, "downgrade", "-1")
    fresh_gone = not snapshot(FRESH_DB)["exists"]
    print(f"\n[FRESH] downgrade -1 -> rc={rc2}; table dropped: {fresh_gone}")

    print("\n=== VERDICT ===")
    ok = (
        results["fresh"]["exists"]
        and results["fresh"]["comment"] == "alembic:owned"
        and "ix_activities_user_id" in (results["fresh"]["indexes"] or [])
        and "idx_activities_user_date" in (results["fresh"]["indexes"] or [])
        and results["adopt"]["exists"]
        and results["adopt"]["comment"] is None
        and "ix_activities_user_id" in (results["adopt"]["indexes"] or [])
        and blocked
        and still_there
        and fresh_gone
    )
    print("ALL CHECKS PASS" if ok else "CHECKS FAILED")


if __name__ == "__main__":
    main()
