"""Slack socket-mode transport (Spec C3 ⛔, D-C3-2/D-C3-3) — the default inbound WS.

Socket mode is the **connection-authenticated** Slack transport (the default — no public
endpoint needed): the **app-level token** (``xapp-…``, ``connections:write``) opens a
WebSocket via ``apps.connections.open``, and events flow over that pre-authorized socket.

**The trust boundary (D-C3-3) is connection-auth — like Discord's gateway, UNLIKE the
HTTP-events per-request signing.** Only the holder of the app-token can open the socket, so
the WS URL is the authenticated event channel; events on it are trusted (Slack does not
re-sign socket envelopes). The app-token is a ``SecretStr``, used only in the
``apps.connections.open`` ``Authorization`` header, never logged.

Each envelope must be **acked** (``{"envelope_id": …}``) or Slack re-delivers it. The pure
envelope interpretation (:func:`interpret_envelope`, :func:`build_ack`) is unit-tested; the
live recv loop (:meth:`SlackSocketClient.run`) is the deploy seam, exercised by the operator
pass (the ``RuntimeFactory``-is-a-live-seam posture). api-free (httpx + ``websockets``).
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx
from persona.logging import get_logger
from pydantic import BaseModel, ConfigDict
from websockets.exceptions import ConnectionClosed

from persona_connectors.errors import SlackApiError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic import SecretStr

__all__ = [
    "SocketDisconnect",
    "SocketEnvelope",
    "SocketEvent",
    "SocketHello",
    "SocketIgnore",
    "SlackSocketClient",
    "SlackSocketConnection",
    "build_ack",
    "interpret_envelope",
]

_log = get_logger("connectors.slack_socket")


def _endpoint(url: str) -> str:
    """The socket URL's HOST only — the query string carries a one-time connection ticket.

    Socket-mode URLs look like ``wss://wss-primary.slack.com/link/?ticket=…&app_id=…``.
    The ticket is a credential, so only the host is ever logged (the app-token itself is a
    ``SecretStr`` that never leaves the ``apps.connections.open`` header — module docstring).
    """
    return urlsplit(url).netloc or "<unparsable>"


class SocketHello(BaseModel):
    """The socket ``hello`` — the connection is up."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class SocketEvent(BaseModel):
    """An ``events_api`` envelope — ``envelope_id`` (to ack) + the inner ``event``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    envelope_id: str
    event: dict[str, object]


class SocketDisconnect(BaseModel):
    """A ``disconnect`` envelope — close and reconnect (Slack refreshes the socket URL)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str


class SocketIgnore(BaseModel):
    """An envelope with nothing to act on (an unknown type / malformed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str


SocketEnvelope = SocketHello | SocketEvent | SocketDisconnect | SocketIgnore


def interpret_envelope(envelope: dict[str, object]) -> SocketEnvelope:
    """Map a decoded socket-mode envelope to a :data:`SocketEnvelope` (pure, total)."""
    kind = envelope.get("type")
    if kind == "hello":
        return SocketHello()
    if kind == "disconnect":
        reason = envelope.get("reason")
        return SocketDisconnect(reason=reason if isinstance(reason, str) else "")
    if kind == "events_api":
        envelope_id = envelope.get("envelope_id")
        payload = envelope.get("payload")
        event = payload.get("event") if isinstance(payload, dict) else None
        if isinstance(envelope_id, str) and envelope_id:
            return SocketEvent(
                envelope_id=envelope_id, event=event if isinstance(event, dict) else {}
            )
        return SocketIgnore(reason="events-api-without-envelope-id")
    return SocketIgnore(reason="unknown-type")


def build_ack(envelope_id: str) -> dict[str, object]:
    """Build the ack a received envelope requires (or Slack re-delivers it)."""
    return {"envelope_id": envelope_id}


@runtime_checkable
class SlackSocketConnection(Protocol):
    """The minimal WebSocket surface socket mode needs (a ``websockets`` connection)."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


