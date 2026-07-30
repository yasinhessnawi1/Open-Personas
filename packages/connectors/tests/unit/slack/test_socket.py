"""Slack socket mode (Spec C3 ⛔) — connection-auth boundary + envelope ack/dispatch.

The socket's trust boundary is connection-auth (the app-token opens the WS — unlike HTTP
events' per-request signing). Tests: ``apps.connections.open`` carries the app-token (only its
holder can open the socket); ``interpret_envelope`` is pure/total; the driven ``run`` acks then
dispatches an ``events_api`` envelope and reconnects on ``disconnect``.
"""

from __future__ import annotations

# ruff: noqa: ARG001 — httpx MockTransport handlers must take `request`; some ignore it.
import asyncio
import json

import httpx
import pytest
from persona_connectors.errors import SlackApiError
from persona_connectors.slack.socket import (
    SlackSocketClient,
    SocketDisconnect,
    SocketEvent,
    SocketHello,
    SocketIgnore,
    build_ack,
    interpret_envelope,
)
from pydantic import SecretStr
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

_APP_TOKEN = "xapp-socket-token.secret"  # noqa: S105 — test literal


class _ClosedError(Exception):
    """Stand-in for a dropped WebSocket."""


class _FakeConn:
    def __init__(self, incoming: list[str] | None = None) -> None:
        self._incoming = list(incoming) if incoming else []
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self._incoming:
            raise _ClosedError
        return self._incoming.pop(0)

    async def close(self) -> None:
        self.closed = True


class _FakeConnClosedOnSend:
    """A connection whose ``send`` (the ack) always raises a ``ConnectionClosed`` subclass.

    Stands in for the exact R9-071 shape sibling: the socket closed (a normal Slack
    ``disconnect`` refresh, or a network drop) between an envelope arriving and this
    client acking it (production, 2026-07-29 pattern).
    """

    def __init__(self, incoming: list[str], closed_exc: type[Exception]) -> None:
        self._incoming = list(incoming)
        self._closed_exc = closed_exc
        self.sent: list[str] = []
        self.closed = False

    async def send(self, _message: str) -> None:
        raise self._closed_exc(None, None)  # type: ignore[call-arg]

    async def recv(self) -> str:
        if not self._incoming:
            raise _ClosedError
        return self._incoming.pop(0)

    async def close(self) -> None:
        self.closed = True


class _CancellingConn:
    """A connection whose ``recv`` raises ``CancelledError`` — a normal shutdown."""

    async def send(self, _message: str) -> None:
        raise AssertionError("send should not be reached before the cancellation")

    async def recv(self) -> str:
        raise asyncio.CancelledError

    async def close(self) -> None:
        return None


# --- pure interpretation ---


def test_interpret_envelope_kinds() -> None:
    assert isinstance(interpret_envelope({"type": "hello"}), SocketHello)
    assert interpret_envelope({"type": "disconnect", "reason": "warning"}) == SocketDisconnect(
        reason="warning"
    )
    event = interpret_envelope(
        {"type": "events_api", "envelope_id": "e1", "payload": {"event": {"type": "message"}}}
    )
    assert event == SocketEvent(envelope_id="e1", event={"type": "message"})
    assert isinstance(interpret_envelope({"type": "events_api"}), SocketIgnore)  # no envelope_id
    assert isinstance(interpret_envelope({"type": "whatever"}), SocketIgnore)


def test_build_ack() -> None:
    assert build_ack("e1") == {"envelope_id": "e1"}


# --- the app-token connection-auth boundary (apps.connections.open) ---


@pytest.mark.asyncio
async def test_open_connection_url_uses_the_app_token() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/apps.connections.open"
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"ok": True, "url": "wss://socket.test/link"})

    client = _client(handler, _FakeConn())
    assert await client.open_connection_url() == "wss://socket.test/link"
    assert seen["auth"] == f"Bearer {_APP_TOKEN}"  # the app-token IS the connection auth


@pytest.mark.asyncio
async def test_open_connection_ok_false_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

    with pytest.raises(SlackApiError):
        await _client(handler, _FakeConn()).open_connection_url()


# --- the driven run loop: open → ack + dispatch → reconnect on disconnect ---


@pytest.mark.asyncio
async def test_run_acks_then_dispatches_an_event() -> None:
    events: list[dict[str, object]] = []

    async def on_event(event: dict[str, object]) -> None:
        events.append(event)

    conn = _FakeConn(
        [
            json.dumps({"type": "hello"}),
            json.dumps(
                {
                    "type": "events_api",
                    "envelope_id": "e1",
                    "payload": {"event": {"type": "message", "text": "hi"}},
                }
            ),
            json.dumps({"type": "disconnect", "reason": "refresh"}),
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "url": "wss://socket.test/link"})

    client = _client(handler, conn, on_event=on_event)
    await client.run(should_continue=lambda: not conn.closed)

    # The envelope was acked (Slack re-delivers unacked) BEFORE dispatch.
    assert json.loads(conn.sent[0]) == {"envelope_id": "e1"}
    assert events == [{"type": "message", "text": "hi"}]


