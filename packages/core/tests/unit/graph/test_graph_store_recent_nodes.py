"""Unit test — the A5 ``recent_nodes`` store delegation (Spec A5, A5-D-X-reads).

The store's job is delegation (owner + limit pass-through, CQS read); the SQL
(ordering + the three exclusions) is proven in
``tests/integration/test_graph_recent_nodes_postgres.py``. The fake-backend
shape mirrors ``test_graph_store_flagged_reads`` — the flagged-reads precedent
this additive read follows.
"""

# ruff: noqa: ARG002 — fakes deliberately ignore some args
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.audit import MemoryAuditLogger
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.graph.store import PostgresGraphStore
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Sequence

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
DIM = 8


def _node(node_id: str) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name="c",
        content="c",
        provenance=(NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW),),
        created_at=NOW,
    )


class _RecentFakeBackend:
    """Returns canned recent nodes; records the (owner, limit) it was asked for."""

    def __init__(self) -> None:
        self.recent: list[ConceptNode] = []
        self.calls: list[tuple[str, int]] = []

    def recent_nodes(self, owner_id: str, *, limit: int) -> list[ConceptNode]:
        self.calls.append((owner_id, limit))
        return list(self.recent)

    # --- the rest of the _StoreBackend surface (unused here) ----------------
    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]:
        return []

    def node_ids_for_owner(self, owner_id: str) -> list[str]:
        return []

    def surrogate_for(self, owner_id: str, node_id: str) -> int | None:
        return None

    def get_embeddings(self, owner_id: str, node_ids: Sequence[str]) -> dict[str, list[float]]:
        return {}

    def get_nodes_by_surrogates(
        self, owner_id: str, surrogates: Sequence[int]
    ) -> dict[int, ConceptNode]:
        return {}

    def get_node(self, owner_id: str, node_id: str) -> ConceptNode | None:
        return None

    def delete_node(self, owner_id: str, node_id: str) -> int | None:
        return None

    def surrogates_for_owner(self, owner_id: str) -> list[int]:
        return []

    def surrogates_for_nodes(self, owner_id: str, node_ids: Sequence[str]) -> list[int]:
        return []

    def fts_query(self, owner_id: str, query: str, top_k: int) -> list[ConceptNode]:
        return []

    def neighbors(self, owner_id: str, node_id: str, *, link_types: object, limit: int) -> list:  # type: ignore[type-arg]
        return []

    def entity_neighbors(self, owner_id: str, node_id: str) -> list[ConceptNode]:
        return []

    def iter_embeddings(self, owner_id: str) -> list[tuple[int, list[float]]]:
        return []


class _NoopIndex:
    def add(self, *, surrogate: int, vector: Sequence[float]) -> None: ...
    def replace(self, *, surrogate: int, vector: Sequence[float]) -> None: ...
    def remove(self, surrogate: int) -> bool:
        return True

    def contains(self, surrogate: int) -> bool:
        return False

    def search(
        self, *, query_vector: Sequence[float], top_k: int, allowlist: Sequence[int] | None = None
    ) -> list[tuple[int, float]]:
        return []

    def rebuild(self, items: object) -> None: ...
    def persist(self) -> None: ...


class _NoopMerge:
    def merge(self, owner_id: str, candidate: object) -> object:  # pragma: no cover — unused
        raise NotImplementedError


class _Emb:
    model_name = "fake"

    @property
    def dimension(self) -> int:
        return DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * DIM for _ in texts]


def test_recent_nodes_delegates_owner_and_limit_and_returns_backend_result() -> None:
    backend = _RecentFakeBackend()
    backend.recent = [_node("u1::node::00000001"), _node("u1::node::00000002")]
    store = PostgresGraphStore(
        backend=backend,
        index=_NoopIndex(),
        merge_engine=_NoopMerge(),
        embedder=_Emb(),
        audit_logger=MemoryAuditLogger(),
    )
    out = store.recent_nodes("u1", limit=7)
    assert [n.id for n in out] == ["u1::node::00000001", "u1::node::00000002"]
    assert backend.calls == [("u1", 7)]
