"""R4 - `activities` migration: fresh creation vs adoption of an existing table.

Runs against a DISPOSABLE schema in a disposable test database. Gated so the
default suite is unaffected:

    DRHIRO_ACTIVITIES_MIGRATION_DB=1 DRHIRO_TEST_DB_URL=<disposable-db>

No production access. The production-shaped fixture below is transcribed from a
read-only `\\d activities` on the real database (defaults, CHECK, composite index).
"""
from __future__ import annotations

import os

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
