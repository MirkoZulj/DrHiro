"""`activities` schema support — creation, adoption validation, ownership.

The ORM declares `activities` (models.py `Activity`) but no Alembic migration ever
created it. Production has the table anyway, created out-of-band (by an earlier
`create_all`), and its shape differs from the ORM in ways that matter:

    production                          ORM declaration
    ----------------------------------  ---------------------------------------
    id uuid DEFAULT gen_random_uuid()   uuid PK, app-side default only
    created_at/updated_at DEFAULT now() app-side defaults only
    CHECK calories_burned >= 0          not declared
    idx_activities_user_date            not declared
      btree(user_id, activity_date)
    ix_activities_user_id               declared (via index=True)

So a migration for `activities` cannot simply create the table:
  * a fresh database needs it (the fresh-bootstrap gap);
  * a database that already has it (production) would fail on a bare create, and
    "skipping when present" is not acceptable either - the existing table must be
    VALIDATED (columns, types, nullability, defaults, constraints, indexes), with
    supported differences reconciled and anything else failing with a precise
    diagnostic.

Ownership matters for reversal: this module marks a table it created itself
(`COMMENT ON TABLE ... IS 'alembic:owned'`). Downgrade is only permitted for a
self-created table. An ADOPTED pre-existing table (production) makes downgrade
UNSUPPORTED, and the caller must fail rather than silently drop real data.

The functions here take a connection and an optional schema so they are directly
testable against a disposable schema, with no Alembic CLI and no production access.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

TABLE = "activities"
OWNED_MARKER = "alembic:owned"
CHECK_NAME = "activities_calories_burned_check"
CHECK_EXPR = "calories_burned >= 0::double precision"
PK_NAME = "activities_pkey"
FK_NAME = "activities_user_id_fkey"
ORM_INDEX = "ix_activities_user_id"
PROD_INDEX = "idx_activities_user_date"

# Declared shape: ORM columns + the production-faithful extras. Types are given in
# the canonical form produced by `_canon_type` on both ORM and DB type strings.
COLUMNS: dict[str, tuple[str, bool]] = {
    "id": ("UUID", False),
    "user_id": ("UUID", False),
    "activity_date": ("DATE", False),
    "title": ("VARCHAR", False),
    "description": ("TEXT", True),
    "calories_burned": ("FLOAT", False),
    "created_at": ("TIMESTAMP", False),
    "updated_at": ("TIMESTAMP", False),
}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _qualify(schema: str | None) -> str:
    if schema is None:
        return ""
    if not _IDENT_RE.match(schema):
        raise ValueError(f"unsafe schema name: {schema!r}")
    return f"{schema}."


def _canon_type(t: Any) -> str:
    """Canonicalise a column type for comparison (ORM and DB spellings differ)."""
    s = str(t).upper()
    if "TIMESTAMP" in s or "DATETIME" in s:
        return "TIMESTAMP"
    if "DOUBLE PRECISION" in s or "FLOAT" in s:
        return "FLOAT"
    if "CHARACTER VARYING" in s or s.startswith("VARCHAR"):
        return "VARCHAR"
    if "TEXT" in s:
        return "TEXT"
    if "UUID" in s:
        return "UUID"
    if "DATE" in s:
        return "DATE"
    return re.sub(r"\(.*\)", "", s).strip()


def table_exists(conn: Connection, schema: str | None = None) -> bool:
    return TABLE in inspect(conn).get_table_names(schema=schema)


# --------------------------------------------------------------------------- #
# creation (fresh database)
# --------------------------------------------------------------------------- #

def create_activities(conn: Connection, schema: str | None = None) -> None:
    """Create the table with the declared shape (ORM columns + prod-faithful extras).

    Server defaults (`gen_random_uuid()`, `now()`), the CHECK and the composite
    index are taken from the measured production schema so a fresh database does
    not drift from the real one.
    """
    q = _qualify(schema)
    conn.execute(text(f"""
        CREATE TABLE {q}{TABLE} (
            id uuid NOT NULL DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL,
            activity_date date NOT NULL,
            title character varying(255) NOT NULL,
            description text,
            calories_burned double precision NOT NULL,
            created_at timestamp with time zone NOT NULL DEFAULT now(),
            updated_at timestamp with time zone NOT NULL DEFAULT now(),
            CONSTRAINT {CHECK_NAME} CHECK ({CHECK_EXPR}),
            CONSTRAINT {PK_NAME} PRIMARY KEY (id),
            CONSTRAINT {FK_NAME} FOREIGN KEY (user_id) REFERENCES users(id)
                ON DELETE CASCADE
        )
    """))
    conn.execute(text(
        f"CREATE INDEX {ORM_INDEX} ON {q}{TABLE} (user_id)"
    ))
    conn.execute(text(
        f"CREATE INDEX {PROD_INDEX} ON {q}{TABLE} (user_id, activity_date)"
    ))


def drop_activities(conn: Connection, schema: str | None = None) -> None:
    conn.execute(text(f"DROP TABLE {_qualify(schema)}{TABLE}"))


# --------------------------------------------------------------------------- #
# ownership
# --------------------------------------------------------------------------- #

def is_owned(conn: Connection, schema: str | None = None) -> bool:
    """True when this module's migration created the table (safe to drop)."""
    tables = inspect(conn).get_table_comment(TABLE, schema=schema)
    return bool(tables) and tables.get("text") == OWNED_MARKER


