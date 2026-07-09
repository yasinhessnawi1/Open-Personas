"""Owner-scoped nav-badge counts — one cheap read for the sidebar (R9-010).

The web sidebar shows a live count on each nav row (Personas / Conversations /
Calls / Activity / Memory / Schedule). Before this service the personas and
conversations badges were derived from the sidebar's *truncated preview lists*
(4 personas / 30 conversations) — capped, dishonest totals — and the other rows
had no counts at all. This module answers all of them in ONE round-trip of
index-friendly ``COUNT`` queries (every table carries an ``owner_id`` index).

Scoping is belt-and-braces: every count carries an explicit ``owner_id``
predicate (correct on the community SQLite store, which has no RLS) AND runs on
the caller's RLS engine (cloud: the policy predicate is enforced even if a
future edit dropped the explicit filter).

Semantics (the honest choices, pinned):

- ``conversations`` counts chat-born threads only (``origin != 'call'``) — call
  transcripts belong to the Calls surface, and the count captions the sidebar's
  "All chats" affordance.
- ``active_tasks`` is the active working set: the complement of
  :data:`persona.tasks.state.TERMINAL_STATES` (``defined``/``active``/
  ``waiting``), derived from the enum so a lifecycle change can't silently
  desynchronise the badge. Never completed/failed/cancelled history.
- ``schedules`` counts schedule ROWS — a recurring schedule counts once, never
  its occurrences or fires.
- ``memory_nodes`` counts canonical graph nodes (``merged_into IS NULL``,
  matching what the Memory surface renders — K7-D-4 soft-merged nodes are
  invisible). The ``graph_nodes`` table is cloud-only (Spec 33), so the caller
  gates it with ``include_memory`` (graph store wired?); ungated it would raise
  on the community store where the table does not exist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.tasks.state import TERMINAL_STATES, TaskState
from sqlalchemy import func, select

from persona_api.db.models import calls as calls_t
from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import graph_nodes as graph_nodes_t
from persona_api.db.models import personas as personas_t
from persona_api.db.models import schedules as schedules_t
from persona_api.db.models import tasks as tasks_t

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = ["ACTIVE_TASK_STATES", "get_nav_counts"]

#: The non-terminal task states — the "active working set" the Activity badge
#: shows. Derived from the enum (never a hand-typed list) so the badge and the
#: lifecycle can't drift apart.
ACTIVE_TASK_STATES: tuple[str, ...] = tuple(
    sorted(s.value for s in TaskState if s not in TERMINAL_STATES)
)


def get_nav_counts(rls_engine: Engine, *, owner_id: str, include_memory: bool) -> dict[str, int]:
    """All six owner-scoped nav counts in one round-trip (see module docstring).

    Read-only (CQS). ``include_memory=False`` (no graph store wired — community /
    graph off) skips the cloud-only ``graph_nodes`` table and reports ``0``; the
    Memory nav row is hidden in that deployment anyway.
    """
    columns = [
        select(func.count())
        .select_from(personas_t)
        .where(personas_t.c.owner_id == owner_id)
        .scalar_subquery()
        .label("personas"),
        select(func.count())
        .select_from(conversations_t)
        .where(
            conversations_t.c.owner_id == owner_id,
            conversations_t.c.origin != "call",
        )
        .scalar_subquery()
        .label("conversations"),
        select(func.count())
        .select_from(calls_t)
        .where(calls_t.c.owner_id == owner_id)
        .scalar_subquery()
        .label("calls"),
        select(func.count())
        .select_from(tasks_t)
        .where(
            tasks_t.c.owner_id == owner_id,
            tasks_t.c.state.in_(ACTIVE_TASK_STATES),
        )
        .scalar_subquery()
        .label("active_tasks"),
        select(func.count())
        .select_from(schedules_t)
        .where(schedules_t.c.owner_id == owner_id)
        .scalar_subquery()
        .label("schedules"),
    ]
    if include_memory:
        columns.append(
            select(func.count())
            .select_from(graph_nodes_t)
            .where(
                graph_nodes_t.c.owner_id == owner_id,
                graph_nodes_t.c.merged_into.is_(None),
            )
            .scalar_subquery()
            .label("memory_nodes")
        )
    with rls_engine.connect() as conn:
        row = conn.execute(select(*columns)).mappings().one()
    counts = {key: int(value) for key, value in row.items()}
    counts.setdefault("memory_nodes", 0)
    return counts
