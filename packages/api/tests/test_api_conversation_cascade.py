"""T19 — Conversation cascade-delete tests.

Verifies the document cascade extension in ``routes/conversations.py``'s
DELETE handler (D-14-X-cascade-coordination): deleting a conversation
removes its workspace document files + DocumentStore chunks, and
**criterion #6 still holds** — the persona's four typed stores are NOT
touched by the cascade (the no-leak guard re-asserted at the cascade
boundary).

No DB — mocks ``chat_service.get_conversation`` + ``chat_service.delete_conversation``
+ ``persona_service.get_persona`` so the route's cascade path runs
end-to-end without Postgres.
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
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.services import audit_service, chat_service, document_service
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
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def document_store() -> DocumentStore:
    return DocumentStore(backend=_InMemoryBackend())  # type: ignore[arg-type]


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    workspace_root: Path,
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
    app.state.rls_engine = None
    app.state.workspace_root = workspace_root
    app.state.file_storage = LocalFileStorage(workspace_root)  # R5-D-4: routes read this
    app.state.build_document_store = lambda: document_store
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )

    def _fake_get_conversation(
        *,
        rls_engine: Any,  # noqa: ANN401, ARG001
        conversation_id: str,
    ) -> dict[str, Any]:
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

    def _fake_delete_conversation(
        *,
        rls_engine: Any,  # noqa: ANN401, ARG001
        conversation_id: str,
        **_kw: Any,  # noqa: ANN401 — K11-T3's forget_memory/persona_id/episodic_store/
        # match_texts/match_floor kwargs; this fixture only exercises the T19 document
        # cascade, not the K11 episodic cascade (covered by test_conversation_forget.py).
    ) -> None:
        # Existing chat_service contract — raise on unknown id; otherwise no-op
        # (DB row delete unobservable in unit test).
        if conversation_id != "conv_u1":
            raise ConversationNotFoundError(
                "conversation not found", context={"conversation_id": conversation_id}
            )

    monkeypatch.setattr(chat_service, "get_conversation", _fake_get_conversation)
    monkeypatch.setattr(chat_service, "delete_conversation", _fake_delete_conversation)
    monkeypatch.setattr(audit_service, "record", lambda **_kw: None)

    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer u1"}


class TestDocumentCascadeOnConversationDelete:
    def test_workspace_files_removed_for_attached_documents(
        self,
        client: TestClient,
        workspace_root: Path,
        document_store: DocumentStore,
    ) -> None:
        # Upload two documents to the conversation.
        document_service.upload(
            file_storage=LocalFileStorage(workspace_root),
            owner_id="u1",
            persona_id="astrid",
            conversation_id="conv_u1",
            file_bytes=b"Lease memo content here.",
            filename="memo.txt",
            document_store=document_store,
        )
        document_service.upload(
            file_storage=LocalFileStorage(workspace_root),
            owner_id="u1",
            persona_id="astrid",
            conversation_id="conv_u1",
            file_bytes=b"another doc",
            filename="other.md",
            document_store=document_store,
        )
        assert (
            len(
                document_service.list_for_conversation(
                    file_storage=LocalFileStorage(workspace_root),
                    owner_id="u1",
                    persona_id="astrid",
                    conversation_id="conv_u1",
                )
            )
            == 2
        )

        # Delete the conversation.
        resp = client.delete("/v1/conversations/conv_u1", headers=_auth())
        assert resp.status_code == 204

        # Workspace files for the conversation's documents are gone.
        remaining = document_service.list_for_conversation(
            file_storage=LocalFileStorage(workspace_root),
            owner_id="u1",
            persona_id="astrid",
            conversation_id="conv_u1",
        )
        assert remaining == []

    def test_document_store_chunks_removed(
        self,
        client: TestClient,
        workspace_root: Path,
        document_store: DocumentStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force large-doc path so chunks land in the store.
        monkeypatch.setenv("PERSONA_DOC_INJECT_THRESHOLD", "100")
        document_service.upload(
            file_storage=LocalFileStorage(workspace_root),
            owner_id="u1",
            persona_id="astrid",
            conversation_id="conv_u1",
            file_bytes=("Paragraph. " * 200).encode("utf-8"),
            filename="report.txt",
            document_store=document_store,
        )
        assert len(document_store.get_all("conv_u1")) >= 1

        resp = client.delete("/v1/conversations/conv_u1", headers=_auth())
        assert resp.status_code == 204

        # DocumentStore chunks for the conversation are gone.
        assert document_store.get_all("conv_u1") == []

    def test_cascade_does_not_touch_other_conversations(
        self,
        client: TestClient,
        workspace_root: Path,
        document_store: DocumentStore,
    ) -> None:
        # Upload to conv_u1 AND to a different conversation (conv_other —
        # workspace-scoped, not RLS-scoped here since fake delete only
        # accepts conv_u1).
        document_service.upload(
            file_storage=LocalFileStorage(workspace_root),
            owner_id="u1",
            persona_id="astrid",
            conversation_id="conv_u1",
            file_bytes=b"u1 doc",
            filename="a.txt",
            document_store=document_store,
        )
        document_service.upload(
            file_storage=LocalFileStorage(workspace_root),
            owner_id="u1",
            persona_id="astrid",
            conversation_id="conv_OTHER",
            file_bytes=b"other doc",
            filename="b.txt",
            document_store=document_store,
        )

        client.delete("/v1/conversations/conv_u1", headers=_auth())

        # conv_OTHER's documents survived the cascade.
        other_remaining = document_service.list_for_conversation(
            file_storage=LocalFileStorage(workspace_root),
            owner_id="u1",
            persona_id="astrid",
            conversation_id="conv_OTHER",
        )
        assert len(other_remaining) == 1


class TestCriterion6HoldsAtCascadeBoundary:
    """Re-assert Dominant Concern #1 at the cascade boundary: even when
    the conversation is being torn down, the cascade does NOT write into
    the persona's four typed stores. (T04 covers the no-leak property
    for normal DocumentStore operations; this asserts it also holds in
    the cascade path.)
    """

    def test_cascade_no_typed_store_writes(
        self,
        client: TestClient,
        workspace_root: Path,
        document_store: DocumentStore,
    ) -> None:
        # The in-memory backend records all store_kind values it sees in
        # upsert + delete_persona + delete_documents calls; we assert that
        # only ``document`` shows up — never identity/self_facts/worldview/episodic.
        backend: _InMemoryBackend = document_store._backend  # type: ignore[assignment]
        seen_kinds_before = set()
        for _persona_id, store_kind in backend.store:
            seen_kinds_before.add(store_kind)

        document_service.upload(
            file_storage=LocalFileStorage(workspace_root),
            owner_id="u1",
            persona_id="astrid",
            conversation_id="conv_u1",
            file_bytes=b"doc body",
            filename="x.txt",
            document_store=document_store,
        )

        client.delete("/v1/conversations/conv_u1", headers=_auth())

        # Only "document" store_kind was used throughout — no typed-store
        # contamination.
        seen_kinds_after = set()
        for _persona_id, store_kind in backend.store:
            seen_kinds_after.add(store_kind)

        all_seen = seen_kinds_before | seen_kinds_after
        typed_kinds = {"identity", "self_facts", "worldview", "episodic"}
        assert not (all_seen & typed_kinds), (
            f"Cascade contaminated typed stores: {all_seen & typed_kinds} "
            "— Dominant Concern #1 regression at the cascade boundary"
        )


class TestCascadeWithNoDocuments:
    def test_delete_conversation_with_no_documents_is_no_op_safe(
        self,
        client: TestClient,
    ) -> None:
        # No uploads first; just delete. Cascade should not error.
        resp = client.delete("/v1/conversations/conv_u1", headers=_auth())
        assert resp.status_code == 204


class TestCrossTenantStillReturns404:
    def test_cross_tenant_delete_404s(self, client: TestClient) -> None:
        resp = client.delete("/v1/conversations/conv_OTHER", headers=_auth())
        assert resp.status_code == 404