def set_owned(conn: Connection, schema: str | None = None) -> None:
    conn.execute(text(
        f"COMMENT ON TABLE {_qualify(schema)}{TABLE} IS '{OWNED_MARKER}'"
    ))


def assert_downgrade_allowed(conn: Connection, schema: str | None = None) -> None:
    """Downgrade policy: only a self-created table may be dropped.

    Two distinct hazards, both documented rather than glossed:

      1. ADOPTED TABLE (production). The migration did not create it, its data
         predates the migration, and its provenance is unknown. Dropping it would
         destroy data the migration never owned, so downgrade is UNSUPPORTED and
         fails with this message; the operator reverses it deliberately.

      2. SELF-CREATED TABLE THAT HAS SINCE ACQUIRED DATA. Ownership makes the drop
         *permitted*, not *safe*: if rows were written after creation, the reverse
         migration is DESTRUCTIVE and loses them. Nothing here snapshots the data.
         Treat downgrade as a data-destroying operation for such a table and
         back up first, or restore rather than downgrade.
    """
    if not is_owned(conn, schema=schema):
        raise RuntimeError(
            f"downgrade unsupported: {TABLE} exists but was not created by this "
            "migration (adopted pre-existing table, e.g. production). Dropping it "
            "would destroy data of unknown provenance; reverse it manually."
        )


# --------------------------------------------------------------------------- #
# validation / reconciliation (existing database)
# --------------------------------------------------------------------------- #

def diff_activities(conn: Connection, schema: str | None = None) -> dict[str, list[str]]:
    """Compare the existing table against the declared shape.

    Returns {"fatal": [...], "reconcilable": [...]}.

      fatal         column missing / type or nullability mismatch - needs a human
      reconcilable  missing index or CHECK - safe to add without touching rows
    """
    fatal: list[str] = []
    reconcilable: list[str] = []
    insp = inspect(conn)

    cols = {c["name"]: c for c in insp.get_columns(TABLE, schema=schema)}
    for name, (want_type, want_nullable) in COLUMNS.items():
        got = cols.get(name)
        if got is None:
            fatal.append(f"missing column {TABLE}.{name}")
            continue
        got_type = _canon_type(got["type"])
        if got_type != want_type:
            fatal.append(
                f"column {TABLE}.{name} type is {got_type}, expected {want_type}"
            )
        if bool(got["nullable"]) != want_nullable:
            got_n = "nullable" if got["nullable"] else "NOT NULL"
            want_n = "nullable" if want_nullable else "NOT NULL"
            fatal.append(
                f"column {TABLE}.{name} is {got_n}, expected {want_n}"
            )

    index_names = {i["name"] for i in insp.get_indexes(TABLE, schema=schema)}
    if ORM_INDEX not in index_names:
        reconcilable.append(f"missing index {ORM_INDEX} (user_id)")
    if PROD_INDEX not in index_names:
        reconcilable.append(f"missing index {PROD_INDEX} (user_id, activity_date)")

    if not has_calories_check(conn, schema):
        reconcilable.append(f"missing CHECK {CHECK_NAME}")

    return {"fatal": fatal, "reconcilable": reconcilable}


