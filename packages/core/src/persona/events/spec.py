"""The trigger spec an A4 contract carries — "what to watch" (Spec A7, T7).

A :class:`TriggerSpec` is the event half of a confirmed contract: which closed :class:`EventKind`
to react to, the typed :class:`TriggerFilter` narrowing it, and the human phrase the A4 echo renders
("an email from landlord@… arrives"). It is the trigger analogue of A4's ``ParsedSchedule`` — the
draft carries one, and per A7-D-3 a contract has EITHER a schedule OR a trigger, never both. The
registry row (action + enabled + cooldown state) is derived from this at origination, when the
confirmed contract's task exists to fire.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from persona.events.catalogue import EventKind
from persona.events.filters import (
    LifecycleFilter,
    LinkFilter,
    MessageFilter,
    TriggerFilter,
)

__all__ = ["TriggerSpec"]

# Which filter shape each event kind admits (the A7-D-2 pairing).
_MESSAGE_KINDS = frozenset({EventKind.CONNECTOR_MESSAGE_RECEIVED})
_LIFECYCLE_KINDS = frozenset(
    {EventKind.TASK_LEG_COMPLETED, EventKind.TASK_LEG_FAILED, EventKind.TASK_MILESTONE}
)
_LINK_KINDS = frozenset({EventKind.CONNECTOR_LINKED, EventKind.CONNECTOR_UNLINKED})


class TriggerSpec(BaseModel):
    """The "when X happens" a confirmed contract watches — kind + typed filter + echo phrase."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_kind: EventKind
    filter: TriggerFilter
    #: The human phrase the A4 echo renders after "When: whenever " (A7-R-3; user-comprehension).
    human_terms: str = Field(min_length=1)

    @model_validator(mode="after")
    def _filter_matches_kind(self) -> TriggerSpec:
        """The filter shape must match the event kind family (A7-D-2) — fail fast."""
        ok = (
            (self.event_kind in _MESSAGE_KINDS and isinstance(self.filter, MessageFilter))
            or (self.event_kind in _LIFECYCLE_KINDS and isinstance(self.filter, LifecycleFilter))
            or (self.event_kind in _LINK_KINDS and isinstance(self.filter, LinkFilter))
        )
        if not ok:
            msg = f"filter {type(self.filter).__name__} does not match event kind {self.event_kind}"
            raise ValueError(msg)
        return self
