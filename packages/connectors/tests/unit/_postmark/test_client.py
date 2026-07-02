"""The Postmark send client (Spec C5, Group C) — the thin httpx boundary, against MockTransport.

Proves the send shape (token in the header, threading headers passed, MessageID returned) and
the never-silent outcome mapping (429 → rate-limit, ErrorCode/non-2xx → api error, transport
fault → api error) — without leaking the server token into an error. The REAL Postmark send is
the R4 live-Postmark leg; this proves the client's contract, not Postmark's behaviour.
"""

from __future__ import annotations

import json

import httpx
import pytest
from persona_connectors._postmark.client import PostmarkClient
from persona_connectors.errors import PostmarkApiError, PostmarkRateLimitError
from pydantic import SecretStr


def _client(handler: httpx.MockTransport | object) -> PostmarkClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return PostmarkClient(server_token=SecretStr("server-tok"), http=http)


@pytest.mark.asyncio
async def test_send_success_returns_message_id_with_token_in_header() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["token"] = request.headers.get("X-Postmark-Server-Token")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"MessageID": "msg-1", "ErrorCode": 0, "Message": "OK"})

    result = await _client(handler).send_email(
        from_="Astrid via Open Persona <inbound@x>", to="u@x", subject="Hi", text_body="body"
    )
    assert result.message_id == "msg-1"
    assert captured["token"] == "server-tok"  # token rides in the header
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["From"] == "Astrid via Open Persona <inbound@x>"
    assert body["MessageStream"] == "outbound"


@pytest.mark.asyncio
async def test_threading_headers_are_sent() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"MessageID": "m", "ErrorCode": 0})

    await _client(handler).send_email(
        from_="A <a@x>",
        to="u@x",
        subject="Re: Hi",
        text_body="b",
        headers=[("In-Reply-To", "<r@x>"), ("References", "<root@x>")],
    )
    body = captured["body"]
    assert isinstance(body, dict)
    assert {"Name": "In-Reply-To", "Value": "<r@x>"} in body["Headers"]
    assert {"Name": "References", "Value": "<root@x>"} in body["Headers"]


@pytest.mark.asyncio
async def test_nonzero_error_code_maps_to_api_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"ErrorCode": 300, "Message": "Invalid 'To' address"})

    with pytest.raises(PostmarkApiError):
        await _client(handler).send_email(from_="a", to="bad", subject="s", text_body="t")


@pytest.mark.asyncio
async def test_429_maps_to_rate_limit_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"Message": "rate exceeded"})

    with pytest.raises(PostmarkRateLimitError):
        await _client(handler).send_email(from_="a", to="b", subject="s", text_body="t")


@pytest.mark.asyncio
async def test_transport_fault_is_api_error_and_never_leaks_the_token() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    with pytest.raises(PostmarkApiError) as exc:
        await _client(handler).send_email(from_="a", to="b", subject="s", text_body="t")
    assert "server-tok" not in str(exc.value)  # the token never reaches the error
    assert "server-tok" not in str(exc.value.context)
