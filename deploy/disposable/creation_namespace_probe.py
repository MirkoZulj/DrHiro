#!/usr/bin/env python3
"""Compare PostgreSQL's native unqualified-CREATE destination with the destination this
module's helper chooses, where the FIRST search_path schema lacks CREATE.

Scenario: search_path = first, second; `first` grants only USAGE; `second` grants
CREATE. PostgreSQL 16 native behaviour, verified here, is to target `first` and fail
with `permission denied for schema first` - it does NOT scan for a schema granting
CREATE. A helper that scanned for the first CREATE-granting schema would silently
create the table somewhere native SQL never would.

Usage: creation_namespace_probe.py <source-root>
Prints NATIVE / HELPER / AGREE or DIVERGE lines.
"""
import sys

from sqlalchemy import create_engine, text

sys.path.insert(0, sys.argv[1] + "/apps/api/src")

import drhiro_api.schema_activities as sa  # noqa: E402

URL = "postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_test"
ROLE = "creation_probe_role"


def setup(c):
    for n in ("first", "second"):
        c.execute(text(f"DROP SCHEMA IF EXISTS {n} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {n}"))
    c.execute(text(f"DROP ROLE IF EXISTS {ROLE}"))
    c.execute(text(f"CREATE ROLE {ROLE} NOLOGIN"))
    c.execute(text(f"GRANT USAGE ON SCHEMA first TO {ROLE}"))          # USAGE only
    c.execute(text(f"GRANT CREATE, USAGE ON SCHEMA second TO {ROLE}"))  # CREATE here


def teardown(c):
    c.execute(text("RESET ROLE"))
    for n in ("first", "second"):
        c.execute(text(f"DROP SCHEMA IF EXISTS {n} CASCADE"))
    c.execute(text(f"DROP ROLE IF EXISTS {ROLE}"))


def where(c, relname):
    return c.execute(text("""
        SELECT n.nspname FROM pg_class k JOIN pg_namespace n
        ON n.oid = k.relnamespace WHERE k.relname = :r
    """), {"r": relname}).scalars().all()


def main() -> int:
    print(f"imported: {sa.__file__}")
    engine = create_engine(URL)

    native_dest = None
    with engine.begin() as c:
        setup(c)
    try:
        with engine.begin() as c:
            c.execute(text(f"SET ROLE {ROLE}"))
            c.execute(text("SET search_path TO first, second"))
            first = c.execute(text("SELECT (current_schemas(false))[1]")).scalar()
            print(f"first search_path schema: {first}")
            try:
                c.execute(text("CREATE TABLE native_probe (id int)"))
                native_dest = where(c, "native_probe")
                print(f"NATIVE: created in {native_dest}")
            except Exception as exc:                        # noqa: BLE001
                native_dest = f"ERROR: {str(exc).splitlines()[0]}"
                print(f"NATIVE: {native_dest}")

        with engine.begin() as c:
            c.execute(text(f"SET ROLE {ROLE}"))
            c.execute(text("SET search_path TO first, second"))
            try:
                sa.create_activities(c)
                helper_dest = where(c, "activities")
                print(f"HELPER: created in {helper_dest}")
            except Exception as exc:                        # noqa: BLE001
                helper_dest = f"ERROR({type(exc).__name__}): {str(exc).splitlines()[0]}"
                print(f"HELPER: {helper_dest}")

        with engine.begin() as c:
            c.execute(text("RESET ROLE"))
            for n in ("first", "second"):
                present = c.execute(text(
                    "SELECT count(*) FROM pg_class k JOIN pg_namespace nn"
                    " ON nn.oid = k.relnamespace"
                    " WHERE k.relname IN ('activities','native_probe')"
                    " AND nn.nspname = :n"), {"n": n}).scalar()
                print(f"STATE: objects in {n} = {present}")

        native_schema = "first" if "first" in str(native_dest) else str(native_dest)
        helper_schema = "first" if "first" in str(helper_dest) else str(helper_dest)
        # Both must refuse on `first`; a helper reporting `second` would DIVERGE.
        is_err = lambda d: "ERROR" in str(d)
        agree = (is_err(native_dest) and is_err(helper_dest)
                 and native_schema == helper_schema)
        if agree:
            print(f"AGREE: native and helper both target/refuse `{native_schema}`")
        else:
            print(f"DIVERGE: native={native_dest!r} helper={helper_dest!r}")
    finally:
        with engine.begin() as c:
            teardown(c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
