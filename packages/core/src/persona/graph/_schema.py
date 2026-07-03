"""Core-side SQLAlchemy table views for the knowledge graph (Spec K0, T3 / D-K0-3).

persona-core CANNOT import the api package, so — exactly as ``stores/postgres.py``
defines its own minimal ``memory_chunks`` view — this module defines core's own
view of the three graph tables. The api-owned Alembic migration (T4) is the
production schema + RLS; a T4 contract test asserts the two agree, guarding drift.

Three tables, all user-scoped (``owner_id`` → ``users.id``), RLS-isolated in prod
(direct ``owner_id`` policy, added by the migration — NOT here; ``create_all`` in
tests builds tables only, like ``pg_engine``):

- ``graph_nodes`` — concept-nodes: durable string ``id`` + ``BIGINT IDENTITY``
  ``surrogate`` (the turbovec uint64 index key), float32 ``embedding`` (durable
  truth + rerank source), generated ``fts`` tsvector (K1 BM25 leg), the
  accumulation ``provenance`` trail (JSONB), the K4 ``wellbeing_category``.
- ``graph_edges`` — typed links (semantic/entity/temporal/causal), composite-FK'd
  to nodes (the Spec 07 finding-1 defense-in-depth), ON DELETE CASCADE.
- ``graph_entities`` — the canonical-entity registry + alias set + name embedding.
"""

from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, REAL, TSVECTOR

__all__ = [
    "EMBEDDING_DIM",
    "graph_consolidation_markers",
    "graph_edges",
    "graph_entities",
    "graph_metadata",
    "graph_node_entities",
    "graph_node_versions",
    "graph_nodes",
]

# HNSW build params (Spec K7, K7-D-9): m=16 is right at 384-dim; ef_construction 200
# is the first-knob win. The migration recreates the indexes WITH these; declaring
# them here keeps ``create_all`` (core integration tests) building the same shape.
_HNSW_WITH = {"m": 16, "ef_construction": 200}

# bge-small-en-v1.5. Must match the api migration's vector(384); the T4 contract
# test asserts both ends agree.
EMBEDDING_DIM: int = 384

graph_metadata = MetaData()

