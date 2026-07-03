"""The Slack connector's OAuth ASGI routes (Spec C3) — issue + callback.

The Slack adapter serves the OAuth account-linking carrier as two HTTP routes (api-free —
every dependency injected). The inbound *event* transport (socket mode / HTTP events) is a
separate ⛔ task; this is the linking carrier only.

- ``POST /v1/connectors/slack/link`` — the authenticated issue route. **The owner is
  derived from the verified Clerk JWT, NEVER from the request body/params** — so a caller
  can only mint an authorize URL binding to THEIR account.
- ``GET /slack/oauth/callback`` — the **out-of-band OAuth callback** (the binding WRITE).
  Slack redirects the browser here with ``code`` + ``state``; the injected
  ``complete_oauth`` exchanges the code → identity and binds it via C1's
  ``redeem_and_bind``. A tampered / replayed / expired / wrong-platform ``state`` (or a
  failed exchange) **fails closed** — a 400, no bind (the CSRF-class boundary, D-C3-4).

This route module is intentionally structured like ``discord/app.py``; if a third HTTP
OAuth carrier appears (C4/C5), a shared ``build_oauth_app`` is the natural extraction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from persona.errors import AuthenticationError

from persona_connectors.errors import LinkTokenInvalidError, SlackApiError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from persona.auth.jwt_verifier import AuthenticatedUser

__all__ = ["build_slack_app"]

_ISSUE_PATH = "/v1/connectors/slack/link"
_CALLBACK_PATH = "/slack/oauth/callback"
# The OAuth-state token default TTL (the composition root passes the config value,
# ``config.slack_link_token_ttl_minutes``); the default is only a test fallback.
_DEFAULT_LINK_TTL = timedelta(minutes=15)


def _utcnow() -> datetime:
    return datetime.now(UTC)


_LINKED_HTML = (
    "<html><body><h3>You're linked!</h3>"
    "<p>Return to Slack and DM your persona by name to start.</p></body></html>"
)
_FAILED_HTML = (
    "<html><body><h3>That link didn't work.</h3>"
    "<p>It may have expired or already been used — generate a fresh link from your "
    "Open Persona settings and try again.</p></body></html>"
)


def build_slack_app(
    *,
    issue_authorize_url: Callable[[str], Awaitable[str]],
    complete_oauth: Callable[[str, str], Awaitable[str]],
    verify_jwt: Callable[[str], Awaitable[AuthenticatedUser]],
    link_ttl: timedelta = _DEFAULT_LINK_TTL,
    now: Callable[[], datetime] = _utcnow,
) -> FastAPI:
    """Build the Slack OAuth ASGI app from injected dependencies (api-free).

    ``link_ttl`` (``config.slack_link_token_ttl_minutes``) sets the issue response's
    server-authoritative ``expires_at`` (``issue_time + link_ttl``, C6-D-8); no
    ``destination`` — the authorize URL is self-contained. ``now`` is injected for tests.
    """
    app = FastAPI(title="persona-connectors (slack)")

    @app.post(_ISSUE_PATH)
    async def issue_link(request: Request) -> JSONResponse:
        # AUTHORIZATION boundary: the owner comes from the VERIFIED token, never the request.
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
        # The out-of-band binding WRITE. ``state`` (the C1 LinkToken) carries the CSRF guard;
        # a tampered/replayed/expired/wrong-platform state fails closed in complete_oauth
        # (C1 raises before any bind) → a 400, never an attacker binding.
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code or not state:
            return HTMLResponse(_FAILED_HTML, status_code=400)
        try:
            await complete_oauth(code, state)
        except (LinkTokenInvalidError, SlackApiError):
            return HTMLResponse(_FAILED_HTML, status_code=400)
        return HTMLResponse(_LINKED_HTML, status_code=200)

    return app
