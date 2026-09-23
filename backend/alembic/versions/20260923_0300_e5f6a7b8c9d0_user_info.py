"""free-form Manager notes on a user

USR-9: the Users page shows Maker/Reviewer cards with an Information box the
Manager can fill in (phone number, location, …).

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-23 03:00:00.000000+00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

import app.models  # noqa: F401 - custom GUID / DateTimeTZ types

revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('info', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'info')
