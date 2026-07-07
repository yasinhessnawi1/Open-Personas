"""Store adapters for the K9 recall providers (Spec K9, T8/T9) — chat + voice share these.

The K9 pipeline consumes two structural providers behind Protocols so its core stays pure and
testable; the composition binds them to the real K8 pyramid and K7 graph here (one place, both
turn paths):

- :class:`PyramidEpisodeProvider` — resolves a hit's surrounding **episode** (the gist's ordered
  ``member_ids`` + the seed's position) via ``pyramid.covering_gists`` + ``pyramid.drill``, for
  temporal-contiguity expansion (K9-D-6). No covering gist ⇒ ``None`` (fail-soft, no expansion).
- :class:`GraphNeighbourProvider` — one-hop graph neighbours for the diffusion tiebreak (K9-D-8),
  over the owner-scoped ``store.neighbors``; no owner ⇒ empty (fail-closed, no diffusion).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.recall.contiguity import Episode
from persona.recall.models import RecallSource

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.graph.protocol import GraphStore
    from persona.recall.models import RecallCandidate
    from persona.stores.pyramid import EpisodicPyramid

__all__ = ["GraphNeighbourProvider", "PyramidEpisodeProvider"]


class PyramidEpisodeProvider:
    """Resolve a candidate's surrounding episode from the K8 pyramid (K9-D-6/D-7)."""

    def __init__(self, pyramid: EpisodicPyramid, persona_id: str) -> None:
        self._pyramid = pyramid
        self._persona_id = persona_id

    def episode(self, candidate: RecallCandidate) -> Episode | None:
        """The gist members (ordered) + the seed's index, or ``None`` (no covering gist)."""
        chunk = candidate.chunk
        if chunk is None:
            return None
        if candidate.source is RecallSource.EPISODIC_GIST:
            # A gist hit IS the episode — drill its members; the whole episode is the neighbourhood.
            members = self._pyramid.drill(self._persona_id, chunk.id)
            return Episode(members=tuple(members), seed_index=None) if members else None
        covering = self._pyramid.covering_gists(self._persona_id, [chunk.id])
        gist = covering.get(chunk.id)
        if gist is None:
            return None  # uncovered chunk (progressive backfill) — no expansion, fail-soft
        members = self._pyramid.drill(self._persona_id, gist.id)
        member_ids = [m.id for m in members]
        if chunk.id not in member_ids:
            return None
        return Episode(members=tuple(members), seed_index=member_ids.index(chunk.id))


class GraphNeighbourProvider:
    """One-hop graph neighbours of a node for the diffusion tiebreak (K9-D-8)."""

    def __init__(
        self, store: GraphStore, owner_provider: Callable[[], str | None], *, limit: int = 8
    ) -> None:
        self._store = store
        self._owner_provider = owner_provider
        self._limit = limit

    def neighbours(self, node_id: str) -> list[str]:
        """The one-hop neighbour node ids, owner-scoped; empty when no owner (fail-closed)."""
        owner = self._owner_provider()
        if not owner:
            return []
        return [
            node.id
            for _edge, node in self._store.neighbors(
                owner, node_id, link_types=None, limit=self._limit
            )
        ]
