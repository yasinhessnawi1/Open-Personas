"""Unit tests for :class:`VoiceRoom` (spec V1 T05).

Exercise the LiveKit-Room facade without a live LiveKit Server. A fake
substrate mimics the :class:`livekit.rtc.Room` surface :class:`VoiceRoom`
depends on; tests assert wiring, lifecycle, and the inbound/outbound audio
seams T07 will plug into.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from livekit import rtc
from persona_voice.transport.room import (
    AUDIO_INBOUND_CHANNELS,
    AUDIO_INBOUND_SAMPLE_RATE,
    AUDIO_OUTBOUND_CHANNELS,
    AUDIO_OUTBOUND_SAMPLE_RATE,
    InboundAudioFrame,
    RoomSubstrate,
    VoiceRoom,
)

# ---------- fake substrate ---------------------------------------------------


class _FakeLocalParticipant:
    def __init__(self) -> None:
        self.publish_track = AsyncMock(return_value=None)


class _FakeRoom:
    """Minimum surface :class:`VoiceRoom` depends on; records calls for assertions."""

    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}
        self.connect = AsyncMock(return_value=None)
        self.disconnect = AsyncMock(return_value=None)
        self._connected = False
        self.local_participant = _FakeLocalParticipant()

    def on(self, event: str, callback: Any) -> None:  # noqa: ANN401
        self.handlers.setdefault(event, []).append(callback)

    def isconnected(self) -> bool:
        return self._connected

    def fire(self, event: str, *args: Any) -> None:  # noqa: ANN401
        for cb in self.handlers.get(event, []):
            cb(*args)


# ---------- construction wiring ----------------------------------------------


def test_construction_registers_inbound_and_disconnect_handlers() -> None:
    room = _FakeRoom()
    _vr = VoiceRoom(room)
    assert "track_subscribed" in room.handlers
    assert "disconnected" in room.handlers
    # R9-202: the caller leaving is its own event, and the bill depends on it.
    assert "participant_disconnected" in room.handlers
    # Exactly one handler per event — we register inline at __init__.
    assert len(room.handlers["track_subscribed"]) == 1
    assert len(room.handlers["disconnected"]) == 1
    assert len(room.handlers["participant_disconnected"]) == 1


def test_participant_left_handler_is_called_on_the_room_event() -> None:
    """R9-202: a remote participant leaving reaches the registered consumer.

    The billable window hangs off this event, so the charge for every call
    depends on the substrate event reaching the handler.
    """
    room = _FakeRoom()
    vr = VoiceRoom(room)
    departures: list[str] = []
    vr.set_participant_left_handler(lambda: departures.append("left"))

    room.fire("participant_disconnected", object())

    assert departures == ["left"]


def test_participant_left_event_without_a_handler_is_a_noop() -> None:
    room = _FakeRoom()
    VoiceRoom(room)
    room.fire("participant_disconnected", object())  # must not raise


def test_fake_room_satisfies_room_substrate_protocol() -> None:
    """``RoomSubstrate`` is ``runtime_checkable`` so test fixtures can
    sanity-check they wired the fake correctly."""
    assert isinstance(_FakeRoom(), RoomSubstrate)


# ---------- lifecycle --------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_calls_substrate_with_url_and_token() -> None:
    room = _FakeRoom()
    vr = VoiceRoom(room)
    await vr.connect("ws://lk.test", "tok_xyz")
    assert room.connect.await_count == 1
    args, _kwargs = room.connect.call_args
    assert args[0] == "ws://lk.test"
    assert args[1] == "tok_xyz"
    # The RoomOptions is passed positionally; we don't assert its internals
    # because the SDK's own defaults (auto_subscribe=True) are what we want.


def test_clear_outbound_clears_the_source_queue() -> None:
    # Spec V3 T10 / D-V3-5 step 4 — barge-in flushes queued outbound audio.
    room = _FakeRoom()
    vr = VoiceRoom(room)
    fake_source = MagicMock()
    vr._outbound_source = fake_source  # noqa: SLF001 — test setup
    vr.clear_outbound()
    fake_source.clear_queue.assert_called_once_with()


def test_clear_outbound_is_a_noop_before_publish() -> None:
    # No source published yet → clear_outbound is a safe no-op (no raise).
    vr = VoiceRoom(_FakeRoom())
    vr.clear_outbound()


@pytest.mark.asyncio
async def test_disconnect_tears_down_outbound_source_and_calls_substrate() -> None:
    room = _FakeRoom()
    vr = VoiceRoom(room)
    # Force an outbound source onto the facade — disconnect must aclose() it.
    fake_source = MagicMock()
    fake_source.aclose = AsyncMock(return_value=None)
    vr._outbound_source = fake_source  # noqa: SLF001 — test setup
    await vr.disconnect()
    fake_source.aclose.assert_awaited_once()
    room.disconnect.assert_awaited_once()
    assert vr._outbound_source is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_disconnect_cancels_pending_drain_tasks() -> None:
    room = _FakeRoom()
    vr = VoiceRoom(room)

    async def _forever() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(_forever())
    vr._drain_tasks.append(task)  # noqa: SLF001
    await vr.disconnect()
    # `task.cancel()` schedules cancellation but the task body needs to
    # observe it on the next event-loop tick. Await with suppression so
    # CancelledError doesn't fail the test.
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


def test_is_connected_delegates_to_substrate() -> None:
    room = _FakeRoom()
    vr = VoiceRoom(room)
    assert vr.is_connected is False
    room._connected = True  # noqa: SLF001
    assert vr.is_connected is True


# ---------- inbound audio path ----------------------------------------------


def test_track_subscribed_with_non_audio_track_is_ignored() -> None:
    room = _FakeRoom()
    vr = VoiceRoom(room)
    captured: list[InboundAudioFrame] = []

    async def _capture(frame: InboundAudioFrame) -> None:
        captured.append(frame)

    vr.set_inbound_handler(_capture)
    # A plain object is not an rtc.RemoteAudioTrack — handler must NOT
    # subscribe a drain task.
    room.fire("track_subscribed", object(), object(), object())
    assert vr._drain_tasks == []  # noqa: SLF001
    assert captured == []


def test_track_subscribed_without_handler_is_a_noop() -> None:
    """If no V2 seam consumer registered (e.g. before T07 wires up), audio
    frames are dropped — no task spawned, no traceback raised."""
    room = _FakeRoom()
    vr = VoiceRoom(room)
    fake_track = MagicMock(spec=rtc.RemoteAudioTrack)
    room.fire("track_subscribed", fake_track, object(), object())
    assert vr._drain_tasks == []  # noqa: SLF001


@pytest.mark.asyncio
async def test_inbound_audio_drains_into_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock :class:`rtc.AudioStream` to yield two events; assert the handler
    receives two :class:`InboundAudioFrame` instances carrying PCM16 bytes
    + sample-rate + channel count + samples-per-channel (D-V1-6 invariant).
    """
    room = _FakeRoom()
    vr = VoiceRoom(room)
    captured: list[InboundAudioFrame] = []

    async def _capture(frame: InboundAudioFrame) -> None:
        captured.append(frame)

    vr.set_inbound_handler(_capture)

    # Fake AudioStream — async-iterable yielding events with a `.frame` attr.
    fake_event_1 = MagicMock()
    fake_event_1.frame = MagicMock(
        data=b"\x01\x02\x03\x04",
        sample_rate=16_000,
        num_channels=1,
        samples_per_channel=2,
    )
    fake_event_2 = MagicMock()
    fake_event_2.frame = MagicMock(
        data=b"\x05\x06",
        sample_rate=16_000,
        num_channels=1,
        samples_per_channel=1,
    )

    class _FakeAudioStream:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self._events = iter([fake_event_1, fake_event_2])

        def __aiter__(self) -> _FakeAudioStream:
            return self

        async def __anext__(self) -> object:
            try:
                return next(self._events)
            except StopIteration:
                raise StopAsyncIteration from None

    monkeypatch.setattr("persona_voice.transport.room.rtc.AudioStream", _FakeAudioStream)
    fake_track = MagicMock(spec=rtc.RemoteAudioTrack)
    room.fire("track_subscribed", fake_track, object(), object())

    # Allow the drain task to run to completion.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(captured) == 2
    assert captured[0].data == b"\x01\x02\x03\x04"
    assert captured[0].sample_rate == 16_000
    assert captured[0].num_channels == 1
    assert captured[0].samples_per_channel == 2
    assert captured[1].data == b"\x05\x06"


