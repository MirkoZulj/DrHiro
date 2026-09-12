"""Add app_settings.telegram_allowed_user_id for settings authorization.

Revision ID: d6e7f8a9b0c1
Revises: c4e5f6a7b8c9
Create Date: 2026-09-12

The settings API previously authorized the administrator by comparing the
configured Telegram *username* against ``ExternalIdentity.provider_subject``,
which stores the numeric Telegram ID. The comparison could therefore never
match and the legitimate administrator was locked out.

The fix authorizes against an immutable numeric Telegram ID held in a new
``app_settings.telegram_allowed_user_id`` column, which the ORM declares
(``models.AppSetting``) and ``settings_store`` reads/writes.

This is the same class of gap as 9a1b2c3d4e5f: the ORM declares the column but
the Alembic chain never created it. Because this project has two schema
lineages -- databases built by the Alembic chain and databases built by ORM
``create_all`` (which already contain the column) -- both upgrade and downgrade
check for the column first so the migration is safe on either lineage.

Additive and reversible: the column is nullable and carries no data.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd6e7f8a9b0c1'
down_revision: Union[str, None] = 'c4e5f6a7b8c9'
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

TABLE = 'app_settings'
COLUMN = 'telegram_allowed_user_id'


def _columns(bind) -> set:
    """Column names of app_settings, or an empty set if the table is absent."""
    insp = sa.inspect(bind)
    if TABLE not in insp.get_table_names():
        return set()
    return {c['name'] for c in insp.get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        # The chain creates app_settings in e7f8a9b0c1d2, which is an ancestor
        # of this revision; nothing to do if the table is genuinely absent.
        return
    if COLUMN not in _columns(bind):
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(64), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if COLUMN in _columns(bind):
        op.drop_column(TABLE, COLUMN)
