"""A noise-triggered phantom turn must give the floor back, not freeze on it (R9-119).

The owner's number-one voice complaint across three specs: "stuck on listening,
small noise around". Mechanism: room noise trips the VAD, the orchestrator enters
USER_SPEAKING, the noise stops and arms the turn-end timer, the controller returns
WAIT (an uncorroborated offset with no transcript is "most likely a noise blip"),
and WAIT was terminal -- nothing re-armed the timer, nothing reset, and the mic
stayed gated until the user made a second noise. Threshold tuning could never fix
that, because no threshold changes what happens after a WAIT.

These pin the bounded escape and, just as importantly, that real turns and real
hold-token pauses are untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from persona_voice.loop.streaming import Transcript
from persona_voice.stt.types import SpeechEndedEvent, SpeechStartedEvent
from persona_voice.turn_taking import orchestrator as orch_mod
from persona_voice.turn_taking.orchestrator import ConversationalOrchestrator
from persona_voice.turn_taking.states import ConversationalState

_BASE = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = _BASE

    def __call__(self) -> datetime:
        return self.now

    def advance_ms(self, ms: float) -> None:
        self.now += timedelta(milliseconds=ms)


class _Handle:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _Scheduler:
    """Records call_later; the test fires the newest live callback by hand."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[object, _Handle]] = []

    def call_later(self, _delay_s: float, callback: object) -> _Handle:
        handle = _Handle()
        self.scheduled.append((callback, handle))
        return handle

    async def fire_last(self) -> bool:
        for cb, handle in reversed(self.scheduled):
            if not handle.cancelled:
                handle.cancelled = True
                await cb()  # type: ignore[operator]
                return True
        return False

    def live(self) -> int:
        return sum(1 for _, h in self.scheduled if not h.cancelled)


class _Actions:
    def __init__(self) -> None:
        self.invocations = 0

    async def invoke_model_for_turn(self, _t: object) -> None:
        self.invocations += 1

    async def cancel_generation(self) -> None: ...

    async def interrupt(self) -> None: ...


def _build(clock: _Clock, sched: _Scheduler) -> tuple[ConversationalOrchestrator, _Actions]:
    actions = _Actions()
    orch = ConversationalOrchestrator(
        actions=actions,  # type: ignore[arg-type]
        scheduler=sched,  # type: ignore[arg-type]
        clock=clock,
    )
    return orch, actions


async def _noise_blip(orch: ConversationalOrchestrator, clock: _Clock) -> None:
    """The real trigger chain: an onset with no transcript, then an UNcorroborated offset."""
    await orch.on_speech_started(
        SpeechStartedEvent(ts_audio_s=0.0, ts_emit=clock(), source="silero", confidence=0.9)
    )
    clock.advance_ms(200)
    await orch.on_speech_ended(SpeechEndedEvent(ts_audio_s=0.2, ts_emit=clock(), source="silero"))


async def _drain_timers(clock: _Clock, sched: _Scheduler) -> int:
    """Fire the turn-end timer until nothing is armed; return how many times it fired."""
    fired = 0
    while sched.live() and fired < 50:
        clock.advance_ms(2_000)  # well past any silence threshold
        if not await sched.fire_last():
            break
        fired += 1
    return fired


@pytest.mark.asyncio
async def test_a_noise_blip_returns_the_floor_without_answering() -> None:
    """THE regression: the session must not stay wedged in USER_SPEAKING."""
    clock, sched = _Clock(), _Scheduler()
    orch, actions = _build(clock, sched)

    await _noise_blip(orch, clock)
    assert orch.state is ConversationalState.USER_SPEAKING  # the phantom turn opened

    fired = await _drain_timers(clock, sched)

    assert orch.state is ConversationalState.LISTENING, "still wedged after the noise stopped"
    assert actions.invocations == 0, "a cough must not be answered"
    # Bounded: the initial arm plus at most PHANTOM_TURN_REARM_LIMIT re-arms.
    assert fired == orch_mod.PHANTOM_TURN_REARM_LIMIT + 1


@pytest.mark.asyncio
async def test_the_escape_is_bounded_not_a_spin() -> None:
    """Re-arming forever would trade a frozen mic for a busy loop; the bound must hold."""
    clock, sched = _Clock(), _Scheduler()
    orch, _ = _build(clock, sched)

    await _noise_blip(orch, clock)
    fired = await _drain_timers(clock, sched)

    assert fired <= orch_mod.PHANTOM_TURN_REARM_LIMIT + 1
    assert sched.live() == 0, "a timer is still armed after the reset"


@pytest.mark.asyncio
async def test_a_real_turn_still_ends_and_is_answered_once() -> None:
    """Untouched path: real speech with a transcript ends the turn exactly as before."""
    clock, sched = _Clock(), _Scheduler()
    orch, actions = _build(clock, sched)

    await orch.on_speech_started(
        SpeechStartedEvent(ts_audio_s=0.0, ts_emit=clock(), source="silero", confidence=0.9)
    )
    await orch.on_transcript(Transcript(is_final=True, text="what time is it", confidence=0.9))
    clock.advance_ms(800)
    await orch.on_speech_ended(
        SpeechEndedEvent(ts_audio_s=0.8, ts_emit=clock(), source="silero", corroborates=True)
    )
    clock.advance_ms(2_000)
    await sched.fire_last()

    assert actions.invocations == 1
    assert orch.state is not ConversationalState.USER_SPEAKING


@pytest.mark.asyncio
async def test_a_mid_thought_pause_with_real_text_is_left_alone() -> None:
    """A WAIT that carries text is a person pausing, not noise; no re-arm, no reset.

    Their next offset re-arms the timer exactly as before this change. Widening the
    escape to every WAIT would cut people off mid-sentence, which is the damaging
    direction the original design was protecting against.
    """
    clock, sched = _Clock(), _Scheduler()
    orch, actions = _build(clock, sched)

    await orch.on_speech_started(
        SpeechStartedEvent(ts_audio_s=0.0, ts_emit=clock(), source="silero", confidence=0.9)
    )
    await orch.on_transcript(
        Transcript(is_final=False, text="so I was thinking and", confidence=0.9)
    )
    clock.advance_ms(300)
    await orch.on_speech_ended(SpeechEndedEvent(ts_audio_s=0.3, ts_emit=clock(), source="silero"))
    armed_before = sched.live()
    clock.advance_ms(100)  # short: below the silence threshold, so the controller WAITs
    await sched.fire_last()

    assert orch.state is ConversationalState.USER_SPEAKING, "a real pause was reset"
    assert actions.invocations == 0
    assert sched.live() == armed_before - 1, "a WAIT with text must not re-arm the phantom timer"
