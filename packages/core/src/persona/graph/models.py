"""Graph domain primitives — the concept-node + typed-link model (Spec K0, T1).

:class:`ConceptNode` is a **sibling** of :class:`persona.schema.chunks.PersonaChunk`,
not a subclass (D-K0-5 / the D-14-X sibling-class precedent): a graph node is not
a persona chunk, and the type system reads that isolation — ``isinstance(node,
PersonaChunk)`` is ``False`` on every graph path. The provenance / content-hash /
tz-aware-UTC *conventions* are reused; the classes are independent.

**Scope binding (CSA-1 / D-14-X-scope-binding-discipline).** Graph nodes are
*user*-scoped. The owner (user) id is passed into store methods per-call
(``write(owner_id, nodes)``), NOT carried on the node — mirroring
``MemoryStore.write(persona_id, chunks)``. :func:`make_node_id` /
:func:`make_entity_id` embed the owner id in the durable string id for
readability (as ``make_chunk_id`` embeds ``persona_id``).

**The durable string id is the identity.** ``ConceptNode.id`` (a string) is the
domain / API / K5-facing identity. The ``uint64`` ``BIGINT`` surrogate that keys
the dense index (D-K0-3) is a *storage* concern and never appears on the domain
model; the store maps surrogate↔string id and rebuilds the map from Postgres.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from persona.schema.chunks import WriteSource  # noqa: TC001 — Pydantic needs runtime access

__all__ = [
    "NODE_ID_INDEX_WIDTH",
    "CanonicalEntity",
    "ConceptNode",
    "EntityAlias",
    "LinkType",
    "NodeKind",
    "NodeProvenance",
    "NodeVersion",
    "TypedLink",
    "make_edge_id",
    "make_entity_id",
    "make_node_id",
    "make_self_node_id",
]

# Wider than chunks' 4-digit index (D-01-2): the graph is an unbounded,
# forever-accumulating per-user set, so 8 digits (100M nodes/user) gives
# headroom. The string-id index is for determinism + human readability; the
# BIGINT surrogate (D-K0-3) is the real index key and ordering source.
NODE_ID_INDEX_WIDTH: int = 8


class NodeKind(StrEnum):
    """What a concept-node represents about the user (Spec K0 §2).

    ``SELF`` (Spec K6, K6-D-3) marks the single central per-user node — the user
    themselves, named with the user's name. It is an ordinary node in every other
    respect (Option C / K6-D-2: no forced star; facts connect via the normal typed
    links when they organically relate). Distinguishing it as a *kind* — rather
    than a boolean flag — lets ``get_self_node`` and the K5 renderer identify the
    anchor with a clean predicate, and costs no migration (``node_kind`` is stored
    as free ``TEXT`` with no CHECK constraint).
    """

    CONCEPT = "concept"
    FACT = "fact"
    PREFERENCE = "preference"
    TRAIT = "trait"
    GOAL = "goal"
    CIRCUMSTANCE = "circumstance"
    ENTITY = "entity"
    SELF = "self"
    #: Learned interaction patterns — "how this user likes the persona to behave"
    #: (Spec K7, K7-D-12; CoALA's procedural leg, arXiv 2309.02427). Zero migration:
    #: ``node_kind`` is free ``TEXT`` (the K6-D-3 precedent). Storable / retrievable /
    #: K4-gated exactly like every other kind; K2's extractor learns to emit it as a
    #: named follow-on (out of K7). Lifecycle (version / consolidate / decay) is the
    #: same as any node — Memp's "update, correct, deprecate" maps to
    #: invalidate-don't-delete (per-kind disuse rate is config, moderate).
    PROCEDURAL = "procedural"


class LinkType(StrEnum):
    """The four typed relationships between nodes (Spec K0 §3).

    Values:
        SEMANTIC: Derived automatically by embedding similarity — the
            automatic baseline that wires the graph as it grows (D-K0-2).
        ENTITY: The two nodes concern the same canonical entity (falls out of
            canonical resolution, D-K0-9).
        TEMPORAL: Before / after / at-the-time-of, derived from provenance
            timestamps and asserted by synthesis where meaningful.
        CAUSAL: Led-to / because — asserted **sparingly** by K2 only on the
            user's stated/strongly-implied causation, never inferred (D-K0-8).
    """

    SEMANTIC = "semantic"
    ENTITY = "entity"
    TEMPORAL = "temporal"
    CAUSAL = "causal"


def _ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes; convert tz-aware to UTC (the chunks §11.4 rule).

    Reimplemented locally rather than importing the module-private helper from
    :mod:`persona.schema.chunks` — the sibling-not-subclass discipline keeps the
    graph module self-contained.
    """
    if value.tzinfo is None:
        msg = "naive datetime not allowed; use datetime.now(timezone.utc) or attach a tzinfo"
        raise ValueError(msg)
    return value.astimezone(UTC)


