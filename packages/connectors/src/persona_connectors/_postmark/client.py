"""The Postmark transactional-email client (Spec C5, Group C) — the thin send boundary.

Postmark's send API is JSON-over-HTTPS: ``POST {base}/email`` with the server token in the
``X-Postmark-Server-Token`` header and a body ``{From, To, Subject, TextBody, ReplyTo?,
Headers?, MessageStream}``; the reply is ``{MessageID, ErrorCode, Message, …}`` where
``ErrorCode == 0`` is success. We talk to it with ``httpx`` directly rather than the
Postmark SDK (the no-new-dep rule; the SDK would invert control + duplicate the flow) — the
single Postmark I/O boundary, swappable behind the ``EmailConnector`` (C1-D-1).

**Credential safety (mirrors the C4 D-C2-X-credential rule).** The server token is a
:class:`~pydantic.SecretStr`, unwrapped ONLY at the request header and NEVER in a log line /
exception / ``context``. On an ``httpx`` failure the underlying exception is suppressed
(``raise … from None``) so the token never reaches a traceback; the raised
:class:`~persona_connectors.errors.PostmarkApiError` carries only the method + status +
Postmark ``ErrorCode``. Postmark's ``Message`` text is safe to surface (no secret).

**Threading is surfaced, not interpreted.** ``send_email`` takes optional ``reply_to`` +
custom ``headers`` (``In-Reply-To`` / ``References``) so the connector can thread a reply;
this boundary only sends. api-free (httpx + persona-core errors only).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, ConfigDict

from persona_connectors.errors import PostmarkApiError, PostmarkRateLimitError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import SecretStr

__all__ = ["PostmarkClient", "PostmarkSendResult"]


class PostmarkSendResult(BaseModel):
    """A Postmark send reply, narrowed to what the connector needs.

    Attributes:
        message_id: Postmark's ``MessageID`` for the accepted email (the per-message id;
            becomes the sent message's ``Message-ID`` root for threading).
        error_code: Postmark's ``ErrorCode`` — ``0`` on success. The client raises on a
            non-zero code, so a returned result always carries ``0`` (kept for parity /
            observability).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: str
    error_code: int = 0


class PostmarkClient:
    """A thin async client over the Postmark send API (Spec C5).

    Holds the server token (a :class:`~pydantic.SecretStr`) + an injected
    :class:`httpx.AsyncClient` (DI — the composition root owns the timeouts/pool). No
    globals, no module state.
    """

    def __init__(
        self,
        *,
        server_token: SecretStr,
        http: httpx.AsyncClient,
        api_base_url: str = "https://api.postmarkapp.com",
    ) -> None:
        self._token = server_token
        self._http = http
        self._base = api_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        """Request headers. Contains the server token — NEVER log or surface this."""
        return {
            "X-Postmark-Server-Token": self._token.get_secret_value(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def send_email(
        self,
        *,
        from_: str,
        to: str,
        subject: str,
        text_body: str,
        reply_to: str | None = None,
        headers: Sequence[tuple[str, str]] | None = None,
        message_stream: str = "outbound",
    ) -> PostmarkSendResult:
        """Send one email (the Postmark send) and return its ``MessageID``.

        ``from_`` is the rendered ``From`` (author-affordance display-name + our inbound
        address); ``headers`` carries threading (``In-Reply-To`` / ``References``) as
        ``(Name, Value)`` pairs. Raises :class:`~persona_connectors.errors.PostmarkRateLimitError`
        on 429 (→ the connector maps ``pending``) and
        :class:`~persona_connectors.errors.PostmarkApiError` on any other transport fault,
        non-2xx, or non-zero ``ErrorCode`` (→ ``failed``) — never a silent drop.
        """
        payload: dict[str, object] = {
            "From": from_,
            "To": to,
            "Subject": subject,
            "TextBody": text_body,
            "MessageStream": message_stream,
        }
        if reply_to is not None:
            payload["ReplyTo"] = reply_to
        if headers:
            payload["Headers"] = [{"Name": name, "Value": value} for name, value in headers]
        try:
            response = await self._http.post(
                f"{self._base}/email", json=payload, headers=self._headers()
            )
        except httpx.HTTPError:
            raise PostmarkApiError(
                "postmark request failed", context={"method": "send_email"}
            ) from None
        return self._result(response)

    @staticmethod
    def _result(response: httpx.Response) -> PostmarkSendResult:
        """Map a Postmark reply to a result, raising a domain error on rate-limit / failure."""
        try:
            raw: object = response.json()
        except ValueError:
            raw = None
        body = raw if isinstance(raw, dict) else {}
        error_code = body.get("ErrorCode")
        message = str(body.get("Message", "")).strip()
        context = {
            "method": "send_email",
            "status": str(response.status_code),
            "error_code": str(error_code),
        }
        if response.status_code == 429:  # noqa: PLR2004 — HTTP 429 Too Many Requests
            raise PostmarkRateLimitError(message or "postmark rate-limited", context=context)
        ok_status = 200 <= response.status_code < 300  # noqa: PLR2004 — 2xx success band
        if not ok_status or error_code != 0:
            raise PostmarkApiError(
                f"postmark API error: {message}" if message else "postmark API error",
                context=context,
            )
        message_id = body.get("MessageID")
        return PostmarkSendResult(message_id=str(message_id) if message_id is not None else "")
