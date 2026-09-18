"""Unit tests for the model-backed amendment interpreter's clause patching (Spec A4, T9).

Deterministic — a stub backend returns canned JSON; these pin the clause-patch application:
present clauses are applied onto the draft, unspecified clauses are preserved, a non-amendment
(or a no-op patch) yields ``None``, and an unrepresentable schedule tweak is skipped not coerced.
"""

from __future__ import annotations

from typing import Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.schedules import RecurrenceRule
from persona.tasks import UpdateGranularity
from persona.tools.categories import ActionCategory
from persona_runtime.task_origination import (
    ContractDraft,
    ModelAmendmentInterpreter,
    ParsedSchedule,
)

_TZ = "Europe/Oslo"
_SCHED = ParsedSchedule(
    recurrence=RecurrenceRule.from_rrule_string("FREQ=DAILY;BYHOUR=7;BYMINUTE=0"),
    timezone=_TZ,
    human_terms="every day at 07:00",
)
_DRAFT = ContractDraft(goal="track fares", scope="Oslo→Bergen", schedule=_SCHED)


class _StubBackend:
    def __init__(self, content: str) -> None:
        self._content = content

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
        return ChatResponse(
            content=self._content,
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


async def _amend(content: str, draft: ContractDraft = _DRAFT) -> ContractDraft | None:
    interp = ModelAmendmentInterpreter(backend=_StubBackend(content))
    return await interp.interpret("make it 8am", draft)


@pytest.mark.asyncio
async def test_not_an_amendment_yields_none() -> None:
    assert await _amend('{"amends": false}') is None


@pytest.mark.asyncio
async def test_goal_change_applies_and_preserves_other_clauses() -> None:
    amended = await _amend('{"amends": true, "goal": "track fares AND hotels"}')
    assert amended is not None
    assert amended.goal == "track fares AND hotels"
    assert amended.scope == _DRAFT.scope  # unspecified clause preserved
    assert amended.schedule == _DRAFT.schedule


@pytest.mark.asyncio
async def test_spend_cap_becomes_a_spend_grant() -> None:
    amended = await _amend('{"amends": true, "spend_cap_usd": 500}')
    assert amended is not None
    spend = [g for g in amended.grants if g.category is ActionCategory.SPEND]
    assert len(spend) == 1
    assert spend[0].cap_micros == 5_000_000  # $500 → micros (MICROS_PER_DOLLAR)


@pytest.mark.asyncio
async def test_clear_spend_removes_the_grant() -> None:
    from persona_runtime.task_origination import GrantSpec

    with_spend = _DRAFT.model_copy(
        update={"grants": (GrantSpec(category=ActionCategory.SPEND, cap_micros=9_000_000),)}
    )
    amended = await _amend('{"amends": true, "clear_spend": true}', with_spend)
    assert amended is not None
    assert amended.grants == ()


@pytest.mark.asyncio
async def test_schedule_tweak_is_reparsed_through_the_parse_boundary() -> None:
    amended = await _amend('{"amends": true, "schedule_rrule": "FREQ=DAILY;BYHOUR=8;BYMINUTE=0"}')
    assert amended is not None
    assert amended.schedule is not None
    assert amended.schedule.recurrence is not None
    assert amended.schedule.recurrence.byhour == (8,)


@pytest.mark.asyncio
async def test_unrepresentable_schedule_tweak_is_skipped_not_coerced() -> None:
    # A bad RRULE must not corrupt the draft; the schedule clause is skipped, and since nothing
    # else changed the patch is a no-op → None (the loop does not re-echo an identical draft).
    amended = await _amend('{"amends": true, "schedule_rrule": "not-a-real-rrule"}')
    assert amended is None


@pytest.mark.asyncio
async def test_updates_granularity_change_applies() -> None:
    amended = await _amend('{"amends": true, "updates_granularity": "quiet"}')
    assert amended is not None
    assert amended.updates.granularity is UpdateGranularity.QUIET


@pytest.mark.asyncio
async def test_amends_true_but_empty_patch_is_a_noop_none() -> None:
    # Claiming to amend while changing nothing representable is not an actionable amendment.
    assert await _amend('{"amends": true}') is None


# --- one-time retiming (R4 rail fix, B-3): "once at 9 30" is now expressible -------------


@pytest.mark.asyncio
async def test_one_time_retiming_applies_via_parse_one_time() -> None:
    """THE transcript amendment: a one-time patch lands as the draft-tz instant."""
    from zoneinfo import ZoneInfo

    amended = await _amend('{"amends": true, "one_time_at": "2026-07-07T09:30:00"}')
    assert amended is not None
    assert amended.schedule is not None
    assert amended.schedule.recurrence is None
    assert amended.schedule.one_time_at is not None
    local = amended.schedule.one_time_at.astimezone(ZoneInfo(_TZ))
    assert (local.hour, local.minute) == (9, 30)
    assert "09:30" in amended.schedule.human_terms


@pytest.mark.asyncio
async def test_one_time_bad_iso_is_skipped_not_coerced() -> None:
    # An unparseable instant must not corrupt the draft; nothing else changed → None.
    assert await _amend('{"amends": true, "one_time_at": "half past nine"}') is None


@pytest.mark.asyncio
async def test_prompt_is_now_anchored_when_a_schedule_frame_exists() -> None:
    """A bare-time reply ("once at 9 30") only resolves if the model is told NOW —
    the interpreter must send the current local time + timezone with the proposal."""

    class _CapturingBackend(_StubBackend):
        def __init__(self) -> None:
            super().__init__('{"amends": false}')
            self.seen: str = ""

        async def chat(self, messages: object, **kwargs: Any) -> ChatResponse:  # noqa: ANN401
            assert isinstance(messages, list)
            self.seen = str(messages[-1].content)
            return await super().chat(messages, **kwargs)

    backend = _CapturingBackend()
    interp = ModelAmendmentInterpreter(backend=backend)
    await interp.interpret("once at 9 30", _DRAFT)
    assert "current local time:" in backend.seen
    assert _TZ in backend.seen


# --- acceptance criteria (completion sweep, part 2, finding C) --------------


@pytest.mark.asyncio
async def test_an_amendment_can_rewrite_the_done_when_list() -> None:
    """The criteria are part of what the user confirms, so a reply like "and make sure it
    names the airline" has to land on the draft the same way a new cap does."""
    with_criteria = _DRAFT.model_copy(
        update={"acceptance_criteria": ("a fare under 500 USD is reported",)}
    )
    amended = await _amend(
        '{"amends": true, "acceptance_criteria": '
        '["a fare under 500 USD is reported", "the report names the airline"]}',
        with_criteria,
    )

    assert amended is not None
    assert amended.acceptance_criteria == (
        "a fare under 500 USD is reported",
        "the report names the airline",
    )
    assert amended.goal == _DRAFT.goal  # unspecified clauses preserved


@pytest.mark.asyncio
async def test_a_criteria_patch_in_the_wrong_shape_is_skipped_not_coerced() -> None:
    assert await _amend('{"amends": true, "acceptance_criteria": "one string"}') is None


@pytest.mark.asyncio
async def test_the_interpreter_shows_the_model_the_current_criteria() -> None:
    """A patch replaces the whole list, so the model must see the list it is editing."""

    class _CapturingBackend(_StubBackend):
        def __init__(self) -> None:
            super().__init__('{"amends": false}')
            self.seen: str = ""

        async def chat(self, messages: object, **kwargs: Any) -> ChatResponse:  # noqa: ANN401
            assert isinstance(messages, list)
            self.seen = str(messages[-1].content)
            return await super().chat(messages, **kwargs)

    backend = _CapturingBackend()
    draft = _DRAFT.model_copy(update={"acceptance_criteria": ("the airline is named",)})
    await ModelAmendmentInterpreter(backend=backend).interpret("also the date", draft)

    assert "the airline is named" in backend.seen


# --- deadline + leg cap (completion sweep, part 2, finding O) ---------------


@pytest.mark.asyncio
async def test_an_amendment_can_set_and_clear_the_leg_cap() -> None:
    amended = await _amend('{"amends": true, "max_legs": 5}')
    assert amended is not None
    assert amended.max_legs == 5

    capped = _DRAFT.model_copy(update={"max_legs": 5})
    cleared = await _amend('{"amends": true, "clear_max_legs": true}', capped)
    assert cleared is not None
    assert cleared.max_legs is None


@pytest.mark.asyncio
async def test_an_amendment_resolves_a_deadline_in_the_drafts_timezone() -> None:
    from zoneinfo import ZoneInfo

    amended = await _amend('{"amends": true, "deadline": "2099-09-19T17:00:00"}')
    assert amended is not None
    assert amended.deadline is not None
    assert amended.deadline.astimezone(ZoneInfo(_TZ)).hour == 17

    cleared = await _amend('{"amends": true, "clear_deadline": true}', amended)
    assert cleared is not None
    assert cleared.deadline is None


@pytest.mark.asyncio
async def test_a_deadline_that_has_passed_or_cannot_be_read_is_skipped() -> None:
    assert await _amend('{"amends": true, "deadline": "2020-01-01T09:00:00"}') is None
    assert await _amend('{"amends": true, "deadline": "Friday-ish"}') is None
    assert await _amend('{"amends": true, "max_legs": 0}') is None
