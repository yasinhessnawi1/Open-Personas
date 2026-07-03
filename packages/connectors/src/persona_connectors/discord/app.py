"""The Discord connector's ASGI app (Spec C3) — the OAuth issue + callback routes.

Discord's inbound is the gateway (a WebSocket, a later ⛔ task), so the only HTTP the
adapter serves is the OAuth account-linking carrier — two routes, both api-free (every
dependency injected, so the composition root wires the api-coupled bits):

- ``POST /v1/connectors/discord/link`` — the authenticated linking issue route. **The
  owner is derived from the verified Clerk JWT (the ``sub`` claim), NEVER from the
  request body/params** — otherwise anyone could mint an authorize URL binding to an
  arbitrary owner. The verified owner is handed to the injected ``issue_authorize_url``
  and the ``https://discord.com/oauth2/authorize?...&state=<LinkToken>`` URL is returned.

- ``GET /discord/oauth/callback`` — the **out-of-band OAuth callback** (the binding
  WRITE). Discord redirects the user's browser here with ``code`` + ``state``; the
  injected ``complete_oauth`` exchanges the code → identity and binds it via C1's
  ``redeem_and_bind``. A tampered / replayed / expired / wrong-platform ``state`` (or a
  failed exchange) **fails closed** — a 400, no bind — never an attacker-chosen binding
  (the CSRF-class boundary, D-C3-4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from persona.errors import AuthenticationError

from persona_connectors.errors import DiscordApiError, LinkTokenInvalidError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from persona.auth.jwt_verifier import AuthenticatedUser

__all__ = ["build_discord_app"]

_ISSUE_PATH = "/v1/connectors/discord/link"
_CALLBACK_PATH = "/discord/oauth/callback"
# The OAuth-state token default TTL (the composition root passes the config value,
# ``config.discord_link_token_ttl_minutes``); the default is only a test fallback.
_DEFAULT_LINK_TTL = timedelta(minutes=15)


def _utcnow() -> datetime:
    return datetime.now(UTC)


_LINKED_HTML = (
    "<html><body><h3>You're linked!</h3>"
    "<p>Return to Discord and DM your persona by name to start.</p></body></html>"
)
_FAILED_HTML = (
    "<html><body><h3>That link didn't work.</h3>"
    "<p>It may have expired or already been used — generate a fresh link from your "
    "Open Persona settings and try again.</p></body></html>"
)


def build_discord_app(
    *,
    issue_authorize_url: Callable[[str], Awaitable[str]],
    complete_oauth: Callable[[str, str], Awaitable[str]],
    verify_jwt: Callable[[str], Awaitable[AuthenticatedUser]],
    link_ttl: timedelta = _DEFAULT_LINK_TTL,
    now: Callable[[], datetime] = _utcnow,
) -> FastAPI:
    """Build the Discord connector ASGI app from injected dependencies (api-free).

    Args:
        issue_authorize_url: ``owner_id`` → the Discord authorize URL (the OAuth carrier,
            owner-bound — the owner comes from the verified JWT, never the request).
        complete_oauth: ``(code, state)`` → the bound owner id; raises
            :class:`~persona_connectors.errors.LinkTokenInvalidError` on a bad ``state``
            (fail-closed) or :class:`~persona_connectors.errors.DiscordApiError` on a
            failed exchange.
        verify_jwt: The Clerk JWT verifier — maps a bearer token to an
            :class:`AuthenticatedUser`, raising
            :class:`~persona.errors.AuthenticationError` on any failure.
        link_ttl: The OAuth-state token TTL (``config.discord_link_token_ttl_minutes``) — the
            issue response's server-authoritative ``expires_at`` is ``issue_time + link_ttl``
            (C6-D-8). No ``destination``: the authorize URL is self-contained.
        now: Tz-aware UTC clock (injected for deterministic tests).

    Returns:
        The configured :class:`fastapi.FastAPI` app.
    """
    app = FastAPI(title="persona-connectors (discord)")

    @app.post(_ISSUE_PATH)
    async def issue_link(request: Request) -> JSONResponse:
        # AUTHORIZATION boundary: the owner comes from the VERIFIED token, never the
        # request body/params — so a caller can only mint a link binding to THEIR account.
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return JSONResponse({"detail": "missing bearer token"}, status_code=401)
        bearer = authorization.removeprefix("Bearer ").strip()
        try:
            user = await verify_jwt(bearer)
        except AuthenticationError:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        authorize_url = await issue_authorize_url(user.id)
        return JSONResponse(
            {"authorize_url": authorize_url, "expires_at": (now() + link_ttl).isoformat()}
        )

    @app.get(_CALLBACK_PATH)
    async def oauth_callback(request: Request) -> HTMLResponse:
        # The out-of-band binding WRITE. ``state`` (the C1 LinkToken) carries the CSRF
        # guard; a tampered/replayed/expired/wrong-platform state fails closed in
        # complete_oauth (C1 raises before any bind) → a 400, never an attacker binding.
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code or not state:
            return HTMLResponse(_FAILED_HTML, status_code=400)
        try:
            await complete_oauth(code, state)
        except (LinkTokenInvalidError, DiscordApiError):
            return HTMLResponse(_FAILED_HTML, status_code=400)
        return HTMLResponse(_LINKED_HTML, status_code=200)

    return app
