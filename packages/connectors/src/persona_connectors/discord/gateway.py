"""Discord gateway transport (Spec C3 ⛔, D-C3-1 / D-C3-3) — the persistent inbound WS.

Discord delivers DM events only over a persistent WebSocket gateway (there is no
message webhook), so the connector maintains one gateway connection inside the C1-D-1
single process (the ``run_long_poll`` precedent — an injected ``should_continue`` makes
shutdown/tests deterministic), over ``websockets`` (no ``discord.py`` — D-C3-X-no-new-dep).

**The trust boundary (event-authenticity, D-C3-3).** The gateway connection is
authenticated by the **bot token in the IDENTIFY (op 2) over TLS** — every event that
arrives on that authenticated connection is a genuine Discord event; there is **no
per-message signature** (the connection *is* the trust boundary, symmetric with Telegram
long-poll). The bot token is a ``SecretStr``, unwrapped only into the IDENTIFY/RESUME
payloads, never logged.

**The lifecycle (D-C3-1).** HELLO (op 10) → IDENTIFY (op 2, ``intents=DIRECT_MESSAGES``)
or RESUME (op 6) when a session exists; a heartbeat (op 1) at the interval with an **ACK
(op 11) watchdog** (a heartbeat falling due with the previous one un-ACKed ⇒ the
connection is dead ⇒ reconnect + resume, so no events are missed or double-processed);
READY captures ``session_id`` + ``resume_gateway_url``; a **MESSAGE_CREATE** dispatch's
payload is handed to the injected ``on_event`` (which DM-filters via ``classify_message``
— only DMs drive a turn); op 7 Reconnect / op 9 Invalid Session(resumable) → RESUME, op 9
(non-resumable) → a fresh IDENTIFY.

The **pure protocol layer** (the payload builders, :func:`interpret_frame`, the
:class:`GatewaySession`) and the lifecycle *decisions* (:meth:`DiscordGateway._apply` /
:meth:`DiscordGateway._heartbeat_due`) are exhaustively unit-tested; the concurrent
recv + heartbeat I/O loop (:meth:`DiscordGateway.run`) is the deploy seam, exercised by
the operator pass (the ``RuntimeFactory``-is-a-live-seam posture). api-free.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.logging import get_logger
from pydantic import BaseModel, ConfigDict
from websockets.exceptions import ConnectionClosed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic import SecretStr

_log = get_logger("connectors.discord_gateway")

__all__ = [
    "Dispatch",
    "DiscordGateway",
    "GatewayConnection",
    "GatewayDirective",
    "GatewaySession",
    "Hello",
    "HeartbeatAck",
    "HeartbeatRequest",
    "Ignore",
    "InvalidSession",
    "Reconnect",
    "build_heartbeat",
    "build_identify",
    "build_resume",
    "interpret_frame",
]

# Gateway opcodes (Discord API v10).
_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RESUME = 6
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10
_OP_HEARTBEAT_ACK = 11

# DIRECT_MESSAGES intent (1 << 12). DM message content is exempt from the privileged
# MESSAGE_CONTENT intent (C3-R-1), so this single non-privileged intent suffices.
INTENTS_DIRECT_MESSAGES = 1 << 12

# A fixed mid-interval first-heartbeat offset. The spec's random jitter exists to avoid a
# thundering herd across MANY bots reconnecting at once; with a single bot (D-C3-X-v1-reach)
# that is moot, so a deterministic half-interval offset is operationally fine + testable.
_FIRST_HEARTBEAT_FRACTION = 0.5

# A short pause before a fresh ``connect``/reconnect attempt after an unexpected failure
# (R9-071) — stops a persistently-failing gateway from hot-spinning the reconnect loop.
# Not applied on the ordinary control-flow reconnects (a dropped recv, op 7, a resumable
# op 9) — those are already paced by real network I/O; only the defensive fallback paths
# in :meth:`DiscordGateway.run` use it.
_RECONNECT_BACKOFF_SECONDS = 1.0


# --- payload builders (pure; the token is unwrapped by the caller at the call site) ---


def build_identify(token: str, *, intents: int) -> dict[str, object]:
    """Build the IDENTIFY (op 2) — authenticates the connection (the trust anchor)."""
    return {
        "op": _OP_IDENTIFY,
        "d": {
            "token": token,
            "intents": intents,
            "properties": {"os": "linux", "browser": "open-persona", "device": "open-persona"},
        },
    }


def build_resume(token: str, *, session_id: str, seq: int) -> dict[str, object]:
    """Build the RESUME (op 6) — replays missed events after a drop (no double-process)."""
    return {"op": _OP_RESUME, "d": {"token": token, "session_id": session_id, "seq": seq}}


def build_heartbeat(seq: int | None) -> dict[str, object]:
    """Build the heartbeat (op 1) carrying the last sequence number (or ``null``)."""
    return {"op": _OP_HEARTBEAT, "d": seq}


# --- frame interpretation (pure) ---


class Hello(BaseModel):
    """HELLO (op 10) — start heartbeating at ``heartbeat_interval_ms`` + IDENTIFY/RESUME."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    heartbeat_interval_ms: int


