"""A correction whose node keeps changing under it answers 409 (R9-284 round 11 LOW-2).

The node write is a compare-and-set on what was read (D-R9-284-node-cas); the store retries a
few times and then raises GraphStaleWriteError, which the route turns into "this memory
changed, try again" rather than a 500.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from persona.graph.errors import GraphStaleWriteError
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig, Edition
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter


class _AlwaysChangedStore:
    def correct_node(self, *_a: Any, **_k: Any) -> None:  # noqa: ANN401
        raise GraphStaleWriteError("changed", context={"node_id": "n1"})


@pytest.fixture
def client() -> TestClient:
    app = create_app(
        APIConfig(
            database_url="postgresql+psycopg://u:p@localhost:1/none",  # never connected
            app_database_url="postgresql+psycopg://persona_app@localhost:1/none",
            edition=Edition.cloud,
        )
    )

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    app.state.verify_token = _verify
    app.state.rls_engine = None
    app.state.graph_store = _AlwaysChangedStore()
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )
    return TestClient(app)


def test_a_correction_that_keeps_losing_the_race_is_a_409(client: TestClient) -> None:
    resp = client.patch(
        "/v1/memory/nodes/n1",
        json={"content": "the user's correction"},
        headers={"Authorization": "Bearer u_a"},
    )

    assert resp.status_code == 409
    body = resp.json()
    assert "memory_node_changed" in str(body)
    assert "this memory changed, try again" in str(body)
