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
    "title": ("VARCHAR(255)", False),
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

    Erases ONLY formatting (whitespace, case, trailing semicolon, a redundant outer
    parenthesis pair) - NEVER a cast. `now()::date` is not equivalent to `now()`: the
    cast changes both the value and the type, and any row written through it differs.
    PostgreSQL's exact stored rendering (pg_get_expr) is the comparison basis; the
    test database supplies that rendering, not a fixture.
    """
    s = str(expr or "").strip()
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(";").strip()
    # Drop a single redundant outer parenthesis pair, repeatedly.
    for _ in range(10):
        if not (s.startswith("(") and s.endswith(")")):
            break
        depth = 0
        wraps = True
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    wraps = False
                    break
        if wraps:
            s = s[1:-1].strip()
        else:
            break
    return s.lower()

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _qualify(schema: str | None) -> str:
    if schema is None:
        return ""
    if not _IDENT_RE.match(schema):
        raise ValueError(f"unsafe schema name: {schema!r}")
    return f"{schema}."


def _type_precision(rendered: str) -> str:
    """Extract the numeric precision/scale a type renders, if any.

    `TIMESTAMP(6) WITH TIME ZONE` -> '6'; `character varying(255)` -> '255';
    `double precision` -> '' (its scale is not a parenthesised length). The declared
    shape carries no precision, so a non-empty result is a difference to reject.
    """
    m = re.search(r"\((\d+(?:\s*,\s*\d+)?)\)", rendered)
    return m.group(1) if m else ""


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
        # SQLAlchemy type object: preserve the precision, not just the zone.
        prec = _type_precision(str(t))
        zone = "WITH TIME ZONE" if tz else "WITHOUT TIME ZONE"
        return f"TIMESTAMP({prec}) {zone}" if prec else f"TIMESTAMP {zone}"
    s = str(t).upper()
    prec = _type_precision(s)
    # Normalise `TIMESTAMP(6) WITH TIME ZONE`, `TIMESTAMP WITH TIME ZONE`, etc. The
    # declared shape has NO precision, so any precision is a difference - conservative.
    if "TIMESTAMP" in s or "DATETIME" in s:
        if "WITH TIME ZONE" in s or "TIMESTAMPTZ" in s:
            return f"TIMESTAMP({prec}) WITH TIME ZONE" if prec else "TIMESTAMP WITH TIME ZONE"
        if "WITHOUT TIME ZONE" in s:
            return (f"TIMESTAMP({prec}) WITHOUT TIME ZONE" if prec
                    else "TIMESTAMP WITHOUT TIME ZONE")
        # A bare TIMESTAMP means "without time zone" in PostgreSQL.
        return (f"TIMESTAMP({prec}) WITHOUT TIME ZONE" if prec
                else "TIMESTAMP WITHOUT TIME ZONE")
    if "DOUBLE PRECISION" in s or "FLOAT" in s:
        return "FLOAT"
    # Preserve VARCHAR length: VARCHAR(1) is NOT equivalent to VARCHAR(255); ordinary
    # activity titles will fail to insert into a 1-character column.
    m = re.search(r"(?:CHARACTER VARYING|VARCHAR)\s*\((\d+)\)", s)
    if m or "CHARACTER VARYING" in s or s.startswith("VARCHAR"):
        return f"VARCHAR({m.group(1)})" if m else "VARCHAR"
    if "TEXT" in s:
        return "TEXT"
    if "UUID" in s:
        return "UUID"
    if "DATE" in s:
        return "DATE"
    return re.sub(r"\(.*\)", "", s).strip()


def _relation_oid(conn: Connection, name: str, schema: str | None) -> int | None:
    """Resolve the ONE intended relation to its OID.

    Name resolution follows the SAME policy as the migration's own unqualified SQL:
    when `schema` is None the name resolves through the connection's search_path
    (to_regclass('activities')); when given, it is schema-qualified. This is the
    crux of review finding 2: with the public API's default schema=None, every
    catalog read must anchor to THIS relation's OID. Filtering by `relname = :t` with
    `(:s IS NULL OR nspname = :s)` instead combined public.activities with
    unrelated.activities when schema was None - column types, indexes and CHECKs from
    one relation could be mixed with columns and foreign keys from another.
    """
    if not name or _IDENT_RE.sub("", name):
        return None
    if schema is not None and not _IDENT_RE.match(schema):
        return None
    qualified = f"{schema}.{name}" if schema else name
    # `to_regclass` yields the regclass type, whose TEXT form is the relation NAME
    # (psycopg2 returns the name, not the OID). Cast to oid explicitly so we get the
    # numeric identity every catalog query below anchors on.
    return conn.execute(text("SELECT pg_catalog.to_regclass(:q)::oid"),
                        {"q": qualified}).scalar()


def _namespace_of(conn: Connection, oid: int) -> int | None:
    """The pg_namespace OID that a resolved relation lives in."""
    return conn.execute(
        text("SELECT relnamespace FROM pg_class WHERE oid = :oid"), {"oid": oid}
    ).scalar()


def _relation_in_namespace(conn: Connection, name: str, ns_oid: int | None) -> int | None:
    """Resolve `name` INSIDE a specific namespace, by OID.

    SCHEMA POLICY (review #10): the foreign key must target the `users` relation in the
    SAME namespace as the `activities` relation the migration operates on. Resolving
    both names independently through search_path does NOT enforce that: with a
    search_path of `first, second` where `first` holds users but no activities and
    `second` holds both, `activities` resolves to `second.activities` while a separate
    unqualified lookup of `users` resolves to `first.users`. Comparing OIDs would then
    accept a constraint the module's own diagnostic calls cross-schema. The intended
    users relation is therefore derived from the RESOLVED activities namespace, which
    matches what the migration creates (a fresh table's FK is emitted
    schema-qualified against the same namespace).
    """
    if not name or _IDENT_RE.sub("", name) or ns_oid is None:
        return None
    return conn.execute(text("""
        SELECT c.oid
        FROM pg_class c
        WHERE c.relname = :n
          AND c.relnamespace = :ns
          AND c.relkind IN ('r', 'p')
    """), {"n": name, "ns": ns_oid}).scalar()


def column_types(conn: Connection, schema: str | None = None) -> dict[str, str]:
    """Authoritative column types from the catalog (`format_type`).

    `format_type` spells the FULL type including the timezone attribute
    ("timestamp with time zone") and the length ("character varying(255)"), so the
    comparison does not depend on how a driver or ORM renders the type.
    """
    oid = _relation_oid(conn, TABLE, schema)
    if oid is None:
        return {}
    rows = conn.execute(text("""
        SELECT a.attname AS column_name,
               format_type(a.atttypid, a.atttypmod) AS column_type
        FROM pg_attribute a
        WHERE a.attrelid = :oid
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum
    """), {"oid": oid}).mappings().all()
    return {r["column_name"]: r["column_type"] for r in rows}


def index_definitions(conn: Connection, schema: str | None = None) -> dict[str, dict]:
    """Full index definitions: columns, method, uniqueness and PREDICATE.

    `inspect().get_indexes()` gives the columns but not a partial index's predicate,
    so an index named and columned correctly but created `WHERE false` (empty) or
    `WHERE user_id IS NULL` reads as equivalent. Read the catalog instead so method,
    uniqueness and partiality are all visible and comparable.
    """
    oid = _relation_oid(conn, TABLE, schema)
    if oid is None:
        return {}
    rows = conn.execute(text("""
        SELECT i.relname            AS index_name,
               am.amname            AS method,
               ix.indisunique       AS is_unique,
               ix.indisprimary      AS is_primary,
               (ix.indpred IS NOT NULL) AS is_partial,
               pg_get_indexdef(ix.indexrelid) AS indexdef
        FROM pg_index ix
        JOIN pg_class i ON i.oid = ix.indexrelid
        JOIN pg_am am ON am.oid = i.relam
        WHERE ix.indrelid = :oid
        ORDER BY i.relname
    """), {"oid": oid}).mappings().all()
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
    # OID-resolved, not a name list: get_table_names(schema=None) would return every
    # schema's tables and a name test would be ambiguous across schemas.
    return _relation_oid(conn, TABLE, schema) is not None


# --------------------------------------------------------------------------- #
# creation (fresh database)
# --------------------------------------------------------------------------- #

class MissingUsersRelation(RuntimeError):
    """Fresh creation refuses to bind a `users` target from another namespace.

    Under the same-schema policy the created table and the `users` it references must
    live in ONE namespace. An unqualified `REFERENCES users(id)` instead resolves
    through search_path at CREATE time, which can bind a different schema's users than
    the destination schema of the new table - producing a cross-schema relationship
    that `diff_activities` rejects. Deriving the target from whichever `users` happens
    to resolve is exactly the bug being prevented, so creation fails loudly instead.
    """


def _creation_namespace_oid(conn: Connection, schema: str | None) -> int:
    """The namespace an unqualified CREATE would actually land in.

    With an explicit schema that IS the destination. With schema=None PostgreSQL
    resolves an unqualified CREATE TABLE to the FIRST schema in the effective search
    path in which the current user may CREATE (not merely the first that exists), so
    that is what is resolved here rather than guessed from name resolution of some
    other relation.
    """
    if schema:
        oid = conn.execute(
            text("SELECT oid FROM pg_namespace WHERE nspname = :s"), {"s": schema}
        ).scalar()
        if oid is None:
            raise MissingUsersRelation(f"schema {schema!r} does not exist")
        return oid
    oid = conn.execute(text("""
        SELECT n.oid
        FROM pg_namespace n
        WHERE n.nspname = ANY(current_schemas(false))
          AND has_schema_privilege(n.oid, 'CREATE')
        ORDER BY array_position(current_schemas(false), n.nspname)
        LIMIT 1
    """)).scalar()
    if oid is None:
        raise MissingUsersRelation(
            "cannot determine a writable schema for an unqualified CREATE - "
            "search_path has no schema in which the current user may CREATE"
        )
    return oid


def create_activities(conn: Connection, schema: str | None = None) -> None:
    """Create the table with the declared shape (ORM columns + prod-faithful extras).

    Server defaults (`gen_random_uuid()`, `now()`), the CHECK and the composite
    index are taken from the measured production schema so a fresh database does
    not drift from the real one.

    Both the new table AND its `users` target are emitted SCHEMA-QUALIFIED against the
    resolved DESTINATION namespace - including when `schema` is None and the caller
    relies on search_path. Emitting an unqualified `REFERENCES users(id)` would let the
    FK bind a different schema's users than the table's own namespace (the same-schema
    policy the validator enforces), and emitting an unqualified table name would place
    the table by search_path while the FK resolved independently. If the destination
    namespace has no `users` relation, creation FAILS rather than silently falling back
    to another schema's users.
    """
    dest_ns = _creation_namespace_oid(conn, schema)
    dest_name = conn.execute(
        text("SELECT nspname FROM pg_namespace WHERE oid = :o"), {"o": dest_ns}
    ).scalar()
    if not dest_name or _IDENT_RE.sub("", dest_name):
        raise MissingUsersRelation(f"unusable destination schema name {dest_name!r}")
    if _relation_in_namespace(conn, "users", dest_ns) is None:
        raise MissingUsersRelation(
            f"refusing to create {dest_name}.{TABLE}: that schema has no `users` "
            f"relation, and the same-schema policy forbids referencing another "
            f"schema's users"
        )
    q = f"{dest_name}."
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
            CONSTRAINT {FK_NAME} FOREIGN KEY (user_id) REFERENCES {q}users(id)
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
    # the table in the SAME schema as `activities`. Compared by OID: both the
    # activities relation and the intended users relation are resolved once (same
    # search_path/schema policy as the migration's own SQL), and the constraint's
    # confrelid must equal the intended users OID. Name-only comparison accepts
    # `unrelated.users(id)` - a different table that merely shares the name; relying
    # on the reflected referred_schema is also unsafe, because reflection can omit
    # schema qualification based on search_path visibility.
    def _fk_faults() -> list[str]:
        faults: list[str] = []
        acts_oid = _relation_oid(conn, TABLE, schema)
        if acts_oid is None:
            return [f"cannot resolve {TABLE} to validate its foreign key"]
        # SCHEMA POLICY (review #10): the target is the `users` relation in the SAME
        # namespace as the RESOLVED activities relation - derived from activities'
        # namespace, not resolved independently through search_path (which can bind a
        # different schema's users than this module's own same-schema rule requires).
        acts_ns = _namespace_of(conn, acts_oid)
        users_oid = _relation_in_namespace(conn, "users", acts_ns)
        rows = conn.execute(text("""
            SELECT c.conname,
                   c.confrelid,
                   c.confdeltype,
                   string_agg(a.attname,  ',' ORDER BY k.ord) AS cols,
                   string_agg(fa.attname, ',' ORDER BY f.ord) AS ref_cols,
                   r.relname  AS referred_table,
                   n.nspname  AS referred_schema
            FROM pg_constraint c
            JOIN unnest(c.conkey)  WITH ORDINALITY k(attnum, ord) ON true
            JOIN unnest(c.confkey) WITH ORDINALITY f(attnum, ord) ON f.ord = k.ord
            JOIN pg_attribute a  ON a.attrelid  = c.conrelid  AND a.attnum  = k.attnum
            JOIN pg_attribute fa ON fa.attrelid = c.confrelid AND fa.attnum = f.attnum
            JOIN pg_class r     ON r.oid = c.confrelid
            JOIN pg_namespace n ON n.oid = r.relnamespace
            WHERE c.conrelid = :oid AND c.contype = 'f'
            GROUP BY c.conname, c.confrelid, c.confdeltype, r.relname, n.nspname
            ORDER BY c.conname
        """), {"oid": acts_oid}).mappings().all()
        found = False
        for r in rows:
            if r["conname"] != FK_NAME:
                continue
            found = True
            cols = (r["cols"] or "").split(",")
            # BOTH sides of the mapping, each aggregated in ORDINAL order, so the
            # pairing is what is compared - `REFERENCES users(alternate_id)` is a
            # different constraint from `REFERENCES users(id)` even when both are
            # unique UUID columns on the same table.
            ref_cols = (r["ref_cols"] or "").split(",")
            if cols != ["user_id"]:
                faults.append(
                    f"foreign key {FK_NAME} constrains {cols}, expected ['user_id']"
                )
            if ref_cols != ["id"]:
                faults.append(
                    f"foreign key {FK_NAME} references columns {ref_cols} on "
                    f"{r['referred_schema']}.{r['referred_table']}; expected ['id']"
                )
            if r["confrelid"] != users_oid:
                faults.append(
                    f"foreign key {FK_NAME} refers to {r['referred_schema']}."
                    f"{r['referred_table']} (oid {r['confrelid']}); expected the "
                    f"users relation in the SAME namespace as {TABLE} "
                    f"(oid {users_oid})"
                )
            if r["confdeltype"] != "c":
                faults.append(
                    f"foreign key {FK_NAME} ON DELETE is {r['confdeltype']!r}, "
                    "expected 'c' (CASCADE)"
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
    oid = _relation_oid(conn, TABLE, schema)
    if oid is None:
        return []
    rows = conn.execute(text("""
        SELECT a.attname
        FROM pg_attribute a
        WHERE a.attrelid = :oid
          AND a.attnotnull
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum
    """), {"oid": oid}).fetchall()
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
    oid = _relation_oid(conn, TABLE, schema)
    if oid is None:
        return []
    rows = conn.execute(text("""
        SELECT c.conname, pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        WHERE c.conrelid = :oid
          AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) LIKE 'CHECK%'
    """), {"oid": oid}).fetchall()
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
