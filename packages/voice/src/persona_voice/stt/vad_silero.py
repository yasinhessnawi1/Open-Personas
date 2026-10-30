"""Silero VAD adapter — local speech-activity detection per D-V2-X-silero-implementation-shape LOCK.

Three pillars per Phase-4 decisions.md:

1. **ONNX-only path.** Uses ``silero-vad-lite`` (MIT; bundles `silero_vad.onnx` v5
   weights + C++ ONNX runtime wrapper; single transitive surface, no torch).
   The official ``silero-vad`` PyPI pulls torch (200-500 MB) — rejected here.
2. **`SileroFramer` mandatory.** V1's variable PCM16 frame cadence (room.py
   delivers whatever the LiveKit substrate produces) does NOT align with
   Silero's strict 512-sample / 32 ms windows. The framer buffers raw PCM16
   bytes + emits exact 512-sample float32 arrays normalised to [-1.0, 1.0].
   Worst-case latency add: ≤31 ms (one Silero frame minus one sample). Per
   D-V2-2 wall-clock framing this lands the WORST-CASE onset at 116-121 ms;
   typical at 85-90 ms. T05 benchmark harness records the measurement.
3. **Lazy-construct + explicit prewarm.** ``__init__`` validates Pydantic
   config + framer + state; the C++ ONNX session materialises only when
   ``load()`` is called (typically at agent-worker process init). First-frame
   lazy-load is REJECTED because LiveKit issue #4761 documents an
   "inference slower than realtime" cold-start spike on the first 1-2 s of
   audio (Windows-scoped upstream; T05 records baseline on actual deployment
   OS via the benchmark harness). Spec 02 D-02-10 HFLocalBackend precedent
   for the lazy-validate-eager-prewarm shape.

The adapter implements neither the V1 ``STTStream`` Protocol nor the V2
``StreamingSTT`` Protocol — those carry transcripts, not activity events.
It produces a separate :class:`~persona_voice.stt.types.SpeechStartedEvent`
/ :class:`~persona_voice.stt.types.SpeechEndedEvent` stream on the V4
``SpeechActivityListener`` port per D-V2-X-activity-listener-shape (Pipecat
issue #1323 production-bug precedent: mixing activity events with
transcripts on one queue causes 4× repetitions in production). T06's
``V1STTStreamSeamAdapter`` is the composition root that wires this adapter
in parallel with the STT backend.

TTS-mute-window safety net per D-V2-X-echo-cancellation-v1-dependency
(now **opt-in, default OFF** — D-V8-X-bargein-during-speech-fix, operator-pass
2026-06-23): the adapter accepts a ``session_state_provider: Callable[[], bool]``
callable that returns True when the persona is currently speaking; while it
returns True, the adapter suppresses ``speech_started`` event emission. The raw
VAD score still runs internally (so ``speech_ended`` boundaries from previous
utterances still flush correctly) — only the listener-facing notification is
muted.

**Why it is off by default.** A hard mute is too blunt: it also suppresses a
*real* barge-in onset, so the persona could not be interrupted while speaking,
and (after V8 gated the billed STT stream during persona speech) the user's
barge-in audio was withheld from Deepgram until the persona finished — a
transcription regression ("thank you" → a clipped fragment). Browser/transport
AEC (on by default) removes the persona's echo from the inbound mic, and the
orchestrator's confidence floor + confirm-window are the primary echo defense,
so the runner leaves ``session_state_provider`` unwired unless
``PERSONA_STT_SILERO_ECHO_MUTE_WHILE_SPEAKING`` is set (a proven-no-AEC
deployment). Silero's published ~51 % FP rate on TTS bleed-through (LiveKit
adaptive-interruption data) is a *raw*-bleed-through figure — AEC makes it moot.

The benchmark harness :func:`benchmark_onset_latency` measures the
wall-clock onset latency INCLUDING the ``SileroFramer`` overhead on the
actual agent-worker CPU. Per D-V2-2 + T12 measurement gate #3, T05 records
the baseline at run time but UNIT TESTS MUST NOT ASSERT any threshold —
the 150 ms P95 binding gate is operator-passed at T12 external smoke.

See:
- ``docs/specs/phase2/spec_V2/decisions.md`` D-V2-2 + D-V2-X-silero-implementation-shape
  + D-V2-X-echo-cancellation-v1-dependency
- ``docs/specs/phase2/spec_V2/research.md`` R-V2-2 — Silero onset budget +
  combination_design + silero_chunk_design
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
from silero_vad_lite import SileroVAD

from persona_voice.stt.types import SpeechEndedEvent, SpeechStartedEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterable

    import numpy.typing as npt

    from persona_voice.stt.config import StreamingSTTConfig


__all__ = [
    "SILERO_FRAME_SAMPLES",
    "SILERO_SAMPLE_RATE_HZ",
    "SileroFramer",
    "SileroVADAdapter",
    "benchmark_onset_latency",
]


# D-V2-X-silero-implementation-shape pillar #2 — strict frame contract.
# 512 samples at 16 kHz = exactly 32 ms. ``silero-vad-lite``'s SileroVAD
# enforces this at runtime via ``window_size_samples`` introspection;
# encoded here as a module constant so callers (T06 seam adapter, T11
# integration spine) can pin the invariant in tests + assertions without
# constructing a SileroVAD instance.
SILERO_FRAME_SAMPLES: int = 512
SILERO_SAMPLE_RATE_HZ: int = 16_000


class SileroFramer:
    """Buffers raw PCM16 little-endian bytes into Silero's exact frame contract.

    V1 inbound frames carry whatever cadence the LiveKit substrate produces
    (typically 20-50 ms per frame). Silero requires EXACTLY 512 samples at
    16 kHz mono float32 per inference call; any other size silently corrupts
    probability output (snakers4 source) or raises in Pipecat's enforcement
    wrapper. This buffer holds the running PCM16 byte stream + emits one
    or more float32 arrays of shape ``(512,)`` per push as the bytes
    accumulate past the 1024-byte (= 512 sample × 2 byte) threshold.

    The conversion is ``int16 → float32 / 32768.0`` (the standard PCM16
    normalisation). Per D-V1-6 + R-V2-2 audio adaptation: this is the only
    sample-rate / format transformation V2 performs on inbound audio
    (T04's DeepgramStreamingSTT forwards V1 PCM16 bytes verbatim).
    """

    _BYTES_PER_FRAME = SILERO_FRAME_SAMPLES * 2  # int16 → 2 bytes per sample

    def __init__(self) -> None:
        self._buf: bytearray = bytearray()

    def push(self, pcm16_le: bytes) -> Iterable[npt.NDArray[np.float32]]:
        """Append PCM16 bytes; yield zero or more 512-sample float32 frames.

        Args:
            pcm16_le: Raw PCM16 little-endian bytes. Length need not be a
                multiple of the frame size; the remainder is held until the
                next push.

        Yields:
            Float32 numpy arrays of shape ``(SILERO_FRAME_SAMPLES,)``
            normalised to ``[-1.0, 1.0]``, one per complete frame in the
            buffer.
        """
        self._buf.extend(pcm16_le)
        while len(self._buf) >= self._BYTES_PER_FRAME:
            frame_bytes = bytes(self._buf[: self._BYTES_PER_FRAME])
            del self._buf[: self._BYTES_PER_FRAME]
            yield (np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float32) / 32768.0)

    @property
    def buffered_bytes(self) -> int:
        """Bytes currently held back waiting for a full frame (test helper)."""
        return len(self._buf)


class SileroVADAdapter:
    """Silero VAD adapter emitting `SpeechStartedEvent` / `SpeechEndedEvent`.

    Implements neither ``STTStream`` nor ``StreamingSTT`` — those carry
    transcripts. The adapter exposes a separate
    :meth:`speech_activity_events` async iterator yielding events on the
    V4 ``SpeechActivityListener`` port per D-V2-X-activity-listener-shape.

    Internal state machine:

    * Per-frame VAD score from ``SileroVAD.process(frame)`` is compared
      against ``config.silero_activation_threshold``.
    * Consecutive in-speech frames accumulate; ``speech_started`` fires
      after at least ``config.silero_min_speech_duration_ms`` of voiced
      audio.
    * Once started, consecutive silence frames accumulate; ``speech_ended``
      fires after at least ``config.silero_min_silence_duration_ms`` of
      silence.
    * The state machine is fully deterministic + monotonic; ``ts_audio_s``
      counts samples-pushed / sample-rate so it survives jitter at the
      transport boundary.

    TTS-mute-window safety net (D-V2-X-echo-cancellation-v1-dependency):
    if ``session_state_provider`` is wired AND returns True at the moment
    an in-speech transition would fire ``speech_started``, the event is
    suppressed (the internal state still advances so subsequent
    ``speech_ended`` boundaries flush correctly; only the listener-facing
    notification is muted). ``speech_ended`` is NEVER muted — even
    bleed-through utterance ends are real silence transitions in the audio
    stream and downstream consumers must be told.
    """

    def __init__(
        self,
        config: StreamingSTTConfig,
        *,
        session_state_provider: Callable[[], bool] | None = None,
    ) -> None:
        """Validate config + framer; defer ONNX session to :meth:`load`.

        Args:
            config: Streaming-STT config carrying Silero tuning knobs
                (``silero_activation_threshold``,
                ``silero_min_speech_duration_ms``,
                ``silero_min_silence_duration_ms``,
                ``vad_library``).
            session_state_provider: Optional callable returning True iff
                the persona is currently speaking (TTS-mute-window per
                D-V2-X-echo-cancellation-v1-dependency). T06 seam adapter
                wires this in production composition.

        Raises:
            ValueError: ``config.vad_library`` is not ``"silero"``.
        """
        if config.vad_library != "silero":
            msg = (
                f"SileroVADAdapter requires vad_library='silero' "
                f"(got {config.vad_library!r}); D-V2-X-silero-implementation-shape "
                f"is the only v0.1-locked path"
            )
            raise ValueError(msg)
        self._config = config
        self._session_state_provider = session_state_provider
        self._framer = SileroFramer()
        # Lazy ONNX session — materialised in load().
        self._vad: SileroVAD | None = None
        # State-machine accumulators.
        self._consecutive_voiced_ms: float = 0.0
        self._consecutive_silent_ms: float = 0.0
        self._in_speech: bool = False
        self._samples_pushed: int = 0
        # Event-bridge queue: speech_activity_events() drains this.
        self._event_queue: asyncio.Queue[SpeechStartedEvent | SpeechEndedEvent | None] = (
            asyncio.Queue()
        )
        self._closed: bool = False

    async def load(self) -> None:
        """Materialise the Silero ONNX session (idempotent).

        Called at agent-worker process init for prewarm per
        D-V2-X-silero-implementation-shape pillar #3. First-frame lazy
        load is REJECTED because LiveKit issue #4761 documents a
        cold-start "inference slower than realtime" spike on the first
        1-2 s (Windows-scoped upstream; T05 benchmark harness records the
        baseline on actual deployment OS).

        Idempotent: second call is a no-op + returns the existing session.
        """
        if self._vad is not None:
            return
        # SileroVAD(sample_rate) loads the bundled v5 ONNX weights via the
        # ``silero-vad-lite`` shared library. window_size_samples is
        # constant 512 at 16 kHz; verified at __init__ in the wheel.
        self._vad = SileroVAD(SILERO_SAMPLE_RATE_HZ)
        # Sanity-pin the frame contract — protects against any future
        # silero-vad-lite version that changes window_size_samples.
        if self._vad.window_size_samples != SILERO_FRAME_SAMPLES:
            msg = (
                f"SileroVAD window_size_samples={self._vad.window_size_samples} "
                f"does not match expected {SILERO_FRAME_SAMPLES}; the "
                f"D-V2-X-silero-implementation-shape pillar #2 frame contract is "
                f"violated — silero-vad-lite version drift"
            )
            raise RuntimeError(msg)

    async def push_audio(self, pcm: bytes, sample_rate: int) -> None:
        """Push PCM16 audio + advance the VAD state machine.

        Args:
            pcm: PCM16 little-endian bytes. Length need not be a multiple of
                512 samples; the :class:`SileroFramer` buffers the remainder.
            sample_rate: Sample rate in Hz. Must be ``SILERO_SAMPLE_RATE_HZ``
                (16 000); other rates raise (D-V1-6 invariant + Silero
                strict-frame contract).

        Raises:
            RuntimeError: :meth:`load` has not been called yet (explicit
                prewarm discipline per D-V2-X-silero-implementation-shape).
            ValueError: ``sample_rate`` does not match ``SILERO_SAMPLE_RATE_HZ``.
        """
        if self._closed:
            # After close() the adapter is inert; subsequent push_audio
            # calls are no-ops (eases shutdown paths in T06 seam adapter).
            return
        if self._vad is None:
            msg = (
                "SileroVADAdapter.push_audio called before load(); "
                "D-V2-X-silero-implementation-shape pillar #3 requires explicit "
                "prewarm at agent-worker process init (LiveKit issue #4761 "
                "cold-start spike)"
            )
            raise RuntimeError(msg)
        if sample_rate != SILERO_SAMPLE_RATE_HZ:
            msg = (
                f"SileroVADAdapter expects {SILERO_SAMPLE_RATE_HZ} Hz "
                f"(D-V1-6 invariant + Silero frame contract); got {sample_rate}"
            )
            raise ValueError(msg)
        # Per-frame state-machine advance. ms_per_frame is a module constant
        # (32 ms at 16 kHz) computed once.
        ms_per_frame = (SILERO_FRAME_SAMPLES / SILERO_SAMPLE_RATE_HZ) * 1000.0
        # ``SileroVAD.process`` is synchronous C++ ONNX inference, invoked per
        # 512-sample frame at high frequency for the whole call. Running it inline
        # starves the asyncio loop so LiveKit heartbeats + the Deepgram/Cartesia
        # WebSocket handshakes time out — the live voice incident's root cause.
        # Offload this push's whole frame batch to ONE worker thread (one hop per
        # push, not per frame, so the high-frequency path is not pessimised), then
        # advance the deterministic state machine + emit events back ON the loop:
        # the ``asyncio.Queue`` is loop-affine (``put_nowait`` is not thread-safe),
        # so emission must stay on the loop thread.
        frames = list(self._framer.push(pcm))
        if not frames:
            return
        scores = await asyncio.to_thread(self._score_frames, self._vad, frames)
        for score in scores:
            self._samples_pushed += SILERO_FRAME_SAMPLES
            voiced = score >= self._config.silero_activation_threshold
            if voiced:
                self._consecutive_voiced_ms += ms_per_frame
                self._consecutive_silent_ms = 0.0
                if (
                    not self._in_speech
                    and self._consecutive_voiced_ms >= self._config.silero_min_speech_duration_ms
                ):
                    self._in_speech = True
                    self._emit_speech_started(score)
            else:
                self._consecutive_silent_ms += ms_per_frame
                self._consecutive_voiced_ms = 0.0
                if (
                    self._in_speech
                    and self._consecutive_silent_ms >= self._config.silero_min_silence_duration_ms
                ):
                    self._in_speech = False
                    self._emit_speech_ended()

    @staticmethod
    def _score_frames(vad: SileroVAD, frames: list[npt.NDArray[np.float32]]) -> list[float]:
        """Run the blocking ONNX inference for a batch of frames (off-loop).

        Called via :func:`asyncio.to_thread` from :meth:`push_audio`. Touches only
        the ``vad`` argument (the C++ ONNX session) — never adapter state — so it is
        safe to run off the event-loop thread. The state machine advances on the
        loop thread once the scores return. ``vad`` is passed explicitly (rather
        than read from ``self`` in the thread) so the live-session reference cannot
        be torn out by a concurrent :meth:`close` mid-inference.

        Args:
            vad: The loaded Silero ONNX session.
            frames: This push's 512-sample float32 frames, in order.

        Returns:
            One activation score per frame, in order.
        """
        return [vad.process(frame.tobytes()) for frame in frames]

    def _emit_speech_started(self, score: float) -> None:
        """Emit speech_started unless TTS-mute-window is active."""
        if self._session_state_provider is not None and self._session_state_provider():
            # D-V2-X-echo-cancellation-v1-dependency safety net: persona is
            # speaking; suppress listener-facing notification. Internal
            # state still advances so subsequent speech_ended flushes.
            return
        event = SpeechStartedEvent(
            ts_audio_s=self._samples_pushed / SILERO_SAMPLE_RATE_HZ,
            ts_emit=datetime.now(UTC),
            source="silero",
            synthetic=False,
            confidence=float(score),
        )
        # put_nowait — the event-bridge queue is unbounded; backpressure is
        # not a v0.1 concern (single-call per session per D-V1-5 advisory
        # lock cap).
        self._event_queue.put_nowait(event)

    def _emit_speech_ended(self) -> None:
        """Emit speech_ended unconditionally — silence transitions are real."""
        event = SpeechEndedEvent(
            ts_audio_s=self._samples_pushed / SILERO_SAMPLE_RATE_HZ,
            ts_emit=datetime.now(UTC),
            source="silero",
            synthetic=False,
            transcript_settled=False,
            corroborates=False,
            silence_duration_ms=int(self._consecutive_silent_ms),
        )
        self._event_queue.put_nowait(event)

    async def speech_activity_events(
        self,
    ) -> AsyncIterator[SpeechStartedEvent | SpeechEndedEvent]:
        """Drain the event bridge until :meth:`close` is called.

        Yields:
            ``SpeechStartedEvent`` and ``SpeechEndedEvent`` instances as
            the state machine fires them.
        """
        while True:
            event = await self._event_queue.get()
            if event is None:
                # Sentinel from close() — terminate the iterator cleanly.
                return
            yield event

    async def close(self) -> None:
        """Stop draining the event bridge; release the ONNX session.

        Idempotent: second call is a no-op. The ``silero-vad-lite``
        ``SileroVAD`` C++ wrapper releases via ``__del__``; we drop our
        reference so the next GC cycle frees the native handle.
        """
        if self._closed:
            return
        self._closed = True
        # Sentinel terminates the event iterator on its next .get().
        self._event_queue.put_nowait(None)
        self._vad = None


async def benchmark_onset_latency(
    adapter: SileroVADAdapter,
    *,
    speech: bytes,
    iterations: int = 100,
) -> dict[str, float]:
    """Measure wall-clock onset latency INCLUDING `SileroFramer` overhead.

    Per D-V2-2 + T12 measurement gate #3, T05 records the baseline at run
    time but UNIT TESTS MUST NOT ASSERT any threshold — the 150 ms P95
    binding gate is operator-passed at T12 external smoke.

    Methodology: each iteration starts the model from a fresh state
    (``SileroVAD.reset()`` clears its recurrent state and audio context, so the
    burst is a true silence-to-speech transition and not a continuation of the
    previous one), pushes the caller's speech burst, and measures the
    wall-clock interval from the ``push_audio`` call to the ``speech_started``
    event arriving on the event queue. Repeat ``iterations`` times; return
    percentiles.

    The burst must be speech the model scores as speech at the adapter's
    threshold. A pure tone is not: silero-vad-lite 0.3 scores a 440 Hz sine
    near 0.002, which is why this harness takes recorded or rendered speech
    from the caller instead of synthesising one (the tests pass a slice of the
    committed V8 corpus, ``tests/_speech_audio.py``).

    Args:
        adapter: Loaded (``load()`` called) :class:`SileroVADAdapter`.
        speech: The speech burst pushed each iteration: 16 kHz mono PCM16
            little-endian, at least one 512-sample Silero frame.
        iterations: Number of onset transitions to sample.

    Returns:
        Dict with keys ``p50_ms``, ``p95_ms``, ``p99_ms``, ``iterations``
        (the number of onsets captured; an iteration whose burst raised no
        onset within 500 ms is not counted). All zero if no onset was
        captured; callers that gate on the result check ``iterations``.

    Raises:
        RuntimeError: :meth:`SileroVADAdapter.load` has not been called.
        ValueError: ``speech`` is shorter than one frame or not whole samples.
    """
    if adapter._vad is None:
        msg = "benchmark_onset_latency requires adapter.load() called first"
        raise RuntimeError(msg)
    frame_bytes = SILERO_FRAME_SAMPLES * 2
    if len(speech) < frame_bytes or len(speech) % 2:
        msg = (
            f"benchmark_onset_latency needs a speech burst of whole PCM16 samples, "
            f"at least one 512-sample frame ({frame_bytes} bytes); got {len(speech)} bytes"
        )
        raise ValueError(msg)

    latencies_ms: list[float] = []
    for _ in range(iterations):
        # Reset adapter state between iterations by draining queue + zeroing
        # the state machine and the model. We deliberately access internals
        # here (the harness IS the benchmark; public API would over-constrain).
        while not adapter._event_queue.empty():
            try:
                adapter._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        adapter._in_speech = False
        adapter._consecutive_voiced_ms = 0.0
        adapter._consecutive_silent_ms = 0.0
        adapter._framer = SileroFramer()
        adapter._vad.reset()

        t_start = time.perf_counter()
        await adapter.push_audio(speech, SILERO_SAMPLE_RATE_HZ)
        # Best-effort onset detection: drain queue with a short timeout.
        try:
            await asyncio.wait_for(adapter._event_queue.get(), timeout=0.5)
            elapsed_ms = (time.perf_counter() - t_start) * 1000.0
            latencies_ms.append(elapsed_ms)
        except TimeoutError:
            # No onset within 500 ms: the burst did not cross the activation
            # threshold. Not counted; callers gate on the ``iterations`` count.
            continue

    if not latencies_ms:
        return {"p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "iterations": 0.0}

    arr = np.array(latencies_ms)
    return {
        "p50_ms": float(np.percentile(arr, 50)),
        "p95_ms": float(np.percentile(arr, 95)),
        "p99_ms": float(np.percentile(arr, 99)),
        "iterations": float(len(latencies_ms)),
    }
