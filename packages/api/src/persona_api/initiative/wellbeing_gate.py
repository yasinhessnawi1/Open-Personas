"""One definition of a conversation the persona must not raise unprompted (R9-249).

Wellbeing content is tagged on graph NODES (K2 writes ``wellbeing_category`` at
synthesis) and a node's provenance names the interaction it was sourced from. A
conversation carries no tag of its own. This module is the ONE place a conversation
(or any interaction a node names as its source) is judged; A5's scan readers (nodes,
lens targets, tasks), A5's pipeline subject rule and A7's event door all call it, so
they cannot drift. An item is gated when its SOURCE is: a node learned from a gated
conversation is gated even when the node itself carries no tag (the dinner-party
node synthesis extracted beside the disclosure it tagged).

- **disclosed**: a wellbeing-flagged node was sourced from the conversation (its
  provenance ``interaction_id`` equals the conversation id). This is A7's original
  layer-(a) rule, moved here unchanged.
- **unsynthesised**: K2 synthesis has not caught up with the conversation, so a
  disclosure in it cannot have been tagged yet. Synthesis runs off the critical path
  at every completed turn (and at run and voice end) and records its progress in
  ``synthesis_markers.synthesised_up_to``, the count of transcript messages it has
  processed; a conversation is unsynthesised while it holds more transcript messages
  than that. A missing marker counts as 0, so a conversation with transcript
  messages and no marker is unsynthesised, and one with NO transcript messages is
  not (synthesis has nothing to do there either). The count is synthesis's own:
  the same roles, and superseded rows counted, as synthesis's read counts them
  (both pinned by tests). Precision first: the persona waits for synthesis before
  raising anything from it unprompted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from persona_api.db.engine import rls_connection
from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import messages as messages_t
from persona_api.db.models import synthesis_markers
from persona_api.db.models import tasks as tasks_t

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from persona.graph.models import ConceptNode
    from persona.graph.protocol import GraphStore
    from sqlalchemy import Engine

__all__ = ["SYNTHESISED_ROLES", "ConversationWellbeingGate", "node_sources"]

#: The message roles K2 synthesis reads as the transcript (its ``_TRANSCRIPT_ROLES``,
#: pinned equal by a test): the count ``synthesised_up_to`` is compared against.
SYNTHESISED_ROLES: tuple[str, ...] = ("user", "assistant")


def node_sources(node: ConceptNode | None) -> set[str]:
    """The interactions a node was learned from (its provenance ``interaction_id`` s)."""
    if node is None:
        return set()
    return {p.interaction_id for p in node.provenance if p.interaction_id is not None}


class ConversationWellbeingGate:
    """Which conversations are wellbeing-gated for unprompted initiative."""

    def __init__(self, store: GraphStore, engine: Engine | None = None) -> None:
        """Bind the graph store (the flagged set) and, for the freshness half, the engine."""
        self._store = store
        self._engine = engine

    def disclosed(
        self,
        owner_id: str,
        refs: Iterable[str],
        *,
        flagged: Sequence[ConceptNode] | None = None,
    ) -> set[str]:
        """The refs some wellbeing-flagged node was sourced from (kind-agnostic)."""
        wanted = set(refs)
        nodes = self._store.flagged_nodes(owner_id) if flagged is None else flagged
        return {
            p.interaction_id
            for node in nodes
            for p in node.provenance
            if p.interaction_id is not None and p.interaction_id in wanted
        }

    def unsynthesised(self, owner_id: str, conversation_ids: Iterable[str]) -> set[str]:
        """The conversations holding transcript messages K2 synthesis has not processed."""
        ids = sorted(set(conversation_ids))
        if not ids:
            return set()
        if self._engine is None:
            msg = "the synthesis-freshness gate needs a database engine"
            raise ValueError(msg)
        transcript = (
            select(func.count())
            .select_from(messages_t)
            .where(
                messages_t.c.conversation_id == conversations_t.c.id,
                messages_t.c.role.in_(SYNTHESISED_ROLES),
            )
            .scalar_subquery()
        )
        processed = (
            select(synthesis_markers.c.synthesised_up_to)
            .where(
                synthesis_markers.c.owner_id == owner_id,
                synthesis_markers.c.interaction_kind == "conversation",
                synthesis_markers.c.interaction_id == conversations_t.c.id,
            )
            .scalar_subquery()
        )
        stmt = select(conversations_t.c.id).where(
            conversations_t.c.id.in_(ids), transcript > func.coalesce(processed, 0)
        )
        with rls_connection(self._engine, owner_id) as conn:
            return {str(row[0]) for row in conn.execute(stmt).all()}

    def task_origins(self, owner_id: str, task_ids: Iterable[str]) -> dict[str, set[str]]:
        """Each task's recorded origins: its originating conversation and its runs."""
        ids = sorted(set(task_ids))
        if not ids:
            return {}
        if self._engine is None:
            msg = "the task-origin read needs a database engine"
            raise ValueError(msg)
        stmt = select(tasks_t.c.id, tasks_t.c.conversation_id, tasks_t.c.run_ids).where(
            tasks_t.c.id.in_(ids)
        )
        with rls_connection(self._engine, owner_id) as conn:
            rows = conn.execute(stmt).all()
        return {str(r[0]): {str(x) for x in [r[1], *(r[2] or [])] if x is not None} for r in rows}

    def gated(
        self,
        owner_id: str,
        refs: Iterable[str],
        *,
        include_unsynthesised: bool,
        flagged: Sequence[ConceptNode] | None = None,
    ) -> set[str]:
        """The refs that are disclosed, plus (when asked) the unsynthesised ones."""
        wanted = set(refs)
        if not wanted:
            return set()
        gated = self.disclosed(owner_id, wanted, flagged=flagged)
        if include_unsynthesised:
            gated |= self.unsynthesised(owner_id, wanted)
        return gated