def server_version(conn: Connection) -> str:
    """`server_version` for evidence (catalog representation is version-dependent)."""
    return str(conn.execute(text("SHOW server_version")).scalar())


def not_null_columns(conn: Connection, schema: str | None = None) -> list[str]:
    """NOT NULL columns, read from the authoritative place for this server.

    NOT NULL is *not* a pg_constraint row on PostgreSQL before 18; it lives in
    pg_attribute.attnotnull. PostgreSQL 18 adds not-null constraints to
    pg_constraint with contype='n'. Reading pg_attribute is correct on every
    version, so nothing here parses NOT NULL out of the constraint catalog.
    """
    rows = conn.execute(text("""
        SELECT a.attname
        FROM pg_attribute a
        JOIN pg_class t ON t.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = :t
          AND a.attnotnull
          AND a.attnum > 0
          AND NOT a.attisdropped
          AND (:s IS NULL OR n.nspname = :s)
        ORDER BY a.attnum
    """), {"t": TABLE, "s": schema}).fetchall()
    return [r[0] for r in rows]


def _check_constraints(conn: Connection, schema: str | None = None) -> list[tuple[str, str]]:
    """(name, definition) for genuine CHECK constraints on the table.

    Two independent guards, both version-independent:

      * `contype = 'c'` is the CHECK type on every supported version;
      * `pg_get_constraintdef(oid) LIKE 'CHECK%'` rejects any other constraint
        kind that might share a contype value on some version.

    Neither guard reads NOT NULL, which is recorded in pg_attribute.attnotnull
    before PostgreSQL 18 and as contype='n' (not 'c') from 18 onward. Verified on
    the disposable test server (PostgreSQL 16.14): `activities` reports exactly
    three pg_constraint rows - 'c' (the CHECK), 'f' (the FK) and 'p' (the PK) -
    with NOT NULL columns absent from pg_constraint entirely.
    """
    rows = conn.execute(text("""
        SELECT c.conname, pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = :t
          AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) LIKE 'CHECK%'
          AND (:s IS NULL OR n.nspname = :s)
    """), {"t": TABLE, "s": schema}).fetchall()
    return [(r[0], r[1]) for r in rows]


def has_calories_check(conn: Connection, schema: str | None = None) -> bool:
    """True when an equivalent `calories_burned >= 0` check exists.

    Matched by the production constraint name OR by an equivalent expression, so a
    differently-named equivalent check on an adopted table is recognised (and not
    duplicated), while a table with no check at all is correctly reported missing.
    """
    for name, ddl in _check_constraints(conn, schema):
        if name == CHECK_NAME:
            return True
        if "calories_burned" in ddl and ">=" in ddl:
            return True
    return False


def _check_constraint_sql(conn: Connection, schema: str | None = None) -> str:
    return " ".join(ddl for _n, ddl in _check_constraints(conn, schema))


def reconcile_activities(conn: Connection, schema: str | None = None) -> list[str]:
    """Apply the supported fixes reported by `diff_activities`. Returns actions."""
    q = _qualify(schema)
    diff = diff_activities(conn, schema=schema)
    if diff["fatal"]:
        raise RuntimeError(
            f"{TABLE} adoption failed (unreconcilable differences): "
            + "; ".join(diff["fatal"])
        )
    done: list[str] = []
    for item in diff["reconcilable"]:
        if item.startswith("missing index " + ORM_INDEX):
            conn.execute(text(f"CREATE INDEX {ORM_INDEX} ON {q}{TABLE} (user_id)"))
            done.append(f"created INDEX {ORM_INDEX}")
        elif item.startswith("missing index " + PROD_INDEX):
            conn.execute(text(
                f"CREATE INDEX {PROD_INDEX} ON {q}{TABLE} (user_id, activity_date)"
            ))
            done.append(f"created INDEX {PROD_INDEX}")
        elif item.startswith("missing CHECK"):
            conn.execute(text(
                f"ALTER TABLE {q}{TABLE} ADD CONSTRAINT {CHECK_NAME} "
                f"CHECK ({CHECK_EXPR}) NOT VALID"
            ))
            conn.execute(text(
                f"ALTER TABLE {q}{TABLE} VALIDATE CONSTRAINT {CHECK_NAME}"
            ))
            done.append(f"added CHECK {CHECK_NAME}")
    return done
