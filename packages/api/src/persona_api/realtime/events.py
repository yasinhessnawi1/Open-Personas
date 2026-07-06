"""Spec A11 T1 — the closed, versioned, data-only channel event catalogue (A11-D-2).

The persistent user channel (``GET /v1/me/events``) carries a **closed** set of
frozen, versioned, data-only events. Each event is a thin liveness signal — it
carries just enough to update a badge / append / dedupe; the client **refetches**
the authoritative record (P6 feed, the conversation, the task) where it needs full
fidelity. New event types are added by the same discipline (a new frozen model + a
bump of :data:`ChannelEvent`), never free-form — so every consumer (A6/A7, the web
``useMeEvents``) knows the exhaustive set at compile time.

Two **control** events (:class:`ReadyControl`, :class:`ResyncControl`) ride the same
channel but are deliberately NOT in :data:`ChannelEvent`: they are out-of-band
transport signals (baseline / restart-safe resume — A11-D-3), distinguished from
data events by their reserved SSE ``event:`` name so the client parser can never
confuse a control signal for a data append.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

__all__ = [
    "ChannelEvent",
    "MessageDeliveredEvent",
    "NotificationCreatedEvent",
    "ReadyControl",
    "ResyncControl",
    "TaskUpdatedEvent",
]


class _Event(BaseModel):
    """Frozen, extra-forbidding base with the pinned protocol version.

    ``v`` is a ``Literal[1]`` so a wrong version fails validation (not silently
    coerced); ``frozen`` makes an event immutable once emitted; ``extra="forbid"``
    fails fast on an unexpected field at the boundary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    v: Literal[1] = 1


class NotificationCreatedEvent(_Event):
    """The bell updates instantly (A11-D-2).

    Data-only: the client refetches ``/v1/me/notifications`` (unioned with P6's
    durable feed, read-state honoured, no duplication) for full fidelity.
    """

    type: Literal["notification.created"] = "notification.created"
    kind: str
    ref_id: str | None = None
    # Optional: the durable write is ``ON CONFLICT DO NOTHING`` (no id returned), so
    # the event is a data-only "your bell changed" ping keyed on kind+ref_id — the
    # client refetches ``/v1/me/notifications`` (unioned with P6, read-state honoured)
    # and reconciles by id (A11-D-2). Paths that know the id may fill it.
    notification_id: str | None = None


class MessageDeliveredEvent(_Event):
    """A background-originated message → the open conversation appends it live and
    the conversation list re-orders (A11-D-2). The client refetches the
    authoritative message where full content fidelity is needed."""

    type: Literal["message.delivered"] = "message.delivered"
    conversation_id: str
    # Optional: the background origination boundary (`OriginatedMessage`) has no
    # persisted message id, so the event is a data-only "conversation X has a new
    # message" ping and the client refetches the conversation to reconcile by id
    # (A11-D-2). Paths that DO know the id (a future in-turn hand-off) may fill it;
    # the client dedupes on it when present.
    message_id: str | None = None
    persona_id: str
    persona_name: str
    visual_ref: str | None = None


class TaskUpdatedEvent(_Event):
    """A task state/fire transition — consumed by A6's Review/Approvals surfaces."""

    type: Literal["task.updated"] = "task.updated"
    task_id: str
    state: str


#: The closed v1 data catalogue. Control events are deliberately excluded.
ChannelEvent = NotificationCreatedEvent | MessageDeliveredEvent | TaskUpdatedEvent


class ReadyControl(_Event):
    """Sent once on stream open — hands the client its ``{epoch, latest_seq}``
    baseline so an idle-then-drop-then-reconnect (before any data event) can still
    resume, and flushes past the Fly edge proxy's response buffering."""

    type: Literal["ready"] = "ready"
    epoch: str
    latest_seq: int


class ResyncControl(_Event):
    """The restart-safe resume signal (A11-D-3): the server cannot honour the
    client's ``Last-Event-ID`` from its current ring (a prior process ``epoch``, or
    a cursor that fell off the ring), so the client must **full-refetch** the durable
    feeds (P6 + the open conversation) rather than replay-nothing-and-gap. Carries
    the current ``{epoch, latest_seq}`` so the client re-baselines cleanly."""

    type: Literal["resync"] = "resync"
    epoch: str
    latest_seq: int
    reason: Literal["epoch_changed", "ring_gap"]
