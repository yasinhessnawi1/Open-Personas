"""Unit tests for :class:`StreamingLoop` (spec V1 T07).

Cover the four named Protocol seams (V2 STT / V3 TTS / V5 ModelReplyProducer)
plus the pass-through echo default (no STT registered → inbound piped to
outbound) plus the V2 → V5 → V3 pipeline streaming chain plus the V4 barge-in
entry. The :class:`VoiceRoom` is faked at the boundary so no LiveKit Server is
required.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from persona_voice.loop.streaming import (
    AudioChunk,
    PassThroughEchoMode,
    StreamingLoop,
    STTStream,
    Transcript,
    TTSStream,
)
from persona_voice.session.state_machine import (
    SessionLifecycleEvent,
    SessionStateMachine,
)
from persona_voice.transport.room import InboundAudioFrame

# ---------- fixtures --------------------------------------------------------


def _build_voice_room_fake() -> Any:  # noqa: ANN401
    """Fake VoiceRoom — captures the inbound-handler registration + records
    outbound-frame pushes for assertions."""
    vr = MagicMock()
    vr.set_inbound_handler = MagicMock()
    vr.set_disconnect_handler = MagicMock()
    vr.publish_outbound = AsyncMock(return_value=MagicMock())
    vr.capture_outbound_frame = AsyncMock(return_value=None)
    vr.clear_outbound = MagicMock(return_value=None)  # Spec V3 T10 (D-V3-5 step 4)
    return vr


def _build_session() -> SessionStateMachine:
    engine = MagicMock()
    engine.dispose = MagicMock()
    return SessionStateMachine(
        session_id="s1",
        user_id="u1",
        persona_id="p1",
        conversation_id="c1",
        rls_engine=engine,
    )


def _inbound_frame(data: bytes = b"\x01\x02") -> InboundAudioFrame:
    return InboundAudioFrame(
        data=data,
        sample_rate=16_000,
        num_channels=1,
        samples_per_channel=len(data) // 2,
    )


# ---------- boundary records ------------------------------------------------


def test_transcript_is_frozen_and_confidence_is_bounded() -> None:
    t = Transcript(is_final=True, text="hello", confidence=0.95)
    assert t.is_final is True
    assert t.text == "hello"
    # confidence ∈ [0, 1]
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Transcript(is_final=True, text="x", confidence=1.5)
    with pytest.raises(ValidationError):
        Transcript(is_final=True, text="x", confidence=-0.1)


def test_audio_chunk_carries_explicit_sample_rate() -> None:
    """D-V1-6: every audio record carries ``sample_rate`` so the mismatch
    bug R-V1-5 names is structurally impossible."""
    chunk = AudioChunk(
        data=b"\x00\x01",
        sample_rate=24_000,
        num_channels=1,
        samples_per_channel=1,
    )
    assert chunk.sample_rate == 24_000
    assert chunk.num_channels == 1


# ---------- construction wiring --------------------------------------------


def test_construction_registers_inbound_handler_on_voice_room() -> None:
    vr = _build_voice_room_fake()
    sm = _build_session()
    _loop = StreamingLoop(voice_room=vr, session=sm)
    vr.set_inbound_handler.assert_called_once()


# ---------- pass-through echo path -----------------------------------------


@pytest.mark.asyncio
async def test_inbound_frame_with_no_stt_falls_back_to_echo() -> None:
    """No STT registered + echo mode ON: inbound frame is piped to outbound."""
    vr = _build_voice_room_fake()
    sm = _build_session()
    _loop = StreamingLoop(voice_room=vr, session=sm, echo_mode=PassThroughEchoMode.ECHO)
    handler = vr.set_inbound_handler.call_args[0][0]
    await handler(_inbound_frame(b"\x10\x20"))
    # publish_outbound idempotent (lazy first-call), then capture_outbound_frame.
    vr.publish_outbound.assert_awaited()
    vr.capture_outbound_frame.assert_awaited_once()


@pytest.mark.asyncio
async def test_inbound_frame_with_echo_disabled_is_dropped() -> None:
    vr = _build_voice_room_fake()
    sm = _build_session()
    _loop = StreamingLoop(voice_room=vr, session=sm, echo_mode=PassThroughEchoMode.DISABLED)
    handler = vr.set_inbound_handler.call_args[0][0]
    await handler(_inbound_frame())
    vr.capture_outbound_frame.assert_not_called()


@pytest.mark.asyncio
async def test_inbound_frame_with_stt_is_pushed_into_stt_not_echoed() -> None:
    """STT wired → frames flow to V2; the echo fallback is suppressed."""
    vr = _build_voice_room_fake()
    sm = _build_session()
    stt = MagicMock(spec=STTStream)
    stt.push_audio = AsyncMock(return_value=None)
    _loop = StreamingLoop(voice_room=vr, session=sm, stt=stt)
    handler = vr.set_inbound_handler.call_args[0][0]
    frame = _inbound_frame(b"\x11\x22")
    await handler(frame)
    stt.push_audio.assert_awaited_once_with(b"\x11\x22", 16_000)
    vr.capture_outbound_frame.assert_not_called()


# ---------- V2 → V5 → V3 pipeline ------------------------------------------


@pytest.mark.asyncio
async def test_start_pipeline_with_incomplete_seams_is_noop() -> None:
    """V2/V3/V5 not all wired → pipeline can't run; no task spawned."""
    vr = _build_voice_room_fake()
    sm = _build_session()
    loop = StreamingLoop(voice_room=vr, session=sm)
    await loop.start_pipeline()
    assert loop._pipeline_task is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_start_pipeline_is_idempotent() -> None:
    vr = _build_voice_room_fake()
    sm = _build_session()

    class _STT:
        async def push_audio(self, pcm: bytes, sample_rate: int) -> None: ...

        async def transcripts(self) -> Any:  # noqa: ANN401
            await asyncio.sleep(3600)
            return
            yield  # unreachable

    class _Model:
        async def __call__(self, _t: Transcript) -> Any:  # noqa: ANN401
            return
            yield

    class _TTS:
        async def synthesize(self, _stream: Any) -> Any:  # noqa: ANN401
            return
            yield

        async def cancel(self) -> None: ...

    loop = StreamingLoop(voice_room=vr, session=sm, stt=_STT(), model=_Model(), tts=_TTS())
    await loop.start_pipeline()
    first = loop._pipeline_task  # noqa: SLF001
    await loop.start_pipeline()
    assert loop._pipeline_task is first  # noqa: SLF001
    await loop.stop()


