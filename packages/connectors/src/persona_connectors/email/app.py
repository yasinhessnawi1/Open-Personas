"""The email connector ASGI app (Spec C5, Group E) — the inbound webhook + issue routes.

The connector service (a separate process from persona-api, C1-D-1) runs this minimal
FastAPI app for the email channel. Two routes, injected-dependency-only (api-free):

- ``POST /email/webhook`` — a Postmark inbound-parse POST. **Security (D-C5-5, the B1→B2
  chain):** :func:`guard_inbound` runs **B1** (Basic-Auth, :mod:`_postmark.webhook`) and only
  on success invokes the handler that **parses** the body and dispatches ``on_inbound`` (which
  runs **B2**, the DMARC verdict, then the flow). A forged / missing-auth POST returns **401
  and the body is never parsed** — validate-before-parse. Postmark needs a 2xx or it retries;
  the persona reply is sent out-of-band via the REST client, not in this response.
- ``POST /v1/connectors/email/link`` — the authenticated issue route. **The owner is the
  verified Clerk JWT ``sub``, NEVER the request body** — a caller can only mint a code bound
  to THEIR account; the code is shown in the web app and emailed back.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from persona.errors import AuthenticationError

from persona_connectors._postmark.webhook import guard_inbound
from persona_connectors.email.inbound import parse_inbound_email

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from persona.auth.jwt_verifier import AuthenticatedUser

    from persona_connectors._postmark.webhook import PostmarkWebhookAuth
    from persona_connectors.email.inbound import ParsedEmail

__all__ = ["build_email_app"]

# The email-link token default TTL (the composition root passes the config value,
# ``config.email_link_token_ttl_minutes``); the default is only a test fallback.
_DEFAULT_LINK_TTL = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(UTC)


def build_email_app(
    *,
    webhook_auth: PostmarkWebhookAuth | None,
    on_inbound: Callable[[ParsedEmail], Awaitable[None]],
    issue_code: Callable[[str], Awaitable[str]],
    verify_jwt: Callable[[str], Awaitable[AuthenticatedUser]],
    destination: str | None = None,
    link_ttl: timedelta = _DEFAULT_LINK_TTL,
    now: Callable[[], datetime] = _now,
) -> FastAPI:
    """Build the email connector's ASGI app from injected dependencies (api-free).

    Args:
        webhook_auth: The Basic-Auth credential the Postmark webhook is configured with
            (``None`` ⇒ every inbound request is rejected — fail-closed, B1).
        on_inbound: The parsed-inbound handler (B2 verdict → OTP redeem / shared flow).
        issue_code: ``owner_id`` → the OTP code to show the user (owner-bound).
        verify_jwt: The Clerk JWT verifier — bearer → :class:`AuthenticatedUser`.
        destination: The shared inbound address (``config.email_inbound_address``) — the
            PUBLIC address the reversed C5 flow tells the user to email the code to (C6-D-7);
            the issuing channel owns it (single source of truth). ``None`` omits the field.
        link_ttl: The OTP token TTL (``config.email_link_token_ttl_minutes``) — the response's
            server-authoritative ``expires_at`` is ``issue_time + link_ttl`` (C6-D-8).
        now: Tz-aware UTC clock for the inbound ``received_at`` + ``expires_at`` (injected for
            tests).
    """
    app = FastAPI(title="persona-connectors (email)")

    @app.post("/email/webhook")
    async def inbound(request: Request) -> JSONResponse:
        async def handle() -> bool:
            # Parsed ONLY after B1 passes (validate-before-parse). A malformed payload
            # (parse → None) is a 200 no-op — Postmark must not retry a bad body.
            payload = await request.json()
            parsed = parse_inbound_email(payload, now=now())
            if parsed is not None:
                await on_inbound(parsed)
            return True

        result = await guard_inbound(
            expected=webhook_auth,
            authorization=request.headers.get("Authorization"),
            handle=handle,
        )
        if result is None:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return JSONResponse({"status": "ok"}, status_code=200)

    @app.post("/v1/connectors/email/link")
    async def issue(request: Request) -> JSONResponse:
        # AUTHORIZATION boundary: the owner comes from the VERIFIED token, never the body.
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return JSONResponse({"detail": "missing bearer token"}, status_code=401)
        bearer = authorization.removeprefix("Bearer ").strip()
        try:
            user = await verify_jwt(bearer)
        except AuthenticationError:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        code = await issue_code(user.id)
        body = {"code": code, "expires_at": (now() + link_ttl).isoformat()}
        if destination:
            body["destination"] = destination
        return JSONResponse(body)

    return app
