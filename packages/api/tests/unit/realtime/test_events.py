"""Spec A11 T1 — the closed, versioned, data-only event catalogue (A11-D-2).

Pins the frozen shape of every v1 channel event + the two control events
(``ready``/``resync``). These models are the authoritative source the web
``useMeEvents`` consumer hand-mirrors (D-09-1: OpenAPI can't model SSE payloads),
so a renamed/added field fails here first.
"""

from __future__ import annotations

import pytest
from persona_api.realtime.events import (
    ChannelEvent,
    MessageDeliveredEvent,
    NotificationCreatedEvent,
    ReadyControl,
    ResyncControl,
    TaskUpdatedEvent,
)
from pydantic import ValidationError


def test_notification_created_shape_and_defaults() -> None:
    e = NotificationCreatedEvent(kind="schedule_fire", ref_id="s1")
    assert e.v == 1
    assert e.type == "notification.created"
    assert e.notification_id is None  # data-only ping; client refetches
    assert e.model_dump(mode="json") == {
        "v": 1,
        "type": "notification.created",
        "kind": "schedule_fire",
        "ref_id": "s1",
        "notification_id": None,
    }


def test_notification_created_ref_id_optional() -> None:
    e = NotificationCreatedEvent(kind="persona_ready")
    assert e.ref_id is None


def test_notification_created_id_may_be_filled_when_known() -> None:
    e = NotificationCreatedEvent(kind="run_terminal", ref_id="run1", notification_id="n9")
    assert e.notification_id == "n9"


def test_message_delivered_shape() -> None:
    e = MessageDeliveredEvent(
        conversation_id="c1",
        message_id="m1",
        persona_id="p1",
        persona_name="Iris",
        visual_ref="avatars/p1.png",
    )
    assert e.type == "message.delivered"
    assert e.model_dump(mode="json") == {
        "v": 1,
        "type": "message.delivered",
        "conversation_id": "c1",
        "message_id": "m1",
        "persona_id": "p1",
        "persona_name": "Iris",
        "visual_ref": "avatars/p1.png",
    }


def test_message_delivered_visual_ref_optional() -> None:
    e = MessageDeliveredEvent(
        conversation_id="c1", message_id="m1", persona_id="p1", persona_name="Iris"
    )
    assert e.visual_ref is None


def test_message_delivered_message_id_optional_for_background_origination() -> None:
    # The background origination boundary has no persisted id — the event is a
    # conversation-level ping and the client refetches (A11-D-2).
    e = MessageDeliveredEvent(conversation_id="c1", persona_id="p1", persona_name="Iris")
    assert e.message_id is None
    assert e.model_dump(mode="json")["message_id"] is None


def test_task_updated_shape() -> None:
    e = TaskUpdatedEvent(task_id="t1", state="WAITING")
    assert e.type == "task.updated"
    assert e.model_dump(mode="json") == {
        "v": 1,
        "type": "task.updated",
        "task_id": "t1",
        "state": "WAITING",
    }


def test_events_are_frozen() -> None:
    e = NotificationCreatedEvent(notification_id="n1", kind="k")
    with pytest.raises(ValidationError):
        e.notification_id = "n2"  # type: ignore[misc]


def test_events_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        NotificationCreatedEvent(notification_id="n1", kind="k", surprise="x")  # type: ignore[call-arg]


def test_type_field_is_pinned_not_overridable() -> None:
    # A caller cannot mint a data event under a control-reserved type — the
    # Literal makes the name structural (client-parser distinctness guarantee).
    with pytest.raises(ValidationError):
        NotificationCreatedEvent(  # type: ignore[call-arg]
            type="resync", notification_id="n1", kind="k"
        )


def test_version_is_pinned_to_one() -> None:
    with pytest.raises(ValidationError):
        TaskUpdatedEvent(v=2, task_id="t1", state="WAITING")  # type: ignore[arg-type]


def test_ready_control_shape() -> None:
    c = ReadyControl(epoch="abc", latest_seq=42)
    assert c.type == "ready"
    assert c.model_dump(mode="json") == {
        "v": 1,
        "type": "ready",
        "epoch": "abc",
        "latest_seq": 42,
    }


def test_resync_control_shape_and_reason_enum() -> None:
    c = ResyncControl(epoch="abc", latest_seq=42, reason="epoch_changed")
    assert c.type == "resync"
    assert c.model_dump(mode="json") == {
        "v": 1,
        "type": "resync",
        "epoch": "abc",
        "latest_seq": 42,
        "reason": "epoch_changed",
    }
    with pytest.raises(ValidationError):
        ResyncControl(epoch="abc", latest_seq=1, reason="not_a_reason")  # type: ignore[arg-type]


def test_channel_event_union_covers_exactly_the_three_data_types() -> None:
    # The union is the closed catalogue; control events are deliberately NOT in it.
    members = set(ChannelEvent.__args__)  # type: ignore[attr-defined]
    assert members == {
        NotificationCreatedEvent,
        MessageDeliveredEvent,
        TaskUpdatedEvent,
    }
