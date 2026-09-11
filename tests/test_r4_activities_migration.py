"""R4 - `activities` migration: fresh creation vs adoption of an existing table.

Runs against a DISPOSABLE schema in a disposable test database. Gated so the
default suite is unaffected:

    DRHIRO_ACTIVITIES_MIGRATION_DB=1 DRHIRO_TEST_DB_URL=<disposable-db>

No production access. The production-shaped fixture below is transcribed from a
read-only `\\d activities` on the real database (defaults, CHECK, composite index).
"""
from __future__ import annotations

import os
import re

import pytest
from sqlalchemy import create_engine, inspect, text

from drhiro_api import schema_activities as sa_act

pytestmark = pytest.mark.skipif(
    os.environ.get("DRHIRO_ACTIVITIES_MIGRATION_DB") != "1",
    reason=(
        "requires a disposable database; set DRHIRO_ACTIVITIES_MIGRATION_DB=1 "
        "(writes a throwaway schema)"
    ),
)

SCHEMA = "act_test"
TEST_DB_URL = os.environ.get(
    "DRHIRO_TEST_DB_URL", "postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_test"
)

# Transcribed verbatim from production `\d activities` (read-only inspection).
PROD_SHAPE_DDL = """
CREATE TABLE {s}.activities (
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
CREATE INDEX idx_activities_user_date ON {s}.activities (user_id, activity_date);
"""


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    with eng.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        conn.execute(text(f"SET search_path TO {SCHEMA}"))
        conn.execute(text(f"CREATE TABLE {SCHEMA}.users (id uuid PRIMARY KEY)"))
    yield eng
    with eng.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
    eng.dispose()


@pytest.fixture()
def conn(engine):
    """A connection whose search_path puts the disposable schema first."""
    with engine.begin() as c:
        c.execute(text(f"SET search_path TO {SCHEMA}"))
        c.execute(text(f"DROP TABLE IF EXISTS {SCHEMA}.activities"))
        yield c


def _create_prod_shape(c):
    for stmt in PROD_SHAPE_DDL.format(s=SCHEMA).split(";"):
        if stmt.strip():
            c.execute(text(stmt))


class TestFreshCreation:
    def test_fresh_creation_matches_declared_shape_and_is_owned(self, conn):
        sa_act.create_activities(conn, schema=SCHEMA)
        sa_act.set_owned(conn, schema=SCHEMA)

        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert diff["fatal"] == [], diff["fatal"]
        assert diff["reconcilable"] == [], diff["reconcilable"]

        insp = inspect(conn)
        assert {c["name"] for c in insp.get_columns("activities", schema=SCHEMA)} == set(
            sa_act.COLUMNS
        )
        idx = {i["name"] for i in insp.get_indexes("activities", schema=SCHEMA)}
        assert sa_act.ORM_INDEX in idx, "ORM-declared index name must exist"
        assert sa_act.PROD_INDEX in idx, "production composite index must exist"
        assert sa_act.has_calories_check(conn, SCHEMA), "CHECK must exist"

        # Server defaults come from the measured production schema.
        defaults = {
            c["name"]: (c.get("default") or "")
            for c in insp.get_columns("activities", schema=SCHEMA)
        }
        assert "gen_random_uuid" in defaults["id"]
        assert "now()" in defaults["created_at"]

        assert sa_act.is_owned(conn, schema=SCHEMA) is True
        sa_act.assert_downgrade_allowed(conn, schema=SCHEMA)  # must not raise
        sa_act.drop_activities(conn, schema=SCHEMA)
        assert not sa_act.table_exists(conn, schema=SCHEMA)


class TestAdoption:
    def test_adopts_production_shape_and_reconciles_missing_orm_index(self, conn):
        _create_prod_shape(conn)

        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        # Production shape is compatible: no unreconcilable differences.
        assert diff["fatal"] == [], diff["fatal"]
        # The ORM-declared index is genuinely absent in production.
        assert any(sa_act.ORM_INDEX in d for d in diff["reconcilable"]), diff["reconcilable"]

        actions = sa_act.reconcile_activities(conn, schema=SCHEMA)
        assert any(sa_act.ORM_INDEX in a for a in actions)
        idx = {i["name"] for i in inspect(conn).get_indexes("activities", schema=SCHEMA)}
        assert sa_act.ORM_INDEX in idx

        # A second pass is clean (idempotent).
        assert sa_act.diff_activities(conn, schema=SCHEMA)["reconcilable"] == []

    def test_adopted_table_is_not_owned_and_downgrade_is_unsupported(self, conn):
        _create_prod_shape(conn)
        sa_act.reconcile_activities(conn, schema=SCHEMA)

        assert sa_act.is_owned(conn, schema=SCHEMA) is False
        with pytest.raises(RuntimeError) as exc:
            sa_act.assert_downgrade_allowed(conn, schema=SCHEMA)
        assert "downgrade unsupported" in str(exc.value)
        # The table must still be there - nothing was dropped.
        assert sa_act.table_exists(conn, schema=SCHEMA)

    def test_missing_check_is_reconcilable_not_fatal(self, conn):
        # Production shape minus the CHECK constraint.
        _create_prod_shape(conn)
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities "
            "DROP CONSTRAINT activities_calories_burned_check"
        ))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert diff["fatal"] == [], diff["fatal"]
        assert any("CHECK" in d for d in diff["reconcilable"]), diff["reconcilable"]

        actions = sa_act.reconcile_activities(conn, schema=SCHEMA)
        assert any("CHECK" in a for a in actions), actions
        assert sa_act.has_calories_check(conn, SCHEMA), "CHECK must now exist"
        assert sa_act.diff_activities(conn, schema=SCHEMA)["reconcilable"] == []


