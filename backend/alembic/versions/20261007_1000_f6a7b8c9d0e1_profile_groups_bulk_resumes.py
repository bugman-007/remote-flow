"""profile groups, bulk resumes, job links and automatic skips

PRO-11: Profiles can belong to one named, coloured group.
BULK-1: a Manager uploads a CSV of job links + JDs and generates resumes for a
group or a set of Profiles; each job remembers its source, link and batch.
Jobs the system skipped by itself (duplicate / failed) record why.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-07 10:00:00.000000+00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401 - custom GUID / DateTimeTZ types

revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        'profile_groups',
        sa.Column('id', app.models.GUID(length=36), nullable=False),
        sa.Column('created_at', app.models.DateTimeTZ(), nullable=False),
        sa.Column('updated_at', app.models.DateTimeTZ(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('color', sa.String(length=16), nullable=False),
        sa.Column('created_by', app.models.GUID(length=36), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'bulk_batches',
        sa.Column('id', app.models.GUID(length=36), nullable=False),
        sa.Column('created_at', app.models.DateTimeTZ(), nullable=False),
        sa.Column('updated_at', app.models.DateTimeTZ(), nullable=False),
        sa.Column('created_by', app.models.GUID(length=36), nullable=True),
        sa.Column('filename', sa.String(length=300), nullable=True),
        sa.Column('rows', _json(), nullable=False),
        sa.Column('rejected', _json(), nullable=False),
        sa.Column('generated_at', app.models.DateTimeTZ(), nullable=True),
        sa.Column('summary', _json(), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.add_column('profiles', sa.Column('group_id', app.models.GUID(length=36), nullable=True))
    op.create_index('ix_profiles_group_id', 'profiles', ['group_id'])
    op.create_foreign_key(
        'fk_profiles_group_id', 'profiles', 'profile_groups', ['group_id'], ['id'], ondelete='SET NULL'
    )

    op.add_column('jobs', sa.Column('source', sa.String(length=32), nullable=False, server_default='manual'))
    op.add_column('jobs', sa.Column('job_link', sa.String(length=2000), nullable=True))
    op.add_column('jobs', sa.Column('bulk_batch_id', app.models.GUID(length=36), nullable=True))
    op.add_column('jobs', sa.Column('skip_reason', sa.String(length=40), nullable=True))
    op.create_index('ix_jobs_bulk_batch_id', 'jobs', ['bulk_batch_id'])
    op.create_foreign_key('fk_jobs_bulk_batch_id', 'jobs', 'bulk_batches', ['bulk_batch_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('fk_jobs_bulk_batch_id', 'jobs', type_='foreignkey')
    op.drop_index('ix_jobs_bulk_batch_id', table_name='jobs')
    op.drop_column('jobs', 'skip_reason')
    op.drop_column('jobs', 'bulk_batch_id')
    op.drop_column('jobs', 'job_link')
    op.drop_column('jobs', 'source')
    op.drop_constraint('fk_profiles_group_id', 'profiles', type_='foreignkey')
    op.drop_index('ix_profiles_group_id', table_name='profiles')
    op.drop_column('profiles', 'group_id')
    op.drop_table('bulk_batches')
    op.drop_table('profile_groups')
