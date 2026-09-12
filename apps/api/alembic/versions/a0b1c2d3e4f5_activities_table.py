"""Create the activities table (fresh database) if it does not already exist.

Revision ID: a0b1c2d3e4f5
Revises: e1f2a3b4c5d6
Create Date: 2026-09-12

The ORM declares `models.Activity` but no live Alembic migration ever created
the `activities` table -- a database built purely from the chain could not serve
a fresh application bootstrap. The adoption revision (b7c8d9e0f1a2) is parked
under alembic/deferred/ and must remain undiscovered by Alembic; this is the
live, guarded counterpart that creates the table on a fresh database while
no-op-ing on an ORM-built database that already has it.

Guarded (inspect before alter) because this project has two schema lineages:
databases built by the Alembic chain and databases built by ORM create_all.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a0b1c2d3e4f5'
down_revision: Union[str, None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

TABLE = 'activities'


def _table_exists(bind) -> bool:
    return TABLE in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind):
        # ORM-built database already has the table; nothing to do.
        return
    op.create_table(
        TABLE,
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('activity_date', sa.Date(), nullable=False),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('calories_burned', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )
    op.create_index('ix_activities_user_id', TABLE, ['user_id'])
    op.create_index('ix_activities_user_date', TABLE, ['user_id', 'activity_date'])


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind):
        return
    op.drop_index('ix_activities_user_date', table_name=TABLE)
    op.drop_index('ix_activities_user_id', table_name=TABLE)
    op.drop_table(TABLE)