class Dispatch(BaseModel):
    """A DISPATCH (op 0) event — ``event_type`` (``t``), ``data`` (``d``), ``seq`` (``s``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: str
    data: dict[str, object]
    seq: int | None


class HeartbeatAck(BaseModel):
    """Heartbeat ACK (op 11) — the connection is alive (clears the watchdog)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class HeartbeatRequest(BaseModel):
    """The server asked us to heartbeat NOW (op 1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Reconnect(BaseModel):
    """Reconnect (op 7) — close and resume."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class InvalidSession(BaseModel):
    """Invalid Session (op 9) — resume if ``resumable`` else re-identify fresh."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resumable: bool


class Ignore(BaseModel):
    """A frame with nothing to act on (an unknown op / malformed HELLO)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str


GatewayDirective = (
    Hello | Dispatch | HeartbeatAck | HeartbeatRequest | Reconnect | InvalidSession | Ignore
)


def interpret_frame(frame: dict[str, object]) -> GatewayDirective:
    """Map a decoded gateway frame to a :data:`GatewayDirective` (pure, total)."""
    op = frame.get("op")
    if op == _OP_HELLO:
        data = frame.get("d")
        interval = data.get("heartbeat_interval") if isinstance(data, dict) else None
        if isinstance(interval, int) and not isinstance(interval, bool):
            return Hello(heartbeat_interval_ms=interval)
        return Ignore(reason="hello-without-interval")
    if op == _OP_HEARTBEAT_ACK:
        return HeartbeatAck()
    if op == _OP_HEARTBEAT:
        return HeartbeatRequest()
    if op == _OP_RECONNECT:
        return Reconnect()
    if op == _OP_INVALID_SESSION:
        return InvalidSession(resumable=frame.get("d") is True)
    if op == _OP_DISPATCH:
        event_type = frame.get("t")
        seq = frame.get("s")
        data = frame.get("d")
        return Dispatch(
            event_type=event_type if isinstance(event_type, str) else "",
            data=data if isinstance(data, dict) else {},
            seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
        )
    return Ignore(reason="unknown-op")


# --- the session (resumability state) ---


class GatewaySession:
    """The resumability state — ``session_id`` + ``resume_gateway_url`` + ``last_seq``.

    Mutated as the connection lives: READY sets the session, each dispatch advances the
    sequence, a non-resumable Invalid Session resets it. Instance state (DI), no globals.
    """

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.resume_url: str | None = None
        self.last_seq: int | None = None

    def advance(self, seq: int | None) -> None:
        """Record the latest dispatch sequence (for heartbeats + resume)."""
        if seq is not None:
            self.last_seq = seq

    def on_ready(self, data: dict[str, object]) -> None:
        """Capture ``session_id`` + ``resume_gateway_url`` from the READY payload."""
        session_id = data.get("session_id")
        resume_url = data.get("resume_gateway_url")
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
        if isinstance(resume_url, str) and resume_url:
            self.resume_url = resume_url

    def can_resume(self) -> bool:
        """Whether a RESUME is possible (a session id + a sequence to resume from)."""
        return self.session_id is not None and self.last_seq is not None

    def reset(self) -> None:
        """Forget the session — a fresh IDENTIFY is required (non-resumable invalidation)."""
        self.session_id = None
        self.resume_url = None
        self.last_seq = None