class TestDivergence:
    def test_divergent_shape_fails_with_precise_diagnostic(self, conn):
        # Same look, but a wrong type and a missing column.
        conn.execute(text(f"""
            CREATE TABLE {SCHEMA}.activities (
                id uuid NOT NULL,
                user_id uuid NOT NULL,
                activity_date date NOT NULL,
                title integer NOT NULL,
                calories_burned double precision NOT NULL,
                created_at timestamp with time zone NOT NULL,
                updated_at timestamp with time zone NOT NULL,
                CONSTRAINT activities_pkey PRIMARY KEY (id)
            )
        """))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("description" in d for d in diff["fatal"]), diff["fatal"]
        assert any("title" in d and "VARCHAR" in d for d in diff["fatal"]), diff["fatal"]

        with pytest.raises(RuntimeError) as exc:
            sa_act.reconcile_activities(conn, schema=SCHEMA)
        msg = str(exc.value)
        assert "activities adoption failed" in msg
        assert "description" in msg and "title" in msg

    def test_nullability_mismatch_is_fatal(self, conn):
        conn.execute(text(f"""
            CREATE TABLE {SCHEMA}.activities (
                id uuid NOT NULL,
                user_id uuid NOT NULL,
                activity_date date NOT NULL,
                title character varying(255) NOT NULL,
                description text NOT NULL,
                calories_burned double precision NOT NULL,
                created_at timestamp with time zone NOT NULL,
                updated_at timestamp with time zone NOT NULL,
                CONSTRAINT activities_pkey PRIMARY KEY (id)
            )
        """))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("description" in d and "nullable" in d for d in diff["fatal"])


class TestCatalogRepresentation:
    """The CHECK-detection guards must be version-independent and must not ignore
    genuine CHECK constraints. Facts below are read from the live server, not
    assumed: catalog representation of NOT NULL changed across major versions."""

    def test_server_version_is_reported(self, conn):
        # Evidence, not an assertion on a specific version: the point is that the
        # representation is version-dependent, so the code must not rely on it.
        assert re.match(r"^\d+\.", sa_act.server_version(conn))

    def test_not_null_is_not_a_check_constraint(self, conn):
        sa_act.create_activities(conn, schema=SCHEMA)

        # NOT NULL is authoritative in pg_attribute.attnotnull...
        nn = set(sa_act.not_null_columns(conn, SCHEMA))
        assert {"id", "user_id", "activity_date", "title", "calories_burned"} <= nn
        assert "description" not in nn, "description is nullable"

        # ...and must NOT be reported as a CHECK constraint.
        checks = sa_act._check_constraints(conn, SCHEMA)
        assert checks, "the real CHECK must be found"
        assert len(checks) == 1, f"expected exactly one check, got {checks}"
        for name, ddl in checks:
            assert ddl.startswith("CHECK"), ddl
            assert "NOT NULL" not in ddl.upper(), ddl

    def test_genuine_check_is_validated_not_ignored(self, conn):
        """A real CHECK must be detected - the guards must not over-filter."""
        sa_act.create_activities(conn, schema=SCHEMA)
        assert sa_act.has_calories_check(conn, SCHEMA) is True

        # Drop it: detection must flip to False and report it reconcilable.
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities "
            "DROP CONSTRAINT activities_calories_burned_check"
        ))
        assert sa_act.has_calories_check(conn, SCHEMA) is False
        assert any(
            "CHECK" in d for d in sa_act.diff_activities(conn, schema=SCHEMA)["reconcilable"]
        )

        # Reconciling restores a working constraint (enforced, not NOT VALID).
        actions = sa_act.reconcile_activities(conn, schema=SCHEMA)
        assert any("CHECK" in a for a in actions), actions
        assert sa_act.has_calories_check(conn, SCHEMA) is True
        with pytest.raises(Exception):
            conn.execute(text(
                f"INSERT INTO {SCHEMA}.activities "
                "(id, user_id, activity_date, title, calories_burned) VALUES "
                "(gen_random_uuid(), gen_random_uuid(), current_date, 'x', -1)"
            ))

    def test_equivalent_differently_named_check_is_recognised(self, conn):
        """An adopted table with an equivalent check must not be duplicated."""
        _create_prod_shape(conn)
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities "
            "DROP CONSTRAINT activities_calories_burned_check"
        ))
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities ADD CONSTRAINT some_other_name "
            "CHECK (calories_burned >= 0)"
        ))
        assert sa_act.has_calories_check(conn, SCHEMA) is True
        assert not any(
            "CHECK" in d for d in sa_act.diff_activities(conn, schema=SCHEMA)["reconcilable"]
        )


