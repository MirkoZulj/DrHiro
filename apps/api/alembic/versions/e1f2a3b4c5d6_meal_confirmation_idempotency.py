"""Meal confirmation idempotency via (user_id, source_operation_id).

Revision ID: e1f2a3b4c5d6
Revises: d6e7f8a9b0c1
Create Date: 2026-09-12

Problem
-------
``confirm_meal`` does a standalone ``SELECT`` looking for a recent duplicate and
then an **unconstrained** ``INSERT``. Two overlapping confirmation retries can
both pass the lookup before either commit and persist SEPARATE meals for the
same draft -- the check-then-insert race that Finding #3 reports. (A prior
round added the ``user_id`` filter to the lookup, which fixed cross-user leakage
but not this intra-user race.)

Fix
---
``confirm_meal`` now stamps each meal with a *stable* idempotency key -- the
draft id the caller already re-sends on retry (see
``packages/drhiro-mcp/.../sse_server.py``: the retry re-POSTs the same
``confirm_payload`` with the same ``draft_id``). A database UNIQUE constraint
on ``(user_id, source_operation_id)`` makes the claim atomic: the ``INSERT`` is
issued with ``ON CONFLICT DO UPDATE ... RETURNING`` so a second concurrent
confirm re-uses the already-created row instead of writing a duplicate.

The column itself (``meals.source_operation_id``) already exists -- it was added
by f1a2b3c4d5e6 (a transitive ancestor of this revision) and is declared on
the ORM ``Meal``. This migration only adds the unique index. Because the column
is **nullable** (legacy rows carry ``NULL``), the index is ``PARTIAL`` (``WHERE
source_operation_id IS NOT NULL``) so multiple legacy NULL rows remain allowed --
a plain unique constraint would reject every NULL row beyond the first.

Guarded (inspect before alter) because this project has two schema lineages:
databases built by the Alembic chain and databases built by ORM ``create_all``.
Both upgrade and downgrade no-op safely if the index is already present/absent.
An additional defensive guard skips index creation if the column is absent (it
is guaranteed to exist on any supported upgrade path).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'd6e7f8a9b0c1'
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

TABLE = 'meals'
COLUMN = 'source_operation_id'
INDEX = 'uq_meals_user_source_op'


def _indexes(bind) -> set:
    """Index names on ``meals``, or empty set if the table is absent."""
    insp = sa.inspect(bind)
    if TABLE not in insp.get_table_names():
        return set()
    # get_indexes returns dicts with a 'name' key.
    return {idx['name'] for idx in insp.get_indexes(TABLE)}


def _columns(bind, table: str) -> set:
    """Column names of `table`, or empty set if the table is absent."""
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {c['name'] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        # meals is created by the canonical-schema migration, an ancestor of this
        # revision; nothing to index if the table is genuinely absent.
        return
    if COLUMN not in _columns(bind, TABLE):
        # Defensive guard: the column is added by f1a2b3c4d5e6, a transitive
        # ancestor of this revision, so it is guaranteed to exist on any
        # supported upgrade path. If it is somehow absent (e.g. a hand-edited
        # schema), skip index creation rather than aborting the upgrade.
        print(f"WARNING: {TABLE}.{COLUMN} is absent; skipping index creation "
              f"(expected on any supported upgrade path).")
        return
    if INDEX in _indexes(bind):
        # Idempotent on the ORM-create_all lineage (index already exists) or a
        # re-run.
        return
    op.execute(
        f"CREATE UNIQUE INDEX {INDEX} ON {TABLE} (user_id, {COLUMN}) "
        f"WHERE {COLUMN} IS NOT NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if INDEX in _indexes(bind):
        op.drop_index(INDEX, table_name=TABLE)
