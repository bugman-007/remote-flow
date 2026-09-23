"""manager-defined interview steps, statuses and per-step history

INT-14/15/16: the Manager owns an ordered list of steps and a set of status
labels; each interview records who handled which step (done/rejected), gains a
tech stack, and can be created manually with an attached resume + JD.

Revision ID: d4e5f6a7b8c9
Revises: c2a7d9e10f31
Create Date: 2026-09-23 02:00:00.000000+00:00
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401 - custom GUID / DateTimeTZ types

revision = 'd4e5f6a7b8c9'
down_revision = 'c2a7d9e10f31'
branch_labels = None
depends_on = None

DEFAULT_STEPS = [
    ("Phone call", "#38bdf8"),
    ("Initial meeting", "#a78bfa"),
    ("Tech meeting", "#34d399"),
    ("Final meeting", "#fbbf24"),
]
DEFAULT_STATUSES = [
    ("Scheduled", "#38bdf8"),
    ("Rescheduled", "#fbbf24"),
    ("Done", "#34d399"),
    ("Cancelled", "#94a3b8"),
    ("Rejected", "#f87171"),
]
#: legacy enum value -> default status label
STATUS_MAP = {
    "scheduled": "Scheduled",
    "completed": "Done",
    "cancelled": "Cancelled",
    "no_show": "Done",
}


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _insert(table: str, rows: list[tuple[str, str]]) -> dict[str, str]:
    """Insert name/colour rows and return ``name -> id``."""
    ids: dict[str, str] = {}
    now = datetime.now(timezone.utc)
    for position, (name, color) in enumerate(rows):
        row_id = str(uuid.uuid4())
        ids[name] = row_id
        op.execute(
            sa.text(
                f"INSERT INTO {table} (id, name, color, position, is_active, created_at, updated_at) "
                "VALUES (:id, :name, :color, :position, :is_active, :created_at, :updated_at)"
            ).bindparams(
                id=row_id, name=name, color=color, position=position,
                is_active=True, created_at=now, updated_at=now,
            )
        )
    return ids


def upgrade() -> None:
    op.create_table(
        'interview_steps',
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('color', sa.String(length=32), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('id', app.models.GUID(length=36), nullable=False),
        sa.Column('created_at', app.models.DateTimeTZ(), nullable=False),
        sa.Column('updated_at', app.models.DateTimeTZ(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_index('ix_interview_steps_position', 'interview_steps', ['position'], unique=False)

    op.create_table(
        'interview_statuses',
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('color', sa.String(length=32), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('id', app.models.GUID(length=36), nullable=False),
        sa.Column('created_at', app.models.DateTimeTZ(), nullable=False),
        sa.Column('updated_at', app.models.DateTimeTZ(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_index('ix_interview_statuses_position', 'interview_statuses', ['position'], unique=False)

    op.create_table(
        'interview_step_records',
        sa.Column('interview_id', app.models.GUID(length=36), nullable=True),
        sa.Column('step_id', app.models.GUID(length=36), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('reviewer_id', app.models.GUID(length=36), nullable=True),
        sa.Column('done', sa.Boolean(), nullable=False),
        sa.Column('rejected', sa.Boolean(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('done_at', app.models.DateTimeTZ(), nullable=True),
        sa.Column('id', app.models.GUID(length=36), nullable=False),
        sa.Column('created_at', app.models.DateTimeTZ(), nullable=False),
        sa.Column('updated_at', app.models.DateTimeTZ(), nullable=False),
        sa.ForeignKeyConstraint(['interview_id'], ['interviews.id']),
        sa.ForeignKeyConstraint(['step_id'], ['interview_steps.id']),
        sa.ForeignKeyConstraint(['reviewer_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_interview_step_records_interview_id', 'interview_step_records', ['interview_id'], unique=False)
    op.create_index('ix_interview_step_records_reviewer_id', 'interview_step_records', ['reviewer_id'], unique=False)

    op.create_table(
        'interview_attachments',
        sa.Column('interview_id', app.models.GUID(length=36), nullable=True),
        sa.Column('kind', sa.Enum('resume', 'jd', name='interview_attachment_kind', native_enum=False, length=32), nullable=False),
        sa.Column('filename', sa.String(length=300), nullable=False),
        sa.Column('path', sa.String(length=1000), nullable=False),
        sa.Column('content_type', sa.String(length=200), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=True),
        sa.Column('id', app.models.GUID(length=36), nullable=False),
        sa.Column('created_at', app.models.DateTimeTZ(), nullable=False),
        sa.Column('updated_at', app.models.DateTimeTZ(), nullable=False),
        sa.ForeignKeyConstraint(['interview_id'], ['interviews.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_interview_attachments_interview_id', 'interview_attachments', ['interview_id'], unique=False)

    with op.batch_alter_table('interviews', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tech_stack', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('status_id', app.models.GUID(length=36), nullable=True))
        batch_op.add_column(sa.Column('company_name', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('job_title', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('candidate_name', sa.String(length=300), nullable=True))
        batch_op.create_index('ix_interviews_company_name', ['company_name'], unique=False)
        batch_op.alter_column('doc_set_id', existing_type=app.models.GUID(length=36), nullable=True)
        batch_op.alter_column('generation_id', existing_type=app.models.GUID(length=36), nullable=True)
        batch_op.create_index('ix_interviews_status_id', ['status_id'], unique=False)
        batch_op.create_foreign_key('fk_interviews_status_id', 'interview_statuses', ['status_id'], ['id'])

    step_ids = _insert('interview_steps', DEFAULT_STEPS)
    status_ids = _insert('interview_statuses', DEFAULT_STATUSES)
    del step_ids  # only the statuses are needed to backfill existing rows

    op.execute(
        sa.text(
            "UPDATE interviews SET company_name = ("
            "SELECT company_name FROM doc_sets WHERE doc_sets.id = interviews.doc_set_id), "
            "job_title = (SELECT job_title FROM doc_sets WHERE doc_sets.id = interviews.doc_set_id), "
            "candidate_name = (SELECT candidate_name FROM doc_sets WHERE doc_sets.id = interviews.doc_set_id) "
            "WHERE doc_set_id IS NOT NULL"
        )
    )

    for legacy, label in STATUS_MAP.items():
        op.execute(
            sa.text("UPDATE interviews SET status_id = :status_id WHERE status = :legacy").bindparams(
                status_id=status_ids[label], legacy=legacy
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('interviews', schema=None) as batch_op:
        batch_op.drop_constraint('fk_interviews_status_id', type_='foreignkey')
        batch_op.drop_index('ix_interviews_status_id')
        batch_op.drop_column('candidate_name')
        batch_op.drop_column('job_title')
        batch_op.drop_index('ix_interviews_company_name')
        batch_op.drop_column('company_name')
        batch_op.drop_column('status_id')
        batch_op.drop_column('tech_stack')
    op.drop_index('ix_interview_attachments_interview_id', table_name='interview_attachments')
    op.drop_table('interview_attachments')
    op.drop_index('ix_interview_step_records_reviewer_id', table_name='interview_step_records')
    op.drop_index('ix_interview_step_records_interview_id', table_name='interview_step_records')
    op.drop_table('interview_step_records')
    op.drop_index('ix_interview_statuses_position', table_name='interview_statuses')
    op.drop_table('interview_statuses')
    op.drop_index('ix_interview_steps_position', table_name='interview_steps')
    op.drop_table('interview_steps')
