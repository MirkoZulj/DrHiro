"""Reconstructed placeholder for the missing revision a1b2c3d4e5f6.

The original file is gone.  ``c4d8e2f6a9b1_food_resolution_rules`` declares
``down_revision = "a1b2c3d4e5f6"`` but no revision with that id existed in this
repository, so Alembic could not build its revision map and every alembic
command failed (``KeyError`` on the missing parent).

This revision is a deliberate NO-OP, for the same reasons as its sibling
``a0b1c2d3e4f5``: the schema was built by ORM ``create_all``, the original
statements are unrecoverable, and guessed DDL would risk a fresh database.
It exists only so the graph loads.

Note for future migrations: this branch (a1b2c3d4e5f6 -> c4d8e2f6a9b1
food_resolution_rules -> d5e6f7a8b9c0 recipes) is merged back into the main
line by ``a9b8c7d6e5f4``.  Its objects already exist in production
(``food_resolution_rules``, ``recipes``), so those revisions must be STAMPED,
never executed.

Do not add DDL to this file.
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "a0b1c2d3e4f5"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    """No-op: see the module docstring."""


def downgrade() -> None:
    """No-op: see the module docstring."""
