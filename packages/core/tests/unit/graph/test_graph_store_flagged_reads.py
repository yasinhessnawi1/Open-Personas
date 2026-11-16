"""Unit tests for the K4 wellbeing-layer reads on the GraphStore (T8).

The store's only job for these two legs is to delegate to the transport,
owner-scoped, returning exactly what the backend returns — a CQS read (no
writes, no audit). The real SQL is covered in the postgres integration test;
here a fake backend proves the delegation + owner pass-through in isolation.
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

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
DIM = 8


def _node(node_id: str, *, wellbeing_category: str | None = None) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name="c",
        content="c",
        wellbeing_category=wellbeing_category,
        provenance=(NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW),),
        created_at=NOW,
    )


class _FlaggedFakeBackend:
    """Records the owner_id it was called with and returns canned reads."""

    def __init__(self) -> None:
        self.flagged: list[ConceptNode] = []
        self.node_ids: list[str] = []
        self.flagged_calls: list[str] = []
        self.node_ids_calls: list[str] = []

    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]:
        self.flagged_calls.append(owner_id)
        return list(self.flagged)

    def node_ids_for_owner(self, owner_id: str) -> list[str]:
        self.node_ids_calls.append(owner_id)
        return list(self.node_ids)

    # --- the rest of the _StoreBackend surface (unused here) ---------------
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

    def delete_node_and_members(self, owner_id: str, node_id: str) -> dict[str, int]:
        return {}

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
    def merge(self, owner_id: str, candidate: object) -> object:  # pragma: no cover - unused
        raise NotImplementedError


class _Emb:
    model_name = "fake"

    @property
    def dimension(self) -> int:
        return DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * DIM for _ in texts]


def _store(backend: _FlaggedFakeBackend, audit: MemoryAuditLogger) -> PostgresGraphStore:
    return PostgresGraphStore(
        backend=backend,
        index=_NoopIndex(),
        merge_engine=_NoopMerge(),
        embedder=_Emb(),
        audit_logger=audit,
    )


def test_flagged_nodes_returns_backend_result() -> None:
    b, audit = _FlaggedFakeBackend(), MemoryAuditLogger()
    b.flagged = [_node("u1::node::00000001", wellbeing_category="health")]
    out = _store(b, audit).flagged_nodes("u1")
    assert [n.id for n in out] == ["u1::node::00000001"]
    assert out[0].wellbeing_category == "health"
    assert audit.events == []  # a read — no audit (CQS)


def test_node_ids_for_owner_returns_backend_result() -> None:
    b, audit = _FlaggedFakeBackend(), MemoryAuditLogger()
    b.node_ids = ["u1::node::00000001", "u1::node::00000002"]
    out = _store(b, audit).node_ids_for_owner("u1")
    assert out == ["u1::node::00000001", "u1::node::00000002"]
    assert audit.events == []


def test_flagged_reads_are_owner_scoped() -> None:
    b, audit = _FlaggedFakeBackend(), MemoryAuditLogger()
    store = _store(b, audit)
    store.flagged_nodes("owner-a")
    store.node_ids_for_owner("owner-b")
    assert b.flagged_calls == ["owner-a"]  # owner_id passed straight through
    assert b.node_ids_calls == ["owner-b"]
