"""The closed v1 event catalogue (Spec A7, A7-D-1).

A7 reacts to a **small, closed** set of typed platform events — never a config-declared or
user-authored kind (the named rules-engine non-goal). Each :class:`EventKind` has a frozen,
``extra="forbid"`` envelope carrying only the fields that kind's real birth point already has in
scope (research §3): message events at the connector inbound seam, lifecycle events at the task
state fork, link events at the connector link/unlink seam.

Every envelope carries a ``causal_chain`` — the ordered trigger-ids of the fires that (transitively)
caused this event. It is ``()`` for an organically-born event (a real inbound, a user-driven task
outcome) and inherited+extended when an event is emitted *by* a triggered action. The A7 loop guard
(A7-D-4/D-6) reads this chain to refuse a match whose trigger is already in it — the chain travels
in the :class:`persona.tasks.EventFire` payload across the connector→worker boundary and is stamped
onto any event a triggered leg emits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "CATALOGUE_VERSION",
    "ConnectorLinked",
    "ConnectorMessageReceived",
    "ConnectorUnlinked",
    "Event",
    "EventKind",
    "TaskLegCompleted",
    "TaskLegFailed",
    "TaskMilestone",
    "event_kind_of",
]

#: Bumped whenever the catalogue changes shape — new kinds are spec work, not config (A7-D-1).
CATALOGUE_VERSION = "v1"


class EventKind(StrEnum):
    """The closed v1 catalogue of platform events A7 can react to (A7-D-1)."""

    CONNECTOR_MESSAGE_RECEIVED = "connector.message_received"
    TASK_LEG_COMPLETED = "task.leg_completed"
    TASK_LEG_FAILED = "task.leg_failed"
    TASK_MILESTONE = "task.milestone"
    CONNECTOR_LINKED = "connector.linked"
    CONNECTOR_UNLINKED = "connector.unlinked"


def _ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes; normalise tz-aware ones to UTC (the trigger.py house rule)."""
    if value.tzinfo is None:
        msg = "naive datetime not allowed; use datetime.now(UTC) or attach a tzinfo"
        raise ValueError(msg)
    return value.astimezone(UTC)


class _EventBase(BaseModel):
    """Fields every event envelope carries (identity, owner scope, provenance chain).

    Attributes:
        event_id: The platform-stable dedup key (the ``idempotency_key`` seed — a re-delivered
            webhook re-keys to the same action and no-ops).
        owner_id: The tenant the event belongs to (RLS scope; matching is owner-scoped).
        occurred_at: When the event happened (tz-aware UTC).
        causal_chain: The ordered trigger-ids that caused this event — ``()`` for an organic event,
            inherited+extended for an event emitted by a triggered action (the loop-guard marker).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    occurred_at: datetime
    causal_chain: tuple[str, ...] = ()

    @field_validator("occurred_at", mode="after")
    @classmethod
    def _occurred_at_tz_aware(cls, value: datetime) -> datetime:
        return _ensure_utc(value)


class ConnectorMessageReceived(_EventBase):
    """An inbound message reached the persona (the C-track inbound seam).

    Born at ``SharedInboundFlow.handle_text`` where owner/persona/platform/sender/body are all
    resolved. ``subject`` is present only where the platform carries one (email); ``thread_id`` only
    where a thread is known. ``body``+``subject`` are the keyword-match haystack (A7-D-2).
    """

    kind: Literal[EventKind.CONNECTOR_MESSAGE_RECEIVED] = EventKind.CONNECTOR_MESSAGE_RECEIVED
    platform: str = Field(min_length=1)
    sender_id: str = Field(min_length=1)
    thread_id: str | None = None
    subject: str | None = None
    body: str = ""
    conversation_id: str = Field(min_length=1)
    persona_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)


class TaskLegCompleted(_EventBase):
    """A task leg settled COMPLETED (A2 lifecycle) — the occurrence or terminal completion."""

    kind: Literal[EventKind.TASK_LEG_COMPLETED] = EventKind.TASK_LEG_COMPLETED
    task_id: str = Field(min_length=1)
    persona_id: str = Field(min_length=1)


class TaskLegFailed(_EventBase):
    """A task leg failed terminally (dead-lettered) — carries the accumulated failure count.

    Born at ``TaskContinuation.react_to_dead_leg`` (retry-exhausted), NOT the transient re-raise —
    ``failure_count`` is the "fails twice → escalate" signal the lifecycle filter reads (A7-D-2).
    """

    kind: Literal[EventKind.TASK_LEG_FAILED] = EventKind.TASK_LEG_FAILED
    task_id: str = Field(min_length=1)
    persona_id: str = Field(min_length=1)
    failure_count: int = Field(ge=1)


class TaskMilestone(_EventBase):
    """A task hit a milestone (A2 lifecycle) — an occurrence-complete / timed-wait boundary."""

    kind: Literal[EventKind.TASK_MILESTONE] = EventKind.TASK_MILESTONE
    task_id: str = Field(min_length=1)
    persona_id: str = Field(min_length=1)


class ConnectorLinked(_EventBase):
    """A platform identity was linked to the owner (C1 lifecycle)."""

    kind: Literal[EventKind.CONNECTOR_LINKED] = EventKind.CONNECTOR_LINKED
    platform: str = Field(min_length=1)


class ConnectorUnlinked(_EventBase):
    """A platform identity was unlinked (C1 lifecycle) — drives A7 unlink hygiene (criterion 7)."""

    kind: Literal[EventKind.CONNECTOR_UNLINKED] = EventKind.CONNECTOR_UNLINKED
    platform: str = Field(min_length=1)


#: A platform event — a discriminated union over the closed catalogue (Pydantic keys on ``kind``).
Event = Annotated[
    ConnectorMessageReceived
    | TaskLegCompleted
    | TaskLegFailed
    | TaskMilestone
    | ConnectorLinked
    | ConnectorUnlinked,
    Field(discriminator="kind"),
]


def event_kind_of(event: _EventBase) -> EventKind:
    """Return the :class:`EventKind` of any catalogue event (the discriminator, typed)."""
    # Every concrete envelope pins ``kind`` to an EventKind literal; mypy narrows via the union.
    return event.kind  # type: ignore[attr-defined,no-any-return]
