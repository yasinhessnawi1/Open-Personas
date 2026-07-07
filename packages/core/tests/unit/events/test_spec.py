"""Unit tests for TriggerSpec — the "what to watch" a contract carries (Spec A7, T7)."""

from __future__ import annotations

import pytest
from persona.events import (
    EventKind,
    LifecycleFilter,
    LinkFilter,
    MessageFilter,
    TriggerSpec,
)
from pydantic import ValidationError


def test_message_kind_accepts_a_message_filter() -> None:
    spec = TriggerSpec(
        event_kind=EventKind.CONNECTOR_MESSAGE_RECEIVED,
        filter=MessageFilter(platform="email", sender="x@y.com"),
        human_terms="an email from x@y.com arrives",
    )
    assert spec.event_kind is EventKind.CONNECTOR_MESSAGE_RECEIVED
    assert spec.human_terms.startswith("an email")


def test_lifecycle_kind_accepts_a_lifecycle_filter() -> None:
    spec = TriggerSpec(
        event_kind=EventKind.TASK_LEG_FAILED,
        filter=LifecycleFilter(task_id="task-1", min_failure_count=2),
        human_terms="the task fails twice",
    )
    assert spec.event_kind is EventKind.TASK_LEG_FAILED


def test_mismatched_filter_is_rejected() -> None:
    # A message kind with a link filter is incoherent — fail fast at the boundary (A7-D-2).
    with pytest.raises(ValidationError):
        TriggerSpec(
            event_kind=EventKind.CONNECTOR_MESSAGE_RECEIVED,
            filter=LinkFilter(platform="email"),
            human_terms="nonsense",
        )
    with pytest.raises(ValidationError):
        TriggerSpec(
            event_kind=EventKind.TASK_LEG_COMPLETED,
            filter=MessageFilter(platform="email"),
            human_terms="nonsense",
        )


def test_human_terms_required_non_empty() -> None:
    with pytest.raises(ValidationError):
        TriggerSpec(
            event_kind=EventKind.CONNECTOR_UNLINKED,
            filter=LinkFilter(platform="slack"),
            human_terms="",
        )