@pytest.mark.asyncio
async def test_disconnect_handler_is_called_on_disconnected_event() -> None:
    room = _FakeRoom()
    vr = VoiceRoom(room)
    called = asyncio.Event()

    async def _on_disconnect() -> None:
        called.set()

    vr.set_disconnect_handler(_on_disconnect)
    room.fire("disconnected")
    # Allow the spawned task to run.
    await asyncio.wait_for(called.wait(), timeout=1.0)
    assert called.is_set()


# ---------- outbound audio path ---------------------------------------------


@pytest.mark.asyncio
async def test_publish_outbound_creates_source_publishes_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``publish_outbound`` must create an AudioSource with the canonical
    24 kHz mono PCM16 rates, wrap it in a LocalAudioTrack, and publish via
    the substrate's ``local_participant.publish_track``."""
    room = _FakeRoom()
    vr = VoiceRoom(room)

    fake_source = MagicMock()
    fake_source.aclose = AsyncMock(return_value=None)
    fake_source.capture_frame = AsyncMock(return_value=None)
    audio_source_ctor = MagicMock(return_value=fake_source)

    fake_track = MagicMock()
    create_audio_track_mock = MagicMock(return_value=fake_track)

    monkeypatch.setattr("persona_voice.transport.room.rtc.AudioSource", audio_source_ctor)
    monkeypatch.setattr(
        "persona_voice.transport.room.rtc.LocalAudioTrack.create_audio_track",
        create_audio_track_mock,
    )

    out = await vr.publish_outbound()
    assert out is fake_source

    # AudioSource was constructed at the canonical outbound rate.
    audio_source_ctor.assert_called_once_with(
        sample_rate=AUDIO_OUTBOUND_SAMPLE_RATE,
        num_channels=AUDIO_OUTBOUND_CHANNELS,
    )
    # LocalAudioTrack.create_audio_track received the configured track name
    # and our source.
    create_audio_track_mock.assert_called_once_with("voice_out", fake_source)
    # Substrate's local participant published the track.
    room.local_participant.publish_track.assert_awaited_once_with(fake_track)