graph_nodes = Table(
    "graph_nodes",
    graph_metadata,
    Column("id", Text, primary_key=True),
    # The turbovec uint64 index key (D-K0-3): collision-free + monotonic.
    Column("surrogate", BigInteger, Identity(always=True), nullable=False, unique=True),
    Column("owner_id", Text, nullable=False),
    Column("node_kind", Text, nullable=False),
    Column("concept_name", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("metadata", JSONB, nullable=False),
    Column("wellbeing_category", Text),
    Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
    Column("embedding_model", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    # The accumulation trail (D-K0-4): a JSONB array of NodeProvenance dumps.
    Column("provenance", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # --- Spec K7 lifecycle columns (K7-D-6/-4/-X-migration) ----------------
    # Evidence-salience (K7-D-6): a real typed/indexable column, not metadata JSONB.
    Column("salience", REAL, nullable=False, server_default=text("1.0")),
    # The ordinal evidence-clock epoch of this node's last evidence event — NEVER
    # wall time (K7-D-6). Disuse decay is measured in epochs beyond grace.
    Column("last_evidence_epoch", BigInteger, nullable=False, server_default=text("0")),
    # Soft merge reference (K7-D-4): a consolidated node points at its canonical.
    # Deliberately NO FK (K5's future delete-a-canonical must not cascade/fail on
    # members; integrity is code-validated). NULL = a live, non-merged node.
    Column("merged_into", Text, nullable=True),
    # Dirty-neighbourhood watermark source (K7-D-4): maintained by insert/update
    # transport writes; the consolidation pass scans updated_at > watermark.
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column(
        "fts",
        TSVECTOR,
        Computed("to_tsvector('english', concept_name || ' ' || content)", persisted=True),
    ),
    # Lets graph_edges reference (id, owner_id) so an edge can never cross tenants.
    UniqueConstraint("id", "owner_id", name="uq_graph_nodes_id_owner"),
    Index("ix_graph_nodes_owner", "owner_id"),
    Index("ix_graph_nodes_updated_at", "owner_id", "updated_at"),
    Index(
        "ix_graph_nodes_embedding_hnsw",
        "embedding",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with=_HNSW_WITH,
    ),
    Index("ix_graph_nodes_fts", "fts", postgresql_using="gin"),
)

# Valid-time-windowed edges (Spec K7, K7-D-1/-X-migration). The PK moves off ``id``
# to a BIGINT ``edge_key`` surrogate so a closed fact row and its re-asserted open
# successor can coexist as rows sharing the fact-``id``. A partial UNIQUE on ``id``
# WHERE ``invalid_at IS NULL`` enforces "one open edge per fact" — the idempotent
# upsert's conflict target. Windows: ``valid_at`` (backfilled := created_at),
# ``invalid_at`` (NULL = open), ``invalidated_by`` (provenance-of-closure),
# ``invalidated_at`` (bookkeeping, K7-D-10). Closed edges are NEVER deleted (§0).
graph_edges = Table(
    "graph_edges",
    graph_metadata,
    Column("edge_key", BigInteger, Identity(always=True), primary_key=True),
    Column("id", Text, nullable=False),
    Column("owner_id", Text, nullable=False),
    Column("src_node_id", Text, nullable=False),
    Column("dst_node_id", Text, nullable=False),
    Column("link_type", Text, nullable=False),
    Column("weight", REAL),
    Column("provenance", JSONB),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("valid_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("invalid_at", DateTime(timezone=True), nullable=True),
    Column("invalidated_by", Text, nullable=True),
    Column("invalidated_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "link_type IN ('semantic', 'entity', 'temporal', 'causal')",
        name="graph_edges_link_type_check",
    ),
    CheckConstraint(
        "invalid_at IS NULL OR invalid_at > valid_at",
        name="graph_edges_window_check",
    ),
    # Composite FKs: both endpoints must belong to the SAME owner (finding-1
    # defense-in-depth); ON DELETE CASCADE removes a node's edges with it.
    ForeignKeyConstraint(
        ["src_node_id", "owner_id"],
        ["graph_nodes.id", "graph_nodes.owner_id"],
        ondelete="CASCADE",
        name="fk_graph_edges_src_owner",
    ),
    ForeignKeyConstraint(
        ["dst_node_id", "owner_id"],
        ["graph_nodes.id", "graph_nodes.owner_id"],
        ondelete="CASCADE",
        name="fk_graph_edges_dst_owner",
    ),
    # One OPEN edge per fact-id (the idempotent-upsert conflict target).
    Index(
        "uq_graph_edges_open_id",
        "id",
        unique=True,
        postgresql_where=text("invalid_at IS NULL"),
    ),
    # Traversal indexes scoped to OPEN edges (the common current-state read).
    Index(
        "ix_graph_edges_src",
        "owner_id",
        "src_node_id",
        "link_type",
        postgresql_where=text("invalid_at IS NULL"),
    ),
    Index(
        "ix_graph_edges_dst",
        "owner_id",
        "dst_node_id",
        "link_type",
        postgresql_where=text("invalid_at IS NULL"),
    ),
)

graph_entities = Table(
    "graph_entities",
    graph_metadata,
    Column("id", Text, primary_key=True),
    Column("owner_id", Text, nullable=False),
    Column("canonical_name", Text, nullable=False),
    Column("aliases", JSONB, nullable=False),
    Column("name_embedding", Vector(EMBEDDING_DIM), nullable=False),
    Column("provenance", JSONB),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Lets graph_node_entities reference (id, owner_id) so an association can never
    # cross tenants (mirrors graph_nodes' uq_graph_nodes_id_owner).
    UniqueConstraint("id", "owner_id", name="uq_graph_entities_id_owner"),
    Index("ix_graph_entities_owner", "owner_id"),
    Index(
        "ix_graph_entities_name_hnsw",
        "name_embedding",
        postgresql_using="hnsw",
        postgresql_ops={"name_embedding": "vector_cosine_ops"},
        postgresql_with=_HNSW_WITH,
    ),
)

# node ↔ canonical-entity associations (Spec K0, T6b / D-K0-9). Which concept-nodes
# concern which entity — the substrate for entity links (criterion 2: "all nodes
# entity-linked to Dr. Hansen"). ENTITY traversal resolves through THIS table
# on-the-fly (node→entities→sibling nodes); node↔node entity edges are NOT
# materialized in graph_edges (no O(n²) within an entity cluster). Composite FKs to
# both parents keep an association single-tenant; ON DELETE CASCADE cleans up.
graph_node_entities = Table(
    "graph_node_entities",
    graph_metadata,
    Column("owner_id", Text, nullable=False),
    Column("node_id", Text, nullable=False),
    Column("entity_id", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("node_id", "entity_id", name="pk_graph_node_entities"),
    ForeignKeyConstraint(
        ["node_id", "owner_id"],
        ["graph_nodes.id", "graph_nodes.owner_id"],
        ondelete="CASCADE",
        name="fk_gne_node_owner",
    ),
    ForeignKeyConstraint(
        ["entity_id", "owner_id"],
        ["graph_entities.id", "graph_entities.owner_id"],
        ondelete="CASCADE",
        name="fk_gne_entity_owner",
    ),
    Index("ix_gne_entity", "owner_id", "entity_id"),
    Index("ix_gne_node", "owner_id", "node_id"),
)

# Node-version history (Spec K7, K7-D-1). On a superseding evolve the node's PRIOR
# mutable state (content + embedding + metadata + wellbeing + content_hash +
# provenance-trail snapshot) is preserved here, window-closed — the §0-honest fix
# for the one destructive spot in the old write path. Point-in-time node reads read
# these; retrieval stays scoped to current graph_nodes rows (no ranking pollution).
# Composite FK to (id, owner_id) ON DELETE CASCADE: a user-deleted node takes its
# history with it. Direct owner_id RLS (the 011 template) is added by the migration.
graph_node_versions = Table(
    "graph_node_versions",
    graph_metadata,
    Column("version_key", BigInteger, Identity(always=True), primary_key=True),
    Column("owner_id", Text, nullable=False),
    Column("node_id", Text, nullable=False),
    Column("node_kind", Text, nullable=False),
    Column("concept_name", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("metadata", JSONB, nullable=False),
    Column("wellbeing_category", Text),
    Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
    Column("embedding_model", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("provenance", JSONB, nullable=False),
    Column("valid_at", DateTime(timezone=True), nullable=False),
    Column("invalid_at", DateTime(timezone=True), nullable=False),
    Column("invalidated_by", Text, nullable=True),
    Column("invalidated_at", DateTime(timezone=True), nullable=True),
    ForeignKeyConstraint(
        ["node_id", "owner_id"],
        ["graph_nodes.id", "graph_nodes.owner_id"],
        ondelete="CASCADE",
        name="fk_gnv_node_owner",
    ),
    Index("ix_gnv_node", "owner_id", "node_id", "invalid_at"),
)

# Per-owner consolidation bookkeeping (Spec K7, K7-D-4/-6). ``current_epoch`` is the
# ORDINAL evidence clock (incremented once per consolidation run — never wall time);
# ``watermark`` bounds the dirty-neighbourhood scan; ``last_run_at`` is diagnostic.
# Direct owner_id RLS (migration).
graph_consolidation_markers = Table(
    "graph_consolidation_markers",
    graph_metadata,
    Column("owner_id", Text, primary_key=True),
    Column("current_epoch", BigInteger, nullable=False, server_default=text("0")),
    Column("watermark", DateTime(timezone=True), nullable=True),
    Column("last_run_at", DateTime(timezone=True), nullable=True),
)
