"""Verify the activities-table migration creates the soft-delete column.

Qodo #4: a fresh Alembic install must have `deleted_at` on both
`measurements` and `activities` so ledger soft-delete works.
"""
from __future__ import annotations

import os
import sys

import alembic.config
import alembic.script

# tests/ -> repo root is 2 levels up
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_DIR = os.path.join(REPO, "apps", "api", "alembic")

sys.path.insert(0, os.path.join(REPO, "apps", "api", "src"))


def _script():
    cfg = alembic.config.Config(os.path.join(REPO, "apps", "api", "alembic.ini"))
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    return alembic.script.ScriptDirectory.from_config(cfg)


class TestActivitiesSoftDeleteColumn:
    def test_activities_migration_creates_deleted_at(self):
        """a0b1c2d3e4f5 creates `activities` WITH deleted_at."""
        path = os.path.join(ALEMBIC_DIR, "versions",
                            "a0b1c2d3e4f5_activities_table.py")
        src = open(path).read()
        # The activities table definition must include a deleted_at column.
        assert "deleted_at" in src, "activities migration missing deleted_at"

    def test_activities_deleted_at_is_nullable(self):
        """deleted_at must be nullable (additive, no NOT NULL constraint)."""
        path = os.path.join(ALEMBIC_DIR, "versions",
                            "a0b1c2d3e4f5_activities_table.py")
        src = open(path).read()
        # Must appear as nullable=True
        assert "deleted_at" in src and "nullable=True" in src, \
            "activities.deleted_at must be nullable"

    def test_both_ledgers_have_deleted_at_on_fresh_install(self):
        """Chain order: logging-idempotency (9a1b2c3d4e5f) adds deleted_at
        to measurements, activities (a0b1c2d3e4f5) creates activities with
        deleted_at. Verify both migrations exist and are in the chain."""
        revisions = {r.revision: r for r in _script().walk_revisions()}
        # Logging-idempotency migration must exist
        assert "9a1b2c3d4e5f" in revisions, "logging-idempotency migration missing"
        # Activities-table migration must exist
        assert "a0b1c2d3e4f5" in revisions, "activities-table migration missing"
        # Both must be ancestors of the head
        heads = _script().get_heads()
        assert len(heads) == 1, f"Expected 1 head, got {heads}"
        # Walk back from head and ensure both migrations are reached
        found_logging = False
        found_activities = False
        current_id = heads[0]
        rev_map = {r.revision: r for r in _script().walk_revisions()}
        while current_id is not None:
            if current_id == "9a1b2c3d4e5f":
                found_logging = True
            if current_id == "a0b1c2d3e4f5":
                found_activities = True
            rev = rev_map.get(current_id)
            if rev is None:
                break
            current_id = rev.down_revision
            if isinstance(current_id, tuple):
                current_id = current_id[0]
        assert found_logging, "logging-idempotency not in chain"
        assert found_activities, "activities-table not in chain"

    def test_activities_is_head(self):
        """a0b1c2d3e4f5 is the head (creates activities last)."""
        heads = _script().get_heads()
        assert heads[0] == "a0b1c2d3e4f5"
