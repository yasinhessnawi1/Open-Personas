"""Unit tests for digest-granularity task updates (Spec A4, T10; A4-D-3)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.schema.origination import PersonaIdentityTag
from persona.tasks import Contract, Task, UpdateGranularity, UpdatePreference
from persona_api.approvals.cadence import CadenceDecision, MessagePriority
from persona_api.tasks.updates import TaskUpdatePublisher, should_deliver_update

if TYPE_CHECKING:
    from datetime import datetime as _dt

_NOW = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
_TAG = PersonaIdentityTag(persona_id="astrid", display_name="Astrid")


# --- the granularity filter (A4-D-3) ---------------------------------------------------


@pytest.mark.parametrize("granularity", list(UpdateGranularity))
@pytest.mark.parametrize(
    "priority", [MessagePriority.APPROVAL, MessagePriority.FAILURE, MessagePriority.SAFETY]
)
def test_always_pass_classes_bypass_every_granularity(
    granularity: UpdateGranularity, priority: MessagePriority
) -> None:
    # The invariant: approval/failure/safety ALWAYS deliver — even under QUIET.
    assert should_deliver_update(
        priority=priority, granularity=granularity, is_milestone=False, is_completion=False
    )


def test_quiet_suppresses_progress() -> None:
    assert not should_deliver_update(
        priority=MessagePriority.PROGRESS,
        granularity=UpdateGranularity.QUIET,
        is_milestone=True,
        is_completion=True,
    )


def test_every_leg_delivers_all_progress() -> None:
    assert should_deliver_update(
        priority=MessagePriority.PROGRESS,
        granularity=UpdateGranularity.EVERY_LEG,
        is_milestone=False,
        is_completion=False,
    )


def test_milestones_delivers_only_milestones() -> None:
    assert should_deliver_update(
        priority=MessagePriority.PROGRESS,
        granularity=UpdateGranularity.MILESTONES,
        is_milestone=True,
        is_completion=False,
    )
    assert not should_deliver_update(
        priority=MessagePriority.PROGRESS,
        granularity=UpdateGranularity.MILESTONES,
        is_milestone=False,
        is_completion=False,
    )


def test_completion_only_delivers_only_completion() -> None:
    assert should_deliver_update(
        priority=MessagePriority.PROGRESS,
        granularity=UpdateGranularity.COMPLETION_ONLY,
        is_milestone=True,
        is_completion=True,
    )
    assert not should_deliver_update(
        priority=MessagePriority.PROGRESS,
        granularity=UpdateGranularity.COMPLETION_ONLY,
        is_milestone=True,
        is_completion=False,
    )


# --- the publisher (composes the filter + the sender + the contract's channel) ----------


class _FakeSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(
        self,
        *,
        persona: PersonaIdentityTag,
        owner_id: str,
        content: str,
        channel: str | None,
        conversation_id: str | None,
        priority: MessagePriority,
        now: _dt,
    ) -> None:
        _ = (persona, now)  # part of the Protocol; unused by the fake
        self.sent.append(
            {
                "owner_id": owner_id,
                "content": content,
                "channel": channel,
                "conversation_id": conversation_id,
                "priority": priority,
            }
        )


def _task(*, updates: UpdatePreference | None, conversation_id: str = "conv-1") -> Task:
    return Task(
        id="t1",
        owner_id="user-a",
        persona_id="astrid",
        contract=Contract(goal="track fares", updates=updates),
        conversation_id=conversation_id,
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.mark.asyncio
async def test_publisher_suppresses_progress_on_quiet() -> None:
    sender = _FakeSender()
    task = _task(updates=UpdatePreference(granularity=UpdateGranularity.QUIET))
    delivered = await TaskUpdatePublisher(sender=sender).publish(
        task=task,
        persona=_TAG,
        priority=MessagePriority.PROGRESS,
        is_milestone=True,
        is_completion=False,
        content="found 9 fares",
        now=_NOW,
    )
    assert delivered is False
    assert sender.sent == []


class _FakeCadence:
    """A cadence stub returning a scripted decision (no DB)."""

    def __init__(self, decision: CadenceDecision) -> None:
        self._decision = decision
        self.admit_calls: list[tuple[str, str]] = []

    def admit(
        self, owner_id: str, persona_id: str, priority: MessagePriority, *, now: _dt
    ) -> CadenceDecision:
        _ = (priority, now)
        self.admit_calls.append((owner_id, persona_id))
        return self._decision


class _FakeDigestSink:
    def __init__(self) -> None:
        self.deferred: list[tuple[str, str, str]] = []

    def defer(self, owner_id: str, persona_id: str, content: str, *, now: _dt) -> None:
        _ = now
        self.deferred.append((owner_id, persona_id, content))


@pytest.mark.asyncio
async def test_over_cap_progress_batches_to_the_digest_sink_not_delivered() -> None:
    """A3-D-4 / A6-D-10: a granularity-admitted progress update over the daily cap DIGESTs.

    The audit gap: CadenceGate + DeferredDigestStore.defer had no producer, so the morning
    review's deferred-chatter section was permanently empty. Now an over-cap progress update
    batches to the sink instead of being dropped or delivered.
    """
    sender = _FakeSender()
    cadence = _FakeCadence(CadenceDecision.DIGEST)  # over the cap
    sink = _FakeDigestSink()
    task = _task(updates=UpdatePreference(granularity=UpdateGranularity.EVERY_LEG))
    delivered = await TaskUpdatePublisher(sender=sender, cadence=cadence, digest_sink=sink).publish(
        task=task,
        persona=_TAG,
        priority=MessagePriority.PROGRESS,
        is_milestone=False,
        is_completion=False,
        content="still scanning fares",
        now=_NOW,
    )
    assert delivered is False  # not delivered now
    assert sender.sent == []  # …not sent…
    assert sink.deferred == [
        ("user-a", "astrid", "still scanning fares")
    ]  # …deferred to the digest
    assert cadence.admit_calls == [("user-a", "astrid")]


@pytest.mark.asyncio
async def test_under_cap_progress_delivers_and_counts() -> None:
    sender = _FakeSender()
    cadence = _FakeCadence(CadenceDecision.DELIVER)  # under the cap
    sink = _FakeDigestSink()
    task = _task(updates=UpdatePreference(granularity=UpdateGranularity.EVERY_LEG))
    delivered = await TaskUpdatePublisher(sender=sender, cadence=cadence, digest_sink=sink).publish(
        task=task,
        persona=_TAG,
        priority=MessagePriority.PROGRESS,
        is_milestone=False,
        is_completion=False,
        content="found a fare",
        now=_NOW,
    )
    assert delivered is True
    assert len(sender.sent) == 1  # delivered now
    assert sink.deferred == []  # nothing batched


@pytest.mark.asyncio
async def test_publisher_still_delivers_failure_on_quiet_the_invariant() -> None:
    # THE A4-D-3 invariant end-to-end: a quiet-preference task still emits a failure.
    sender = _FakeSender()
    task = _task(updates=UpdatePreference(granularity=UpdateGranularity.QUIET, channel="email"))
    delivered = await TaskUpdatePublisher(sender=sender).publish(
        task=task,
        persona=_TAG,
        priority=MessagePriority.FAILURE,
        is_milestone=False,
        is_completion=False,
        content="I hit a wall and need you",
        now=_NOW,
    )
    assert delivered is True
    assert len(sender.sent) == 1
    assert sender.sent[0]["channel"] == "email"  # on the contract's preferred channel (A4-D-6)
    assert sender.sent[0]["priority"] is MessagePriority.FAILURE


@pytest.mark.asyncio
async def test_publisher_defaults_to_milestones_and_home_channel_when_unset() -> None:
    sender = _FakeSender()
    task = _task(updates=None)  # no preference → milestones, home channel
    delivered = await TaskUpdatePublisher(sender=sender).publish(
        task=task,
        persona=_TAG,
        priority=MessagePriority.PROGRESS,
        is_milestone=True,
        is_completion=False,
        content="major progress",
        now=_NOW,
    )
    assert delivered is True
    assert sender.sent[0]["channel"] is None  # None → the router's home fallback
