"""A persona create that never finished is hidden and erased (R9-293 review HIGH-2).

``personas.create_complete`` is false from the create's INSERT until its identity is indexed.
A row still false is a create whose process died between the commit and the undo: hidden from
every owner-facing read, and erased by the persona erasure sweep after a grace period.
Existing rows are complete (the server default). Idempotent split-home: on a fresh database
``001_initial``'s ``metadata.create_all`` already built the column from the canonical model.

Locking (as 057 and 058, R9-293 round 2 LOW-2): ``lock_timeout`` is 5 s, so on a busy
``personas`` table the migration fails fast and can be retried instead of queueing every
persona read and write behind its exclusive lock. It is set for the session at the start and
``RESET`` at the end, upgrade and downgrade alike. Adding a NOT NULL column with a constant
default is a catalog-only change on Postgres 11 and later, so the lock is brief.

Revision ID: 069_persona_create_complete
Revises: 068_forgotten_conversations
Create Date: 2026-11-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "069_persona_create_complete"
down_revision = "068_forgotten_conversations"
branch_labels = None
depends_on = None

_TABLE = "personas"
_COLUMN = "create_complete"


def _has_column() -> bool:
    return any(c["name"] == _COLUMN for c in inspect(op.get_bind()).get_columns(_TABLE))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    if not _has_column():
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Boolean(), nullable=False, server_default=sa.text("true")),
        )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    if _has_column():
        op.drop_column(_TABLE, _COLUMN)
    op.execute("RESET lock_timeout")
