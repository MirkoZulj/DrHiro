"""Add the `activities` table (fresh creation) or validate and adopt it.

Revision ID: b7c8d9e0f1a2
Revises: 9a1b2c3d4e5f
Create Date: 2026-09-09

The ORM declares `activities` but no migration ever created it: a database built
purely from the Alembic chain could not serve a fresh application bootstrap. The
food-domain baseline's docstring claimed "It is provided by a separate migration
that follows 3c003" - no such migration exists. That false claim is corrected here.

Placed at HEAD (down_revision = 9a1b2c3d4e5f), NOT after 3c003 as that docstring
suggested: production has already applied up to d5e6f7a8b9c0, so inserting a
revision mid-chain would rewrite already-applied history.

Two paths, both required:

* FRESH - the table is absent (a chain-built database): create it with the ORM
  columns plus the production-faithful server defaults, CHECK and composite index
  measured from the real schema. The table is then marked owned, so downgrade may
  drop it.
* ADOPT - the table already exists (production, created out-of-band): validate it
  against the declared shape and reconcile supported differences (missing indexes,
  missing CHECK). Column/type/nullability differences are unreconcilable and fail
  with a precise diagnostic rather than being skipped. An adopted table is NOT
  marked owned, so its downgrade is UNSUPPORTED - this migration must never drop
  data it did not create.

`activities` must not be dropped automatically on downgrade for an adopted table.
"""
from typing import Sequence, Union

from alembic import op

from drhiro_api import schema_activities as sa_act

revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, None] = '9a1b2c3d4e5f'
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    conn = op.get_bind()
    if sa_act.table_exists(conn):
        # Existing (possibly production) table: validate, reconcile, never skip.
        actions = sa_act.reconcile_activities(conn)
        if actions:
            print(f"activities adopted; reconciled: {', '.join(actions)}")
        else:
            print("activities adopted; shape already matches the declared schema")
    else:
        sa_act.create_activities(conn)
        sa_act.set_owned(conn)
        print("activities created")


def downgrade() -> None:
    conn = op.get_bind()
    if not sa_act.table_exists(conn):
        return
    # Raises for an adopted (pre-existing) table - see schema_activities.
    sa_act.assert_downgrade_allowed(conn)
    sa_act.drop_activities(conn)
