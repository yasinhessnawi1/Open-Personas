"""Forgetting a conversation's memory takes the facts learned from it out of the graph (R9-304).

Synthesis stamps every fact it learns with the conversation it came from (provenance
``interaction_id``, kind ``conversation``). Deleting a conversation with ``forget_memory`` used
to remove its episodic memory but leave those facts. Now the delete also takes the conversation
out of the graph, by the persona erasure's rules with the conversation as the source
(:func:`persona_api.services.erasure_graph.take_source_out`):

- a fact only this conversation supports is deleted, with its edges, versions and entity links;
  consolidation's bookkeeping is nobody's support. A duplicate merged into it that another
  source supports is unmerged first and stays; one merged into a fact that stays gives back its
  mirrors, entity links and name (R9-287's give-back, the same functions the Memory page's
  forget uses);
- a fact another conversation (or the user, or any other source) also supports loses only this
  conversation's entries; the SELF node keeps dated stubs. Its wording stays as it is, so it
  may still include words this conversation added (merging appends them, unattributed; R9-311);
- version snapshots get the same treatment, an edge this conversation asserted goes with its
  mirrors, and an entity it established goes when nothing links to it, else loses the citation;
- an episodic consolidation entry whose window lists this conversation (``conversation_ids``)
  is this conversation's too (R9-325): its gist is removed when any of the window's chunks is
  forgotten, and the other conversations' chunks are gisted again on the next pass. A
  consolidation entry written before R9-325 lists no conversations and is left as it is.

Why not the Memory page's per-fact delete, node by node: that delete takes every duplicate
merged into the fact with it (D-R9-287-forget-canonical, right when the user says "this fact is
wrong"), which here would delete a duplicate another conversation supports; and each node would
be its own transaction, so the forget could not be one unit with the row delete.

It is one unit. The step runs inside the transaction that deletes the conversation's row, before
the delete: the owner's graph maintenance lock first (nothing else held, so no deadlock cycle
passes through it; :mod:`persona.graph.maintenance_lock`), waiting at most
:data:`persona.graph.postgres.DELETE_LOCK_TIMEOUT` for a running consolidation; then the
conversation's row ``FOR UPDATE``, which waits for a graph write in flight that cites this
conversation (it holds the row ``FOR KEY SHARE``, the backend's guard) and makes every later
one wait and then find no row; then the graph. A failure rolls everything back and the
conversation stays, so the delete can be retried; a retry after success finds nothing.

Postgres only: the community edition has no graph.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from persona.graph.maintenance_lock import take_graph_maintenance_lock
from persona.graph.models import make_self_node_id
from persona.graph.postgres import DELETE_LOCK_TIMEOUT
from sqlalchemy import select, text

from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import graph_nodes as nodes_t
from persona_api.services.conversation_memory_forget import hold_owners
from persona_api.services.erasure_graph import (
    Source,
    _naming,
    plan_trails_from,
    take_source_out,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine
    from sqlalchemy.exc import OperationalError

__all__ = [
    "ConversationGraphPreview",
    "conversation_source",
    "forget_conversation_graph",
    "is_busy",
    "preview",
    "preview_conversation_graph",
    "preview_counts",
]

# Postgres' "lock not available" (the lock timeout ran out) and "deadlock detected": the forget
# met a consolidation or a writer and lost; trying again is the answer (as for a node delete).
_BUSY_SQLSTATES = frozenset({"55P03", "40P01"})


@dataclass(frozen=True)
class ConversationGraphPreview:
    """What forgetting a conversation's memory would do to the facts the user sees in Memory.

    ``facts_removed``: facts only this conversation supports. ``facts_kept``: facts another
    source also supports, which stay without this conversation as a source. Only its entries
    go: a kept fact's wording may still include words from this conversation, which merging
    appended and nothing attributes to it (R9-311). Merged duplicates (hidden behind their
    canonical) and the user's SELF node are not counted.
    """

    facts_removed: int
    facts_kept: int


def conversation_source(conversation_id: str) -> Source:
    """The :data:`~persona_api.services.erasure_graph.Source` of one conversation's facts."""
    return {"interaction_id": conversation_id}


