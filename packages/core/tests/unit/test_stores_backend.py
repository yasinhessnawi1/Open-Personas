"""Unit tests for the ``Backend`` transport protocol (spec 07, T02).

These assert the *additive* extraction: ``ChromaBackend`` satisfies the new
``Backend`` Protocol unchanged, and a minimal fake transport also satisfies it,
so ``TypedStore`` can compose either. No Chroma client is constructed here — the
behavioural round-trip lives in the integration suite; this is the structural
contract only.
"""
# ruff: noqa: ANN401, ARG001, ARG002

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from persona.stores.backend import Backend

if TYPE_CHECKING:
    from persona.schema.chunks import PersonaChunk


class _FakeBackend:
    """A minimal in-memory transport implementing the Backend surface."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        self.calls.append("upsert")

    def query(
        self,
        *,
        persona_id: str,
        store_kind: str,
        text: str,
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[PersonaChunk]:
        self.calls.append("query")
        return []

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        self.calls.append("get_all")
        return []

    def count(self, *, persona_id: str, store_kind: str, include_superseded: bool = False) -> int:
        self.calls.append("count")
        return 0

    def recent(self, *, persona_id: str, store_kind: str, limit: int) -> list[PersonaChunk]:
        self.calls.append("recent")
        return []

    def get_by_logical_ids(
        self, *, persona_id: str, store_kind: str, logical_ids: list[str]
    ) -> list[PersonaChunk]:
        self.calls.append("get_by_logical_ids")
        return []

    def reinforce(
        self, *, persona_id: str, store_kind: str, ids: list[str], recalled_at: object
    ) -> None:
        self.calls.append("reinforce")

    def set_bands(self, *, persona_id: str, store_kind: str, bands: dict[str, int]) -> None:
        self.calls.append("set_bands")

    def band_histogram(self, *, persona_id: str, store_kind: str) -> dict[int, int]:
        self.calls.append("band_histogram")
        return {}

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        self.calls.append("delete_persona")

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None:
        self.calls.append("delete_documents")


class _MissingMethodBackend:
    """Lacks ``delete_persona`` — must NOT satisfy the Protocol."""

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None: ...
    def query(
        self, *, persona_id: str, store_kind: str, text: str, top_k: int, where: Any = None
    ) -> list[PersonaChunk]:
        return []

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return []

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None: ...


def test_fake_backend_satisfies_protocol() -> None:
    assert isinstance(_FakeBackend(), Backend)


def test_incomplete_backend_does_not_satisfy_protocol() -> None:
    # runtime_checkable structural check catches the missing delete_persona.
    assert not isinstance(_MissingMethodBackend(), Backend)


def test_chroma_backend_class_has_the_full_backend_surface() -> None:
    # Assert ChromaBackend exposes every Backend method WITHOUT constructing a
    # Chroma client (which would need chromadb + a persist path). The rename
    # delete_collection -> delete_persona is the only non-additive edit; this
    # guards it.
    from persona.stores.chroma import ChromaBackend

    for method in (
        "upsert",
        "query",
        "get_all",
        "delete_persona",
        "delete_documents",
        "count",
        "recent",
        "get_by_logical_ids",
        "reinforce",
        "set_bands",
        "band_histogram",
    ):
        assert callable(getattr(ChromaBackend, method, None)), f"ChromaBackend missing {method}"
    assert not hasattr(ChromaBackend, "delete_collection"), "old Chroma-ism name still present"


def test_typed_store_accepts_a_fake_backend() -> None:
    # The widened TypedStore.__init__ (backend: Backend) composes the fake.
    from persona.audit import MemoryAuditLogger
    from persona.stores.self_facts import SelfFactsStore

    backend = _FakeBackend()
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    # delete() routes through the neutral delete_persona name.
    store.delete("p1")
    assert "delete_persona" in backend.calls
