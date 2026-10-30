"""Merge the reconstructed main line with the food-resolution branch.

The repository had two disconnected heads once the two missing revisions were
reconstructed:

  * main line      : 3c00321778bc -> a0b1c2d3e4f5 -> b1c2d3e4f5a6 ->
                     e6f7a8b9c0d1 -> f7a8b9c0d1e2
  * food branch    : a1b2c3d4e5f6 -> c4d8e2f6a9b1 (food_resolution_rules) ->
                     d5e6f7a8b9c0 (recipes)

A single head is needed or ``alembic upgrade head`` fails with "multiple
heads".  This merge revision joins them and carries NO DDL, because both
branches' objects already exist in production — the schema was built by ORM
``create_all``, not by walking the chain (``food_resolution_rules`` and
``recipes`` are both present and in use).

Production is stamped to this revision.  That is honest rather than a
formality: the schema already contains everything both branches describe, so
there is nothing left to apply.  ``alembic upgrade head`` is therefore a no-op
on any database already stamped here, and future migrations chain off this
revision normally.
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "a9b8c7d6e5f4"
down_revision: Union[str, Sequence[str], None] = ("f7a8b9c0d1e2", "d5e6f7a8b9c0")
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    """No-op merge: both branches' objects already exist (see module docstring)."""


def downgrade() -> None:
    """Split the merge back into its two heads."""
