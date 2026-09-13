"""Unit tests for the resume-trigger seam (Spec A2, T3; D-A2-5).

All three wait kinds converge on ONE resume mechanism: ``resume_task(task_id, trigger)``
materialises a resume leg carrying the trigger. ``ScheduledFire`` / ``UserReply`` are
exercised for real in v1; ``EventTrigger`` is the reserved seam — defined, no producer.
The ``TaskResumer`` Protocol is the one function-shaped seam, not a framework.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.tasks import (
    EventFire,
    EventTrigger,
    ResumeTrigger,
    ScheduledFire,
    TaskResumer,
    UserReply,
    WaitKind,
    wait_kind_for,
)
from pydantic import TypeAdapter, ValidationError

_FIRE = datetime(2026, 6, 25, 7, 0, tzinfo=UTC)


def test_scheduled_fire_carries_the_anchor() -> None:
    fire = ScheduledFire(schedule_id="sched-1", fire_time=_FIRE)
    assert fire.kind == "scheduled_fire"
    assert fire.schedule_id == "sched-1"
    assert fire.fire_time == _FIRE


def test_user_reply_carries_the_reply() -> None:
    reply = UserReply(reply="yes, Tuesday works", in_reply_to="q-1")
    assert reply.kind == "user_reply"
    assert reply.reply == "yes, Tuesday works"
    assert reply.in_reply_to == "q-1"


def test_event_trigger_is_the_reserved_seam() -> None:
    event = EventTrigger(source="calendar", payload="meeting moved")
    assert event.kind == "event"


def test_event_fire_carries_provenance_and_the_causal_chain() -> None:
    # A7-D-6: the structured on-event producer — the chain the loop guard reads rides on it.
    fire = EventFire(
        trigger_id="trg-1",
        event_kind="connector.message_received",
        event_id="evt-9",
        fired_at=_FIRE,
        human="an email from landlord@example.com arrived",
        causal_chain=("trg-1",),
    )
    assert fire.kind == "event_fire"
    assert fire.trigger_id == "trg-1"
    assert fire.event_id == "evt-9"
    assert fire.human.startswith("an email")
    assert fire.causal_chain == ("trg-1",)
    assert wait_kind_for(fire) == WaitKind.ON_EVENT


def test_event_fire_causal_chain_defaults_empty_and_is_frozen() -> None:
    fire = EventFire(
        trigger_id="t", event_kind="task.leg_failed", event_id="e", fired_at=_FIRE, human="h"
    )
    assert fire.causal_chain == ()
    with pytest.raises(ValidationError):
        fire.trigger_id = "z"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        EventFire(  # type: ignore[call-arg]
            trigger_id="t", event_kind="k", event_id="e", fired_at=_FIRE, human="h", extra="no"
        )


def test_event_fire_fired_at_must_be_tz_aware() -> None:
    with pytest.raises(ValidationError):
        EventFire(
            trigger_id="t",
            event_kind="k",
            event_id="e",
            fired_at=datetime(2026, 6, 25, 7, 0),  # noqa: DTZ001
            human="h",
        )


def test_event_fire_is_in_the_resume_trigger_union() -> None:
    adapter: TypeAdapter[ResumeTrigger] = TypeAdapter(ResumeTrigger)
    parsed = adapter.validate_python(
        {
            "kind": "event_fire",
            "trigger_id": "t",
            "event_kind": "task.milestone",
            "event_id": "e",
            "fired_at": _FIRE.isoformat(),
            "human": "the task hit a milestone",
            "causal_chain": ["t"],
        }
    )
    assert isinstance(parsed, EventFire)
    assert parsed.causal_chain == ("t",)


def test_triggers_are_frozen_and_forbid_extra() -> None:
    fire = ScheduledFire(schedule_id="s", fire_time=_FIRE)
    with pytest.raises(ValidationError):
        fire.schedule_id = "z"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ScheduledFire(schedule_id="s", fire_time=_FIRE, extra="no")  # type: ignore[call-arg]


def test_fire_time_must_be_tz_aware() -> None:
    with pytest.raises(ValidationError):
        ScheduledFire(schedule_id="s", fire_time=datetime(2026, 6, 25, 7, 0))  # noqa: DTZ001


def test_resume_trigger_is_a_discriminated_union() -> None:
    adapter: TypeAdapter[ResumeTrigger] = TypeAdapter(ResumeTrigger)
    parsed = adapter.validate_python({"kind": "user_reply", "reply": "ok"})
    assert isinstance(parsed, UserReply)
    parsed_fire = adapter.validate_python(
        {"kind": "scheduled_fire", "schedule_id": "s", "fire_time": _FIRE.isoformat()}
    )
    assert isinstance(parsed_fire, ScheduledFire)


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [
        (ScheduledFire(schedule_id="s", fire_time=_FIRE), WaitKind.UNTIL_TIME),
        (UserReply(reply="ok"), WaitKind.ON_USER),
        (EventTrigger(source="x", payload="y"), WaitKind.ON_EVENT),
        (
            EventFire(trigger_id="t", event_kind="k", event_id="e", fired_at=_FIRE, human="h"),
            WaitKind.ON_EVENT,
        ),
    ],
)
def test_wait_kind_for_each_trigger(trigger: ResumeTrigger, expected: WaitKind) -> None:
    assert wait_kind_for(trigger) == expected


def test_task_resumer_protocol_is_structural() -> None:
    class _Resumer:
        def resume_task(self, task_id: str, trigger: ResumeTrigger) -> None:  # noqa: ARG002 — stub conforms to the Protocol shape
            return None

    class _NotAResumer:
        def something_else(self) -> None:
            return None

    assert isinstance(_Resumer(), TaskResumer)
    assert not isinstance(_NotAResumer(), TaskResumer)


# --- UserDispatch (Spec W1, D-W1-1): a one-off the user asked for directly --------


def test_user_dispatch_carries_when_the_user_asked() -> None:
    from persona.tasks import UserDispatch, wait_kind_for

    trigger = UserDispatch(dispatched_at=datetime(2026, 9, 6, 12, 0, tzinfo=UTC))
    assert trigger.kind == "user_dispatch"
    assert trigger.dispatched_at.tzinfo is UTC
    # The user's own hand un-parks like a reply does.
    assert wait_kind_for(trigger) is WaitKind.ON_USER


def test_user_dispatch_rejects_a_naive_timestamp() -> None:
    from persona.tasks import UserDispatch

    with pytest.raises(ValidationError):
        UserDispatch(dispatched_at=datetime(2026, 9, 6, 12, 0))  # noqa: DTZ001 — the point


def test_user_dispatch_round_trips_through_the_resume_trigger_union() -> None:
    from persona.tasks import ResumeTrigger, UserDispatch
    from pydantic import TypeAdapter

    original = UserDispatch(dispatched_at=datetime(2026, 9, 6, 12, 0, tzinfo=UTC))
    adapter: TypeAdapter[ResumeTrigger] = TypeAdapter(ResumeTrigger)
    assert adapter.validate_python(original.model_dump(mode="json")) == original


# --- the system's own wakes (Spec W1, D-W1-34 / D-W1-38) --------------------------------------
#
# Neither is a UserReply, and that is the point: a leg woken by the system must not be told a
# person said something. Both are pinned here, where a unit run catches a union that silently
# stopped accepting one of them.


def test_auto_retry_carries_the_failure_it_is_retrying() -> None:
    from persona.tasks import AutoRetry, wait_kind_for

    trigger = AutoRetry(cause="429 rate limit", retried_at=datetime(2026, 9, 11, 9, 0, tzinfo=UTC))
    assert trigger.kind == "auto_retry"
    assert trigger.retried_at.tzinfo is UTC
    assert trigger.cause == "429 rate limit"  # the next leg knows WHAT broke, so it can retry it
    # It un-parks a task that was waiting on the user for a failure they were offered.
    assert wait_kind_for(trigger) is WaitKind.ON_USER


def test_revived_carries_the_reason_it_was_put_back() -> None:
    from persona.tasks import Revived, wait_kind_for

    trigger = Revived(
        reason="the user resumed it after a pause",
        revived_at=datetime(2026, 9, 11, 9, 0, tzinfo=UTC),
    )
    assert trigger.kind == "revived"
    assert trigger.revived_at.tzinfo is UTC
    assert trigger.reason == "the user resumed it after a pause"
    assert wait_kind_for(trigger) is WaitKind.ON_USER


def test_auto_retry_rejects_a_naive_timestamp() -> None:
    from persona.tasks import AutoRetry

    with pytest.raises(ValidationError):
        AutoRetry(cause="429", retried_at=datetime(2026, 9, 11, 9, 0))  # noqa: DTZ001 — the point


def test_revived_rejects_a_naive_timestamp() -> None:
    from persona.tasks import Revived

    with pytest.raises(ValidationError):
        Revived(reason="put back", revived_at=datetime(2026, 9, 11, 9, 0))  # noqa: DTZ001 — the point


@pytest.mark.parametrize(
    "original",
    [
        pytest.param(
            "auto_retry",
            id="auto_retry",
        ),
        pytest.param(
            "revived",
            id="revived",
        ),
    ],
)
def test_the_system_wakes_round_trip_through_the_resume_trigger_union(original: str) -> None:
    """The trigger crosses a process boundary inside the job payload, so union membership is
    the thing that makes a revived leg readable on the other side."""
    from persona.tasks import AutoRetry, ResumeTrigger, Revived

    at = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
    trigger: ResumeTrigger = (
        AutoRetry(cause="429 rate limit", retried_at=at)
        if original == "auto_retry"
        else Revived(reason="the user extended its budget", revived_at=at)
    )
    adapter: TypeAdapter[ResumeTrigger] = TypeAdapter(ResumeTrigger)
    assert adapter.validate_python(trigger.model_dump(mode="json")) == trigger
