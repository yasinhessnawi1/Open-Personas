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
persona, so it would list one, half-made, to its owner. The downgrade therefore deletes the
unfinished creates before it drops the column; the foreign key cascade takes the rows that hang
off them (their identity chunks among them), as the abandoned-create sweep would have.

What no cascade reaches (graph nodes, workspace files) is erased by the persona erasure
(R9-322): the downgrade opens one for each persona it deleted, in the ``erasures`` ledger (061,
so older code has it and its sweep), and that sweep runs it. In order, in one transaction:

1. Read every unfinished create, and for each the ids its erasure needs once the rows are gone:
   what ``capture_persona`` (``services/erasure_service.py``) captures, mirrored here on
   lightweight tables so this migration does not change when that service does (PR #56 review
   LOW-1): conversations, tasks, runs (the persona's, and those its tasks name), schedules (its
   tasks', its initiative scan ``initsched:<id>``, and any of the owner's whose template names
   the persona, its tasks or its conversations) and event triggers. Ids only, never content.
2. Delete those ids, and only those, while still unfinished (review LOW-2). A create that began
   after the read is still running, and its own completion or undo settles it; one that finished
   meanwhile is a persona, and is kept. Under READ COMMITTED a delete by predicate alone would
   take a row the read never saw, with no erasure for it.
3. Open an erasure for each persona the delete really removed. One that already has an open
   erasure keeps it (the ledger allows one), and an id that is not minted
   (``persona_<32 hex>``) is skipped, as in 063: the erasure refuses any other.

Revision ID: 069_persona_create_complete
Revises: 068_forgotten_conversations
Create Date: 2026-11-17
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, Any

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

# The tables the capture reads, as they stand at 069: only the columns it needs.
_personas = sa.table(_TABLE, sa.column("id"), sa.column("owner_id"), sa.column(_COLUMN, sa.Boolean))
_conversations = sa.table(
    "conversations", sa.column("id"), sa.column("owner_id"), sa.column("persona_id")
)
_tasks = sa.table(
    "tasks",
    sa.column("id"),
    sa.column("owner_id"),
    sa.column("persona_id"),
    sa.column("schedule_id"),
    sa.column("run_ids", sa.JSON),
)
_runs = sa.table("runs", sa.column("id"), sa.column("owner_id"), sa.column("persona_id"))
_schedules = sa.table(
    "schedules", sa.column("id"), sa.column("owner_id"), sa.column("payload_template", sa.JSON)
)
_event_triggers = sa.table(
    "event_triggers", sa.column("id"), sa.column("owner_id"), sa.column("persona_id")
)

#: An unfinished create: its owner, and the ids its erasure needs.
Create = tuple[str, dict[str, list[str]]]


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


def _ids(conn: Connection, stmt: Any) -> list[str]:  # noqa: ANN401 - a select
    return sorted({str(value) for (value,) in conn.execute(stmt) if value is not None})


def _names(value: Any, ids: frozenset[str]) -> bool:  # noqa: ANN401 - stored JSON
    """Whether a stored JSON value names any of ``ids`` anywhere inside it."""
    if isinstance(value, str):
        return value in ids
    if isinstance(value, Mapping):
        return any(_names(v, ids) for v in value.values())
    if isinstance(value, list | tuple):
        return any(_names(v, ids) for v in value)
    return False


def _owned(table: Any, owner_id: str, persona_id: str) -> Any:  # noqa: ANN401 - sa objects
    return sa.and_(table.c.persona_id == persona_id, table.c.owner_id == owner_id)


def capture(conn: Connection, owner_id: str, persona_id: str) -> dict[str, list[str]]:
    """What ``capture_persona`` captures for this persona, read the same way."""
    conversations = _ids(
        conn, sa.select(_conversations.c.id).where(_owned(_conversations, owner_id, persona_id))
    )
    task_rows = conn.execute(
        sa.select(_tasks.c.id, _tasks.c.schedule_id, _tasks.c.run_ids).where(
            _owned(_tasks, owner_id, persona_id)
        )
    ).all()
    tasks = sorted(str(t.id) for t in task_rows)
    runs = _ids(conn, sa.select(_runs.c.id).where(_owned(_runs, owner_id, persona_id)))
    runs = sorted({*runs, *(str(r) for t in task_rows for r in (t.run_ids or []))})
    named = frozenset({persona_id, *conversations, *tasks})
    schedules = {str(t.schedule_id) for t in task_rows if t.schedule_id is not None}
    for schedule_id, template in conn.execute(
        sa.select(_schedules.c.id, _schedules.c.payload_template).where(
            _schedules.c.owner_id == owner_id
        )
    ):
        if schedule_id == f"initsched:{persona_id}" or _names(template, named):
            schedules.add(str(schedule_id))
    triggers = _ids(
        conn,
        sa.select(_event_triggers.c.id).where(_owned(_event_triggers, owner_id, persona_id)),
    )
    return {
        "conversations": conversations,
        "tasks": tasks,
        "runs": runs,
        "schedules": sorted(schedules),
        "event_triggers": triggers,
    }


def unfinished_creates(conn: Connection) -> dict[str, Create]:
    """Every unfinished create, with what its erasure needs (step 1)."""
    rows = conn.execute(
        sa.select(_personas.c.id, _personas.c.owner_id).where(_personas.c[_COLUMN].is_(False))
    ).all()
    return {str(pid): (str(owner), capture(conn, str(owner), str(pid))) for pid, owner in rows}


def delete_creates(conn: Connection, persona_ids: Collection[str]) -> list[str]:
    """Delete these creates, and only these, while still unfinished (step 2); the ids deleted."""
    if not persona_ids:
        return []
    deleted = conn.execute(
        sa.delete(_personas)
        .where(_personas.c.id.in_(sorted(persona_ids)), _personas.c[_COLUMN].is_(False))
        .returning(_personas.c.id)
    )
    return sorted(str(pid) for (pid,) in deleted)


def open_erasures(conn: Connection, creates: Mapping[str, Create]) -> int:
    """Open a persona erasure for each of these deleted creates (step 3); how many."""
    opened = 0
    for persona_id, (owner_id, captured) in sorted(creates.items()):
        if not _MINTED.fullmatch(persona_id):
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
                owner_id=owner_id,
                subject_kind="persona",
                subject_id=persona_id,
                captured=captured,
            )
        )
        opened += 1
    return opened


def erase_unfinished_creates(conn: Connection) -> list[str]:
    """Steps 1 to 3: the unfinished creates deleted, each with an erasure open for the sweep."""
    creates = unfinished_creates(conn)
    deleted = delete_creates(conn, creates)
    open_erasures(conn, {pid: creates[pid] for pid in deleted})
    return deleted


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    if _has_column():
        erase_unfinished_creates(op.get_bind())
        op.drop_column(_TABLE, _COLUMN)
    op.execute("RESET lock_timeout")
