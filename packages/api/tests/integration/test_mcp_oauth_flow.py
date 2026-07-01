"""R8 T4/T5/T6 — OAuth flow end-to-end: initiate, callback matrix, refresh-rotation.

Real Postgres (RLS) + a mocked provider token endpoint (``httpx.MockTransport`` swapped
into the service). Proves, at the service layer where the logic lives:

- **T4** initiate mints a server-side state + returns a proper authorize URL;
- **T5** the callback happy path exchanges the code and persists tokens ENCRYPTED
  (access in ``credentials_encrypted``, rotating refresh in ``refresh_token_encrypted``),
  and the fail-closed MATRIX (unknown / reused / expired / cross-tenant state, and a
  token-endpoint error) writes NO token — the server stays not-connected;
- **T6** refresh-before-inject rotates a near-expiry token (new access AND new refresh
  persisted; expiry advanced), and a hard refresh failure drops the server to
  not-connected (tokens cleared).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from persona_api.config import APIConfig
from persona_api.errors import MCPOAuthError, MCPOAuthStateError
from persona_api.mcp import store as mcp_store
from persona_api.mcp.oauth import service as oauth_service
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration


def _config() -> APIConfig:
    return APIConfig(
        mcp_credential_key=Fernet.generate_key().decode(),
        mcp_oauth_github_client_id="cid_test",
        mcp_oauth_github_client_secret="csecret_test",
        mcp_oauth_redirect_base_url="https://app.example",
    )


def _app_engine(migrated_engine: Engine) -> Engine:
    url = migrated_engine.url.set(username="persona_app", password="persona_app").render_as_string(
        hide_password=False
    )
    return make_rls_engine(url)


def _make_oauth_server(migrated_engine: Engine, owner: str, server_id: str) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": owner, "e": f"{owner}@x"},
        )
        conn.execute(
            text(
                "INSERT INTO user_mcp_servers "
                "(id, owner_id, name, url, auth_method, oauth_provider)"
                " VALUES (:s, :o, :n, 'https://api.githubcopilot.com/mcp/', 'oauth', 'github')"
            ),
            {"s": server_id, "o": owner, "n": f"gh-{server_id}"},
        )


def _mock_token_client(handler: Callable[[httpx.Request], httpx.Response]) -> Callable[..., object]:
    # Capture the REAL AsyncClient now — the test patches ``httpx.AsyncClient`` with this
    # factory, so referencing it by name inside would recurse infinitely.
    real_client = httpx.AsyncClient

    def factory(*_a: object, **_k: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler))

    return factory


def _state_from_url(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


# --- T4 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initiate_mints_state_and_returns_authorize_url(
    migrated_engine: Engine,
) -> None:
    config = _config()
    engine = _app_engine(migrated_engine)
    _make_oauth_server(migrated_engine, "u_init", "srv_init")
    tok = current_user_id.set("u_init")
    try:
        url = await oauth_service.initiate_authorize(
            rls_engine=engine, config=config, server_id="srv_init", redirect_after="/x"
        )
        assert url.startswith("https://github.com/login/oauth/authorize?")
        params = parse_qs(urlparse(url).query)
        assert params["redirect_uri"] == ["https://app.example/mcp/oauth/callback"]
        assert params["code_challenge_method"] == ["S256"]
        # The state was persisted server-side, bound to owner + server.
        with migrated_engine.connect() as conn:
            row = conn.execute(
                text("SELECT owner_id, server_id FROM mcp_oauth_states WHERE state = :s"),
                {"s": _state_from_url(url)},
            ).first()
        assert row == ("u_init", "srv_init")
    finally:
        current_user_id.reset(tok)


# --- T5 happy path + persistence -------------------------------------------


@pytest.mark.asyncio
async def test_callback_happy_path_persists_encrypted_tokens(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    engine = _app_engine(migrated_engine)
    _make_oauth_server(migrated_engine, "u_cb", "srv_cb")

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "ACCESS_SECRET_1",
                "refresh_token": "REFRESH_SECRET_1",
                "expires_in": 28800,
                "scope": "repo",
                "token_type": "bearer",
            },
        )

    monkeypatch.setattr(oauth_service.httpx, "AsyncClient", _mock_token_client(handler))

    tok = current_user_id.set("u_cb")
    try:
        url = await oauth_service.initiate_authorize(
            rls_engine=engine, config=config, server_id="srv_cb", redirect_after="/done"
        )
        result = await oauth_service.handle_callback(
            rls_engine=engine, config=config, state=_state_from_url(url), code="CODE_1"
        )
        assert result.server_id == "srv_cb"
        assert result.redirect_after == "/done"

        # Tokens persisted ENCRYPTED (raw columns ≠ plaintext; decrypt back).
        with migrated_engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT credentials_encrypted, refresh_token_encrypted, "
                        "access_token_expires_at, oauth_scopes "
                        "FROM user_mcp_servers WHERE id = 'srv_cb'"
                    )
                )
                .mappings()
                .first()
            )
        assert row is not None
        assert "ACCESS_SECRET_1" not in str(row["credentials_encrypted"])
        assert "REFRESH_SECRET_1" not in str(row["refresh_token_encrypted"])
        cipher = mcp_store.cipher_from_config(config)
        assert cipher is not None
        assert cipher.decrypt(str(row["credentials_encrypted"])) == "ACCESS_SECRET_1"
        assert cipher.decrypt(str(row["refresh_token_encrypted"])) == "REFRESH_SECRET_1"
        assert row["access_token_expires_at"] is not None
        assert row["oauth_scopes"] == "repo"
    finally:
        current_user_id.reset(tok)


# --- T5 fail-closed matrix --------------------------------------------------


@pytest.mark.asyncio
async def test_callback_unknown_state_writes_nothing(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    engine = _app_engine(migrated_engine)
    _make_oauth_server(migrated_engine, "u_unk", "srv_unk")
    called = {"exchanged": False}

    def handler(_r: httpx.Request) -> httpx.Response:
        called["exchanged"] = True
        return httpx.Response(200, json={"access_token": "x", "token_type": "bearer"})

    monkeypatch.setattr(oauth_service.httpx, "AsyncClient", _mock_token_client(handler))
    tok = current_user_id.set("u_unk")
    try:
        with pytest.raises(MCPOAuthStateError):
            await oauth_service.handle_callback(
                rls_engine=engine, config=config, state="NOT_A_STATE", code="c"
            )
        assert called["exchanged"] is False, "must not exchange a code for an invalid state"
        _assert_not_connected(migrated_engine, "srv_unk")
    finally:
        current_user_id.reset(tok)


@pytest.mark.asyncio
async def test_callback_state_is_one_time(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    engine = _app_engine(migrated_engine)
    _make_oauth_server(migrated_engine, "u_1t", "srv_1t")

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "a", "refresh_token": "r", "token_type": "bearer"}
        )

    monkeypatch.setattr(oauth_service.httpx, "AsyncClient", _mock_token_client(handler))
    tok = current_user_id.set("u_1t")
    try:
        url = await oauth_service.initiate_authorize(
            rls_engine=engine, config=config, server_id="srv_1t", redirect_after=None
        )
        state = _state_from_url(url)
        await oauth_service.handle_callback(rls_engine=engine, config=config, state=state, code="c")
        with pytest.raises(MCPOAuthStateError):
            await oauth_service.handle_callback(
                rls_engine=engine, config=config, state=state, code="c"
            )
    finally:
        current_user_id.reset(tok)


@pytest.mark.asyncio
async def test_callback_expired_state_writes_nothing(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    engine = _app_engine(migrated_engine)
    _make_oauth_server(migrated_engine, "u_exp", "srv_exp")

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "a", "token_type": "bearer"})

    monkeypatch.setattr(oauth_service.httpx, "AsyncClient", _mock_token_client(handler))
    tok = current_user_id.set("u_exp")
    try:
        url = await oauth_service.initiate_authorize(
            rls_engine=engine, config=config, server_id="srv_exp", redirect_after=None
        )
        state = _state_from_url(url)
        with migrated_engine.begin() as conn:
            conn.execute(
                text("UPDATE mcp_oauth_states SET expires_at = :e WHERE state = :s"),
                {"e": datetime.now(UTC) - timedelta(seconds=1), "s": state},
            )
        with pytest.raises(MCPOAuthStateError):
            await oauth_service.handle_callback(
                rls_engine=engine, config=config, state=state, code="c"
            )
        _assert_not_connected(migrated_engine, "srv_exp")
    finally:
        current_user_id.reset(tok)


@pytest.mark.asyncio
async def test_callback_cross_tenant_state_writes_nothing(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    engine = _app_engine(migrated_engine)
    _make_oauth_server(migrated_engine, "u_victim", "srv_victim")
    _make_oauth_server(migrated_engine, "u_attacker", "srv_attacker")

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "a", "token_type": "bearer"})

    monkeypatch.setattr(oauth_service.httpx, "AsyncClient", _mock_token_client(handler))

    tok_v = current_user_id.set("u_victim")
    try:
        url = await oauth_service.initiate_authorize(
            rls_engine=engine, config=config, server_id="srv_victim", redirect_after=None
        )
        victim_state = _state_from_url(url)
    finally:
        current_user_id.reset(tok_v)

    tok_a = current_user_id.set("u_attacker")
    try:
        with pytest.raises(MCPOAuthStateError):
            await oauth_service.handle_callback(
                rls_engine=engine, config=config, state=victim_state, code="c"
            )
    finally:
        current_user_id.reset(tok_a)

    # Neither server got a token; the victim's state row still exists.
    _assert_not_connected(migrated_engine, "srv_victim")
    _assert_not_connected(migrated_engine, "srv_attacker")
    with migrated_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT 1 FROM mcp_oauth_states WHERE state = :s"), {"s": victim_state}
            ).first()
            is not None
        )


@pytest.mark.asyncio
async def test_callback_token_endpoint_error_leaves_not_connected(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    engine = _app_engine(migrated_engine)
    _make_oauth_server(migrated_engine, "u_err", "srv_err")

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad_verification_code"})

    monkeypatch.setattr(oauth_service.httpx, "AsyncClient", _mock_token_client(handler))
    tok = current_user_id.set("u_err")
    try:
        url = await oauth_service.initiate_authorize(
            rls_engine=engine, config=config, server_id="srv_err", redirect_after=None
        )
        with pytest.raises(MCPOAuthError):
            await oauth_service.handle_callback(
                rls_engine=engine, config=config, state=_state_from_url(url), code="c"
            )
        _assert_not_connected(migrated_engine, "srv_err")
    finally:
        current_user_id.reset(tok)


# --- T6 refresh-rotation ----------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_before_inject_rotates_and_persists(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    engine = _app_engine(migrated_engine)
    _make_oauth_server(migrated_engine, "u_rot", "srv_rot")
    cipher = mcp_store.cipher_from_config(config)
    assert cipher is not None
    # Seed a near-expiry access token + a refresh token, assign to a persona.
    near = datetime.now(UTC) + timedelta(seconds=30)  # within default 120s leeway
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p_rot','u_rot','x')"))
        conn.execute(
            text(
                "INSERT INTO persona_mcp_assignments (persona_id, server_id) "
                "VALUES ('p_rot','srv_rot')"
            )
        )
        conn.execute(
            text(
                "UPDATE user_mcp_servers SET credentials_encrypted = :a, "
                "refresh_token_encrypted = :r, access_token_expires_at = :e WHERE id = 'srv_rot'"
            ),
            {"a": cipher.encrypt("OLD_ACCESS"), "r": cipher.encrypt("OLD_REFRESH"), "e": near},
        )

    seen: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(parse_qs(request.content.decode()))
        return httpx.Response(
            200,
            json={
                "access_token": "NEW_ACCESS",
                "refresh_token": "NEW_REFRESH",  # rotation
                "expires_in": 28800,
                "token_type": "bearer",
            },
        )

    monkeypatch.setattr(oauth_service.httpx, "AsyncClient", _mock_token_client(handler))
    tok = current_user_id.set("u_rot")
    try:
        await oauth_service.refresh_persona_oauth_servers(
            rls_engine=engine, config=config, persona_id="p_rot"
        )
        # The old refresh token was sent; new access + new refresh persisted; expiry advanced.
        assert seen["grant_type"] == ["refresh_token"]
        assert seen["refresh_token"] == ["OLD_REFRESH"]
        with migrated_engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT credentials_encrypted, refresh_token_encrypted, "
                        "access_token_expires_at "
                        "FROM user_mcp_servers WHERE id = 'srv_rot'"
                    )
                )
                .mappings()
                .first()
            )
        assert row is not None
        assert cipher.decrypt(str(row["credentials_encrypted"])) == "NEW_ACCESS"
        assert cipher.decrypt(str(row["refresh_token_encrypted"])) == "NEW_REFRESH"
        assert row["access_token_expires_at"] > near
    finally:
        current_user_id.reset(tok)


@pytest.mark.asyncio
async def test_refresh_hard_failure_drops_to_not_connected(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    engine = _app_engine(migrated_engine)
    _make_oauth_server(migrated_engine, "u_rf", "srv_rf")
    cipher = mcp_store.cipher_from_config(config)
    assert cipher is not None
    near = datetime.now(UTC) + timedelta(seconds=30)
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p_rf','u_rf','x')"))
        conn.execute(
            text(
                "INSERT INTO persona_mcp_assignments (persona_id, server_id) "
                "VALUES ('p_rf','srv_rf')"
            )
        )
        conn.execute(
            text(
                "UPDATE user_mcp_servers SET credentials_encrypted = :a, "
                "refresh_token_encrypted = :r, access_token_expires_at = :e WHERE id = 'srv_rf'"
            ),
            {"a": cipher.encrypt("OLD_ACCESS"), "r": cipher.encrypt("OLD_REFRESH"), "e": near},
        )

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})  # rotated token spent

    monkeypatch.setattr(oauth_service.httpx, "AsyncClient", _mock_token_client(handler))
    tok = current_user_id.set("u_rf")
    try:
        await oauth_service.refresh_persona_oauth_servers(
            rls_engine=engine, config=config, persona_id="p_rf"
        )
        # Fail-closed: tokens cleared → server not connected (reconnect prompt).
        _assert_not_connected(migrated_engine, "srv_rf")
    finally:
        current_user_id.reset(tok)


def _assert_not_connected(migrated_engine: Engine, server_id: str) -> None:
    with migrated_engine.connect() as conn:
        creds = conn.execute(
            text("SELECT credentials_encrypted FROM user_mcp_servers WHERE id = :i"),
            {"i": server_id},
        ).scalar_one()
    assert creds is None, f"{server_id} must have no access token (not connected)"
