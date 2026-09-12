#!/usr/bin/env python3
"""Probe the CREATION path's same-schema policy against a given source tree.

Sets up the reviewer's counterexample in disposable PostgreSQL:

    search_path = first, second
    first  : exists, writable, NO users, NO activities
    second : users present, NO activities

An unqualified CREATE TABLE lands in `first`, while an unqualified
`REFERENCES users(id)` resolves to `second.users` - a cross-schema relationship the
same-schema validator rejects. A compliant create_activities must REFUSE (the
destination schema has no users of its own) and leave no activities table or index.

Usage: creation_path_probe.py <source-root>    (prints RESULT/STATE lines)
"""
import sys

from sqlalchemy import create_engine, text

sys.path.insert(0, sys.argv[1] + "/apps/api/src")

import drhiro_api.schema_activities as sa  # noqa: E402

URL = ("postgresql+psycopg2://"
       + "drhiro:drhiro@localhost:5435/drhiro_test")


def main() -> int:
    print(f"imported: {sa.__file__}")
    engine = create_engine(URL)

    def cleanup(c):
        for n in ("first", "second"):
            c.execute(text(f"DROP SCHEMA IF EXISTS {n} CASCADE"))

    with engine.begin() as c:
        cleanup(c)
        c.execute(text("CREATE SCHEMA first"))
        c.execute(text("CREATE SCHEMA second"))
        c.execute(text("CREATE TABLE second.users (id uuid PRIMARY KEY)"))
        c.execute(text("SET search_path TO first, second"))
        try:
            sa.create_activities(c)
            print("RESULT: create_activities SUCCEEDED  <-- policy violation")
            created = c.execute(text("""
                SELECT kn.nspname AS table_schema, rn.nspname AS ref_schema
                FROM pg_constraint con
                JOIN pg_class k ON k.oid = con.conrelid
                JOIN pg_namespace kn ON kn.oid = k.relnamespace
                JOIN pg_class r ON r.oid = con.confrelid
                JOIN pg_namespace rn ON rn.oid = r.relnamespace
                WHERE con.conname = :fk
                  AND kn.nspname IN ('first', 'second')
            """), {"fk": sa.FK_NAME}).mappings().all()
            for r in created:
                print(f"STATE: created {r['table_schema']}.activities "
                      f"referencing {r['ref_schema']}.users")
        except Exception as exc:                       # noqa: BLE001
            print(f"RESULT: refused with {type(exc).__name__}: {exc}")
        tables = c.execute(text("""
            SELECT count(*) FROM pg_class k JOIN pg_namespace n
            ON n.oid = k.relnamespace
            WHERE k.relname = 'activities' AND n.nspname IN ('first', 'second')
        """)).scalar()
        indexes = c.execute(text("""
            SELECT count(*) FROM pg_indexes
            WHERE schemaname IN ('first', 'second') AND tablename = 'activities'
        """)).scalar()
        print(f"STATE: activities tables in first/second = {tables}; "
              f"indexes = {indexes}")

    with engine.begin() as c:
        cleanup(c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