@pytest.mark.asyncio
async def test_pipeline_streams_transcript_to_tokens_to_audio() -> None:
    """End-to-end seam-chain: V2 emits a final transcript, V5 produces two
    tokens, V3 synthesizes two audio chunks; both chunks land on the
    outbound rail at 24 kHz."""
    vr = _build_voice_room_fake()
    sm = _build_session()

    final_t = Transcript(is_final=True, text="hello", confidence=0.95, eou_at=datetime.now(UTC))

    class _STT:
        async def push_audio(self, pcm: bytes, sample_rate: int) -> None: ...

        async def transcripts(self) -> Any:  # noqa: ANN401
            yield Transcript(is_final=False, text="hel", confidence=0.5)
            yield final_t
            # End the stream so the pipeline can exit cleanly.

    seen_transcripts: list[Transcript] = []

    async def _model(t: Transcript) -> Any:  # noqa: ANN401
        seen_transcripts.append(t)

        async def _gen() -> Any:  # noqa: ANN401
            yield "Hi "
            yield "there"

        return _gen()

    captured_text: list[str] = []

    class _TTS:
        async def synthesize(self, text_stream: Any) -> Any:  # noqa: ANN401
            async for tok in text_stream:
                captured_text.append(tok)
                yield AudioChunk(
                    data=b"\x00" * 2,
                    sample_rate=24_000,
                    num_channels=1,
                    samples_per_channel=1,
                )

        async def cancel(self) -> None: ...

    loop = StreamingLoop(voice_room=vr, session=sm, stt=_STT(), model=_model, tts=_TTS())
    await loop.start_pipeline()
    # Allow the pipeline task to drain the STT stream.
    if loop._pipeline_task is not None:  # noqa: SLF001
        await asyncio.wait_for(loop._pipeline_task, timeout=2.0)  # noqa: SLF001
    # V5 only ever sees final transcripts.
    assert seen_transcripts == [final_t]
    assert captured_text == ["Hi ", "there"]
    # Two outbound frames pushed (one per TTS chunk).
    assert vr.capture_outbound_frame.await_count == 2