def _sorted_metadata_repr(metadata: dict[str, str]) -> str:
    """Deterministic repr of a metadata dict (stable across key order)."""
    return repr(sorted(metadata.items(), key=lambda kv: kv[0]))


def _compute_node_hash(concept_name: str, content: str, metadata: dict[str, str]) -> str:
    """SHA-256 of ``concept_name`` + ``content`` + sorted-metadata repr.

    Same inputs → same output regardless of metadata key order. The accumulation
    trail (``provenance``), ``created_at``, and the query-time ``distance`` are
    deliberately excluded so the hash tracks *content*, not bookkeeping.
    """
    payload = (
        concept_name.encode("utf-8")
        + b"\x00"
        + content.encode("utf-8")
        + b"\x00"
        + _sorted_metadata_repr(metadata).encode("utf-8")
    )
    return hashlib.sha256(payload).hexdigest()


class NodeProvenance(BaseModel):
    """Where a node contribution (or an asserted edge) came from (Spec K0 §2/§3).

    A :class:`ConceptNode` carries a *tuple* of these — the accumulation trail
    (D-K0-4: nodes extend in place with a provenance trail, no silent
    overwrite). Edges carry at most one (an edge is asserted once).

    Attributes:
        source: Which update source produced this contribution. ``persona_self``
            for a persona's direct write (K2), ``system`` for synthesis, ``user``
            for a K5 edit.
        persona_id: Which persona contributed it (``None`` for user/system edits).
        interaction_id: The source interaction (conversation / run) id, if any.
        written_at: UTC timestamp of the contribution. Naive datetimes rejected.
        grounding: The supporting basis the contribution rests on — K2's
            grounded-extraction discipline records *why* this is believed.
        reason: Short free-text rationale (e.g. ``"contradiction: left job X"``).
        superseded_content: On an UPDATE/CONTRADICT contribution (D-K0-4), the
            node's PRIOR content — preserved as structured data (not buried in
            ``reason``) so K4 can read the trajectory and K5 can render a clean
            before/after. ``None`` on ordinary accumulation.
        channel: The originating surface of a synthesis contribution (``voice`` for
            a fact minted from a call, ``chat`` for a web/chat conversation), Spec
            V13. ``source`` stays ``system`` for all synthesis; ``channel`` is the
            finer provenance marker. ``None`` for pre-V13 nodes + non-synthesis
            writes (a direct persona/user write carries no channel).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: WriteSource
    persona_id: str | None = None
    interaction_id: str | None = None
    written_at: datetime
    grounding: str | None = None
    reason: str | None = None
    superseded_content: str | None = None
    channel: str | None = None

    @field_validator("written_at", mode="after")
    @classmethod
    def _written_at_must_be_tz_aware(cls, value: datetime) -> datetime:
        return _ensure_utc(value)


class ConceptNode(BaseModel):
    """A concept about the user that accumulates (the unit of the graph).

    Sibling of :class:`persona.schema.chunks.PersonaChunk`, not a subclass
    (D-K0-5). Frozen, ``extra="forbid"``, ``content_hash`` computed at
    construction, tz-aware UTC — the chunk conventions, reused.

    Attributes:
        id: The durable string node-id (API / K5-facing). Conventionally from
            :func:`make_node_id`. The dense-index ``uint64`` surrogate is a
            storage concern and is NOT a field here.
        node_kind: What the node represents (:class:`NodeKind`).
        concept_name: Short canonical label for the concept.
        content: The accumulating freeform understanding (D-K0-5
            freeform-with-structure).
        metadata: Arbitrary string-keyed metadata.
        wellbeing_category: The K4 sensitive-category tag, set at write by K2
            (``None`` until tagged). K4's retrieval-side policy depends on it.
        distance: Set by the store's dense query on retrieved nodes; never
            populated by writers.
        content_hash: SHA-256 of ``concept_name`` + ``content`` + sorted
            metadata. Computed if empty; if supplied, must match (tamper check).
        provenance: The accumulation trail — at least one entry; each extend
            appends one (D-K0-4).
        created_at: UTC creation timestamp. Naive datetimes rejected.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    node_kind: NodeKind
    concept_name: str
    content: str
    metadata: dict[str, str] = Field(default_factory=dict)
    wellbeing_category: str | None = None
    distance: float | None = None
    content_hash: str = ""
    provenance: tuple[NodeProvenance, ...] = Field(min_length=1)
    created_at: datetime

    @field_validator("created_at", mode="after")
    @classmethod
    def _created_at_must_be_tz_aware(cls, value: datetime) -> datetime:
        return _ensure_utc(value)

    @model_validator(mode="after")
    def _populate_or_verify_content_hash(self) -> ConceptNode:
        expected = _compute_node_hash(self.concept_name, self.content, self.metadata)
        if not self.content_hash:
            object.__setattr__(self, "content_hash", expected)
            return self
        if self.content_hash != expected:
            msg = f"content_hash mismatch: expected {expected!r}, got {self.content_hash!r}"
            raise ValueError(msg)
        return self