def is_busy(exc: OperationalError) -> bool:
    """Whether a failed forget lost a lock wait (timeout or deadlock) and may simply be retried."""
    return getattr(exc.orig, "sqlstate", None) in _BUSY_SQLSTATES


def forget_conversation_graph(
    conn: Connection, *, owner_id: str, conversation_id: str
) -> dict[str, int]:
    """Take the conversation out of the owner's graph, in ``conn``'s transaction; the counts.

    Call it first in the transaction that deletes the conversation's row, before the delete
    (see the module docstring). ``{}`` off Postgres, and when the conversation is not the
    owner's (the delete then answers 404 and rolls back).
    """
    if conn.dialect.name != "postgresql":
        return {}
    # The shape is fixed in core: digits and a unit, nothing else.
    conn.execute(text(f"SET LOCAL lock_timeout = '{DELETE_LOCK_TIMEOUT}'"))
    take_graph_maintenance_lock(conn, owner_id)
    # R9-311: the owner's rows before the conversation's, the erasures' order (users, persona,
    # conversation), so a forget and a persona or account erasure cannot deadlock.
    hold_owners(conn, owner_id=owner_id, conversation_id=conversation_id)
    held = conn.execute(
        select(conversations_t.c.id)
        .where(conversations_t.c.owner_id == owner_id, conversations_t.c.id == conversation_id)
        .with_for_update()
    ).first()
    if held is None:
        return {}
    return take_source_out(conn, owner_id, conversation_source(conversation_id))


def preview_counts(
    rows: Iterable[tuple[str, list[dict[str, Any]], str | None]],
    *,
    conversation_id: str,
    self_node_id: str,
) -> ConversationGraphPreview:
    """Count ``(node id, trail, merged_into)`` rows the way the forget would treat them."""
    rows = list(rows)
    plan = plan_trails_from(
        ((node_id, node_id, trail) for node_id, trail, _ in rows),
        source=conversation_source(conversation_id),
        self_node_id=self_node_id,
    )
    shown = {node_id for node_id, _, merged in rows if merged is None and node_id != self_node_id}
    return ConversationGraphPreview(
        facts_removed=len(shown.intersection(plan.doomed)),
        facts_kept=len(shown.intersection(plan.rewrites)),
    )


def preview_conversation_graph(
    conn: Connection, *, owner_id: str, conversation_id: str
) -> ConversationGraphPreview:
    """:func:`preview_counts` over the owner's facts that cite the conversation. Reads only."""
    found = conn.execute(
        select(nodes_t.c.id, nodes_t.c.provenance, nodes_t.c.merged_into).where(
            nodes_t.c.owner_id == owner_id,
            _naming(nodes_t.c.provenance, conversation_source(conversation_id)),
        )
    ).all()
    return preview_counts(
        ((str(r.id), _trail(r.provenance), r.merged_into) for r in found),
        conversation_id=conversation_id,
        self_node_id=make_self_node_id(owner_id),
    )


def preview(
    engine: Engine | None, *, owner_id: str, conversation_id: str
) -> ConversationGraphPreview:
    """The delete dialog's count: zeros where there is no graph (no engine, or not Postgres)."""
    if engine is None or engine.dialect.name != "postgresql":
        return ConversationGraphPreview(facts_removed=0, facts_kept=0)
    with engine.connect() as conn:
        return preview_conversation_graph(conn, owner_id=owner_id, conversation_id=conversation_id)


def _trail(raw: object) -> list[dict[str, Any]]:
    """A stored trail as a list of entries (anything else reads as empty)."""
    if not isinstance(raw, list):
        return []
    return [dict(entry) for entry in raw if isinstance(entry, Mapping)]