class TestDowngradeDestructiveness:
    def test_owned_table_with_data_is_still_destructive_to_drop(self, conn):
        """Ownership permits the drop; it does not make it safe. Documented and
        asserted so the hazard cannot be mistaken for a lossless rollback."""
        sa_act.create_activities(conn, schema=SCHEMA)
        sa_act.set_owned(conn, schema=SCHEMA)
        uid = conn.execute(text(f"""
            INSERT INTO {SCHEMA}.users (id) VALUES (gen_random_uuid()) RETURNING id
        """)).scalar()
        conn.execute(text(f"""
            INSERT INTO {SCHEMA}.activities
                (id, user_id, activity_date, title, calories_burned)
            VALUES (gen_random_uuid(), :uid, current_date, 'run', 100)
        """), {"uid": uid})
        assert _rowcount(conn) == 1

        sa_act.assert_downgrade_allowed(conn, schema=SCHEMA)  # permitted...
        sa_act.drop_activities(conn, schema=SCHEMA)           # ...but destructive
        assert not sa_act.table_exists(conn, schema=SCHEMA)
        # The row is gone with the table: no snapshot, no restore. Recorded
        # explicitly in assert_downgrade_allowed's docstring.


def _rowcount(conn):
    return conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.activities")).scalar()


class TestAdoptionRejectsInvalidSchemas:
    """The adoption validator must reject invalid production-shaped tables with a
    precise diagnostic - it must NOT accept a CHECK by loose substring, or accept a
    table whose indexes/PK/FK/defaults are wrong.

    These are the negative cases the reviewer required (wrong CHECK bound, missing/
    wrong FK, missing/wrong PK, wrong defaults, wrong index columns).
    """

    def test_check_with_wrong_lower_bound_is_fatal(self, conn):
        # Same shape, but CHECK allows calories_burned >= -100: that is NOT the
        # promised >= 0 invariant.
        conn.execute(text(f"""
            CREATE TABLE {SCHEMA}.activities (
                id uuid NOT NULL DEFAULT gen_random_uuid(),
                user_id uuid NOT NULL,
                activity_date date NOT NULL,
                title character varying(255) NOT NULL,
                description text,
                calories_burned double precision NOT NULL,
                created_at timestamp with time zone NOT NULL DEFAULT now(),
                updated_at timestamp with time zone NOT NULL DEFAULT now(),
                CONSTRAINT activities_pkey PRIMARY KEY (id),
                CONSTRAINT activities_calories_burned_check
                    CHECK (calories_burned >= -100::double precision),
                CONSTRAINT activities_user_id_fkey FOREIGN KEY (user_id)
                    REFERENCES users(id) ON DELETE CASCADE
            );
        """))
        assert sa_act.has_calories_check(conn, SCHEMA) is False, \
            "a >= -100 check must NOT satisfy the >= 0 invariant"
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("CHECK" in d for d in diff["fatal"]), diff["fatal"]
        with pytest.raises(RuntimeError):
            sa_act.reconcile_activities(conn, schema=SCHEMA)

    def test_wrong_index_columns_are_fatal_not_reconciled(self, conn):
        _create_prod_shape(conn)
        # Index exists with the right NAME but the WRONG columns. This is the exact
        # case the old code missed (it validated names only).
        conn.execute(text(
            f"ALTER INDEX {SCHEMA}.idx_activities_user_date "
            "RENAME TO idx_activities_user_date_wrong"
        ))
        conn.execute(text(
            f"CREATE INDEX idx_activities_user_date ON {SCHEMA}.activities (title)"
        ))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("idx_activities_user_date" in d and "columns" in d
                   for d in diff["fatal"]), diff["fatal"]

    def test_missing_pk_is_fatal(self, conn):
        _create_prod_shape(conn)
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities DROP CONSTRAINT activities_pkey"
        ))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("primary key" in d or "PK" in d.upper() for d in diff["fatal"]), diff["fatal"]

    def test_wrong_pk_columns_are_fatal(self, conn):
        _create_prod_shape(conn)
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities DROP CONSTRAINT activities_pkey"
        ))
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities ADD PRIMARY KEY (title)"
        ))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("primary key" in d for d in diff["fatal"]), diff["fatal"]

    def test_missing_fk_is_fatal(self, conn):
        _create_prod_shape(conn)
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities DROP CONSTRAINT activities_user_id_fkey"
        ))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("foreign key" in d.lower() for d in diff["fatal"]), diff["fatal"]

    def test_wrong_fk_target_is_fatal(self, conn):
        # An FK from user_id to a WRONG table, or with the wrong ON DELETE action,
        # must not be accepted as equivalent to users(id) ON DELETE CASCADE.
        _create_prod_shape(conn)
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities DROP CONSTRAINT activities_user_id_fkey"
        ))
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities ADD CONSTRAINT activities_user_id_fkey "
            f"FOREIGN KEY (user_id) REFERENCES {SCHEMA}.users(id) ON DELETE SET NULL"
        ))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("foreign key" in d.lower() for d in diff["fatal"]), diff["fatal"]

    def test_missing_server_defaults_are_fatal(self, conn):
        # Production has gen_random_uuid()/now() server defaults; an adopted table
        # without them is not equivalent (app-side only is not the same).
        _create_prod_shape(conn)
        conn.execute(text(f"ALTER TABLE {SCHEMA}.activities ALTER COLUMN id DROP DEFAULT"))
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities ALTER COLUMN created_at DROP DEFAULT"
        ))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("default" in d for d in diff["fatal"]), diff["fatal"]
        with pytest.raises(RuntimeError):
            sa_act.reconcile_activities(conn, schema=SCHEMA)


