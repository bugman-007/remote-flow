"""raise the default JD character limit

The general setting ``max_jd_chars`` ships at 200000 now (JD-1). Installs that
were seeded with the old 20000 default keep their stored row, so bump it once
here for anyone who never changed it by hand.

Revision ID: b1f0c2d4e5a6
Revises: f9012ba1bc5d
Create Date: 2026-09-22 23:30:00.000000+00:00
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

revision = 'b1f0c2d4e5a6'
down_revision = 'f9012ba1bc5d'
branch_labels = None
depends_on = None

OLD_LIMIT = 20000
NEW_LIMIT = 200000


def _decode(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


def _encode(value: int, sample: object) -> object:
    return json.dumps(value) if isinstance(sample, str) else value


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, value FROM settings WHERE key = 'max_jd_chars'")
    ).fetchall()
    for row in rows:
        if _decode(row.value) != OLD_LIMIT:
            continue
        bind.execute(
            sa.text("UPDATE settings SET value = :value WHERE id = :id"),
            {"value": _encode(NEW_LIMIT, row.value), "id": row.id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, value FROM settings WHERE key = 'max_jd_chars'")
    ).fetchall()
    for row in rows:
        if _decode(row.value) != NEW_LIMIT:
            continue
        bind.execute(
            sa.text("UPDATE settings SET value = :value WHERE id = :id"),
            {"value": _encode(OLD_LIMIT, row.value), "id": row.id},
        )
