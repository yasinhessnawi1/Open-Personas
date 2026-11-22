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

Downgrade (round 3, Finding 8): code before 069 cannot tell an unfinished create from a
persona, so it would list one, half-made, to its owner. The downgrade therefore deletes every
row ``WHERE NOT create_complete`` before it drops the column; the foreign key cascade takes
the rows that hang off it (its identity chunks among them), as the abandoned-create sweep
would have. What no cascade reaches (graph nodes, workspace files) is erased by the persona
erasure (R9-322): before the delete, the downgrade opens one for each such persona in the
``erasures`` ledger (061, so older code has it and its sweep), and that sweep runs it. The
ledger row holds ids only, as the sweep's own claim does: the persona's conversations, tasks,
runs and event triggers, read before the cascade takes them. A persona that already has an
open erasure keeps it (the ledger allows one), and an id that is not minted
(``persona_<32 hex>``) is skipped, as in 063: the erasure refuses any other.

Revision ID: 069_persona_create_complete
Revises: 068_forgotten_conversations
Create Date: 2026-11-17
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from persona_api.db.models import erasures
from sqlalchemy import inspect

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

revision = "069_persona_create_complete"
down_revision = "068_forgotten_conversations"
branch_labels = None
depends_on = None

_TABLE = "personas"
_COLUMN = "create_complete"
#: The only persona ids an erasure acts on (``new_persona_id``; 063 holds to the same).
_MINTED = re.compile(r"persona_[0-9a-f]{32}")
#: What the erasure's later steps need once the rows are gone, keyed as ``capture_persona``
#: keys it: each table's ids for the persona under its owner.
_CAPTURED = ("conversations", "tasks", "runs", "event_triggers")


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


def _capture(conn: Connection, owner_id: str, persona_id: str) -> dict[str, list[str]]:
    return {
        table: sorted(
            str(row_id)
            for (row_id,) in conn.execute(
                sa.text(f"SELECT id FROM {table} WHERE persona_id = :p AND owner_id = :o"),  # noqa: S608 - constants
                {"p": persona_id, "o": owner_id},
            )
        )
        for table in _CAPTURED
    }


def open_erasures(conn: Connection) -> int:
    """Open a persona erasure for each unfinished create the downgrade deletes; how many."""
    unfinished = conn.execute(
        sa.text(f"SELECT id, owner_id FROM {_TABLE} WHERE NOT {_COLUMN}")  # noqa: S608 - constants
    ).all()
    opened = 0
    for persona_id, owner_id in unfinished:
        if not _MINTED.fullmatch(str(persona_id)):
            continue
        already = conn.execute(
            sa.text(
                "SELECT 1 FROM erasures WHERE subject_kind = 'persona' "
                "AND subject_id = :p AND state <> 'complete'"
            ),
            {"p": persona_id},
        ).first()
        if already is not None:
            continue
        conn.execute(
            erasures.insert().values(
                id=f"erasure_{uuid.uuid4().hex}",
                owner_id=str(owner_id),
                subject_kind="persona",
                subject_id=str(persona_id),
                captured=_capture(conn, str(owner_id), str(persona_id)),
            )
        )
        opened += 1
    return opened


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    if _has_column():
        open_erasures(op.get_bind())
        op.execute(sa.text(f"DELETE FROM {_TABLE} WHERE NOT {_COLUMN}"))  # noqa: S608 - constants
        op.drop_column(_TABLE, _COLUMN)
    op.execute("RESET lock_timeout")
