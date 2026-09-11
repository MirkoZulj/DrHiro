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
    "created_at": ("TIMESTAMP WITH TIME ZONE", False),
    "updated_at": ("TIMESTAMP WITH TIME ZONE", False),
}

# Server defaults the adopted table must carry, compared as EXACT canonical
# expressions (not substrings). Measured from production: id defaults to
# gen_random_uuid(), created_at/updated_at to now().
#
# Exactness matters: a substring test accepts `now() + interval '1 day'` because it
# CONTAINS "now". Any additional expression changes behaviour and is not equivalent.
EXPECTED_DEFAULTS: dict[str, str] = {
    "id": "gen_random_uuid()",
    "created_at": "now()",
    "updated_at": "now()",
}


def _canon_default(expr: Any) -> str:
    """Canonicalise a server default expression for EQUALITY comparison.

    Erases only formatting and casts (`now()::timestamp with time zone` is the same
    default as `now()`), never structure - so a modified expression stays unequal.
    """
    s = str(expr or "").strip().lower()
    # Drop casts: `now()::timestamp with time zone`, `'1 day'::interval`.
    s = re.sub(r"::\s*[a-z_][a-z0-9_ ]*", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Trailing semicolons/quotes are not part of the expression.
    return s.strip(";").strip()

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _qualify(schema: str | None) -> str:
    if schema is None:
        return ""
    if not _IDENT_RE.match(schema):
        raise ValueError(f"unsafe schema name: {schema!r}")
    return f"{schema}."


def _canon_type(t: Any) -> str:
    """Canonicalise a column type for comparison (ORM and DB spellings differ).

    Deliberately CONSERVATIVE: only spelling differences are erased. Meaningful
    attributes are preserved, because collapsing them makes the validator accept a
    schema that is not equivalent. In particular `timestamp with time zone` and
    `timestamp without time zone` are DIFFERENT types and must not compare equal -
    the expected production shape is `with time zone`.

    Type ATTRIBUTES are read before falling back to `str()`, because SQLAlchemy
    renders `TIMESTAMP(timezone=True)` as plain "TIMESTAMP" - stringifying alone
    would erase exactly the distinction being checked.
    """
    tz = getattr(t, "timezone", None)
    if tz is not None:
        return "TIMESTAMP WITH TIME ZONE" if tz else "TIMESTAMP WITHOUT TIME ZONE"
    s = str(t).upper()
    # Normalise `TIMESTAMP(6) WITH TIME ZONE`, `TIMESTAMP WITH TIME ZONE`, etc.
    if "TIMESTAMP" in s or "DATETIME" in s:
        if "WITH TIME ZONE" in s or "TIMESTAMPTZ" in s:
            return "TIMESTAMP WITH TIME ZONE"
        if "WITHOUT TIME ZONE" in s:
            return "TIMESTAMP WITHOUT TIME ZONE"
        # A bare TIMESTAMP means "without time zone" in PostgreSQL.
        return "TIMESTAMP WITHOUT TIME ZONE"
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


def column_types(conn: Connection, schema: str | None = None) -> dict[str, str]:
    """Authoritative column types from the catalog (`format_type`).

    `format_type` spells the FULL type including the timezone attribute
    ("timestamp with time zone") and the length ("character varying(255)"), so the
    comparison does not depend on how a driver or ORM renders the type.
    """
    rows = conn.execute(text("""
        SELECT a.attname AS column_name,
               format_type(a.atttypid, a.atttypmod) AS column_type
        FROM pg_attribute a
        JOIN pg_class t ON t.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = :t
          AND a.attnum > 0
          AND NOT a.attisdropped
          AND (:s IS NULL OR n.nspname = :s)
        ORDER BY a.attnum
    """), {"t": TABLE, "s": schema}).mappings().all()
    return {r["column_name"]: r["column_type"] for r in rows}


def index_definitions(conn: Connection, schema: str | None = None) -> dict[str, dict]:
    """Full index definitions: columns, method, uniqueness and PREDICATE.

    `inspect().get_indexes()` gives the columns but not a partial index's predicate,
    so an index named and columned correctly but created `WHERE false` (empty) or
    `WHERE user_id IS NULL` reads as equivalent. Read the catalog instead so method,
    uniqueness and partiality are all visible and comparable.
    """
    rows = conn.execute(text("""
        SELECT i.relname            AS index_name,
               am.amname            AS method,
               ix.indisunique       AS is_unique,
               ix.indisprimary      AS is_primary,
               (ix.indpred IS NOT NULL) AS is_partial,
               pg_get_indexdef(ix.indexrelid) AS indexdef
        FROM pg_index ix
        JOIN pg_class i ON i.oid = ix.indexrelid
        JOIN pg_class t ON t.oid = ix.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_am am ON am.oid = i.relam
        WHERE t.relname = :t
          AND (:s IS NULL OR n.nspname = :s)
        ORDER BY i.relname
    """), {"t": TABLE, "s": schema}).mappings().all()
    out: dict[str, dict] = {}
    for r in rows:
        out[r["index_name"]] = {
            "method": r["method"],
            "unique": bool(r["is_unique"]),
            "primary": bool(r["is_primary"]),
            "partial": bool(r["is_partial"]),
            "definition": r["indexdef"],
        }
    return out


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

      fatal         a mismatch that must not be auto-fixed: missing/wrong column,
                    wrong type or nullability, wrong/missing server default, wrong/
                    missing PRIMARY KEY, wrong/missing FOREIGN KEY, an index that
                    exists with the WRONG columns, or a CHECK that exists but is not
                    the required `calories_burned >= 0`.
      reconcilable  a missing index or missing CHECK - safe to add without touching
                    rows.
    """
    fatal: list[str] = []
    reconcilable: list[str] = []
    insp = inspect(conn)

    cols = {c["name"]: c for c in insp.get_columns(TABLE, schema=schema)}
    # Prefer the catalog's full type spelling over the reflected type object.
    cat_types = column_types(conn, schema)
    for name, (want_type, want_nullable) in COLUMNS.items():
        got = cols.get(name)
        if got is None:
            fatal.append(f"missing column {TABLE}.{name}")
            continue
        got_type = _canon_type(cat_types.get(name, got["type"]))
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

    # Server defaults: EXACT canonical expression, never a substring. A modified
    # expression (`now() + interval '1 day'`) must be fatal, not accepted.
    for name, want in EXPECTED_DEFAULTS.items():
        got = (cols.get(name) or {}).get("default")
        if _canon_default(got) != _canon_default(want):
            fatal.append(
                f"column {TABLE}.{name} default is {got!r}, expected exactly {want}"
            )

    # Indexes: match on the FULL definition - columns, access method, uniqueness and
    # partiality. A name with the wrong columns is fatal (it cannot be fixed by
    # adding a differently-shaped index), and so is a correctly named/columned index
    # that is PARTIAL or non-btree: both change what the index actually covers.
    cat_indexes = index_definitions(conn, schema)
    insp_idx_cols = {
        i["name"]: list(i.get("column_names") or i.get("columns") or [])
        for i in insp.get_indexes(TABLE, schema=schema)
    }
    for index_name, want_cols in (
        (ORM_INDEX, ["user_id"]),
        (PROD_INDEX, ["user_id", "activity_date"]),
    ):
        got_cols = insp_idx_cols.get(index_name)
        meta = cat_indexes.get(index_name)
        if got_cols is None and meta is None:
            reconcilable.append(
                f"missing index {index_name} ({', '.join(want_cols)})"
            )
            continue
        if got_cols != want_cols:
            fatal.append(
                f"index {index_name} columns {got_cols} do not match "
                f"expected {want_cols}"
            )
            continue
        # Columns match: reject unsupported differences instead of assuming
        # equivalence. Only a plain, non-unique, non-partial btree index is the
        # declared shape.
        if meta is None:
            fatal.append(f"index {index_name} columns match but definition unreadable")
            continue
        if meta["partial"]:
            fatal.append(
                f"index {index_name} is PARTIAL; expected a non-partial index "
                f"({meta['definition']})"
            )
        if meta["unique"] and index_name != PK_NAME:
            fatal.append(
                f"index {index_name} is UNIQUE; expected a non-unique index"
            )
        if meta["method"] != "btree":
            fatal.append(
                f"index {index_name} uses access method {meta['method']}; "
                "expected btree"
            )

    # PRIMARY KEY must be activities_pkey on (id).
    pk = insp.get_pk_constraint(TABLE, schema=schema)
    pk_name = pk.get("name")
    pk_cols = list(pk.get("constrained_columns") or [])
    if pk_name != PK_NAME or pk_cols != ["id"]:
        fatal.append(
            f"primary key {pk_name or '<none>'}({pk_cols}) does not match "
            f"expected {PK_NAME}(id)"
        )

    # FOREIGN KEY must be user_id -> users(id) ON DELETE CASCADE, where `users` is
    # the table in the SAME schema as `activities`. Checking only the referred TABLE
    # NAME accepts `unrelated.users(id)`: a different table that merely shares the
    # name, which is a different constraint entirely.
    def _fk_faults() -> list[str]:
        faults: list[str] = []
        found = False
        for fk in insp.get_foreign_keys(TABLE, schema=schema):
            if fk.get("name") != FK_NAME:
                continue
            found = True
            opts = fk.get("options") or {}
            referred_schema = fk.get("referred_schema")
            # A None referred_schema means "the same schema as the table".
            if (referred_schema or schema) != schema:
                faults.append(
                    f"foreign key {FK_NAME} targets schema "
                    f"{referred_schema!r}, expected {schema!r}"
                )
            if list(fk.get("constrained_columns") or []) != ["user_id"]:
                faults.append(
                    f"foreign key {FK_NAME} constrains "
                    f"{fk.get('constrained_columns')}, expected ['user_id']"
                )
            if fk.get("referred_table") != "users":
                faults.append(
                    f"foreign key {FK_NAME} refers to table "
                    f"{fk.get('referred_table')!r}, expected 'users'"
                )
            if list(fk.get("referred_columns") or []) != ["id"]:
                faults.append(
                    f"foreign key {FK_NAME} refers to columns "
                    f"{fk.get('referred_columns')}, expected ['id']"
                )
            if opts.get("ondelete", "NO ACTION") != "CASCADE":
                faults.append(
                    f"foreign key {FK_NAME} ON DELETE is "
                    f"{opts.get('ondelete', 'NO ACTION')!r}, expected 'CASCADE'"
                )
        if not found:
            faults.append(
                f"foreign key {FK_NAME} (user_id -> users(id) ON DELETE CASCADE) "
                "not found"
            )
        return faults

    fatal.extend(_fk_faults())

    # CHECK: missing -> reconcilable; present-but-wrong -> fatal.
    checks = _check_constraints(conn, schema)
    if has_calories_check(conn, schema):
        pass
    else:
        wrong = [f"{n} ({ddl})" for n, ddl in checks if "calories_burned" in ddl]
        if wrong:
            fatal.append(
                f"CHECK(s) {', '.join(wrong)} is not the required {CHECK_EXPR}"
            )
        else:
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


def _canon_check_expr(ddl: str) -> str:
    """Extract and canonicalise the boolean expression of a CHECK constraint DDL.

    PostgreSQL re-renders the stored expression, so `pg_get_constraintdef` returns
    forms like:

        CHECK ((calories_burned >= (0)::double precision))
        CHECK ((calories_burned >= (- (100)::double precision)))

    (casts, redundant parens, and unary minus re-rendered as `(- (N))`). This
    normalises those to a comparable canonical form such as ``calories_burned >= 0``
    or ``calories_burned >= -100``, so a different lower bound is correctly treated
    as NOT equivalent to the required `>= 0`.
    """
    m = re.match(r"CHECK\s*\((.*)\)\s*$", ddl, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    expr = m.group(1)
    # Remove type casts (::double precision etc.).
    expr = re.sub(r"::\s*[a-z_][a-z_ ]*", "", expr)
    expr = re.sub(r"\s+", " ", expr).strip()
    # Collapse single-value parens and unary-minus re-renderings, repeatedly.
    for _ in range(10):
        prev = expr
        expr = re.sub(
            r"\(\s*(-)?\s*\(\s*(-)?\s*(\d+(?:\.\d+)?)\s*\)\s*\)", r"\1\2\3", expr
        )
        expr = re.sub(r"\(\s*(-)?\s*(\d+(?:\.\d+)?)\s*\)", r"\1\2", expr)
        if expr == prev:
            break
    # Strip balanced outer parentheses that wrap the whole expression.
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        wraps = True
        for i, ch in enumerate(expr):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(expr) - 1:
                    wraps = False
                    break
        if wraps:
            expr = expr[1:-1].strip()
        else:
            break
    return expr.lower()


REQUIRED_CHECK_CANON = "calories_burned >= 0"


def has_calories_check(conn: Connection, schema: str | None = None) -> bool:
    """True when an equivalent `calories_burned >= 0` check exists.

    The test is SEMANTIC (canonical expression equals `calories_burned >= 0`), not
    nominal. A constraint that merely shares the production NAME but encodes a
    different bound (e.g. `calories_burned >= -100`) is NOT accepted - the name alone
    does not make it the required invariant. A differently-named equivalent check is
    accepted (and not duplicated).
    """
    for _name, ddl in _check_constraints(conn, schema):
        if _canon_check_expr(ddl) == REQUIRED_CHECK_CANON:
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
