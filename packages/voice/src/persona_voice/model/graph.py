"""The K4-gated graph-retrieval composition for a voice turn (Spec V13, V13-D-1/D-5).

Part A of V13 composes onto the voice runner the graph-knowledge retrieval that
chat already runs — **mirroring chat's ``RuntimeFactory._build_graph_retrieval``
exactly** (``runtime_factory.py``), never a voice-local gate variant. That
identity IS the safety property: the standing rule (K4's recorded BLOCKING
constraint) is that voice graph surfacing routes through K4's gate or it is a
safety regression. All four gate elements are present, from the same runtime
primitives chat uses:

1. **Allowlist subtraction** — ``make_allowlist_provider`` withholds a
   gate-eligible wellbeing node the caller has not opened this turn.
2. **Recent-window lift** — ``get_recent_window`` (the per-turn ContextVar the
   reply producer publishes before the off-thread query) re-admits it when the
   conversation opens the topic, not just the bare query.
3. **Surfacing** — the K4 per-category spoken-care text (``surfacing_guidance``)
   for the K3 prompt slot, recency-weighted.
4. **Recency** — ``recency_bucket`` → ``recency_band`` bands each flagged node.

Two things differ from chat, both non-gate adaptations (D-V13-1):

* **Owner provider is the fixed caller id**, not chat's request ContextVar. The
  voice runner has no per-request middleware; its session RLS engine is already
  scoped to the one caller (``make_session_rls_engine(..., user_id=...)``), so a
  fixed ``lambda: owner_id`` is the correct owner scope — and it fails closed the
  same way (empty owner ⇒ empty ``GraphContext``).
* **The voice profile** — ``voice_graph_settings`` (traversal OFF, a raised
  relevance floor) + ``VOICE_NODE_BUDGET`` node cap (K3-D-6) — the tighter slice
  the latency-bound path wants, from the same mechanism chat's profile uses.

The graph query itself runs off the event loop, overlapped with pre-model work
and taken only if ready (``persona_runtime.graph_voice`` — the dormant shell this
composition finally fills); this module only *builds* the callable + the
surfacing provider the runner hands to :class:`VoiceTurnContext`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.graph.config import GraphSettings
from persona.graph.retrieval import HybridRetriever
from persona.wellbeing_policy import is_gate_eligible, parse_category
from persona_runtime.graph_selection import make_graph_retrieval, recency_bucket
from persona_runtime.graph_voice import VOICE_NODE_BUDGET, voice_graph_settings
from persona_runtime.graph_window import get_recent_window
from persona_runtime.wellbeing import (
    FlaggedNode,
    make_allowlist_provider,
    recency_band,
    surfacing_guidance,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.graph.protocol import GraphStore
    from persona.stores.episodic import EpisodicStore
    from persona_runtime.graph_selection import GatingContext
    from persona_runtime.prompt import GraphContext, GraphRecency
    from persona_runtime.unified_recall import UnifiedProjection

__all__ = [
    "VoiceGraphComposition",
    "VoiceUnifiedComposition",
    "build_voice_graph_retrieval",
    "build_voice_unified_recall",
]


def _voice_allowlist_provider(
    store: GraphStore,
) -> Callable[[GatingContext], set[str] | None]:
    """The K4 allowlist provider for a voice caller (K4-D-2) — identical to chat's gate.

    Mirrors ``RuntimeFactory._build_graph_allowlist_provider`` exactly (the gate is never a
    voice-local variant, V13-D-5): the owner's wellbeing-tagged nodes narrowed to the
    gate-eligible categories, each recency-banded; most callers have none ⇒ ``None`` ⇒ no
    subtraction. Shared by the voice graph path (V13) and the voice unified recall (K9).
    """

    def flagged(owner: str) -> list[FlaggedNode]:
        now = datetime.now(UTC)
        out: list[FlaggedNode] = []
        for node in store.flagged_nodes(owner):
            category = parse_category(node.wellbeing_category)
            if category is None or not is_gate_eligible(category):
                continue
            out.append(
                FlaggedNode(
                    node_id=node.id,
                    category=category,
                    recency=recency_band(recency_bucket(node, now)),
                    text=f"{node.concept_name} {node.content}",
                )
            )
        return out

    return make_allowlist_provider(
        flagged_nodes=flagged,
        owner_node_ids=lambda owner: set(store.node_ids_for_owner(owner)),
    )


@dataclass(frozen=True)
class VoiceGraphComposition:
    """The two callables the K4-gated voice graph shell hands the turn context.

    Both are set together (iff a graph store is composed) or both ``None`` — never
    one without the other, so a surfaced wellbeing node always rides its care
    guidance, exactly as on the chat path.

    Attributes:
        retrieval: The per-turn ``query -> GraphContext`` callable (the K4-gated
            retrieval). Assigned to ``VoiceTurnContext.graph_retrieval``.
        surfacing_guidance: The K4 per-category spoken-care text provider for the
            K3 surfacing slot. Assigned to ``VoiceTurnContext.graph_surfacing_guidance``.
    """

    retrieval: Callable[[str], GraphContext]
    surfacing_guidance: Callable[[str, GraphRecency], str | None]


def build_voice_graph_retrieval(
    graph_store: GraphStore,
    *,
    owner_id: str,
) -> VoiceGraphComposition:
    """Compose the K4-gated voice graph retrieval + surfacing (V13-D-1/D-5).

    Mirrors chat's ``_build_graph_retrieval`` — the same ``HybridRetriever``, the
    same ``make_allowlist_provider`` gate (gate-eligible flagged nodes narrowed by
    category, banded by recency), the same ``get_recent_window`` lift source, the
    same ``surfacing_guidance`` care text — adapted only for the voice owner scope
    (a fixed caller id) and the voice profile (traversal-off + node budget). No
    voice-local gate variant exists; this is the identity K4's constraint requires.

    Args:
        graph_store: The owner-scoped graph store (``build_graph_store`` on the
            session RLS engine). Reads are confined to ``owner_id``.
        owner_id: The caller's user id — the fixed owner scope for the session
            (the voice runner's session engine is already scoped to it).

    Returns:
        The retrieval + surfacing callables for :class:`VoiceTurnContext`.
    """
    settings: GraphSettings = voice_graph_settings(GraphSettings())
    retriever = HybridRetriever(store=graph_store, settings=settings)
    store = graph_store

    # K4 (K4-D-2): the gate-eligible flagged nodes the allowlist provider gates
    # over — the owner's wellbeing-tagged nodes narrowed to the gate-eligible
    # categories, each with its recency band. Most owners have none → the provider
    # returns None (no subtraction) → the hot path stays free. Identical to chat's
    # closure (runtime_factory.py) — the gate is not reinvented here.
    def flagged(owner: str) -> list[FlaggedNode]:
        now = datetime.now(UTC)
        out: list[FlaggedNode] = []
        for node in store.flagged_nodes(owner):
            category = parse_category(node.wellbeing_category)
            if category is None or not is_gate_eligible(category):
                continue
            out.append(
                FlaggedNode(
                    node_id=node.id,
                    category=category,
                    recency=recency_band(recency_bucket(node, now)),
                    text=f"{node.concept_name} {node.content}",
                )
            )
        return out

    allowlist_provider = make_allowlist_provider(
        flagged_nodes=flagged,
        owner_node_ids=lambda owner: set(store.node_ids_for_owner(owner)),
    )
    # The recent-window source: the per-turn ContextVar the reply producer
    # publishes (``set_recent_window_from_messages``) BEFORE the off-thread query,
    # so the gate reads the conversation, not the bare query (no uncanny
    # re-closing). ``asyncio.to_thread`` copies the ContextVar into the worker
    # thread — pinned live in T2. Unset ⇒ empty ⇒ query-only (fail-safe).
    retrieval = make_graph_retrieval(
        retriever=retriever,
        owner_provider=lambda: owner_id,
        settings=settings,
        max_items=VOICE_NODE_BUDGET,
        allowlist_provider=allowlist_provider,
        recent_window_provider=get_recent_window,
    )
    return VoiceGraphComposition(retrieval=retrieval, surfacing_guidance=surfacing_guidance)


@dataclass(frozen=True)
class VoiceUnifiedComposition:
    """The K9 unified-recall callables for a voice turn (K9, T9).

    Attributes:
        retrieval: The per-turn ``query -> UnifiedProjection`` (fuse-don't-route over the
            pyramid + graph, reranked+gated). Handed to ``retrieve_context`` via
            ``VoiceTurnContext.unified_recall``; it runs inside the reply producer's
            ``asyncio.to_thread`` (the reranker OFF the event loop — a stall degrades to the
            fused order, never stalls the spoken turn; K9-D-3/D-4).
        surfacing_guidance: The K4 per-category spoken-care text provider (the K3 slot).
    """

    retrieval: Callable[[str], UnifiedProjection]
    surfacing_guidance: Callable[[str, GraphRecency], str | None]


def build_voice_unified_recall(
    graph_store: GraphStore,
    episodic_store: EpisodicStore,
    *,
    owner_id: str,
    persona_id: str,
) -> VoiceUnifiedComposition:
    """Compose the K9 unified recall for a voice turn (K9, T9) — NO voice fork.

    Reuses ``make_unified_recall`` + the shared adapters exactly as chat does (K9-D-11), with
    the voice adaptations that are NOT gate variants: the fixed caller owner scope (V13-D-1),
    the voice profile (traversal-off graph settings + a tighter node budget, V13-D-3), and a
    voice reranker bounded by the measured voice deadline (K9-D-4 — a rerank stall degrades to
    fused). The K4 gate is IDENTICAL to chat's (``_voice_allowlist_provider`` + the recent-window
    lift + surfacing) — the standing voice-safety identity (V13-D-5). No P7 scorer yet ⇒ the
    fail-soft shell yields the fused order (D-12 stub-safe). Runs OFF the loop inside the reply
    producer's ``to_thread``.
    """
    from persona.recall.config import RecallSettings
    from persona.recall.rerank import build_reranker
    from persona.stores.lifecycle import EpisodicSettings
    from persona_runtime.recall_adapters import GraphNeighbourProvider, PyramidEpisodeProvider
    from persona_runtime.unified_recall import make_unified_recall

    graph_settings: GraphSettings = voice_graph_settings(GraphSettings())
    retriever = HybridRetriever(store=graph_store, settings=graph_settings)
    # Voice profile: fewer, surer nodes (V13-D-3 VOICE_NODE_BUDGET); the voice-deadline reranker
    # (stub ⇒ fused now; a P7 tiny encoder later, bounded so a stall degrades to fused).
    settings = RecallSettings(result_budget=VOICE_NODE_BUDGET)
    reranker = build_reranker(
        scorer=None, settings=settings, timeout_s=settings.rerank_timeout_ms_voice / 1000.0
    )
    retrieval = make_unified_recall(
        reranker=reranker,
        settings=settings,
        episodic_settings=EpisodicSettings(),
        persona_id=persona_id,
        owner_provider=lambda: owner_id,
        episodic_query=lambda q, k: episodic_store.query(persona_id, q, k),
        resolve_display=lambda chunks: episodic_store.resolve_display(persona_id, list(chunks)),
        gist_query=lambda q, k: episodic_store.pyramid.query(persona_id, q, k),
        graph_retrieve=lambda q: retriever.retrieve(owner_id, q),
        episode_provider=PyramidEpisodeProvider(episodic_store.pyramid, persona_id),
        neighbour_provider=GraphNeighbourProvider(graph_store, lambda: owner_id),
        allowlist_provider=_voice_allowlist_provider(graph_store),
        recent_window_provider=get_recent_window,
        rerank_top_k=settings.rerank_top_k_voice,
    )
    return VoiceUnifiedComposition(retrieval=retrieval, surfacing_guidance=surfacing_guidance)
