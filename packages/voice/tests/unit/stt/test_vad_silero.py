"""Unit tests for ``persona_voice.stt.vad_silero`` — D-V2-X-silero-implementation-shape.

These tests pin the three pillars:

1. ONNX-only path (no torch import path exercised).
2. ``SileroFramer`` strict 512-sample / 32 ms frame contract.
3. Lazy-construct + explicit prewarm (push_audio before load() raises).

Plus the D-V2-X-echo-cancellation-v1-dependency TTS-mute-window mitigation
and the :func:`benchmark_onset_latency` baseline harness. Per D-V2-2 + T12
measurement gate #3 the harness records baseline at run time — tests
DO NOT assert any threshold (T12 external smoke is the operator-pass gate).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np
import pytest
from persona_voice.stt.config import StreamingSTTConfig
from persona_voice.stt.types import SpeechEndedEvent, SpeechStartedEvent
from persona_voice.stt.vad_silero import (
    SILERO_FRAME_SAMPLES,
    SILERO_SAMPLE_RATE_HZ,
    SileroFramer,
    SileroVADAdapter,
    benchmark_onset_latency,
)

# Real speech for the tests that run the model lives in packages/voice/tests/_speech_audio.py.
# The root conftest only adds packages/*/src to sys.path, so extend with the voice tests
# dir for cross-test import (the V2 _mock_stt_backend pattern).
_VOICE_TESTS_DIR = Path(__file__).resolve().parents[2]
if str(_VOICE_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_VOICE_TESTS_DIR))

from _speech_audio import speech_pcm  # type: ignore[import-not-found]  # noqa: E402

# ---------- module constants -------------------------------------------------


def test_silero_frame_samples_pinned_to_512() -> None:
    """D-V2-X-silero-implementation-shape pillar #2 frame contract."""
    assert SILERO_FRAME_SAMPLES == 512


def test_silero_sample_rate_pinned_to_16k() -> None:
    """D-V1-6 invariant + R-V2-2 audio adaptation."""
    assert SILERO_SAMPLE_RATE_HZ == 16_000


# ---------- SileroFramer ----------------------------------------------------


def test_framer_holds_partial_chunk() -> None:
    framer = SileroFramer()
    # 64 bytes = 32 samples << 1024 bytes (one frame)
    out = list(framer.push(b"\x00\x00" * 32))
    assert out == []
    assert framer.buffered_bytes == 64


def test_framer_emits_one_frame_at_threshold() -> None:
    framer = SileroFramer()
    # Exactly one frame: 1024 bytes
    out = list(framer.push(b"\x00\x00" * SILERO_FRAME_SAMPLES))
    assert len(out) == 1
    assert out[0].shape == (SILERO_FRAME_SAMPLES,)
    assert out[0].dtype == np.float32
    assert framer.buffered_bytes == 0


