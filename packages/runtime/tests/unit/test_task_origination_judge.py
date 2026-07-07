"""Unit tests for the model-backed standing-intent judge's parsing (Spec A4, T5).

Deterministic — a stub backend returns canned JSON; these pin the judge's *conservative*
parsing (any doubt → AMBIGUOUS), not a real model's discrimination (that is the external
battery run in test_task_origination_no_accidental.py).
"""

from __future__ import annotations

from typing import Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.tools.categories import ActionCategory
from persona_runtime.task_origination import (
    ModelStandingIntentJudge,
    StandingVerdict,
)


class _StubBackend:
    """A chat backend that returns one canned response and counts calls."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls = 0

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return "claude-haiku-4-5-20251001"

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: object, **kwargs: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        self.calls += 1
        return ChatResponse(
            content=self._content,
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


async def _judge(content: str, message: str = "every morning, track fares") -> object:
    judge = ModelStandingIntentJudge(backend=_StubBackend(content))
    return await judge.judge(message, language="en")


@pytest.mark.asyncio
async def test_standing_json_yields_standing_with_draft() -> None:
    result = await _judge('{"verdict": "standing", "goal": "track the fares each morning"}')
    assert result.verdict is StandingVerdict.STANDING
    assert result.draft is not None
    assert result.draft.goal == "track the fares each morning"


@pytest.mark.asyncio
async def test_now_work_json() -> None:
    result = await _judge('{"verdict": "now_work"}')
    assert result.verdict is StandingVerdict.NOW_WORK
    assert result.draft is None


@pytest.mark.asyncio
async def test_ambiguous_json() -> None:
    result = await _judge('{"verdict": "ambiguous"}')
    assert result.verdict is StandingVerdict.AMBIGUOUS


@pytest.mark.asyncio
async def test_unparseable_output_is_conservative_ambiguous() -> None:
    result = await _judge("I think you might want a daily task?")
    assert result.verdict is StandingVerdict.AMBIGUOUS


@pytest.mark.asyncio
async def test_unknown_verdict_is_conservative_ambiguous() -> None:
    result = await _judge('{"verdict": "maybe_standing", "goal": "x"}')
    assert result.verdict is StandingVerdict.AMBIGUOUS


@pytest.mark.asyncio
async def test_standing_without_goal_degrades_to_ambiguous() -> None:
    # STANDING without an actionable goal must not be guessed into a task.
    result = await _judge('{"verdict": "standing", "goal": ""}')
    assert result.verdict is StandingVerdict.AMBIGUOUS


@pytest.mark.asyncio
async def test_json_in_markdown_fence_is_parsed() -> None:
    result = await _judge('```json\n{"verdict": "standing", "goal": "watch the portal"}\n```')
    assert result.verdict is StandingVerdict.STANDING
    assert result.draft is not None


@pytest.mark.asyncio
async def test_spend_cap_becomes_a_spend_grant() -> None:
    result = await _judge(
        '{"verdict": "standing", "goal": "book the trip", "spend_cap_kr": 1500, '
        '"spend_note": "I may book under 1500kr"}'
    )
    assert result.draft is not None
    spend = [g for g in result.draft.grants if g.category is ActionCategory.SPEND]
    assert len(spend) == 1
    assert spend[0].cap_micros == 15_000_000  # 1500 kr → micros
    assert spend[0].human == "I may book under 1500kr"


@pytest.mark.asyncio
async def test_zero_or_negative_spend_cap_is_ignored() -> None:
    result = await _judge('{"verdict": "standing", "goal": "g", "spend_cap_kr": 0}')
    assert result.draft is not None
    assert result.draft.grants == ()


# --- cadence extraction (Spec A4 schedule-attach) ----------------------------------------


@pytest.mark.asyncio
async def test_recurrence_rrule_becomes_a_recurring_schedule() -> None:
    result = await _judge(
        '{"verdict": "standing", "goal": "track fares", '
        '"recurrence_rrule": "FREQ=DAILY;BYHOUR=8;BYMINUTE=0"}'
    )
    assert result.draft is not None
    assert result.draft.schedule is not None
    assert result.draft.schedule.recurrence is not None  # a real recurring cadence
    assert result.draft.schedule.recurrence.byhour == (8,)
    assert result.draft.schedule.one_time_at is None


@pytest.mark.asyncio
async def test_one_time_at_becomes_a_one_time_schedule() -> None:
    result = await _judge(
        '{"verdict": "standing", "goal": "brief me", "one_time_at": "2099-01-02T09:00:00"}'
    )
    assert result.draft is not None
    assert result.draft.schedule is not None
    assert result.draft.schedule.one_time_at is not None  # a single future instant
    assert result.draft.schedule.recurrence is None


@pytest.mark.asyncio
async def test_no_cadence_falls_back_to_run_once_now_never_inert() -> None:
    # Option B: a standing task with no representable cadence still gets a one-time schedule so it
    # ACTUALLY runs (never an inert row) — the user can make it recurring by reply.
    result = await _judge('{"verdict": "standing", "goal": "watch the portal"}')
    assert result.draft is not None
    assert result.draft.schedule is not None
    assert result.draft.schedule.one_time_at is not None  # run-once, not None → not inert
    assert result.draft.schedule.recurrence is None


@pytest.mark.asyncio
async def test_unrepresentable_rrule_declines_to_run_once_not_coerced() -> None:
    # Parse-honesty: a bad RRULE is NOT coerced into an approximate cadence — it degrades to the
    # safe run-once fallback (a one-time schedule), never a wrong recurring rule.
    result = await _judge('{"verdict": "standing", "goal": "g", "recurrence_rrule": "NOT-A-RRULE"}')
    assert result.draft is not None
    assert result.draft.schedule is not None
    assert result.draft.schedule.recurrence is None  # never a coerced/approximate rule
    assert result.draft.schedule.one_time_at is not None


# --- R4 (BUG A): sub-daily cadences parse; unrepresentable ones decline HONESTLY ---------


@pytest.mark.asyncio
async def test_every_15_minutes_becomes_a_recurring_schedule_with_the_volume_line() -> None:
    """The transcript ask now parses: MINUTELY;15 → the pinned-DAILY grid, volume stated."""
    result = await _judge(
        '{"verdict": "standing", "goal": "check email inbox", '
        '"recurrence_rrule": "FREQ=MINUTELY;INTERVAL=15"}'
    )
    assert result.draft is not None
    assert result.draft.schedule is not None
    assert result.draft.schedule.recurrence is not None
    assert result.draft.schedule.cadence_note == ""
    assert "every 15 minutes, around the clock — 96 times a day" in (
        result.draft.schedule.human_terms
    )


@pytest.mark.asyncio
async def test_unrepresentable_cadence_fallback_carries_the_honest_note() -> None:
    """NEVER a silent once-fallback (the R4 transcript): the run-once fallback names what
    could not be set and the nearest cadences that CAN be held."""
    result = await _judge(
        '{"verdict": "standing", "goal": "g", "recurrence_rrule": "FREQ=MINUTELY;INTERVAL=45"}'
    )
    assert result.draft is not None
    assert result.draft.schedule is not None
    assert result.draft.schedule.recurrence is None  # still the safe run-once
    assert result.draft.schedule.one_time_at is not None
    note = result.draft.schedule.cadence_note
    assert "couldn't set that exact cadence" in note
    assert "every 5, 10, 15, 20, 30 or 60 minutes" in note
