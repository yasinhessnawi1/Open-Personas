"""The Twilio Messages API client (Spec C4 T2) — the thin transport boundary.

The Twilio Messages API is form-encoded-over-HTTPS (``POST {base}/2010-04-01/
Accounts/{AccountSid}/Messages.json`` with HTTP **Basic** auth (AccountSid :
AuthToken), ``application/x-www-form-urlencoded`` params ``To`` / ``From`` / ``Body``
(+ ``StatusCallback`` / ``ContentSid`` / ``ContentVariables``), and a JSON reply
``{sid, status, error_code, …}``). So this adapter talks to it with ``httpx``
directly rather than adopting the heavyweight ``twilio`` SDK that would invert
control and duplicate C1's flow (D-C4-1 / the no-new-dep rule carried forward). The
client is the **single Twilio I/O boundary** — both the WhatsApp and the SMS adapters
share it (one account, channel chosen by the ``From`` prefix); every call goes through
one ``_call`` that maps every transport fault or logical rejection to a
:class:`~persona_connectors.errors.TwilioApiError`, and a ``429`` to a
:class:`~persona_connectors.errors.TwilioRateLimitError`.

**The 24h-window signal (63016) is surfaced, not interpreted.** An out-of-window
WhatsApp send can succeed at create-time with ``status="failed"`` + ``error_code=
63016`` (or report it later via a status callback — the async path is T9). This
client only EXPOSES ``error_code`` on :class:`TwilioMessageResult`; mapping it to a
:class:`~persona.delivery.DeliveryResult` is the connector's / T9's / T12's job — the
boundary stays thin.

**Credential safety (D-C4-1 / D-C2-X-credential).** The Account SID is a public
identifier (it rides in the URL path — safe to surface). The auth token is the
credential: unwrapped from its :class:`~pydantic.SecretStr` **only** at the
``httpx`` ``auth=`` call site and **never** in a log line / exception message /
``context``. On an ``httpx`` failure the underlying exception is suppressed with
``raise … from None``; the raised domain error carries only the method + status +
the Twilio ``error_code``. Twilio's own ``message`` text is bot-facing (no secret),
so it is safe to surface.

This module is **api-free** (httpx + persona-core errors only).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, ConfigDict

from persona_connectors.errors import TwilioApiError, TwilioRateLimitError

if TYPE_CHECKING:
    from pydantic import SecretStr

__all__ = ["TWILIO_MAX_MESSAGE_CHARS", "TwilioClient", "TwilioMessageResult"]

# Twilio's hard per-message body cap (both SMS concatenated segments and WhatsApp
# text — Twilio enforces 1600 on each; the splitter budgets against this). The single
# source of the platform fact.
TWILIO_MAX_MESSAGE_CHARS = 1600


class TwilioMessageResult(BaseModel):
    """A Twilio Messages-API create reply, narrowed to what the adapters need.

    Attributes:
        sid: The message resource SID (``SM…`` / ``MM…``) — the per-message id.
        status: The Twilio delivery status (``queued`` / ``sent`` / ``failed`` / …).
        error_code: The Twilio error code present on a create-time failure
            (e.g. ``63016`` — out-of-window WhatsApp), else ``None``. The client only
            SURFACES this; the delivery-outcome mapping is T9/T12.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sid: str
    status: str
    error_code: int | None = None


