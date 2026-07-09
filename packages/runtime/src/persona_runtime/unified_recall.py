"""Unified recall composition — the K9 path projected into the prompt seam (Spec K9, T8/T9).

Binds K9's :func:`~persona.recall.pipeline.compose_recall` (fuse → rerank → score → contiguity
→ diffusion → K4-then-abstention) to the real stores and projects its result **back into the
existing ``RetrievedContext`` seam** (K9-D-11 — no forked prompt shape): surfaced episodic
candidates become band-resolved episodic chunks, surfaced graph candidates become
``GraphKnowledgeItem``s (the single-sourced :func:`~persona_runtime.graph_selection.project_node`
projection), and the reinforcement ids ride ``episodic_recalled_ids`` so the loop's existing
``reinforce_recalled`` reinforces the right set (surfaced raw hits + contiguity members).

Two properties are structural, not hoped:

- **Fail-soft** (K9-D-3/D-4): the reranker is the fail-soft shell (:func:`build_reranker`), and a
  whole-path failure degrades the turn to **memoryless** (empty projection) — never turn-fatal,
  mirroring the K3/V13 posture. The reranker is injected, so a stub (P7 absent) yields the fused
  order — the D-12 skeleton property holds end-to-end on real stores.
- **Abstention → honest absence** (K9-D-9): when the gate abstains, the projection is empty, so no
  memory reaches the prompt and nothing is fabricated. K4 subtraction is applied inside
  ``compose_recall`` **before** abstention evaluates the remainder — this composition never
  reorders that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.recall.pipeline import compose_recall

from persona_runtime.graph_selection import project_node
from persona_runtime.prompt import GraphContext

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from persona.graph.fusion import HybridResult
    from persona.recall.config import RecallSettings
    from persona.recall.contiguity import EpisodeProvider
    from persona.recall.diffusion import NeighbourProvider
    from persona.recall.rerank import Reranker
    from persona.schema.chunks import PersonaChunk
    from persona.stores.lifecycle import EpisodicSettings

    from persona_runtime.graph_selection import GatingContext

__all__ = ["UnifiedProjection", "make_unified_recall"]

_LOG = "runtime.unified_recall"


@dataclass(frozen=True)
class UnifiedProjection:
    """The K9 result projected into the existing prompt fields (K9-D-11).

    Attributes:
        episodic: The band-resolved episodic chunks to display (empty on abstention).
        episodic_recalled_ids: The raw ids to reinforce (surfaced raw hits + contiguity
            members) — fed to the loop's ``reinforce_recalled`` via ``RetrievedContext``.
        graph: The graph-knowledge bundle (empty on abstention).
        abstained: ``True`` when the gate abstained — the honest-absence turn (memoryless).
    """

    episodic: list[PersonaChunk] = field(default_factory=list)
    episodic_recalled_ids: tuple[str, ...] = ()
    graph: GraphContext = field(default_factory=GraphContext)
    abstained: bool = False


def make_unified_recall(
    *,
    reranker: Reranker,
    settings: RecallSettings,
    episodic_settings: EpisodicSettings,
    persona_id: str,
    owner_provider: Callable[[], str | None],
    episodic_query: Callable[[str, int], Sequence[PersonaChunk]],
    resolve_display: Callable[[Sequence[PersonaChunk]], list[PersonaChunk]],
    gist_query: Callable[[str, int], Sequence[PersonaChunk]] | None = None,
    graph_retrieve: Callable[[str], Sequence[HybridResult]] | None = None,
    episode_provider: EpisodeProvider | None = None,
    neighbour_provider: NeighbourProvider | None = None,
    allowlist_provider: Callable[[GatingContext], set[str] | None] | None = None,
    recent_window_provider: Callable[[], Sequence[str]] | None = None,
    rerank_top_k: int | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Callable[[str], UnifiedProjection]:
    """Build the per-turn ``query -> UnifiedProjection`` for the chat/voice loop (K9, T8/T9).

    Composes the K9 recall path over the injected store surfaces (all owner/persona-scoped by the
    caller) and projects it into the prompt seam. The K4 ``allowlist_provider`` + ``recent_window``
    are the same primitives the K3/V13 graph path uses — the gate is not reinvented here. A gated
    turn's subtraction happens inside ``compose_recall`` before abstention (K9-D-9).

    Args:
        reranker: The path's fail-soft reranker (chat sync / voice off-loop shell).
        settings: The K9 recall tunables.
        episodic_settings: K8's lifecycle settings (the recency τ₀).
        persona_id: The episodic scope (fixed for the loop).
        owner_provider: Resolves the graph owner scope per turn; empty ⇒ no graph read.
        episodic_query: ``(query, k) -> raw chunks`` (the episodic store bound).
        resolve_display: Band-resolves the surfaced episodic chunks for display (K8-D-11).
        gist_query: ``(query, k) -> gist chunks``; ``None`` ⇒ no gist leg.
        graph_retrieve: ``query -> [HybridResult]``; ``None`` ⇒ no graph legs.
        episode_provider: Pyramid episode resolver for contiguity; ``None`` ⇒ no expansion.
        neighbour_provider: Graph neighbour resolver for diffusion; ``None`` ⇒ no tiebreak.
        allowlist_provider: The K4 gating policy (``GatingContext`` ⇒ permitted graph-node set).
        recent_window_provider: The recent-conversation window for the gate's "still in the topic"
            signal (the per-turn ContextVar source).
        rerank_top_k: The fused top-k to rerank (chat/voice split); ``None`` ⇒ result budget.
        now: The turn clock (injected for testable recency/scoring).

    Returns:
        A ``query -> UnifiedProjection`` callable to hand ``retrieve_context``. Fail-soft: any
        error degrades to an empty (memoryless) projection.
    """
    from persona.recall.sources import gather_legs

    from persona_runtime.graph_selection import GatingContext

    def retrieve(query: str) -> UnifiedProjection:
        owner_id = owner_provider()
        allowlist: set[str] | None = None
        if allowlist_provider is not None and owner_id:
            window = tuple(recent_window_provider()) if recent_window_provider is not None else ()
            allowlist = allowlist_provider(
                GatingContext(owner_id=owner_id, query=query, recent_messages=window)
            )
        graph_leg = graph_retrieve if owner_id else None  # no owner ⇒ no graph read (fail closed)
        legs = gather_legs(
            query=query,
            top_k=settings.pool_size,
            episodic_query=episodic_query,
            gist_query=gist_query,
            graph_retrieve=graph_leg,
        )
        result = compose_recall(
            legs=legs,
            query=query,
            reranker=reranker,
            settings=settings,
            episodic_settings=episodic_settings,
            now=now(),
            owner_id=owner_id,
            persona_id=persona_id,
            rerank_top_k=rerank_top_k,
            episode_provider=episode_provider,
            neighbour_provider=neighbour_provider,
            allowlist=allowlist,
        )
        return _project(result, resolve_display=resolve_display, now=now())

    def safe_retrieve(query: str) -> UnifiedProjection:
        # Fail-soft (K9-D-3/D-4, the K3/V13 posture): memoryless beats turn-fatal.
        try:
            return retrieve(query)
        except Exception:  # noqa: BLE001
            from persona.logging import get_logger

            get_logger(_LOG).opt(exception=True).warning(
                "unified recall failed; turn degrades to memoryless"
            )
            return UnifiedProjection()

    return safe_retrieve


def _project(
    result: object,  # RecallResult — imported lazily to keep this module import-light
    *,
    resolve_display: Callable[[Sequence[PersonaChunk]], list[PersonaChunk]],
    now: datetime,
) -> UnifiedProjection:
    from persona.recall.models import RecallSource
    from persona.recall.pipeline import RecallResult

    assert isinstance(result, RecallResult)  # noqa: S101 — the compose_recall return contract
    if result.abstained:
        return UnifiedProjection(abstained=True)
    ep_chunks = [c.chunk for c in result.surfaced if c.chunk is not None]
    displayed = resolve_display(ep_chunks) if ep_chunks else []
    graph_items = tuple(
        project_node(c.node, relevance=c.relevance, now=now)
        for c in result.surfaced
        if c.source is RecallSource.GRAPH and c.node is not None
    )
    return UnifiedProjection(
        episodic=displayed,
        episodic_recalled_ids=result.reinforce_ids,
        graph=GraphContext(items=graph_items),
    )
