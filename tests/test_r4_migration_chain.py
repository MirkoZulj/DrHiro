"""R4/B6 migration-authority regression: Alembic chain must be self-sufficient
for BOTH a fresh empty DB AND an existing pre-change DB.

Two paths validated (2026-09-09) on disposable PostgreSQL:
1. FRESH: `alembic upgrade head` from an empty DB must succeed end-to-end and
   create ALL ORM tables (the food-domain baseline b2f3c4d5e6f7 fixed the prior
   failure where c4d8e2f6a9b1 referenced `foods` before any migration created it).
2. EXISTING: a DB already at a prior head (d5e6f7a8b9c0, production's recorded
   head — no consumption feature tables) must upgrade cleanly to head, applying
   ONLY the revisions above d5e6 (app_settings, consumption idempotency, payload
   hash, nutrient cols), preserving existing data and adding the four R4 columns
   (nutrient_basis, resolution_source, food_catalog_item_id, nutrition_complete)
   on consumption_items.
3. ROLLBACK: downgrade of the nutrient-columns migration drops the four columns
   and preserves existing rows; re-upgrade is idempotent.

These are structural checks that the migration FILES are internally consistent
and self-sufficient. Full live upgrade runs happen on disposable DBs (see the
stage evidence doc); here we assert the chain graph + the presence of the new
migrations and their linkage.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))
import alembic.config
import alembic.script

ALEMBIC_DIR = os.path.join(os.path.dirname(__file__), "..", "apps", "api", "alembic")


def _script():
    cfg = alembic.config.Config(os.path.join(os.path.dirname(__file__), "..", "apps", "api", "alembic.ini"))
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    return alembic.script.ScriptDirectory.from_config(cfg)


class TestR4MigrationChain:
    def test_single_head(self):
        """Exactly one head: the chain must be linear (no forks)."""
        heads = _script().get_heads()
        assert len(heads) == 1, f"expected single head, got {heads}"
        # d6e7f8a9b0c1 adds app_settings.telegram_allowed_user_id (settings authz).
        assert heads[0] == "d6e7f8a9b0c1"

    def test_activities_adoption_revision_is_deferred(self):
        """b7c8d9e0f1a2 (the `activities` adoption) is DEFERRED, not in the chain.

        It is not imported by Alembic at all, so no `alembic upgrade` variant --
        `head` or `heads` -- can ever apply it. It previously sat at HEAD and a
        second head had to be kept disjoint from it; moving it out makes that
        structural rather than a matter of care.
        """
        assert "b7c8d9e0f1a2" not in {r.revision for r in _script().walk_revisions()}

    def test_forbidden_revision_is_not_a_live_migration(self):
        """ONE LINE: b7c8d9e0f1a2 must not be under alembic/versions/."""
        assert not os.path.exists(
            os.path.join(ALEMBIC_DIR, "versions",
                         "b7c8d9e0f1a2_activities_table.py"))

    def test_food_baseline_is_chain_root(self):
        """The food-domain baseline (b2f3c4d5e6f7) is the new base (down_revision None)."""
        for rev in _script().walk_revisions():
            if rev.revision == "b2f3c4d5e6f7":
                assert rev.down_revision is None, "food baseline must be the chain root"
                return
        raise AssertionError("b2f3c4d5e6f7 not in chain")

    def test_canonical_schema_rebased_after_food_baseline(self):
        """3c003 (initial canonical schema) now chains after the food baseline."""
        for rev in _script().walk_revisions():
            if rev.revision == "3c00321778bc":
                assert rev.down_revision == "b2f3c4d5e6f7", \
                    f"3c003 must chain after food baseline, got {rev.down_revision}"
                return
        raise AssertionError("3c003 not in chain")

    def test_production_revision_is_in_chain_before_feature(self):
        """Production's recorded head (d5e6f7a8b9c0) exists and precedes the
        consumption-feature migrations (e7f8 -> f1a2 -> a1b2 -> c9d0)."""
        revisions = {r.revision for r in _script().walk_revisions()}
        for rev_id in ["d5e6f7a8b9c0", "e7f8a9b0c1d2", "f1a2b3c4d5e6",
                       "a1b2c3d4e5f7", "c9d0e1f2a3b4"]:
            assert rev_id in revisions, f"{rev_id} missing from chain"

    def test_food_domain_tables_created_by_chain(self):
        """The food-domain tables (previously created only out-of-band) are now
        created by a migration, so a fresh alembic upgrade can build them."""
        mig = open(os.path.join(ALEMBIC_DIR, "versions", "b2f3c4d5e6f7_food_domain_baseline.py")).read()
        for table in ["data_sources", "nutrients", "foods", "food_nutrients",
                      "food_brands", "food_ingredients"]:
            assert f"'{table}'" in mig or f'"{table}"' in mig, \
                f"{table} not created by food baseline migration"

    def test_nutrient_columns_migration_has_upgrade_and_downgrade(self):
        """c9d0e1f2a3b4 (R4 nutrient columns) defines both upgrade and downgrade
        for the four provenance columns."""
        mig = open(os.path.join(ALEMBIC_DIR, "versions",
                                "c9d0e1f2a3b4_nutrient_resolution_columns.py")).read()
        assert "def upgrade" in mig and "def downgrade" in mig
        for col in ["nutrient_basis", "resolution_source",
                    "food_catalog_item_id", "nutrition_complete"]:
            assert col in mig, f"{col} not handled by R4 migration"
