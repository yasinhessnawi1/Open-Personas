"""A turn is everything the person said, and a pause inside it must not lose it (R9-126/127).

Both came out of one call, read from the per-turn log. The caller said "I'm doing good,
my problem is that the schedule I keep" then "Telling you to schedule every day you
never did"; the model received 49 characters, the second half. Later "You keep telling
me that you will schedule but never do" sat unanswered until "Hm?" and "Yes." arrived,
and then only "Yes." was answered. Two mechanisms: the orchestrator kept only the latest
STT segment, and a WAIT on real text never re-armed the turn-end timer.

These drive the real orchestrator through its real triggers (onsets, offsets, partials,
finals) and assert what the model is handed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from persona_voice.loop.streaming import Transcript
from persona_voice.stt.types import SpeechEndedEvent, SpeechStartedEvent
from persona_voice.turn_taking import orchestrator as orch_mod
from persona_voice.turn_taking.orchestrator import ConversationalOrchestrator
from persona_voice.turn_taking.states import ConversationalState

_BASE = datetime(2026, 9, 5, 16, 30, 0, tzinfo=UTC)
_FIRST = "I'm doing good, my problem is that the schedule I keep"
_SECOND = "Telling you to schedule every day you never did."


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
        self.heard: list[str] = []

    async def invoke_model_for_turn(self, transcript: Transcript) -> None:
        self.heard.append(transcript.text)

    async def cancel_generation(self) -> None: ...

    async def interrupt(self) -> None: ...


def _build() -> tuple[ConversationalOrchestrator, _Actions, _Clock, _Scheduler]:
    clock, sched, actions = _Clock(), _Scheduler(), _Actions()
    orch = ConversationalOrchestrator(
        actions=actions,  # type: ignore[arg-type]
        scheduler=sched,  # type: ignore[arg-type]
        clock=clock,
    )
    return orch, actions, clock, sched


def _onset(clock: _Clock) -> SpeechStartedEvent:
    return SpeechStartedEvent(ts_audio_s=0.0, ts_emit=clock(), source="silero", confidence=0.9)


def _offset(clock: _Clock) -> SpeechEndedEvent:
    return SpeechEndedEvent(ts_audio_s=1.0, ts_emit=clock(), source="silero")


def _partial(text: str) -> Transcript:
    return Transcript(is_final=False, text=text, confidence=0.5)


def _final(text: str) -> Transcript:
    return Transcript(is_final=True, text=text, confidence=0.9)


async def _fire_after_threshold(clock: _Clock, sched: _Scheduler) -> None:
    clock.advance_ms(2_000)  # past every silence threshold
    assert await sched.fire_last(), "no turn-end timer was armed"


@pytest.mark.asyncio
async def test_a_turn_split_by_a_pause_reaches_the_model_whole() -> None:
    """THE regression: two STT finals inside one turn, the model must get both."""
    orch, actions, clock, sched = _build()
    await orch.on_speech_started(_onset(clock))
    await orch.on_transcript(_partial("I'm doing good"))
    await orch.on_transcript(_final(_FIRST))
    clock.advance_ms(300)
    await orch.on_speech_ended(_offset(clock))  # the mid-sentence pause arms the timer
    clock.advance_ms(200)
    await orch.on_speech_started(_onset(clock))  # ...and the caller resumes
    await orch.on_transcript(_partial("Telling you"))
    await orch.on_transcript(_final(_SECOND))
    clock.advance_ms(300)
    await orch.on_speech_ended(_offset(clock))
    await _fire_after_threshold(clock, sched)

    assert actions.heard == [f"{_FIRST} {_SECOND}"]
    assert orch.state is ConversationalState.PROCESSING


@pytest.mark.asyncio
async def test_a_partial_beyond_the_last_final_is_kept_until_its_final_lands() -> None:
    """The newest partial rides on the end; its own final replaces it, not duplicates it."""
    orch, actions, clock, sched = _build()
    await orch.on_speech_started(_onset(clock))
    await orch.on_transcript(_final(_FIRST))
    await orch.on_transcript(_partial("Telling you now"))
    clock.advance_ms(300)
    await orch.on_speech_ended(_offset(clock))
    await _fire_after_threshold(clock, sched)
    assert actions.heard == [f"{_FIRST} Telling you now"]

    orch2, actions2, clock2, sched2 = _build()
    await orch2.on_speech_started(_onset(clock2))
    await orch2.on_transcript(_partial(_SECOND))
    await orch2.on_transcript(_final(_SECOND))
    clock2.advance_ms(300)
    await orch2.on_speech_ended(_offset(clock2))
    await _fire_after_threshold(clock2, sched2)
    assert actions2.heard == [_SECOND], "a final must replace its partial, not append to it"


@pytest.mark.asyncio
async def test_a_trailing_hold_token_re_arms_then_answers_instead_of_wedging() -> None:
    """ "...schedule but" is a mid-thought hold; it must not hold the floor forever."""
    orch, actions, clock, sched = _build()
    await orch.on_speech_started(_onset(clock))
    await orch.on_transcript(_partial("You keep telling me that you will schedule but"))
    clock.advance_ms(300)
    await orch.on_speech_ended(_offset(clock))

    fired = 0
    while sched.live() and fired < 20:
        await _fire_after_threshold(clock, sched)
        fired += 1

    assert fired == orch_mod.HOLD_TURN_REARM_LIMIT + 1, "the hold must be bounded, not a spin"
    assert actions.heard == ["You keep telling me that you will schedule but"]
    assert orch.state is ConversationalState.PROCESSING


@pytest.mark.asyncio
async def test_a_final_landing_during_the_hold_ends_the_turn_on_the_next_check() -> None:
    """The re-arm exists so a late final can resolve the pause: it must, and only once."""
    orch, actions, clock, sched = _build()
    await orch.on_speech_started(_onset(clock))
    await orch.on_transcript(_partial("You keep telling me that you will schedule but"))
    clock.advance_ms(300)
    await orch.on_speech_ended(_offset(clock))
    await _fire_after_threshold(clock, sched)  # WAIT: hold token, re-armed
    await orch.on_transcript(_final("You keep telling me that you will schedule but never do."))
    await _fire_after_threshold(clock, sched)

    assert actions.heard == ["You keep telling me that you will schedule but never do."]
    assert sched.live() == 0


@pytest.mark.asyncio
async def test_resuming_speech_resets_the_hold_budget() -> None:
    orch, actions, clock, sched = _build()
    await orch.on_speech_started(_onset(clock))
    await orch.on_transcript(_partial("so I was thinking and"))
    clock.advance_ms(300)
    await orch.on_speech_ended(_offset(clock))
    await _fire_after_threshold(clock, sched)
    await _fire_after_threshold(clock, sched)  # two of the three re-arms spent
    clock.advance_ms(100)
    await orch.on_speech_started(_onset(clock))  # the caller resumes
    await orch.on_transcript(_final("so I was thinking and then I stopped."))
    clock.advance_ms(300)
    await orch.on_speech_ended(_offset(clock))
    await _fire_after_threshold(clock, sched)

    assert actions.heard == ["so I was thinking and then I stopped."]


@pytest.mark.asyncio
async def test_a_noise_blip_still_returns_the_floor_without_answering() -> None:
    """R9-119's escape is untouched by the hold re-arm: no text, no invocation."""
    orch, actions, clock, sched = _build()
    await orch.on_speech_started(_onset(clock))
    clock.advance_ms(200)
    await orch.on_speech_ended(_offset(clock))
    fired = 0
    while sched.live() and fired < 20:
        await _fire_after_threshold(clock, sched)
        fired += 1
    assert actions.heard == []
    assert orch.state is ConversationalState.LISTENING
