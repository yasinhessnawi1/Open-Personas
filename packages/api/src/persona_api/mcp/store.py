"""Bring-your-own MCP server store + lifecycle service (spec 30 T09, D-30-3/4/5/6).

CRUD + test-connection/discovery + persona assignment for user-owned MCP
servers. RLS-scoped (the request's ``app.current_user_id`` is bound by the auth
dependency; every query runs through the ``rls_engine`` so a user only ever
touches their own rows). The two security-load-bearing properties:

- **SSRF** — the user-supplied URL is validated with
  :func:`persona.tools.mcp.ssrf.assert_url_allowed` at create/update/test
  (eager) and resolve-then-pinned on every live connect (:mod:`...ssrf`). https
  only; private/loopback/metadata targets refused.
- **Credentials** — encrypted at rest (T07, :mod:`persona_api.mcp.crypto`),
  never returned to the client (only ``has_credential``) and never logged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from persona.errors import MCPServerUnavailableError, MCPUrlNotAllowedError
from persona.logging import get_logger
from persona.tools.mcp.client import MCPClient
from persona.tools.mcp.ssrf import assert_url_allowed
from sqlalchemy import delete, insert, select, update

from persona_api.db.models import persona_mcp_assignments as assignments_t
from persona_api.db.models import personas as personas_t
from persona_api.db.models import user_mcp_servers as servers_t
from persona_api.errors import (
    MCPCredentialError,
    MCPServerNotFoundError,
    MCPServerValidationError,
)
from persona_api.mcp.crypto import cipher_from_config

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from persona_api.config import APIConfig

__all__ = [
    "assign_to_persona",
    "clear_oauth_tokens",
    "create_server",
    "decrypted_servers_for_persona",
    "delete_server",
    "get_server",
    "list_servers",
    "list_servers_for_persona",
    "oauth_server_context",
    "oauth_servers_for_persona",
    "store_oauth_client_id",
    "store_oauth_tokens",
    "test_connection",
    "unassign_from_persona",
    "update_server",
]

_log = get_logger("api.mcp.store")


def _to_detail(row: dict[str, Any]) -> dict[str, Any]:
    """Project a DB row to the MCPServerDetail shape (credential REDACTED)."""
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "url": str(row["url"]),
        "auth_method": str(row["auth_method"]),
        "enabled": bool(row["enabled"]),
        "has_credential": row["credentials_encrypted"] is not None,
        "discovered_tools": row["discovered_tools"],
        # Adoption provenance (Spec N4, N4-D-9): the catalog entry an adoption came from,
        # or None for a manually-added BYO server. NOT a secret — display metadata only.
        "catalog_source": row.get("catalog_source"),
        # Spec R8: the oauth provider key for display (drives Connect/Reconnect), or None.
        "oauth_provider": row.get("oauth_provider"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _encrypt_credential(config: APIConfig, auth_method: str, credential: str | None) -> str | None:
    """Encrypt a credential for storage, or return None for no-auth servers.

    Raises:
        MCPServerValidationError: auth requested but no credential supplied.
        MCPCredentialError: a credential was supplied but no key is configured
            (never store a secret in plaintext — fail loud, D-30-4).
    """
    if auth_method in ("none", "oauth"):
        # ``oauth`` (Spec R8): the access token is obtained via the OAuth dance and
        # persisted by :func:`store_oauth_tokens`, NEVER supplied at create — so there
        # is no user credential to encrypt here (any passed value is ignored).
        return None
    if not credential:
        raise MCPServerValidationError(
            "auth_method requires a credential", context={"reason": "missing_credential"}
        )
    cipher = cipher_from_config(config)
    if cipher is None:
        raise MCPCredentialError(
            "credential encryption is not configured (set MCP_CREDENTIAL_KEY)",
            context={"reason": "no_key"},
        )
    return cipher.encrypt(credential)


def _auth_headers_for_row(config: APIConfig, row: dict[str, Any]) -> dict[str, str] | None:
    """Build the outbound auth header from a row's stored credential, or None.

    Decrypts transiently (in memory) only to authenticate the connect — the
    plaintext is never returned over the API or logged. ``bearer`` →
    ``Authorization: Bearer <token>``. No-auth / no-key → no header.
    """
    # ``oauth`` (Spec R8) injects exactly like ``bearer``: the per-user OAuth access
    # token lives in the SAME ``credentials_encrypted`` blob (one credential channel,
    # R8-D-4). test-connection uses this pre-refresh token as-is.
    if row["auth_method"] in ("bearer", "oauth") and row["credentials_encrypted"] is not None:
        cipher = cipher_from_config(config)
        if cipher is None:
            return None
        token = cipher.decrypt(str(row["credentials_encrypted"]))
        return {"Authorization": f"Bearer {token}"}
    return None


def create_server(
    *,
    rls_engine: Engine,
    config: APIConfig,
    owner_id: str,
    name: str,
    url: str,
    auth_method: str,
    credential: str | None,
    catalog_source: str | None = None,
    oauth_provider: str | None = None,
) -> dict[str, Any]:
    """Create a BYO MCP server (SSRF-validated, credential encrypted). Returns the detail.

    ``catalog_source`` (Spec N4, N4-D-9) records the catalog entry a self-extension
    adoption came from; ``None`` (the default) marks a manually-added BYO server, keeping
    the pre-N4 call sites byte-identical. It is provenance metadata — never a secret.

    ``oauth_provider`` (Spec R8) is the provider-registry key for an ``auth_method =
    "oauth"`` server (e.g. ``github``); the token itself is obtained later via the OAuth
    dance, so no credential is stored at create. ``None`` for non-oauth servers.
    """
    assert_url_allowed(url)  # eager SSRF gate (https + public target)
    if auth_method == "oauth" and not oauth_provider:
        raise MCPServerValidationError(
            "oauth requires an oauth_provider", context={"reason": "missing_provider"}
        )
    encrypted = _encrypt_credential(config, auth_method, credential)
    with rls_engine.begin() as conn:
        row = (
            conn.execute(
                insert(servers_t)
                .values(
                    owner_id=owner_id,
                    name=name,
                    url=url,
                    auth_method=auth_method,
                    credentials_encrypted=encrypted,
                    catalog_source=catalog_source,
                    oauth_provider=oauth_provider,
                )
                .returning(*servers_t.c)
            )
            .mappings()
            .first()
        )
    if row is None:  # pragma: no cover — RLS WITH CHECK would reject, not return None
        raise MCPServerValidationError("could not create server", context={"reason": "rls_reject"})
    return _to_detail(dict(row))


def list_servers(*, rls_engine: Engine) -> list[dict[str, Any]]:
    """List the caller's BYO MCP servers (RLS-scoped)."""
    with rls_engine.begin() as conn:
        rows = (
            conn.execute(select(servers_t).order_by(servers_t.c.created_at.desc())).mappings().all()
        )
    return [_to_detail(dict(r)) for r in rows]


