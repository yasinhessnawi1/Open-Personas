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
  provenance ``interaction_id`` equals the conversation id), A7's original layer-(a)
  rule, OR the durable disclosure fact holds it (R9-253). The fact is written whenever
  synthesis or a direct write sees a disclosure and no node or conversation deletion
  clears it, so a user forgetting the tagged node does not un-gate the untagged nodes
  learned beside it (D-ORCH-11-18-m). Both halves count; the node half keeps every
  interaction a tagged node cites gated even where no fact was ever written.
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
- **released** (D-ORCH-11-18-u): the user said a disclosed conversation may be drawn on
  again. Honoured ONLY when the caller asks (``honour_releases``), which the readers of
  what a conversation TAUGHT do (a node's sources, a lens target's, the judge's context
  notes). The conversation as its own citation (its raw transcript holds the disclosure)
  and every task path stay strict, so the default is strict. A release lifts both halves
  of ``disclosed`` for its conversation while it holds: no disclosure recorded since (the
  fact's count), no tagged node citing it with a write made since, and nothing in it
  synthesis has not read. Tagged nodes themselves are never touched: every reader drops
  them by their own tag (K4's floor).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from sqlalchemy import func, select

from persona_api.db.engine import rls_connection
from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import messages as messages_t
from persona_api.db.models import synthesis_markers
from persona_api.db.models import tasks as tasks_t
from persona_api.initiative.disclosures import PgDisclosureStore

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime

    from persona.graph.models import ConceptNode
    from persona.graph.protocol import GraphStore
    from sqlalchemy import Engine

    from persona_api.initiative.disclosures import DisclosureState

__all__ = [
    "SYNTHESISED_ROLES",
    "ConversationWellbeingGate",
    "DisclosureReader",
    "ReleaseReader",
    "node_sources",
]

#: The message roles K2 synthesis reads as the transcript (its ``_TRANSCRIPT_ROLES``,
#: pinned equal by a test): the count ``synthesised_up_to`` is compared against.
SYNTHESISED_ROLES: tuple[str, ...] = ("user", "assistant")


class DisclosureReader(Protocol):
    """The read half of the durable disclosure fact (``PgDisclosureStore`` in production)."""

    def disclosed(self, owner_id: str, refs: Iterable[str]) -> set[str]:
        """The refs among ``refs`` that ever carried a disclosure for this owner."""
        ...


class ReleaseReader(Protocol):
    """The fact counters a release is judged by (``PgDisclosureStore`` in production)."""

    def states(self, owner_id: str, refs: Iterable[str]) -> Mapping[str, DisclosureState]:
        """Each fact among ``refs``: its disclosure count and its release, if any."""
        ...


def node_sources(node: ConceptNode | None) -> set[str]:
    """The interactions a node was learned from (its provenance ``interaction_id`` s)."""
    if node is None:
        return set()
    return {p.interaction_id for p in node.provenance if p.interaction_id is not None}


class ConversationWellbeingGate:
    """Which conversations are wellbeing-gated for unprompted initiative."""

    def __init__(
        self,
        store: GraphStore,
        engine: Engine | None = None,
        *,
        disclosures: DisclosureReader | None = None,
        releases: ReleaseReader | None = None,
    ) -> None:
        """Bind the graph store (the flagged set), the engine and the disclosure facts.

        ``disclosures`` and ``releases`` default to the fact table over ``engine``; without
        it, only the flagged-node half of ``disclosed`` runs and no release is honoured (a
        unit-test shape, never production's).
        """
        self._store = store
        self._engine = engine
        self._disclosures = disclosures
        self._releases = releases
        if engine is not None and (disclosures is None or releases is None):
            table = PgDisclosureStore(engine)
            self._disclosures = table if disclosures is None else disclosures
            self._releases = table if releases is None else releases

    def disclosed(
        self,
        owner_id: str,
        refs: Iterable[str],
        *,
        flagged: Sequence[ConceptNode] | None = None,
        honour_releases: bool = False,
    ) -> set[str]:
        """The refs a wellbeing-flagged node was sourced from, or a disclosure fact holds.

        With ``honour_releases``, less the refs whose release holds (see the module).
        """
        wanted = set(refs)
        nodes = self._store.flagged_nodes(owner_id) if flagged is None else flagged
        found = {
            p.interaction_id
            for node in nodes
            for p in node.provenance
            if p.interaction_id is not None and p.interaction_id in wanted
        }
        if self._disclosures is not None and wanted:
            found |= self._disclosures.disclosed(owner_id, wanted)
        if honour_releases and found:
            found -= self._exempt(owner_id, found, nodes)
        return found

    def released(
        self,
        owner_id: str,
        refs: Iterable[str],
        *,
        flagged: Sequence[ConceptNode] | None = None,
    ) -> dict[str, datetime]:
        """The refs whose release is in force, with when it was given.

        In force: it covers every disclosure recorded for the ref, and no tagged node cites
        the ref with a write made after it (a tagging path that skipped the fact).
        """
        wanted = set(refs)
        if self._releases is None or not wanted:
            return {}
        in_force = {
            ref: state.released_at
            for ref, state in self._releases.states(owner_id, wanted).items()
            if state.release_in_force and state.released_at is not None and ref in wanted
        }
        if not in_force:
            return {}
        nodes = self._store.flagged_nodes(owner_id) if flagged is None else flagged
        for node in nodes:
            for p in node.provenance:
                ref = p.interaction_id
                if ref is not None and ref in in_force and p.written_at > in_force[ref]:
                    del in_force[ref]
        return in_force

    def _exempt(self, owner_id: str, refs: set[str], nodes: Sequence[ConceptNode]) -> set[str]:
        """The released refs synthesis has fully read (no engine: none, fail closed)."""
        released = set(self.released(owner_id, refs, flagged=nodes))
        if not released or self._engine is None:
            return set()
        return released - self.unsynthesised(owner_id, released)

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
        honour_releases: bool = False,
    ) -> set[str]:
        """The refs that are disclosed, plus (when asked) the unsynthesised ones.

        ``honour_releases`` only for refs judged as what a node was learned from; never for a
        conversation cited as itself, nor for a task's origins (D-ORCH-11-18-u).
        """
        wanted = set(refs)
        if not wanted:
            return set()
        gated = self.disclosed(owner_id, wanted, flagged=flagged, honour_releases=honour_releases)
        if include_unsynthesised:
            gated |= self.unsynthesised(owner_id, wanted)
        return gated
