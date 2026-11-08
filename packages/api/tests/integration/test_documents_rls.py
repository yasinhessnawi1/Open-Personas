"""T22a — Cross-tenant RLS sweep across all Spec 14 document endpoints.

§9 criterion #13: "Security: upload/serve/retrieve all go through the
structural RLS engine; cross-tenant document access is impossible (verified)."

This file aggregates the cross-tenant 404 assertions across the three
Spec 14 routes (POST upload, GET list, DELETE per-document) into a
single explicit sweep that documents the structural RLS guarantee at the
criterion-#13 level. The per-route tests (T17 / T18 / T19) cover these
endpoints individually; this sweep is the binary aggregate test.

No DB — the cross-tenant 404 path is exercised by mocked
chat_service / persona_service stand-ins that model the RLS-engine
behavior (RLS-filtered conversation/persona ids return
``ConversationNotFoundError`` / ``PersonaNotFoundError`` → 404).
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
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.services import audit_service, chat_service, persona_service

if TYPE_CHECKING:
    from persona.schema.chunks import PersonaChunk

#: The one persona the stubbed RLS lookup makes visible to u1 (a minted-format id).
_ASTRID = "persona_a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5"


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
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    """Test client where ONLY 'astrid' persona + 'conv_u1' conversation are
    visible under the user's RLS scope. Anything else → 404."""
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

    document_store = DocumentStore(backend=_InMemoryBackend())  # type: ignore[arg-type]
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    app.state.verify_token = _verify
    app.state.rls_engine = None
    app.state.workspace_root = workspace_root
    app.state.build_document_store = lambda: document_store
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )

    def _fake_get_persona(
        *,
        rls_engine: Any,  # noqa: ANN401, ARG001
        persona_id: str,
    ) -> dict[str, Any]:
        if persona_id == _ASTRID:
            return {"id": _ASTRID, "owner_id": "u1", "yaml": ""}
        raise PersonaNotFoundError("persona not found", context={"persona_id": persona_id})

    def _fake_get_conversation(
        *,
        rls_engine: Any,  # noqa: ANN401, ARG001
        conversation_id: str,
    ) -> dict[str, Any]:
        if conversation_id == "conv_u1":
            return {
                "id": "conv_u1",
                "owner_id": "u1",
                "persona_id": _ASTRID,
                "title": "test",
                "messages": [],
                "created_at": None,
                "updated_at": None,
            }
        raise ConversationNotFoundError(
            "conversation not found", context={"conversation_id": conversation_id}
        )

    monkeypatch.setattr(persona_service, "get_persona", _fake_get_persona)
    monkeypatch.setattr(chat_service, "get_conversation", _fake_get_conversation)
    monkeypatch.setattr(chat_service, "delete_conversation", lambda **_kw: None)
    monkeypatch.setattr(audit_service, "record", lambda **_kw: None)

    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer u1"}


class TestCriterion13RlsSweep:
    """Single-file binary sweep for §9 criterion #13.

    Each of the three Spec 14 document endpoints rejects cross-tenant
    references with 404 (existence-disclosure-safe per D-13 + Spec 08
    structural RLS pattern).
    """

    def test_upload_with_cross_tenant_persona_returns_404(self, client: TestClient) -> None:
        """POST /v1/personas/{cross_tenant_persona_id}/uploads → 404."""
        resp = client.post(
            "/v1/personas/persona_0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e/uploads",
            headers=_auth(),
            files={"file": ("memo.txt", b"hi", "text/plain")},
            data={"conversation_id": "conv_u1"},
        )
        assert resp.status_code == 404

    def test_upload_with_cross_tenant_conversation_returns_404(self, client: TestClient) -> None:
        """POST upload with cross-tenant conversation_id → 404."""
        resp = client.post(
            f"/v1/personas/{_ASTRID}/uploads",
            headers=_auth(),
            files={"file": ("memo.txt", b"hi", "text/plain")},
            data={"conversation_id": "conv_OTHER_TENANT"},
        )
        assert resp.status_code == 404

    def test_list_with_cross_tenant_conversation_returns_404(self, client: TestClient) -> None:
        """GET /v1/conversations/{cross_tenant}/documents → 404."""
        resp = client.get(
            "/v1/conversations/conv_OTHER_TENANT/documents",
            headers=_auth(),
        )
        assert resp.status_code == 404

    def test_delete_with_cross_tenant_conversation_returns_404(self, client: TestClient) -> None:
        """DELETE /v1/conversations/{cross_tenant}/documents/{ref} → 404."""
        resp = client.delete(
            "/v1/conversations/conv_OTHER_TENANT/documents/anything",
            headers=_auth(),
        )
        assert resp.status_code == 404

    def test_delete_conversation_with_cross_tenant_returns_404(self, client: TestClient) -> None:
        """DELETE /v1/conversations/{cross_tenant} → 404 (cascade is also
        RLS-scoped via the same get_conversation pre-flight)."""
        resp = client.delete(
            "/v1/conversations/conv_OTHER_TENANT",
            headers=_auth(),
        )
        assert resp.status_code == 404


class TestExistenceDisclosureSafety:
    """Cross-tenant 404 (not 401/403) — the user cannot distinguish
    "doesn't exist" from "exists for another tenant" from the response.
    """

    def test_cross_tenant_returns_404_not_403(self, client: TestClient) -> None:
        resp = client.get(
            "/v1/conversations/conv_OTHER_TENANT/documents",
            headers=_auth(),
        )
        # 404, not 403 (would leak existence).
        assert resp.status_code == 404
        assert resp.status_code != 403

    def test_unauthenticated_returns_401_not_404(self, client: TestClient) -> None:
        # Auth is required first — 401 BEFORE the RLS check would even run.
        # This distinguishes "no auth header" (401) from "wrong tenant" (404).
        resp = client.get("/v1/conversations/conv_u1/documents")
        assert resp.status_code == 401
