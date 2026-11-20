"""A persona create that never finished is hidden and erased (R9-293 review HIGH-2).

``personas.create_complete`` is false from the create's INSERT until its identity is indexed.
A row still false is a create whose process died between the commit and the undo: hidden from
every owner-facing read, and erased by the persona erasure sweep after a grace period.
Existing rows are complete (the server default). Idempotent split-home: on a fresh database
``001_initial``'s ``metadata.create_all`` already built the column from the canonical model.

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
    if _has_column():
        return
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    if _has_column():
        op.drop_column(_TABLE, _COLUMN)
