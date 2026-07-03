"""Dense-index selection — the pgvector/turbovec factory (Spec K0, T7 / D-K0-6).

``make_graph_index`` is the one place the backend is chosen. **pgvector is the
default and the only wired production path for v0.1**; ``turbovec`` is opt-in via
``PERSONA_GRAPH_INDEX_BACKEND=turbovec`` and is lazy-constructed (its library
import happens inside the adapter, behind the ``[turbovec]`` extra) — so a plain
install never needs turbovec. The turbovec path REQUIRES a ``float32_fetch`` (its
rerank is mandatory); the store wires that to the durable Postgres embeddings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.graph._schema import EMBEDDING_DIM
from persona.graph.errors import GraphIndexError
from persona.graph.index_pgvector import PgvectorGraphIndex

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy import Engine

    from persona.graph.config import GraphSettings
    from persona.graph.protocol import GraphIndex

__all__ = ["make_graph_index"]


def make_graph_index(
    *,
    settings: GraphSettings,
    engine: Engine,
    float32_fetch: Callable[[Sequence[int]], dict[int, list[float]]] | None = None,
) -> GraphIndex:
    """Build the configured dense index (pgvector default; turbovec opt-in).

    Args:
        settings: graph config; ``index_backend`` selects the adapter.
        engine: the Postgres engine (the pgvector adapter searches over it).
        float32_fetch: durable-float32 accessor — REQUIRED for the turbovec path
            (its exact-rerank is mandatory), ignored for pgvector.

    Raises:
        GraphIndexError: turbovec selected without a ``float32_fetch``.
    """
    if settings.index_backend == "turbovec":
        if float32_fetch is None:
            raise GraphIndexError(
                "turbovec index requires float32_fetch (the rerank is mandatory)",
                context={"backend": "turbovec"},
            )
        from persona.graph.index_turbovec import TurbovecGraphIndex

        return TurbovecGraphIndex(
            float32_fetch=float32_fetch,
            dim=EMBEDDING_DIM,
            bit_width=settings.index_bit_width,
            rerank_n=settings.rerank_n,
            path=settings.index_path,
        )
    return PgvectorGraphIndex(engine=engine, settings=settings)
