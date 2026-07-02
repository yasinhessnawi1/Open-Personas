"""Unit tests for the task_originated event emission + content hash (Spec A4, T6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.schedules import RecurrenceFreq, RecurrenceRule
from persona.tools.categories import ActionCategory
from persona_runtime.task_origination import (
    ContractDraft,
    GrantSpec,
    ParsedSchedule,
    build_task_originated_event,
    draft_content_hash,
)

if TYPE_CHECKING:
    from persona_runtime.agentic.events import RunEvent


def _draft(grants: tuple[GrantSpec, ...] = ()) -> ContractDraft:
    return ContractDraft(
        goal="track the Oslo→Bergen fares every morning",
        scope="under 2000kr",
        schedule=ParsedSchedule(
            recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(7,)),
            timezone="Europe/Oslo",
            human_terms="every day at 07:00 your time",
        ),
        grants=grants,
    )


def _event(draft: ContractDraft, *, assistant_message_id: str = "msg-1") -> RunEvent:
    return build_task_originated_event(
        draft=draft,
        owner_id="user-1",
        persona_id="astrid",
        persona_name="Astrid",
        conversation_id="conv-1",
        assistant_message_id=assistant_message_id,
    )


def test_content_hash_is_stable_across_grant_order() -> None:
    spend = GrantSpec(category=ActionCategory.SPEND, cap_micros=1_000_000)
    mutate = GrantSpec(category=ActionCategory.EXTERNAL_MUTATE)
    a = _draft(grants=(spend, mutate))
    b = _draft(grants=(mutate, spend))
    # Canonicalisation makes the hash order-independent (the T6 dedup key needs this).
    assert draft_content_hash(a) == draft_content_hash(b)


def test_content_hash_changes_with_content() -> None:
    a = _draft()
    b = a.model_copy(update={"goal": "a different goal"})
    assert draft_content_hash(a) != draft_content_hash(b)


def test_event_carries_the_dedup_anchors_and_contract() -> None:
    event = _event(_draft())
    assert event.type == "task_originated"
    assert event.data["assistant_message_id"] == "msg-1"
    assert event.data["draft_hash"] == draft_content_hash(_draft())
    assert event.data["owner_id"] == "user-1"
    assert event.data["persona_id"] == "astrid"
    assert event.data["contract"]["goal"] == "track the Oslo→Bergen fares every morning"


def test_event_schedule_payload_is_json_safe() -> None:
    event = _event(_draft())
    schedule = event.data["schedule"]
    assert schedule["timezone"] == "Europe/Oslo"
    assert schedule["recurrence"]["freq"] == "DAILY"


def test_event_carries_spend_grant_in_contract_matrix() -> None:
    grant = GrantSpec(
        category=ActionCategory.SPEND, cap_micros=15_000_000, human="may book under 1500kr"
    )
    event = _event(_draft(grants=(grant,)))
    contract = event.data["contract"]
    # The spend cap landed on the contract bounds; the policy carries the spend override.
    assert contract["bounds"]["total_budget_micros"] == 15_000_000
    overrides = contract["category_policy"]["overrides"]
    assert any(o["category"] == "spend" and o["decision"] == "allow" for o in overrides)


def test_event_without_schedule_carries_empty_schedule() -> None:
    event = _event(ContractDraft(goal="one-off-ish standing task"))
    assert event.data["schedule"] == {}


def test_event_is_json_serialisable() -> None:
    # The whole point of the data-only event: it crosses runtime→api as JSON.
    event = _event(_draft())
    dumped = event.model_dump_json()
    assert "task_originated" in dumped
