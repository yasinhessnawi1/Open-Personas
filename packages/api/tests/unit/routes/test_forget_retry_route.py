"""A forget is idempotent: its retry removes the evidence a failed first try left (R9-287).

Round 2 MEDIUM-1: the node goes first, then the confirmed episodic evidence. If the evidence
delete fails after the node is gone, the retry with the same body must not 404 on the missing
node; it removes the evidence and answers 204, so the sleep-time engine has nothing left to
re-distill.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig, Edition
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.routes import memory as memory_routes
from persona_api.services import memory_service


class _Store:
    """The node is there for the first delete only."""

    def __init__(self) -> None:
        self.present = True

    def delete_node(self, owner_id: str, node_id: str) -> bool:  # noqa: ARG002
        was, self.present = self.present, False
        return was


class _Episodic:
    removed: list[tuple[str, list[str]]]
    fail_next: bool

    def __init__(self, **_kwargs: Any) -> None:  # noqa: ANN401
        pass

    def remove_documents(self, persona_id: str, chunk_ids: list[str]) -> None:
        if _Episodic.fail_next:
            _Episodic.fail_next = False
            msg = "the memory store went away"
            raise RuntimeError(msg)
        _Episodic.removed.append((persona_id, chunk_ids))


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = create_app(
        APIConfig(
            database_url="postgresql+psycopg://u:p@localhost:1/none",  # never connected
            app_database_url="postgresql+psycopg://persona_app@localhost:1/none",
            edition=Edition.cloud,
        )
    )

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    _Episodic.removed = []
    _Episodic.fail_next = True
    monkeypatch.setattr(memory_service, "EpisodicStore", _Episodic)
    monkeypatch.setattr(memory_service, "_list_all_owner_personas", lambda _e: [{"id": "p1"}])
    # The node detail the preview and the delete route read: gone once the first try ran.
    store = _Store()
    monkeypatch.setattr(
        memory_routes.memory_service,
        "node_detail",
        lambda *_a, **_k: {"id": "n1"} if store.present else None,
    )
    app.state.verify_token = _verify
    app.state.admin_engine = None
    app.state.graph_store = store
    app.state.memory_backend = object()
    app.state.audit_logger = object()
    app.state.rls_engine = object()
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )
    return TestClient(app, raise_server_exceptions=False)


_AUTH = {"Authorization": "Bearer u_a"}
_BODY = {"episodic": [{"persona_id": "p1", "chunk_id": "c1"}]}


def test_a_retried_forget_removes_the_evidence_the_first_try_left(client: TestClient) -> None:
    first = client.post("/v1/memory/nodes/n1/forget", json=_BODY, headers=_AUTH)
    assert first.status_code == 500, "the evidence delete failed after the node went"
    assert _Episodic.removed == []

    retry = client.post("/v1/memory/nodes/n1/forget", json=_BODY, headers=_AUTH)

    assert retry.status_code == 204
    assert _Episodic.removed == [("p1", ["c1"])]


def test_the_preview_still_needs_the_node(client: TestClient) -> None:
    client.post("/v1/memory/nodes/n1/forget", json=_BODY, headers=_AUTH)

    preview = client.post("/v1/memory/nodes/n1/forget-preview", headers=_AUTH)

    assert preview.status_code == 404
