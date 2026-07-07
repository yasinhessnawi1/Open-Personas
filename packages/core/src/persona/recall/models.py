"""The unified-recall candidate contract — the one heterogeneous shape (Spec K9, T1).

K9 fuses one candidate set over the K8 episodic pyramid (raw chunks + gists) **and**
the K7 knowledge graph — *fuse-don't-route* (K9-D-1): no episodic-vs-graph router
exists anywhere. To fuse structurally-different sources into one ranked list, every
retrieved thing is projected into a single frozen :class:`RecallCandidate` that carries
its **source** (which structure it came from — for per-source quotas and display
resolution), the underlying :class:`~persona.schema.chunks.PersonaChunk` **or**
:class:`~persona.graph.models.ConceptNode`, its **per-leg provenance ranks** (mirroring
K1's ``HybridResult`` observability — a leg-rank of ``None`` makes "surfaced by one leg
only" observable), the **two scoping keys** it must never cross (episodic ``persona_id``,
graph ``owner_id``), and the fused score/rank the pipeline assigns.

Score-incomparability across a pgvector cosine, a BM25/FTS rank, and a graph one-hop is
**not** resolved here: fusion crosses *rank* (RRF, K9-D-2) and the cross-encoder reranker
re-scores candidate *text* downstream — so this shape only needs to hold each source's
best candidates as peers, not to make their raw scores comparable.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from persona.graph.models import ConceptNode  # noqa: TC001 — Pydantic runtime
from persona.schema.chunks import PersonaChunk  # noqa: TC001 — Pydantic runtime

__all__ = ["RecallCandidate", "RecallLeg", "RecallSource"]


class RecallSource(StrEnum):
    """Which structure a candidate came from — the per-source-quota + display key.

    Coarser than :class:`RecallLeg`: a graph node retrieved by the dense leg, the
    sparse leg, and one-hop traversal is one candidate with ``source=GRAPH``. The
    source decides display resolution (an episodic-raw hit renders its chunk; a gist
    hit is an index into its members; a graph node injects its content) and the
    per-source contribution cap that stops one structure crowding the pool (K9-D-2).

    Values:
        EPISODIC_RAW: A raw episodic chunk (``store_kind='episodic'``) — text +
            embedding kept forever (Spec K8).
        EPISODIC_GIST: A gist row (``store_kind='episodic_gist'``) — a background
            summary whose ``member_ids`` are the drill/contiguity pointers.
        GRAPH: A knowledge-graph concept node (K7).
    """

    EPISODIC_RAW = "episodic_raw"
    EPISODIC_GIST = "episodic_gist"
    GRAPH = "graph"


class RecallLeg(StrEnum):
    """A retrieval leg — one ranked list fed into the fuse (K9-D-1).

    Five legs union into the one candidate set; each is a *method* of retrieval, and
    the same object can appear in several graph legs (a node in dense **and** sparse).
    Fusion weights and quotas are keyed per-leg (K9-D-2); the leg a candidate entered
    by is recorded on it (``*_rank`` fields) so "surfaced by one leg only" stays
    observable, exactly as K1's ``HybridResult`` per-leg ranks do.

    Values:
        EPISODIC_RAW: Dense recall over raw episodic chunks.
        EPISODIC_GIST: Dense recall over gist rows (gist-as-key).
        GRAPH_DENSE: The graph's dense (pgvector) leg.
        GRAPH_SPARSE: The graph's BM25/FTS leg.
        GRAPH_TRAVERSAL: The graph's bounded one-hop expansion.
    """

    EPISODIC_RAW = "episodic_raw"
    EPISODIC_GIST = "episodic_gist"
    GRAPH_DENSE = "graph_dense"
    GRAPH_SPARSE = "graph_sparse"
    GRAPH_TRAVERSAL = "graph_traversal"


#: Which :class:`RecallSource` each leg contributes to (fixed, total).
LEG_SOURCE: dict[RecallLeg, RecallSource] = {
    RecallLeg.EPISODIC_RAW: RecallSource.EPISODIC_RAW,
    RecallLeg.EPISODIC_GIST: RecallSource.EPISODIC_GIST,
    RecallLeg.GRAPH_DENSE: RecallSource.GRAPH,
    RecallLeg.GRAPH_SPARSE: RecallSource.GRAPH,
    RecallLeg.GRAPH_TRAVERSAL: RecallSource.GRAPH,
}


class RecallCandidate(BaseModel):
    """One fused candidate + why it was retrieved — the K9 pipeline's carried unit.

    A sibling of K1's ``HybridResult`` generalised across structures: it holds the
    underlying chunk **or** node (exactly one), the fused RRF score + final rank, the
    per-leg ranks (``None`` where a leg did not surface it), the dense-similarity
    reading that admitted it (``None`` for a sparse-only / traversal-only hit), and the
    two scoping keys resolved once at the seam so a candidate can never be read against
    the wrong owner/persona.

    Frozen + ``extra='forbid'`` so a candidate can be carried across pipeline stages
    without mutation or leakage (the ``HybridResult`` / ``GraphKnowledgeItem``
    discipline).

    Attributes:
        source: The structure this candidate came from (:class:`RecallSource`).
        key: The candidate's identity — the chunk id or the node id. Equals the set
            object's ``id`` (enforced), so it is the stable fusion/dedup key.
        chunk: The episodic chunk, when ``source`` is episodic; ``None`` for graph.
        node: The graph concept node, when ``source`` is ``GRAPH``; ``None`` otherwise.
        owner_id: The graph owner scope (set for graph candidates; ``None`` for
            episodic — episodic scopes by ``persona_id``). Carried, never crossed.
        persona_id: The episodic persona scope (set for episodic candidates; ``None``
            for graph). Carried, never crossed.
        fused_score: The fused RRF score (``Σ_leg weight_leg · 1/(rrf_k + rank_leg)``).
        rank: The final 1-indexed position in the fused ranking.
        relevance: The authoritative relevance reading in ``[0, 1]``. Pre-rerank it is the
            fused dense cosine similarity (``1 − distance``), or ``None`` for a
            sparse/traversal-only hit (no embedding distance); the cross-encoder reranker
            **overwrites it with its absolute score** (K9-D-5), so downstream the composite
            score (T4) and the abstention floor (T6) read the *reranked* relevance.
        episodic_raw_rank: 1-indexed rank in the episodic-raw leg; ``None`` if absent.
        gist_rank: 1-indexed rank in the gist leg; ``None`` if absent.
        graph_dense_rank: 1-indexed rank in the graph dense leg; ``None`` if absent.
        graph_sparse_rank: 1-indexed rank in the graph sparse leg; ``None`` if absent.
        graph_traversal_rank: 1-indexed rank in the graph one-hop leg; ``None`` if absent.
        composite_score: The additive relevance + recency + importance score (T4, K9-D-5),
            set by :func:`~persona.recall.scoring.apply_composite_score`; ``None`` before
            that stage runs (the fused order still governs until then).
        via_contiguity: ``True`` if this candidate was pulled in by temporal-contiguity
            expansion (T4, K9-D-6) rather than a retrieval leg — the reserved-budget
            neighbours of a hit, not a similarity match.
        diffusion_score: The low-weight graph-diffusion (PPR) tiebreak contribution (T5,
            K9-D-8); ``None`` unless the multi-hop gate admitted diffusion this turn.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: RecallSource
    key: str
    chunk: PersonaChunk | None = None
    node: ConceptNode | None = None
    owner_id: str | None = None
    persona_id: str | None = None
    fused_score: float = 0.0
    rank: int = 0
    relevance: float | None = None
    episodic_raw_rank: int | None = None
    gist_rank: int | None = None
    graph_dense_rank: int | None = None
    graph_sparse_rank: int | None = None
    graph_traversal_rank: int | None = None
    composite_score: float | None = None
    via_contiguity: bool = False
    diffusion_score: float | None = None

    @model_validator(mode="after")
    def _exactly_one_payload_matching_source(self) -> RecallCandidate:
        """Enforce exactly one payload, consistent with ``source`` and ``key``."""
        is_graph = self.source is RecallSource.GRAPH
        if is_graph:
            if self.node is None or self.chunk is not None:
                msg = "a GRAPH candidate carries a node and no chunk"
                raise ValueError(msg)
            payload_id = self.node.id
        else:
            if self.chunk is None or self.node is not None:
                msg = f"an episodic candidate ({self.source}) carries a chunk and no node"
                raise ValueError(msg)
            payload_id = self.chunk.id
        if self.key != payload_id:
            msg = f"key {self.key!r} must equal the payload id {payload_id!r}"
            raise ValueError(msg)
        return self

    @property
    def rerank_text(self) -> str:
        """The text a cross-encoder reranks against (T3 reads this; K9-D-3).

        A chunk yields its raw text; a node yields ``concept_name`` + ``content`` so the
        label frames the accumulating understanding. Pure derived accessor — a candidate
        must always be able to yield its own rerankable text.
        """
        if self.node is not None:
            return f"{self.node.concept_name} {self.node.content}".strip()
        assert self.chunk is not None  # noqa: S101 — invariant proven by the validator
        return self.chunk.text
