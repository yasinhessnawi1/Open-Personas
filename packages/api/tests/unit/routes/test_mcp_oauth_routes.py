"""R8 T10 — OAuth route contract: OpenAPI presence, auth-required, response shapes.

Exercises the two new endpoints at the HTTP boundary with the service monkeypatched
(the service logic is covered by the integration suite). Confirms the routes are
registered in the OpenAPI schema (the client is regenerated at merge-back), require
authentication, and shape their responses correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.mcp import store as mcp_store
from persona_api.mcp.oauth import service as oauth_service
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.services import audit_service

_AUTHORIZE = "/v1/mcp-servers/srv1/oauth/authorize"
_CALLBACK = "/v1/mcp-servers/oauth/callback"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = create_app(
        APIConfig(
            database_url="postgresql+psycopg://super@localhost/persona_shell",
            app_database_url="postgresql+psycopg://persona_app@localhost/persona_shell",
        )
    )

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    app.state.verify_token = _verify
    app.state.rls_engine = None
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )

    async def _fake_initiate(**_kw: object) -> str:
        return "https://github.com/login/oauth/authorize?state=abc&code_challenge=x"

    async def _fake_callback(**_kw: object) -> oauth_service.CallbackResult:
        return oauth_service.CallbackResult(server_id="srv1", redirect_after="/done")

    def _fake_get_server(**_kw: object) -> dict[str, Any]:
        now = datetime.now(UTC)
        return {
            "id": "srv1",
            "name": "gh",
            "url": "https://api.githubcopilot.com/mcp/",
            "auth_method": "oauth",
            "enabled": True,
            "has_credential": True,
            "discovered_tools": None,
            "catalog_source": None,
            "oauth_provider": "github",
            "created_at": now,
            "updated_at": now,
        }

    monkeypatch.setattr(oauth_service, "initiate_authorize", _fake_initiate)
    monkeypatch.setattr(oauth_service, "handle_callback", _fake_callback)
    monkeypatch.setattr(mcp_store, "get_server", _fake_get_server)
    monkeypatch.setattr(audit_service, "record", lambda **_kw: None)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer u1"}


def test_openapi_registers_both_oauth_paths(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/v1/mcp-servers/{server_id}/oauth/authorize" in paths
    assert "/v1/mcp-servers/oauth/callback" in paths


def test_authorize_returns_url(client: TestClient) -> None:
    resp = client.post(_AUTHORIZE, headers=_auth(), json={"redirect_after": "/x"})
    assert resp.status_code == 200
    assert resp.json()["authorize_url"].startswith("https://github.com/login/oauth/authorize")


def test_callback_returns_server_and_redirect(client: TestClient) -> None:
    resp = client.post(_CALLBACK, headers=_auth(), json={"state": "abc", "code": "code123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["server"]["id"] == "srv1"
    assert body["server"]["oauth_provider"] == "github"
    assert body["redirect_after"] == "/done"
    # The response NEVER carries a token — only the connected marker.
    assert "credential" not in body["server"]
    assert body["server"]["has_credential"] is True


def test_authorize_requires_auth(client: TestClient) -> None:
    assert client.post(_AUTHORIZE, json={}).status_code == 401


# R9-048 — redirect_after must be a relative in-app path; a request-boundary
# 422 stops a bad value before it ever reaches the service/state store.
def test_authorize_rejects_external_redirect_after(client: TestClient) -> None:
    resp = client.post(_AUTHORIZE, headers=_auth(), json={"redirect_after": "https://evil.com"})
    assert resp.status_code == 422


def test_authorize_rejects_protocol_relative_redirect_after(client: TestClient) -> None:
    resp = client.post(_AUTHORIZE, headers=_auth(), json={"redirect_after": "//evil.com"})
    assert resp.status_code == 422


def test_callback_requires_auth(client: TestClient) -> None:
    assert client.post(_CALLBACK, json={"state": "a", "code": "c"}).status_code == 401
