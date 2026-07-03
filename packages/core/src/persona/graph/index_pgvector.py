"""pgvector dense-index adapter — the DEFAULT + only-wired-prod path (Spec K0, T7; K7-D-9).

For pgvector "the index IS Postgres": the float32 embeddings already live in
``graph_nodes`` (the transport writes them at merge), so this adapter stores nothing
of its own. ``add`` / ``replace`` / ``remove`` / ``rebuild`` / ``persist`` are
**no-ops**; ``search`` runs pgvector cosine over ``graph_nodes``.

**The K7 common-path reshape (K7-D-9).** The 99% retrieval case (no K4 gating) uses
:meth:`search_owner`: scoping is the **``owner_id`` predicate (in-kernel) + RLS
backstop + ``merged_into IS NULL``** — **NO positive surrogate IN-list**, so the
per-query ``surrogates_for_owner`` enumeration leaves the hot path. Session GUCs are
set via ``set_config()`` (NEVER ``SET LOCAL … $1`` — the Spec-07 syntax trap):
``hnsw.ef_search`` (recall/latency knob), ``hnsw.iterative_scan = relaxed_order`` +
``hnsw.max_scan_tuples`` (pgvector ≥ 0.8.0 — fix the filtered-recall failure class, up
to 9× faster filtered queries). Old-pgvector deployments degrade gracefully: the
iterative-scan GUCs are best-effort (exact scans stay correct; only tuning suffers).

**The K4-gated path (rare) is UNCHANGED**: when the store passes a positive allowlist
(the K4 subtraction set), :meth:`search` keeps the ``surrogate = ANY(allowlist)`` IN
path — K4's provider contract is not re-opened. Merged nodes are excluded on BOTH
paths (``merged_into IS NULL``). The turbovec adapter is untouched (mandatory positive
allowlist + exact-rerank).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select, text

from persona.graph._schema import graph_nodes

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy import Connection, Engine

    from persona.graph.config import GraphSettings

__all__ = ["PgvectorGraphIndex", "pgvector_extversion"]

_SET_GUC = text("SELECT set_config(:k, :v, true)")  # transaction-local; NOT `SET LOCAL … $1`


def pgvector_extversion(engine: Engine) -> str | None:
    """The installed ``vector`` extension version (``None`` if absent) — the K7-D-9 gate read."""
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one_or_none()


class PgvectorGraphIndex:
    """Exact/HNSW pgvector dense index over ``graph_nodes`` (implements ``GraphIndex``).

    Owner-agnostic for the shared-index mutate surface; the K7 common-path read
    (:meth:`search_owner`) scopes by the ``owner_id`` predicate instead of a positive
    allowlist (K7-D-9). The mutating methods are no-ops because the embeddings are
    maintained in ``graph_nodes`` by the transport — there is no separate index.
    """

    def __init__(self, *, engine: Engine, settings: GraphSettings | None = None) -> None:
        from persona.graph.config import GraphSettings as _Settings

        self._engine = engine
        self._settings = settings or _Settings()

    # -- mutate (no-ops: the table IS the index) ----------------------------

    def add(self, *, surrogate: int, vector: Sequence[float]) -> None:  # noqa: ARG002
        return

    def replace(self, *, surrogate: int, vector: Sequence[float]) -> None:  # noqa: ARG002
        return

    def remove(self, surrogate: int) -> bool:  # noqa: ARG002
        # The transport's delete_node removes the row; nothing to do here.
        return True

    def rebuild(self, items: Iterable[tuple[int, Sequence[float]]]) -> None:  # noqa: ARG002
        return

    def persist(self) -> None:
        return

    # -- read ---------------------------------------------------------------

    def contains(self, surrogate: int) -> bool:
        stmt = select(graph_nodes.c.surrogate).where(graph_nodes.c.surrogate == surrogate)
        with self._engine.connect() as conn:
            return conn.execute(stmt).first() is not None

    def _apply_gucs(self, conn: Connection) -> None:
        """Set the HNSW session GUCs via ``set_config`` (K7-D-9). Iterative-scan best-effort."""
        conn.execute(_SET_GUC, {"k": "hnsw.ef_search", "v": str(self._settings.hnsw_ef_search)})
        if self._settings.hnsw_iterative_scan != "off":
            # pgvector ≥ 0.8.0 only; unknown-GUC on older builds → degrade gracefully.
            from sqlalchemy.exc import DBAPIError

            try:
                conn.execute(
                    _SET_GUC,
                    {"k": "hnsw.iterative_scan", "v": self._settings.hnsw_iterative_scan},
                )
                conn.execute(
                    _SET_GUC,
                    {"k": "hnsw.max_scan_tuples", "v": str(self._settings.hnsw_max_scan_tuples)},
                )
            except DBAPIError:  # pragma: no cover - only on pgvector < 0.8.0
                conn.rollback()

    def search(
        self,
        *,
        query_vector: Sequence[float],
        top_k: int,
        allowlist: Sequence[int] | None = None,
    ) -> list[tuple[int, float]]:
        """The K4-gated / turbovec-parity path: cosine restricted to a surrogate allowlist.

        ``allowlist=None`` → the whole (non-merged) index; empty → ``[]``. Merged nodes
        are excluded. The K7 common path uses :meth:`search_owner` instead (no IN-list).
        """
        if allowlist is not None and len(allowlist) == 0:
            return []
        distance = graph_nodes.c.embedding.cosine_distance(list(query_vector)).label("distance")
        stmt = select(graph_nodes.c.surrogate, distance).where(graph_nodes.c.merged_into.is_(None))
        if allowlist is not None:
            stmt = stmt.where(graph_nodes.c.surrogate.in_(list(allowlist)))
        stmt = stmt.order_by(distance).limit(top_k)
        with self._engine.begin() as conn:
            self._apply_gucs(conn)
            rows = conn.execute(stmt).all()
        return [(int(r[0]), 1.0 - float(r[1])) for r in rows]

    def search_owner(
        self, *, owner_id: str, query_vector: Sequence[float], top_k: int
    ) -> list[tuple[int, float]]:
        """The K7 common-path search (K7-D-9): owner-predicate scoped, NO positive IN-list.

        Scoping = ``owner_id`` predicate (in-kernel) + ``merged_into IS NULL`` + RLS (prod
        backstop) + the HNSW iterative-scan GUCs. Drops the per-query surrogate
        enumeration; returns ``(surrogate, similarity)``.
        """
        distance = graph_nodes.c.embedding.cosine_distance(list(query_vector)).label("distance")
        stmt = (
            select(graph_nodes.c.surrogate, distance)
            .where(
                graph_nodes.c.owner_id == owner_id,
                graph_nodes.c.merged_into.is_(None),
            )
            .order_by(distance)
            .limit(top_k)
        )
        with self._engine.begin() as conn:
            self._apply_gucs(conn)
            rows = conn.execute(stmt).all()
        return [(int(r[0]), 1.0 - float(r[1])) for r in rows]
