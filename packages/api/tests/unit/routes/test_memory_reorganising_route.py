"""A delete or forget that meets a running consolidation answers 409 (R9-287).

The node delete waits up to its lock timeout for the owner's graph maintenance lock and then
raises GraphNodeBusyError; the memory routes turn it into "this memory is being reorganised,
try again" rather than a 500.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from persona.graph.errors import GraphNodeBusyError
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig, Edition
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.routes import memory as memory_routes


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

    def _busy(*_a: Any, **_k: Any) -> bool:  # noqa: ANN401
        raise GraphNodeBusyError("busy", context={"node_id": "n1"})

    monkeypatch.setattr(memory_routes.memory_service, "delete", _busy)
    monkeypatch.setattr(memory_routes.memory_service, "forget", _busy)
    monkeypatch.setattr(
        memory_routes, "_forget_composition", lambda *_a: (object(), object(), object(), object())
    )
    app.state.verify_token = _verify
    app.state.admin_engine = None
    app.state.graph_store = object()
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )
    return TestClient(app)


_AUTH = {"Authorization": "Bearer u_a"}


def test_a_delete_during_a_consolidation_is_a_409(client: TestClient) -> None:
    resp = client.delete("/v1/memory/nodes/n1", headers=_AUTH)

    assert resp.status_code == 409
    assert "this memory is being reorganised, try again" in resp.text


def test_a_forget_during_a_consolidation_is_a_409(client: TestClient) -> None:
    resp = client.post("/v1/memory/nodes/n1/forget", json={"episodic": []}, headers=_AUTH)

    assert resp.status_code == 409
    assert "this memory is being reorganised, try again" in resp.text