# --- R9-071 sibling: a closed connection on the ack SEND must reconnect, never raise ---


@pytest.mark.asyncio
@pytest.mark.parametrize("closed_exc", [ConnectionClosedOK, ConnectionClosedError])
async def test_send_on_a_closed_connection_returns_false_not_raises(
    closed_exc: type[Exception],
) -> None:
    """The point fix, mirroring Discord gateway's ``_send`` (``discord/gateway.py``): a
    send racing a closed socket must be swallowed and reported ``False``, never raise
    ``ConnectionClosed`` out of the ack path."""
    conn = _FakeConnClosedOnSend([], closed_exc)

    async def _noop_event(_event: dict[str, object]) -> None:
        return None

    async def _connect(_url: str) -> object:
        return conn

    client = SlackSocketClient(
        app_token=SecretStr(_APP_TOKEN),
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200))  # type: ignore[arg-type]
        ),
        on_event=_noop_event,
        connect=_connect,  # type: ignore[arg-type]
    )
    sent = await client._send(conn, {"envelope_id": "e1"})  # noqa: SLF001 — testing the guard directly
    assert sent is False


@pytest.mark.asyncio
@pytest.mark.parametrize("closed_exc", [ConnectionClosedOK, ConnectionClosedError])
async def test_run_reconnects_when_the_ack_send_hits_a_closed_connection(
    closed_exc: type[Exception],
) -> None:
    """The Slack sibling of the Discord R9-071 crash: the envelope ack racing a routine
    socket close must reconnect ``run``, not propagate ``ConnectionClosed`` out to the
    service's ``asyncio.gather`` (which would kill every OTHER platform + the HTTP
    server along with this one, 2026-07-29 production shape). A SECOND ``connect``
    attempt happening proves the loop reconnected instead of crashing; the event whose
    ack failed is never dispatched (Slack re-delivers the unacked envelope on its own).
    """
    envelope = json.dumps(
        {
            "type": "events_api",
            "envelope_id": "e1",
            "payload": {"event": {"type": "message", "text": "hi"}},
        }
    )
    closing_conn = _FakeConnClosedOnSend([envelope], closed_exc)
    fresh_conn = _FakeConn()  # empty incoming → recv drops immediately, ending pass 2
    conns = iter([closing_conn, fresh_conn])
    connect_calls = 0

    async def connect(_url: str) -> object:
        nonlocal connect_calls
        connect_calls += 1
        return next(conns)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "url": "wss://socket.test/link"})

    events: list[dict[str, object]] = []

    async def on_event(event: dict[str, object]) -> None:
        events.append(event)

    client = SlackSocketClient(
        app_token=SecretStr(_APP_TOKEN),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
        on_event=on_event,
        connect=connect,  # type: ignore[arg-type]
        api_base_url="https://slack.test/api",
    )

    # Must NOT raise — the closed ack send reconnects instead of crashing the loop.
    await client.run(should_continue=lambda: connect_calls < 2)

    assert connect_calls == 2  # reconnected after the closed send
    assert events == []  # the ack never got through, so the event was never dispatched


@pytest.mark.asyncio
async def test_run_propagates_cancelled_error_for_clean_shutdown() -> None:
    """Normal shutdown (``asyncio.CancelledError``) must propagate through ``run``
    untouched — the reconnect-on-closed-send guard only ever swallows a genuine
    ``ConnectionClosed``, never a deliberate cancellation."""

    async def connect(_url: str) -> _CancellingConn:
        return _CancellingConn()

    async def _noop_event(_event: dict[str, object]) -> None:
        return None

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "url": "wss://socket.test/link"})

    client = SlackSocketClient(
        app_token=SecretStr(_APP_TOKEN),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
        on_event=_noop_event,
        connect=connect,  # type: ignore[arg-type]
        api_base_url="https://slack.test/api",
    )
    with pytest.raises(asyncio.CancelledError):
        await client.run()


def _client(handler: object, conn: _FakeConn, *, on_event: object = None) -> SlackSocketClient:
    async def _noop_event(_event: dict[str, object]) -> None:
        return None

    async def connect(_url: str) -> _FakeConn:
        return conn

    return SlackSocketClient(
        app_token=SecretStr(_APP_TOKEN),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
        on_event=on_event or _noop_event,  # type: ignore[arg-type]
        connect=connect,  # type: ignore[arg-type]
        api_base_url="https://slack.test/api",
    )
