"""Postgres + pgvector transport for the knowledge graph (Spec K0, T3).

The graph analogue of :class:`persona.stores.postgres.PostgresBackend`: a dumb SQL
transport over the three :mod:`persona.graph._schema` tables. It does NOT own the
embedder (unlike the Spec 07 backend) — the merge engine (T6) embeds a node's
content ONCE and passes the vector here, so extend-vs-create matching and storage
share one embedding. Policy/merge/audit live above it (T6/T8); this is transport
only.

Decisions in force:

- **D-K0-3:** durable string ``id`` is the identity; the ``BIGINT IDENTITY``
  ``surrogate`` is the turbovec index key — assigned by Postgres on insert and
  returned so the store can sync the index.
- **D-K0-4:** the accumulation ``provenance`` trail is stored as a JSONB array;
  ``update_node`` (extend) replaces content/embedding/trail in place — no
  ``superseded_by`` chunk-chain.
- **scope:** every method filters ``owner_id`` in ``WHERE`` (correctness) AND
  relies on RLS (the migration's direct ``owner_id`` policy) for tenant isolation
  in prod. Dim mismatches fail fast at the boundary as ``GraphIndexError``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from persona.graph._schema import (
    EMBEDDING_DIM,
    graph_edges,
    graph_entities,
    graph_node_entities,
    graph_nodes,
)
from persona.graph.errors import GraphIndexError
from persona.graph.models import (
    CanonicalEntity,
    ConceptNode,
    EntityAlias,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

__all__ = ["PostgresGraphBackend"]

_DEFAULT_EMBEDDING_MODEL = "bge-small-en-v1.5"


class PostgresGraphBackend:
    """Transport over a Postgres + pgvector engine for the graph (D-K0-3).

    One instance owns one engine/pool; rows are partitioned by ``owner_id``. The
    caller (the API composition root, or a test) owns the engine lifecycle and any
    RLS ``set_config`` plumbing — the transport just issues parameterised
    statements. Vectors are supplied pre-computed (the store embeds once).
    """

    def __init__(self, *, engine: Engine) -> None:
        self._engine = engine

    # ===== nodes ===========================================================

    def insert_node(
        self,
        owner_id: str,
        node: ConceptNode,
        embedding: Sequence[float],
        *,
        embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
    ) -> int:
        """Insert a node row; return the Postgres-assigned ``surrogate`` (index key)."""
        row = self._node_to_row(owner_id, node, embedding, embedding_model)
        stmt = pg_insert(graph_nodes).values(**row).returning(graph_nodes.c.surrogate)
        with self._engine.begin() as conn:
            surrogate = conn.execute(stmt).scalar_one()
        return int(surrogate)

    def insert_node_if_absent(
        self,
        owner_id: str,
        node: ConceptNode,
        embedding: Sequence[float],
        *,
        embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
    ) -> int | None:
        """Insert a node only if its id is free; return the surrogate, or ``None`` on conflict.

        The race-safe get-or-create primitive (Spec K6): ``ON CONFLICT (id) DO
        NOTHING`` so two concurrent inserts of the same reserved id (the self node)
        collapse to a single row — the winner gets a ``surrogate``, the loser gets
        ``None`` (and re-reads the winner). Distinct from :meth:`insert_node`, which
        assumes a fresh id and raises on a duplicate.
        """
        row = self._node_to_row(owner_id, node, embedding, embedding_model)
        stmt = (
            pg_insert(graph_nodes)
            .values(**row)
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(graph_nodes.c.surrogate)
        )
        with self._engine.begin() as conn:
            surrogate = conn.execute(stmt).scalar_one_or_none()
        return None if surrogate is None else int(surrogate)

    def update_node(
        self,
        owner_id: str,
        node: ConceptNode,
        embedding: Sequence[float],
        *,
        embedding_model: str = _DEFAULT_EMBEDDING_MODEL,
    ) -> int | None:
        """Replace a node's mutable fields in place (the extend path, D-K0-4).

        Keeps ``id``/``owner_id``/``surrogate``/``created_at``; refreshes content,
        embedding, metadata, wellbeing tag, content_hash, and the provenance trail.
        Returns the ``surrogate`` (for index replace) or ``None`` if absent.
        """
        self._check_dim(embedding, node.id)
        stmt = (
            update(graph_nodes)
            .where(graph_nodes.c.id == node.id, graph_nodes.c.owner_id == owner_id)
            .values(
                node_kind=str(node.node_kind),
                concept_name=node.concept_name,
                content=node.content,
                metadata=dict(node.metadata),
                wellbeing_category=node.wellbeing_category,
                embedding=list(embedding),
                embedding_model=embedding_model,
                content_hash=node.content_hash,
                provenance=[p.model_dump(mode="json") for p in node.provenance],
            )
            .returning(graph_nodes.c.surrogate)
        )
        with self._engine.begin() as conn:
            surrogate = conn.execute(stmt).scalar_one_or_none()
        return None if surrogate is None else int(surrogate)

    def get_node(self, owner_id: str, node_id: str) -> ConceptNode | None:
        stmt = select(graph_nodes).where(
            graph_nodes.c.id == node_id, graph_nodes.c.owner_id == owner_id
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().one_or_none()
        return None if row is None else self._row_to_node(dict(row))

    def get_nodes_by_surrogates(
        self, owner_id: str, surrogates: Sequence[int]
    ) -> dict[int, ConceptNode]:
        """Hydrate nodes for index-search results (surrogate → node)."""
        if not surrogates:
            return {}
        stmt = select(graph_nodes).where(
            graph_nodes.c.owner_id == owner_id,
            graph_nodes.c.surrogate.in_(list(surrogates)),
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return {int(r["surrogate"]): self._row_to_node(dict(r)) for r in rows}

    def surrogate_for(self, owner_id: str, node_id: str) -> int | None:
        stmt = select(graph_nodes.c.surrogate).where(
            graph_nodes.c.id == node_id, graph_nodes.c.owner_id == owner_id
        )
        with self._engine.connect() as conn:
            val = conn.execute(stmt).scalar_one_or_none()
        return None if val is None else int(val)

    def surrogates_for_owner(self, owner_id: str) -> list[int]:
        """All of the user's node surrogates — the dense-search allowlist (criterion 6)."""
        stmt = select(graph_nodes.c.surrogate).where(graph_nodes.c.owner_id == owner_id)
        with self._engine.connect() as conn:
            return [int(r[0]) for r in conn.execute(stmt)]

    def surrogates_for_nodes(self, owner_id: str, node_ids: Sequence[str]) -> list[int]:
        """Surrogates for the given node-ids, owner-scoped (the K4-subtraction allowlist)."""
        if not node_ids:
            return []
        stmt = select(graph_nodes.c.surrogate).where(
            graph_nodes.c.owner_id == owner_id, graph_nodes.c.id.in_(list(node_ids))
        )
        with self._engine.connect() as conn:
            return [int(r[0]) for r in conn.execute(stmt)]

    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]:
        """The owner's wellbeing-tagged nodes (K4 gate-eligible flagged set; criterion 5).

        Every node whose ``wellbeing_category`` is set, hydrated with its full
        provenance trail so K4 can compute recency. Owner-scoped (RLS in prod).
        """
        stmt = select(graph_nodes).where(
            graph_nodes.c.owner_id == owner_id,
            graph_nodes.c.wellbeing_category.isnot(None),
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r)) for r in rows]

    def node_ids_for_owner(self, owner_id: str) -> list[str]:
        """All of the owner's durable node ids — the K4 positive-allowlist enumeration."""
        stmt = select(graph_nodes.c.id).where(graph_nodes.c.owner_id == owner_id)
        with self._engine.connect() as conn:
            return [str(r[0]) for r in conn.execute(stmt)]

    def delete_node(self, owner_id: str, node_id: str) -> int | None:
        """Delete a node (edges cascade); return its ``surrogate`` for index removal."""
        stmt = (
            delete(graph_nodes)
            .where(graph_nodes.c.id == node_id, graph_nodes.c.owner_id == owner_id)
            .returning(graph_nodes.c.surrogate)
        )
        with self._engine.begin() as conn:
            surrogate = conn.execute(stmt).scalar_one_or_none()
        return None if surrogate is None else int(surrogate)

    # ===== dense + sparse retrieval (the K1 legs' SQL) =====================

    def dense_query(
        self,
        owner_id: str,
        query_vector: Sequence[float],
        top_k: int,
        *,
        allowed_surrogates: Sequence[int] | None = None,
    ) -> list[ConceptNode]:
        """Exact pgvector cosine search, allowlist-scoped (the pgvector dense leg).

        ``allowed_surrogates=None`` → all the user's nodes; an empty sequence →
        no candidates (returns ``[]``) — isolation never relies on ``None``
        (design call #3). Returned nodes carry ``distance``.
        """
        self._check_dim(query_vector, "<query>")
        if allowed_surrogates is not None and len(allowed_surrogates) == 0:
            return []
        q_vec = list(query_vector)
        distance = graph_nodes.c.embedding.cosine_distance(q_vec).label("distance")
        stmt = select(graph_nodes, distance).where(graph_nodes.c.owner_id == owner_id)
        if allowed_surrogates is not None:
            stmt = stmt.where(graph_nodes.c.surrogate.in_(list(allowed_surrogates)))
        stmt = stmt.order_by(distance).limit(top_k)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r), distance=float(r["distance"])) for r in rows]

    def fts_query(self, owner_id: str, query: str, top_k: int) -> list[ConceptNode]:
        """Postgres FTS (BM25-class) over node content — the K1 sparse leg (crit 3)."""
        tsquery = func.websearch_to_tsquery("english", query)
        rank = func.ts_rank(graph_nodes.c.fts, tsquery)
        stmt = (
            select(graph_nodes)
            .where(graph_nodes.c.owner_id == owner_id, graph_nodes.c.fts.op("@@")(tsquery))
            .order_by(rank.desc())
            .limit(top_k)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r)) for r in rows]

    def count_nodes(self, owner_id: str) -> int:
        """Number of the user's ``::node::`` fact nodes (the next ``make_node_id`` index).

        Excludes the K6 ``SELF`` node (Spec K6): it carries a reserved ``::self`` id
        outside the monotonic ``::node::{index}`` scheme, so counting it would push
        fact ids past their true index (harmless gaps, but the two id schemes are
        cleaner kept independent). Only fact-kind nodes participate in this count.
        """
        stmt = (
            select(func.count())
            .select_from(graph_nodes)
            .where(
                graph_nodes.c.owner_id == owner_id,
                graph_nodes.c.node_kind != str(NodeKind.SELF),
            )
        )
        with self._engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    # ===== embeddings (rerank source + rebuild) ===========================

    def get_embeddings(self, owner_id: str, node_ids: Sequence[str]) -> dict[str, list[float]]:
        """Durable float32 embeddings by node-id (the K1 rerank source, crit 2)."""
        if not node_ids:
            return {}
        stmt = select(graph_nodes.c.id, graph_nodes.c.embedding).where(
            graph_nodes.c.owner_id == owner_id, graph_nodes.c.id.in_(list(node_ids))
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return {str(r[0]): [float(x) for x in r[1]] for r in rows}

    def embeddings_by_surrogate(self, surrogates: Sequence[int]) -> dict[int, list[float]]:
        """Float32 embeddings keyed by surrogate — the turbovec exact-rerank source (T7).

        Owner-agnostic: the surrogates are already the user's (the ANN allowlist was
        the user's set); RLS scopes in prod.
        """
        if not surrogates:
            return {}
        stmt = select(graph_nodes.c.surrogate, graph_nodes.c.embedding).where(
            graph_nodes.c.surrogate.in_(list(surrogates))
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return {int(r[0]): [float(x) for x in r[1]] for r in rows}

    def iter_embeddings(self, owner_id: str) -> list[tuple[int, list[float]]]:
        """All ``(surrogate, embedding)`` for the user — the index-rebuild source (crit 9)."""
        stmt = select(graph_nodes.c.surrogate, graph_nodes.c.embedding).where(
            graph_nodes.c.owner_id == owner_id
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [(int(r[0]), [float(x) for x in r[1]]) for r in rows]

    # ===== edges ===========================================================

    def upsert_edge(self, owner_id: str, link: TypedLink) -> None:
        """Insert or replace a typed edge by id (idempotent link assertion, D-K0-2)."""
        row = self._link_to_row(owner_id, link)
        stmt = pg_insert(graph_edges).values(**row)
        update_cols = {c.name: stmt.excluded[c.name] for c in graph_edges.c if c.name != "id"}
        stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_cols)
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def delete_links_from(self, owner_id: str, node_id: str, link_type: LinkType) -> None:
        """Delete a node's OUTGOING edges of one type (semantic-link re-eval on extend, D-K0-2)."""
        stmt = delete(graph_edges).where(
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.src_node_id == node_id,
            graph_edges.c.link_type == str(link_type),
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def neighbors(
        self,
        owner_id: str,
        node_id: str,
        *,
        link_types: set[LinkType] | None = None,
        limit: int,
    ) -> list[tuple[TypedLink, ConceptNode]]:
        """Typed one-hop traversal in BOTH directions (K1 §2; crit 7).

        Returns ``(edge, neighbour-node)``. ``link_types=None`` traverses all four
        types; bounded by ``limit`` (K1-D-3 anti-flooding).
        """
        conds = [
            graph_edges.c.owner_id == owner_id,
            or_(graph_edges.c.src_node_id == node_id, graph_edges.c.dst_node_id == node_id),
        ]
        if link_types is not None:
            conds.append(graph_edges.c.link_type.in_([str(lt) for lt in link_types]))
        edge_stmt = (
            select(graph_edges).where(*conds).order_by(graph_edges.c.created_at).limit(limit)
        )
        with self._engine.connect() as conn:
            edge_rows = [dict(r) for r in conn.execute(edge_stmt).mappings().all()]
            other_ids = [
                r["dst_node_id"] if r["src_node_id"] == node_id else r["src_node_id"]
                for r in edge_rows
            ]
            nodes: dict[str, ConceptNode] = {}
            if other_ids:
                node_rows = (
                    conn.execute(
                        select(graph_nodes).where(
                            graph_nodes.c.owner_id == owner_id, graph_nodes.c.id.in_(other_ids)
                        )
                    )
                    .mappings()
                    .all()
                )
                nodes = {str(r["id"]): self._row_to_node(dict(r)) for r in node_rows}
        out: list[tuple[TypedLink, ConceptNode]] = []
        for r, other in zip(edge_rows, other_ids, strict=True):
            neighbour = nodes.get(other)
            if neighbour is not None:
                out.append((self._row_to_link(r), neighbour))
        return out

    # ===== entities (the canonical registry) ==============================

    def insert_entity(
        self,
        owner_id: str,
        entity: CanonicalEntity,
        name_embedding: Sequence[float],
        *,
        embedding_model: str = _DEFAULT_EMBEDDING_MODEL,  # noqa: ARG002 — symmetry; not stored
    ) -> None:
        self._check_dim(name_embedding, entity.id)
        row = {
            "id": entity.id,
            "owner_id": owner_id,
            "canonical_name": entity.canonical_name,
            "aliases": [a.model_dump(mode="json") for a in entity.aliases],
            "name_embedding": list(name_embedding),
            "provenance": None
            if entity.provenance is None
            else entity.provenance.model_dump(mode="json"),
            "created_at": entity.created_at,
        }
        with self._engine.begin() as conn:
            conn.execute(pg_insert(graph_entities).values(**row))

    def get_entity(self, owner_id: str, entity_id: str) -> CanonicalEntity | None:
        stmt = select(graph_entities).where(
            graph_entities.c.id == entity_id, graph_entities.c.owner_id == owner_id
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().one_or_none()
        return None if row is None else self._row_to_entity(dict(row))

    def add_alias(self, owner_id: str, entity_id: str, alias: EntityAlias) -> None:
        """Append a confirmed surface form to an entity's alias set (K2's MERGE callback)."""
        existing = self.get_entity(owner_id, entity_id)
        if existing is None:
            return
        merged = [*existing.aliases, alias]
        stmt = (
            update(graph_entities)
            .where(graph_entities.c.id == entity_id, graph_entities.c.owner_id == owner_id)
            .values(aliases=[a.model_dump(mode="json") for a in merged])
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def find_entity_by_text(self, owner_id: str, normalized_name: str) -> CanonicalEntity | None:
        """Exact (case/whitespace-insensitive) lookup by canonical name OR alias surface.

        The deterministic resolver's first step (research §2 "exact alias lookup →
        fuzzy → embedding"): a registered alias mention ("my doctor") is NOT near
        the entity's name embedding, so it can't be found by cosine — only by this
        exact-text match. ``normalized_name`` is the caller's
        lower+whitespace-collapsed mention. Matched case-insensitively here via
        ``lower(btrim(...))`` over the canonical name and every alias surface.

        v0.1 uses a correlated ``jsonb_array_elements`` scan over the per-user
        alias arrays (small N per owner); a normalized-alias GIN index is the
        v0.2 push-down if registries grow large.
        """
        sql = text(
            "SELECT id, owner_id, canonical_name, aliases, provenance, created_at "
            "FROM graph_entities "
            "WHERE owner_id = :owner AND ("
            "  lower(btrim(canonical_name)) = :norm "
            "  OR EXISTS (SELECT 1 FROM jsonb_array_elements(aliases) AS e "
            "             WHERE lower(btrim(e->>'surface')) = :norm)"
            ") LIMIT 1"
        )
        params = {"owner": owner_id, "norm": normalized_name}
        with self._engine.connect() as conn:
            row = conn.execute(sql, params).mappings().one_or_none()
        return None if row is None else self._row_to_entity(dict(row))

    def count_entities(self, owner_id: str) -> int:
        """Number of canonical entities for the user (the next ``make_entity_id`` index)."""
        stmt = (
            select(func.count())
            .select_from(graph_entities)
            .where(graph_entities.c.owner_id == owner_id)
        )
        with self._engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    def entity_candidates(
        self, owner_id: str, name_vector: Sequence[float], top_k: int
    ) -> list[tuple[CanonicalEntity, float]]:
        """Cosine candidate-gen over canonical-name embeddings (the resolve blocking step, T5)."""
        self._check_dim(name_vector, "<name>")
        n_vec = list(name_vector)
        distance = graph_entities.c.name_embedding.cosine_distance(n_vec).label("distance")
        stmt = (
            select(graph_entities, distance)
            .where(graph_entities.c.owner_id == owner_id)
            .order_by(distance)
            .limit(top_k)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [(self._row_to_entity(dict(r)), float(r["distance"])) for r in rows]

    # ===== node ↔ entity associations (T6b) ===============================

    def associate_entities(self, owner_id: str, node_id: str, entity_ids: Sequence[str]) -> None:
        """Record that ``node_id`` concerns each entity (idempotent on the PK)."""
        if not entity_ids:
            return
        now = datetime.now(UTC)
        rows = [
            {"owner_id": owner_id, "node_id": node_id, "entity_id": eid, "created_at": now}
            for eid in dict.fromkeys(entity_ids)  # de-dupe, preserve order
        ]
        stmt = (
            pg_insert(graph_node_entities)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["node_id", "entity_id"])
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def nodes_for_entity(self, owner_id: str, entity_id: str) -> list[ConceptNode]:
        """All nodes that concern ``entity_id`` (criterion 2 — the entity thread)."""
        stmt = (
            select(graph_nodes)
            .join(
                graph_node_entities,
                (graph_node_entities.c.node_id == graph_nodes.c.id)
                & (graph_node_entities.c.owner_id == graph_nodes.c.owner_id),
            )
            .where(
                graph_node_entities.c.owner_id == owner_id,
                graph_node_entities.c.entity_id == entity_id,
            )
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r)) for r in rows]

    def entities_for_node(self, owner_id: str, node_id: str) -> list[str]:
        """The canonical-entity ids a node concerns."""
        stmt = select(graph_node_entities.c.entity_id).where(
            graph_node_entities.c.owner_id == owner_id,
            graph_node_entities.c.node_id == node_id,
        )
        with self._engine.connect() as conn:
            return [str(r[0]) for r in conn.execute(stmt)]

    def entity_neighbors(self, owner_id: str, node_id: str) -> list[ConceptNode]:
        """Sibling nodes sharing ≥1 entity with ``node_id`` (on-the-fly ENTITY traversal).

        Resolves entity links through the association table (node→entities→sibling
        nodes) WITHOUT materialised node↔node entity edges — no O(n²) explosion.
        Excludes ``node_id`` itself; distinct.
        """
        mine = graph_node_entities.alias("mine")
        theirs = graph_node_entities.alias("theirs")
        stmt = (
            select(graph_nodes)
            .distinct()
            .join(theirs, theirs.c.node_id == graph_nodes.c.id)
            .join(mine, mine.c.entity_id == theirs.c.entity_id)
            .where(
                mine.c.owner_id == owner_id,
                theirs.c.owner_id == owner_id,
                graph_nodes.c.owner_id == owner_id,
                mine.c.node_id == node_id,
                theirs.c.node_id != node_id,
            )
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r)) for r in rows]

    # ===== row <-> model ===================================================

    @staticmethod
    def _check_dim(vector: Sequence[float], ref: str) -> None:
        if len(vector) != EMBEDDING_DIM:
            raise GraphIndexError(
                "embedding dimension mismatch",
                context={"expected": str(EMBEDDING_DIM), "got": str(len(vector)), "ref": ref},
            )

    def _node_to_row(
        self, owner_id: str, node: ConceptNode, embedding: Sequence[float], embedding_model: str
    ) -> dict[str, Any]:
        self._check_dim(embedding, node.id)
        return {
            "id": node.id,
            "owner_id": owner_id,
            "node_kind": str(node.node_kind),
            "concept_name": node.concept_name,
            "content": node.content,
            "metadata": dict(node.metadata),
            "wellbeing_category": node.wellbeing_category,
            "embedding": list(embedding),
            "embedding_model": embedding_model,
            "content_hash": node.content_hash,
            "provenance": [p.model_dump(mode="json") for p in node.provenance],
            "created_at": node.created_at,
        }

    def _row_to_node(self, row: dict[str, Any], *, distance: float | None = None) -> ConceptNode:
        trail = tuple(NodeProvenance.model_validate(p) for p in row["provenance"])
        return ConceptNode(
            id=str(row["id"]),
            node_kind=NodeKind(row["node_kind"]),
            concept_name=str(row["concept_name"]),
            content=str(row["content"]),
            metadata=_as_str_dict(row.get("metadata")),
            wellbeing_category=row.get("wellbeing_category"),
            distance=distance,
            content_hash=str(row["content_hash"]),
            provenance=trail,
            created_at=_as_utc(row["created_at"]),
        )

    def _link_to_row(self, owner_id: str, link: TypedLink) -> dict[str, Any]:
        return {
            "id": link.id,
            "owner_id": owner_id,
            "src_node_id": link.src_node_id,
            "dst_node_id": link.dst_node_id,
            "link_type": str(link.link_type),
            "weight": link.weight,
            "provenance": None
            if link.provenance is None
            else link.provenance.model_dump(mode="json"),
            "created_at": link.created_at,
        }

    def _row_to_link(self, row: dict[str, Any]) -> TypedLink:
        prov = row.get("provenance")
        return TypedLink(
            id=str(row["id"]),
            src_node_id=str(row["src_node_id"]),
            dst_node_id=str(row["dst_node_id"]),
            link_type=LinkType(row["link_type"]),
            weight=None if row.get("weight") is None else float(row["weight"]),
            provenance=None if prov is None else NodeProvenance.model_validate(prov),
            created_at=_as_utc(row["created_at"]),
        )

    def _row_to_entity(self, row: dict[str, Any]) -> CanonicalEntity:
        prov = row.get("provenance")
        aliases = tuple(EntityAlias.model_validate(a) for a in (row.get("aliases") or []))
        return CanonicalEntity(
            id=str(row["id"]),
            canonical_name=str(row["canonical_name"]),
            aliases=aliases,
            provenance=None if prov is None else NodeProvenance.model_validate(prov),
            created_at=_as_utc(row["created_at"]),
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_str_dict(value: Any) -> dict[str, str]:  # noqa: ANN401 — JSONB comes back as Any
    if value is None:
        return {}
    return {str(k): str(v) for k, v in dict(value).items()}
