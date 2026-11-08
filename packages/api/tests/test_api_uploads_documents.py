"""T17 — content-type dispatch tests for ``POST /v1/personas/:id/uploads``.

Verifies the CSA-2 dispatcher in ``routes/uploads.py``: ``image/*``
content-type routes to the Spec 13 image_service path (covered by
``test_uploads.py``); document MIME types + supported extensions route
to Spec 14's document_service path; anything else returns 415.

No DB — mocks ``chat_service.get_conversation`` +
``persona_service.get_persona`` to return synthetic rows; uses an
in-memory ``DocumentStore`` and a ``tmp_path``-backed workspace root.
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
from persona_api.errors import ConversationNotFoundError, PersonaNotFoundError
from persona_api.services import audit_service, chat_service, persona_service

#: The persona the stubbed lookup shows the caller (minted id format).
_PID = "persona_a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5"

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

    from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter

    app.state.verify_token = _verify
    app.state.rls_engine = None
    app.state.workspace_root = workspace_root
    app.state.build_document_store = lambda: document_store
    # Rate limiter (in-memory; never hits the limit in these tests).
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )

    # Mock persona pre-flight (Spec 13's _ensure_persona_visible path).
    def _fake_get_persona(*, rls_engine: Any, persona_id: str) -> dict[str, Any]:  # noqa: ANN401, ARG001
        if persona_id in (_PID, "persona_0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"):
            return {"id": persona_id, "owner_id": "u1", "yaml": ""}
        raise PersonaNotFoundError("persona not found", context={"persona_id": persona_id})

    monkeypatch.setattr(persona_service, "get_persona", _fake_get_persona)

    # Mock conversation lookup for document path.
    def _fake_get_conversation(*, rls_engine: Any, conversation_id: str) -> dict[str, Any]:  # noqa: ANN401, ARG001
        if conversation_id == "conv_u1":
            return {
                "id": "conv_u1",
                "owner_id": "u1",
                "persona_id": _PID,
                "title": "test",
                "messages": [],
                "created_at": None,
                "updated_at": None,
            }
        raise ConversationNotFoundError(
            "conversation not found", context={"conversation_id": conversation_id}
        )

    monkeypatch.setattr(chat_service, "get_conversation", _fake_get_conversation)
    monkeypatch.setattr(audit_service, "record", lambda **_kw: None)

    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer u1"}


class TestDocumentMIMEDispatch:
    """Documents (PDF / docx / xlsx / csv / txt / md / code) dispatch to
    document_service via the content-type / filename detector."""

    def test_txt_upload_routes_to_document_service(self, client: TestClient) -> None:
        resp = client.post(
            f"/v1/personas/{_PID}/uploads",
            headers=_auth(),
            files={
                "file": ("memo.txt", b"The lease runs for twelve months.", "text/plain"),
            },
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201
        payload = resp.json()
        # The DocumentRef shape is the document path's return type.
        assert "doc_ref" in payload
        assert payload["format"] == "txt"
        assert payload["strategy"] == "whole_inject"
        assert payload["token_count"] > 0

    def test_csv_upload_routes_to_document_service(self, client: TestClient) -> None:
        resp = client.post(
            f"/v1/personas/{_PID}/uploads",
            headers=_auth(),
            files={
                "file": ("data.csv", b"a,b\n1,2\n3,4\n", "text/csv"),
            },
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201
        assert resp.json()["format"] == "csv"

    def test_markdown_upload_routes_to_document_service(self, client: TestClient) -> None:
        resp = client.post(
            f"/v1/personas/{_PID}/uploads",
            headers=_auth(),
            files={
                "file": ("notes.md", b"# Heading\n\nBody.", "text/markdown"),
            },
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201
        assert resp.json()["format"] == "md"

    def test_pdf_content_type_routes_to_document_service(self, client: TestClient) -> None:
        # A real text-PDF fixture from the core test fixtures.
        pdf_path = (
            Path(__file__).resolve().parents[2]
            / "core"
            / "tests"
            / "fixtures"
            / "documents"
            / "sample-text.pdf"
        ).resolve()
        pdf_bytes = pdf_path.read_bytes()
        resp = client.post(
            f"/v1/personas/{_PID}/uploads",
            headers=_auth(),
            files={"file": ("tenancy.pdf", pdf_bytes, "application/pdf")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201
        assert resp.json()["format"] == "pdf"


class TestConversationIdRequired:
    """Documents are conversation-scoped — uploads must carry conversation_id."""

    def test_document_without_conversation_id_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            f"/v1/personas/{_PID}/uploads",
            headers=_auth(),
            files={"file": ("memo.txt", b"hi", "text/plain")},
            # NO conversation_id form field
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"]["error"] == "conversation_id_required"

    def test_document_with_empty_conversation_id_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            f"/v1/personas/{_PID}/uploads",
            headers=_auth(),
            files={"file": ("memo.txt", b"hi", "text/plain")},
            data={"conversation_id": "   "},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "conversation_id_required"


class TestUnsupportedMediaType:
    def test_unknown_extension_returns_415(self, client: TestClient) -> None:
        resp = client.post(
            f"/v1/personas/{_PID}/uploads",
            headers=_auth(),
            files={
                "file": ("archive.rar", b"junk-bytes", "application/x-rar"),
            },
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 415

    def test_no_extension_no_content_type_returns_415(self, client: TestClient) -> None:
        resp = client.post(
            f"/v1/personas/{_PID}/uploads",
            headers=_auth(),
            files={
                "file": ("noext", b"some bytes", "application/octet-stream"),
            },
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 415


class TestCrossTenantConversation:
    def test_unknown_conversation_returns_404(self, client: TestClient) -> None:
        resp = client.post(
            f"/v1/personas/{_PID}/uploads",
            headers=_auth(),
            files={"file": ("memo.txt", b"hi", "text/plain")},
            data={"conversation_id": "conv_OTHER_TENANT"},
        )
        assert resp.status_code == 404


class TestCorruptDocument:
    def test_empty_bytes_returns_422_corrupt(self, client: TestClient) -> None:
        resp = client.post(
            f"/v1/personas/{_PID}/uploads",
            headers=_auth(),
            files={
                "file": ("empty.txt", b"   \n\n  \n", "text/plain"),
            },
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"]["error"] == "corrupt_document"


class TestVisionHandoffPath:
    """T21 — scanned PDFs succeed via rasterisation + ImageContent
    (no longer 422). The interim VisionHandoffRequiredError → 422 path has
    been removed per the TODO(T21) close-out.
    """

    def test_scanned_pdf_returns_201_with_vision_handoff_strategy(self, client: TestClient) -> None:
        scanned_path = (
            Path(__file__).resolve().parents[2]
            / "core"
            / "tests"
            / "fixtures"
            / "documents"
            / "scanned-like.pdf"
        ).resolve()
        scanned_bytes = scanned_path.read_bytes()
        resp = client.post(
            f"/v1/personas/{_PID}/uploads",
            headers=_auth(),
            files={"file": ("scan.pdf", scanned_bytes, "application/pdf")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["strategy"] == "vision_handoff"
        assert len(body["images"]) == 3  # fixture has 3 pages
        for image in body["images"]:
            assert image["type"] == "image"
            assert image["media_type"] == "image/png"
            assert image["workspace_path"].endswith(".png")


class TestAuthRequired:
    def test_unauthenticated_returns_401(self, client: TestClient) -> None:
        resp = client.post(
            f"/v1/personas/{_PID}/uploads",
            files={"file": ("memo.txt", b"hi", "text/plain")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 401
