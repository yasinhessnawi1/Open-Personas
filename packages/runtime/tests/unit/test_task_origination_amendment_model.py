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
    amended = await _amend('{"amends": true, "spend_cap_kr": 500}')
    assert amended is not None
    spend = [g for g in amended.grants if g.category is ActionCategory.SPEND]
    assert len(spend) == 1
    assert spend[0].cap_micros == 5_000_000  # 500 kr → micros


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
