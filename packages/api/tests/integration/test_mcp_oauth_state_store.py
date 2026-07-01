"""R8 T1/T2 — server-side OAuth state store: mint/consume, one-time, expiry, cross-tenant.

Real Postgres (RLS on ``mcp_oauth_states``). Proves the CSRF/PKCE binding is
fail-closed on every axis (R8-D-6): the verifier is encrypted at rest, a state is
consumable exactly once, an expired state is rejected, and a state is invisible to
any tenant but its owner (RLS, non-vacuous under ``persona_app``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from cryptography.fernet import Fernet
from persona_api.config import APIConfig
from persona_api.errors import MCPOAuthStateError
from persona_api.mcp.oauth import state_store
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration


def _config() -> APIConfig:
    return APIConfig(mcp_credential_key=Fernet.generate_key().decode())


def _app_engine(migrated_engine: Engine) -> Engine:
    url = migrated_engine.url.set(username="persona_app", password="persona_app").render_as_string(
        hide_password=False
    )
    return make_rls_engine(url)


def _seed(migrated_engine: Engine, owner: str, server_id: str) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": owner, "e": f"{owner}@x"},
        )
        conn.execute(
            text(
                "INSERT INTO user_mcp_servers "
                "(id, owner_id, name, url, auth_method, oauth_provider)"
                " VALUES (:s, :o, :n, 'https://api.github.com/mcp', 'oauth', 'github')"
                " ON CONFLICT DO NOTHING"
            ),
            {"s": server_id, "o": owner, "n": f"gh-{server_id}"},
        )


def test_mint_then_consume_roundtrip_and_verifier_encrypted(migrated_engine: Engine) -> None:
    config = _config()
    engine = _app_engine(migrated_engine)
    _seed(migrated_engine, "u_state_a", "srv_state_a")

    tok = current_user_id.set("u_state_a")
    try:
        minted = state_store.create_state(
            rls_engine=engine,
            config=config,
            owner_id="u_state_a",
            server_id="srv_state_a",
            provider="github",
            redirect_after="/personas/p1",
        )
        # The verifier is NEVER stored in plaintext.
        with migrated_engine.connect() as conn:
            raw = conn.execute(
                text("SELECT code_verifier_encrypted FROM mcp_oauth_states WHERE state = :s"),
                {"s": minted.state},
            ).scalar_one()
        assert minted.pkce.verifier not in str(raw)

        # Consume returns the bound record + the decrypted verifier.
        rec = state_store.consume_state(rls_engine=engine, config=config, state=minted.state)
        assert rec.server_id == "srv_state_a"
        assert rec.provider == "github"
        assert rec.redirect_after == "/personas/p1"
        assert rec.code_verifier == minted.pkce.verifier
    finally:
        current_user_id.reset(tok)


def test_consume_is_one_time(migrated_engine: Engine) -> None:
    config = _config()
    engine = _app_engine(migrated_engine)
    _seed(migrated_engine, "u_state_once", "srv_once")
    tok = current_user_id.set("u_state_once")
    try:
        minted = state_store.create_state(
            rls_engine=engine,
            config=config,
            owner_id="u_state_once",
            server_id="srv_once",
            provider="github",
            redirect_after=None,
        )
        state_store.consume_state(rls_engine=engine, config=config, state=minted.state)
        # Second consume of the same state is rejected (row deleted on first use).
        with pytest.raises(MCPOAuthStateError):
            state_store.consume_state(rls_engine=engine, config=config, state=minted.state)
    finally:
        current_user_id.reset(tok)


def test_unknown_state_rejected(migrated_engine: Engine) -> None:
    config = _config()
    engine = _app_engine(migrated_engine)
    _seed(migrated_engine, "u_state_unk", "srv_unk")
    tok = current_user_id.set("u_state_unk")
    try:
        with pytest.raises(MCPOAuthStateError):
            state_store.consume_state(rls_engine=engine, config=config, state="NEVER_MINTED")
    finally:
        current_user_id.reset(tok)


def test_expired_state_rejected(migrated_engine: Engine) -> None:
    config = _config()
    engine = _app_engine(migrated_engine)
    _seed(migrated_engine, "u_state_exp", "srv_exp")
    tok = current_user_id.set("u_state_exp")
    try:
        minted = state_store.create_state(
            rls_engine=engine,
            config=config,
            owner_id="u_state_exp",
            server_id="srv_exp",
            provider="github",
            redirect_after=None,
        )
        # Force expiry into the past (superuser update).
        with migrated_engine.begin() as conn:
            conn.execute(
                text("UPDATE mcp_oauth_states SET expires_at = :e WHERE state = :s"),
                {"e": datetime.now(UTC) - timedelta(seconds=1), "s": minted.state},
            )
        with pytest.raises(MCPOAuthStateError):
            state_store.consume_state(rls_engine=engine, config=config, state=minted.state)
    finally:
        current_user_id.reset(tok)


def test_cross_tenant_state_is_invisible(migrated_engine: Engine) -> None:
    """Tenant B cannot consume tenant A's state (RLS → not found → CSRF reject)."""
    config = _config()
    engine = _app_engine(migrated_engine)
    _seed(migrated_engine, "u_state_victim", "srv_victim")
    _seed(migrated_engine, "u_state_attacker", "srv_attacker")

    tok_a = current_user_id.set("u_state_victim")
    try:
        minted = state_store.create_state(
            rls_engine=engine,
            config=config,
            owner_id="u_state_victim",
            server_id="srv_victim",
            provider="github",
            redirect_after=None,
        )
    finally:
        current_user_id.reset(tok_a)

    # Attacker replays the victim's state under their own session → rejected.
    tok_b = current_user_id.set("u_state_attacker")
    try:
        with pytest.raises(MCPOAuthStateError):
            state_store.consume_state(rls_engine=engine, config=config, state=minted.state)
    finally:
        current_user_id.reset(tok_b)

    # And the victim's row still exists (the attacker's failed consume didn't delete it).
    with migrated_engine.connect() as conn:
        still_there = conn.execute(
            text("SELECT 1 FROM mcp_oauth_states WHERE state = :s"), {"s": minted.state}
        ).first()
    assert still_there is not None, "attacker's rejected consume must not delete the victim row"
