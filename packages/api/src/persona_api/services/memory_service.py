"""Memory (knowledge-graph UI) read projections over the K0 graph store (Spec K5).

Pure projection: shapes the K0 graph types (:class:`ConceptNode`, :class:`TypedLink`,
:class:`NodeProvenance`) into the Memory API response models. **No graph logic lives
here** — the store owns structure, retrieval, and policy (criterion 12). The owner is
always the authenticated caller (RLS-scoped); every function is a read (CQS — no writes).

Windowing (K5-D-2): the seed (no focus) or a one-hop focus neighbourhood — never the
whole graph. The constants below are the provisional sizes (B1-tuned later; config in a
follow-up — YAGNI for the read batch).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from persona.extraction import InteractionKind
from persona.graph.config import GraphSettings
from persona.graph.models import LinkType, TypedLink, make_edge_id
from persona.graph.retrieval import HybridRetriever
from persona.stores.episodic import EpisodicStore
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from persona_api.schemas.responses import (
    EpisodicGistView,
    EpisodicMembersResponse,
    EpisodicMemberView,
    EpisodicWindowResponse,
    ForgetCandidate,
    MemoryEvolutionEntry,
    MemoryLinkEdge,
    MemoryLinkView,
    MemoryNodeDetail,
    MemoryNodeSummary,
    MemoryProvenanceView,
    MemorySearchResponse,
    MemorySearchResult,
    MemoryWindowResponse,
)
from persona_api.services import persona_service
from persona_api.services.persona_service import persona_name_from_yaml

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.audit import AuditLogger
    from persona.graph.models import ConceptNode, NodeProvenance
    from persona.graph.protocol import GraphStore
    from persona.schema.chunks import PersonaChunk
    from persona.stores.backend import Backend
    from sqlalchemy import Engine

# Provisional window sizes (K5-D-2 — B1-tuned; config later).
_SEED_LIMIT = 200
_NEIGHBOR_LIMIT = 60

# Spec K11: how many episodic candidates to pull per persona before floor-filtering.
# Generous — the forget-preview is an off-turn action (D-K11-8: latency is a
# non-constraint), so wide recall beats a tight budget that might miss evidence.
_FORGET_QUERY_TOP_K = 20
# Spec K11, T2: the episodic browser's ``q`` search budget — same generous, off-turn
# recall as the forget-preview above (a browse/search click, not a chat turn).
_EPISODIC_QUERY_TOP_K = 20
# Page size for paging through the owner's personas (below) — NOT a cap: every page
# is fetched, so an owner with more personas than this is still scanned exhaustively.
_PERSONA_SCAN_PAGE_SIZE = 500


class ForgetSettings(BaseSettings):
    """Cross-layer forget tunables (Spec K11), read from ``PERSONA_FORGET_*`` env vars.

    Mirrors the ``GraphSettings``/``EpisodicSettings`` precedent (env-driven, never
    hardcoded thresholds).

    Attributes:
        similarity_floor: A forget-preview candidate's cosine similarity
            (``1 - distance``) to the concept node's content must be at least this to
            surface (D-K11-1). The hybrid semantic + confirm design: wide recall, a
            confirmable floor, never a silent auto-delete.
    """

    model_config = SettingsConfigDict(env_prefix="PERSONA_FORGET_", extra="ignore")

    similarity_floor: float = Field(default=0.80, ge=0.0, le=1.0)


def _conversation_id(origin: NodeProvenance) -> str | None:
    """The conversation this memory can be opened at, or ``None`` if it isn't a conversation.

    ``interaction_id`` is a conversation id for chat/voice-sourced memories but a run id
    for agentic-run-sourced ones (R-K5-OPEN-CONV) — routing to ``/chat/{run_id}`` would
    404. We expose the id as ``conversation_id`` ONLY when the source is a conversation.
    A legacy/absent ``interaction_kind`` is treated as a conversation (every persisted
    memory today is conversation-sourced — the run source is not yet wired).
    """
    if origin.interaction_id is None:
        return None
    if origin.interaction_kind == InteractionKind.AGENTIC_RUN.value:
        return None
    return origin.interaction_id


def _summary(node: ConceptNode, *, degree: int = 0) -> MemoryNodeSummary:
    """Project a node to its canvas summary (no content/provenance — that's detail)."""
    return MemoryNodeSummary(
        id=node.id,
        kind=str(node.node_kind),
        label=node.concept_name,
        wellbeing_category=node.wellbeing_category,
        degree=degree,
    )


def build_window(
    store: GraphStore,
    owner_id: str,
    *,
    focus_id: str | None = None,
    seed_limit: int = _SEED_LIMIT,
    neighbor_limit: int = _NEIGHBOR_LIMIT,
) -> MemoryWindowResponse:
    """A windowed slice of the owner's graph — the seed, or a focus neighbourhood.

    No focus → the seed window (most-connected, most-recent; K5-D-8). A focus →
    that node plus its one-hop neighbours. ``total_nodes`` is the owner's full tally
    (the header), but ``nodes``/``links`` are only the loaded window (K5-D-2).
    """
    total = store.count_nodes(owner_id)
    if focus_id is None:
        nodes = store.seed_nodes(owner_id, limit=seed_limit)
        return _assemble_window(store, owner_id, nodes, focus_id=None, is_seed=True, total=total)

    focus = store.get_node(owner_id, focus_id)
    if focus is None:
        return MemoryWindowResponse(
            focus_id=focus_id, is_seed=False, total_nodes=total, nodes=[], links=[]
        )
    nodes = [focus]
    seen = {focus.id}
    # The focus's own edges (incl. on-the-fly ENTITY links) come from ``neighbors``;
    # neighbour↔neighbour stored edges come from ``edges_among`` below.
    focus_edges = store.neighbors(owner_id, focus_id, limit=neighbor_limit)
    for _edge, neighbor in focus_edges:
        if neighbor.id not in seen:
            seen.add(neighbor.id)
            nodes.append(neighbor)
    return _assemble_window(
        store,
        owner_id,
        nodes,
        focus_id=focus_id,
        is_seed=False,
        total=total,
        extra_edges=[edge for edge, _node in focus_edges],
    )


def _temporal_key(node: ConceptNode) -> datetime:
    """The node's asserted-time anchor for before/after ordering (D-K12-B).

    The earliest :attr:`NodeProvenance.written_at` across the node's
    provenance trail — when the fact was FIRST established, which is the
    natural before/after anchor. Falls back to :attr:`ConceptNode.created_at`
    when the trail is empty or carries no ``written_at`` (never crashes,
    never returns ``None``). Used by both the cluster sort and the
    consecutive-pair tie check in :func:`_derive_temporal_edges` so they
    agree on the same value.
    """
    return min((p.written_at for p in node.provenance), default=node.created_at)


def _derive_temporal_edges(nodes: list[ConceptNode], edges: list[TypedLink]) -> list[TypedLink]:
    """Read-time TEMPORAL edges among window nodes that SHARE CONTEXT (D-K12-B).

    Two nodes share context when they concern the same canonical entity (an
    ENTITY edge already present in ``edges``, on-the-fly per D-K0-9) OR were
    written from the same source interaction (any ``provenance.interaction_id``
    in common). Shared-context is unioned across BOTH signals (a node can
    belong to more than one group) via a plain union-find, so overlapping
    entity/interaction pairs merge into one cluster rather than fragmenting.

    Each resulting cluster is ordered by the node's asserted time — the
    EARLIEST :attr:`NodeProvenance.written_at` across its provenance trail
    (when the fact was first established), falling back to
    :attr:`ConceptNode.created_at` only when a node has no provenance
    ``written_at`` to read (never ``None``, never a crash) — see
    :func:`_temporal_key`. Per D-K12-B this is the ASSERTED time, not the DB
    row's creation time: a consolidated/backfilled node's ``created_at`` can
    postdate the event it describes, which would give a wrong before/after
    order if ``created_at`` were used directly. Ties are broken by id, for
    determinism. Ordered clusters get CONSECUTIVE TEMPORAL edges — A→B→C for
    a 3-node cluster, never all-pairs and never a chain across unrelated
    clusters. Direction is older → newer; a consecutive pair with the EXACT
    same temporal key (a true tie — no real before/after to assert) is
    skipped rather than given an arbitrary direction. Computed fresh on every
    read (like ENTITY edges) — no materialisation, no migration.

    An LLM-asserted TEMPORAL edge (materialised in ``graph_edges`` via ``merge.py``)
    runs newer → older (the just-merged candidate points at its chronologically
    earlier target) — the OPPOSITE order from a derived edge's older → newer. Because
    :func:`make_edge_id` is order-dependent, the two would mint DIFFERENT ids for the
    same node pair and both would render as contradictory duplicate temporal arrows.
    The asserted edge carries real LLM-judged semantic order, so it wins: derivation
    is SUPPRESSED for any pair that already has a TEMPORAL edge among ``edges`` in
    EITHER direction, and only fills pairs with no asserted temporal relation.
    """
    if len(nodes) < 2:  # noqa: PLR2004 — an edge needs two distinct endpoints
        return []
    node_ids = {n.id for n in nodes}
    asserted_temporal_pairs = {
        frozenset((edge.src_node_id, edge.dst_node_id))
        for edge in edges
        if edge.link_type == LinkType.TEMPORAL
    }
    parent: dict[str, str] = {n.id: n.id for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for edge in edges:
        if (
            edge.link_type == LinkType.ENTITY
            and edge.src_node_id in node_ids
            and edge.dst_node_id in node_ids
        ):
            union(edge.src_node_id, edge.dst_node_id)

    by_interaction: dict[str, list[str]] = {}
    for node in nodes:
        interaction_ids = {
            p.interaction_id for p in node.provenance if p.interaction_id is not None
        }
        for interaction_id in interaction_ids:
            by_interaction.setdefault(interaction_id, []).append(node.id)
    for group in by_interaction.values():
        for other in group[1:]:
            union(group[0], other)

    clusters: dict[str, list[ConceptNode]] = {}
    for node in nodes:
        clusters.setdefault(find(node.id), []).append(node)

    now = datetime.now(UTC)
    derived: list[TypedLink] = []
    for cluster in clusters.values():
        if len(cluster) < 2:  # noqa: PLR2004 — a chain needs at least two nodes
            continue
        ordered = sorted(cluster, key=lambda n: (_temporal_key(n), n.id))
        for older, newer in zip(ordered, ordered[1:], strict=False):
            if _temporal_key(older) == _temporal_key(newer):
                # Simultaneous — no real before/after to assert (a same-instant tie
                # in the test/seed data, or two facts written in the same batch).
                continue
            if frozenset((older.id, newer.id)) in asserted_temporal_pairs:
                # An LLM-asserted TEMPORAL edge already relates this pair (in either
                # direction) — it wins over the derived one (see docstring above).
                continue
            derived.append(
                TypedLink(
                    id=make_edge_id(older.id, newer.id, LinkType.TEMPORAL),
                    src_node_id=older.id,
                    dst_node_id=newer.id,
                    link_type=LinkType.TEMPORAL,
                    created_at=now,
                )
            )
    return derived


def _assemble_window(
    store: GraphStore,
    owner_id: str,
    nodes: list[ConceptNode],
    *,
    focus_id: str | None,
    is_seed: bool,
    total: int,
    extra_edges: list[TypedLink] | None = None,
) -> MemoryWindowResponse:
    node_ids = [n.id for n in nodes]
    # Entity relations (D-K12-A) are captured in the join table but never materialised
    # as ``graph_edges`` rows (D-K0-9) — union the on-the-fly ENTITY edges in so they
    # surface on this window too, not only via ``neighbors`` on the focus/detail path.
    # ``seen_edges`` below (keyed by the deterministic edge id) dedupes any overlap.
    edges = [*store.edges_among(owner_id, node_ids), *store.entity_edges_among(owner_id, node_ids)]
    if extra_edges:
        edges.extend(extra_edges)
    # Temporal relations (D-K12-B) are derived read-time from timestamps + shared
    # context (never materialised either). An LLM-asserted TEMPORAL edge runs the
    # OPPOSITE direction from a derived one (newer → older vs. older → newer), so a
    # same-pair collision does NOT collapse via the by-id dedup below — suppression
    # happens inside ``_derive_temporal_edges`` itself (skips any pair already
    # asserted, in either direction) before the edges are ever merged here.
    edges.extend(_derive_temporal_edges(nodes, edges))
    degree: dict[str, int] = dict.fromkeys(node_ids, 0)
    seen_edges: set[str] = set()
    # ENTITY edges are synthesised on-the-fly by TWO independent sources that disagree on the
    # edge id for the SAME relationship: ``neighbors`` (feeding ``extra_edges``) anchors the id
    # at the focus node (``{focus_id}::entity::{neighbor_id}``), while ``entity_edges_among``
    # canonicalises lexicographically (``make_edge_id`` is order-dependent) — so the two sources
    # can mint different ids for one A-B relationship. Dedup ENTITY edges by their UNORDERED
    # node-pair + link_type instead, so the relationship collapses to one edge regardless of
    # which source produced it or which direction its id encodes (no double-counted degree).
    # Non-entity edges (semantic/temporal/causal) carry a stable, deterministic (src, type, dst)
    # id (``make_edge_id``) whether sourced from a ``graph_edges`` row or derived read-time
    # (D-K12-B temporal), so id-based dedup applies to them unchanged either way.
    seen_entity_pairs: set[tuple[frozenset[str], str]] = set()
    link_views: list[MemoryLinkEdge] = []
    for edge in edges:
        link_type = str(edge.link_type)
        if edge.link_type == LinkType.ENTITY:
            entity_key = (frozenset((edge.src_node_id, edge.dst_node_id)), link_type)
            if entity_key in seen_entity_pairs:
                continue
            seen_entity_pairs.add(entity_key)
        else:
            if edge.id in seen_edges:
                continue
            seen_edges.add(edge.id)
        degree[edge.src_node_id] = degree.get(edge.src_node_id, 0) + 1
        degree[edge.dst_node_id] = degree.get(edge.dst_node_id, 0) + 1
        link_views.append(
            MemoryLinkEdge(
                src_node_id=edge.src_node_id,
                dst_node_id=edge.dst_node_id,
                link_type=link_type,
                weight=edge.weight,
            )
        )
    node_views = [_summary(n, degree=degree.get(n.id, 0)) for n in nodes]
    return MemoryWindowResponse(
        focus_id=focus_id,
        is_seed=is_seed,
        total_nodes=total,
        nodes=node_views,
        links=link_views,
    )


def node_detail(
    store: GraphStore,
    owner_id: str,
    node_id: str,
    *,
    neighbor_limit: int = _NEIGHBOR_LIMIT,
    persona_name_resolver: Callable[[str], str | None] | None = None,
) -> MemoryNodeDetail | None:
    """The node's full detail — content, provenance-as-story, evolution, typed links.

    Returns ``None`` when the node is not the owner's (the route 404s). The evolution
    is the node's full accumulation trail (D-K0-4); ``origin`` is its first
    contribution (where it came from). Typed links come from ``neighbors`` so the four
    relationships — including on-the-fly ENTITY threads — are all traversable.

    ``persona_name_resolver`` resolves the origin's ``persona_id`` to a display name so
    the panel reads "learned by <persona>" instead of a bare "system" (R-K5-PROV-PERSONA);
    the route binds it to the request's RLS engine (owner-scoped). ``origin.conversation_id``
    is the openable conversation link, set only for conversation-sourced memories
    (R-K5-OPEN-CONV).
    """
    node = store.get_node(owner_id, node_id)
    if node is None:
        return None
    trail = node.provenance  # at least one entry (ConceptNode invariant)
    origin = trail[0]
    persona_name = (
        persona_name_resolver(origin.persona_id)
        if persona_name_resolver is not None and origin.persona_id is not None
        else None
    )
    links: list[MemoryLinkView] = []
    for edge, neighbor in store.neighbors(owner_id, node_id, limit=neighbor_limit):
        direction: Literal["out", "in"] = "out" if edge.src_node_id == node_id else "in"
        links.append(
            MemoryLinkView(
                link_type=str(edge.link_type),
                weight=edge.weight,
                direction=direction,
                neighbor=_summary(neighbor),
            )
        )
    return MemoryNodeDetail(
        id=node.id,
        kind=str(node.node_kind),
        label=node.concept_name,
        content=node.content,
        wellbeing_category=node.wellbeing_category,
        created_at=node.created_at,
        origin=MemoryProvenanceView(
            source=str(origin.source),
            persona_id=origin.persona_id,
            persona_name=persona_name,
            interaction_id=origin.interaction_id,
            conversation_id=_conversation_id(origin),
            written_at=origin.written_at,
            reason=origin.reason,
            grounding=origin.grounding,
        ),
        evolution=[
            MemoryEvolutionEntry(
                source=str(p.source),
                written_at=p.written_at,
                reason=p.reason,
                superseded_content=p.superseded_content,
            )
            for p in trail
        ],
        links=links,
    )


def search(
    store: GraphStore,
    owner_id: str,
    query: str,
    *,
    settings: GraphSettings | None = None,
    top_k: int | None = None,
) -> MemorySearchResponse:
    """Search-to-navigate over the owner's graph — K1 hybrid (criterion 5).

    Exact-term and paraphrase both surface; ``dense_rank``/``sparse_rank`` are passed
    through so the UI can show *why* a node matched. **No K4 subtraction** here
    (``allowlist=None``): this is the user's own view and they see + control their
    flagged nodes (criterion 8), unlike persona retrieval which gates them (D-K1-7).
    """
    retriever = HybridRetriever(store=store, settings=settings or GraphSettings())
    results = retriever.retrieve(owner_id, query, allowlist=None, top_k=top_k)
    return MemorySearchResponse(
        query=query,
        results=[
            MemorySearchResult(
                node_id=r.node.id,
                label=r.node.concept_name,
                kind=str(r.node.node_kind),
                score=r.score,
                dense_rank=r.dense_rank,
                sparse_rank=r.sparse_rank,
            )
            for r in results
        ],
    )


def correct(
    store: GraphStore,
    owner_id: str,
    node_id: str,
    new_content: str,
    *,
    interaction_id: str | None = None,
) -> MemoryNodeDetail:
    """Apply a user's correction, then return the node's fresh detail (criterion 6).

    The write (re-embed, re-index, semantic links re-evaluated, provenance → user-edited)
    is the store's :meth:`correct_node`; this re-queries the corrected node so the caller
    (the ``PATCH`` route) returns the updated detail. Propagates
    ``GraphNodeNotFoundError`` (the route maps it to 404) when the node is not the owner's.
    """
    store.correct_node(owner_id, node_id, new_content, interaction_id=interaction_id)
    detail = node_detail(store, owner_id, node_id)
    if detail is None:  # pragma: no cover — correct_node already raised if absent
        from persona.graph.errors import GraphNodeNotFoundError

        raise GraphNodeNotFoundError(
            "memory vanished after correction", context={"node_id": node_id, "owner_id": owner_id}
        )
    return detail


def delete(store: GraphStore, owner_id: str, node_id: str) -> bool:
    """Delete a node — gone from Postgres AND the index in the same path (criterion 7).

    Returns ``True`` if the node existed (and is now removed everywhere — including from
    every persona's retrieval, via K0's same-path sync), ``False`` if it was not the
    owner's (the route 404s). The trust-critical action: a deletion that doesn't truly
    delete is the worst breach in the product (K5 §7). CQS — returns confirmation only.
    """
    return store.delete_node(owner_id, node_id)


def _list_all_owner_personas(rls_engine: Engine) -> list[dict[str, object]]:
    """Every one of the owner's personas — paged, never truncated.

    Spec K11: a forget is a privacy action, and a privacy action must never silently
    skip data. ``persona_service.list_personas`` is itself paginated (``limit``/
    ``offset``), so we loop pages of ``_PERSONA_SCAN_PAGE_SIZE`` until a short page
    signals the end, accumulating every persona regardless of how many the owner has.
    """
    personas: list[dict[str, object]] = []
    offset = 0
    while True:
        page = persona_service.list_personas(
            rls_engine=rls_engine, limit=_PERSONA_SCAN_PAGE_SIZE, offset=offset
        )
        personas.extend(page)
        if len(page) < _PERSONA_SCAN_PAGE_SIZE:
            break
        offset += _PERSONA_SCAN_PAGE_SIZE
    return personas


def forget_preview(
    *,
    graph_store: GraphStore,
    memory_backend: Backend,
    audit_logger: AuditLogger,
    rls_engine: Engine,
    owner_id: str,
    node_id: str,
    floor: float,
) -> list[ForgetCandidate]:
    """Cross-persona episodic evidence for a concept node, ready for owner confirmation.

    Spec K11, D-K11-1 (hybrid semantic + confirm): embeds the node's content — via
    ``episodic.query``, so the SAME embedder the episodic store writes with does the
    matching — then queries every one of the owner's personas' episodic stores
    (D-K11-3: the concept graph is user-wide but episodic is per-persona, so a forget
    must scan across all of them). Only **raw** evidence at/above ``floor`` similarity
    is returned: deleting it is what starves re-distillation (D-K11-2) and it already
    cascades the gists it covers on delete (K8-D-14), so no separate gist candidate is
    needed here. Nothing is deleted — a pure preview (CQS). Returns ``[]`` when the
    node is not the owner's (the route 404s via ``node_detail``, mirroring K5).

    ``memory_backend``/``audit_logger``/``rls_engine`` are threaded in (not rebuilt
    here) so the SAME process-shared embedder and audit routing the rest of the app
    uses apply — construction, not a fresh env-config each call (D-K11-8 makes
    latency a non-constraint for the request itself, not for reloading an ML model
    from disk on every preview click).
    """
    node = graph_store.get_node(owner_id, node_id)
    if node is None:
        return []
    episodic = EpisodicStore(backend=memory_backend, audit_logger=audit_logger)
    personas = _list_all_owner_personas(rls_engine)
    candidates: list[ForgetCandidate] = []
    for row in personas:
        persona_id = str(row["id"])
        persona_name = persona_name_from_yaml(str(row.get("yaml") or "")) or persona_id
        for chunk, similarity in _match_episodic_chunks(
            episodic, persona_id, [node.content], floor, top_k=_FORGET_QUERY_TOP_K
        ):
            candidates.append(
                ForgetCandidate(
                    persona_id=persona_id,
                    persona_name=persona_name,
                    chunk_id=chunk.id,
                    kind="raw",
                    text=chunk.text,
                    score=similarity,
                )
            )
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def _match_episodic_chunks(
    store: EpisodicStore,
    persona_id: str,
    texts: list[str],
    floor: float,
    *,
    top_k: int,
) -> list[tuple[PersonaChunk, float]]:
    """Query ``texts`` against a persona's episodic store; keep hits at/above ``floor``.

    The one similarity-matching primitive behind both :func:`forget_preview` (content-
    match a concept node's content against every owned persona) and
    :func:`match_episodic_by_text` (content-match a deleted conversation's message
    texts against ONE persona, Spec K11 T3, D-K11-9) — factored out so the two forget
    paths share the exact same matcher instead of two independently-drifting
    implementations of "similarity = 1 - distance, at/above the floor."

    Runs one ``store.query(persona_id, text, top_k)`` per text in ``texts`` and keeps
    each hit whose similarity clears ``floor``, deduplicated by chunk id in first-hit
    order across ``texts`` (a chunk matching two different candidate texts is reported
    once, at its first — not necessarily highest — score).
    """
    seen: set[str] = set()
    matched: list[tuple[PersonaChunk, float]] = []
    for text in texts:
        for chunk in store.query(persona_id, text, top_k):
            if chunk.id in seen:
                continue
            similarity = 0.0 if chunk.distance is None else 1.0 - float(chunk.distance)
            if similarity < floor:
                continue
            seen.add(chunk.id)
            matched.append((chunk, similarity))
    return matched


def match_episodic_by_text(
    memory_backend: Backend,
    audit_logger: AuditLogger,
    persona_id: str,
    texts: list[str],
    floor: float,
    *,
    top_k: int = _FORGET_QUERY_TOP_K,
) -> list[str]:
    """Content-match one persona's episodic chunk ids against candidate texts.

    Spec K11, T3 (D-K11-9): the LEGACY half of conversation-delete's forget cascade —
    a pre-stamp chunk carries no ``metadata["conversation_id"]``, so
    ``chat_service.delete_conversation`` falls back to this semantic match over the
    deleted conversation's own message texts (the exact-match half is a plain metadata
    filter, no store call needed). Reuses :func:`_match_episodic_chunks`, the SAME
    matcher :func:`forget_preview` uses — one similarity formula for both forget paths.
    Returns bare chunk ids (not full :class:`ForgetCandidate`\\ s): the caller only
    needs ids to feed ``episodic.remove_documents``.
    """
    store = EpisodicStore(backend=memory_backend, audit_logger=audit_logger)
    return [
        chunk.id
        for chunk, _similarity in _match_episodic_chunks(
            store, persona_id, texts, floor, top_k=top_k
        )
    ]


def forget(
    *,
    graph_store: GraphStore,
    memory_backend: Backend,
    audit_logger: AuditLogger,
    rls_engine: Engine,
    owner_id: str,
    node_id: str,
    episodic: list[tuple[str, str]],
) -> None:
    """Commit a cross-layer forget (Spec K11, D-K11-2): episodic evidence, then the node.

    The confirmed ``episodic`` pairs (``(persona_id, chunk_id)``, grouped per persona)
    are deleted FIRST via ``episodic.remove_documents`` — the primary act, since it
    already cascades every gist the raw chunk covers (K8-D-14) and starves the
    sleep-time engine's re-distillation (the resurrection guard). The concept node's
    delete rides along in the same operation.

    ``rls_engine`` re-derives the owner's persona-id set (``list_personas``) and any
    pair naming a persona outside it is dropped before deletion — defense in depth
    on top of RLS (which already fails a foreign ``persona_id`` closed at the
    ``memory_backend`` connection, matching zero rows, never an error, never a leak —
    the K5-proven pattern): a bad id is silently dropped here rather than issuing a
    DELETE the database would no-op anyway.
    """
    owned_persona_ids = {str(row["id"]) for row in _list_all_owner_personas(rls_engine)}
    store = EpisodicStore(backend=memory_backend, audit_logger=audit_logger)
    by_persona: dict[str, list[str]] = {}
    for persona_id, chunk_id in episodic:
        if persona_id not in owned_persona_ids:
            continue
        by_persona.setdefault(persona_id, []).append(chunk_id)
    for persona_id, chunk_ids in by_persona.items():
        store.remove_documents(persona_id, chunk_ids)
    delete(graph_store, owner_id, node_id)


def episodic_window(
    memory_backend: Backend,
    audit_logger: AuditLogger,
    persona_id: str,
    *,
    q: str | None = None,
    top_k: int = _EPISODIC_QUERY_TOP_K,
) -> EpisodicWindowResponse:
    """The episodic browser's gist-layer window for one persona (Spec K11, D-K11-5).

    No ``q``: every gist for the persona, newest-first. With ``q``: a semantic search
    over the RAW layer (``episodic.query`` — the exact recall method the chat/voice
    loop calls), reported at gist granularity by resolving each hit to its covering
    gist (``pyramid.covering_gists``), deduplicated in hit order. A raw hit with no
    covering gist yet (not consolidated) has no browser entry — the browser is
    gist-layer by default (D-K11-5, for scale); the standalone raw layer is reached
    via drill-down (``GET .../members``), not via this window.

    ``memory_backend``/``audit_logger`` are threaded in (not rebuilt here) so the SAME
    process-shared embedder applies (mirrors :func:`forget_preview`'s D-K11-8 rationale).
    """
    store = EpisodicStore(backend=memory_backend, audit_logger=audit_logger)
    gists: list[PersonaChunk]
    if q:
        hits = store.query(persona_id, q, top_k)
        covering = store.pyramid.covering_gists(persona_id, [c.id for c in hits])
        seen: set[str] = set()
        gists = []
        for chunk in hits:
            gist = covering.get(chunk.id)
            if gist is None or gist.id in seen:
                continue
            seen.add(gist.id)
            gists.append(gist)
    else:
        gists = sorted(store.pyramid.gists(persona_id), key=lambda g: g.created_at, reverse=True)
    return EpisodicWindowResponse(
        available=True,
        gists=[
            EpisodicGistView(
                id=g.id, text=g.text, member_ids=list(g.member_ids), created_at=g.created_at
            )
            for g in gists
        ],
    )


def episodic_members(
    memory_backend: Backend,
    audit_logger: AuditLogger,
    persona_id: str,
    gist_id: str,
) -> EpisodicMembersResponse:
    """A gist's raw members, in gist order (drill-down; Spec K11, D-K11-5).

    Empty when the gist id is unknown/not the persona's (RLS already scopes
    ``persona_id``) — the route never errors on a stale/foreign gist id, it just
    shows nothing (mirrors :meth:`persona.stores.pyramid.EpisodicPyramid.drill`).
    """
    store = EpisodicStore(backend=memory_backend, audit_logger=audit_logger)
    members = store.pyramid.drill(persona_id, gist_id)
    return EpisodicMembersResponse(
        members=[EpisodicMemberView(id=m.id, text=m.text, created_at=m.created_at) for m in members]
    )


def episodic_delete(
    memory_backend: Backend,
    audit_logger: AuditLogger,
    persona_id: str,
    chunk_id: str,
    *,
    is_gist: bool,
) -> bool:
    """Delete one episodic node in the browser (Spec K11, D-K11-6).

    A **raw chunk** deletes itself via ``remove_documents([id])`` (cascades its
    covering gist, K8-D-14). A **gist** deletes its cluster's raw members via
    ``remove_documents(gist.member_ids)`` — the cascade then removes the gist
    itself, so the browser never leaves an orphaned gist pointing at nothing.
    Concept-graph facts are untouched either way (D-K11-6: episodic-initiated
    deletes do not reach the concept graph — facts are managed in the concept view).

    Returns ``False`` (the route 404s) when the id is not found on the persona's
    (RLS-scoped) episodic store — existence-disclosure-safe, mirroring
    :func:`delete`'s ``bool`` return for the K5 ``delete_node`` route.
    """
    store = EpisodicStore(backend=memory_backend, audit_logger=audit_logger)
    if is_gist:
        gist = next((g for g in store.pyramid.gists(persona_id) if g.id == chunk_id), None)
        if gist is None:
            return False
        store.remove_documents(persona_id, list(gist.member_ids))
        return True
    existing = memory_backend.get_by_logical_ids(
        persona_id=persona_id, store_kind="episodic", logical_ids=[chunk_id]
    )
    if not existing:
        return False
    store.remove_documents(persona_id, [chunk_id])
    return True


__all__ = [
    "ForgetSettings",
    "build_window",
    "correct",
    "delete",
    "episodic_delete",
    "episodic_members",
    "episodic_window",
    "forget",
    "forget_preview",
    "match_episodic_by_text",
    "node_detail",
    "search",
]
