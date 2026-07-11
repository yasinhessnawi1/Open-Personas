"""TwilioClient — the Twilio Messages API transport boundary (Spec C4 T2).

Exercised entirely offline via ``httpx.MockTransport`` (no network): a handler
inspects the request and returns a canned Twilio reply, so the tests assert the
client's request shape (form-encoded, HTTP Basic auth) + its mapping of
replies/faults to domain errors, and — critically — that the auth token NEVER
leaks into an error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qs

import httpx
import pytest
from persona_connectors._twilio.client import TwilioClient, TwilioMessageResult
from persona_connectors.errors import TwilioApiError, TwilioRateLimitError
from pydantic import SecretStr

if TYPE_CHECKING:
    from collections.abc import Callable

_SID = "AC" + "0" * 32  # scanner-safe fake (GH push protection flags realistic SIDs)
_TOKEN = "SUPER-SECRET-AUTH-TOKEN"  # noqa: S105 — test literal


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> TwilioClient:
    """Build a TwilioClient whose httpx client routes through a mock handler."""
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return TwilioClient(account_sid=_SID, auth_token=SecretStr(_TOKEN), http=http)


@pytest.mark.asyncio
async def test_send_message_posts_form_encoded_and_returns_result() -> None:
    """send_message form-encodes To/From/Body and returns sid/status/error_code."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = parse_qs(request.content.decode())
        return httpx.Response(201, json={"sid": "SM123", "status": "queued", "error_code": None})

    client = _client(handler)
    result = await client.send_message(to="+14155551234", from_="+14155238886", body="hi")

    assert isinstance(result, TwilioMessageResult)
    assert result.sid == "SM123"
    assert result.status == "queued"
    assert result.error_code is None
    assert seen["body"] == {"To": ["+14155551234"], "From": ["+14155238886"], "Body": ["hi"]}
    assert "application/x-www-form-urlencoded" in str(seen["content_type"])
    # The account SID (not the token) rides in the URL path.
    assert str(seen["url"]).endswith(f"/Accounts/{_SID}/Messages.json")


@pytest.mark.asyncio
async def test_send_message_uses_http_basic_auth() -> None:
    """HTTP Basic auth is AccountSid:AuthToken (the Twilio auth scheme)."""
    import base64

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization", "")
        return httpx.Response(201, json={"sid": "SM1", "status": "queued"})

    client = _client(handler)
    await client.send_message(to="+1", from_="+2", body="x")

    expected = base64.b64encode(f"{_SID}:{_TOKEN}".encode()).decode()
    assert seen["authorization"] == f"Basic {expected}"


@pytest.mark.asyncio
async def test_status_callback_is_included_when_set() -> None:
    """A StatusCallback URL is form-encoded when provided (the async delivery path, T9)."""
    seen: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(parse_qs(request.content.decode()))
        return httpx.Response(201, json={"sid": "SM1", "status": "queued"})

    client = _client(handler)
    await client.send_message(
        to="+1", from_="+2", body="x", status_callback="https://example.test/status"
    )
    assert seen["StatusCallback"] == ["https://example.test/status"]


@pytest.mark.asyncio
async def test_content_template_params_are_form_encoded() -> None:
    """ContentSid + ContentVariables ride the form (the WhatsApp re-engagement template)."""
    seen: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(parse_qs(request.content.decode()))
        return httpx.Response(201, json={"sid": "SM1", "status": "queued"})

    client = _client(handler)
    await client.send_message(
        to="+1", from_="+2", content_sid="HX123", content_variables='{"1":"Ada"}'
    )
    assert seen["ContentSid"] == ["HX123"]
    assert seen["ContentVariables"] == ['{"1":"Ada"}']
    assert "Body" not in seen  # template send omits Body


@pytest.mark.asyncio
async def test_create_time_error_code_is_surfaced_on_the_result() -> None:
    """An out-of-window WhatsApp send returns status=failed + error_code 63016 (T9 maps it)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"sid": "SM9", "status": "failed", "error_code": 63016})

    client = _client(handler)
    result = await client.send_message(to="+1", from_="+2", body="x")
    # The client only SURFACES error_code — mapping 63016 → DeliveryOutcome is T9/T12.
    assert result.status == "failed"
    assert result.error_code == 63016


@pytest.mark.asyncio
async def test_4xx_rejection_maps_to_twilio_api_error_with_safe_context() -> None:
    """A non-2xx Twilio error body becomes a TwilioApiError carrying status + code (no token)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"code": 21211, "message": "The 'To' number is not a valid phone number"},
        )

    client = _client(handler)
    with pytest.raises(TwilioApiError) as excinfo:
        await client.send_message(to="bad", from_="+2", body="x")

    err = excinfo.value
    assert err.context["method"] == "send_message"
    assert err.context["status"] == "400"
    assert err.context["error_code"] == "21211"
    assert _TOKEN not in str(err)
    assert _TOKEN not in repr(err)


@pytest.mark.asyncio
async def test_429_maps_to_rate_limit_error() -> None:
    """A 429 becomes a TwilioRateLimitError (a TwilioApiError subclass)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"code": 20429, "message": "Too Many Requests"})

    client = _client(handler)
    with pytest.raises(TwilioRateLimitError) as excinfo:
        await client.send_message(to="+1", from_="+2", body="x")
    assert isinstance(excinfo.value, TwilioApiError)
    assert _TOKEN not in str(excinfo.value)


@pytest.mark.asyncio
async def test_network_fault_maps_to_domain_error_without_leaking_token() -> None:
    """A transport fault becomes a TwilioApiError — and the auth token NEVER leaks.

    The token rides in the Basic-auth header; the httpx exception is suppressed
    (``from None``) so it never reaches a traceback (the D-C4-1 credential guarantee).
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    with pytest.raises(TwilioApiError) as excinfo:
        await client.send_message(to="+1", from_="+2", body="x")

    err = excinfo.value
    assert err.context == {"method": "send_message"}
    assert _TOKEN not in str(err)
    assert _TOKEN not in repr(err)
    assert err.__cause__ is None  # httpx exception suppressed (from None)
    assert err.__suppress_context__ is True


@pytest.mark.asyncio
async def test_non_object_response_maps_to_domain_error() -> None:
    """A non-object JSON body is a fault, not a crash."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=["unexpected"])

    client = _client(handler)
    with pytest.raises(TwilioApiError):
        await client.send_message(to="+1", from_="+2", body="x")


@pytest.mark.asyncio
async def test_validate_gets_the_account_resource() -> None:
    """validate() GETs the account resource to fail-fast on bad creds (mirror getMe)."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"sid": _SID, "status": "active"})

    client = _client(handler)
    await client.validate()
    assert seen["method"] == "GET"
    assert str(seen["url"]).endswith(f"/Accounts/{_SID}.json")