class TestDefinitionEquivalenceIsConservative:
    """REVIEW #6: the validator must reject NON-EQUIVALENT definitions.

    The previous checks rejected the submitted negative cases but still accepted
    schemas that are not equivalent to the declared shape:

      * a modified default that merely CONTAINS the expected expression;
      * a correctly named/columned index that is PARTIAL or the wrong method;
      * a foreign key that targets a SAME-NAMED table in a different schema;
      * `timestamp without time zone` where the declared shape is `with time zone`.

    Each test builds the valid production shape, applies exactly one such
    alteration, and asserts it is FATAL. The companion baseline test asserts the
    unaltered shape is NOT flagged, so these tests cannot pass by rejecting
    everything.
    """

    def _prod_shape(self, conn):
        conn.execute(text(PROD_SHAPE_DDL.format(s=SCHEMA)))
        conn.execute(
            text(f"CREATE INDEX ix_activities_user_id ON {SCHEMA}.activities (user_id)")
        )

    def test_unaltered_production_shape_is_accepted(self, conn):
        """Baseline: the declared shape must remain valid under the strict checks."""
        self._prod_shape(conn)
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert diff["fatal"] == [], diff["fatal"]
        assert diff["reconcilable"] == [], diff["reconcilable"]

    def test_modified_default_expression_is_fatal(self, conn):
        """`now() + interval '1 day'` CONTAINS `now` but is a different default."""
        self._prod_shape(conn)
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities ALTER COLUMN created_at "
            "SET DEFAULT now() + interval '1 day'"
        ))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("created_at" in f and "default" in f for f in diff["fatal"]), \
            f"modified default was accepted: fatal={diff['fatal']}"

    def test_partial_index_is_fatal(self, conn):
        """Right name, right columns, but WHERE false covers no rows."""
        self._prod_shape(conn)
        conn.execute(text(f"DROP INDEX {SCHEMA}.idx_activities_user_date"))
        conn.execute(text(
            f"CREATE INDEX idx_activities_user_date ON {SCHEMA}.activities "
            "(user_id, activity_date) WHERE false"
        ))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("idx_activities_user_date" in f for f in diff["fatal"]), \
            f"partial index was accepted: fatal={diff['fatal']}"

    def test_unique_index_is_fatal(self, conn):
        """Uniqueness is a meaningful difference, not a spelling variant."""
        self._prod_shape(conn)
        conn.execute(text(f"DROP INDEX {SCHEMA}.idx_activities_user_date"))
        conn.execute(text(
            f"CREATE UNIQUE INDEX idx_activities_user_date ON {SCHEMA}.activities "
            "(user_id, activity_date)"
        ))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("idx_activities_user_date" in f for f in diff["fatal"]), \
            f"unique index was accepted: fatal={diff['fatal']}"

    def test_foreign_key_to_same_name_in_other_schema_is_fatal(self, conn):
        """`unrelated.users` shares the name but is a different table."""
        conn.execute(text("DROP SCHEMA IF EXISTS unrelated CASCADE"))
        conn.execute(text("CREATE SCHEMA unrelated"))
        conn.execute(text("CREATE TABLE unrelated.users (id uuid PRIMARY KEY)"))
        conn.execute(text(
            f"CREATE TABLE {SCHEMA}.activities ("
            "    id uuid NOT NULL DEFAULT gen_random_uuid(),"
            "    user_id uuid NOT NULL,"
            "    activity_date date NOT NULL,"
            "    title character varying(255) NOT NULL,"
            "    description text,"
            "    calories_burned double precision NOT NULL,"
            "    created_at timestamp with time zone NOT NULL DEFAULT now(),"
            "    updated_at timestamp with time zone NOT NULL DEFAULT now(),"
            "    CONSTRAINT activities_pkey PRIMARY KEY (id),"
            f"    CONSTRAINT activities_calories_burned_check CHECK ({sa_act.CHECK_EXPR}),"
            "    CONSTRAINT activities_user_id_fkey FOREIGN KEY (user_id)"
            "        REFERENCES unrelated.users(id) ON DELETE CASCADE"
            ")"
        ))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("foreign key" in f for f in diff["fatal"]), \
            f"cross-schema FK was accepted: fatal={diff['fatal']}"

    def test_timestamp_without_time_zone_is_fatal(self, conn):
        """The declared shape is `with time zone`; dropping it is a real change."""
        self._prod_shape(conn)
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities ALTER COLUMN created_at "
            "TYPE timestamp without time zone USING created_at AT TIME ZONE 'UTC'"
        ))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("created_at" in f and "type" in f for f in diff["fatal"]), \
            f"timestamp without time zone was accepted: fatal={diff['fatal']}"


