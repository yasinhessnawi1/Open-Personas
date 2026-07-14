"""N7-T3a — the synthetic-AS chain STARTING at catalog adoption (D-N7-3).

Extends ``test_mcp_oauth_flow``'s fixture pattern one hop earlier: instead of
hand-seeding a ``user_mcp_servers`` row directly, this drives the REAL production
entry point — ``adoption_service.adopt_catalog_app("github", credential=None)``
against the REAL bundled ``catalog.toml`` entry (``server_type="remote"``,
``remote_url="https://api.githubcopilot.com/mcp/"``, ``auth_method="oauth"``,
``oauth_provider="github"`` — N7-T3a) — then ``oauth_service.initiate_authorize`` →
``oauth_service.handle_callback`` (a mocked github token endpoint, same seam
``test_mcp_oauth_flow`` uses) → ``mcp_store.decrypted_servers_for_persona`` yields a
live decrypted credential. Zero manual/hand-forced steps
(feedback_synthetic_harness_real_transition): the real trigger chain end-to-end, the
same catalog + adoption + oauth machinery a Connect click drives (T3b).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from persona_api.config import APIConfig, Edition
from persona_api.mcp import store as mcp_store
from persona_api.mcp.oauth import service as oauth_service
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import adoption_service, persona_service
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_PERSONA_YAML = """\
schema_version: "1.0"
identity:
  name: Octavia
  role: assistant
  background: A helper for the N7-T3a catalog-oauth chain test.
tools:
  - web_search
"""


def _config() -> APIConfig:
    return APIConfig(
        mcp_credential_key=Fernet.generate_key().decode(),
        mcp_oauth_github_client_id="cid_catalog_test",
        mcp_oauth_github_client_secret="csecret_catalog_test",  # noqa: S106
        mcp_oauth_redirect_base_url="https://app.example",
        edition=Edition.community,
    )


def _ensure_user(engine: Engine, owner: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": owner, "e": f"{owner}@x"},
        )


def _make_persona(engine: Engine, embedder: HashEmbedder384, audit: Path, owner: str) -> str:
    return persona_service.create_persona(
        rls_engine=engine,
        embedder=embedder,
        audit_root=audit,
        owner_id=owner,
        yaml_str=_PERSONA_YAML,
    )


def _mock_token_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., httpx.AsyncClient]:
    # Capture the REAL AsyncClient now — the test patches ``httpx.AsyncClient`` with this
    # factory, so referencing it by name inside would recurse infinitely.
    real_client = httpx.AsyncClient

    def factory(*_a: object, **_k: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler))

    return factory


def _state_from_url(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


@pytest.mark.asyncio
async def test_catalog_adopt_then_connect_yields_a_live_decrypted_token(
    migrated_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = make_rls_engine(migrated_engine.url.render_as_string(hide_password=False))
    config = _config()
    owner = "user_catalog_oauth"
    tok = current_user_id.set(owner)
    try:
        _ensure_user(engine, owner)
        persona_id = _make_persona(engine, embedder, tmp_path / "audit", owner)

        # 1. Catalog adoption — the REAL bundled github entry (N7-T3a), no credential.
        detail = adoption_service.adopt_catalog_app(
            rls_engine=engine,
            config=config,
            owner_id=owner,
            persona_id=persona_id,
            catalog_name="github",
            credential=None,
        )
        assert detail["auth_method"] == "oauth"
        assert detail["oauth_provider"] == "github"
        assert detail["url"] == "https://api.githubcopilot.com/mcp/"
        assert detail["has_credential"] is False  # not yet connected
        server_id = detail["id"]
        assigned = mcp_store.list_servers_for_persona(rls_engine=engine, persona_id=persona_id)
        assert [s["name"] for s in assigned] == ["github"]

        # 2. Not yet connected: decrypted_servers_for_persona skips it (fail-closed, R8) —
        #    the adopt alone never grants a usable connection.
        connected_before = mcp_store.decrypted_servers_for_persona(
            rls_engine=engine, config=config, persona_id=persona_id
        )
        assert connected_before == []

        # 3. Connect — initiate_authorize mints server-side state (config path, no DCR);
        #    handle_callback exchanges the code against a mocked github token endpoint.
        authorize_url = await oauth_service.initiate_authorize(
            rls_engine=engine, config=config, server_id=server_id, redirect_after="/personas"
        )
        assert authorize_url.startswith("https://github.com/login/oauth/authorize?")

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "access_token": "GH_ACCESS_LIVE",
                    "refresh_token": "GH_REFRESH_LIVE",
                    "expires_in": 28800,
                    "scope": "repo",
                    "token_type": "bearer",
                },
            )

        monkeypatch.setattr(oauth_service.httpx, "AsyncClient", _mock_token_client(handler))
        result = await oauth_service.handle_callback(
            rls_engine=engine,
            config=config,
            state=_state_from_url(authorize_url),
            code="CODE_LIVE",
        )
        assert result.server_id == server_id
        assert result.redirect_after == "/personas"

        # 4. Live decrypted credential — the whole chain, catalog adopt → authorize →
        #    callback → decrypted-for-connect, zero hand-forced state.
        connected_after = mcp_store.decrypted_servers_for_persona(
            rls_engine=engine, config=config, persona_id=persona_id
        )
        assert len(connected_after) == 1
        assert connected_after[0]["name"] == "github"
        assert connected_after[0]["auth_method"] == "oauth"
        assert connected_after[0]["credential"] == "GH_ACCESS_LIVE"  # noqa: S105

        # Redacted detail (as the route would return post-connect) never leaks the token.
        detail_after = mcp_store.get_server(rls_engine=engine, server_id=server_id)
        assert detail_after["has_credential"] is True
        assert "GH_ACCESS_LIVE" not in str(detail_after)
    finally:
        current_user_id.reset(tok)
