"""Door (b): the ``event_candidate`` job — an event becomes an A5 initiative candidate (Spec A7).

The dispatcher's second door enqueues this job (cross-process safe: message events are born in the
connector service, the A5 pipeline lives only in the worker). The worker-side handler turns the
event provenance into a scored :class:`~persona.initiative.InitiativeCandidate` via an injected
:class:`EventCandidateProducer`, then submits it to the **unchanged** A5 pipeline (grounding →
envelope → restraint → K4). A7 plugs in without touching a single A5 gate (A7-D-7).

The concrete model-backed producer (and the K4 criterion-8 adversarial fixture over it) lands in a
later task; T3 ships the job type, the payload, the handler, and the producer seam — proven live
end-to-end against a test producer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.jobs import MEDIUM_LEASE, JobPayload, JobTypeSpec, RetryPolicy
from persona.logging import get_logger

if TYPE_CHECKING:
    from persona.initiative import InitiativeCandidate
    from persona.jobs import JobContext, JobRegistry

    from persona_api.initiative.handler import CandidateSink

__all__ = [
    "EVENT_CANDIDATE_JOB_TYPE",
    "EventCandidateHandler",
    "EventCandidatePayload",
    "EventCandidateProducer",
    "EventWellbeingCheck",
    "register_event_candidate_handler",
]

EVENT_CANDIDATE_JOB_TYPE = "event_candidate"

_log = get_logger("api.events.candidate")

#: Append one audit row (owner, action, target, metadata) — the no-silent-skips trail A6 renders.
#: Mirrors the dispatcher's ``AuditSink`` shape; defined here to keep the import acyclic (the
#: dispatcher imports this module).
AuditSink = Callable[[str, str, str, "Mapping[str, str] | None"], None]


class EventCandidatePayload(JobPayload):
    """The event provenance an ``event_candidate`` job carries into the A5 pipeline.

    ``grounding_kind``/``grounding_ref`` name the existing store citation the candidate grounds on
    (A7-D-7: ``conversation`` for a message turn, ``task`` for a lifecycle event) — no new
    ``CitationKind``. ``causal_chain`` rides through so a candidate born of a triggered action keeps
    the loop provenance. Owner comes from the job context (RLS scope), not the payload.
    """

    persona_id: str
    event_kind: str
    event_id: str
    trigger_id: str
    human: str
    causal_chain: tuple[str, ...]
    grounding_kind: str
    grounding_ref: str


@runtime_checkable
class EventCandidateProducer(Protocol):
    """Turns event provenance into a scored A5 candidate (or ``None`` — ungroundable / gated).

    The event analogue of the A5 scanner: it resolves the cited grounding, scores value/acceptance,
    and stamps ``source=EVENT``. ``None`` means "raise nothing" (a thin event is success, exactly as
    a thin scan is). The concrete small-tier implementation lands with the K4 fixture (criterion 8).
    """

    async def produce(
        self, payload: EventCandidatePayload, context: JobContext
    ) -> InitiativeCandidate | None: ...


@runtime_checkable
class EventWellbeingCheck(Protocol):
    """Layer (a) of criterion 8 — the deterministic wellbeing subject-exclusion (A7-D-7).

    ``True`` iff the event's grounding is a wellbeing-gated subject (a flagged node was sourced from
    it). The api adapter (:class:`~persona_api.events.candidate_producer.ApiEventWellbeingCheck`)
    reads the graph flag set — the SAME set the pipeline's own subject rule consults, so this does
    not shrink the real residual.
    """

    def is_gated_subject(self, owner_id: str, grounding_kind: str, grounding_ref: str) -> bool: ...


class EventCandidateHandler:
    """Enforce the wellbeing gate (layer a), run the producer, submit any candidate to A5.

    The wellbeing exclusion lives HERE — at the seam, before any producer runs — so it is structural
    for every door-b candidate ever, not producer discretion (the impossible-green doctrine; the
    binding T8 refinement). A gated grounding is dropped with an audit row (never silent) and the
    model is never even consulted.
    """

    def __init__(
        self,
        *,
        producer: EventCandidateProducer,
        sink: CandidateSink,
        wellbeing: EventWellbeingCheck,
        audit: AuditSink,
    ) -> None:
        self._producer = producer
        self._sink = sink
        self._wellbeing = wellbeing
        self._audit = audit

    async def handle(self, payload: EventCandidatePayload, context: JobContext) -> None:
        """Gate → produce → submit (the A5 gates decide the rest of any surviving candidate)."""
        owner = context.owner_id
        # Layer (a) — criterion 8: gated-category grounding never reaches the producer or the
        # pipeline. Deterministic, structural, before any model call; audited, never silent.
        if self._wellbeing.is_gated_subject(owner, payload.grounding_kind, payload.grounding_ref):
            self._audit(
                owner,
                "event_trigger.candidate_wellbeing_dropped",
                payload.trigger_id,
                {
                    "event_id": payload.event_id,
                    "grounding": f"{payload.grounding_kind}/{payload.grounding_ref}",
                },
            )
            _log.info(
                "door-b candidate dropped: wellbeing-gated grounding (criterion 8)",
                event_id=payload.event_id,
                trigger_id=payload.trigger_id,
            )
            return
        candidate = await self._producer.produce(payload, context)
        if candidate is None:
            _log.info(
                "event candidate produced nothing (thin event)",
                event_id=payload.event_id,
                trigger_id=payload.trigger_id,
            )
            return
        await self._sink.submit((candidate,))


def register_event_candidate_handler(
    registry: JobRegistry,
    *,
    producer: EventCandidateProducer,
    sink: CandidateSink,
    wellbeing: EventWellbeingCheck,
    audit: AuditSink,
) -> None:
    """Register the ``event_candidate`` tenant (door (b)'s worker-side pipeline entry)."""
    registry.register(
        JobTypeSpec(
            type=EVENT_CANDIDATE_JOB_TYPE,
            payload_model=EventCandidatePayload,
            handler=EventCandidateHandler(
                producer=producer, sink=sink, wellbeing=wellbeing, audit=audit
            ),
            idempotency_key=lambda p: f"evtcand:{p.event_id}:{p.trigger_id}",
            retry=RetryPolicy(max_attempts=2),
            lease=MEDIUM_LEASE,
        )
    )