# --------------------------------------------------------------------------- #
# REVIEW #9 finding 2 - the DEFAULT schema=None entry point must not combine
# relations that share a name across schemas.
#
# The public API's default schema is None. Before this round the catalog reads used
# `(:s IS NULL OR nspname = :s)` and keyed their results by column/index name only,
# so with schema=None they read EVERY `activities` table in EVERY schema and could
# mix column types, indexes or CHECKs from one relation with columns and FKs from
# another. They now resolve the ONE intended relation by OID (search_path policy,
# same as the migration's own unqualified SQL) and anchor every catalog read to it.
# --------------------------------------------------------------------------- #
OTHER = "unrelated"


def _full_valid_activities(ddl, ref_table):
    """Create a full valid `activities` shape in `ddl`'s schema referencing
    `ref_table` (schema-qualified)."""
    for stmt in ddl.format(s=SCHEMA).split(";"):
        if stmt.strip():
            yield stmt


class TestSchemaNoneDoesNotCombineRelations:
    """Two schemas each contain `activities` (and `users`). The intended relation is
    the one in search_path. Misleading metadata in the OTHER schema must neither mask
    a genuine defect nor create a false mismatch."""

    @staticmethod
    def _prod_shape(conn):
        conn.execute(text(PROD_SHAPE_DDL.format(s=SCHEMA)))

    @pytest.fixture()
    def two_schema(self, engine):
        with engine.begin() as c:
            c.execute(text(f"SET search_path TO {SCHEMA}"))
            c.execute(text(f"DROP SCHEMA IF EXISTS {OTHER} CASCADE"))
            c.execute(text(f"CREATE SCHEMA {OTHER}"))
            c.execute(text(f"CREATE TABLE {OTHER}.users (id uuid PRIMARY KEY)"))
            c.execute(text(
                f"CREATE TABLE {OTHER}.activities ("
                "    id uuid NOT NULL DEFAULT gen_random_uuid(),"
                "    user_id uuid NOT NULL,"
                "    activity_date date NOT NULL,"
                "    title character varying(1) NOT NULL,"            # misleading
                "    description text,"
                "    calories_burned double precision NOT NULL,"
                "    created_at timestamp without time zone NOT NULL DEFAULT now(),"
                "    updated_at timestamp with time zone NOT NULL DEFAULT now(),"
                "    CONSTRAINT activities_pkey PRIMARY KEY (id),"
                "    CONSTRAINT activities_calories_burned_check "
                "        CHECK (calories_burned >= 0::double precision),"  # VALID here
                "    CONSTRAINT activities_user_id_fkey FOREIGN KEY (user_id)"
                "        REFERENCES {s}.users(id) ON DELETE CASCADE"
                ")".format(s=OTHER)
            ))
            c.execute(text(
                f"CREATE INDEX idx_activities_user_date ON {OTHER}.activities "
                "(user_id) WHERE false"          # partial, misleading
            ))
        yield
        with engine.begin() as c:
            c.execute(text(f"DROP SCHEMA IF EXISTS {OTHER} CASCADE"))

    def test_types_and_checks_come_from_the_intended_relation(self, two_schema, conn):
        """The intended (search_path) `activities` is valid; the OTHER schema holds a
        VALID `>= 0` CHECK and a WRONG `created_at` type. Neither may leak: the
        intended relation's correct types must be accepted, and a wrong CHECK in the
        intended relation must NOT be masked by the OTHER relation's valid one."""
        conn.execute(text(PROD_SHAPE_DDL.format(s=SCHEMA)))
        # No leak of the OTHER schema's timestamp-without-tz into created_at:
        diff = sa_act.diff_activities(conn, schema=None)
        assert diff["fatal"] == [], diff["fatal"]

        # Now corrupt the INTENDED relation's CHECK. Its wrong bound must be FATAL
        # even though the OTHER relation carries a valid `>= 0` check.
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities DROP CONSTRAINT "
            "activities_calories_burned_check"
        ))
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities ADD CONSTRAINT "
            "activities_calories_burned_check CHECK (calories_burned >= -100)"
        ))
        diff = sa_act.diff_activities(conn, schema=None)
        assert any("calories_burned" in f for f in diff["fatal"]),             f"wrong CHECK masked by the other schema's valid check: fatal={diff['fatal']}"

    def test_foreign_key_resolved_by_identity_not_name(self, two_schema, conn):
        """A faulty target relation (OTHER.users) is FATAL through schema=None; the
        intended relation's own users must not be confused with the OTHER schema's."""
        conn.execute(text(PROD_SHAPE_DDL.format(s=SCHEMA)))
        # repoint the FK at OTHER.users: wrong identity, shared name
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities DROP CONSTRAINT "
            "activities_user_id_fkey"
        ))
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities ADD CONSTRAINT activities_user_id_fkey "
            f"FOREIGN KEY (user_id) REFERENCES {OTHER}.users(id) ON DELETE CASCADE"
        ))
        diff = sa_act.diff_activities(conn, schema=None)
        assert any("foreign key" in f for f in diff["fatal"]),             f"cross-schema FK accepted through schema=None: fatal={diff['fatal']}"

    def test_unqualified_fk_resolves_to_intended_relation(self, two_schema, conn):
        """`REFERENCES users(id)` (schema omitted, search_path-visible) must resolve
        to the intended users relation and be ACCEPTED - the reviewer's
        search_path-sensitive reflection case, in real PostgreSQL."""
        conn.execute(text(PROD_SHAPE_DDL.format(s=SCHEMA)))
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities DROP CONSTRAINT activities_user_id_fkey"
        ))
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities ADD CONSTRAINT activities_user_id_fkey "
            "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"
        ))
        diff = sa_act.diff_activities(conn, schema=None)
        assert diff["fatal"] == [], diff["fatal"]