class TwilioClient:
    """A thin async client over the Twilio Messages API (Spec C4 T2).

    Holds the Account SID (public) + the auth token (a :class:`~pydantic.SecretStr`)
    and an injected :class:`httpx.AsyncClient` (DI — the composition root owns the
    client's timeouts/pool). No globals, no module state. Shared by the WhatsApp and
    SMS adapters (one account, channel by the ``From`` prefix).
    """

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: SecretStr,
        http: httpx.AsyncClient,
        api_base_url: str = "https://api.twilio.com",
    ) -> None:
        self._sid = account_sid
        self._token = auth_token
        self._http = http
        self._base = api_base_url.rstrip("/")

    def _auth(self) -> tuple[str, str]:
        """The HTTP Basic auth pair. Contains the token — NEVER log or surface this."""
        return (self._sid, self._token.get_secret_value())

    def _messages_url(self) -> str:
        """The account's Messages collection URL (carries only the public SID)."""
        return f"{self._base}/2010-04-01/Accounts/{self._sid}/Messages.json"

    def _account_url(self) -> str:
        """The account resource URL (a startup creds check; carries only the public SID)."""
        return f"{self._base}/2010-04-01/Accounts/{self._sid}.json"

    async def _call(self, method: str, url: str, *, data: dict[str, str] | None = None) -> object:
        """Send one Twilio request and return its decoded body (or raise a domain error).

        Args:
            method: The logical method name (e.g. ``"send_message"``) — for the context.
            url: The fully-built request URL (carries only the public Account SID).
            data: The form-encoded body for a POST, or ``None`` for a GET.

        Returns:
            The decoded JSON reply body.

        Raises:
            TwilioRateLimitError: Twilio returned ``429`` — back off and retry.
            TwilioApiError: Any other transport fault or logical rejection.
        """
        try:
            if data is None:
                response = await self._http.get(url, auth=self._auth())
            else:
                response = await self._http.post(url, data=data, auth=self._auth())
        except httpx.HTTPError:
            # The httpx exception can reference the auth — suppress it entirely
            # (``from None``) so the token never reaches a traceback.
            raise TwilioApiError("twilio request failed", context={"method": method}) from None
        return self._parse(method, response)

    @staticmethod
    def _parse(method: str, response: httpx.Response) -> object:
        """Parse a Twilio reply, mapping 429 + non-2xx to domain errors."""
        try:
            raw: object = response.json()
        except ValueError:
            raw = None

        if 200 <= response.status_code < 300:
            if not isinstance(raw, dict):
                raise TwilioApiError(
                    "twilio returned a non-object response",
                    context={"method": method, "status": str(response.status_code)},
                )
            return raw

        code: object = None
        message = ""
        if isinstance(raw, dict):
            code = raw.get("code")
            message = str(raw.get("message", "")).strip()
        context = {
            "method": method,
            "status": str(response.status_code),
            "error_code": str(code),
        }
        if response.status_code == 429:
            raise TwilioRateLimitError(message or "twilio rate-limited", context=context)
        raise TwilioApiError(
            f"twilio API error: {message}" if message else "twilio API error", context=context
        )

    @staticmethod
    def _result(body: object, method: str) -> TwilioMessageResult:
        """Narrow a Messages create reply to :class:`TwilioMessageResult`."""
        if not isinstance(body, dict):
            raise TwilioApiError(
                "twilio returned an unexpected result shape", context={"method": method}
            )
        sid = body.get("sid")
        status = body.get("status")
        error_code = body.get("error_code")
        return TwilioMessageResult(
            sid=str(sid) if sid is not None else "",
            status=str(status) if status is not None else "",
            error_code=int(error_code)
            if isinstance(error_code, int) and not isinstance(error_code, bool)
            else None,
        )

    async def send_message(
        self,
        *,
        to: str,
        from_: str,
        body: str | None = None,
        content_sid: str | None = None,
        content_variables: str | None = None,
        status_callback: str | None = None,
    ) -> TwilioMessageResult:
        """Send a message (the Messages API create) and return its sid/status/error_code.

        ``to`` / ``from_`` are ``whatsapp:+E164`` (WhatsApp) or bare ``+E164`` (SMS) —
        the channel is chosen by the prefix (D-C4-1). A free-form text send sets
        ``body``; a WhatsApp re-engagement template send sets ``content_sid`` (+ optional
        ``content_variables``) and omits ``body``. ``status_callback`` registers the
        async delivery-receipt URL (T9). The reply's ``error_code`` is SURFACED, not
        interpreted — the 63016 out-of-window mapping is T9/T12.
        """
        data: dict[str, str] = {"To": to, "From": from_}
        if body is not None:
            data["Body"] = body
        if content_sid is not None:
            data["ContentSid"] = content_sid
        if content_variables is not None:
            data["ContentVariables"] = content_variables
        if status_callback is not None:
            data["StatusCallback"] = status_callback
        result = await self._call("send_message", self._messages_url(), data=data)
        return self._result(result, "send_message")

    async def validate(self) -> None:
        """Fail-fast on bad creds at startup (``GET`` the account resource — mirror getMe).

        A successful 2xx confirms the Account SID + auth token are valid; any rejection
        raises a :class:`~persona_connectors.errors.TwilioApiError` so the service never
        starts a Twilio channel with credentials that cannot send.
        """
        await self._call("validate", self._account_url())
