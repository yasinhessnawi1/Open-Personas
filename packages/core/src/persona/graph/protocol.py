"""The knowledge-graph ports — ``GraphStore`` / ``GraphIndex`` / ``EntityRegistry`` (Spec K0, T2).

These are the seams the later tasks fill (T3 transport, T5 registry, T7 index
adapters, T8 store) and that **K1 (hybrid retrieval) and K2 (write paths) build
on**. The shapes are designed to anticipate K1+K2, not merely to pass K0's own
criteria — the K-track lives or dies on getting them right.

**Scope binding (CSA-1 / D-14-X-scope-binding-discipline).** The graph is
*user*-scoped. Every store/registry method takes ``owner_id`` as its first
parameter — the scope identifier passed into the call slot, exactly as
``MemoryStore.write(persona_id, …)`` does. The parameter name is NOT a rename of
``persona_id``; it is the user-scope analogue. No Protocol fork: these are *new*
ports for a new store type, but they keep the single-Protocol calling convention.

**The durable string id is the identity; the surrogate is the index key.**
``GraphStore`` speaks in durable string node-ids (``ConceptNode.id``, the K1/K5
contract). ``GraphIndex`` speaks in ``uint64`` **surrogates** (D-K0-3) — the
store maps between them. Surrogates never escape into K1/K2/K5.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic needs runtime access for KnowledgeCandidate
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from persona.graph.models import (  # noqa: TC001 — Pydantic runtime
    LinkType,
    NodeKind,
    NodeProvenance,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    from persona.graph.models import (
        CanonicalEntity,
        ConceptNode,
        EntityAlias,
        NodeVersion,
        TypedLink,
    )

__all__ = [
    "EntityCandidate",
    "EntityRegistry",
    "GraphIndex",
    "GraphStore",
    "KnowledgeCandidate",
    "MergeAction",
    "MergeOutcome",
    "ProposedLink",
    "ResolutionDecision",
    "ResolutionVerdict",
    "UpdateIntent",
]


# ===========================================================================
# K2 write-path boundary shapes (K0 owns the merge-input contract; K2 fills it)
# ===========================================================================


class UpdateIntent(StrEnum):
    """Whether a candidate updates/contradicts existing knowledge (K2 §2 → D-K0-4).

    ``NONE`` = ordinary accumulate; ``UPDATE`` = refine/replace the value of a
    named prior node; ``CONTRADICT`` = the user's account reversed ("I no longer
    work at X"). For UPDATE/CONTRADICT the candidate names ``target_node_id`` and
    merge evolves that node with provenance — never a silent overwrite.
    """

    NONE = "none"
    UPDATE = "update"
    CONTRADICT = "contradict"


class ProposedLink(BaseModel):
    """A typed link K2 proposes from the new/extended node to an existing node.

    Carries the K2-asserted relationships (K2 §2 "typed-relationship
    assertion"): temporal where the account orders events, causal **only** on
    stated/strongly-implied causation (D-K0-8 — K0 stores what K2 sends; the
    conservatism is K2's). ``target_node_id`` must already exist (or land in the
    same merge batch); merge attaches the edge via :func:`make_edge_id`
    (idempotent).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_node_id: str
    link_type: LinkType
    weight: float | None = Field(default=None)
    reason: str | None = None


class KnowledgeCandidate(BaseModel):
    """The single structured shape merge consumes from BOTH K2 write paths (K2-D-4).

    Direct-write and synthesis both produce this (K2 §3 "both converge on one
    merge"). Entities are pre-resolved by K2 (it drives :meth:`EntityRegistry.resolve`
    and owns the LLM judge on the AMBIGUOUS band, D-K0-9), so by merge time the
    candidate carries resolved ``entity_ids`` — merge attaches entity links to
    them, it does not itself resolve aliases or call an LLM.

    Attributes:
        concept_name / content / node_kind: the concept to extend-or-create.
        entity_ids: resolved canonical entity ids this concept concerns (→ entity links).
        proposed_links: K2-asserted temporal/causal/extra links (:class:`ProposedLink`).
        wellbeing_category: K4 sensitive-category tag set at write (K2 §2; criterion 7).
        provenance: who/when/grounding (K2's grounded-extraction basis lives here).
        update_intent / target_node_id: the contradiction/update signal (D-K0-4).
    valid_at: the candidate's world-time event time (Spec K7, K7-D-2). The
        supersede gate compares this against the target's current ``valid_at``;
        ``None`` defaults to ``provenance.written_at``. K2 does not extract event
        time today — the slot is additive for a later K2 extraction extension.
    close_link_ids: edges K2's extraction layer (which has LLM judgment, off the
        hot path) explicitly names to window-close (Spec K7, K7-D-1.4). Merge
        validates each is the owner's + open, then calls ``invalidate_edge``.
        Defaulted-empty and not v1-wired by K2 (a later K2 follow-on emits it).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    concept_name: str
    content: str
    node_kind: NodeKind
    entity_ids: tuple[str, ...] = ()
    proposed_links: tuple[ProposedLink, ...] = ()
    wellbeing_category: str | None = None
    provenance: NodeProvenance
    update_intent: UpdateIntent = UpdateIntent.NONE
    target_node_id: str | None = None
    # --- K7 additive contract slots (K7-D-8; defaulted → K2 payloads byte-compatible)
    valid_at: datetime | None = None
    close_link_ids: tuple[str, ...] = ()


class MergeAction(StrEnum):
    """What merge did with a candidate (the observable half of idempotency, K2 §2).

    Widened additively by K7 (K7-D-8): ``EVOLVED`` distinguishes an UPDATE/CONTRADICT
    that superseded a prior account (Phase-1 finding: ``_evolve`` returned an
    indistinct ``EXTENDED``), and ``UNCHANGED`` names the observable idempotent no-op
    (the evolve short-circuit). Verified before widening: no consumer matches
    ``MergeAction`` exhaustively — ``store.py`` compares ``is CREATED`` (EVOLVED /
    UNCHANGED correctly take the index-``replace`` branch), K2 only logs it, and
    ``MergeOutcome`` is an in-process return value, never a persisted snapshot.
    """

    CREATED = "created"
    EXTENDED = "extended"
    EVOLVED = "evolved"
    UNCHANGED = "unchanged"


class MergeOutcome(BaseModel):
    """What :meth:`GraphStore.merge` did — K2 reads it for idempotency + K5 shows it.

    Lets K2 confirm a re-run over an already-synthesised interaction produced no
    new knowledge (``EXTENDED`` of the same node / no new links), the second line
    of defence behind K2's synthesised-marker (K2 criterion 8).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: MergeAction
    node_id: str
    created_link_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    #: The version row written by an evolve-that-superseded (Spec K7, K7-D-8) — the
    #: durable ``NodeVersion.version_id`` so K8/K5 can point at the prior account.
    #: ``None`` unless this outcome closed a node account.
    superseded_version_id: str | None = None


# ===========================================================================
# Entity resolution boundary shapes (K0 deterministic; K2 judges AMBIGUOUS)
# ===========================================================================


class ResolutionDecision(StrEnum):
    """The deterministic three-way verdict (D-K0-9 / Fellegi-Sunter three zones)."""

    MERGE = "merge"  # confident alias of a known entity → use canonical_id
    SEPARATE = "separate"  # confidently not a known entity → K2 creates a new one
    AMBIGUOUS = "ambiguous"  # the review band → K2's LLM judge decides on candidates


class EntityCandidate(BaseModel):
    """A registry candidate for an ambiguous mention — for K2's LLM judge to weigh."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str
    canonical_name: str
    score: float


class ResolutionVerdict(BaseModel):
    """The output of :meth:`EntityRegistry.resolve` (D-K0-9).

    ``MERGE`` → ``canonical_id`` is set. ``SEPARATE`` → neither (K2 creates).
    ``AMBIGUOUS`` → ``candidates`` holds the registry's top matches with scores
    for K2's binary LLM judge; K2 then calls :meth:`EntityRegistry.add_alias`
    (confirmed) or :meth:`EntityRegistry.create_entity` (rejected).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: ResolutionDecision
    canonical_id: str | None = None
    candidates: tuple[EntityCandidate, ...] = ()


# ===========================================================================
# Ports
# ===========================================================================


@runtime_checkable
class GraphIndex(Protocol):
    """The dense-index port — turbovec and pgvector adapters fill it (T7, D-K0-6).

    Keyed by ``uint64`` **surrogate** (D-K0-3), never by string node-id. The
    pgvector adapter is the default + only wired prod path for v0.1; the turbovec
    adapter is the opt-in quantized in-RAM index. Both must hold the same-path
    sync invariant (criterion 8) and be rebuildable from Postgres (criterion 9).
    """

    def add(self, *, surrogate: int, vector: Sequence[float]) -> None:
        """Add one vector under ``surrogate``. Raises GraphIndexError on dim mismatch / dup."""
        ...

    def replace(self, *, surrogate: int, vector: Sequence[float]) -> None:
        """Replace the vector for ``surrogate`` (extend path).

        turbovec has no upsert, so the adapter does remove-then-add; pgvector
        does a true upsert. Same observable result either way.
        """
        ...

    def remove(self, surrogate: int) -> bool:
        """Remove ``surrogate`` in O(1). Returns ``True`` if it was present."""
        ...

    def contains(self, surrogate: int) -> bool:
        """Whether ``surrogate`` is currently indexed (sync assertions)."""
        ...

    def search(
        self,
        *,
        query_vector: Sequence[float],
        top_k: int,
        allowlist: Sequence[int] | None = None,
    ) -> list[tuple[int, float]]:
        """Return up to ``top_k`` ``(surrogate, score)`` nearest to ``query_vector``.

        ``allowlist``, when given, restricts the candidate set to those
        surrogates **inside the search kernel** — the user-scoping + K4
        enforcement seam (K1 §2/§4; criterion 6). For the turbovec adapter this
        is ANN (approximate) and the store reranks above it; for pgvector it is
        exact. ``allowlist=None`` means "the whole index" (the store always
        passes the user's surrogate set, so isolation never relies on None).
        """
        ...

    def rebuild(self, items: Iterable[tuple[int, Sequence[float]]]) -> None:
        """Drop and rebuild the index from ``(surrogate, vector)`` pairs (criterion 9).

        The derived-not-authoritative safety property: the index is reconstructed
        from Postgres alone. For turbovec this is also the cold-start strategy
        (D-K0-6 — build once the user crosses the calibration threshold).
        """
        ...

    def persist(self) -> None:
        """Checkpoint the index to its backing store.

        turbovec writes its ``.tvim`` file; pgvector no-ops (Postgres is the
        store). Optional fast-restart aid — rebuild from Postgres is always the
        fallback.
        """
        ...


@runtime_checkable
class EntityRegistry(Protocol):
    """The canonical-entity registry port — deterministic + LLM-free (T5, D-K0-9).

    K0 owns the registry and the deterministic three-way verdict; K2 owns the LLM
    judge on the AMBIGUOUS band and calls back via :meth:`add_alias` /
    :meth:`create_entity`. Serves K2 §2 "entity recognition and resolution".
    """

    def resolve(self, owner_id: str, mention: str) -> ResolutionVerdict:
        """Resolve a surface mention to ``MERGE`` / ``SEPARATE`` / ``AMBIGUOUS`` (D-K0-9).

        Candidate-gen via name-embedding cosine + lexical match against the alias
        table; auto-merge upper ≈ 0.92, wide review band, F0.5 precision-biased
        (all config-driven). The K2 write path calls this per recognised entity.
        """
        ...

    def get_entity(self, owner_id: str, entity_id: str) -> CanonicalEntity | None:
        """Fetch a canonical entity by id (K5 display, K2 follow-up)."""
        ...

    def create_entity(
        self,
        owner_id: str,
        *,
        canonical_name: str,
        aliases: tuple[EntityAlias, ...] = (),
        provenance: NodeProvenance | None = None,
    ) -> CanonicalEntity:
        """Create a new canonical entity (K2 on ``SEPARATE`` / a judged-new mention)."""
        ...

    def add_alias(self, owner_id: str, entity_id: str, alias: EntityAlias) -> None:
        """Attach a confirmed surface form to a canonical entity (K2's MERGE callback)."""
        ...


@runtime_checkable
class GraphStore(Protocol):
    """The user-scoped graph store port — the K1+K2 surface (T8).

    Composes the Postgres transport (T3), the dense index (T7), the merge engine
    (T6), and the entity registry (T5). Synchronous (the persona-core convention,
    D-07-1) — K2 owns any off-critical-path scheduling (K2 §2 fire-and-forget).
    Every mutation writes Postgres (authoritative) AND the index in the same path
    (criterion 8) and emits one ``AuditEvent`` (the Spec 01 discipline).
    """

    # -- write (K2) ---------------------------------------------------------

    def merge(self, owner_id: str, candidate: KnowledgeCandidate) -> MergeOutcome:
        """Run the canonicalise→extend-vs-create→link→index-sync path (K2 §2/§3).

        The single write entrypoint both K2 paths converge on. Extends a
        semantically-matching node or creates a new linked one (D-K0-1), evolves
        with provenance on UPDATE/CONTRADICT (D-K0-4, no silent overwrite),
        attaches entity/proposed links, tags ``wellbeing_category`` (criterion 7),
        and syncs the index. Idempotent on an identical candidate (criterion 8).
        Raises ``NodeMergeError`` when it cannot proceed safely.
        """
        ...

    def correct_node(
        self, owner_id: str, node_id: str, new_content: str, *, interaction_id: str | None = None
    ) -> None:
        """Apply a user's correction to one node, in place (K5-D-7) — the highest-quality write.

        Targets *this* exact node (unlike :meth:`merge`'s similarity routing): re-embeds the
        new content, re-syncs the index (same path), re-evaluates the node's SEMANTIC links
        from the fresh embedding (entity/temporal/causal preserved — assertion-based), and
        appends a ``WriteSource.USER`` provenance entry carrying the prior content as
        ``superseded_content`` (D-K0-4 accumulate-in-place). A CQS command (returns
        confirmation by not raising; the caller re-queries ``get_node``).

        Raises ``GraphNodeNotFoundError`` when the node is not the owner's.
        """
        ...

    def delete_node(self, owner_id: str, node_id: str) -> bool:
        """Delete a node from Postgres AND the index in the same path (criterion 8; K5).

        Returns ``True`` if it existed. O(1) index removal by surrogate.
        """
        ...

    # -- the user's self node (Spec K6) ------------------------------------

    def get_self_node(self, owner_id: str) -> ConceptNode | None:
        """Read the user's central self node (Spec K6), or ``None`` if not yet created."""
        ...

    def get_or_create_self_node(
        self, owner_id: str, *, display_name: str | None = None
    ) -> ConceptNode:
        """Ensure the user's central ``SELF`` node exists, sync its name, return it (K6).

        Idempotent + race-safe via the reserved id (K6-D-5). ``display_name=None``
        ensures existence without touching an already-set name; a changed name is
        applied with a provenance append recording the prior name (K6-D-9).
        """
        ...

    # -- read: the K1 retrieval legs ---------------------------------------

    def get_node(
        self, owner_id: str, node_id: str, *, as_of: datetime | None = None
    ) -> ConceptNode | None:
        """Fetch a node (with its provenance trail) by durable id (K1 traversal, K5).

        ``as_of=None`` returns the CURRENT account (today's behaviour). With an
        ``as_of`` world-time, returns the account that was current at that instant
        (Spec K7, K7-D-1 / K7-D-X-read-surface point-in-time read): the matching
        window-closed :class:`~persona.graph.models.NodeVersion` reconstructed as a
        ``ConceptNode``, the current account if ``as_of`` is at/after its start, or
        ``None`` if the node did not yet exist. Retrieval (dense/FTS) is unaffected —
        it always scopes to current rows.
        """
        ...

    def get_node_versions(self, owner_id: str, node_id: str) -> list[NodeVersion]:
        """A node's window-closed prior accounts, oldest→newest (Spec K7, K7-D-1).

        Each :class:`~persona.graph.models.NodeVersion` preserves the account's
        content + metadata + provenance snapshot and its ``[valid_at, invalid_at)``
        world-time window — the §0-honest history the old in-place ``_evolve`` erased.
        Empty when the node has never been superseded.
        """
        ...

    def search_dense(
        self,
        owner_id: str,
        query: str,
        top_k: int,
        *,
        allowlist: set[str] | None = None,
    ) -> list[ConceptNode]:
        """The dense leg with MANDATORY exact-rerank (K1 §2; D-K0-7; criterion 1/2/11).

        Embeds ``query``, ANN-searches the index allowlist-scoped to the user's
        nodes (criterion 6), then **reranks the top-N against the float32 truth
        and returns the top-k** — the precision-recovery contract (raw quantized
        rankings never escape). On pgvector the search is already exact.
        ``allowlist`` is the positive allowed node-id set (K4 passes
        user-nodes − flagged, K1 §4); ``None`` = all the user's nodes. Returned
        nodes carry ``distance``.
        """
        ...

    def search_fts(self, owner_id: str, query: str, top_k: int) -> list[ConceptNode]:
        """The sparse BM25 leg over node content via Postgres FTS (K1 §2; criterion 3).

        RLS-scoped like all Postgres access. K1 fuses this with
        :meth:`search_dense` via RRF — K0 provides the legs, K1 owns the fusion
        (the §4 no-gating discipline is K1's).
        """
        ...

    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]:
        """The owner's wellbeing-tagged nodes — the K4 gate-eligible flagged read.

        Returns every node whose ``wellbeing_category`` is set, each with its
        full provenance trail so K4 can compute recency over the flagged set.
        RLS-scoped like all Postgres access (criterion 5).
        """
        ...

    def node_ids_for_owner(self, owner_id: str) -> list[str]:
        """Enumerate the owner's node ids — the K4 positive-allowlist basis.

        Returns every durable node-id the owner holds; K4 subtracts the flagged
        set from this to build :meth:`search_dense`'s positive allowlist.
        RLS-scoped like all Postgres access.
        """
        ...

    def count_nodes(self, owner_id: str) -> int:
        """The owner's total node count — the Memory header tally (K5-D-8).

        A cheap aggregate, so the windowed read never enumerates the whole graph
        just to show a total. RLS-scoped; a read (CQS).
        """
        ...

    def seed_nodes(self, owner_id: str, *, limit: int) -> list[ConceptNode]:
        """The first-paint seed window — the owner's most-recent nodes (K5-D-8, B1-refined).

        Returns up to ``limit`` nodes ordered by ``created_at`` DESC — "what you've been
        thinking about lately", the view the Memory map opens on when there is no focus.
        Index-served (``owner_id, created_at``), O(limit) regardless of graph size; never a
        whole-graph load. Recency is the v1 product choice (the seed is recoverable); the
        degree-anchor is a recorded planned option (decisions.md B1). RLS-scoped; a read (CQS).
        """
        ...

    def edges_among(self, owner_id: str, node_ids: Sequence[str]) -> list[TypedLink]:
        """The stored typed edges whose both endpoints lie in ``node_ids`` (K5-D-2).

        The induced sub-graph of a window, in one query — the
        semantic/temporal/causal edges materialised in storage. ENTITY links are
        resolved on-the-fly (:meth:`neighbors`, D-K0-9) and are not returned here.
        RLS-scoped; a read (CQS).
        """
        ...

    def recent_nodes(self, owner_id: str, *, limit: int) -> list[ConceptNode]:
        """The A5 noticing pool — the owner's most-salient, most-recent nodes.

        Additive read (Spec A5, A5-D-X-reads; the ``flagged_nodes`` precedent).
        Ordered by evidence salience then ``updated_at``, both descending — an
        ORDERING use of K7's salience column only, never a retrieval gate
        (K7-D-6's boundary holds: no field is added to :class:`ConceptNode`).
        Excludes, at the read: the SELF anchor, merged members
        (``merged_into`` set), and EVERY wellbeing-tagged node — the initiative
        subject-exclusion enforced where the pool is born
        (A5-D-X-k4-initiative-side). RLS-scoped; a read (CQS — no writes).
        """
        ...

    def neighbors(
        self,
        owner_id: str,
        node_id: str,
        *,
        link_types: set[LinkType] | None = None,
        limit: int,
        as_of: datetime | None = None,
    ) -> list[tuple[TypedLink, ConceptNode]]:
        """Typed one-hop traversal from ``node_id`` (K1 §2 link-aware; criterion 7).

        ``link_types=None`` traverses all four types; K1 passes a subset to pull
        the entity thread (``{ENTITY}``) or the story around an event
        (``{TEMPORAL, CAUSAL}``). Bounded by ``limit`` (K1-D-3 anti-flooding).
        Returns ``(edge, neighbour-node)`` so K1 can weight by link type/weight.

        **Traverses OPEN edges by default** (K7-D-1 / K7-D-X-read-surface); an
        ``as_of`` world-time returns the edges whose valid-time window covers that
        instant (point-in-time traversal). ENTITY threads are a current-state
        relationship (un-windowed) and are excluded when ``as_of`` is set.
        """
        ...

    def get_embeddings(self, owner_id: str, node_ids: Sequence[str]) -> dict[str, list[float]]:
        """Return the durable float32 embeddings for ``node_ids`` (K1 §2 rerank source).

        The exact truth K1's rerank verification (criterion 2) compares against,
        and the source the dense rerank and index rebuild read.
        """
        ...

    def record_recall(self, owner_id: str, node_ids: Sequence[str]) -> None:
        """Reinforce recalled nodes' evidence-salience — fail-soft, OFF the token path (K7-D-6).

        The recall-side hook K3 calls AFTER its injection selection (the nodes that
        entered the prompt), never on the reply stream. Best-effort ``+δ_r``; a
        failure never breaks the turn. v1 wires salience into no retrieval gate — this
        only records the evidence dynamics (K9 owns recall-side use).
        """
        ...

    # -- lifecycle ----------------------------------------------------------

    def rebuild_index(self, owner_id: str) -> None:
        """Rebuild the dense index from Postgres (criterion 9; D-K0-6 cold-start).

        Drops the derived index and re-adds every node's float32 embedding by
        surrogate — retrieval-equivalent results, the bound on the young-library
        risk. Also the pgvector→turbovec transition once the user crosses the
        calibration threshold.
        """
        ...