class TestConservativeTypeAndDefaultEquivalence:
    """REVIEW #9 finding 3: type ATTRIBUTES (VARCHAR length, timestamp precision)
    and casts in defaults are part of equivalence and must be preserved."""

    @staticmethod
    def _prod_shape(conn):
        conn.execute(text(PROD_SHAPE_DDL.format(s=SCHEMA)))

    def test_varchar1_title_is_fatal(self, conn):
        """VARCHAR(1) cannot hold ordinary activity titles; it is not equivalent."""
        self._prod_shape(conn)
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities ALTER COLUMN title TYPE varchar(1)"
        ))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("title" in f and "type" in f for f in diff["fatal"]),             f"VARCHAR(1) accepted: fatal={diff['fatal']}"

    def test_varchar255_baseline_is_accepted(self, conn):
        """The declared length is preserved and the unchanged shape stays clean."""
        self._prod_shape(conn)
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert diff["fatal"] == [], diff["fatal"]

    def test_timestamp_precision_is_fatal(self, conn):
        """`timestamp(3) with time zone` differs from the declared no-precision shape."""
        self._prod_shape(conn)
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities ALTER COLUMN created_at "
            "TYPE timestamp(3) with time zone"
        ))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("created_at" in f and "type" in f for f in diff["fatal"]),             f"timestamp(3) accepted: fatal={diff['fatal']}"

    def test_cast_changing_default_is_fatal(self, conn):
        """A cast-altered default is not equivalent. Verified against PostgreSQL's
        EXACT stored rendering (pg_get_expr), not a fixture normaliser."""
        self._prod_shape(conn)
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities ALTER COLUMN created_at "
            "SET DEFAULT (now()::date)"
        ))
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("created_at" in f and "default" in f for f in diff["fatal"]),             f"cast-changed default accepted: fatal={diff['fatal']}"

    def test_redundant_cast_pg_rendering_decides(self, conn):
        """PG may simplify a redundant cast away; whatever it stores is the basis.
        The declared `DEFAULT now()` on the intended relation stays accepted."""
        self._prod_shape(conn)
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities ALTER COLUMN created_at "
            "SET DEFAULT now()"
        ))
        got = conn.execute(text(
            f"SELECT pg_get_expr(d.adbin, d.adrelid) "
            f"FROM pg_attrdef d "
            f"JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
            f"JOIN pg_class c ON c.oid = d.adrelid "
            f"JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE c.relname = 'activities' AND n.nspname = '{SCHEMA}' "
            f"AND a.attname = 'created_at'"
        )).scalar()
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert got.strip() == "now()", f"unexpected stored rendering: {got!r}"
        assert diff["fatal"] == [], diff["fatal"]