class SlackSocketClient:
    """Maintains the Slack socket-mode connection (the default inbound transport).

    Dependencies injected (DI): the app-level token (``SecretStr``), an ``httpx`` client (for
    ``apps.connections.open``), the ``on_event`` handler (the flow), a ``connect`` factory
    (wraps ``websockets.connect``), and ``sleep`` (for reconnect backoff, injected for tests).
    """

    def __init__(
        self,
        *,
        app_token: SecretStr,
        http: httpx.AsyncClient,
        on_event: Callable[[dict[str, object]], Awaitable[None]],
        connect: Callable[[str], Awaitable[SlackSocketConnection]],
        api_base_url: str = "https://slack.com/api",
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._app_token = app_token
        self._http = http
        self._on_event = on_event
        self._connect = connect
        self._base = api_base_url.rstrip("/")
        self._sleep = sleep

    async def open_connection_url(self) -> str:
        """Open a socket-mode URL via ``apps.connections.open`` (app-token connection-auth).

        The app-token authenticates this call — only its holder can open the socket, so the
        returned WS URL is the authenticated event channel (the trust boundary, D-C3-3).
        """
        try:
            response = await self._http.post(
                f"{self._base}/apps.connections.open",
                headers={"Authorization": f"Bearer {self._app_token.get_secret_value()}"},
            )
        except httpx.HTTPError:
            raise SlackApiError(
                "slack socket open failed", context={"method": "apps.connections.open"}
            ) from None
        if response.status_code != 200:
            raise SlackApiError(
                "slack socket open rejected",
                context={"method": "apps.connections.open", "status": str(response.status_code)},
            )
        try:
            body: object = response.json()
        except ValueError:
            body = None
        url = body.get("url") if isinstance(body, dict) and body.get("ok") is True else None
        if not isinstance(url, str) or not url:
            raise SlackApiError(
                "slack apps.connections.open returned no url",
                context={"method": "apps.connections.open"},
            )
        return url

    async def _send(self, conn: SlackSocketConnection, payload: dict[str, object]) -> bool:
        """Send a socket frame; return ``False`` (never raise) if the peer already closed.

        A socket-mode connection can close (a normal ``disconnect`` refresh, a network
        drop) between the envelope arriving and this ack going out — that races exactly
        like Discord's gateway send-on-a-closing-connection (R9-071). Left unguarded,
        ``ConnectionClosed`` escapes ``_receive_loop`` → ``run`` → the service's
        ``asyncio.gather``, killing every platform + the HTTP server, not just this
        socket (the same production shape, 2026-07-29 sibling fix).
        """
        try:
            await conn.send(json.dumps(payload))
        except ConnectionClosed as exc:
            _log.info(
                "slack socket: send on a closed connection ({error}); reconnecting",
                error=str(exc),
            )
            return False
        return True

    async def run(self, *, should_continue: Callable[[], bool] = lambda: True) -> None:
        """Maintain the socket: open → recv envelopes → ack + dispatch → reconnect on close.

        The live I/O loop (the deploy seam). Each ``events_api`` envelope is **acked** then its
        inner event dispatched; a ``disconnect`` (or a drop) reconnects with a fresh URL.

        R9-078 (observability): this loop used to log NOTHING — not the open, not the
        ``hello``, not a single arriving envelope — so a Slack that never replied could not
        be localised between "the socket never opened", "the socket is open but Slack sends
        no events" (an owner-side Event-Subscriptions / bot-scope leg) and "events arrive but
        the flow drops them". Every step now says so. The app token is never logged (it is a
        ``SecretStr`` used only in the open call's header) and neither is the socket URL's
        one-time ticket — see :func:`_endpoint`.
        """
        connections = 0
        while should_continue():
            connections += 1
            _log.info(
                "slack socket: opening a connection URL (attempt #{n}) via apps.connections.open",
                n=connections,
            )
            url = await self.open_connection_url()
            _log.info(
                "slack socket: connecting to {endpoint} (attempt #{n})",
                endpoint=_endpoint(url),
                n=connections,
            )
            conn = await self._connect(url)
            _log.info("slack socket: connected to {endpoint}", endpoint=_endpoint(url))
            try:
                await self._receive_loop(conn, should_continue)
            finally:
                with contextlib.suppress(Exception):
                    await conn.close()
            _log.info(
                "slack socket: session #{n} ended — reconnecting with a fresh URL "
                "(continue={cont})",
                n=connections,
                cont=should_continue(),
            )

    async def _receive_loop(
        self, conn: SlackSocketConnection, should_continue: Callable[[], bool]
    ) -> None:
        while should_continue():
            try:
                raw = await conn.recv()
            except Exception as exc:  # noqa: BLE001 — a drop ends the session → reconnect
                _log.info(
                    "slack socket: receive ended ({error_class}: {error}) — reconnecting",
                    error_class=type(exc).__name__,
                    error=str(exc),
                )
                return
            try:
                envelope = json.loads(raw)
            except (ValueError, TypeError):
                _log.warning("slack socket: dropped a frame that is not valid JSON")
                continue
            if not isinstance(envelope, dict):
                _log.warning("slack socket: dropped a JSON frame that is not an object")
                continue
            directive = interpret_envelope(envelope)
            if isinstance(directive, SocketHello):
                # The connection is live and Slack has accepted it. If this line appears but
                # no `events_api` line ever follows, the gap is on the Slack APP side (Event
                # Subscriptions off / the bot not subscribed to message.im or app_mention),
                # not in this process.
                _log.info("slack socket: hello — the connection is live, awaiting events")
            elif isinstance(directive, SocketIgnore):
                _log.warning(
                    "slack socket: ignoring an envelope ({reason} type={type})",
                    reason=directive.reason,
                    type=str(envelope.get("type", "<none>")),
                )
            if isinstance(directive, SocketEvent):
                # Envelope TYPES only — never the message text (a Slack event carries the
                # user's words in `text`).
                _log.info(
                    "slack socket: events_api envelope {envelope_id} "
                    "(event_type={event_type} subtype={subtype} channel_type={channel_type})",
                    envelope_id=directive.envelope_id,
                    event_type=str(directive.event.get("type", "<none>")),
                    subtype=str(directive.event.get("subtype", "<none>")),
                    channel_type=str(directive.event.get("channel_type", "<none>")),
                )
                # Ack first (Slack re-delivers unacked envelopes), then dispatch the event.
                # A closed connection on the ack send (R9-071 sibling) must reconnect, not
                # raise — the event is simply not dispatched this round; Slack will
                # re-deliver the unacked envelope once the fresh socket is open.
                sent = await self._send(conn, build_ack(directive.envelope_id))
                if not sent:
                    return  # reconnect with a fresh URL
                if directive.event:
                    await self._on_event(directive.event)
                else:
                    _log.warning(
                        "slack socket: envelope {envelope_id} carried no inner event — acked, "
                        "not dispatched",
                        envelope_id=directive.envelope_id,
                    )
            elif isinstance(directive, SocketDisconnect):
                _log.info(
                    "slack socket: disconnect requested by Slack (reason={reason}) — reconnecting",
                    reason=directive.reason or "<none>",
                )
                return  # reconnect with a fresh URL
