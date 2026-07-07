"""The A7 event catalogue, filters, actions, and settings (Spec A7).

The pure, storage-free core of event triggers: the closed typed event catalogue (:mod:`catalogue`),
the typed per-kind match filters (:mod:`filters`), the two-door action union (:mod:`actions`), and
the env-driven tunables (:mod:`config`). The registry store + dispatcher live in persona-api; the
``EventFire`` leg trigger this seam produces lives in :mod:`persona.tasks` (A7-D-6).
"""

from __future__ import annotations

from persona.events.actions import (
    TRIGGER_ACTION_KINDS,
    EnqueueInitiativeCandidate,
    FireTaskLeg,
    TriggerAction,
)
from persona.events.catalogue import (
    CATALOGUE_VERSION,
    ConnectorLinked,
    ConnectorMessageReceived,
    ConnectorUnlinked,
    Event,
    EventKind,
    TaskLegCompleted,
    TaskLegFailed,
    TaskMilestone,
    event_kind_of,
)
from persona.events.config import EventTriggerSettings
from persona.events.filters import (
    LifecycleFilter,
    LinkFilter,
    MessageFilter,
    TriggerFilter,
    platform_of,
)
from persona.events.spec import TriggerSpec

__all__ = [
    "CATALOGUE_VERSION",
    "TRIGGER_ACTION_KINDS",
    "ConnectorLinked",
    "ConnectorMessageReceived",
    "ConnectorUnlinked",
    "EnqueueInitiativeCandidate",
    "Event",
    "EventKind",
    "EventTriggerSettings",
    "FireTaskLeg",
    "LifecycleFilter",
    "LinkFilter",
    "MessageFilter",
    "TaskLegCompleted",
    "TaskLegFailed",
    "TaskMilestone",
    "TriggerAction",
    "TriggerFilter",
    "TriggerSpec",
    "event_kind_of",
    "platform_of",
]
