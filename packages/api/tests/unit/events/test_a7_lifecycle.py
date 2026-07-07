"""Unit tests for the lifecycle emitter's chain inheritance (Spec A7, T4).

The loop-critical propagation: a leg fired by an ``EventFire`` emits a lifecycle event that INHERITS
the trigger's causal chain; a clock/reply-fired leg emits an organic (empty-chain) event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from persona.events import Event, TaskLegCompleted, TaskMilestone
from persona.tasks import EventFire, ScheduledFire
from persona_api.events.lifecycle import LifecycleEmitter
from persona_runtime.legs import LegDisposition

_NOW = datetime(2026, 7, 6, 9, 0, tzinfo=UTC)


class _FakeDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[Event] = []

    def dispatch(self, event: Event, *, now: datetime) -> list[object]:  # noqa: ARG002
        self.dispatched.append(event)
        return []


def _outcome(disposition: LegDisposition, *, resume_at: datetime | None = None) -> object:
    task = SimpleNamespace(
        id="task-1", owner_id="own-1", persona_id="pers-1", head_checkpoint_seq=2
    )
    return SimpleNamespace(task=task, disposition=disposition, resume_at=resume_at)


def _event_fire(chain: tuple[str, ...]) -> EventFire:
    return EventFire(
        trigger_id=chain[-1] if chain else "t",
        event_kind="task.leg_completed",
        event_id="e",
        fired_at=_NOW,
        human="h",
        causal_chain=chain,
    )


@pytest.mark.asyncio
async def test_completed_leg_emits_task_leg_completed_inheriting_the_chain() -> None:
    dispatcher = _FakeDispatcher()
    emitter = LifecycleEmitter(dispatcher=dispatcher)  # type: ignore[arg-type]
    await emitter.on_leg_settled(
        _outcome(LegDisposition.COMPLETED),  # type: ignore[arg-type]
        _event_fire(("trg-a", "trg-b")),
        _NOW,
    )
    assert len(dispatcher.dispatched) == 1
    event = dispatcher.dispatched[0]
    assert isinstance(event, TaskLegCompleted)
    assert event.task_id == "task-1"
    assert event.causal_chain == ("trg-a", "trg-b")  # INHERITED — the loop marker survives


@pytest.mark.asyncio
async def test_clock_fired_leg_emits_an_organic_empty_chain_event() -> None:
    dispatcher = _FakeDispatcher()
    emitter = LifecycleEmitter(dispatcher=dispatcher)  # type: ignore[arg-type]
    await emitter.on_leg_settled(
        _outcome(LegDisposition.COMPLETED),  # type: ignore[arg-type]
        ScheduledFire(schedule_id="s", fire_time=_NOW),
        _NOW,
    )
    event = dispatcher.dispatched[0]
    assert event.causal_chain == ()  # a schedule-fired leg's lifecycle event is organic


@pytest.mark.asyncio
async def test_timed_wait_emits_a_milestone() -> None:
    dispatcher = _FakeDispatcher()
    emitter = LifecycleEmitter(dispatcher=dispatcher)  # type: ignore[arg-type]
    await emitter.on_leg_settled(
        _outcome(LegDisposition.CONTINUE, resume_at=_NOW),  # type: ignore[arg-type]
        _event_fire(("trg-a",)),
        _NOW,
    )
    assert isinstance(dispatcher.dispatched[0], TaskMilestone)


@pytest.mark.asyncio
async def test_immediate_continue_emits_nothing() -> None:
    dispatcher = _FakeDispatcher()
    emitter = LifecycleEmitter(dispatcher=dispatcher)  # type: ignore[arg-type]
    await emitter.on_leg_settled(
        _outcome(LegDisposition.CONTINUE, resume_at=None),  # type: ignore[arg-type]
        _event_fire(("trg-a",)),
        _NOW,
    )
    assert dispatcher.dispatched == []  # an immediate continuation is not a lifecycle-trigger event
