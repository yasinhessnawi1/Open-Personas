"""The A7 event-trigger registry + dispatcher (persona-api side).

:mod:`store` owns the durable, RLS-scoped trigger registry (the match lookup, the lifecycle
mutations, the atomic cooldown claim). The dispatcher (event→action, the two doors) lands in T3.
The pure catalogue/filters/actions/settings live in :mod:`persona.events`; the ``EventFire`` leg
trigger this registry produces lives in :mod:`persona.tasks`.
"""

from __future__ import annotations

from persona_api.events.candidate_handler import (
    EVENT_CANDIDATE_JOB_TYPE,
    EventCandidateHandler,
    EventCandidatePayload,
    EventCandidateProducer,
    EventWellbeingCheck,
    register_event_candidate_handler,
)
from persona_api.events.candidate_producer import (
    EVENT_CANDIDATE_PROMPT_VERSION,
    ApiEventWellbeingCheck,
    SmallTierEventCandidateProducer,
)
from persona_api.events.connector_hooks import (
    make_connector_linked_emit,
    make_message_received_emit,
    on_connector_unlinked,
)
from persona_api.events.dispatcher import (
    DispatchDisposition,
    DispatchOutcome,
    EventDispatcher,
    EventDispatchError,
)
from persona_api.events.lifecycle import LifecycleEmitter
from persona_api.events.provenance import (
    CANDIDATE_WELLBEING_DROPPED_METADATA_KEYS,
    DEPTH_EXCEEDED_METADATA_KEYS,
    EVENT_FIRE_IDENTITY_FIELDS,
    EVENT_TRIGGER_AUDIT_ACTIONS,
    EVENT_TRIGGER_CANDIDATE_WELLBEING_DROPPED,
    EVENT_TRIGGER_DEPTH_EXCEEDED,
    EVENT_TRIGGER_FIRED,
    EVENT_TRIGGER_LOOP_REFUSED,
    EVENT_TRIGGER_STORM_DROPPED,
    EVENT_TRIGGER_TASK_MISSING,
    EVENT_TRIGGER_UNGROUNDABLE,
    FIRED_METADATA_KEYS,
    LOOP_REFUSED_METADATA_KEYS,
    PROVENANCE_RENDER_FIELD,
    STORM_DROPPED_METADATA_KEYS,
)
from persona_api.events.store import EventTriggerRecord, EventTriggerStore, FireClaim
from persona_api.events.wiring import build_event_dispatcher

__all__ = [
    "CANDIDATE_WELLBEING_DROPPED_METADATA_KEYS",
    "DEPTH_EXCEEDED_METADATA_KEYS",
    "EVENT_CANDIDATE_JOB_TYPE",
    "EVENT_CANDIDATE_PROMPT_VERSION",
    "EVENT_FIRE_IDENTITY_FIELDS",
    "EVENT_TRIGGER_AUDIT_ACTIONS",
    "EVENT_TRIGGER_CANDIDATE_WELLBEING_DROPPED",
    "EVENT_TRIGGER_DEPTH_EXCEEDED",
    "EVENT_TRIGGER_FIRED",
    "EVENT_TRIGGER_LOOP_REFUSED",
    "EVENT_TRIGGER_STORM_DROPPED",
    "EVENT_TRIGGER_TASK_MISSING",
    "EVENT_TRIGGER_UNGROUNDABLE",
    "FIRED_METADATA_KEYS",
    "LOOP_REFUSED_METADATA_KEYS",
    "PROVENANCE_RENDER_FIELD",
    "STORM_DROPPED_METADATA_KEYS",
    "ApiEventWellbeingCheck",
    "DispatchDisposition",
    "DispatchOutcome",
    "EventCandidateHandler",
    "EventCandidatePayload",
    "EventCandidateProducer",
    "EventDispatchError",
    "EventDispatcher",
    "EventTriggerRecord",
    "EventTriggerStore",
    "EventWellbeingCheck",
    "FireClaim",
    "LifecycleEmitter",
    "SmallTierEventCandidateProducer",
    "build_event_dispatcher",
    "make_connector_linked_emit",
    "make_message_received_emit",
    "on_connector_unlinked",
    "register_event_candidate_handler",
]
