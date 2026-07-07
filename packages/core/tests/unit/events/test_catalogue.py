"""Unit tests for the closed v1 event catalogue (Spec A7, A7-D-1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.events import (
    CATALOGUE_VERSION,
    ConnectorLinked,
    ConnectorMessageReceived,
    ConnectorUnlinked,
    Event,
    EventKind,
    TaskLegCompleted,
    TaskLegFailed,
    TaskMilestone,
    event_kind_of,
)
from pydantic import TypeAdapter, ValidationError

_WHEN = datetime(2026, 6, 25, 7, 0, tzinfo=UTC)


def _message(**overrides: object) -> ConnectorMessageReceived:
    base: dict[str, object] = {
        "event_id": "evt-1",
        "owner_id": "owner-1",
        "occurred_at": _WHEN,
        "platform": "email",
        "sender_id": "landlord@example.com",
        "body": "the rent is due",
        "conversation_id": "conv-1",
        "persona_id": "persona-1",
        "message_id": "msg-1",
    }
    base.update(overrides)
    return ConnectorMessageReceived(**base)  # type: ignore[arg-type]


def test_catalogue_is_the_closed_six_member_set() -> None:
    assert {k.value for k in EventKind} == {
        "connector.message_received",
        "task.leg_completed",
        "task.leg_failed",
        "task.milestone",
        "connector.linked",
        "connector.unlinked",
    }
    assert CATALOGUE_VERSION == "v1"


def test_message_event_carries_the_inbound_metadata() -> None:
    event = _message(thread_id="thr-1", subject="Rent")
    assert event.kind == EventKind.CONNECTOR_MESSAGE_RECEIVED
    assert event.platform == "email"
    assert event.thread_id == "thr-1"
    assert event.subject == "Rent"
    assert event.causal_chain == ()  # organic event — empty chain


def test_lifecycle_failed_requires_a_failure_count() -> None:
    failed = TaskLegFailed(
        event_id="e",
        owner_id="o",
        occurred_at=_WHEN,
        task_id="task-1",
        persona_id="p",
        failure_count=2,
    )
    assert failed.failure_count == 2
    with pytest.raises(ValidationError):  # failure_count >= 1
        TaskLegFailed(
            event_id="e",
            owner_id="o",
            occurred_at=_WHEN,
            task_id="t",
            persona_id="p",
            failure_count=0,
        )


def test_events_are_frozen_and_forbid_extra() -> None:
    event = _message()
    with pytest.raises(ValidationError):
        event.body = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _message(unexpected="x")


def test_occurred_at_must_be_tz_aware() -> None:
    with pytest.raises(ValidationError):
        _message(occurred_at=datetime(2026, 6, 25, 7, 0))  # noqa: DTZ001


def test_causal_chain_is_carried_when_supplied() -> None:
    # An event emitted BY a triggered action inherits the chain (the cross-process loop marker).
    event = _message(causal_chain=("trg-1", "trg-2"))
    assert event.causal_chain == ("trg-1", "trg-2")


def test_event_is_a_discriminated_union_over_kind() -> None:
    adapter: TypeAdapter[Event] = TypeAdapter(Event)
    parsed = adapter.validate_python(
        {
            "kind": "task.leg_completed",
            "event_id": "e",
            "owner_id": "o",
            "occurred_at": _WHEN.isoformat(),
            "task_id": "t",
            "persona_id": "p",
        }
    )
    assert isinstance(parsed, TaskLegCompleted)
    assert event_kind_of(parsed) is EventKind.TASK_LEG_COMPLETED


@pytest.mark.parametrize(
    ("event", "kind"),
    [
        (
            TaskMilestone(
                event_id="e", owner_id="o", occurred_at=_WHEN, task_id="t", persona_id="p"
            ),
            EventKind.TASK_MILESTONE,
        ),
        (
            ConnectorLinked(event_id="e", owner_id="o", occurred_at=_WHEN, platform="slack"),
            EventKind.CONNECTOR_LINKED,
        ),
        (
            ConnectorUnlinked(event_id="e", owner_id="o", occurred_at=_WHEN, platform="slack"),
            EventKind.CONNECTOR_UNLINKED,
        ),
    ],
)
def test_event_kind_of_returns_the_discriminator(event: Event, kind: EventKind) -> None:
    assert event_kind_of(event) is kind
