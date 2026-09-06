"""The authenticated link front door, embedded and not (Spec I1 T4, D-I1-16).

``POST /v1/me/connectors/{platform}/link`` is the web's single door for starting a link.
Standalone it forwards to the connector service over HTTP. Embedded, that same route is
mounted on THIS app, so forwarding would be the process calling its own public hostname:
a wasted round trip, an opaque 503 in place of a local failure, and a dependency on DNS
already being correct in order to serve the request the cutover exists to make correct.

Both paths are pinned here, because the flag has to be reversible: with it OFF the surface
must be byte-identical to what ships today.
"""

# ruff: noqa: ARG002 — the httpx doubles mirror AsyncClient.post's real signature;
# `kwargs` exists so a caller passing headers/timeout is accepted, not because it is read.
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.background.connectors_host import find_mounted_link_endpoint
from persona_api.config import APIConfig, Edition
from persona_api.errors import register_exception_handlers
from persona_api.routes import connectors as connectors_route

if TYPE_CHECKING:
    from collections.abc import Iterator

_EXPIRES = "2026-09-06T12:00:00+00:00"


def _api(*, embedded: bool, connector_service_url: str = "") -> FastAPI:
    """An api-shaped app carrying the real front-door router and the real handlers."""
    app = FastAPI(title="persona-api (link harness)")
    register_exception_handlers(app)
    app.include_router(connectors_route.router)
    app.state.config = APIConfig(
        edition=Edition.community, connector_service_url=connector_service_url
    )
    app.state.rls_engine = MagicMock()
    # The embed marker the front door consults. Truthy stands for a started host.
    app.state.embedded_connectors = MagicMock() if embedded else None
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        id="owner-1", email="o@example.test"
    )
    return app


def _mount_link_route(app: FastAPI, platform: str, response: JSONResponse) -> list[Request]:
    """Mount a stand-in for the connector's own link route and record what it receives."""
    seen: list[Request] = []

    async def issue(request: Request) -> JSONResponse:
        seen.append(request)
        return response

    app.router.add_api_route(
        f"/v1/connectors/{platform}/link",
        issue,
        methods=["POST"],
        include_in_schema=False,
    )
    return seen


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Fail loudly on ANY outbound httpx call, so 'in process' is proven, not assumed."""
    calls: list[str] = []

    class _Forbidden(httpx.AsyncClient):
        async def post(self, url: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — passthrough
            calls.append(str(url))
            msg = f"outbound HTTP was attempted to {url}"
            raise AssertionError(msg)

    monkeypatch.setattr(connectors_route.httpx, "AsyncClient", _Forbidden)
    return calls


def test_embedded_resolves_the_link_in_process_with_no_outbound_call(
    no_network: list[str],
) -> None:
    """D-I1-16: no self-HTTP hop. Proven by making any outbound call an error.

    A test that merely asserted the response shape would pass just as happily against the
    forwarder, so the absence of the hop is enforced rather than described.
    """
    app = _api(embedded=True, connector_service_url="https://connectors.example.test")
    seen = _mount_link_route(
        app,
        "telegram",
        JSONResponse({"deep_link": "https://t.me/opbot?start=X", "expires_at": _EXPIRES}),
    )

    with TestClient(app) as client:
        resp = client.post("/v1/me/connectors/telegram/link", headers={"Authorization": "Bearer t"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["deep_link"] == "https://t.me/opbot?start=X"
    assert no_network == [], f"the front door went out over HTTP: {no_network}"
    assert len(seen) == 1, "the mounted handler was never called"
    # The handler derives the owner from the bearer it verifies ITSELF, so it must receive
    # the caller's Authorization header rather than a rewritten or stripped request.
    assert seen[0].headers.get("Authorization") == "Bearer t"


def test_not_embedded_still_forwards_exactly_as_it_does_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag OFF keeps the shipped surface, which is what makes the flag reversible."""
    posted: list[str] = []

    class _Recording(httpx.AsyncClient):
        async def post(self, url: Any, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
            posted.append(str(url))
            return httpx.Response(
                200, json={"authorize_url": "https://discord.test/oauth", "expires_at": _EXPIRES}
            )

    monkeypatch.setattr(connectors_route.httpx, "AsyncClient", _Recording)

    app = _api(embedded=False, connector_service_url="https://connectors.example.test")
    with TestClient(app) as client:
        resp = client.post("/v1/me/connectors/discord/link", headers={"Authorization": "Bearer t"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["authorize_url"] == "https://discord.test/oauth"
    assert posted == ["https://connectors.example.test/v1/connectors/discord/link"]


def test_an_embedded_failure_fails_soft_exactly_like_an_unreachable_service(
    no_network: list[str],
) -> None:
    """One honest "unavailable" from either hosting; the sub-reason is never an oracle.

    The forwarder collapses every upstream non-200 into a single 503 so the web cannot
    distinguish a config mismatch from an unconfigured platform. The in-process path has
    to collapse the same way, or the embedded deployment would leak a distinction the
    forwarded one hides.
    """
    app = _api(embedded=True)
    _mount_link_route(app, "slack", JSONResponse({"detail": "unauthorized"}, status_code=401))

    with TestClient(app) as client:
        resp = client.post("/v1/me/connectors/slack/link", headers={"Authorization": "Bearer t"})

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"] == "connector_unavailable"
    assert no_network == []


def test_embedded_but_unmounted_platform_falls_back_to_the_forwarder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Embedded does not mean every platform is configured.

    A deployment can embed Telegram alone; asking for a platform whose route was never
    mounted must degrade to the existing forwarder rather than 500 on a missing endpoint.
    """
    posted: list[str] = []

    class _Recording(httpx.AsyncClient):
        async def post(self, url: Any, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
            posted.append(str(url))
            return httpx.Response(200, json={"code": "123456", "expires_at": _EXPIRES})

    monkeypatch.setattr(connectors_route.httpx, "AsyncClient", _Recording)

    app = _api(embedded=True, connector_service_url="https://connectors.example.test")
    _mount_link_route(app, "telegram", JSONResponse({"deep_link": "x", "expires_at": _EXPIRES}))

    with TestClient(app) as client:
        resp = client.post("/v1/me/connectors/sms/link", headers={"Authorization": "Bearer t"})

    assert resp.status_code == 200, resp.text
    assert posted == ["https://connectors.example.test/v1/connectors/sms/link"]


def test_the_endpoint_lookup_matches_only_the_platforms_own_post_route() -> None:
    """The resolver must not match a neighbouring path or the wrong method."""
    app = FastAPI()
    _mount_link_route(app, "telegram", JSONResponse({}))

    assert find_mounted_link_endpoint(app, "telegram") is not None
    assert find_mounted_link_endpoint(app, "discord") is None