def get_server(*, rls_engine: Engine, server_id: str) -> dict[str, Any]:
    """Return one server (RLS-scoped → 404 when not the caller's)."""
    return _to_detail(_require_row(rls_engine, server_id))


def _require_row(rls_engine: Engine, server_id: str) -> dict[str, Any]:
    with rls_engine.begin() as conn:
        row = conn.execute(select(servers_t).where(servers_t.c.id == server_id)).mappings().first()
    if row is None:
        raise MCPServerNotFoundError("mcp server not found", context={"id": server_id})
    return dict(row)


def update_server(
    *,
    rls_engine: Engine,
    config: APIConfig,
    server_id: str,
    name: str | None = None,
    url: str | None = None,
    auth_method: str | None = None,
    credential: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Patch a server (omitted fields unchanged). Re-validates a new URL; re-encrypts creds."""
    current = _require_row(rls_engine, server_id)
    values: dict[str, Any] = {}
    if name is not None:
        values["name"] = name
    if url is not None:
        assert_url_allowed(url)
        values["url"] = url
    if enabled is not None:
        values["enabled"] = enabled
    # Credential / auth changes: recompute the encrypted blob against the
    # effective auth_method (the incoming one, else the stored one).
    if auth_method is not None or credential is not None:
        effective_auth = auth_method if auth_method is not None else str(current["auth_method"])
        if auth_method is not None:
            values["auth_method"] = effective_auth
        if effective_auth == "none":
            values["credentials_encrypted"] = None
        elif credential is not None:
            values["credentials_encrypted"] = _encrypt_credential(
                config, effective_auth, credential
            )
    if not values:
        return _to_detail(current)
    values["updated_at"] = datetime.now(UTC)
    with rls_engine.begin() as conn:
        row = (
            conn.execute(
                update(servers_t)
                .where(servers_t.c.id == server_id)
                .values(**values)
                .returning(*servers_t.c)
            )
            .mappings()
            .first()
        )
    if row is None:  # pragma: no cover
        raise MCPServerNotFoundError("mcp server not found", context={"id": server_id})
    return _to_detail(dict(row))


def delete_server(*, rls_engine: Engine, server_id: str) -> None:
    """Delete a server (RLS-scoped → 404 when not the caller's). Assignments cascade."""
    with rls_engine.begin() as conn:
        result = conn.execute(
            delete(servers_t).where(servers_t.c.id == server_id).returning(servers_t.c.id)
        )
        if result.first() is None:
            raise MCPServerNotFoundError("mcp server not found", context={"id": server_id})


async def test_connection(
    *, rls_engine: Engine, config: APIConfig, server_id: str
) -> dict[str, Any]:
    """Connect to the server (SSRF-pinned) + discover tools; cache them (D-30-5).

    Returns ``{ok, tools, error}``. A connection failure is reported, not raised
    (the row is unchanged); an SSRF rejection IS surfaced as ``ok=false`` with a
    category. Decrypts the credential transiently only to authenticate — it is
    never returned or logged.
    """
    row = _require_row(rls_engine, server_id)
    try:
        assert_url_allowed(str(row["url"]))  # re-validate before connecting
        client = MCPClient(
            server_name=str(row["name"]),
            server_url=str(row["url"]),
            enforce_ssrf=True,  # the LIVE pinned path — resolve-then-pin per request
            headers=_auth_headers_for_row(config, row),
        )
        await client.connect(strict=True)
        tools = [t.name for t in client.get_tools()]
        await client.disconnect(reason="test_connection")
    except MCPUrlNotAllowedError as exc:
        return {"ok": False, "tools": [], "error": exc.context.get("reason", "blocked")}
    except MCPServerUnavailableError:
        return {"ok": False, "tools": [], "error": "unreachable"}
    # Cache the discovered tools on the row (lazy refresh on later use).
    with rls_engine.begin() as conn:
        conn.execute(
            update(servers_t)
            .where(servers_t.c.id == server_id)
            .values(discovered_tools=tools, updated_at=datetime.now(UTC))
        )
    return {"ok": True, "tools": tools, "error": None}


def assign_to_persona(*, rls_engine: Engine, persona_id: str, server_id: str) -> None:
    """Assign a server to a persona (D-30-6). Idempotent. RLS guards both ends.

    Both the persona and the server must be the caller's (RLS hides others); the
    join row's RLS policy scopes through the persona's owner. A non-owned persona
    or server is invisible → 404.
    """
    _require_row(rls_engine, server_id)  # 404 if not the caller's server
    with rls_engine.begin() as conn:
        persona = conn.execute(select(personas_t.c.id).where(personas_t.c.id == persona_id)).first()
        if persona is None:
            raise MCPServerNotFoundError("persona not found", context={"id": persona_id})
        existing = conn.execute(
            select(assignments_t.c.server_id).where(
                assignments_t.c.persona_id == persona_id,
                assignments_t.c.server_id == server_id,
            )
        ).first()
        if existing is None:
            conn.execute(insert(assignments_t).values(persona_id=persona_id, server_id=server_id))


def unassign_from_persona(*, rls_engine: Engine, persona_id: str, server_id: str) -> None:
    """Remove a persona↔server assignment (idempotent; RLS-scoped)."""
    with rls_engine.begin() as conn:
        conn.execute(
            delete(assignments_t).where(
                assignments_t.c.persona_id == persona_id,
                assignments_t.c.server_id == server_id,
            )
        )


def list_servers_for_persona(*, rls_engine: Engine, persona_id: str) -> list[dict[str, Any]]:
    """List the servers assigned to a persona (RLS-scoped; credential redacted)."""
    with rls_engine.begin() as conn:
        rows = (
            conn.execute(
                select(servers_t)
                .join(assignments_t, assignments_t.c.server_id == servers_t.c.id)
                .where(assignments_t.c.persona_id == persona_id)
                .order_by(servers_t.c.name.asc())
            )
            .mappings()
            .all()
        )
    return [_to_detail(dict(r)) for r in rows]


def decrypted_servers_for_persona(
    *, rls_engine: Engine, config: APIConfig, persona_id: str
) -> list[dict[str, Any]]:
    """Internal: enabled assigned servers with the credential DECRYPTED for connect (T10).

    Used only by the runtime-factory wiring to build :class:`MCPClient`s. The
    plaintext credential lives in memory for the connect and is never persisted,
    logged, or returned over the API. Disabled servers are omitted.
    """
    cipher = cipher_from_config(config)
    with rls_engine.begin() as conn:
        rows = (
            conn.execute(
                select(servers_t)
                .join(assignments_t, assignments_t.c.server_id == servers_t.c.id)
                .where(assignments_t.c.persona_id == persona_id, servers_t.c.enabled.is_(True))
            )
            .mappings()
            .all()
        )
    out: list[dict[str, Any]] = []
    for r in rows:
        credential: str | None = None
        # Spec R8 fail-closed: an ``oauth`` server with no access token yet (never
        # authorized, or token cleared on a hard refresh failure) is NOT connected —
        # never connect it unauthenticated (that would bypass the per-user model).
        if str(r["auth_method"]) == "oauth" and r["credentials_encrypted"] is None:
            _log.warning(
                "mcp server {name} is oauth but not yet authorized; skipped", name=r["name"]
            )
            continue
        if r["credentials_encrypted"] is not None:
            if cipher is None:
                # A stored credential but no key — skip the server (cannot auth);
                # fail closed rather than connect unauthenticated.
                _log.warning(
                    "mcp server {name} has a credential but no key; skipped", name=r["name"]
                )
                continue
            credential = cipher.decrypt(str(r["credentials_encrypted"]))
        out.append(
            {
                "id": str(r["id"]),
                "name": str(r["name"]),
                "url": str(r["url"]),
                "auth_method": str(r["auth_method"]),
                "credential": credential,
            }
        )
    return out


def store_oauth_tokens(
    *,
    rls_engine: Engine,
    config: APIConfig,
    server_id: str,
    provider: str,
    access_token: str,
    refresh_token: str | None,
    access_token_expires_at: datetime | None,
    scopes: str | None,
) -> None:
    """Persist OAuth tokens on a server row (Spec R8, R8-D-4/5). Fail loud with no key.

    The access token goes to the SAME ``credentials_encrypted`` blob as a PAT bearer
    (one credential channel); the rotating refresh token to ``refresh_token_encrypted``
    — both under the one Fernet key. Called on the callback exchange AND on every
    refresh: ``refresh_token`` is written ONLY when the AS returned a new one (rotation),
    so a non-rotating refresh keeps the prior refresh token. Never touches ``enabled``
    (the user's preference) — connection is gated by token presence at inject.

    Raises:
        MCPCredentialError: no ``MCP_CREDENTIAL_KEY`` — never store a token in the clear.
    """
    cipher = cipher_from_config(config)
    if cipher is None:
        raise MCPCredentialError(
            "credential encryption is not configured (set MCP_CREDENTIAL_KEY)",
            context={"reason": "no_key"},
        )
    values: dict[str, Any] = {
        "auth_method": "oauth",
        "oauth_provider": provider,
        "credentials_encrypted": cipher.encrypt(access_token),
        "access_token_expires_at": access_token_expires_at,
        "oauth_scopes": scopes,
        "updated_at": datetime.now(UTC),
    }
    if refresh_token is not None:
        values["refresh_token_encrypted"] = cipher.encrypt(refresh_token)
    with rls_engine.begin() as conn:
        result = conn.execute(
            update(servers_t)
            .where(servers_t.c.id == server_id)
            .values(**values)
            .returning(servers_t.c.id)
        )
    if result.first() is None:
        raise MCPServerNotFoundError("mcp server not found", context={"id": server_id})


def oauth_server_context(
    *, rls_engine: Engine, config: APIConfig, server_id: str
) -> dict[str, Any]:
    """Load the OAuth-relevant fields of a server (RLS-scoped → 404). Refresh token DECRYPTED.

    Internal helper for the OAuth service (initiate / callback / refresh). The
    decrypted refresh token lives in memory only for the immediate refresh call and
    is never returned over the API or logged. ``owner_id`` is the RLS-scoped owner
    (the caller) — safe to pass to :func:`create_state`'s WITH CHECK.
    """
    row = _require_row(rls_engine, server_id)
    refresh_token: str | None = None
    if row.get("refresh_token_encrypted") is not None:
        cipher = cipher_from_config(config)
        if cipher is not None:
            refresh_token = cipher.decrypt(str(row["refresh_token_encrypted"]))
    return {
        "id": str(row["id"]),
        "owner_id": str(row["owner_id"]),
        "url": str(row["url"]),
        "oauth_provider": row.get("oauth_provider"),
        "oauth_client_id": row.get("oauth_client_id"),
        "refresh_token": refresh_token,
        "access_token_expires_at": row.get("access_token_expires_at"),
        "oauth_scopes": row.get("oauth_scopes"),
    }


def store_oauth_client_id(*, rls_engine: Engine, server_id: str, client_id: str) -> None:
    """Persist the DCR-issued client_id for a discovered ``mcp-native`` server (T7).

    The registered client_id cannot be re-derived (re-registering yields a new one), so
    it is stored once at first discovery and reused on every later authorize/refresh.
    Not a secret (public client identifier); the endpoints stay re-discovered from ``url``.
    """
    with rls_engine.begin() as conn:
        conn.execute(
            update(servers_t)
            .where(servers_t.c.id == server_id)
            .values(oauth_client_id=client_id, updated_at=datetime.now(UTC))
        )


def oauth_servers_for_persona(
    *, rls_engine: Engine, config: APIConfig, persona_id: str
) -> list[dict[str, Any]]:
    """Enabled ``oauth`` servers assigned to a persona, refresh tokens DECRYPTED (T6).

    The refresh-before-inject reader: returns each oauth server's id, url, provider,
    expiry, and (in-memory only) decrypted refresh token so the service can refresh
    any near-expiry token before the header is built. Non-oauth servers are omitted.
    """
    cipher = cipher_from_config(config)
    with rls_engine.begin() as conn:
        rows = (
            conn.execute(
                select(servers_t)
                .join(assignments_t, assignments_t.c.server_id == servers_t.c.id)
                .where(
                    assignments_t.c.persona_id == persona_id,
                    servers_t.c.enabled.is_(True),
                    servers_t.c.auth_method == "oauth",
                )
            )
            .mappings()
            .all()
        )
    out: list[dict[str, Any]] = []
    for r in rows:
        refresh_token: str | None = None
        if r["refresh_token_encrypted"] is not None and cipher is not None:
            refresh_token = cipher.decrypt(str(r["refresh_token_encrypted"]))
        out.append(
            {
                "id": str(r["id"]),
                "url": str(r["url"]),
                "oauth_provider": r["oauth_provider"],
                "oauth_client_id": r["oauth_client_id"],
                "refresh_token": refresh_token,
                "access_token_expires_at": r["access_token_expires_at"],
            }
        )
    return out


def clear_oauth_tokens(*, rls_engine: Engine, server_id: str) -> None:
    """De-authorize an oauth server (Spec R8 fail-closed): drop tokens → not connected.

    Called on a hard refresh failure (R8-D-5): the rotating refresh token is spent
    and the exchange failed, so the connection cannot continue. Clearing the access +
    refresh blobs makes :func:`decrypted_servers_for_persona` skip the server (fail
    closed) and the UI show a reconnect prompt. ``enabled`` (the user preference) is
    left intact — the user still wants it, they just need to re-auth.
    """
    with rls_engine.begin() as conn:
        conn.execute(
            update(servers_t)
            .where(servers_t.c.id == server_id)
            .values(
                credentials_encrypted=None,
                refresh_token_encrypted=None,
                access_token_expires_at=None,
                updated_at=datetime.now(UTC),
            )
        )
