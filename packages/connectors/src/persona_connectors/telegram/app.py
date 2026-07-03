"""The Telegram connector's ASGI app (Spec C2 T7) — webhook + issue route.

The connector service (a separate process from persona-api, C1-D-1) runs this
minimal FastAPI app exposing two endpoints, both api-free (every dependency is
injected, so the composition root wires the api-coupled bits — the reversibility
ideal):

- ``POST /telegram/webhook`` — receives Telegram updates. **Security (D-C2-2):** it
  validates the ``X-Telegram-Bot-Api-Secret-Token`` header (constant-time,
  fail-closed on an unset secret) **before** the body is parsed, then hands the raw
  update to the injected ``on_update`` handler (the inbound flow, wired in T8).

- ``POST /v1/connectors/telegram/link`` — the authenticated linking issue route.
  **The owner is derived from the verified Clerk JWT (the ``sub`` claim), NEVER
  from the request body/params** — otherwise anyone could mint a deep link binding
  to an arbitrary owner. The verified owner is handed to the injected
  ``issue_deep_link`` (T6) and the ``t.me/<bot>?start=<token>`` link is returned.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from persona.errors import AuthenticationError

from persona_connectors.telegram.webhook import TELEGRAM_SECRET_HEADER, verify_webhook_secret

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from persona.auth.jwt_verifier import AuthenticatedUser
    from pydantic import SecretStr

__all__ = ["build_telegram_app"]

_WEBHOOK_PATH = "/telegram/webhook"
_ISSUE_PATH = "/v1/connectors/telegram/link"
# The deep-link token default TTL (the composition root passes the config value,
# ``config.telegram_link_token_ttl_minutes``); the default is only a test fallback.
_DEFAULT_LINK_TTL = timedelta(minutes=15)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def build_telegram_app(
    *,
    webhook_secret: SecretStr | None,
    on_update: Callable[[dict[str, object]], Awaitable[None]],
    issue_deep_link: Callable[[str], Awaitable[str]],
    verify_jwt: Callable[[str], Awaitable[AuthenticatedUser]],
    link_ttl: timedelta = _DEFAULT_LINK_TTL,
    now: Callable[[], datetime] = _utcnow,
) -> FastAPI:
    """Build the connector ASGI app from injected dependencies (api-free).

    Args:
        webhook_secret: The configured webhook secret (``None`` ⇒ the webhook
            rejects every request — fail-closed, D-C2-2).
        on_update: The inbound-update handler (the flow, wired by composition).
        issue_deep_link: ``owner_id`` → the ``t.me/<bot>?start=<token>`` deep link
            (T6's ``TelegramLinkingService.issue_deep_link``, owner-bound).
        verify_jwt: The Clerk JWT verifier (the core ``make_jwt_verifier``) — maps a
            bearer token to an :class:`AuthenticatedUser`, raising
            :class:`~persona.errors.AuthenticationError` on any failure.
        link_ttl: The deep-link token TTL (``config.telegram_link_token_ttl_minutes``) — the
            response's server-authoritative ``expires_at`` is ``issue_time + link_ttl``
            (C6-D-8). No ``destination``: the ``t.me`` deep link is self-contained.
        now: Tz-aware UTC clock (injected for deterministic tests).

    Returns:
        The configured :class:`fastapi.FastAPI` app.
    """
    app = FastAPI(title="persona-connectors (telegram)")

    @app.post(_WEBHOOK_PATH)
    async def telegram_webhook(request: Request) -> JSONResponse:
        # SECURITY (D-C2-2): validate the secret token BEFORE parsing the body, so
        # unauthenticated input never reaches the JSON parser. Fail-closed on an
        # unset secret (verify_webhook_secret returns False).
        presented = request.headers.get(TELEGRAM_SECRET_HEADER)
        if not verify_webhook_secret(webhook_secret, presented):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        try:
            update = await request.json()
        except ValueError:
            return JSONResponse({"detail": "invalid JSON"}, status_code=400)
        if not isinstance(update, dict):
            return JSONResponse({"detail": "update must be an object"}, status_code=400)
        await on_update(update)
        # Always 200 to Telegram on an accepted update — a non-2xx makes Telegram
        # retry/back off; processing faults are handled inside the flow, not by
        # failing the webhook.
        return JSONResponse({"ok": True})

    @app.post(_ISSUE_PATH)
    async def issue_link(request: Request) -> JSONResponse:
        # AUTHORIZATION boundary: the owner comes from the VERIFIED token, never the
        # request body/params — so a caller can only mint a link binding to THEIR
        # own account.
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return JSONResponse({"detail": "missing bearer token"}, status_code=401)
        bearer = authorization.removeprefix("Bearer ").strip()
        try:
            user = await verify_jwt(bearer)
        except AuthenticationError:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        deep_link = await issue_deep_link(user.id)
        return JSONResponse({"deep_link": deep_link, "expires_at": (now() + link_ttl).isoformat()})

    return app