@pytest.mark.asyncio
async def test_pipeline_rejects_chunk_with_wrong_sample_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V3 produces an outbound chunk at the wrong rate: the loop never resamples it
    and never plays it. The chunk is refused, the crash is logged as a bug with its
    class (R9-124: loud in the log, not a dead task), and the loop stays alive."""
    from persona_voice.loop import streaming as streaming_mod

    crashes: list[str] = []

    class _Recorder:
        def exception(self, template: str, **kw: object) -> None:
            crashes.append(template + " " + " ".join(f"{k}={v}" for k, v in kw.items()))

        def __getattr__(self, _name: str) -> object:
            return lambda *_a, **_kw: None

    monkeypatch.setattr(streaming_mod, "_LOG", _Recorder())
    vr = _build_voice_room_fake()
    sm = _build_session()

    class _STT:
        async def push_audio(self, pcm: bytes, sample_rate: int) -> None: ...

        async def transcripts(self) -> Any:  # noqa: ANN401
            yield Transcript(is_final=True, text="x", confidence=0.9)

    async def _model(_t: Transcript) -> Any:  # noqa: ANN401
        async def _gen() -> Any:  # noqa: ANN401
            yield "x"

        return _gen()

    class _TTS:
        async def synthesize(self, text_stream: Any) -> Any:  # noqa: ANN401
            async for _ in text_stream:
                yield AudioChunk(
                    data=b"\x00",
                    sample_rate=48_000,  # WRONG — outbound rail expects 24k
                    num_channels=1,
                    samples_per_channel=1,
                )

        async def cancel(self) -> None: ...

    loop = StreamingLoop(voice_room=vr, session=sm, stt=_STT(), model=_model, tts=_TTS())
    await loop.start_pipeline()
    pipeline_task = loop._pipeline_task  # noqa: SLF001
    assert pipeline_task is not None
    await asyncio.wait_for(pipeline_task, timeout=2.0)
    assert any("cls=ValueError" in line for line in crashes), crashes
    assert vr.capture_outbound_frame.await_count == 0, "the wrong-rate chunk reached the rail"


# ---------- V4 barge-in ----------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_cancels_tts_and_notifies_session() -> None:
    vr = _build_voice_room_fake()
    sm = _build_session()
    notified: list[SessionLifecycleEvent] = []

    async def _on(ev: SessionLifecycleEvent, _s: Any) -> None:  # noqa: ANN401
        notified.append(ev)

    sm = SessionStateMachine(
        session_id="s",
        user_id="u",
        persona_id="p",
        conversation_id="c",
        rls_engine=MagicMock(),
        on_event=_on,
    )
    tts = MagicMock(spec=TTSStream)
    tts.cancel = AsyncMock(return_value=None)
    loop = StreamingLoop(voice_room=vr, session=sm, tts=tts)
    await loop.interrupt()
    tts.cancel.assert_awaited_once()
    assert notified == [SessionLifecycleEvent.AGENT_STOPPED_SPEAKING]


@pytest.mark.asyncio
async def test_interrupt_clears_outbound_queue() -> None:
    # Spec V3 T10 / D-V3-5 step 4: barge-in flushes the outbound rail so
    # queued "ghost" frames don't keep playing after the cancel.
    vr = _build_voice_room_fake()
    sm = _build_session()
    tts = MagicMock(spec=TTSStream)
    tts.cancel = AsyncMock(return_value=None)
    loop = StreamingLoop(voice_room=vr, session=sm, tts=tts)
    await loop.interrupt()
    vr.clear_outbound.assert_called_once()


@pytest.mark.asyncio
async def test_interrupt_without_tts_still_clears_outbound() -> None:
    # No TTS wired (echo mode) — interrupt still flushes the rail and notifies.
    vr = _build_voice_room_fake()
    sm = _build_session()
    loop = StreamingLoop(voice_room=vr, session=sm)
    await loop.interrupt()
    vr.clear_outbound.assert_called_once()


@pytest.mark.asyncio
async def test_stop_cancels_pipeline_task() -> None:
    vr = _build_voice_room_fake()
    sm = _build_session()

    class _STT:
        async def push_audio(self, pcm: bytes, sample_rate: int) -> None: ...

        async def transcripts(self) -> Any:  # noqa: ANN401
            await asyncio.sleep(3600)
            return
            yield

    class _Model:
        async def __call__(self, _t: Transcript) -> Any:  # noqa: ANN401
            return
            yield

    class _TTS:
        async def synthesize(self, _stream: Any) -> Any:  # noqa: ANN401
            return
            yield

        async def cancel(self) -> None: ...

    loop = StreamingLoop(voice_room=vr, session=sm, stt=_STT(), model=_Model(), tts=_TTS())
    await loop.start_pipeline()
    task = loop._pipeline_task  # noqa: SLF001
    assert task is not None
    await loop.stop()
    # `stop()` requested cancellation; let the cancel propagate.
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


# ---------- T07 (Spec V2 additive port) -------------------------------------


@pytest.mark.asyncio
async def test_streaming_loop_speech_activity_default_is_none() -> None:
    """V1 backwards-compat — speech_activity defaults to None when omitted."""
    vr = _build_voice_room_fake()
    sm = _build_session()
    loop = StreamingLoop(voice_room=vr, session=sm)
    assert loop.speech_activity is None


@pytest.mark.asyncio
async def test_streaming_loop_speech_activity_ctor_param_stored() -> None:
    """V2 D-V2-X-streaming-loop-additivity-shape — listener accessible via property."""

    class _Listener:
        async def on_speech_started(self, event: Any) -> None: ...  # noqa: ANN401
        async def on_speech_ended(self, event: Any) -> None: ...  # noqa: ANN401

    listener = _Listener()
    vr = _build_voice_room_fake()
    sm = _build_session()
    loop = StreamingLoop(voice_room=vr, session=sm, speech_activity=listener)  # type: ignore[arg-type]
    assert loop.speech_activity is listener


@pytest.mark.asyncio
async def test_streaming_loop_speech_activity_setter_wires_post_construction() -> None:
    """Production composition path — wire the listener after construction."""

    class _Listener:
        async def on_speech_started(self, event: Any) -> None: ...  # noqa: ANN401
        async def on_speech_ended(self, event: Any) -> None: ...  # noqa: ANN401

    listener = _Listener()
    vr = _build_voice_room_fake()
    sm = _build_session()
    loop = StreamingLoop(voice_room=vr, session=sm)
    assert loop.speech_activity is None
    loop.speech_activity = listener  # type: ignore[assignment]
    assert loop.speech_activity is listener
    loop.speech_activity = None
    assert loop.speech_activity is None