# --------------------------------------------------------------------------- #
# REVIEW #10 - the FK must validate the complete COLUMN MAPPING, and the users
# target must come from the RESOLVED activities namespace (same-schema policy).
# --------------------------------------------------------------------------- #
class TestForeignKeyMappingAndSchemaPolicy:
    """The OID rewrite in round 9 checked the referred TABLE but dropped the referred
    COLUMN list, so `REFERENCES users(alternate_id)` could pass against a compatible
    unique UUID column. Round 10 compares both sides of the mapping in ordinal order,
    and derives the intended users relation from the resolved activities namespace."""

    @staticmethod
    def _prod_shape(conn):
        conn.execute(text(PROD_SHAPE_DDL.format(s=SCHEMA)))

    @staticmethod
    def _reqpoint_fk(conn, target: str):
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities DROP CONSTRAINT activities_user_id_fkey"
        ))
        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.activities ADD CONSTRAINT activities_user_id_fkey "
            f"FOREIGN KEY (user_id) REFERENCES {target} ON DELETE CASCADE"
        ))

    def test_referenced_column_alternate_id_is_fatal(self, conn):
        """A different referenced COLUMN is a different constraint, even on the right
        table with a compatible unique UUID type."""
        self._prod_shape(conn)
        # The baseline (references users(id)) must pass first.
        assert sa_act.diff_activities(conn, schema=SCHEMA)["fatal"] == []

        conn.execute(text(
            f"ALTER TABLE {SCHEMA}.users ADD COLUMN alternate_id uuid UNIQUE"
        ))
        self._reqpoint_fk(conn, "users(alternate_id)")

        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert any("references columns" in f and "id" in f for f in diff["fatal"]), \
            f"REFERENCES users(alternate_id) was accepted: fatal={diff['fatal']}"

    def test_intended_fk_baseline_still_passes(self, conn):
        self._prod_shape(conn)
        diff = sa_act.diff_activities(conn, schema=SCHEMA)
        assert diff["fatal"] == [], diff["fatal"]

    def test_users_only_leading_schema_is_not_used(self, engine):
        """search_path = first, second; `first` holds users ONLY, `second` holds both.

        `activities` resolves to second.activities, so the same-schema policy requires
        the users relation in SECOND's namespace. A FK pointing at first.users (which
        an independent unqualified lookup of `users` would have found) must be FATAL.
        """
        with engine.begin() as c:
            for name in ("first", "second"):
                c.execute(text(f"DROP SCHEMA IF EXISTS {name} CASCADE"))
                c.execute(text(f"CREATE SCHEMA {name}"))
                c.execute(text(f"CREATE TABLE {name}.users (id uuid PRIMARY KEY)"))
            c.execute(text(
                f"CREATE TABLE second.activities ("
                "    id uuid NOT NULL DEFAULT gen_random_uuid(),"
                "    user_id uuid NOT NULL,"
                "    activity_date date NOT NULL,"
                "    title character varying(255) NOT NULL,"
                "    description text,"
                "    calories_burned double precision NOT NULL,"
                "    created_at timestamp with time zone NOT NULL DEFAULT now(),"
                "    updated_at timestamp with time zone NOT NULL DEFAULT now(),"
                "    CONSTRAINT activities_pkey PRIMARY KEY (id),"
                f"    CONSTRAINT activities_calories_burned_check CHECK ({sa_act.CHECK_EXPR}),"
                "    CONSTRAINT activities_user_id_fkey FOREIGN KEY (user_id)"
                "        REFERENCES second.users(id) ON DELETE CASCADE"
                ")"
            ))
            c.execute(text(
                "CREATE INDEX ix_activities_user_id ON second.activities (user_id)"
            ))
            c.execute(text(
                "CREATE INDEX idx_activities_user_date ON second.activities "
                "(user_id, activity_date)"
            ))
            # first leads the search_path but has NO activities.
            c.execute(text("SET search_path TO first, second"))
            # the same-schema target is accepted through the DEFAULT schema=None entry
            assert sa_act.diff_activities(c, schema=None)["fatal"] == []

            # Repoint at first.users - the schema an independent `users` lookup finds.
            c.execute(text(
                "ALTER TABLE second.activities DROP CONSTRAINT activities_user_id_fkey"
            ))
            c.execute(text(
                "ALTER TABLE second.activities ADD CONSTRAINT activities_user_id_fkey "
                "FOREIGN KEY (user_id) REFERENCES first.users(id) ON DELETE CASCADE"
            ))
            diff = sa_act.diff_activities(c, schema=None)
            assert any("foreign key" in f for f in diff["fatal"]), \
                f"cross-namespace target accepted under users-only-leading search_path: {diff['fatal']}"
        with engine.begin() as c:
            for name in ("first", "second"):
                c.execute(text(f"DROP SCHEMA IF EXISTS {name} CASCADE"))