class _Control(Enum):
    """The recv-loop control signal after applying a directive."""

    CONTINUE = auto()
    RESUME_RECONNECT = auto()
    FRESH_RECONNECT = auto()


@runtime_checkable
class GatewayConnection(Protocol):
    """The minimal WebSocket surface the gateway needs (a ``websockets`` connection)."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


class DiscordGateway:
    """Maintains the persistent Discord gateway connection (the inbound DM transport).

    Dependencies are injected (DI; no globals): the bot token (``SecretStr``), the
    ``on_event`` handler (the flow — DM-filters via ``classify_message``), a ``connect``
    factory (wraps ``websockets.connect``), and ``sleep`` (injected for deterministic
    tests). The lifecycle *decisions* live in :meth:`_apply` / :meth:`_heartbeat_due`
    (unit-tested); :meth:`run` is the live recv + heartbeat I/O loop (the deploy seam).
    """

    def __init__(
        self,
        *,
        token: SecretStr,
        on_event: Callable[[dict[str, object]], Awaitable[None]],
        connect: Callable[[str], Awaitable[GatewayConnection]],
        gateway_url: str,
        intents: int = INTENTS_DIRECT_MESSAGES,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._token = token
        self._on_event = on_event
        self._connect = connect
        self._gateway_url = gateway_url
        self._intents = intents
        self._sleep = sleep
        self._session = GatewaySession()
        self._acked = True

    async def _send(self, conn: GatewayConnection, payload: dict[str, object]) -> bool:
        """Send a gateway frame; return ``False`` (never raise) if the peer already closed.

        Discord's gateway routinely closes a connection with code 1000 (OK) and expects a
        reconnect — that is normal operation, not an error (R9-071). A send racing that
        close must be treated exactly like a dropped ``recv()``: the caller reconnects and
        resumes. Left unguarded, ``ConnectionClosedOK``/``ConnectionClosedError`` escapes
        ``_apply`` → ``_run_connection`` → ``run`` → the service's ``asyncio.gather``,
        killing every platform + the HTTP server, not just this gateway (production, 2026-07-29).
        """
        try:
            await conn.send(json.dumps(payload))
        except ConnectionClosed as exc:
            _log.info(
                "discord gateway: send on a closed connection ({error}); reconnecting",
                error=str(exc),
            )
            return False
        return True

    async def _identify_or_resume(self, conn: GatewayConnection) -> bool:
        """Authenticate the connection: RESUME an existing session, else a fresh IDENTIFY.

        Returns ``False`` (instead of raising) if the connection is already closed.
        """
        token = self._token.get_secret_value()
        if self._session.can_resume():
            assert self._session.session_id is not None
            assert self._session.last_seq is not None
            payload = build_resume(
                token, session_id=self._session.session_id, seq=self._session.last_seq
            )
        else:
            payload = build_identify(token, intents=self._intents)
        return await self._send(conn, payload)

    async def _apply(self, conn: GatewayConnection, directive: GatewayDirective) -> _Control:
        """Apply one directive; return the recv-loop control signal (the lifecycle logic)."""
        if isinstance(directive, Hello):
            self._acked = True
            sent = await self._identify_or_resume(conn)
            return _Control.CONTINUE if sent else _Control.RESUME_RECONNECT
        if isinstance(directive, HeartbeatAck):
            self._acked = True
            return _Control.CONTINUE
        if isinstance(directive, HeartbeatRequest):
            sent = await self._send(conn, build_heartbeat(self._session.last_seq))
            return _Control.CONTINUE if sent else _Control.RESUME_RECONNECT
        if isinstance(directive, Dispatch):
            self._session.advance(directive.seq)
            if directive.event_type == "READY":
                self._session.on_ready(directive.data)
            elif directive.event_type == "MESSAGE_CREATE":
                # The trusted DM event → the flow (which DM-filters via classify_message).
                await self._on_event(directive.data)
            return _Control.CONTINUE
        if isinstance(directive, Reconnect):
            return _Control.RESUME_RECONNECT
        if isinstance(directive, InvalidSession):
            return _Control.RESUME_RECONNECT if directive.resumable else _Control.FRESH_RECONNECT
        return _Control.CONTINUE  # Ignore

    async def _heartbeat_due(self, conn: GatewayConnection) -> bool:
        """A heartbeat is due. Send it (clearing the ACK flag), or report the connection dead.

        The ACK watchdog: if the previous heartbeat was never ACKed (``self._acked`` is
        still ``False`` when the next is due), the connection is dead — return ``True`` so
        the caller reconnects (and resumes). Otherwise send the heartbeat and arm the
        watchdog (``_acked = False`` until the server ACKs). A send that fails because the
        peer already closed the connection is ALSO reported dead (never raised — R9-071).
        """
        if not self._acked:
            return True
        self._acked = False
        sent = await self._send(conn, build_heartbeat(self._session.last_seq))
        return not sent

    async def run(self, *, should_continue: Callable[[], bool] = lambda: True) -> None:
        """Maintain the gateway: connect → identify/resume → recv + heartbeat → reconnect.

        The live I/O loop (the deploy seam): reconnects to the resume URL (keeping the
        session) on a drop / op 7 / resumable op 9, and to the base gateway with a fresh
        session on a non-resumable op 9. Exercised end-to-end by the operator pass.

        Defense in depth (R9-071): a failed ``connect`` or an unexpected exception out of
        ``_run_connection`` is logged and reconnected rather than propagated — the send-path
        close (a closed/broken connection) is already handled inside ``_apply``/
        ``_heartbeat_due``, but this outer guard means a bug in the gateway can *never*
        crash the whole connector service the way it did in production. A short backoff
        avoids hot-spinning a persistently-failing gateway. ``asyncio.CancelledError``
        (normal shutdown) is always re-raised, never swallowed.
        """
        while should_continue():
            resume_url = self._session.resume_url
            url = (
                resume_url
                if self._session.can_resume() and resume_url is not None
                else self._gateway_url
            )
            try:
                conn = await self._connect(url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a failed connect must not kill the service
                _log.warning("discord gateway connect failed ({error}); retrying", error=str(exc))
                await self._sleep(_RECONNECT_BACKOFF_SECONDS)
                continue
            control = _Control.RESUME_RECONNECT
            failed = False
            try:
                control = await self._run_connection(conn, should_continue)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — defense in depth, see docstring
                _log.warning(
                    "discord gateway connection error ({error}); reconnecting", error=str(exc)
                )
                failed = True
            finally:
                with contextlib.suppress(Exception):
                    await conn.close()
            if control is _Control.FRESH_RECONNECT:
                self._session.reset()
            if failed:
                await self._sleep(_RECONNECT_BACKOFF_SECONDS)

    async def _run_connection(
        self, conn: GatewayConnection, should_continue: Callable[[], bool]
    ) -> _Control:
        """One connection's recv loop + heartbeat task; returns how to reconnect."""
        heartbeat: asyncio.Task[None] | None = None
        try:
            while should_continue():
                try:
                    raw = await conn.recv()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — any drop ends the session → reconnect
                    _log.info(
                        "discord gateway connection closed ({error}); reconnecting",
                        error=str(exc),
                    )
                    return _Control.RESUME_RECONNECT
                try:
                    frame = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(frame, dict):
                    continue
                directive = interpret_frame(frame)
                if isinstance(directive, Hello) and heartbeat is None:
                    self._acked = True
                    sent = await self._identify_or_resume(conn)
                    if not sent:
                        return _Control.RESUME_RECONNECT
                    heartbeat = asyncio.create_task(
                        self._heartbeat_loop(conn, directive.heartbeat_interval_ms / 1000)
                    )
                    continue
                control = await self._apply(conn, directive)
                if control is not _Control.CONTINUE:
                    return control
            return _Control.FRESH_RECONNECT
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def _heartbeat_loop(self, conn: GatewayConnection, interval_seconds: float) -> None:
        """Heartbeat at the interval; on a missed ACK, close the conn to force reconnect."""
        await self._sleep(interval_seconds * _FIRST_HEARTBEAT_FRACTION)
        while True:
            dead = await self._heartbeat_due(conn)
            if dead:
                with contextlib.suppress(Exception):
                    await conn.close()  # force the recv loop to drop → reconnect + resume
                return
            await self._sleep(interval_seconds)
