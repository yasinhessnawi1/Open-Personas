"""The shared Twilio connector ASGI app (Spec C4 T13) — inbound + status + issue routes.

The connector service (a separate process from persona-api, C1-D-1) runs this minimal
FastAPI app for a phone channel (WhatsApp / SMS — both Twilio, so one builder). All
dependencies are injected (api-free; the composition root wires the api-coupled bits):

- ``POST /{platform}/webhook`` — an inbound message. **Security (D-C2-2 / D-C3-3):** the
  request is **signature-verified before the handler acts** — the form is read to compute
  Twilio's ``X-Twilio-Signature`` (it signs the URL + sorted params), and a forged /
  missing signature (or an unset token — fail-closed) returns **403 and the injected
  ``on_inbound`` is never called**. Unauthenticated input never drives a turn or a bind.
- ``POST /{platform}/status`` — a delivery status callback (cost + outcome). Same
  signature gate; the injected ``on_status`` records per-segment cost / maps the outcome.
- ``POST /v1/connectors/{platform}/link`` — the authenticated issue route. **The owner is
  the verified Clerk JWT ``sub``, NEVER the request body** — a caller can only mint an OTP
  code bound to THEIR account; the code is shown in the web app and texted back.

The signed URL is ``str(request.url)``; behind a TLS-terminating proxy the public URL
Twilio signed must be reconstructed (``X-Forwarded-Proto``/``Host``) — verified on the
live-Twilio leg, the one thing a stub cannot prove.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from persona.errors import AuthenticationError

from persona_connectors._twilio.webhook import TWILIO_SIGNATURE_HEADER, verify_twilio_signature

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from persona.auth.jwt_verifier import AuthenticatedUser
    from pydantic import SecretStr

__all__ = ["build_twilio_app"]

_EMPTY_TWIML = "<?xml version='1.0' encoding='UTF-8'?><Response></Response>"
# The phone-link token default TTL (the composition root passes the config value,
# ``config.phone_link_token_ttl_minutes``); the default is only a test fallback.
_DEFAULT_LINK_TTL = timedelta(minutes=10)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _verified_params(request: Request, auth_token: SecretStr | None) -> dict[str, str] | None:
    """Read the form + verify Twilio's signature; return the params, or ``None`` to reject.

    Reads the form (to compute the signature, which covers the params) but the caller acts
    ONLY when this returns the params — a forged / missing signature / unset token yields
    ``None`` (reject-before-act).
    """
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    signature = request.headers.get(TWILIO_SIGNATURE_HEADER)
    if not verify_twilio_signature(auth_token, str(request.url), params, signature):
        return None
    return params


def build_twilio_app(
    *,
    platform: str,
    auth_token: SecretStr | None,
    on_inbound: Callable[[Mapping[str, str]], Awaitable[None]],
    on_status: Callable[[Mapping[str, str]], Awaitable[None]],
    issue_code: Callable[[str], Awaitable[str]],
    verify_jwt: Callable[[str], Awaitable[AuthenticatedUser]],
    destination: str | None = None,
    link_ttl: timedelta = _DEFAULT_LINK_TTL,
    now: Callable[[], datetime] = _utcnow,
) -> FastAPI:
    """Build a phone channel's Twilio ASGI app from injected dependencies (api-free).

    Args:
        platform: The channel key (``"whatsapp"`` / ``"sms"``) — paths are per-platform.
        auth_token: The Twilio auth token the webhooks are signed with (``None`` ⇒ every
            request is rejected — fail-closed).
        on_inbound: The inbound-message handler (classify → OTP redeem / shared flow).
        on_status: The status-callback handler (per-segment cost + outcome mapping).
        issue_code: ``owner_id`` → the OTP code to show the user (owner-bound).
        verify_jwt: The Clerk JWT verifier — bearer → :class:`AuthenticatedUser`.
        destination: The channel's own ``From`` address (``whatsapp:+E164`` / bare ``+E164``,
            ``config.twilio_{whatsapp,sms}_from``) — a PUBLIC value the reversed C4 flow shows
            the user ("text this code to …", C6-D-7). The issuing channel owns it (single
            source of truth); ``None`` omits the field.
        link_ttl: The OTP token TTL (``config.phone_link_token_ttl_minutes``) — the response's
            server-authoritative ``expires_at`` is ``issue_time + link_ttl`` (C6-D-8), so the
            web countdown can never drift from the real token TTL.
        now: Tz-aware UTC clock (injected for deterministic tests).

    Returns:
        The configured :class:`fastapi.FastAPI` app.
    """
    app = FastAPI(title=f"persona-connectors ({platform})")
    webhook_path = f"/{platform}/webhook"
    status_path = f"/{platform}/status"
    issue_path = f"/v1/connectors/{platform}/link"

    @app.post(webhook_path)
    async def inbound(request: Request) -> Response:
        params = await _verified_params(request, auth_token)
        if params is None:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        await on_inbound(params)
        # An empty TwiML 200 — Twilio needs a 2xx (a non-2xx triggers retry/back-off);
        # the reply is sent out-of-band via the REST client, not in this response.
        return Response(content=_EMPTY_TWIML, media_type="application/xml")

    @app.post(status_path)
    async def status(request: Request) -> Response:
        params = await _verified_params(request, auth_token)
        if params is None:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        await on_status(params)
        return Response(content=_EMPTY_TWIML, media_type="application/xml")

    @app.post(issue_path)
    async def issue(request: Request) -> JSONResponse:
        # AUTHORIZATION boundary: the owner comes from the VERIFIED token, never the body —
        # a caller can only mint an OTP code bound to THEIR own account.
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