# --------------------------------------------------------------------------- #
# REVIEW #11 - the CREATION path must obey the same-schema policy too. With
# schema=None an unqualified CREATE TABLE lands by search_path while an unqualified
# REFERENCES users(id) resolves independently, so creation could bind a cross-schema
# users that the validator then rejects.
# --------------------------------------------------------------------------- #
class TestCreationDestinationNamespace:
    """The destination namespace is resolved (explicit schema, or the first schema the
    user may CREATE in for schema=None) and BOTH the table and its users target are
    qualified to it. A destination without its own users relation fails loudly."""

    @staticmethod
    def _two_schemas(engine, first_has_users: bool, second_has_users: bool,
                     second_has_activities: bool = False):
        with engine.begin() as c:
            for name in ("first", "second"):
                c.execute(text(f"DROP SCHEMA IF EXISTS {name} CASCADE"))
                c.execute(text(f"CREATE SCHEMA {name}"))
            if first_has_users:
                c.execute(text("CREATE TABLE first.users (id uuid PRIMARY KEY)"))
            if second_has_users:
                c.execute(text("CREATE TABLE second.users (id uuid PRIMARY KEY)"))
            if second_has_activities:
                c.execute(text(
                    "CREATE TABLE second.activities ("
                    "    id uuid NOT NULL DEFAULT gen_random_uuid(),"
                    "    user_id uuid NOT NULL,"
                    "    activity_date date NOT NULL,"
                    "    title character varying(255) NOT NULL,"
                    "    calories_burned double precision NOT NULL,"
                    "    created_at timestamp with time zone NOT NULL DEFAULT now(),"
                    "    updated_at timestamp with time zone NOT NULL DEFAULT now(),"
                    "    CONSTRAINT activities_pkey PRIMARY KEY (id),"
                    "    CONSTRAINT activities_user_id_fkey FOREIGN KEY (user_id)"
                    "        REFERENCES second.users(id) ON DELETE CASCADE"
                    ")"
                ))
                c.execute(text(
                    "CREATE INDEX ix_activities_user_id ON second.activities (user_id)"
                ))
                c.execute(text(
                    "CREATE INDEX idx_activities_user_date ON second.activities "
                    "(user_id, activity_date)"
                ))

    @staticmethod
    def _drop(engine):
        with engine.begin() as c:
            for name in ("first", "second"):
                c.execute(text(f"DROP SCHEMA IF EXISTS {name} CASCADE"))

    def _fk_of(self, conn, schema):
        return conn.execute(text("""
            SELECT c.confrelid, r.relname AS ref_table, n.nspname AS ref_schema,
                   string_agg(fa.attname, ',' ORDER BY f.ord) AS ref_cols
            FROM pg_constraint c
            JOIN unnest(c.confkey) WITH ORDINALITY f(attnum, ord) ON true
            JOIN pg_attribute fa ON fa.attrelid = c.confrelid AND fa.attnum = f.attnum
            JOIN pg_class r ON r.oid = c.confrelid
            JOIN pg_namespace n ON n.oid = r.relnamespace
            WHERE c.conname = :fk AND c.conrelid = (
                SELECT k.oid FROM pg_class k JOIN pg_namespace nn ON nn.oid = k.relnamespace
                WHERE k.relname = 'activities' AND nn.nspname = :s)
            GROUP BY c.confrelid, r.relname, n.nspname
        """), {"fk": sa_act.FK_NAME, "s": schema}).mappings().one()

    def _users_oid(self, conn, schema):
        return conn.execute(
            text("SELECT c.oid FROM pg_class c"
                 " JOIN pg_namespace n ON n.oid = c.relnamespace"
                 " WHERE c.relname = 'users' AND n.nspname = :s"), {"s": schema}
        ).scalar()

    def test_default_schema_creation_refuses_cross_schema_users(self, engine):
        """search_path = first, second; `first` holds neither users nor activities;
        `second` holds users but no activities.

        The destination for an unqualified CREATE is `first`, and an unqualified
        `REFERENCES users(id)` would have found `second.users` - a cross-schema
        relationship the validator rejects. Creation must FAIL, leaving no table.
        """
        self._two_schemas(engine, first_has_users=False, second_has_users=True)
        try:
            with engine.begin() as c:
                c.execute(text("SET search_path TO first, second"))
                with pytest.raises(sa_act.MissingUsersRelation) as ei:
                    sa_act.create_activities(c)
                assert "users" in str(ei.value)
                # no new table or indexes may survive (the caller's transaction rolls
                # back, but assert the failure itself created nothing here)
                assert c.execute(text(
                    "SELECT count(*) FROM pg_class k JOIN pg_namespace n"
                    " ON n.oid = k.relnamespace"
                    " WHERE k.relname = 'activities' AND n.nspname = 'first'"
                )).scalar() == 0
            with engine.begin() as c:
                assert c.execute(text(
                    "SELECT count(*) FROM pg_class k JOIN pg_namespace n"
                    " ON n.oid = k.relnamespace"
                    " WHERE k.relname = 'activities' AND n.nspname = 'first'"
                )).scalar() == 0
                assert c.execute(text(
                    "SELECT count(*) FROM pg_indexes"
                    " WHERE schemaname = 'first' AND tablename = 'activities'"
                )).scalar() == 0
        finally:
            self._drop(engine)

    def test_default_schema_creation_binds_same_schema_users(self, engine):
        """Positive: with schema=None and a destination that owns a users relation, the
        created FK points at THAT schema's users, with the referenced column `id`."""
        self._two_schemas(engine, first_has_users=True, second_has_users=True,
                          second_has_activities=True)
        try:
            with engine.begin() as c:
                c.execute(text("SET search_path TO first, second"))
                sa_act.create_activities(c)
                fk = self._fk_of(c, "first")
                assert fk["ref_schema"] == "first", fk
                assert fk["confrelid"] == self._users_oid(c, "first"), fk
                assert fk["ref_cols"] == "id", fk
                # and the resulting table validates through the same entry point
                c.execute(text("SET search_path TO first, second"))
                assert sa_act.diff_activities(c, schema=None)["fatal"] == []
        finally:
            self._drop(engine)

    def test_explicit_schema_creation_ignores_misleading_search_path(self, engine):
        """Positive: an explicit schema is created WITH its own users even when another
        schema's users leads the search path."""
        self._two_schemas(engine, first_has_users=True, second_has_users=True)
        try:
            with engine.begin() as c:
                c.execute(text("SET search_path TO first"))   # misleading: first.users
                sa_act.create_activities(c, schema="second")
                fk = self._fk_of(c, "second")
                assert fk["ref_schema"] == "second", fk
                assert fk["confrelid"] == self._users_oid(c, "second"), fk
                assert fk["ref_cols"] == "id", fk
                assert sa_act.diff_activities(c, schema="second")["fatal"] == []
        finally:
            self._drop(engine)
