"""A8 T6 — the conversational reschedule verb: honest resolution + full re-echo (bars 1, 2, 5).

Deterministic: a stub backend returns canned JSON for the interpreter (a clean match resolves; an
ambiguous one lists candidates; a hallucinated/absent id or empty list never reschedule). The
re-echo assembly re-states the FULL new clause in the user's tz + a next-run preview, with a
quiet-hours warn-plus-offer when the new time lands in the window.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.schedules import QuietHours
from persona.tasks import IntrospectionStatus, TaskSummary
from persona_runtime.task_origination import (
    ModelRescheduleInterpreter,
    RescheduleIntent,
    RescheduleResolutionKind,
    render_proposal_echo,
)
from persona_runtime.task_origination.reschedule_flow import assemble_reschedule_echo

_ACTIVE = (
    TaskSummary(task_id="task-brief", goal="morning brief", status=IntrospectionStatus.SCHEDULED),
    TaskSummary(task_id="task-fare", goal="track fares", status=IntrospectionStatus.PROGRESSING),
)
_NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


class _StubBackend:
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


async def _interpret(content: str, message: str = "move the morning brief to 9") -> object:
    return await ModelRescheduleInterpreter(backend=_StubBackend(content)).interpret(
        message, _ACTIVE
    )


# --- bar 1: honest target resolution -------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_match_resolves_target_and_cadence() -> None:
    res = await _interpret(
        '{"match":"one","task_id":"task-brief","recurrence_rrule":"FREQ=DAILY;BYHOUR=9;BYMINUTE=0"}'
    )
    assert res.kind is RescheduleResolutionKind.RESOLVED  # type: ignore[attr-defined]
    assert res.intent.task_id == "task-brief"  # type: ignore[union-attr]
    assert res.intent.recurrence_rrule == "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_ambiguous_lists_candidates_and_asks() -> None:
    res = await _interpret('{"match":"ambiguous","candidate_ids":["task-brief","task-fare"]}')
    assert res.kind is RescheduleResolutionKind.AMBIGUOUS  # type: ignore[attr-defined]
    assert set(res.candidate_goals) == {"morning brief", "track fares"}  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_hallucinated_id_never_reschedules() -> None:
    res = await _interpret('{"match":"one","task_id":"task-ghost","recurrence_rrule":"FREQ=DAILY"}')
    assert res.kind is RescheduleResolutionKind.NOT_FOUND  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_not_a_reschedule_is_not_found() -> None:
    assert (await _interpret('{"match":"none"}')).kind is RescheduleResolutionKind.NOT_FOUND  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_empty_active_list_short_circuits_without_a_model_call() -> None:
    backend = _StubBackend('{"match":"one","task_id":"x"}')
    res = await ModelRescheduleInterpreter(backend=backend).interpret("move it", ())
    assert res.kind is RescheduleResolutionKind.NOT_FOUND
    assert backend.calls == 0  # no active tasks → never asks the model


@pytest.mark.asyncio
async def test_skip_next_intent() -> None:
    res = await _interpret('{"match":"one","task_id":"task-brief","skip_next":true}')
    assert res.intent.skip_next is True  # type: ignore[union-attr]


# --- bars 2 + 5: the full re-echo in the user's tz + quiet-hours warn -----------------------


def test_reecho_restates_full_clause_in_user_tz() -> None:
    intent = RescheduleIntent(
        task_id="task-brief", recurrence_rrule="FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0"
    )
    echo = assemble_reschedule_echo(
        intent, task_goal="morning brief", timezone="Europe/Oslo", quiet_hours=None, now=_NOW
    )
    # The WHOLE new clause (not a diff fragment) + the tz + a next-run preview + a confirm invite.
    assert "morning brief" in echo.echo_text
    assert "every Monday at 09:00" in echo.echo_text
    assert "Europe/Oslo" in echo.echo_text
    assert "next run" in echo.echo_text
    assert echo.echo_text.rstrip().endswith("Apply that?")
    assert "FREQ=" not in echo.echo_text  # no raw RRULE, ever


def test_reecho_warns_and_offers_edge_when_inside_quiet_hours() -> None:
    # New time 06:00 Oslo, quiet hours 22:00–07:00 → warn + offer the 07:00 edge.
    intent = RescheduleIntent(
        task_id="task-brief", recurrence_rrule="FREQ=DAILY;BYHOUR=6;BYMINUTE=0"
    )
    quiet = QuietHours(start_minute=22 * 60, end_minute=7 * 60)
    echo = assemble_reschedule_echo(
        intent, task_goal="morning brief", timezone="Europe/Oslo", quiet_hours=quiet, now=_NOW
    )
    assert "quiet hours" in echo.echo_text
    assert "07:00" in echo.echo_text  # the offered nearest edge


def test_reecho_no_warn_when_quiet_hours_unset() -> None:
    intent = RescheduleIntent(
        task_id="task-brief", recurrence_rrule="FREQ=DAILY;BYHOUR=6;BYMINUTE=0"
    )
    echo = assemble_reschedule_echo(
        intent, task_goal="morning brief", timezone="Europe/Oslo", quiet_hours=None, now=_NOW
    )
    assert "quiet hours" not in echo.echo_text  # off-until-set → no warn


# --- T7 bar 4: the persona-proposed copy invites only the wired capability ------------------


def test_proposal_echo_states_reason_and_proposed_cadence_and_asks() -> None:
    echo = render_proposal_echo(
        task_goal="morning brief",
        reason="The 08:00 run keeps hitting your quiet hours.",
        human_terms="every day at 09:00 your time",
        timezone="Europe/Oslo",
    )
    assert "quiet hours" in echo  # the reason
    assert "morning brief" in echo  # the target
    assert "every day at 09:00" in echo  # the proposed cadence, human terms
    assert "Europe/Oslo" in echo
    assert echo.rstrip().endswith("?")  # a plain yes/no invite (the only wired action)
    assert "FREQ=" not in echo  # no raw RRULE
