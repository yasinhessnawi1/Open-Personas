"""R9-185: the per-turn VoiceLog must be produced by a real voice turn.

THE DEFECT CLASS. ``VoiceLog`` and ``JSONLVoiceLogWriter`` were the T10 per-turn
latency record, fully modelled and fully tested, and nothing under ``packages/*/src``
ever constructed one. The record existed only in its own unit tests: the whole
instrument shipped dark (completion-sweep shape 7), so every downstream number built
on it (``compute_e2e_ms``, V4's ``attribute_hops``) could only ever read an empty file.

WHAT THESE TESTS COVER.

1. One real turn, driven through the real turn-taking bridge (the orchestrator
   decides the turn ended and invokes the loop, exactly as production does), leaves
   exactly one ``VoiceLog`` row carrying the session identity and the latency
   segments the T10 design names: the end-of-utterance anchor, the model's first
   token, the first synthesised audio, and the first audio on the outbound rail.
2. The segments are real measurements, so the round-trip number
   (:func:`compute_e2e_ms`) and V4's per-hop attribution both resolve to numbers
   instead of ``None``.
3. Turns are numbered, so a second turn on the same call is a second row.
4. A turn that never reached audio (the stalled path) still leaves a row, with the
   unreached anchors left ``None`` rather than fabricated.
5. A writer that raises does not break the turn (fail-soft: instrumentation
   degrades the record, never the call).
6. The runner actually composes the writer (the structural half; without it the
   behaviour above is unreachable in production and the record ships dark again).
"""

from __future__ import annotations

import ast
import inspect
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from persona_voice.agent import runner
from persona_voice.logging import JSONLVoiceLogWriter, VoiceLog, compute_e2e_ms
from persona_voice.loop.streaming import AudioChunk, StreamingLoop, Transcript
from persona_voice.session.state_machine import SessionStateMachine
from persona_voice.stt.types import SpeechEndedEvent, SpeechStartedEvent
from persona_voice.turn_taking.bridge import wire_orchestrated_loop
from persona_voice.turn_taking.latency import attribute_hops

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from persona_voice.turn_taking.orchestrator import SchedulerHandle

_VOICE_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_VOICE_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_VOICE_TESTS_DIR))

from _mock_model import CancellableStubModel  # type: ignore[import-not-found]  # noqa: E402

_BASE = datetime(2026, 9, 18, 9, 0, 0, tzinfo=UTC)
#: The user stopped speaking 50 ms before the loop picked the turn up.
_EOU = _BASE - timedelta(milliseconds=50)
_STEP_MS = 100.0


class _StepClock:
    """A clock that advances a fixed step on every read (deterministic segments)."""

    def __init__(self, *, step_ms: float = _STEP_MS) -> None:
        self._reads = 0
        self._step_ms = step_ms

    def __call__(self) -> datetime:
        at = _BASE + timedelta(milliseconds=self._step_ms * self._reads)
        self._reads += 1
        return at


class _OrchClock:
    """The orchestrator's own clock (kept apart from the loop's step clock)."""

    def __init__(self) -> None:
        self._now = _BASE

    def __call__(self) -> datetime:
        return self._now

    def advance(self, ms: float) -> None:
        self._now = self._now + timedelta(milliseconds=ms)


class _FakeHandle:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _FakeScheduler:
    def __init__(self) -> None:
        self.scheduled: list[tuple[Callable[[], Awaitable[None]], _FakeHandle]] = []

    def call_later(
        self, _delay_s: float, callback: Callable[[], Awaitable[None]]
    ) -> SchedulerHandle:
        handle = _FakeHandle()
        self.scheduled.append((callback, handle))
        return handle

    async def fire_last(self) -> None:
        for cb, handle in reversed(self.scheduled):
            if not handle.cancelled:
                await cb()
                return
        msg = "no live scheduled callback"
        raise AssertionError(msg)


