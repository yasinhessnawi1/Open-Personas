"""A task is persona-scoped, not channel-scoped (Spec A4, T12; criterion 9).

Structural + unit proof of the cross-channel / cross-switching properties: a task reports on the
contract's **preferred** channel (not the channel it was created on), is steerable **channel-
agnostically** (the steering carries owner + task, never a channel), and is a durable entity that
does not depend on any live conversation (so persona-switching suspends the *conversation*, never
the *task*). The owner-scoping of a cross-channel steer is proved non-vacuously on the real stack
in the companion integration test (test_task_steering_rls.py).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.schema.origination import PersonaIdentityTag
from persona.tasks import Contract, Task, UpdateGranularity, UpdatePreference
from persona_api.approvals.cadence import MessagePriority
from persona_api.tasks.updates import TaskUpdatePublisher
from persona_runtime.agentic.events import RunEvent

_NOW = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
_TAG = PersonaIdentityTag(persona_id="astrid", display_name="Astrid")


class _FakeSender:
    def __init__(self) -> None:
        self.channels: list[str | None] = []

    async def send(self, *, channel: str | None, **_kw: object) -> None:  # noqa: ANN003
        self.channels.append(channel)


# --- reports on the PREFERRED channel, not the creation channel (A4-D-6) ----------------


@pytest.mark.asyncio
async def test_task_reports_on_preferred_channel_not_the_creation_channel() -> None:
    # Created on Telegram (conversation_id), but the contract prefers email — the update goes to
    # email, independent of where the task was born.
    sender = _FakeSender()
    task = Task(
        id="t1",
        owner_id="user-a",
        persona_id="astrid",
        contract=Contract(
            goal="track fares",
            updates=UpdatePreference(granularity=UpdateGranularity.MILESTONES, channel="email"),
        ),
        conversation_id="telegram-conversation-xyz",  # created on Telegram
        created_at=_NOW,
        updated_at=_NOW,
    )
    await TaskUpdatePublisher(sender=sender).publish(
        task=task,
        persona=_TAG,
        priority=MessagePriority.PROGRESS,
        is_milestone=True,
        is_completion=False,
        content="major progress",
        now=_NOW,
    )
    assert sender.channels == ["email"]  # the contract's channel, not "telegram-conversation-xyz"


# --- steering is channel-agnostic (owner + task only) ----------------------------------


def test_steering_event_carries_no_channel_only_verb_and_task() -> None:
    # A steer resolves to (verb, task_id) — no channel. So the same task is steerable from any of
    # the owner's channels; the worker adds owner_id (tenancy), still no channel binding.
    event = RunEvent.task_steering(verb="cancel", task_id="task-fare")
    assert set(event.data) == {"verb", "task_id"}
    assert "channel" not in event.data
    assert "conversation_id" not in event.data


# --- persona-scoped, not conversation-bound (survives switch) --------------------------


def test_task_is_valid_without_a_live_conversation() -> None:
    # A task's existence does not require a conversation — ``conversation_id`` is optional. So
    # suspending the conversation (persona switch, C1) leaves the durable task untouched; it is
    # keyed by (owner_id, persona_id) + task id, not by a channel conversation.
    task = Task(
        id="t1",
        owner_id="user-a",
        persona_id="astrid",
        contract=Contract(goal="keep running regardless of the chat"),
        conversation_id=None,  # no live conversation — still a valid, runnable task
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert task.conversation_id is None
    assert task.owner_id == "user-a"
    assert task.persona_id == "astrid"
