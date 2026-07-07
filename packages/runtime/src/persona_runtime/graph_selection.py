"""T2 — project K1 hybrid results into the injectable graph block (K3-D-2/3/4).

Given the per-turn K1 retrieval (a rank-ordered ``list[HybridResult]``), decide
which nodes are *relevant enough to inject* and project the survivors into the
light :class:`~persona_runtime.prompt.GraphKnowledgeItem` shape. Two pure
responsibilities:

- **The relevance gate (K3-D-3).** Inject on the genuine relevance signal —
  dense cosine similarity (``1 - node.distance``) at or above a floor — **never**
  on the RRF ``score`` (rank-based: rank-1 small talk scores like rank-1
  relevant, so an RRF floor stuffs every turn). Sparse-only nodes carry no
  embedding distance, so they get a deliberately *narrow* high-precision
  fallback: a top exact-term FTS hit (names, drugs) within a tight rank cap. A
  node with no relevance reading at all (pure traversal, no distance, no sparse
  rank) does not clear the gate — it is exactly the loosely-related node the
  threshold keeps out (criterion 3).

- **The light projection (K3-D-4).** A coarse recency bucket (for tentative
  framing of old knowledge), the origin persona/interaction (for the honest
  "how do you know?" answer), and the wellbeing tag (carried to *route* K4's
  surfacing slot, never rendered). No metadata dump.

The injection node budget (K3-D-2) caps how many survivors are kept (default:
K1's ``result_budget``); T5 lowers it under context pressure and the voice
profile lowers it further (K3-D-6). The gate thresholds live on
:class:`~persona.graph.config.GraphSettings`, validated by the calibration sweep
against labelled data (evidence in ``docs/specs/phase3/spec_K3/evidence/``), not
hardcoded. The recency boundaries below are coarse *projection policy* — labels,
not a precision-critical operating point — so they are documented constants, not
swept config.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from persona_runtime.prompt import GraphContext, GraphKnowledgeItem, GraphRecency

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from persona.graph.config import GraphSettings
    from persona.graph.fusion import HybridResult
    from persona.graph.models import ConceptNode

__all__ = [
    "GatingContext",
    "GraphRetriever",
    "make_graph_retrieval",
    "project_node",
    "recency_bucket",
    "select_graph_knowledge",
]


class GatingContext(BaseModel):
    """The per-turn signal K3's allowlist seam hands K4's gating policy (K4-D-2).

    The K4 context gate decides "has the user opened this topic / does the
    conversation concern it" from the conversation, never persona-type heuristics —
    so it needs more than the bare query: the current message **plus a short recent
    window**. Query-alone would re-close the gate on a follow-up turn that doesn't
    re-name the topic ("and then what?"), making the persona suddenly evasive about
    something it was just discussing (the uncanny-concealment failure). This frozen
    bundle is built by :func:`make_graph_retrieval` and passed to the K4
    ``allowlist_provider``; the seam (graph_selection) owns the shape, K4 owns the
    policy that reads it — so this type lives here, never importing K4.

    Attributes:
        owner_id: The resolved owner scope (single resolution, passed explicitly so
            the provider's flagged-node / node-id reads share one owner).
        query: This turn's user message — the explicit "raised it now" signal.
        recent_messages: A short window of recent conversation text (most-recent
            last). Empty until the composition wires a window source (T6); the gate
            then reads it for the "still in the topic" signal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_id: str
    query: str
    recent_messages: tuple[str, ...] = ()


def _no_recent_window() -> tuple[str, ...]:
    """Default recent-window source — empty until the composition wires one (T6)."""
    return ()


class GraphRetriever(Protocol):
    """The slice of K1's ``HybridRetriever`` that the prompt path consumes.

    A structural type so the runtime composition can wire the real core
    retriever without this module importing it, and so tests can substitute a
    fake. The ``allowlist`` is the K4 subtraction seam (passed through to K1,
    enforced post-fusion); T3 does not compute one — T7 wires the allowlist
    provider — but the parameter is honoured so the seam is already on the wire.
    """

    def retrieve(
        self,
        owner_id: str,
        query: str,
        *,
        allowlist: set[str] | None = None,
        top_k: int | None = None,
    ) -> list[HybridResult]: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


# Coarse recency buckets (K3-D-4). Deliberately coarse: the goal is to frame old
# knowledge tentatively ("you mentioned a while back…"), not to date it — so
# these are labels, not the swept relevance operating point. A node's "learned"
# time is its MOST RECENT contribution (the last reinforcement), so a fact
# re-confirmed lately reads as current even if first learned long ago — the
# defence against asserting reinforced-but-old knowledge as stale.
_RECENT_WITHIN = timedelta(days=14)
_WHILE_BACK_WITHIN = timedelta(days=180)


def _learned_at(node: ConceptNode) -> datetime:
    """The most recent contribution time — freshness, not origin."""
    return max((p.written_at for p in node.provenance), default=node.created_at)


def _recency_bucket(node: ConceptNode, now: datetime) -> GraphRecency:
    age = now - _learned_at(node)
    if age <= _RECENT_WITHIN:
        return GraphRecency.RECENT
    if age <= _WHILE_BACK_WITHIN:
        return GraphRecency.A_WHILE_BACK
    return GraphRecency.LONG_AGO


def recency_bucket(node: ConceptNode, now: datetime) -> GraphRecency:
    """The coarse recency bucket of a node (public; K4's flagged-node band computation).

    The same most-recent-contribution bucketing the K3 projection uses, exposed so the
    K4 composition can derive a flagged node's recency band without reaching into a
    private. ``now`` is passed (pure — no clock read).
    """
    return _recency_bucket(node, now)


def _source(node: ConceptNode) -> tuple[str | None, str | None]:
    """The most recent provenance naming a persona — the honest-attribution basis.

    Provenance accumulates oldest→newest (D-K0-4), so the newest persona-bearing
    entry is the most recent mention ("you mentioned this to Kai recently"). A
    node contributed only by the user/system carries no persona — attribution is
    then simply absent, never fabricated (D-K3-5 honesty rule).
    """
    for contribution in reversed(node.provenance):
        if contribution.persona_id is not None:
            return contribution.persona_id, contribution.interaction_id
    return None, None


def _passes_gate(result: HybridResult, settings: GraphSettings) -> bool:
    node = result.node
    if node.distance is not None:
        # The genuine signal: dense cosine similarity. Never the RRF score.
        return (1.0 - node.distance) >= settings.inject_similarity_floor
    # Sparse-only (no embedding distance): the narrow exact-term fallback.
    return result.sparse_rank is not None and result.sparse_rank <= settings.inject_sparse_rank_cap


def project_node(node: ConceptNode, *, relevance: float | None, now: datetime) -> GraphKnowledgeItem:
    """Project a graph node into the injectable item (K3-D-4) — the single-sourced projection.

    Used by K3's per-turn selection (:func:`_project`) AND K9's unified-recall projection
    (persona_runtime.unified_recall), so the two never fork the node→item mapping. ``relevance``
    is the reading that admitted the node (the reranked score on the K9 path, the dense cosine
    on the K3 path); ``now`` is the turn clock for the recency bucket (pure — no clock read).
    """
    persona, interaction = _source(node)
    return GraphKnowledgeItem(
        concept_name=node.concept_name,
        content=node.content,
        recency=_recency_bucket(node, now),
        source_persona=persona,
        source_interaction=interaction,
        wellbeing_category=node.wellbeing_category,
        relevance=relevance,
    )


def _project(result: HybridResult, now: datetime) -> GraphKnowledgeItem:
    node = result.node
    relevance = None if node.distance is None else 1.0 - node.distance
    return project_node(node, relevance=relevance, now=now)


def select_graph_knowledge(
    results: Sequence[HybridResult],
    *,
    settings: GraphSettings,
    now: datetime,
    max_items: int | None = None,
) -> GraphContext:
    """Gate, project, and budget the retrieved nodes into a :class:`GraphContext`.

    Args:
        results: The K1 hybrid retrieval for this turn, in fused-rank order.
        settings: Graph tunables — the relevance floor + sparse-rank cap.
        now: The turn's reference time (passed in; pure function, no clock read).
        max_items: The injection node budget. ``None`` ⇒ K1's ``result_budget``;
            T5 / the voice profile pass a smaller value under pressure.

    Returns:
        A bundle of the survivors in K1 rank order, truncated to the budget. An
        empty bundle when nothing clears the gate (small talk) → the prompt
        renders no graph block (criterion 9).
    """
    budget = settings.result_budget if max_items is None else max_items
    if budget <= 0:
        return GraphContext()
    survivors = [r for r in results if _passes_gate(r, settings)][:budget]
    return GraphContext(items=tuple(_project(r, now) for r in survivors))


def make_graph_retrieval(
    *,
    retriever: GraphRetriever,
    owner_provider: Callable[[], str | None],
    settings: GraphSettings,
    now: Callable[[], datetime] = _utcnow,
    max_items: int | None = None,
    allowlist_provider: Callable[[GatingContext], set[str] | None] | None = None,
    recent_window_provider: Callable[[], Sequence[str]] = _no_recent_window,
) -> Callable[[str], GraphContext]:
    """Build the per-turn ``query -> GraphContext`` callable for the prompt path.

    This is the owner-scoping seam (D-K3-X-a2-seam), mirroring K2's direct-write
    exactly: the toolbox/loop is composed once, but the **owner is resolved per
    turn** at dispatch via ``owner_provider`` (the API wires it to the
    ``current_user_id`` ContextVar, the same one K2's writes use — this runtime
    module never imports the API). The query is therefore confined to the
    requesting owner's graph, never a cross-owner read; with no owner context the
    call **fails closed** (an empty bundle, no read at all), so a missing scope
    can never leak another user's knowledge.

    The ``allowlist_provider`` is the K4 **subtraction** seam (D-K3-X-k4-seam,
    widened by K4-D-X-gating-signal-seam to receive a :class:`GatingContext`):
    it supplies, per turn, the set of node-ids K4 permits (``None`` ⇒ the whole
    owner graph). It is passed to K1 (the *primary* enforcement, post-fusion),
    AND re-applied here as **defense in depth** — K3 is the last surface before
    the model, and a wellbeing-subtracted node must be unreachable even if K1
    regressed. So a subtracted node is dropped by construction at the K3 layer,
    not merely trusted to be absent. K4 owns the policy that computes the set;
    K3 owns this wiring. The provider receives the turn's query + a short recent
    window (``recent_window_provider``) so the gate can decide "has the user opened
    this topic" from the conversation, not the bare query (K4-D-2). Both default to
    the no-op (``None`` provider / empty window), so an unwired composition is
    byte-identical to the reserved seam.

    Args:
        retriever: K1's hybrid retriever (owner-scoped at the call).
        owner_provider: Resolves the current owner id at dispatch; ``None`` ⇒
            fail closed.
        settings: The graph tunables (relevance floor + sparse-rank cap).
        now: The turn's clock (injected for testable recency bucketing).
        max_items: The injection node budget; ``None`` ⇒ K1's ``result_budget``.
            T5 / the voice profile pass a smaller value under pressure.
        allowlist_provider: The K4 gating policy — ``GatingContext`` ⇒ the permitted
            node-id set (``None`` ⇒ no subtraction). Default ``None`` is the reserved
            no-op; K4 lights it up by passing a provider.
        recent_window_provider: Supplies the short recent-conversation window for the
            gate's "still in the topic" signal. Default ``_no_recent_window`` (empty)
            keeps the unwired path byte-identical; the composition (T6) wires the
            real per-turn window.

    Returns:
        A ``Callable[[str], GraphContext]`` to hand ``retrieve_context`` as its
        ``graph_retrieval`` argument.
    """

    def retrieve(query: str) -> GraphContext:
        owner_id = owner_provider()
        if not owner_id:
            return GraphContext()  # fail closed: no owner scope → no graph read
        allowlist: set[str] | None = None
        if allowlist_provider is not None:
            gating = GatingContext(
                owner_id=owner_id, query=query, recent_messages=tuple(recent_window_provider())
            )
            allowlist = allowlist_provider(gating)
        results = retriever.retrieve(owner_id, query, allowlist=allowlist)
        if allowlist is not None:
            # Defense in depth (D-K3-X-k4-seam): K1 already enforces the allowlist
            # post-fusion, but K3 is the LAST surface before the model — re-drop
            # any node outside it so a K4-subtracted (wellbeing-sensitive) node is
            # structurally unreachable here even if the retriever regressed.
            results = [r for r in results if r.node.id in allowlist]
        return select_graph_knowledge(results, settings=settings, now=now(), max_items=max_items)

    return retrieve
