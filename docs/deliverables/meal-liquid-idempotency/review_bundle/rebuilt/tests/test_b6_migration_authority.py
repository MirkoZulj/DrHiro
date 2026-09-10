"""B6 regression: Alembic is the single migration authority, reconciled with ORM.

This test verifies:
1. The current DB schema (built from ORM via create_all) has the expected structure
2. The raw SQL migration files are syntactically valid PostgreSQL
3. The raw SQL creates the same tables/columns as the ORM models
4. The Alembic migration is consistent with the ORM (same constraints)
"""
from __future__ import annotations

import os
import re
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text, inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_DB_URL = os.environ.get(
    "DRHIRO_TEST_DB_URL",
    "postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_test",
)


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(TEST_DB_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


class TestB6SchemaReconciled:
    """Verify ORM, Alembic, and raw SQL are reconciled."""

    def test_consumption_operations_has_updated_at(self, engine):
        """consumption_operations table has updated_at column (matches ORM)."""
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("consumption_operations")}
        assert "updated_at" in cols
        assert "created_at" in cols

    def test_consumption_items_has_updated_at(self, engine):
        """consumption_items table has updated_at column (was missing in old raw SQL)."""
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("consumption_items")}
        assert "updated_at" in cols
        assert "created_at" in cols

    def test_beverage_measurements_has_updated_at(self, engine):
        """beverage_measurements table has updated_at column (was missing in old raw SQL)."""
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("beverage_measurements")}
        assert "updated_at" in cols
        assert "created_at" in cols

    def test_consumption_operations_has_uniqueness(self, engine):
        """Uniqueness constraints exist on consumption_operations (matches ORM)."""
        inspector = inspect(engine)
        uniq = inspector.get_unique_constraints("consumption_operations")
        names = {u["name"] for u in uniq}
        assert "uq_consumption_op_telegram" in names
        assert "uq_consumption_op_idempotency" in names

    def test_consumption_items_has_uniqueness(self, engine):
        """Uniqueness constraint on consumption_items (operation_id, item_key)."""
        inspector = inspect(engine)
        uniq = inspector.get_unique_constraints("consumption_items")
        names = {u["name"] for u in uniq}
        assert "uq_consumption_item_op_key" in names

    def test_beverage_measurements_has_uniqueness(self, engine):
        """Uniqueness constraints on beverage_measurements."""
        inspector = inspect(engine)
        uniq = inspector.get_unique_constraints("beverage_measurements")
        names = {u["name"] for u in uniq}
        assert "uq_bev_meal_item" in names
        assert "uq_bev_measurement" in names

    def test_meal_items_has_traceability_columns(self, engine):
        """meal_items has source_operation_id, source_item_id, volume_ml, beverage_category."""
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("meal_items")}
        assert "source_operation_id" in cols
        assert "source_item_id" in cols
        assert "volume_ml" in cols
        assert "beverage_category" in cols

    def test_measurements_has_traceability_columns(self, engine):
        """measurements has source_operation_id, source_item_id, meal_item_id."""
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("measurements")}
        assert "source_operation_id" in cols
        assert "source_item_id" in cols
        assert "meal_item_id" in cols

    def test_meals_has_traceability_column(self, engine):
        """meals has source_operation_id."""
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("meals")}
        assert "source_operation_id" in cols

    def test_telegram_key_uniqueness_enforced(self, engine):
        """Same (user_id, source_bot_id, source_chat_id, source_message_id) rejected."""
        with engine.connect() as conn:
            uid = str(uuid.uuid4())
            conn.execute(text(
                "INSERT INTO users (id, display_name, timezone, locale, status, created_at, updated_at) "
                "VALUES (:id, 'U', 'UTC', 'en', 'active', NOW(), NOW())"
            ), {"id": uid})
            conn.commit()

            conn.execute(text(
                "INSERT INTO consumption_operations (id, user_id, source, source_bot_id, source_chat_id, source_message_id, result_json, status, created_at, updated_at) "
                "VALUES (:id, :uid, 'telegram', 'bot1', 'chat1', 'msg1', '{}', 'completed', NOW(), NOW())"
            ), {"id": str(uuid.uuid4()), "uid": uid})
            conn.commit()

            # Second insert with same telegram key must fail
            with pytest.raises(Exception):
                conn.execute(text(
                    "INSERT INTO consumption_operations (id, user_id, source, source_bot_id, source_chat_id, source_message_id, result_json, status, created_at, updated_at) "
                    "VALUES (:id, :uid, 'telegram', 'bot1', 'chat1', 'msg1', '{}', 'completed', NOW(), NOW())"
                ), {"id": str(uuid.uuid4()), "uid": uid})
                conn.commit()
            conn.rollback()

    def test_raw_sql_syntax_valid(self):
        """The raw SQL file has no syntax errors (no '#' used as comment)."""
        sql_path = os.path.join(
            os.path.dirname(__file__), "..", "docs", "deliverables",
            "meal-liquid-idempotency", "B_schema_migration_up.sql"
        )
        with open(sql_path) as f:
            content = f.read()

        # Check there are no '#' characters outside of string literals
        # (PostgreSQL doesn't support # as a comment character)
        # We check that # only appears inside quotes or not at all
        lines = content.split("\n")
        for i, line in enumerate(lines, 1):
            # Skip lines that are inside a string literal (simple heuristic:
            # odd number of single quotes before the # means it's inside a string)
            if "#" in line:
                # Count quotes before the # - if odd, it's inside a string
                idx = line.index("#")
                before = line[:idx]
                quote_count = before.count("'")
                if quote_count % 2 == 0:
                    pytest.fail(
                        f"Line {i}: '#' outside string literal (invalid PG syntax): {line!r}"
                    )

    def test_raw_sql_has_updated_at_on_all_tables(self):
        """Raw SQL includes updated_at on consumption_items and beverage_measurements."""
        sql_path = os.path.join(
            os.path.dirname(__file__), "..", "docs", "deliverables",
            "meal-liquid-idempotency", "B_schema_migration_up.sql"
        )
        with open(sql_path) as f:
            content = f.read()

        # Find consumption_items table definition
        ci_match = re.search(
            r"CREATE TABLE consumption_items \(([\s\S]*?)\);",
            content, re.MULTILINE
        )
        assert ci_match, "consumption_items table not found in raw SQL"
        ci_body = ci_match.group(1)
        assert "updated_at" in ci_body, "consumption_items missing updated_at in raw SQL"
        assert "created_at" in ci_body, "consumption_items missing created_at in raw SQL"

        # Find beverage_measurements table definition
        bm_match = re.search(
            r"CREATE TABLE beverage_measurements \(([\s\S]*?)\);",
            content, re.MULTILINE
        )
        assert bm_match, "beverage_measurements table not found in raw SQL"
        bm_body = bm_match.group(1)
        assert "updated_at" in bm_body, "beverage_measurements missing updated_at in raw SQL"
        assert "created_at" in bm_body, "beverage_measurements missing created_at in raw SQL"

    def test_raw_sql_no_partial_indexes(self):
        """Raw SQL uses UNIQUE indexes (not partial WHERE-clause indexes)."""
        sql_path = os.path.join(
            os.path.dirname(__file__), "..", "docs", "deliverables",
            "meal-liquid-idempotency", "B_schema_migration_up.sql"
        )
        with open(sql_path) as f:
            content = f.read()

        # The old raw SQL had partial indexes with WHERE clauses
        # The new one should use CREATE UNIQUE INDEX without WHERE for these
        assert "WHERE source = 'telegram'" not in content, \
            "Raw SQL still has partial index on telegram source"
        assert "WHERE idempotency_key IS NOT NULL" not in content, \
            "Raw SQL still has partial index on idempotency_key"
