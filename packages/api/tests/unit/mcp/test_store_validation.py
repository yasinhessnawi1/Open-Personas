"""BYO-MCP store input validation that fires BEFORE any DB access (spec 30 T09).

These paths (SSRF reject, missing credential, no encryption key) raise before
the insert, so they need no Postgres. The full CRUD + RLS round-trip is covered
by the integration test.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from persona.errors import MCPUrlNotAllowedError
from persona_api.config import APIConfig
from persona_api.errors import MCPCredentialError, MCPServerValidationError
from persona_api.mcp import store as mcp_store


def _noop_url_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_store, "assert_url_allowed", lambda _url: None)


def test_create_rejects_non_https_url_before_db() -> None:
    # http:// fails the scheme check inside assert_url_allowed — no DB touched.
    engine = MagicMock()
    with pytest.raises(MCPUrlNotAllowedError):
        mcp_store.create_server(
            rls_engine=engine,
            config=APIConfig(),
            owner_id="u1",
            name="evil",
            url="http://example.com/mcp",
            auth_method="none",
            credential=None,
        )
    engine.begin.assert_not_called()


def test_create_rejects_loopback_target_before_db() -> None:
    engine = MagicMock()
    with pytest.raises(MCPUrlNotAllowedError):
        mcp_store.create_server(
            rls_engine=engine,
            config=APIConfig(),
            owner_id="u1",
            name="sneaky",
            url="https://127.0.0.1/mcp",
            auth_method="none",
            credential=None,
        )
    engine.begin.assert_not_called()


def test_create_bearer_without_credential_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _noop_url_check(monkeypatch)
    engine = MagicMock()
    with pytest.raises(MCPServerValidationError):
        mcp_store.create_server(
            rls_engine=engine,
            config=APIConfig(mcp_credential_key=Fernet.generate_key().decode()),
            owner_id="u1",
            name="s",
            url="https://example.com/mcp",
            auth_method="bearer",
            credential=None,
        )
    engine.begin.assert_not_called()


def test_create_oauth_without_provider_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # N7-T3a: a catalog entry claiming auth_method="oauth" but no oauth_provider is a
    # malformed adoption — reject before any write (422 via MCPServerValidationError),
    # never a half-authed row.
    _noop_url_check(monkeypatch)
    engine = MagicMock()
    with pytest.raises(MCPServerValidationError):
        mcp_store.create_server(
            rls_engine=engine,
            config=APIConfig(mcp_credential_key=Fernet.generate_key().decode()),
            owner_id="u1",
            name="s",
            url="https://example.com/mcp",
            auth_method="oauth",
            credential=None,
            oauth_provider=None,
        )
    engine.begin.assert_not_called()


def test_create_oauth_ignores_any_caller_supplied_credential() -> None:
    # oauth's token arrives later via the Connect flow (T3b) — _encrypt_credential must
    # ignore a credential even if one is (incorrectly) passed at create time.
    assert (
        mcp_store._encrypt_credential(
            APIConfig(mcp_credential_key=Fernet.generate_key().decode()),
            "oauth",
            "should-be-ignored",  # noqa: S106 — test fixture
        )
        is None
    )


def test_create_bearer_without_key_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    # A credential supplied but MCP_CREDENTIAL_KEY unset → never store plaintext.
    _noop_url_check(monkeypatch)
    engine = MagicMock()
    with pytest.raises(MCPCredentialError):
        mcp_store.create_server(
            rls_engine=engine,
            config=APIConfig(mcp_credential_key=None),
            owner_id="u1",
            name="s",
            url="https://example.com/mcp",
            auth_method="bearer",
            credential="secret-token",  # noqa: S106 — test fixture
        )
    engine.begin.assert_not_called()


def test_auth_headers_for_row_decrypts_bearer() -> None:
    key = Fernet.generate_key().decode()
    config = APIConfig(mcp_credential_key=key)
    cipher = mcp_store.cipher_from_config(config)
    assert cipher is not None
    row: dict[str, Any] = {
        "auth_method": "bearer",
        "credentials_encrypted": cipher.encrypt("tok-123"),
    }
    headers = mcp_store._auth_headers_for_row(config, row)
    assert headers == {"Authorization": "Bearer tok-123"}


def test_auth_headers_none_for_no_auth() -> None:
    row: dict[str, Any] = {"auth_method": "none", "credentials_encrypted": None}
    assert mcp_store._auth_headers_for_row(APIConfig(), row) is None


def test_credential_never_appears_in_log_output() -> None:
    # Round out the never-logged claim: capture ALL loguru output across the
    # cipher + auth-header path and assert the plaintext secret never leaks.
    from loguru import logger

    secret = "tok-NEVER-LOG-zzz-42"  # noqa: S105 — distinctive test fixture
    captured: list[str] = []
    sink_id = logger.add(captured.append, level="TRACE", format="{message} {extra}")
    try:
        config = APIConfig(mcp_credential_key=Fernet.generate_key().decode())
        cipher = mcp_store.cipher_from_config(config)
        assert cipher is not None
        token = cipher.encrypt(secret)
        assert cipher.decrypt(token) == secret
        headers = mcp_store._auth_headers_for_row(
            config, {"auth_method": "bearer", "credentials_encrypted": token}
        )
        assert headers == {"Authorization": f"Bearer {secret}"}
    finally:
        logger.remove(sink_id)
    blob = "".join(captured)
    assert secret not in blob
    assert token not in blob  # not even the ciphertext is logged
