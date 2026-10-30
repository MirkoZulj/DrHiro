"""Reconstructed placeholder for the missing revision a0b1c2d3e4f5.

The original file is gone.  ``b1c2d3e4f5a6_log_write_fingerprints`` declares
``down_revision = "a0b1c2d3e4f5"`` but no revision with that id existed in this
repository, so Alembic could not build its revision map at all
(``KeyError: 'a0b1c2d3e4f5'`` from ``alembic/script/revision.py``) and every
alembic command failed — ``heads``, ``history``, ``current``, ``upgrade``.

This revision is a deliberate NO-OP.  The database it describes was built by
ORM ``create_all`` rather than by walking this chain, so whatever objects the
original revision created are already present.  Guessed DDL here would risk
breaking a database that is built from scratch, and the original statements
cannot be recovered.  It exists only so the graph loads and so future
migrations have consistent ancestry.

Do not add DDL to this file.  If the original revision is ever recovered,
replace this file with it, keeping the same revision id.
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "a0b1c2d3e4f5"
down_revision: Union[str, None] = "3c00321778bc"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    """No-op: see the module docstring."""


def downgrade() -> None:
    """No-op: see the module docstring."""
