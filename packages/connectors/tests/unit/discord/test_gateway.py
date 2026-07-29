"""Discord gateway (Spec C3 ⛔) — trust boundary + heartbeat/ACK-watchdog/RESUME lifecycle.

The pure protocol layer (builders / interpret_frame / session) and the lifecycle
*decisions* (``_apply`` / ``_heartbeat_due``) are tested deterministically; ``run`` is
driven over a fake connection (injected ``sleep`` blocks so the heartbeat never fires)
to prove connect → IDENTIFY → dispatch and resume-on-reconnect end to end.

Trust boundary (D-C3-3): the IDENTIFY carries the bot token + ``intents=4096`` — the
connection's authentication; events on it are trusted (no per-message signature). DM-only
dispatch: only ``MESSAGE_CREATE`` reaches ``on_event`` (which then classify-DM-filters).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from persona_connectors.discord.gateway import (
    DiscordGateway,
    Dispatch,
    GatewaySession,
    HeartbeatAck,
    HeartbeatRequest,
    Hello,
    Ignore,
    InvalidSession,
    Reconnect,
    _Control,
    build_heartbeat,
    build_identify,
    build_resume,
    interpret_frame,
)
from pydantic import SecretStr
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

_TOKEN = "gw-bot-token.secret"  # noqa: S105 — test literal


class _ClosedError(Exception):
    """Stand-in for a dropped WebSocket (websockets.ConnectionClosed)."""


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


class _FakeConnClosesOnSecondSend:
    """A connection whose SECOND ``send`` raises a ``ConnectionClosed`` subclass.

    Discord's gateway routinely closes with 1000 (OK) and expects a reconnect — this
    stands in for a heartbeat/heartbeat-request response racing exactly that close
    (production, 2026-07-29). The FIRST send (IDENTIFY) must still succeed, so the fake
    only starts failing from the second call on.
    """

    def __init__(self, incoming: list[str], closed_exc: type[Exception]) -> None:
        self._incoming = list(incoming)
        self._closed_exc = closed_exc
        self.sent: list[str] = []
        self.closed = False
        self._send_count = 0

    async def send(self, message: str) -> None:
        self._send_count += 1
        if self._send_count >= 2:
            raise self._closed_exc(None, None)  # type: ignore[call-arg]
        self.sent.append(message)

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


async def _block(_seconds: float) -> None:
    """An injected sleep that blocks until cancelled — the heartbeat never fires in tests."""
    await asyncio.Event().wait()


def _gateway(
    *,
    on_event: object = None,
    connect: object = None,
    sleep: object = _block,
) -> DiscordGateway:
    async def _noop_event(_data: dict[str, object]) -> None:
        return None

    async def _noop_connect(_url: str) -> _FakeConn:
        return _FakeConn()

    return DiscordGateway(
        token=SecretStr(_TOKEN),
        on_event=on_event or _noop_event,  # type: ignore[arg-type]
        connect=connect or _noop_connect,  # type: ignore[arg-type]
        gateway_url="wss://gw.test",
        sleep=sleep,  # type: ignore[arg-type]
    )


def _dispatch(event_type: str, data: dict[str, object], seq: int) -> str:
    return json.dumps({"op": 0, "t": event_type, "s": seq, "d": data})


# --- payload builders (the trust anchor) ---


def test_build_identify_carries_token_and_dm_intents() -> None:
    payload = build_identify(_TOKEN, intents=4096)
    assert payload["op"] == 2
    assert payload["d"]["token"] == _TOKEN  # type: ignore[index]
    assert payload["d"]["intents"] == 4096  # DIRECT_MESSAGES only  # type: ignore[index]


def test_build_resume_and_heartbeat_shapes() -> None:
    assert build_resume(_TOKEN, session_id="s1", seq=7) == {
        "op": 6,
        "d": {"token": _TOKEN, "session_id": "s1", "seq": 7},
    }
    assert build_heartbeat(7) == {"op": 1, "d": 7}
    assert build_heartbeat(None) == {"op": 1, "d": None}


# --- interpret_frame (pure, total) ---


def test_interpret_hello() -> None:
    directive = interpret_frame({"op": 10, "d": {"heartbeat_interval": 41250}})
    assert directive == Hello(heartbeat_interval_ms=41250)


def test_interpret_hello_without_interval_is_ignored() -> None:
    assert isinstance(interpret_frame({"op": 10, "d": {}}), Ignore)


def test_interpret_dispatch() -> None:
    directive = interpret_frame({"op": 0, "t": "MESSAGE_CREATE", "s": 5, "d": {"id": "m"}})
    assert directive == Dispatch(event_type="MESSAGE_CREATE", data={"id": "m"}, seq=5)


def test_interpret_control_opcodes() -> None:
    assert isinstance(interpret_frame({"op": 11}), HeartbeatAck)
    assert isinstance(interpret_frame({"op": 1}), HeartbeatRequest)
    assert isinstance(interpret_frame({"op": 7}), Reconnect)
    assert interpret_frame({"op": 9, "d": True}) == InvalidSession(resumable=True)
    assert interpret_frame({"op": 9, "d": False}) == InvalidSession(resumable=False)
    assert isinstance(interpret_frame({"op": 99}), Ignore)


# --- GatewaySession ---


def test_session_ready_and_resume_and_reset() -> None:
    session = GatewaySession()
    assert session.can_resume() is False
    session.on_ready({"session_id": "s1", "resume_gateway_url": "wss://resume"})
    session.advance(3)
    assert session.can_resume() is True
    assert (session.session_id, session.resume_url, session.last_seq) == ("s1", "wss://resume", 3)
    session.reset()
    assert session.can_resume() is False


# --- the trust boundary + lifecycle decisions (_apply) ---


@pytest.mark.asyncio
async def test_hello_with_no_session_sends_identify() -> None:
    """HELLO with no session → IDENTIFY: the token authenticates the connection (trust anchor)."""
    gateway = _gateway()
    conn = _FakeConn()
    control = await gateway._apply(conn, Hello(heartbeat_interval_ms=45000))
    assert control is _Control.CONTINUE
    identify = json.loads(conn.sent[0])
    assert identify["op"] == 2
    assert identify["d"]["token"] == _TOKEN
    assert identify["d"]["intents"] == 4096


@pytest.mark.asyncio
async def test_hello_with_session_sends_resume() -> None:
    """HELLO with a live session → RESUME (replay missed events, no double-process)."""
    gateway = _gateway()
    gateway._session.on_ready({"session_id": "s1", "resume_gateway_url": "wss://r"})
    gateway._session.advance(9)
    conn = _FakeConn()
    await gateway._apply(conn, Hello(heartbeat_interval_ms=45000))
    resume = json.loads(conn.sent[0])
    assert resume["op"] == 6
    assert resume["d"] == {"token": _TOKEN, "session_id": "s1", "seq": 9}


@pytest.mark.asyncio
async def test_message_create_reaches_on_event_and_advances_seq() -> None:
    events: list[dict[str, object]] = []

    async def on_event(data: dict[str, object]) -> None:
        events.append(data)

    gateway = _gateway(on_event=on_event)
    await gateway._apply(
        _FakeConn(), Dispatch(event_type="MESSAGE_CREATE", data={"id": "m9"}, seq=2)
    )
    assert events == [{"id": "m9"}]
    assert gateway._session.last_seq == 2


@pytest.mark.asyncio
async def test_ready_updates_session_without_dispatching() -> None:
    events: list[dict[str, object]] = []

    async def on_event(data: dict[str, object]) -> None:
        events.append(data)

    gateway = _gateway(on_event=on_event)
    await gateway._apply(
        _FakeConn(),
        Dispatch(
            event_type="READY",
            data={"session_id": "s1", "resume_gateway_url": "wss://r"},
            seq=1,
        ),
    )
    assert events == []  # READY is internal, not a turn
    assert gateway._session.session_id == "s1"


@pytest.mark.asyncio
async def test_non_message_dispatch_is_not_forwarded() -> None:
    events: list[dict[str, object]] = []

    async def on_event(data: dict[str, object]) -> None:
        events.append(data)

    gateway = _gateway(on_event=on_event)
    await gateway._apply(_FakeConn(), Dispatch(event_type="TYPING_START", data={}, seq=4))
    assert events == []  # only MESSAGE_CREATE drives the flow


@pytest.mark.asyncio
async def test_reconnect_and_invalid_session_controls() -> None:
    gateway = _gateway()
    conn = _FakeConn()
    assert await gateway._apply(conn, Reconnect()) is _Control.RESUME_RECONNECT
    assert await gateway._apply(conn, InvalidSession(resumable=True)) is _Control.RESUME_RECONNECT
    assert await gateway._apply(conn, InvalidSession(resumable=False)) is _Control.FRESH_RECONNECT


# --- the ACK watchdog (_heartbeat_due) ---


@pytest.mark.asyncio
async def test_heartbeat_sends_when_acked_and_arms_the_watchdog() -> None:
    gateway = _gateway()
    conn = _FakeConn()
    gateway._acked = True
    dead = await gateway._heartbeat_due(conn)
    assert dead is False
    assert json.loads(conn.sent[0]) == {"op": 1, "d": None}
    assert gateway._acked is False  # armed: must be ACKed before the next is due


@pytest.mark.asyncio
async def test_heartbeat_missing_ack_reports_the_connection_dead() -> None:
    gateway = _gateway()
    conn = _FakeConn()
    gateway._acked = False  # the previous heartbeat was never ACKed
    dead = await gateway._heartbeat_due(conn)
    assert dead is True  # → the caller reconnects + resumes
    assert conn.sent == []


@pytest.mark.asyncio
async def test_heartbeat_ack_clears_the_watchdog() -> None:
    gateway = _gateway()
    gateway._acked = False
    await gateway._apply(_FakeConn(), HeartbeatAck())
    assert gateway._acked is True


# --- run(): connect → identify → dispatch (driven over a fake connection) ---


@pytest.mark.asyncio
async def test_run_identifies_then_dispatches_a_dm_event() -> None:
    events: list[dict[str, object]] = []

    async def on_event(data: dict[str, object]) -> None:
        events.append(data)

    conn = _FakeConn(
        [
            json.dumps({"op": 10, "d": {"heartbeat_interval": 600000}}),
            _dispatch("READY", {"session_id": "s1", "resume_gateway_url": "wss://resume.test"}, 1),
            _dispatch("MESSAGE_CREATE", {"id": "m9", "content": "hi"}, 2),
        ]
    )
    connects: list[str] = []

    async def connect(url: str) -> _FakeConn:
        connects.append(url)
        return conn

    gateway = _gateway(on_event=on_event, connect=connect)
    await gateway.run(should_continue=lambda: not conn.closed)

    assert connects == ["wss://gw.test"]  # the base gateway (no session yet)
    assert json.loads(conn.sent[0])["op"] == 2  # IDENTIFY was the first frame out
    assert events == [{"id": "m9", "content": "hi"}]  # the DM event reached the flow
    assert gateway._session.session_id == "s1"  # READY captured the session


@pytest.mark.asyncio
async def test_run_resumes_on_reconnect_using_the_resume_url() -> None:
    """op 7 Reconnect → reconnect to resume_gateway_url + send RESUME (keep the session)."""
    conn1 = _FakeConn(
        [
            json.dumps({"op": 10, "d": {"heartbeat_interval": 600000}}),
            _dispatch("READY", {"session_id": "s1", "resume_gateway_url": "wss://resume.test"}, 1),
            json.dumps({"op": 7}),  # Reconnect → resume
        ]
    )
    conn2 = _FakeConn([json.dumps({"op": 10, "d": {"heartbeat_interval": 600000}})])
    conns = [conn1, conn2]
    connects: list[str] = []

    async def connect(url: str) -> _FakeConn:
        connects.append(url)
        return conns.pop(0)

    gateway = _gateway(connect=connect)
    await gateway.run(should_continue=lambda: not conn2.closed)

    assert connects == ["wss://gw.test", "wss://resume.test"]  # reconnected to the resume URL
    resume = json.loads(conn2.sent[0])
    assert resume["op"] == 6  # RESUME, not a fresh IDENTIFY
    assert resume["d"]["session_id"] == "s1"


# --- R9-071: a closed-connection SEND must reconnect, never crash the service ---


@pytest.mark.asyncio
async def test_apply_heartbeat_request_send_on_closed_connection_reconnects() -> None:
    """The exact production site: ``_apply``'s HeartbeatRequest branch sends a heartbeat
    reply; if the peer already closed (Discord's routine 1000), that must reconnect, not
    raise ``ConnectionClosedOK`` out of ``_apply`` (the traceback captured 2026-07-29).
    """
    gateway = _gateway()
    conn = _FakeConnClosesOnSecondSend([], ConnectionClosedOK)
    await conn.send("priming the first send so the NEXT one is the failing one")
    control = await gateway._apply(conn, HeartbeatRequest())
    assert control is _Control.RESUME_RECONNECT  # not an exception


@pytest.mark.asyncio
async def test_heartbeat_due_send_on_closed_connection_reports_dead_not_raises() -> None:
    gateway = _gateway()
    conn = _FakeConnClosesOnSecondSend([], ConnectionClosedError)
    await conn.send("priming")
    dead = await gateway._heartbeat_due(conn)
    assert dead is True  # treated exactly like a missed ACK — reconnect + resume


@pytest.mark.parametrize("closed_exc", [ConnectionClosedOK, ConnectionClosedError])
@pytest.mark.asyncio
async def test_run_reconnects_when_a_heartbeat_send_hits_a_closed_connection(
    closed_exc: type[Exception],
) -> None:
    """End to end over ``run()``: HELLO → IDENTIFY (send #1, succeeds) → the server
    requests a heartbeat (op 1) → our reply send (send #2) hits the closed connection →
    ``run`` must NOT propagate the exception; it reconnects (a second ``connect`` attempt
    happens) and keeps looping, for both ``ConnectionClosedOK`` and ``ConnectionClosedError``.
    """
    conn1 = _FakeConnClosesOnSecondSend(
        [
            json.dumps({"op": 10, "d": {"heartbeat_interval": 600000}}),
            json.dumps({"op": 1}),  # server-requested heartbeat NOW — its reply send dies
        ],
        closed_exc,
    )
    conn2 = _FakeConn()  # recv() immediately raises -> ends the 2nd connection cleanly
    conns: list[object] = [conn1, conn2]
    connects: list[str] = []

    async def connect(url: str) -> object:
        connects.append(url)
        return conns.pop(0)

    gateway = _gateway(connect=connect)
    await gateway.run(should_continue=lambda: not conn2.closed)

    # A second connect attempt happened — the loop reconnected instead of crashing.
    assert connects == ["wss://gw.test", "wss://gw.test"]
    assert conn1.closed is True
    assert conn2.closed is True


@pytest.mark.asyncio
async def test_run_reconnects_on_a_closed_send_during_identify() -> None:
    """A send-time close during the very first IDENTIFY/RESUME (inside ``_run_connection``'s
    Hello handling, not ``_apply``) must also reconnect rather than propagate.
    """
    conn1 = _FakeConnClosesOnSecondSend(
        [json.dumps({"op": 10, "d": {"heartbeat_interval": 600000}})], ConnectionClosedOK
    )
    # Force send #1 (the IDENTIFY itself) to be the failing one.
    conn1._send_count = 1  # noqa: SLF001 — test setup: pre-arm so the very next send fails
    conn2 = _FakeConn()
    conns: list[object] = [conn1, conn2]
    connects: list[str] = []

    async def connect(url: str) -> object:
        connects.append(url)
        return conns.pop(0)

    gateway = _gateway(connect=connect)
    await gateway.run(should_continue=lambda: not conn2.closed)

    assert connects == ["wss://gw.test", "wss://gw.test"]
    assert conn1.sent == []  # the IDENTIFY never actually went out


@pytest.mark.asyncio
async def test_cancelled_error_from_the_gateway_still_propagates() -> None:
    """Normal shutdown (``asyncio.CancelledError``) must never be swallowed by any of the
    new defensive layers — it must propagate all the way out of ``run()``.
    """

    async def connect(_url: str) -> _CancellingConn:
        return _CancellingConn()

    gateway = _gateway(connect=connect)
    with pytest.raises(asyncio.CancelledError):
        await gateway.run()