class NodeVersion(BaseModel):
    """A window-closed prior state of a :class:`ConceptNode` (Spec K7, K7-D-1).

    Written by ``_evolve`` when a node's account is superseded (an UPDATE/CONTRADICT
    that wins the recency+trust gate, K7-D-2): the node's PRIOR
    ``concept_name``/``content``/``metadata``/``wellbeing_category``/``content_hash``
    + the full provenance-trail snapshot are preserved as an immutable, RLS-scoped
    row, window-closed (``valid_at`` = when that account became current,
    ``invalid_at`` = the superseding fact's event time). This is the §0-honest fix
    for the one destructive spot in the old write path — ``_evolve`` used to replace
    content + embedding in place, leaving the prior account unreachable point-in-time.

    Like :class:`ConceptNode`, the durable ``embedding`` is a *storage* concern (it
    is preserved in the version row for byte-exact restore, K7-D-1, but never
    surfaced on the domain model). Retrieval (dense/FTS) stays scoped to current
    rows — version rows never pollute ranking. Point-in-time reads
    (:meth:`GraphStore.get_node_versions` / ``get_node_as_of``) return these.

    Attributes:
        version_id: The version row's durable string identity (from the BIGINT
            ``version_key``) — what ``MergeOutcome.superseded_version_id`` points at
            so K5/K8 can address a specific historical account.
        node_id: The live node this is a prior state of.
        node_kind / concept_name / content / metadata / wellbeing_category /
            content_hash / provenance: the snapshot of the node's mutable fields as
            they were while this account was current.
        valid_at: When this account became the node's current state.
        invalid_at: When it was superseded (the superseding fact's event time).
        invalidated_by: Provenance-of-closure (the superseding interaction/ref).
        invalidated_at: Bookkeeping (K7-D-10) — the SYSTEM moment of closure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version_id: str
    node_id: str
    node_kind: NodeKind
    concept_name: str
    content: str
    metadata: dict[str, str] = Field(default_factory=dict)
    wellbeing_category: str | None = None
    content_hash: str
    provenance: tuple[NodeProvenance, ...] = Field(min_length=1)
    valid_at: datetime
    invalid_at: datetime
    invalidated_by: str | None = None
    invalidated_at: datetime | None = None

    @field_validator("valid_at", "invalid_at", "invalidated_at", mode="after")
    @classmethod
    def _windows_tz_aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _ensure_utc(value)

    @model_validator(mode="after")
    def _window_ordered(self) -> NodeVersion:
        if self.invalid_at <= self.valid_at:
            msg = f"invalid_at ({self.invalid_at}) must be after valid_at ({self.valid_at})"
            raise ValueError(msg)
        return self


class TypedLink(BaseModel):
    """A typed, directed edge between two concept-nodes (Spec K0 §3).

    Attributes:
        id: Deterministic edge id (conventionally :func:`make_edge_id`), so
            re-asserting the same ``(src, type, dst)`` edge is idempotent.
        src_node_id: The source node's durable string id.
        dst_node_id: The destination node's durable string id.
        link_type: One of the four :class:`LinkType` relationships.
        weight: Optional strength — the semantic-similarity score for semantic
            links, a confidence/strength for others; ``None`` when not scored.
        provenance: Who asserted the edge (entity/temporal/causal links carry
            this; auto semantic links may leave it ``None``).
        created_at: UTC creation timestamp. Naive datetimes rejected.
        valid_at: Start of the edge's world-time validity window (Spec K7,
            K7-D-1). ``None`` on construction means "= ``created_at``" (the
            transport backfills it) — old constructors stay byte-compatible.
        invalid_at: End of the validity window — set by :meth:`invalidate_edge`
            when a later fact closes this edge (K7-D-1); ``None`` = still open.
            Point-in-time reads use ``valid_at <= q AND (invalid_at IS NULL OR
            invalid_at > q)``. A closed edge is NEVER deleted (§0).
        invalidated_by: Provenance-of-closure — who/what closed the window
            (e.g. ``"consolidation:{run_id}"`` or a K2 closure ref); ``None``
            while open.
        invalidated_at: Bookkeeping (K7-D-10) — the SYSTEM moment the window was
            closed (Graphiti's ``expired_at`` analogue); inert, no query surface.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    src_node_id: str
    dst_node_id: str
    link_type: LinkType
    weight: float | None = None
    provenance: NodeProvenance | None = None
    created_at: datetime
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    invalidated_by: str | None = None
    invalidated_at: datetime | None = None

    @field_validator("created_at", mode="after")
    @classmethod
    def _created_at_must_be_tz_aware(cls, value: datetime) -> datetime:
        return _ensure_utc(value)

    @field_validator("valid_at", "invalid_at", "invalidated_at", mode="after")
    @classmethod
    def _window_fields_tz_aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _ensure_utc(value)

    @model_validator(mode="after")
    def _no_self_loops(self) -> TypedLink:
        if self.src_node_id == self.dst_node_id:
            msg = f"a typed link cannot connect a node to itself: {self.src_node_id!r}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _window_ordered(self) -> TypedLink:
        # Mirrors the DDL CHECK (invalid_at > valid_at): a window cannot close
        # before it opens. valid_at may be None on construction (= created_at).
        start = self.valid_at if self.valid_at is not None else self.created_at
        if self.invalid_at is not None and self.invalid_at <= start:
            msg = f"invalid_at ({self.invalid_at}) must be after valid_at ({start})"
            raise ValueError(msg)
        return self


class EntityAlias(BaseModel):
    """A surface form of a canonical entity (Spec K0 §4 / D-K0-9).

    Attributes:
        surface: The alias text as it appeared ("my doctor", "the GP").
        confidence: Optional [0, 1] confidence that this surface maps to the
            canonical entity — set by the resolver (T5); ``None`` for an alias
            asserted without a score.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class CanonicalEntity(BaseModel):
    """A canonical entity in the per-user registry (Spec K0 §4).

    "my doctor" / "Dr. Hansen" / "the GP" resolve to ONE of these (D-K0-9);
    the surface forms live in :attr:`aliases`. Entity links (``LinkType.ENTITY``)
    thread the nodes that concern this entity.

    Attributes:
        id: Durable string entity id (conventionally :func:`make_entity_id`).
        canonical_name: The chosen canonical label.
        aliases: Known surface forms (possibly empty on first creation).
        provenance: Where the entity was first established.
        created_at: UTC creation timestamp. Naive datetimes rejected.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    canonical_name: str
    aliases: tuple[EntityAlias, ...] = ()
    provenance: NodeProvenance | None = None
    created_at: datetime

    @field_validator("created_at", mode="after")
    @classmethod
    def _created_at_must_be_tz_aware(cls, value: datetime) -> datetime:
        return _ensure_utc(value)


def _make_scoped_id(owner_id: str, kind: str, index: int, width: int) -> str:
    if index < 0:
        msg = f"id index must be non-negative; got {index!r}"
        raise ValueError(msg)
    return f"{owner_id}::{kind}::{index:0{width}d}"


def make_node_id(owner_id: str, index: int, *, width: int = NODE_ID_INDEX_WIDTH) -> str:
    """Build a deterministic, sortable durable node id.

    Format ``{owner_id}::node::{index:08d}`` — the graph analogue of
    :func:`persona.schema.chunks.make_chunk_id`, scoped to the *user*
    (``owner_id``) per CSA-1. The store supplies the per-user monotonic
    ``index``; ordering/index-key duties belong to the BIGINT surrogate (D-K0-3).

    Raises:
        ValueError: If ``index`` is negative.
    """
    return _make_scoped_id(owner_id, "node", index, width)


def make_entity_id(owner_id: str, index: int, *, width: int = NODE_ID_INDEX_WIDTH) -> str:
    """Build a deterministic, sortable canonical-entity id.

    Format ``{owner_id}::entity::{index:08d}``.

    Raises:
        ValueError: If ``index`` is negative.
    """
    return _make_scoped_id(owner_id, "entity", index, width)


def make_self_node_id(owner_id: str) -> str:
    """Build the reserved, singular self-node id (Spec K6, K6-D-5).

    Format ``{owner_id}::self`` — deliberately distinct from the monotonic
    ``{owner_id}::node::{index}`` fact ids and carrying NO index, because there is
    exactly ONE self node per user. Being deterministic, it makes get-or-create
    idempotent by primary key: two concurrent first-writes both target this id, so
    the ``uq_graph_nodes_id_owner`` constraint collapses the race to a single node
    (the loser re-reads the winner) rather than creating a duplicate or crashing.
    Verified safe: nothing in the codebase parses the ``::node::`` infix, so a
    distinct ``::self`` namespace breaks no id consumer.
    """
    return f"{owner_id}::self"


def make_edge_id(src_node_id: str, dst_node_id: str, link_type: LinkType) -> str:
    """Build a deterministic edge id for idempotent link assertion.

    Format ``{src_node_id}::{link_type}::{dst_node_id}``. Re-asserting the same
    ``(src, type, dst)`` edge yields the same id, so an upsert is a no-op rather
    than a duplicate — the property the merge engine (T6) relies on for
    semantic-link maintenance.
    """
    return f"{src_node_id}::{link_type}::{dst_node_id}"
