"""A7 T7 — the trigger creation door: draft XOR, echo clause, amendment, emission (Spec A7)."""

from __future__ import annotations

from persona.events import EventKind, MessageFilter, TriggerSpec
from persona.schedules import RecurrenceFreq, RecurrenceRule
from persona_runtime.task_origination import ContractDraft, ParsedSchedule, render_echo
from persona_runtime.task_origination.amendment import changed_clauses
from persona_runtime.task_origination.echo import Clause
from persona_runtime.task_origination.emission import build_task_originated_event
from pydantic import ValidationError

_TRIGGER = TriggerSpec(
    event_kind=EventKind.CONNECTOR_MESSAGE_RECEIVED,
    filter=MessageFilter(platform="email", sender="landlord@example.com"),
    human_terms="an email from landlord@example.com arrives",
)
_SCHEDULE = ParsedSchedule(
    recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY),
    one_time_at=None,
    timezone="Europe/Oslo",
    human_terms="every day at 07:00",
)


def test_schedule_and_trigger_are_mutually_exclusive() -> None:
    # A7-D-3: one impulse per contract — a draft cannot be both time-driven and event-driven.
    import pytest

    with pytest.raises(ValidationError):
        ContractDraft(goal="do it", schedule=_SCHEDULE, trigger=_TRIGGER)
    # Either alone is fine.
    assert ContractDraft(goal="do it", trigger=_TRIGGER).trigger is _TRIGGER
    assert ContractDraft(goal="do it", schedule=_SCHEDULE).schedule is _SCHEDULE


def test_echo_renders_the_trigger_clause_instead_of_a_schedule() -> None:
    echo = render_echo(ContractDraft(goal="summarise it", trigger=_TRIGGER))
    assert "When: whenever an email from landlord@example.com arrives" in echo
    # The schedule "When:" line is NOT present — the trigger replaced it (one "When:").
    assert echo.count("When:") == 1
    assert "no recurring schedule" not in echo


def test_changing_the_trigger_is_a_material_amendment() -> None:
    before = ContractDraft(goal="summarise it", trigger=_TRIGGER)
    after = before.model_copy(
        update={
            "trigger": _TRIGGER.model_copy(
                update={
                    "filter": MessageFilter(platform="email", sender="agent@landlord.com"),
                    "human_terms": "an email from agent@landlord.com arrives",
                }
            )
        }
    )
    assert Clause.TRIGGER in changed_clauses(before, after)


def test_emission_carries_the_trigger_payload_and_no_schedule() -> None:
    event = build_task_originated_event(
        draft=ContractDraft(goal="summarise it", trigger=_TRIGGER),
        owner_id="own-1",
        persona_id="pers-1",
        persona_name="Astrid",
        conversation_id="conv-1",
        assistant_message_id="msg-1",
    )
    assert event.data["trigger"]["event_kind"] == "connector.message_received"
    assert event.data["trigger"]["human_terms"] == "an email from landlord@example.com arrives"
    assert event.data["schedule"] == {}  # schedule XOR trigger
