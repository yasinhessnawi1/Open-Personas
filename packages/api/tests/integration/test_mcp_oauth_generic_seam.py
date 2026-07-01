"""R8 T7 — the generic MCP-native seam driven by a synthetic AS (real round-trip).

R8-D-3 "drive the real transition": a seam that only exists as unreached code is a
false green. This test builds a **synthetic MCP-native authorization server** (a stub
that emits a real ``401 + WWW-Authenticate``, serves RFC 9728 PRM + RFC 8414 AS
metadata, accepts a real RFC 7591 DCR ``POST /register``, and completes a real PKCE
authorization-code exchange with S256 ``code_challenge``↔``code_verifier`` validation)
and drives the WHOLE chain end-to-end:

    401 → PRM → AS-metadata → DCR(register) → authorize(challenge) → token(verifier).

The AS records that it actually validated the PKCE binding (``pkce_validated``) — the
happy path issues tokens ONLY because ``BASE64URL(SHA256(verifier)) == challenge``, so
this is a non-vacuous exercise of the real transition, not an assertion of dead code.
The real-provider GitHub round-trip stays the deferred R4 operator leg.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from persona_api.config import APIConfig
from persona_api.mcp import store as mcp_store
from persona_api.mcp.oauth import discovery as oauth_discovery
from persona_api.mcp.oauth import service as oauth_service
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_MCP_URL = "https://mcp.synthetic.test/mcp"
_AS = "https://as.synthetic.test"


def _b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class SyntheticAS:
    """A stateful stub MCP-native authorization + resource server (test-only)."""

    def __init__(self) -> None:
        self.issued_client_ids: list[str] = []
        self.codes: dict[str, str] = {}  # code → code_challenge (recorded at authorize)
        self.pkce_validated = False
        self._seq = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "mcp.synthetic.test" and path == "/mcp":
            # 1. Unauthenticated probe → 401 + WWW-Authenticate(resource_metadata=…).
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        "Bearer resource_metadata="
                        '"https://mcp.synthetic.test/.well-known/oauth-protected-resource"'
                    )
                },
            )
        if host == "mcp.synthetic.test" and path == "/.well-known/oauth-protected-resource":
            # 2. RFC 9728 PRM → authorization_servers.
            return httpx.Response(200, json={"resource": _MCP_URL, "authorization_servers": [_AS]})
        if host == "as.synthetic.test" and path == "/.well-known/oauth-authorization-server":
            # 3. RFC 8414 AS metadata.
            return httpx.Response(
                200,
                json={
                    "issuer": _AS,
                    "authorization_endpoint": f"{_AS}/authorize",
                    "token_endpoint": f"{_AS}/token",
                    "registration_endpoint": f"{_AS}/register",
                },
            )
        if host == "as.synthetic.test" and path == "/register":
            # 4. RFC 7591 DCR → a client_id.
            cid = f"dcr-client-{len(self.issued_client_ids)}"
            self.issued_client_ids.append(cid)
            return httpx.Response(201, json={"client_id": cid})
        if host == "as.synthetic.test" and path == "/authorize":
            # 5. Browser authorize: record the challenge, redirect back with a code.
            q = parse_qs(request.url.query.decode())
            self._seq += 1
            code = f"code-{self._seq}"
            self.codes[code] = q["code_challenge"][0]
            loc = f"{q['redirect_uri'][0]}?code={code}&state={q['state'][0]}"
            return httpx.Response(302, headers={"Location": loc})
        if host == "as.synthetic.test" and path == "/token":
            form = parse_qs(request.content.decode())
            grant = form["grant_type"][0]
            if grant == "authorization_code":
                # 6. REAL PKCE validation: BASE64URL(SHA256(verifier)) == recorded challenge.
                code = form["code"][0]
                verifier = form["code_verifier"][0]
                expected = self.codes.get(code)
                computed = _b64url_nopad(hashlib.sha256(verifier.encode("ascii")).digest())
                if expected is None or computed != expected:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                self.pkce_validated = True
                return httpx.Response(
                    200,
                    json={
                        "access_token": "AS_ACCESS_1",
                        "refresh_token": "AS_REFRESH_1",
                        "expires_in": 3600,
                        "token_type": "bearer",
                    },
                )
        return httpx.Response(404)


def _client_factory(handler: Callable[[httpx.Request], httpx.Response]) -> Callable[..., object]:
    real = httpx.AsyncClient

    def factory(*_a: object, **_k: object) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(handler))

    return factory


def _config() -> APIConfig:
    return APIConfig(
        mcp_credential_key=Fernet.generate_key().decode(),
        mcp_oauth_redirect_base_url="https://app.example",
    )


def _app_engine(migrated_engine: Engine) -> Engine:
    url = migrated_engine.url.set(username="persona_app", password="persona_app").render_as_string(
        hide_password=False
    )
    return make_rls_engine(url)


def _make_mcp_native_server(migrated_engine: Engine, owner: str, server_id: str) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": owner, "e": f"{owner}@x"},
        )
        conn.execute(
            text(
                "INSERT INTO user_mcp_servers "
                "(id, owner_id, name, url, auth_method, oauth_provider)"
                " VALUES (:s, :o, :n, :u, 'oauth', 'mcp-native')"
            ),
            {"s": server_id, "o": owner, "n": f"mcp-{server_id}", "u": _MCP_URL},
        )


@pytest.mark.asyncio
async def test_generic_seam_real_round_trip(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    engine = _app_engine(migrated_engine)
    _make_mcp_native_server(migrated_engine, "u_seam", "srv_seam")
    as_stub = SyntheticAS()

    # Route the service's HTTP through the synthetic AS; neutralise the SSRF DNS gate for
    # the ``*.synthetic.test`` hosts (the gate itself is unit-tested; here it would only
    # try to resolve a non-existent host).
    monkeypatch.setattr(oauth_service.httpx, "AsyncClient", _client_factory(as_stub.handler))
    monkeypatch.setattr(oauth_discovery, "assert_url_allowed", lambda _u: None)

    tok = current_user_id.set("u_seam")
    try:
        # initiate → discovery + DCR + authorize URL.
        authorize_url = await oauth_service.initiate_authorize(
            rls_engine=engine, config=config, server_id="srv_seam", redirect_after=None
        )
        assert authorize_url.startswith(f"{_AS}/authorize?")
        params = parse_qs(urlparse(authorize_url).query)
        # RFC 8707 resource param present for the MCP-native path (audience binding).
        assert params["resource"] == [_MCP_URL]
        assert params["code_challenge_method"] == ["S256"]
        # DCR ran once + the client_id was persisted (can't be re-derived).
        assert len(as_stub.issued_client_ids) == 1
        with migrated_engine.connect() as conn:
            stored_cid = conn.execute(
                text("SELECT oauth_client_id FROM user_mcp_servers WHERE id = 'srv_seam'")
            ).scalar_one()
        assert stored_cid == as_stub.issued_client_ids[0]

        # Simulate the browser authorize hop → get the code.
        state = params["state"][0]
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(as_stub.handler), follow_redirects=False
        ) as browser:
            redirect = await browser.get(authorize_url)
        assert redirect.status_code == 302
        cb = parse_qs(urlparse(redirect.headers["Location"]).query)
        assert cb["state"] == [state]
        code = cb["code"][0]

        # callback → re-discover (reuse persisted client_id), exchange, persist tokens.
        result = await oauth_service.handle_callback(
            rls_engine=engine, config=config, state=state, code=code
        )
        assert result.server_id == "srv_seam"
        # The AS actually validated the PKCE binding (non-vacuous transition).
        assert as_stub.pkce_validated is True
        # No second DCR on callback (client_id reused).
        assert len(as_stub.issued_client_ids) == 1

        # Tokens persisted, encrypted.
        cipher = mcp_store.cipher_from_config(config)
        assert cipher is not None
        with migrated_engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT credentials_encrypted, refresh_token_encrypted "
                        "FROM user_mcp_servers WHERE id = 'srv_seam'"
                    )
                )
                .mappings()
                .first()
            )
        assert row is not None
        assert cipher.decrypt(str(row["credentials_encrypted"])) == "AS_ACCESS_1"
        assert cipher.decrypt(str(row["refresh_token_encrypted"])) == "AS_REFRESH_1"
    finally:
        current_user_id.reset(tok)


@pytest.mark.asyncio
async def test_generic_seam_not_protected_fails_closed(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that does not 401 (no OAuth metadata) fails closed at discovery."""
    from persona_api.errors import MCPOAuthProviderError

    config = _config()
    engine = _app_engine(migrated_engine)
    _make_mcp_native_server(migrated_engine, "u_np", "srv_np")

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})  # never issues a 401 → not oauth-protected

    monkeypatch.setattr(oauth_service.httpx, "AsyncClient", _client_factory(handler))
    monkeypatch.setattr(oauth_discovery, "assert_url_allowed", lambda _u: None)
    tok = current_user_id.set("u_np")
    try:
        with pytest.raises(MCPOAuthProviderError):
            await oauth_service.initiate_authorize(
                rls_engine=engine, config=config, server_id="srv_np", redirect_after=None
            )
    finally:
        current_user_id.reset(tok)