@pytest.mark.asyncio
async def test_publish_outbound_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling twice returns the same source without re-publishing — the
    second call is a noop so V3 seam wiring stays cheap on hot paths."""
    room = _FakeRoom()
    vr = VoiceRoom(room)
    fake_source = MagicMock()
    fake_source.aclose = AsyncMock(return_value=None)
    audio_source_ctor = MagicMock(return_value=fake_source)
    monkeypatch.setattr("persona_voice.transport.room.rtc.AudioSource", audio_source_ctor)
    monkeypatch.setattr(
        "persona_voice.transport.room.rtc.LocalAudioTrack.create_audio_track",
        MagicMock(return_value=MagicMock()),
    )
    out1 = await vr.publish_outbound()
    out2 = await vr.publish_outbound()
    assert out1 is out2
    assert audio_source_ctor.call_count == 1
    assert room.local_participant.publish_track.await_count == 1


@pytest.mark.asyncio
async def test_capture_outbound_frame_before_publish_raises() -> None:
    """Calling ``capture_outbound_frame`` before ``publish_outbound`` is a
    programming error — fail loud rather than silently swallow frames."""
    room = _FakeRoom()
    vr = VoiceRoom(room)
    frame = MagicMock()
    with pytest.raises(RuntimeError, match="publish_outbound"):
        await vr.capture_outbound_frame(frame)


@pytest.mark.asyncio
async def test_capture_outbound_frame_pushes_into_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    room = _FakeRoom()
    vr = VoiceRoom(room)
    fake_source = MagicMock()
    fake_source.aclose = AsyncMock(return_value=None)
    fake_source.capture_frame = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "persona_voice.transport.room.rtc.AudioSource",
        MagicMock(return_value=fake_source),
    )
    monkeypatch.setattr(
        "persona_voice.transport.room.rtc.LocalAudioTrack.create_audio_track",
        MagicMock(return_value=MagicMock()),
    )
    await vr.publish_outbound()
    frame = MagicMock()
    await vr.capture_outbound_frame(frame)
    fake_source.capture_frame.assert_awaited_once_with(frame)


# ---------- audio-rate invariants -------------------------------------------


def test_inbound_constants_match_d_v1_6() -> None:
    """The D-V1-6 inbound rail is PCM16 mono 16 kHz."""
    assert AUDIO_INBOUND_SAMPLE_RATE == 16_000
    assert AUDIO_INBOUND_CHANNELS == 1


def test_outbound_constants_match_d_v1_6() -> None:
    """The D-V1-6 outbound rail is PCM16 mono 24 kHz."""
    assert AUDIO_OUTBOUND_SAMPLE_RATE == 24_000
    assert AUDIO_OUTBOUND_CHANNELS == 1