class _TTS:
    """One 24 kHz chunk per token (the V3 seam shape the loop expects)."""

    async def synthesize(self, text_stream: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
        async for _tok in text_stream:
            yield AudioChunk(
                data=b"\x00\x00", sample_rate=24_000, num_channels=1, samples_per_channel=1
            )

    async def cancel(self) -> None: ...


class _SilentTTS:
    """A TTS that yields no audio at all (the turn reaches no rail)."""

    async def synthesize(self, text_stream: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
        async for _tok in text_stream:
            pass
        return
        yield  # pragma: no cover - makes this an async generator

    async def cancel(self) -> None: ...


class _RaisingWriter:
    def __init__(self) -> None:
        self.attempts: list[VoiceLog] = []

    async def write(self, log: VoiceLog) -> None:
        self.attempts.append(log)
        msg = "disk full"
        raise OSError(msg)


def _voice_room_fake() -> Any:  # noqa: ANN401
    vr = MagicMock()
    vr.set_inbound_handler = MagicMock()
    vr.publish_outbound = AsyncMock(return_value=MagicMock())
    vr.capture_outbound_frame = AsyncMock(return_value=None)
    vr.clear_outbound = MagicMock(return_value=None)
    return vr


def _session() -> SessionStateMachine:
    return SessionStateMachine(
        session_id="sess-voice-log",
        user_id="u-owner",
        persona_id="p-jarvis",
        conversation_id="conv-7",
        rls_engine=MagicMock(),
    )


def _started(at: datetime) -> SpeechStartedEvent:
    return SpeechStartedEvent(ts_audio_s=1.0, ts_emit=at, source="silero", confidence=0.9)


def _ended(at: datetime) -> SpeechEndedEvent:
    return SpeechEndedEvent(ts_audio_s=2.0, ts_emit=at, source="silero")


class _Harness:
    """A real loop + real orchestrator, wired by the real bridge."""

    def __init__(self, *, model: Any, tts: Any, writer: Any) -> None:  # noqa: ANN401
        self.session = _session()
        self.loop = StreamingLoop(
            voice_room=_voice_room_fake(),
            session=self.session,
            model=model,
            tts=tts,
            voice_log_writer=writer,
            clock=_StepClock(),
        )
        self.sched = _FakeScheduler()
        self.clock = _OrchClock()
        self.orch = wire_orchestrated_loop(
            loop=self.loop, session=self.session, scheduler=self.sched, clock=self.clock
        )

    async def turn(self, text: str) -> None:
        await _drive_one_turn(self.orch, self.sched, self.clock, text)


async def _drive_one_turn(orch: Any, sched: _FakeScheduler, clock: _OrchClock, text: str) -> None:  # noqa: ANN401
    """One full user turn through the real bridge: speech, transcript, turn-end, reply."""
    await orch.on_speech_started(_started(clock()))
    await orch.on_transcript(Transcript(is_final=True, text=text, confidence=0.95, eou_at=_EOU))
    await orch.on_speech_ended(_ended(clock()))
    clock.advance(800.0)
    await sched.fire_last()
    inflight = orch._actions._task  # type: ignore[attr-defined]  # noqa: SLF001
    assert inflight is not None, "the orchestrator never invoked the model"
    await inflight


# ---------- the record a real turn leaves -----------------------------------


@pytest.mark.asyncio
async def test_one_real_turn_writes_one_voice_log_with_its_latency_segments(
    tmp_path: Path,
) -> None:
    """The T10 record, produced by the production turn path (R9-185)."""
    writer = JSONLVoiceLogWriter(tmp_path / "voice-turns.jsonl")
    harness = _Harness(
        model=CancellableStubModel(["Hello ", "there"], hold_after_first=False),
        tts=_TTS(),
        writer=writer,
    )

    await harness.turn("tell me something")

    (log,) = writer.read_all()
    # Identity comes off the live session, not a re-derived guess.
    assert log.session_id == "sess-voice-log"
    assert log.user_id == "u-owner"
    assert log.persona_id == "p-jarvis"
    assert log.conversation_id == "conv-7"
    assert log.turn_index == 0
    # The anchors the T10 design names, in the order a turn produces them.
    assert log.eou_at == _EOU
    assert log.llm_first_token_at is not None
    assert log.tts_first_audio_at is not None
    assert log.audio_first_play_at is not None
    assert log.started_at <= log.llm_first_token_at
    assert log.llm_first_token_at < log.tts_first_audio_at < log.audio_first_play_at
    assert log.ended_at is not None
    assert log.audio_first_play_at <= log.ended_at


@pytest.mark.asyncio
async def test_the_segments_resolve_to_numbers_not_none(tmp_path: Path) -> None:
    """A record with real anchors is what makes the round-trip number computable."""
    writer = JSONLVoiceLogWriter(tmp_path / "voice-turns.jsonl")
    harness = _Harness(
        model=CancellableStubModel(["Hi"], hold_after_first=False), tts=_TTS(), writer=writer
    )

    await harness.turn("hi")

    (log,) = writer.read_all()
    # started_at is read first, the model's first token second, the first
    # synthesised chunk third, the rail fourth: one _STEP_MS step apart each,
    # and the end-of-utterance anchor sits 50 ms before the turn started.
    assert compute_e2e_ms(log) == pytest.approx(50.0 + 3 * _STEP_MS)
    hops = attribute_hops(log)
    assert hops.processing_round_trip_ms == pytest.approx(50.0 + 3 * _STEP_MS)
    assert hops.model_first_token_ms is None  # stt_final_at is not V1's to stamp
    assert hops.transport_playout_ms is None  # tts_first_byte_at is the provider's


@pytest.mark.asyncio
async def test_a_second_turn_is_a_second_numbered_row(tmp_path: Path) -> None:
    writer = JSONLVoiceLogWriter(tmp_path / "voice-turns.jsonl")
    harness = _Harness(
        model=CancellableStubModel(["ok"], hold_after_first=False), tts=_TTS(), writer=writer
    )

    await harness.turn("first")
    await harness.turn("second")

    assert [row.turn_index for row in writer.read_all()] == [0, 1]


@pytest.mark.asyncio
async def test_a_turn_that_never_reached_audio_still_leaves_a_row(tmp_path: Path) -> None:
    """The unreached anchors stay ``None``: a silent turn is recorded, not fabricated."""
    writer = JSONLVoiceLogWriter(tmp_path / "voice-turns.jsonl")
    harness = _Harness(
        model=CancellableStubModel(["nothing"], hold_after_first=False),
        tts=_SilentTTS(),
        writer=writer,
    )

    await harness.turn("say nothing")

    (log,) = writer.read_all()
    assert log.llm_first_token_at is not None
    assert log.tts_first_audio_at is None
    assert log.audio_first_play_at is None
    assert compute_e2e_ms(log) is None


@pytest.mark.asyncio
async def test_a_failing_writer_never_breaks_the_turn() -> None:
    """Fail-soft: instrumentation degrades the record, never the call."""
    writer = _RaisingWriter()
    harness = _Harness(
        model=CancellableStubModel(["fine"], hold_after_first=False), tts=_TTS(), writer=writer
    )

    await harness.turn("still works")

    assert len(writer.attempts) == 1


@pytest.mark.asyncio
async def test_no_writer_wired_is_a_silent_no_op() -> None:
    """The port stays optional: every pre-existing loop construction is unaffected."""
    loop = StreamingLoop(
        voice_room=_voice_room_fake(),
        session=_session(),
        model=CancellableStubModel(["ok"], hold_after_first=False),
        tts=_TTS(),
    )
    await loop.invoke_model_for_turn(Transcript(is_final=True, text="hi", confidence=0.9))


# ---------- the production call site ----------------------------------------


def _streaming_loop_call() -> ast.Call:
    """The ``StreamingLoop(...)`` construction inside the agent runner."""
    source = inspect.getsource(runner)
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "StreamingLoop"
    ]
    assert len(calls) == 1, f"expected exactly one runner construction site, found {len(calls)}"
    return calls[0]


def test_the_runner_composes_a_voice_log_writer() -> None:
    """Without this keyword every turn's latency record goes nowhere (R9-185)."""
    call = _streaming_loop_call()
    writer = [kw for kw in call.keywords if kw.arg == "voice_log_writer"]
    assert writer, "runner builds StreamingLoop with no voice_log_writer"
    assert not any(kw.arg is None for kw in call.keywords), (
        "a **kwargs splat would make this guard blind"
    )


def test_the_runner_writer_is_the_jsonl_voice_log_writer() -> None:
    call = _streaming_loop_call()
    (writer,) = [kw.value for kw in call.keywords if kw.arg == "voice_log_writer"]
    assert isinstance(writer, ast.Call)
    assert isinstance(writer.func, ast.Name)
    assert writer.func.id == "JSONLVoiceLogWriter"
