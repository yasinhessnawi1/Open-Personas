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

from sqlalchemy import BigInteger, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from persona.graph._schema import (
    EMBEDDING_DIM,
    graph_consolidation_markers,
    graph_edges,
    graph_entities,
    graph_node_entities,
    graph_node_versions,
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
    NodeVersion,
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
                # Advance the dirty-neighbourhood watermark (K7-D-4). salience /
                # last_evidence_epoch / merged_into are lifecycle state and are
                # deliberately NOT touched by a content update.
                updated_at=datetime.now(UTC),
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
        exclude_self: bool = False,
    ) -> list[ConceptNode]:
        """Exact pgvector cosine search, allowlist-scoped (the pgvector dense leg).

        ``allowed_surrogates=None`` → all the user's nodes; an empty sequence →
        no candidates (returns ``[]``) — isolation never relies on ``None``
        (design call #3). Returned nodes carry ``distance``.

        **Consolidated nodes are never returned** (``merged_into IS NULL``, K7-D-4 /
        K7-D-X-read-surface — a merged node left the retrievable set). ``exclude_self``
        additionally drops the ``NodeKind.SELF`` anchor (K7-D-7): the merge engine
        passes it for the extend-target top-1 so the anchor is never an extend/evolve
        target; semantic-link wiring leaves it off (SELF may still be a link end).
        """
        self._check_dim(query_vector, "<query>")
        if allowed_surrogates is not None and len(allowed_surrogates) == 0:
            return []
        q_vec = list(query_vector)
        distance = graph_nodes.c.embedding.cosine_distance(q_vec).label("distance")
        stmt = select(graph_nodes, distance).where(
            graph_nodes.c.owner_id == owner_id,
            graph_nodes.c.merged_into.is_(None),
        )
        if exclude_self:
            stmt = stmt.where(graph_nodes.c.node_kind != str(NodeKind.SELF))
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

    def seed_nodes(self, owner_id: str, *, limit: int) -> list[ConceptNode]:
        """The first-paint seed window: the owner's most-recent nodes (K5-D-8, B1-refined).

        Ordered by ``created_at`` DESC — "what you've been thinking about lately" — served by
        the ``(owner_id, created_at)`` index as a bounded backward index-scan (O(limit), flat
        regardless of graph size). Recency is the v1 product choice (the seed is recoverable —
        the user explores outward); the degree-"anchor" alternative is a recorded planned option
        (a materialised degree column on the write path) — see decisions.md B1. Degree-for-sizing
        stays free per-window via :meth:`edges_among`.
        """
        if limit <= 0:
            return []
        stmt = (
            select(graph_nodes)
            .where(graph_nodes.c.owner_id == owner_id)
            .order_by(graph_nodes.c.created_at.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r)) for r in rows]

    def edges_among(self, owner_id: str, node_ids: Sequence[str]) -> list[TypedLink]:
        """The stored typed edges whose BOTH endpoints lie in ``node_ids``.

        The induced sub-graph of a window (K5-D-2) in one query — semantic,
        temporal, and causal edges (which are materialised in ``graph_edges``).
        ENTITY links are resolved on-the-fly (D-K0-9) and are NOT returned here;
        the detail panel surfaces them via :meth:`neighbors`. RLS-scoped read.
        """
        ids = list(node_ids)
        if len(ids) < 2:  # noqa: PLR2004 — an edge needs two distinct endpoints in the set
            return []
        stmt = select(graph_edges).where(
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.src_node_id.in_(ids),
            graph_edges.c.dst_node_id.in_(ids),
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_link(dict(r)) for r in rows]

    def next_node_index(self, owner_id: str) -> int:
        """The next collision-free ``make_node_id`` index for the owner (Spec K7, K7-D-9).

        **Fixes the latent delete→create id collision** the Phase-1 audit flagged:
        ``count_nodes`` (create → 0,1,2; delete id 1; count → 2; next create reuses
        index 2 → collides with the live node 2). The fix is ``MAX(live index) + 1``:
        we always allocate strictly above every LIVE ``::node::`` id, so a deleted
        id's number is only ever reused when it is genuinely gone — two live nodes can
        never share an id (also PK-guarded). ``split_part(id, '::', -1)`` reads the
        trailing index segment (robust to an ``owner_id`` containing ``::``). The K6
        ``{owner}::self`` anchor is the only id excluded — by its SHAPE (see the
        WHERE clause), never by node_kind (R4-C1-12, which cost dead-lettered jobs).

        **K5 coordination (state.md cross-spec item 1):** K7 implemented this
        allocator; R4 corrected the exclusion. One canonical allocator, no double-fix.
        """
        stmt = select(
            func.coalesce(
                func.max(func.split_part(graph_nodes.c.id, "::", -1).cast(BigInteger)),
                -1,
            )
            + 1
        ).where(
            graph_nodes.c.owner_id == owner_id,
            # Exclude by ID SHAPE, not node_kind (R4-C1-12). Only the K6 anchor
            # ``{owner}::self`` must be skipped (its trailing segment is the literal
            # ``self`` — un-castable). Excluding by node_kind was WRONG: a SELF-KIND
            # node can carry a numeric ``{owner}::node::NNNNN`` id (synthesis/entity
            # mis-classifying a person as self), and dropping it from the MAX made the
            # allocator re-hand-out its index → INSERT PK collision that dead-lettered
            # every synthesis job. ``::node::`` ids have ``node`` as the 2nd-to-last
            # segment; the ``::self`` anchor does not — so this counts every numeric
            # node (any kind) and only ever skips the anchor.
            func.split_part(graph_nodes.c.id, "::", -2) == "node",
        )
        with self._engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    def is_merged(self, owner_id: str, node_id: str) -> bool:
        """Whether ``node_id`` has been consolidated into a canonical (K7-D-4/-7).

        A merged node must never be an extend/evolve target; ``get_node`` still
        returns it (K5 inspection / restore), so the merge engine checks this before
        evolving an explicitly-named target.
        """
        stmt = select(graph_nodes.c.merged_into).where(
            graph_nodes.c.id == node_id, graph_nodes.c.owner_id == owner_id
        )
        with self._engine.connect() as conn:
            val = conn.execute(stmt).scalar_one_or_none()
        return val is not None

    # ===== node versions (Spec K7, K7-D-1) ================================

    def snapshot_node_version(
        self,
        owner_id: str,
        node_id: str,
        *,
        valid_at: datetime,
        invalid_at: datetime,
        invalidated_by: str | None,
    ) -> int | None:
        """Preserve the node's CURRENT state as a window-closed version row (K7-D-1).

        Reads the live ``graph_nodes`` row (content + **embedding** + metadata +
        wellbeing + content_hash + provenance-trail snapshot) and inserts an immutable
        version row window-closed at ``[valid_at, invalid_at]`` — the §0-honest
        preservation the caller runs BEFORE overwriting the node with the superseding
        account. Nothing is destroyed; the prior embedding is byte-exact for restore.
        ``invalidated_at`` (bookkeeping, K7-D-10) records the SYSTEM close moment.
        Returns the assigned ``version_key`` (the ``superseded_version_id`` source),
        or ``None`` if the node vanished.
        """
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    select(graph_nodes).where(
                        graph_nodes.c.id == node_id, graph_nodes.c.owner_id == owner_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            ins = (
                pg_insert(graph_node_versions)
                .values(
                    owner_id=owner_id,
                    node_id=node_id,
                    node_kind=row["node_kind"],
                    concept_name=row["concept_name"],
                    content=row["content"],
                    metadata=dict(row["metadata"]),
                    wellbeing_category=row["wellbeing_category"],
                    embedding=list(row["embedding"]),
                    embedding_model=row["embedding_model"],
                    content_hash=row["content_hash"],
                    provenance=row["provenance"],
                    valid_at=valid_at,
                    invalid_at=invalid_at,
                    invalidated_by=invalidated_by,
                    invalidated_at=datetime.now(UTC),
                )
                .returning(graph_node_versions.c.version_key)
            )
            return int(conn.execute(ins).scalar_one())

    def latest_version_invalid_at(self, owner_id: str, node_id: str) -> datetime | None:
        """The max ``invalid_at`` across a node's versions — when its CURRENT account began.

        ``None`` when the node has never been superseded (its current account's
        world-time origin is then the creation provenance's ``written_at``).
        """
        stmt = select(func.max(graph_node_versions.c.invalid_at)).where(
            graph_node_versions.c.owner_id == owner_id,
            graph_node_versions.c.node_id == node_id,
        )
        with self._engine.connect() as conn:
            val = conn.execute(stmt).scalar_one_or_none()
        return None if val is None else _as_utc(val)

    def get_node_versions(self, owner_id: str, node_id: str) -> list[NodeVersion]:
        """A node's window-closed prior accounts, oldest→newest (K7-D-1 point-in-time)."""
        stmt = (
            select(graph_node_versions)
            .where(
                graph_node_versions.c.owner_id == owner_id,
                graph_node_versions.c.node_id == node_id,
            )
            .order_by(graph_node_versions.c.valid_at, graph_node_versions.c.version_key)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_version(dict(r)) for r in rows]

    def read_version_for_restore(
        self, owner_id: str, version_id: str
    ) -> tuple[NodeVersion, list[float]] | None:
        """A version row + its preserved float32 embedding (the byte-exact restore source)."""
        try:
            version_key = int(version_id)
        except (TypeError, ValueError):
            return None
        stmt = select(graph_node_versions).where(
            graph_node_versions.c.version_key == version_key,
            graph_node_versions.c.owner_id == owner_id,
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().one_or_none()
        if row is None:
            return None
        return self._row_to_version(dict(row)), [float(x) for x in row["embedding"]]

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
        """Insert or replace the OPEN edge for a fact-id (idempotent assertion, D-K0-2, K7-D-1).

        The conflict target is the partial unique index ``(id) WHERE invalid_at IS
        NULL`` — one open edge per fact. A window-closed edge with the same ``id``
        coexists as a separate row (it is outside the partial index), so re-asserting
        a fact after it was closed opens a NEW open row (K7-D-1) without touching the
        closed history. ``edge_key`` (IDENTITY) is never written and never updated.
        """
        row = self._link_to_row(owner_id, link)
        stmt = pg_insert(graph_edges).values(**row)
        update_cols = {
            c.name: stmt.excluded[c.name] for c in graph_edges.c if c.name not in ("id", "edge_key")
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            index_where=graph_edges.c.invalid_at.is_(None),
            set_=update_cols,
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def delete_links_from(self, owner_id: str, node_id: str, link_type: LinkType) -> None:
        """Delete a node's OUTGOING edges of one type (semantic-link re-eval on extend, D-K0-2).

        Only ever called with ``LinkType.SEMANTIC`` — semantic wiring is derived and
        recomputable (K7-D-1.3), so it is hard-deleted-and-reformed on evolve, NOT
        window-closed. Assertion edges (temporal/causal) are never touched here.
        """
        stmt = delete(graph_edges).where(
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.src_node_id == node_id,
            graph_edges.c.link_type == str(link_type),
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def delete_links_incident(self, owner_id: str, node_id: str, link_type: LinkType) -> None:
        """Delete a node's edges of one type in BOTH directions (K5-D-7 correction re-eval).

        On a user correction the node's embedding changes, so every semantic edge
        *incident* to it is stale — including INBOUND ones (``dst = node``) that a
        neighbour scored against the OLD embedding (the half-edge a one-sided
        ``delete_links_from`` would leave behind). Clearing both directions, then
        re-forming the node's outgoing links from the fresh embedding, keeps the
        symmetric semantic neighbourhood honest. RLS-scoped.
        """
        stmt = delete(graph_edges).where(
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.link_type == str(link_type),
            or_(
                graph_edges.c.src_node_id == node_id,
                graph_edges.c.dst_node_id == node_id,
            ),
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def invalidate_edge(
        self,
        owner_id: str,
        edge_id: str,
        *,
        invalidated_by: str | None,
        invalid_at: datetime | None = None,
    ) -> bool:
        """Window-close the OPEN edge for a fact-id — never delete (Spec K7, K7-D-1, §0).

        Sets ``invalid_at`` (world-time end), ``invalidated_by`` (provenance-of-closure)
        and ``invalidated_at`` (system bookkeeping, K7-D-10) on the single open row for
        ``edge_id`` (owner-scoped, ``invalid_at IS NULL``). The row PERSISTS — a
        subsequent :meth:`upsert_edge` of the same fact opens a NEW row sharing the
        ``id`` (the two coexist, one closed one open). Idempotent: closing an
        already-closed / absent / other-owner edge affects zero rows and returns
        ``False``. ``invalid_at`` defaults to now; it is clamped strictly after
        ``valid_at`` (the DDL window CHECK) so an immediate open→close stays valid.
        """
        end_ts = invalid_at if invalid_at is not None else datetime.now(UTC)
        closed = func.greatest(end_ts, graph_edges.c.valid_at + text("interval '1 microsecond'"))
        stmt = (
            update(graph_edges)
            .where(
                graph_edges.c.owner_id == owner_id,
                graph_edges.c.id == edge_id,
                graph_edges.c.invalid_at.is_(None),
            )
            .values(
                invalid_at=closed,
                invalidated_by=invalidated_by,
                invalidated_at=datetime.now(UTC),
            )
            .returning(graph_edges.c.edge_key)
        )
        with self._engine.begin() as conn:
            return conn.execute(stmt).scalar_one_or_none() is not None

    def neighbors(
        self,
        owner_id: str,
        node_id: str,
        *,
        link_types: set[LinkType] | None = None,
        limit: int,
        as_of: datetime | None = None,
    ) -> list[tuple[TypedLink, ConceptNode]]:
        """Typed one-hop traversal in BOTH directions (K1 §2; crit 7 / K7-D-1).

        Returns ``(edge, neighbour-node)``. ``link_types=None`` traverses all four
        types; bounded by ``limit`` (K1-D-3 anti-flooding). **Traverses OPEN edges by
        default** (``invalid_at IS NULL``) — a window-closed edge is invisible to
        current-state reads. With an ``as_of`` world-time, returns exactly the edges
        whose window covers that instant (``valid_at <= as_of AND (invalid_at IS NULL
        OR invalid_at > as_of)``, the Zep predicate) — a closed edge is visible inside
        its window, invisible after.
        """
        conds = [
            graph_edges.c.owner_id == owner_id,
            or_(graph_edges.c.src_node_id == node_id, graph_edges.c.dst_node_id == node_id),
        ]
        if link_types is not None:
            conds.append(graph_edges.c.link_type.in_([str(lt) for lt in link_types]))
        if as_of is None:
            conds.append(graph_edges.c.invalid_at.is_(None))
        else:
            conds.append(graph_edges.c.valid_at <= as_of)
            conds.append(or_(graph_edges.c.invalid_at.is_(None), graph_edges.c.invalid_at > as_of))
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
                graph_nodes.c.merged_into.is_(None),  # merged members leave the thread (K7-D-4)
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
                graph_nodes.c.merged_into.is_(None),  # merged siblings are invisible (K7-D-4)
            )
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r)) for r in rows]

    # ===== consolidation (Spec K7, K7-D-4) ================================

    def get_marker(self, owner_id: str) -> tuple[int, datetime | None]:
        """Get-or-create the per-owner consolidation marker → ``(current_epoch, watermark)``."""
        ins = (
            pg_insert(graph_consolidation_markers)
            .values(owner_id=owner_id)
            .on_conflict_do_nothing(index_elements=["owner_id"])
        )
        stmt = select(
            graph_consolidation_markers.c.current_epoch,
            graph_consolidation_markers.c.watermark,
        ).where(graph_consolidation_markers.c.owner_id == owner_id)
        with self._engine.begin() as conn:
            conn.execute(ins)
            row = conn.execute(stmt).one()
        return int(row[0]), (None if row[1] is None else _as_utc(row[1]))

    def advance_marker(
        self, owner_id: str, *, epoch: int, watermark: datetime, last_run_at: datetime
    ) -> None:
        """Advance the marker after a pass (ordinal epoch + dirty watermark + run time)."""
        stmt = (
            update(graph_consolidation_markers)
            .where(graph_consolidation_markers.c.owner_id == owner_id)
            .values(current_epoch=epoch, watermark=watermark, last_run_at=last_run_at)
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def dirty_candidates(self, owner_id: str, watermark: datetime | None) -> list[ConceptNode]:
        """Consolidation candidates: non-merged, non-SELF, dirty-since-watermark (K7-D-4).

        Ordered by evidence (provenance-trail length) DESC, then oldest ``created_at``,
        then min ``id`` — the deterministic seed/canonical survivorship order (K7-D-4.3).
        """
        conds = [
            graph_nodes.c.owner_id == owner_id,
            graph_nodes.c.merged_into.is_(None),
            graph_nodes.c.node_kind != str(NodeKind.SELF),
        ]
        if watermark is not None:
            conds.append(graph_nodes.c.updated_at > watermark)
        stmt = (
            select(graph_nodes)
            .where(*conds)
            .order_by(
                func.jsonb_array_length(graph_nodes.c.provenance).desc(),
                graph_nodes.c.created_at.asc(),
                graph_nodes.c.id.asc(),
            )
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r)) for r in rows]

    def band_semantic_neighbors(
        self, owner_id: str, node_id: str, *, floor: float, ceil: float
    ) -> list[ConceptNode]:
        """Non-merged, non-SELF nodes joined to ``node_id`` by an OPEN band semantic edge.

        The near-dup cluster substrate (K7-D-3): open ``semantic`` edges whose weight
        is in ``[floor, ceil)`` (the existing config band). The pass then re-verifies
        similarity-to-seed from stored embeddings (K7-D-4.2 star-clustering).
        """
        conds = [
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.link_type == str(LinkType.SEMANTIC),
            graph_edges.c.invalid_at.is_(None),
            graph_edges.c.weight >= floor,
            graph_edges.c.weight < ceil,
            or_(graph_edges.c.src_node_id == node_id, graph_edges.c.dst_node_id == node_id),
        ]
        with self._engine.connect() as conn:
            edge_rows = conn.execute(
                select(graph_edges.c.src_node_id, graph_edges.c.dst_node_id).where(*conds)
            ).all()
            other_ids = {(r[1] if r[0] == node_id else r[0]) for r in edge_rows} - {node_id}
            if not other_ids:
                return []
            node_rows = (
                conn.execute(
                    select(graph_nodes).where(
                        graph_nodes.c.owner_id == owner_id,
                        graph_nodes.c.id.in_(other_ids),
                        graph_nodes.c.merged_into.is_(None),
                        graph_nodes.c.node_kind != str(NodeKind.SELF),
                    )
                )
                .mappings()
                .all()
            )
        return [self._row_to_node(dict(r)) for r in node_rows]

    def open_assertion_edges(self, owner_id: str, node_id: str) -> list[TypedLink]:
        """A node's OPEN temporal/causal edges (both directions) — the mirror source (K7-D-4.4)."""
        conds = [
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.invalid_at.is_(None),
            graph_edges.c.link_type.in_([str(LinkType.TEMPORAL), str(LinkType.CAUSAL)]),
            or_(graph_edges.c.src_node_id == node_id, graph_edges.c.dst_node_id == node_id),
        ]
        with self._engine.connect() as conn:
            rows = conn.execute(select(graph_edges).where(*conds)).mappings().all()
        return [self._row_to_link(dict(r)) for r in rows]

    def close_edges_incident(
        self, owner_id: str, node_id: str, *, invalidated_by: str
    ) -> list[str]:
        """Window-close ALL open edges incident to ``node_id``; return their fact-ids (K7-D-4.4)."""
        now = datetime.now(UTC)
        closed = func.greatest(now, graph_edges.c.valid_at + text("interval '1 microsecond'"))
        stmt = (
            update(graph_edges)
            .where(
                graph_edges.c.owner_id == owner_id,
                graph_edges.c.invalid_at.is_(None),
                or_(graph_edges.c.src_node_id == node_id, graph_edges.c.dst_node_id == node_id),
            )
            .values(invalid_at=closed, invalidated_by=invalidated_by, invalidated_at=now)
            .returning(graph_edges.c.id)
        )
        with self._engine.begin() as conn:
            return [str(r[0]) for r in conn.execute(stmt)]

    def reopen_edges_incident(self, owner_id: str, node_id: str, *, invalidated_by: str) -> None:
        """Re-open the edges incident to ``node_id`` closed under ``invalidated_by`` (unmerge)."""
        stmt = (
            update(graph_edges)
            .where(
                graph_edges.c.owner_id == owner_id,
                graph_edges.c.invalidated_by == invalidated_by,
                or_(graph_edges.c.src_node_id == node_id, graph_edges.c.dst_node_id == node_id),
            )
            .values(invalid_at=None, invalidated_by=None, invalidated_at=None)
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def delete_open_edge(self, owner_id: str, edge_id: str) -> None:
        """Remove the OPEN row for ``edge_id`` (unmerge: drop a consolidation-mirrored edge)."""
        stmt = delete(graph_edges).where(
            graph_edges.c.owner_id == owner_id,
            graph_edges.c.id == edge_id,
            graph_edges.c.invalid_at.is_(None),
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def remove_entity_associations(
        self, owner_id: str, node_id: str, entity_ids: Sequence[str]
    ) -> None:
        """Drop specific ``(node_id, entity_id)`` associations (unmerge: undo the copy)."""
        if not entity_ids:
            return
        stmt = delete(graph_node_entities).where(
            graph_node_entities.c.owner_id == owner_id,
            graph_node_entities.c.node_id == node_id,
            graph_node_entities.c.entity_id.in_(list(entity_ids)),
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def set_merged_into(
        self,
        owner_id: str,
        node_id: str,
        *,
        merged_into: str | None,
        metadata: dict[str, str],
        content_hash: str,
    ) -> None:
        """Set/clear ``merged_into`` + bookkeeping ``metadata``/``content_hash`` (K7-D-4.4).

        Targeted update — does NOT touch ``content``/``embedding`` (§0 byte-equality) or
        ``salience``/``last_evidence_epoch``. Advances ``updated_at`` so a re-scan sees
        the change (idempotency converges: a merged node is then excluded from candidacy).
        """
        stmt = (
            update(graph_nodes)
            .where(graph_nodes.c.id == node_id, graph_nodes.c.owner_id == owner_id)
            .values(
                merged_into=merged_into,
                metadata=metadata,
                content_hash=content_hash,
                updated_at=datetime.now(UTC),
            )
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def merged_members(self, owner_id: str, canonical_id: str) -> list[ConceptNode]:
        """The nodes consolidated into ``canonical_id`` (K5 inspection / unmerge scope)."""
        stmt = select(graph_nodes).where(
            graph_nodes.c.owner_id == owner_id, graph_nodes.c.merged_into == canonical_id
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_node(dict(r)) for r in rows]

    # ===== salience by evidence (Spec K7, K7-D-6) =========================
    # Every statement here moves salience by an ordinal EVIDENCE epoch — NEVER a
    # clock. The no-wallclock structural test asserts none of these methods read
    # now()/datetime.now/func.now. Clamping is SQL GREATEST/LEAST so each is a single
    # statement; ``updated_at`` is deliberately untouched (salience is not a content
    # change → a bump must not make a node a fresh consolidation candidate).

    def current_epoch(self, owner_id: str) -> int:
        """The owner's ordinal evidence epoch (the consolidation-run counter), 0 if unset."""
        stmt = select(graph_consolidation_markers.c.current_epoch).where(
            graph_consolidation_markers.c.owner_id == owner_id
        )
        with self._engine.connect() as conn:
            val = conn.execute(stmt).scalar_one_or_none()
        return 0 if val is None else int(val)

    def bump_salience(
        self,
        owner_id: str,
        node_id: str,
        *,
        delta: float,
        epoch: int,
        floor: float,
        cap: float,
    ) -> None:
        """Move one node's salience by ``delta`` (clamped) and stamp its evidence epoch.

        A corroboration (``+δ_c``) / contradiction (``−δ_x``) event — a single clamped
        UPDATE; ``last_evidence_epoch`` records that this node saw evidence at ``epoch``
        (resetting its disuse clock). SELF is never salient (K7-D-7), so it is excluded.
        """
        clamped = func.greatest(floor, func.least(cap, graph_nodes.c.salience + delta))
        stmt = (
            update(graph_nodes)
            .where(
                graph_nodes.c.owner_id == owner_id,
                graph_nodes.c.id == node_id,
                graph_nodes.c.node_kind != str(NodeKind.SELF),
            )
            .values(salience=clamped, last_evidence_epoch=epoch)
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def record_recall(
        self,
        owner_id: str,
        node_ids: Sequence[str],
        *,
        delta: float,
        epoch: int,
        floor: float,
        cap: float,
    ) -> int:
        """Reinforce recalled nodes (``+δ_r``) in ONE statement (K7-D-6 recall path).

        The off-the-token-path recall bump: a single clamped UPDATE over the injected
        node ids (SELF excluded), stamping ``last_evidence_epoch``. Returns the row
        count. The store wraps this fail-soft; the caller invokes it AFTER K3's
        injection selection, never on the reply stream.
        """
        if not node_ids:
            return 0
        clamped = func.greatest(floor, func.least(cap, graph_nodes.c.salience + delta))
        stmt = (
            update(graph_nodes)
            .where(
                graph_nodes.c.owner_id == owner_id,
                graph_nodes.c.id.in_(list(node_ids)),
                graph_nodes.c.node_kind != str(NodeKind.SELF),
            )
            .values(salience=clamped, last_evidence_epoch=epoch)
        )
        with self._engine.begin() as conn:
            return conn.execute(stmt).rowcount

    def apply_disuse_decay(
        self,
        owner_id: str,
        node_kind: str,
        *,
        current_epoch: int,
        grace: int,
        delta: float,
        floor: float,
    ) -> None:
        """Decay one kind's idle-beyond-grace nodes by one step (K7-D-6 disuse, per pass).

        Idleness is ``current_epoch − last_evidence_epoch`` (ordinal epochs, not time).
        Does NOT reset ``last_evidence_epoch`` — an idle node keeps decaying each pass
        until an evidence event stamps it. Callers skip ``TRAIT`` (``delta == 0``) and
        SELF. Merged nodes are left alone (they are outside retrieval).
        """
        clamped = func.greatest(floor, graph_nodes.c.salience - delta)
        stmt = (
            update(graph_nodes)
            .where(
                graph_nodes.c.owner_id == owner_id,
                graph_nodes.c.node_kind == node_kind,
                graph_nodes.c.merged_into.is_(None),
                (current_epoch - graph_nodes.c.last_evidence_epoch) > grace,
            )
            .values(salience=clamped)
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def get_salience(self, owner_id: str, node_id: str) -> tuple[float, int] | None:
        """``(salience, last_evidence_epoch)`` for a node — the read used by tests/K9."""
        stmt = select(graph_nodes.c.salience, graph_nodes.c.last_evidence_epoch).where(
            graph_nodes.c.owner_id == owner_id, graph_nodes.c.id == node_id
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).one_or_none()
        return None if row is None else (float(row[0]), int(row[1]))

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
            # updated_at starts at insert time (K7-D-4 dirty watermark). salience /
            # last_evidence_epoch / merged_into take their server defaults.
            "updated_at": node.created_at,
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
        # valid_at defaults to created_at (K7-D-1 backfill semantics) when a caller
        # leaves it unset — old TypedLink constructors stay byte-compatible. edge_key
        # is IDENTITY-assigned by Postgres and is never written here.
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
            "valid_at": link.valid_at if link.valid_at is not None else link.created_at,
            "invalid_at": link.invalid_at,
            "invalidated_by": link.invalidated_by,
            "invalidated_at": link.invalidated_at,
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
            valid_at=None if row.get("valid_at") is None else _as_utc(row["valid_at"]),
            invalid_at=None if row.get("invalid_at") is None else _as_utc(row["invalid_at"]),
            invalidated_by=row.get("invalidated_by"),
            invalidated_at=None
            if row.get("invalidated_at") is None
            else _as_utc(row["invalidated_at"]),
        )

    def _row_to_version(self, row: dict[str, Any]) -> NodeVersion:
        trail = tuple(NodeProvenance.model_validate(p) for p in row["provenance"])
        return NodeVersion(
            version_id=str(row["version_key"]),
            node_id=str(row["node_id"]),
            node_kind=NodeKind(row["node_kind"]),
            concept_name=str(row["concept_name"]),
            content=str(row["content"]),
            metadata=_as_str_dict(row.get("metadata")),
            wellbeing_category=row.get("wellbeing_category"),
            content_hash=str(row["content_hash"]),
            provenance=trail,
            valid_at=_as_utc(row["valid_at"]),
            invalid_at=_as_utc(row["invalid_at"]),
            invalidated_by=row.get("invalidated_by"),
            invalidated_at=None
            if row.get("invalidated_at") is None
            else _as_utc(row["invalidated_at"]),
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
