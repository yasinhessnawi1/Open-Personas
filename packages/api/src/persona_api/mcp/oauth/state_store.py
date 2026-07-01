"""Server-side OAuth ``state`` store — R8 T1/T2 (R8-D-6, the CSRF/PKCE binding).

The ``state`` is opaque and high-entropy; everything it *means* lives here, mapped
server-side (never encoded in the value — R8-D-6). One row per started flow:

- **mint** (:func:`create_state`, at ``/authorize``): generate ``state`` + a PKCE
  pair, encrypt the verifier at rest (same Fernet key as the token blobs), and
  persist (owner_id, server_id, provider, redirect_after, TTL). Returns the state +
  the PKCE challenge to put on the authorize URL.
- **consume** (:func:`consume_state`, at ``/callback``): look the row up by ``state``
  **through the caller's RLS engine** (so a state that isn't the current user's is
  invisible → rejected), reject if missing / expired, then **delete it** (one-time
  use). Returns the decrypted verifier + the bound (server_id, provider,
  redirect_after). Any miss ⇒ :class:`MCPOAuthStateError` (fail-closed, no token).

RLS does the cross-tenant heavy lifting: the callback runs under the authenticated
caller's ``app.current_user_id``; a stolen victim state maps to a row the attacker's
RLS scope cannot see, so the lookup returns nothing and the flow fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, insert

from persona_api.db.models import mcp_oauth_states as states_t
from persona_api.errors import MCPCredentialError, MCPOAuthStateError
from persona_api.mcp.crypto import cipher_from_config
from persona_api.mcp.oauth.pkce import PkcePair, generate_pkce_pair, generate_state

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from persona_api.config import APIConfig

__all__ = ["MintedState", "StateRecord", "consume_state", "create_state"]


@dataclass(frozen=True, slots=True)
class MintedState:
    """The output of minting a flow: the opaque ``state`` + the public PKCE challenge."""

    state: str
    pkce: PkcePair


@dataclass(frozen=True, slots=True)
class StateRecord:
    """A consumed (validated + deleted) state row's server-side bindings."""

    server_id: str
    provider: str
    code_verifier: str
    redirect_after: str | None


def _cipher(config: APIConfig) -> object:
    cipher = cipher_from_config(config)
    if cipher is None:
        # A flow cannot be started/finished without the encryption key — fail loud,
        # never persist a verifier in the clear (mirrors the N4 credential discipline).
        raise MCPCredentialError(
            "credential encryption is not configured (set MCP_CREDENTIAL_KEY)",
            context={"reason": "no_key"},
        )
    return cipher


def create_state(
    *,
    rls_engine: Engine,
    config: APIConfig,
    owner_id: str,
    server_id: str,
    provider: str,
    redirect_after: str | None,
) -> MintedState:
    """Mint + persist a new OAuth flow state. Returns the state + PKCE challenge.

    The PKCE ``code_verifier`` is encrypted at rest; only the S256 challenge leaves
    on the (public) authorize URL. TTL is ``mcp_oauth_state_ttl_seconds``.
    """
    cipher = _cipher(config)
    state = generate_state()
    pkce = generate_pkce_pair()
    verifier_enc = cipher.encrypt(pkce.verifier)  # type: ignore[attr-defined]
    expires_at = datetime.now(UTC) + timedelta(seconds=config.mcp_oauth_state_ttl_seconds)
    with rls_engine.begin() as conn:
        conn.execute(
            insert(states_t).values(
                owner_id=owner_id,
                server_id=server_id,
                state=state,
                code_verifier_encrypted=verifier_enc,
                provider=provider,
                redirect_after=redirect_after,
                expires_at=expires_at,
            )
        )
    return MintedState(state=state, pkce=pkce)


def consume_state(*, rls_engine: Engine, config: APIConfig, state: str) -> StateRecord:
    """Validate + one-time-consume a callback ``state`` (RLS-scoped). Fail-closed.

    Looks the row up through the caller's RLS engine (a non-owned state is invisible),
    rejects a missing / expired state, then DELETEs the row (one-time use) before
    returning its bindings. Every failure path raises :class:`MCPOAuthStateError` and
    writes nothing.
    """
    cipher = _cipher(config)
    # DELETE … RETURNING is the atomic one-time claim: exactly one caller can consume
    # a given state (a concurrent double-callback / replay finds nothing). RLS scopes
    # the DELETE to the caller's rows, so another tenant's state matches zero rows →
    # invisible → rejected, and the owner's row is untouched by the failed attempt.
    with rls_engine.begin() as conn:
        row = (
            conn.execute(delete(states_t).where(states_t.c.state == state).returning(*states_t.c))
            .mappings()
            .first()
        )
    if row is None:
        # Missing, already-consumed, or another tenant's (RLS-hidden) → CSRF reject.
        raise MCPOAuthStateError(
            "invalid authorization state", context={"reason": "state_not_found"}
        )
    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        # The row is already deleted (atomic claim above) — expired state is both
        # rejected AND swept. Fail-closed: no token is written by the caller.
        raise MCPOAuthStateError("authorization state expired", context={"reason": "state_expired"})
    verifier = cipher.decrypt(str(row["code_verifier_encrypted"]))  # type: ignore[attr-defined]
    return StateRecord(
        server_id=str(row["server_id"]),
        provider=str(row["provider"]),
        code_verifier=verifier,
        redirect_after=row["redirect_after"],
    )
