"""VoiceRoom: the LiveKit Room facade persona-voice's agent worker uses.

The agent worker joins a LiveKit Room as a participant via the access token
T04 mints (room name + identity + persona/conversation metadata). LiveKit
Server handles Opus encode/decode, jitter buffer, STUN/TURN traversal, ICE
negotiation, and DTLS-SRTP encryption — none of that is persona-voice's
concern (D-V1-1 branch (A)).

What persona-voice DOES own:

1. **Subscribing to the user's inbound audio track.** LiveKit fires
   ``track_subscribed`` when an audio publication arrives; this facade wraps
   that into a :class:`AudioStream` and drains frames into a registered
   :data:`InboundAudioHandler` callback (T07 V2 seam consumer).
2. **Publishing the persona's outbound audio track.** A custom
   :class:`AudioSource` is created at construction time and a local audio
   track wrapping it is published when :meth:`VoiceRoom.publish_outbound`
   runs. T07's V3 seam consumer feeds the source via
   :meth:`VoiceRoom.capture_outbound_frame`.
3. **Connection lifecycle events.** ``connected`` / ``disconnected`` /
   ``participant_disconnected`` callbacks bubble up to T06's session state
   machine.

The :class:`RoomSubstrate` Protocol abstracts the subset of
``livekit.rtc.Room`` we depend on so unit tests can inject a fake substrate
without spinning up a LiveKit Server. Production wires the real
``rtc.Room`` via :func:`build_voice_room`.

Audio invariants (D-V1-6): PCM16 mono **16 kHz** on the inbound rail (the
form V2 STT expects), PCM16 mono **24 kHz** on the outbound rail (the form V3
TTS produces). Sample-rate conversion happens at this boundary; the loop
skeleton (T07) never touches a non-canonical rate.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from livekit import rtc
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = [
    "AUDIO_INBOUND_CHANNELS",
    "AUDIO_INBOUND_SAMPLE_RATE",
    "AUDIO_OUTBOUND_CHANNELS",
    "AUDIO_OUTBOUND_SAMPLE_RATE",
    "InboundAudioFrame",
    "InboundAudioHandler",
    "RoomSubstrate",
    "VoiceRoom",
    "build_voice_room",
]


# D-V1-6 audio invariants. STT consumers expect 16 kHz mono PCM16; TTS
# producers typically emit 24 kHz mono PCM16; Opus on the wire is LiveKit's
# concern. Sample-rate conversion happens at this transport boundary.
AUDIO_INBOUND_SAMPLE_RATE: int = 16_000
AUDIO_INBOUND_CHANNELS: int = 1
AUDIO_OUTBOUND_SAMPLE_RATE: int = 24_000
AUDIO_OUTBOUND_CHANNELS: int = 1


class InboundAudioFrame(BaseModel):
    """One inbound audio frame handed to the T07 V2 STT seam.

    Frozen + ``extra="forbid"`` per the boundary-type discipline (D-05-9).
    Carries PCM16 bytes + ``sample_rate`` + ``num_channels`` explicitly so the
    sample-rate mismatch bug (R-V1-5 risk #7 — the AssemblyAI "chipmunky
    audio" production bug) is structurally impossible: every consumer reads
    the rate from the frame, never assumes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    data: bytes
    sample_rate: int = Field(gt=0)
    num_channels: int = Field(gt=0)
    samples_per_channel: int = Field(gt=0)


# The handler signature T07's V2 seam consumer registers via
# :meth:`VoiceRoom.set_inbound_handler`. Async so the consumer can push
# frames into a streaming STT without blocking the LiveKit event loop.
InboundAudioHandler = "Callable[[InboundAudioFrame], Awaitable[None]]"


@runtime_checkable
class RoomSubstrate(Protocol):
    """The minimum LiveKit Room surface :class:`VoiceRoom` depends on.

    Real production passes :class:`livekit.rtc.Room`. Tests inject a fake
    that records ``on``/``connect``/``disconnect``/``publish_track`` calls.
    Declared :func:`typing.runtime_checkable` so tests can ``isinstance``-check
    when wiring fixtures.
    """

    def on(self, event: str, callback: object) -> object: ...

    async def connect(self, url: str, token: str, options: object) -> None: ...

    async def disconnect(self) -> None: ...

    def isconnected(self) -> bool: ...

    @property
    def local_participant(self) -> object: ...


class VoiceRoom:
    """LiveKit Room facade — connect, subscribe to inbound audio, publish outbound.

    The construction order matters: the inbound + disconnect handlers are
    registered on the substrate during ``__init__`` so events that fire
    before :meth:`connect` returns still reach the consumer (LiveKit can
    deliver participant events during the connect handshake).
    """

    def __init__(
        self,
        room: RoomSubstrate,
        *,
        outbound_audio_track_name: str = "voice_out",
    ) -> None:
        self._room = room
        self._outbound_audio_track_name = outbound_audio_track_name
        self._inbound_handler: Callable[[InboundAudioFrame], Awaitable[None]] | None = None
        self._disconnect_handler: Callable[[], Awaitable[None]] | None = None
        self._participant_left_handler: Callable[[], None] | None = None
        self._outbound_source: rtc.AudioSource | None = None
        self._outbound_track: rtc.LocalAudioTrack | None = None
        self._drain_tasks: list[asyncio.Task[None]] = []
        # The substrate's `on` returns a callback; we register both events at
        # construction so any inbound activity during connect surfaces cleanly.
        self._room.on("track_subscribed", self._handle_track_subscribed)
        self._room.on("disconnected", self._handle_disconnected)
        self._room.on("participant_disconnected", self._handle_participant_disconnected)

    # ----- lifecycle ---------------------------------------------------

    async def connect(self, url: str, token: str) -> None:
        """Join the LiveKit Room. Auto-subscribe is left on (LiveKit default)
        so the user's audio track triggers ``track_subscribed`` automatically.
        """
        # Use the SDK's default RoomOptions (auto_subscribe=True); the wrapping
        # RoomOptions(...) construction lives in the real-substrate path where
        # production tuning happens (ICE servers from D-V1-2, e2ee posture).
        await self._room.connect(url, token, rtc.RoomOptions(auto_subscribe=True))

    async def disconnect(self) -> None:
        """Leave the Room and tear down audio resources."""
        for task in self._drain_tasks:
            task.cancel()
        self._drain_tasks.clear()
        if self._outbound_source is not None:
            await self._outbound_source.aclose()
            self._outbound_source = None
            self._outbound_track = None
        await self._room.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._room.isconnected()

    # ----- inbound -----------------------------------------------------

    def set_inbound_handler(
        self,
        handler: Callable[[InboundAudioFrame], Awaitable[None]],
    ) -> None:
        """Register the V2 STT seam consumer (T07 wires this)."""
        self._inbound_handler = handler

    def _handle_track_subscribed(
        self,
        track: object,
        _publication: object,
        _participant: object,
    ) -> None:
        """LiveKit ``track_subscribed`` callback — wrap audio tracks in a drain task.

        Non-audio tracks (e.g. video, which V1 does not publish but a remote
        client could) are ignored — V1 is voice-only by spec §2 out-of-scope.
        """
        if not isinstance(track, rtc.RemoteAudioTrack):
            return
        if self._inbound_handler is None:
            return
        # AudioStream resamples to the requested 16 kHz so V2 STT sees
        # canonical PCM16/16k regardless of the publisher's source rate.
        stream = rtc.AudioStream(
            track,
            sample_rate=AUDIO_INBOUND_SAMPLE_RATE,
            num_channels=AUDIO_INBOUND_CHANNELS,
        )
        task = asyncio.create_task(
            self._drain_inbound_audio(stream, self._inbound_handler),
            name=f"voice-room-inbound-drain-{id(track)}",
        )
        self._drain_tasks.append(task)

    async def _drain_inbound_audio(
        self,
        stream: rtc.AudioStream,
        handler: Callable[[InboundAudioFrame], Awaitable[None]],
    ) -> None:
        """Pull frames from the LiveKit AudioStream and forward to the handler.

        Cancellation is the normal shutdown path (``disconnect()`` cancels
        every drain task); :class:`asyncio.CancelledError` is allowed to
        propagate so the task ends cleanly.
        """
        async for event in stream:
            frame = event.frame
            await handler(
                InboundAudioFrame(
                    data=bytes(frame.data),
                    sample_rate=frame.sample_rate,
                    num_channels=frame.num_channels,
                    samples_per_channel=frame.samples_per_channel,
                )
            )

    def set_disconnect_handler(self, handler: Callable[[], Awaitable[None]]) -> None:
        """Register the T06 session-state-machine consumer (called on Room
        disconnect — clean or abrupt; T06 transitions the session to
        ``ended`` + flushes VoiceLog + releases the advisory lock via the
        ``rls_engine.begin()`` rollback).
        """
        self._disconnect_handler = handler

    def set_participant_left_handler(self, handler: Callable[[], None]) -> None:
        """Register the consumer notified when a remote participant leaves the Room.

        The agent's only remote peer on a call is the caller, so this event is
        the moment the human hung up. R9-202 bills the conversation rather than
        the session object's lifetime, and this is the signal that says when the
        conversation ended; the durable call-record also reads it to record an
        honest ``user_hangup`` rather than a bare ``disconnect``.

        The handler is SYNCHRONOUS on purpose: it only stamps a timestamp, and
        the LiveKit event dispatcher is synchronous, so nothing is scheduled and
        nothing can be lost to a cancelled task at teardown.
        """
        self._participant_left_handler = handler

    def _handle_participant_disconnected(self, *_args: object) -> None:
        """LiveKit ``participant_disconnected`` callback, forwarded to the consumer."""
        if self._participant_left_handler is None:
            return
        self._participant_left_handler()

    def _handle_disconnected(self, *_args: object) -> None:
        """LiveKit ``disconnected`` callback — schedule the consumer to run.

        The substrate's event dispatcher is synchronous; the consumer is
        async, so we spawn a task. T06's consumer is fast (state transition
        + lock release) so this never starves the LiveKit event loop.
        """
        if self._disconnect_handler is None:
            return
        # ``ensure_future`` accepts an Awaitable (a Coroutine satisfies that);
        # ``create_task`` is narrower in mypy --strict and rejects
        # ``Awaitable[None]`` return-typed callables.
        task: asyncio.Task[None] = asyncio.ensure_future(self._disconnect_handler())
        task.set_name("voice-room-disconnect-handler")
        self._drain_tasks.append(task)

    # ----- outbound ----------------------------------------------------

    async def publish_outbound(self) -> rtc.AudioSource:
        """Create + publish the local audio track that carries persona TTS.

        Returns the underlying :class:`livekit.rtc.AudioSource` so T07's V3
        seam consumer can call ``audio_source.capture_frame(rtc.AudioFrame(...))``
        each time TTS produces a chunk. Idempotent — calling twice returns
        the existing source.
        """
        if self._outbound_source is not None:
            return self._outbound_source
        source = rtc.AudioSource(
            sample_rate=AUDIO_OUTBOUND_SAMPLE_RATE,
            num_channels=AUDIO_OUTBOUND_CHANNELS,
        )
        track = rtc.LocalAudioTrack.create_audio_track(
            self._outbound_audio_track_name,
            source,
        )
        # local_participant.publish_track is async in livekit-rtc 1.x.
        await self._room.local_participant.publish_track(track)  # type: ignore[attr-defined]
        self._outbound_source = source
        self._outbound_track = track
        return source

    async def capture_outbound_frame(self, frame: rtc.AudioFrame) -> None:
        """Push one outbound TTS frame to the LiveKit audio source.

        Frame's sample_rate must match :data:`AUDIO_OUTBOUND_SAMPLE_RATE` —
        the V3 seam owns the resampling at the TTS adapter boundary.
        """
        if self._outbound_source is None:
            msg = "publish_outbound() must run before capture_outbound_frame()"
            raise RuntimeError(msg)
        await self._outbound_source.capture_frame(frame)

    async def publish_data(
        self, payload: bytes, *, reliable: bool = True, topic: str | None = None
    ) -> None:
        """Publish a data-channel frame to the Room (spec V6 A1, D-V6-E1).

        The transport primitive the V6 :class:`DataChannelBroadcaster` uses to
        push conversational-state transitions + transcript captions to the
        browser over the same peer connection the audio rides. ``reliable=True``
        (ordered + retransmit) is the default — a dropped ``thinking→speaking``
        transition would desync the client's state visualisation.

        **Room-scoped, owner-only delivery (D-V6-X-additive-no-migration).** No
        ``destination_identities`` is passed, so LiveKit delivers the frame to
        the Room's participants ONLY — and the Room (``persona:{session_id}``) is
        per-session, joinable solely by the call's own room-scoped token (the V1
        issuer grants ``room=<this room>`` only). Per-session room + room-scoped
        token + no cross-room destination compose to: the broadcast reaches the
        call's own authenticated owner and nobody else. There is no API path to
        target another tenant's participant from here.
        """
        kwargs: dict[str, object] = {"reliable": reliable}
        if topic is not None:
            kwargs["topic"] = topic
        # local_participant.publish_data is async in livekit-rtc 1.x; the
        # substrate Protocol types local_participant as ``object`` (it abstracts
        # only the subset VoiceRoom depends on), so the call is attr-ignored —
        # the real rtc.Room satisfies it structurally (mirrors publish_track).
        await self._room.local_participant.publish_data(payload, **kwargs)  # type: ignore[attr-defined]

    def clear_outbound(self) -> None:
        """Drop any queued-but-unplayed outbound audio (Spec V3 D-V3-5 step 4).

        The barge-in transport-queue clear: ``rtc.AudioSource`` buffers up to
        ``queue_size_ms`` of submitted audio, so cancelling synthesis alone
        leaves hundreds of ms of already-queued "ghost" audio playing (R-V3-5).
        Clearing the source queue makes the persona go quiet near-immediately.
        Additive — no reshape of the outbound seam; a no-op before
        :meth:`publish_outbound`.
        """
        if self._outbound_source is not None:
            self._outbound_source.clear_queue()


def build_voice_room(*, outbound_audio_track_name: str = "voice_out") -> VoiceRoom:
    """Production constructor — wires a fresh :class:`livekit.rtc.Room`.

    Test code uses ``VoiceRoom(fake_room, ...)`` directly so the substrate is
    injectable.
    """
    # ``rtc.Room.on`` is typed with a Literal of permitted event names that
    # is narrower than :class:`RoomSubstrate`'s ``str`` parameter — strictly
    # *more* restrictive than the Protocol, but mypy treats parameter
    # contravariance literally and rejects the assignment. The real Room
    # satisfies the Protocol structurally at runtime; cast() makes it
    # acceptable at type-check time.
    from typing import cast

    return VoiceRoom(
        cast("RoomSubstrate", rtc.Room()),
        outbound_audio_track_name=outbound_audio_track_name,
    )
