"""track when a Maker downloaded a doc set

RES-14: the Resumes page gains a "New" filter for sets the Maker has never
downloaded. Store the timestamp on the doc set so the list can filter on it.

Revision ID: c2a7d9e10f31
Revises: b1f0c2d4e5a6
Create Date: 2026-09-23 00:30:00.000000+00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

import app.models  # noqa: F401 - custom GUID / DateTimeTZ types

revision = 'c2a7d9e10f31'
down_revision = 'b1f0c2d4e5a6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('doc_sets', sa.Column('downloaded_at', app.models.DateTimeTZ(), nullable=True))
    op.create_index('ix_doc_sets_downloaded_at', 'doc_sets', ['downloaded_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_doc_sets_downloaded_at', table_name='doc_sets')
    op.drop_column('doc_sets', 'downloaded_at')
