"""Unit tests for typed per-kind trigger filters (Spec A7, A7-D-2)."""

from __future__ import annotations

from datetime import UTC, datetime

from persona.events import (
    ConnectorLinked,
    ConnectorMessageReceived,
    ConnectorUnlinked,
    LifecycleFilter,
    LinkFilter,
    MessageFilter,
    TaskLegCompleted,
    TaskLegFailed,
    TaskMilestone,
)

_WHEN = datetime(2026, 6, 25, 7, 0, tzinfo=UTC)


def _message(**overrides: object) -> ConnectorMessageReceived:
    base: dict[str, object] = {
        "event_id": "e",
        "owner_id": "o",
        "occurred_at": _WHEN,
        "platform": "email",
        "sender_id": "Landlord@Example.com",
        "subject": "Rent due",
        "body": "Please pay the INVOICE by Friday",
        "conversation_id": "c",
        "persona_id": "p",
        "message_id": "m",
    }
    base.update(overrides)
    return ConnectorMessageReceived(**base)  # type: ignore[arg-type]


def test_empty_message_filter_matches_any_message() -> None:
    assert MessageFilter().matches(_message()) is True


def test_message_filter_platform_and_thread_are_exact() -> None:
    assert MessageFilter(platform="email").matches(_message()) is True
    assert MessageFilter(platform="slack").matches(_message()) is False
    assert MessageFilter(thread_id="thr-1").matches(_message(thread_id="thr-1")) is True
    assert MessageFilter(thread_id="thr-1").matches(_message(thread_id="other")) is False


def test_message_filter_sender_is_case_insensitive() -> None:
    # The producer normalises; the filter is defensively case-insensitive on top.
    assert MessageFilter(sender="landlord@example.com").matches(_message()) is True
    assert MessageFilter(sender="someone@else.com").matches(_message()) is False


def test_message_filter_keywords_are_case_insensitive_contains_any() -> None:
    assert MessageFilter(keywords=("invoice",)).matches(_message()) is True  # over body, any-case
    assert MessageFilter(keywords=("rent",)).matches(_message()) is True  # over subject
    assert MessageFilter(keywords=("nope", "invoice")).matches(_message()) is True  # ANY-match
    assert MessageFilter(keywords=("mortgage",)).matches(_message()) is False


def test_message_filter_rejects_non_message_events() -> None:
    lifecycle = TaskLegCompleted(
        event_id="e", owner_id="o", occurred_at=_WHEN, task_id="t", persona_id="p"
    )
    assert MessageFilter().matches(lifecycle) is False


def test_lifecycle_filter_matches_watched_task_only() -> None:
    completed = TaskLegCompleted(
        event_id="e", owner_id="o", occurred_at=_WHEN, task_id="task-1", persona_id="p"
    )
    assert LifecycleFilter(task_id="task-1").matches(completed) is True
    assert LifecycleFilter(task_id="task-2").matches(completed) is False


def test_lifecycle_filter_milestone_matches() -> None:
    milestone = TaskMilestone(
        event_id="e", owner_id="o", occurred_at=_WHEN, task_id="task-1", persona_id="p"
    )
    assert LifecycleFilter(task_id="task-1").matches(milestone) is True


def test_lifecycle_min_failure_count_gates_and_excludes_non_failures() -> None:
    failed_once = TaskLegFailed(
        event_id="e",
        owner_id="o",
        occurred_at=_WHEN,
        task_id="task-1",
        persona_id="p",
        failure_count=1,
    )
    failed_twice = failed_once.model_copy(update={"failure_count": 2})
    filt = LifecycleFilter(task_id="task-1", min_failure_count=2)
    assert filt.matches(failed_once) is False  # below the floor
    assert filt.matches(failed_twice) is True  # at the floor
    # A completed/milestone event carries no failure count — a min_failure_count filter excludes it.
    completed = TaskLegCompleted(
        event_id="e", owner_id="o", occurred_at=_WHEN, task_id="task-1", persona_id="p"
    )
    assert filt.matches(completed) is False


def test_link_filter_matches_platform() -> None:
    linked = ConnectorLinked(event_id="e", owner_id="o", occurred_at=_WHEN, platform="telegram")
    unlinked = ConnectorUnlinked(event_id="e", owner_id="o", occurred_at=_WHEN, platform="telegram")
    assert LinkFilter().matches(linked) is True  # unset platform ⇒ any
    assert LinkFilter(platform="telegram").matches(linked) is True
    assert LinkFilter(platform="telegram").matches(unlinked) is True
    assert LinkFilter(platform="slack").matches(linked) is False


def test_link_filter_rejects_non_link_events() -> None:
    message = _message()
    assert LinkFilter().matches(message) is False
