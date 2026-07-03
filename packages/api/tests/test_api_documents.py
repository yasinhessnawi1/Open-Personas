"""GET /v1/conversations/:id/documents + DELETE per-document (spec 14 T18).

No DB — mocks the chat_service.get_conversation lookup to return a
synthetic conversation row; uses an in-memory ``DocumentStore`` and a
``tmp_path``-backed sandbox root. Asserts the route surface +
ownership-check behaviour (cross-tenant 404).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from persona.stores.document_store import DocumentStore
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.errors import ConversationNotFoundError
from persona_api.services import chat_service, document_service
from persona_api.storage import LocalFileStorage

if TYPE_CHECKING:
    from persona.schema.chunks import PersonaChunk


class _InMemoryBackend:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], list[PersonaChunk]] = {}

    def upsert(
        self,
        *,
        persona_id: str,
        store_kind: str,
        chunks: list[PersonaChunk],
    ) -> None:
        key = (persona_id, store_kind)
        existing = self.store.setdefault(key, [])
        existing_ids = {c.id for c in chunks}
        kept = [c for c in existing if c.id not in existing_ids]
        kept.extend(chunks)
        self.store[key] = kept

    def query(
        self,
        *,
        persona_id: str,
        store_kind: str,
        text: str,  # noqa: ARG002
        top_k: int,
        where: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> list[PersonaChunk]:
        return list(self.store.get((persona_id, store_kind), []))[:top_k]

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return list(self.store.get((persona_id, store_kind), []))

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        self.store.pop((persona_id, store_kind), None)

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None:
        key = (persona_id, store_kind)
        self.store[key] = [c for c in self.store.get(key, []) if c.id not in set(ids)]


@pytest.fixture
def sandbox_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def document_store() -> DocumentStore:
    return DocumentStore(backend=_InMemoryBackend())  # type: ignore[arg-type]


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    sandbox_root: Path,
    document_store: DocumentStore,
) -> TestClient:
    app = create_app(
        # Cloud auth wall, but no lifespan engine is built here (the fixture
        # returns the client without entering its context + sets rls_engine=None).
        # Distinct app DSN satisfies the R2 cloud-config guard (R2-D-1).
        APIConfig(
            database_url="postgresql+psycopg://super@localhost/persona_shell",
            app_database_url="postgresql+psycopg://persona_app@localhost/persona_shell",
        )
    )

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    app.state.verify_token = _verify
    app.state.rls_engine = None  # fake get_conversation ignores it
    app.state.sandbox_root = sandbox_root
    app.state.file_storage = LocalFileStorage(sandbox_root)  # R5-D-4: routes read this
    app.state.build_document_store = lambda: document_store

    # Mock chat_service.get_conversation to return a synthetic row for the
    # "u1's conv" only; raise ConversationNotFoundError otherwise (mimics
    # the RLS 404 behaviour).
    def _fake_get_conversation(*, rls_engine: object, conversation_id: str) -> dict[str, Any]:  # noqa: ARG001 — fake matches real signature shape
        if conversation_id == "conv_u1":
            return {
                "id": "conv_u1",
                "owner_id": "u1",
                "persona_id": "astrid",
                "title": "test",
                "messages": [],
                "created_at": None,
                "updated_at": None,
            }
        raise ConversationNotFoundError(
            "conversation not found", context={"conversation_id": conversation_id}
        )

    monkeypatch.setattr(chat_service, "get_conversation", _fake_get_conversation)

    # Mock audit_service.record to a no-op (avoids DB writes).
    from persona_api.services import audit_service

    monkeypatch.setattr(audit_service, "record", lambda **_kw: None)

    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer u1"}


class TestListDocuments:
    def test_empty_when_no_uploads(self, client: TestClient) -> None:
        resp = client.get("/v1/conversations/conv_u1/documents", headers=_auth())
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_uploaded_documents(
        self,
        client: TestClient,
        sandbox_root: Path,
        document_store: DocumentStore,
    ) -> None:
        document_service.upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="u1",
            persona_id="astrid",
            conversation_id="conv_u1",
            file_bytes=b"hello",
            filename="memo.txt",
            document_store=document_store,
        )
        resp = client.get("/v1/conversations/conv_u1/documents", headers=_auth())
        assert resp.status_code == 200
        payload = resp.json()
        assert len(payload) == 1
        assert payload[0]["format"] == "txt"

    def test_404_on_unknown_conversation(self, client: TestClient) -> None:
        resp = client.get("/v1/conversations/nope/documents", headers=_auth())
        assert resp.status_code == 404

    def test_requires_auth(self, client: TestClient) -> None:
        # No Authorization header.
        resp = client.get("/v1/conversations/conv_u1/documents")
        assert resp.status_code == 401


class TestDeleteDocument:
    def test_deletes_existing_document(
        self,
        client: TestClient,
        sandbox_root: Path,
        document_store: DocumentStore,
    ) -> None:
        ref = document_service.upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="u1",
            persona_id="astrid",
            conversation_id="conv_u1",
            file_bytes=b"hi",
            filename="memo.txt",
            document_store=document_store,
        )
        resp = client.delete(
            f"/v1/conversations/conv_u1/documents/{ref.doc_ref}",
            headers=_auth(),
        )
        assert resp.status_code == 204
        # List confirms the document was removed.
        refs = document_service.list_for_conversation(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="u1",
            persona_id="astrid",
            conversation_id="conv_u1",
        )
        assert refs == []

    def test_idempotent_on_unknown_ref(self, client: TestClient) -> None:
        resp = client.delete(
            "/v1/conversations/conv_u1/documents/never-existed",
            headers=_auth(),
        )
        # Idempotent — service is a no-op; route still returns 204.
        assert resp.status_code == 204

    def test_404_on_unknown_conversation(self, client: TestClient) -> None:
        resp = client.delete(
            "/v1/conversations/nope/documents/anything",
            headers=_auth(),
        )
        assert resp.status_code == 404

    def test_requires_auth(self, client: TestClient) -> None:
        resp = client.delete("/v1/conversations/conv_u1/documents/x")
        assert resp.status_code == 401


class TestCrossTenantIsolation:
    """The RLS guard: chat_service.get_conversation raises
    ConversationNotFoundError when the conversation belongs to another tenant
    (the rls_engine filters it out structurally per D-08-1). The fake
    get_conversation models this by only knowing 'conv_u1'."""

    def test_cross_tenant_get_404s(
        self,
        client: TestClient,
        sandbox_root: Path,
        document_store: DocumentStore,
    ) -> None:
        # Upload a doc to conv_other (persona_id="other_persona"). conv_other
        # is unknown to the fake get_conversation → 404.
        document_service.upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="u1",
            persona_id="other_persona",
            conversation_id="conv_other",
            file_bytes=b"secret",
            filename="other.txt",
            document_store=document_store,
        )
        resp = client.get("/v1/conversations/conv_other/documents", headers=_auth())
        assert resp.status_code == 404

    def test_cross_tenant_delete_404s(
        self,
        client: TestClient,
    ) -> None:
        resp = client.delete(
            "/v1/conversations/conv_other/documents/x",
            headers=_auth(),
        )
        assert resp.status_code == 404