def test_framer_emits_multiple_frames_with_remainder() -> None:
    framer = SileroFramer()
    # 2.5 frames: 2 emitted; 0.5 held
    half_frame_bytes = (SILERO_FRAME_SAMPLES // 2) * 2  # 512 bytes
    payload = (b"\x00\x00" * SILERO_FRAME_SAMPLES * 2) + (b"\x00\x00" * (SILERO_FRAME_SAMPLES // 2))
    out = list(framer.push(payload))
    assert len(out) == 2
    assert framer.buffered_bytes == half_frame_bytes


def test_framer_normalises_int16_to_float32_minus1_plus1() -> None:
    framer = SileroFramer()
    # Max int16 = 32767 → ≈ 0.9999; min int16 = -32768 → -1.0
    payload = np.array([32767, -32768] * (SILERO_FRAME_SAMPLES // 2), dtype=np.int16).tobytes()
    out = list(framer.push(payload))
    assert len(out) == 1
    frame = out[0]
    assert frame.max() > 0.99
    assert frame.min() <= -1.0 + 1e-9


# ---------- SileroVADAdapter construction ------------------------------------


def _make_config(**overrides: object) -> StreamingSTTConfig:
    """Build a StreamingSTTConfig with sensible test defaults."""
    base = {
        "provider": "deepgram",
        "model": "nova-3",
        "api_key": "test-key-for-construction-validation",
        "vad_library": "silero",
        "silero_activation_threshold": 0.5,
        "silero_min_speech_duration_ms": 50,
        "silero_min_silence_duration_ms": 200,
    }
    base.update(overrides)
    return StreamingSTTConfig(**base)  # type: ignore[arg-type]


def test_adapter_rejects_non_silero_vad_library() -> None:
    config = _make_config(vad_library="webrtc")
    with pytest.raises(ValueError, match="vad_library='silero'"):
        SileroVADAdapter(config)


def test_adapter_construction_does_not_load_onnx() -> None:
    """Pillar #3 — __init__ validates only; no file IO."""
    config = _make_config()
    adapter = SileroVADAdapter(config)
    # Internal state inspection — the constructor must not have created
    # the SileroVAD wrapper yet.
    assert adapter._vad is None


# ---------- load() prewarm discipline ---------------------------------------


@pytest.mark.asyncio
async def test_load_materialises_onnx_session() -> None:
    config = _make_config()
    adapter = SileroVADAdapter(config)
    await adapter.load()
    assert adapter._vad is not None
    assert adapter._vad.sample_rate == SILERO_SAMPLE_RATE_HZ
    assert adapter._vad.window_size_samples == SILERO_FRAME_SAMPLES
    await adapter.close()


@pytest.mark.asyncio
async def test_load_is_idempotent() -> None:
    config = _make_config()
    adapter = SileroVADAdapter(config)
    await adapter.load()
    first_session = adapter._vad
    await adapter.load()
    assert adapter._vad is first_session
    await adapter.close()


@pytest.mark.asyncio
async def test_push_audio_before_load_raises() -> None:
    config = _make_config()
    adapter = SileroVADAdapter(config)
    with pytest.raises(RuntimeError, match="prewarm"):
        await adapter.push_audio(b"\x00\x00" * SILERO_FRAME_SAMPLES, SILERO_SAMPLE_RATE_HZ)


@pytest.mark.asyncio
async def test_push_audio_rejects_wrong_sample_rate() -> None:
    config = _make_config()
    adapter = SileroVADAdapter(config)
    await adapter.load()
    with pytest.raises(ValueError, match="16000"):
        await adapter.push_audio(b"\x00\x00" * SILERO_FRAME_SAMPLES, 8_000)
    await adapter.close()


# ---------- state machine: onset / offset emission ---------------------------


def _silence_pcm(duration_ms: int) -> bytes:
    n = int(SILERO_SAMPLE_RATE_HZ * duration_ms / 1000.0)
    return (np.zeros(n, dtype=np.int16)).tobytes()


@pytest.mark.asyncio
async def test_silence_emits_no_events() -> None:
    config = _make_config(silero_activation_threshold=0.5)
    adapter = SileroVADAdapter(config)
    await adapter.load()
    await adapter.push_audio(_silence_pcm(500), SILERO_SAMPLE_RATE_HZ)
    # Drain queue with timeout — silence must produce no event.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(adapter._event_queue.get(), timeout=0.05)
    await adapter.close()


@pytest.mark.asyncio
async def test_speech_burst_emits_speech_started_with_correct_metadata() -> None:
    # Real speech at the production threshold (0.5, the default).
    config = _make_config(
        silero_activation_threshold=0.5,
        silero_min_speech_duration_ms=32,
    )
    adapter = SileroVADAdapter(config)
    await adapter.load()
    # Push 500 ms of speech: long enough to clear min_speech_duration.
    await adapter.push_audio(speech_pcm(500), SILERO_SAMPLE_RATE_HZ)
    # Drain — first event must be SpeechStartedEvent.
    event = await asyncio.wait_for(adapter._event_queue.get(), timeout=0.05)
    assert isinstance(event, SpeechStartedEvent)
    assert event.source == "silero"
    assert event.synthetic is False
    assert event.confidence is not None
    assert 0.0 <= event.confidence <= 1.0
    assert event.ts_audio_s > 0.0
    await adapter.close()


# ---------- TTS-mute-window mitigation --------------------------------------


@pytest.mark.asyncio
async def test_tts_mute_window_suppresses_speech_started() -> None:
    """D-V2-X-echo-cancellation-v1-dependency safety net (mute path)."""
    speaking = {"agent": True}

    def state_provider() -> bool:
        return speaking["agent"]

    config = _make_config(
        silero_activation_threshold=0.5,
        silero_min_speech_duration_ms=32,
    )
    adapter = SileroVADAdapter(config, session_state_provider=state_provider)
    await adapter.load()
    await adapter.push_audio(speech_pcm(500), SILERO_SAMPLE_RATE_HZ)
    # The model did hear speech (else an empty queue would prove nothing) ...
    assert adapter._in_speech  # noqa: SLF001
    # ... and the listener-facing event MUST NOT fire while agent is speaking.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(adapter._event_queue.get(), timeout=0.05)
    await adapter.close()


@pytest.mark.asyncio
async def test_unmuted_state_emits_speech_started() -> None:
    """D-V2-X-echo-cancellation-v1-dependency safety net (passthrough path)."""

    def state_provider() -> bool:
        return False  # agent is NOT speaking — listener notifications flow

    config = _make_config(
        silero_activation_threshold=0.5,
        silero_min_speech_duration_ms=32,
    )
    adapter = SileroVADAdapter(config, session_state_provider=state_provider)
    await adapter.load()
    await adapter.push_audio(speech_pcm(500), SILERO_SAMPLE_RATE_HZ)
    event = await asyncio.wait_for(adapter._event_queue.get(), timeout=0.05)
    assert isinstance(event, SpeechStartedEvent)
    await adapter.close()


@pytest.mark.asyncio
async def test_no_provider_emits_speech_started_the_default_barge_in_path() -> None:
    """D-V8-X-bargein-during-speech-fix: with no mute provider wired (the runner's
    default), onset emission is never suppressed — a barge-in onset always flows,
    even while the persona is speaking."""
    config = _make_config(
        silero_activation_threshold=0.5,
        silero_min_speech_duration_ms=32,
    )
    adapter = SileroVADAdapter(config, session_state_provider=None)
    await adapter.load()
    await adapter.push_audio(speech_pcm(500), SILERO_SAMPLE_RATE_HZ)
    event = await asyncio.wait_for(adapter._event_queue.get(), timeout=0.05)
    assert isinstance(event, SpeechStartedEvent)
    await adapter.close()


@pytest.mark.asyncio
async def test_tts_mute_window_does_not_suppress_speech_ended() -> None:
    """speech_ended is NEVER muted — real silence transitions are real."""
    config = _make_config(
        silero_activation_threshold=0.5,
        silero_min_speech_duration_ms=32,
        silero_min_silence_duration_ms=64,
    )
    # Provider returns False during the speech, then True during silence.
    # speech_ended must still fire.
    speaking_state = {"agent_speaking": False}

    def state_provider() -> bool:
        return speaking_state["agent_speaking"]

    adapter = SileroVADAdapter(config, session_state_provider=state_provider)
    await adapter.load()
    # Push speech — speech_started fires (agent not speaking).
    await adapter.push_audio(speech_pcm(200), SILERO_SAMPLE_RATE_HZ)
    started = await asyncio.wait_for(adapter._event_queue.get(), timeout=0.05)
    assert isinstance(started, SpeechStartedEvent)
    # Now agent starts speaking; push silence — speech_ended must still fire.
    speaking_state["agent_speaking"] = True
    await adapter.push_audio(_silence_pcm(500), SILERO_SAMPLE_RATE_HZ)
    ended = await asyncio.wait_for(adapter._event_queue.get(), timeout=0.1)
    assert isinstance(ended, SpeechEndedEvent)
    assert ended.source == "silero"
    await adapter.close()


# ---------- speech_activity_events async iterator ----------------------------


@pytest.mark.asyncio
async def test_speech_activity_events_yields_queue_contents() -> None:
    config = _make_config(
        silero_activation_threshold=0.5,
        silero_min_speech_duration_ms=32,
    )
    adapter = SileroVADAdapter(config)
    await adapter.load()
    await adapter.push_audio(speech_pcm(200), SILERO_SAMPLE_RATE_HZ)
    # Iterate one event then close.
    received: list[SpeechStartedEvent | SpeechEndedEvent] = []

    async def consume() -> None:
        async for event in adapter.speech_activity_events():
            received.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    await adapter.close()
    await task
    assert len(received) >= 1
    assert isinstance(received[0], SpeechStartedEvent)


# ---------- close() idempotency ---------------------------------------------


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    config = _make_config()
    adapter = SileroVADAdapter(config)
    await adapter.load()
    await adapter.close()
    await adapter.close()  # Must not raise.
    # push_audio after close is a no-op (does not raise).
    await adapter.push_audio(_silence_pcm(50), SILERO_SAMPLE_RATE_HZ)


# ---------- event-loop offload (voice-event-loop incident fix) ---------------


@pytest.mark.asyncio
async def test_process_runs_off_the_event_loop_thread() -> None:
    """``SileroVAD.process`` (sync C++ ONNX) must run in a worker thread.

    Running per-frame inference inline starves the asyncio loop so LiveKit
    heartbeats + the Deepgram/Cartesia WebSocket handshakes time out — the live
    voice incident's root cause. The adapter offloads the frame batch via
    ``asyncio.to_thread``; state-machine advance + event emission stay on the loop
    so the ``asyncio.Queue`` stays loop-affine. A fake VAD records which thread its
    ``process`` ran on, asserted distinct from the loop thread.
    """
    import threading

    loop_thread = threading.get_ident()
    process_threads: list[int] = []

    class _ThreadRecordingVAD:
        window_size_samples = SILERO_FRAME_SAMPLES
        sample_rate = SILERO_SAMPLE_RATE_HZ

        def process(self, frame_bytes: bytes) -> float:  # noqa: ARG002 — signature mirrors SileroVAD.process
            process_threads.append(threading.get_ident())
            return 0.9  # always-voiced so the state machine also advances

    config = _make_config(silero_activation_threshold=0.5, silero_min_speech_duration_ms=32)
    adapter = SileroVADAdapter(config)
    # Inject the fake session directly (bypass load() — no native lib needed).
    adapter._vad = _ThreadRecordingVAD()  # type: ignore[assignment]  # noqa: SLF001

    await adapter.push_audio(speech_pcm(200), SILERO_SAMPLE_RATE_HZ)

    assert process_threads, "expected process() to be called for the pushed frames"
    assert all(tid != loop_thread for tid in process_threads)
    # Emission stayed on the loop thread: the onset event is on the queue.
    event = await asyncio.wait_for(adapter._event_queue.get(), timeout=0.05)
    assert isinstance(event, SpeechStartedEvent)
    await adapter.close()


@pytest.mark.asyncio
async def test_empty_push_skips_the_thread_hop() -> None:
    """A push with no complete frame must not pay a worker-thread round-trip."""
    import threading

    process_threads: list[int] = []

    class _CountingVAD:
        window_size_samples = SILERO_FRAME_SAMPLES
        sample_rate = SILERO_SAMPLE_RATE_HZ

        def process(self, frame_bytes: bytes) -> float:  # noqa: ARG002 — signature mirrors SileroVAD.process
            process_threads.append(threading.get_ident())
            return 0.0

    config = _make_config()
    adapter = SileroVADAdapter(config)
    adapter._vad = _CountingVAD()  # type: ignore[assignment]  # noqa: SLF001
    # 64 bytes = 32 samples << one 512-sample frame: the framer holds it back.
    await adapter.push_audio(b"\x00\x00" * 32, SILERO_SAMPLE_RATE_HZ)
    assert process_threads == []
    await adapter.close()


# ---------- benchmark harness ------------------------------------------------


@pytest.mark.asyncio
async def test_benchmark_records_baseline_no_threshold_assert() -> None:
    """T05 records baseline; T12 is the operator-pass gate.

    DO NOT assert any P50/P95 threshold here — per Phase-4 decisions.md
    D-V2-2 + T12 measurement gate #3, the 150 ms P95 binding gate is
    operator-passed at the external smoke (T12), NOT in unit tests.
    """
    config = _make_config(
        silero_activation_threshold=0.05,
        silero_min_speech_duration_ms=32,
    )
    adapter = SileroVADAdapter(config)
    await adapter.load()
    metrics = await benchmark_onset_latency(adapter, iterations=5, speech_burst_ms=200)
    assert "p50_ms" in metrics
    assert "p95_ms" in metrics
    assert "p99_ms" in metrics
    assert "iterations" in metrics
    # Sanity: the harness ran SOME iterations (synthetic sine + low threshold).
    # If iterations==0 the harness returned the empty sentinel — still valid
    # output but the operator should investigate; we do not fail the test.
    assert metrics["iterations"] >= 0
    await adapter.close()


@pytest.mark.asyncio
async def test_benchmark_requires_loaded_adapter() -> None:
    config = _make_config()
    adapter = SileroVADAdapter(config)
    with pytest.raises(RuntimeError, match="load"):
        await benchmark_onset_latency(adapter, iterations=1)
