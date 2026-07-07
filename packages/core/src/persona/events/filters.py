"""Typed per-kind trigger filters (Spec A7, A7-D-2).

A trigger says *which* events of its kind it wants through a typed filter — one shape per event
family. Matching is pure, in-process boolean logic (no model call on the match path, research §3):
exact equality on platform/sender/thread/task, case-insensitive ``contains`` (ANY-match) on
keywords over subject+body. **No regex, no expression language** (A7-D-2, the named non-goal) — a
filter is typed fields, not user code, so the A4 echo can render it back legibly.

Each filter's :meth:`matches` returns ``False`` for an event outside its family, so a filter paired
with the wrong :class:`~persona.events.catalogue.EventKind` simply never matches (the registry +
dispatcher enforce the pairing; the filter fails closed regardless).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from persona.events.catalogue import (
    ConnectorLinked,
    ConnectorMessageReceived,
    ConnectorUnlinked,
    Event,
    TaskLegCompleted,
    TaskLegFailed,
    TaskMilestone,
)

__all__ = [
    "LifecycleFilter",
    "LinkFilter",
    "MessageFilter",
    "TriggerFilter",
    "platform_of",
]


class MessageFilter(BaseModel):
    """Filter for ``connector.message_received`` (platform/sender/thread + keyword ANY-contains)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["message"] = "message"
    platform: str | None = None
    #: Normalised sender (E.164 / lowercased email / ``team:user``); matched case-insensitively.
    sender: str | None = None
    thread_id: str | None = None
    #: Case-insensitive ``contains``, ANY-match over subject+body (A7-D-2). Empty ⇒ no keyword gate.
    keywords: tuple[str, ...] = ()

    def matches(self, event: Event) -> bool:
        """True iff ``event`` is a message event passing every set field (unset = wildcard)."""
        if not isinstance(event, ConnectorMessageReceived):
            return False
        if self.platform is not None and self.platform != event.platform:
            return False
        if self.sender is not None and self.sender.lower() != event.sender_id.lower():
            return False
        if self.thread_id is not None and self.thread_id != event.thread_id:
            return False
        if self.keywords:
            haystack = f"{event.subject or ''}\n{event.body}".lower()
            if not any(keyword.lower() in haystack for keyword in self.keywords):
                return False
        return True


class LifecycleFilter(BaseModel):
    """Filter for ``task.leg_completed`` / ``task.leg_failed`` / ``task.milestone`` (by task)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["lifecycle"] = "lifecycle"
    #: The watched task — a lifecycle trigger always targets one confirmed task.
    task_id: str = Field(min_length=1)
    #: For ``task.leg_failed``: fire only at/after N failures ("fails twice → escalate"). ``None`` ⇒
    #: any failure. Ignored for completed/milestone events (they carry no failure count).
    min_failure_count: int | None = Field(default=None, ge=1)

    def matches(self, event: Event) -> bool:
        """True iff ``event`` is a lifecycle event for the watched task meeting the fail floor."""
        if not isinstance(event, TaskLegCompleted | TaskLegFailed | TaskMilestone):
            return False
        if self.task_id != event.task_id:
            return False
        if self.min_failure_count is not None:
            if not isinstance(event, TaskLegFailed):
                return False
            if event.failure_count < self.min_failure_count:
                return False
        return True


class LinkFilter(BaseModel):
    """Filter for ``connector.linked`` / ``connector.unlinked`` (by platform)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["link"] = "link"
    platform: str | None = None

    def matches(self, event: Event) -> bool:
        """True iff ``event`` is a link event on the watched platform (unset ⇒ any platform)."""
        if not isinstance(event, ConnectorLinked | ConnectorUnlinked):
            return False
        return self.platform is None or self.platform == event.platform


#: A trigger's typed filter — discriminated over the three event families (keys on ``kind``).
TriggerFilter = Annotated[
    MessageFilter | LifecycleFilter | LinkFilter,
    Field(discriminator="kind"),
]


def platform_of(trigger_filter: MessageFilter | LifecycleFilter | LinkFilter) -> str | None:
    """The platform a filter is scoped to, or ``None`` (the denormalised column's source of truth).

    Message and link filters carry a ``platform`` (used for unlink-hygiene scoping, criterion 7);
    lifecycle filters have none. The registry store denormalises this onto the row via THIS one
    function so the column can never drift from the filter (the single-write-path invariant).
    """
    if isinstance(trigger_filter, MessageFilter | LinkFilter):
        return trigger_filter.platform
    return None
