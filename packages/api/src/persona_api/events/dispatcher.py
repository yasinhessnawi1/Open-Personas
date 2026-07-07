"""The A7 event→action dispatcher — match at the birth point, act through the two doors.

:class:`EventDispatcher` is the whole action surface of A7 (A7-D-5). Given a typed
:class:`~persona.events.Event`, it looks up the owner's ENABLED triggers for that kind (one indexed
:meth:`~persona_api.events.store.EventTriggerStore.list_active` — no model call), applies each typed
filter, and for every match runs a fixed gate chain before routing to **exactly one of two doors**:

1. **loop guard** (A7-D-4) — refuse a match whose trigger is already in the event's causal chain,
   AND refuse once the chain reaches ``max_chain_depth`` (the distinct-trigger-cycle backstop). Both
   are pre-claim, so a refusal never consumes the cooldown window;
2. **autonomy pause** — a paused owner's triggers do not fire (the A6-D-8 seam; the composition
   roots bind the real ``KillSwitchStore.is_owner_autonomy_paused`` reader, wired at A6 merge-back);
3. **storm claim** — the atomic per-trigger cooldown claim (A7-D-4): a burst coalesces into one
   fire carrying a count; a non-winning arrival is dropped (coalesced), never fired;
4. **R7 budget** — the denial-of-wallet pre-check before every fire (A7-D-8): over-cap ⇒
   drop-with-audit **and surfaced**, never silent;
5. **the door** — ``FireTaskLeg`` fires a confirmed task's leg (the A1→A2 bridge with an
   :class:`~persona.tasks.EventFire`); ``EnqueueInitiativeCandidate`` enqueues an A5 candidate
   (the ``event_candidate`` job → the unchanged pipeline).

The ``causal_chain`` is what makes loop prevention work across processes: it rides in the
``EventFire`` on the queued leg, and a leg fired by an event **inherits** it into the lifecycle
event its completion emits (:class:`~persona_api.events.lifecycle.LifecycleEmitter`) — so a
self-triggering task's own output re-enters here already carrying its trigger, and is refused.

There is deliberately **no third door**: the routing is exhaustive over the closed two-member
``TriggerAction`` union, and :data:`~persona.events.TRIGGER_ACTION_KINDS` + the criterion-2
structural test forbid a third. A7 never executes work inline — it enqueues into the existing
worker (same caps, same audit).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from persona.errors import PersonaError, TaskNotFoundError
from persona.events import (
    ConnectorLinked,
    ConnectorMessageReceived,
    ConnectorUnlinked,
    EnqueueInitiativeCandidate,
    Event,
    FireTaskLeg,
    TaskLegCompleted,
    TaskLegFailed,
    TaskMilestone,
    event_kind_of,
)
from persona.logging import get_logger
from persona.tasks import EventFire

from persona_api.events.candidate_handler import EVENT_CANDIDATE_JOB_TYPE, EventCandidatePayload
from persona_api.tasks.handler import enqueue_task_leg

if TYPE_CHECKING:
    from datetime import datetime

    from persona.events import TriggerAction

    from persona_api.events.store import EventTriggerRecord, EventTriggerStore
    from persona_api.jobs.queue import JobQueue
    from persona_api.tasks.store import TaskStore

__all__ = [
    "DispatchDisposition",
    "DispatchOutcome",
    "EventDispatcher",
    "EventDispatchError",
]

_log = get_logger("api.events.dispatcher")

#: Consult the owner's autonomy-pause state (A6-D-8). Default no-op — A6 injects the real
#: ``owner_autonomy_pause`` reader at merge-back. ``True`` ⇒ the owner is paused; do not fire.
PauseCheck = Callable[[str], bool]
#: Book one fire against R7's day-cap (A7-D-8). ``True`` ⇒ under cap (proceed); ``False`` ⇒ over-cap
#: (drop-with-audit + surface). Default allow (unlimited); T6 wires ``book_day_spend``.
BudgetCheck = Callable[[str], bool]
#: Append one audit row (owner, action, target, metadata) — the A6-render + forensics trail.
AuditSink = Callable[[str, str, str, "Mapping[str, str] | None"], None]
#: Surface a dropped fire to the user (a durable P6 bell) — (owner, trigger_id, human). Storm drops
#: are never silent (A7-D-8); the trigger_id keys the bell so re-drops of a trigger coalesce.
SurfaceDrop = Callable[[str, str, str], None]


class EventDispatchError(PersonaError):
    """A dispatch reached an impossible branch (a third action door) — the structural backstop."""


class DispatchDisposition(StrEnum):
    """What happened to one matched trigger (the audited outcome)."""

    FIRED = "fired"
    COALESCED = "coalesced"  # a burst arrival folded into the window's one fire
    DROPPED_OVER_CAP = "dropped_over_cap"  # R7 refused (audited + surfaced)
    PAUSED = "paused"  # the owner's autonomy is paused (A6-D-8)
    LOOP_REFUSED = "loop_refused"  # the trigger is already in the event's causal chain
    DEPTH_EXCEEDED = "depth_exceeded"  # the chain hit max_chain_depth (distinct-trigger cycle)
    TASK_MISSING = "task_missing"  # door-a's task vanished mid-flight (CASCADE race) — benign
    UNGROUNDABLE = "ungroundable"  # door-b had no citable grounding for the event


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """The result of dispatching one matched trigger (returned for audit/testing)."""

    trigger_id: str
    disposition: DispatchDisposition
    action_kind: str | None = None
    coalesced_count: int = 0


def _default_pause_check(_owner_id: str) -> bool:
    return False


def _default_budget_check(_owner_id: str) -> bool:
    return True


class EventDispatcher:
    """Match an event to an owner's triggers and act through the two doors (A7-D-5)."""

    def __init__(
        self,
        *,
        store: EventTriggerStore,
        queue: JobQueue,
        task_store: TaskStore,
        settings_cooldown_seconds: int,
        settings_max_chain_depth: int,
        audit: AuditSink,
        surface_drop: SurfaceDrop,
        pause_check: PauseCheck = _default_pause_check,
        budget_check: BudgetCheck = _default_budget_check,
    ) -> None:
        self._store = store
        self._queue = queue
        self._tasks = task_store
        self._cooldown_seconds = settings_cooldown_seconds
        self._max_chain_depth = settings_max_chain_depth
        self._audit = audit
        self._surface_drop = surface_drop
        self._pause_check = pause_check
        self._budget_check = budget_check

    def dispatch(self, event: Event, *, now: datetime) -> list[DispatchOutcome]:
        """Match ``event`` against its owner's active triggers and act on each match.

        Returns one :class:`DispatchOutcome` per matched trigger (empty when nothing matches). A
        non-match is silent (the common case — most events match no trigger); every *match* produces
        an outcome and, except for a coalesced/paused/refused one, an audited fire.
        """
        kind = event_kind_of(event)
        outcomes: list[DispatchOutcome] = []
        for trigger in self._store.list_active(event.owner_id, kind):
            if not trigger.filter.matches(event):
                continue
            outcomes.append(self._act(trigger, event, now=now))
        return outcomes

    def _act(self, trigger: EventTriggerRecord, event: Event, *, now: datetime) -> DispatchOutcome:
        owner = event.owner_id
        # 1. Loop guard (A7-D-4): a trigger already in this event's causal chain cannot fire again —
        #    the self-triggering cycle defence, audited (criterion 4). Pre-claim: a refusal never
        #    consumes the cooldown window.
        if trigger.id in event.causal_chain:
            self._audit(
                owner,
                "event_trigger.loop_refused",
                trigger.id,
                {"event_id": event.event_id, "chain": ",".join(event.causal_chain)},
            )
            return DispatchOutcome(trigger.id, DispatchDisposition.LOOP_REFUSED)
        # 2. Depth cap (A7-D-4): the belt-and-braces backstop for a cycle of DISTINCT triggers
        #    (each new, so the chain-contains guard never fires) — bounded far below a spend event.
        if len(event.causal_chain) >= self._max_chain_depth:
            self._audit(
                owner,
                "event_trigger.depth_exceeded",
                trigger.id,
                {"event_id": event.event_id, "depth": str(len(event.causal_chain))},
            )
            return DispatchOutcome(trigger.id, DispatchDisposition.DEPTH_EXCEEDED)
        # 3. Autonomy pause (A6-D-8): a paused owner does not fire — checked BEFORE the claim so a
        #    pause never consumes the cooldown window.
        if self._pause_check(owner):
            return DispatchOutcome(trigger.id, DispatchDisposition.PAUSED)
        # 4. Storm claim (A7-D-4): the atomic cooldown/coalesce gate — only the window winner fires.
        claim = self._store.claim_fire(
            owner, trigger.id, now=now, cooldown_seconds=self._cooldown_seconds
        )
        if not claim.fired:
            return DispatchOutcome(
                trigger.id, DispatchDisposition.COALESCED, coalesced_count=claim.coalesced_count
            )
        # 5. R7 budget (A7-D-8): the denial-of-wallet ceiling, consulted before every fire. Over-cap
        #    ⇒ drop-with-audit AND surface (never silent). The cooldown was already consumed — a
        #    dropped fire costs one window, an accepted degrade.
        if not self._budget_check(owner):
            self._audit(
                owner,
                "event_trigger.storm_dropped",
                trigger.id,
                {"reason": "over_day_cap", "coalesced_count": str(claim.coalesced_count)},
            )
            self._surface_drop(
                owner, trigger.id, self._human(trigger, event, claim.coalesced_count)
            )
            return DispatchOutcome(
                trigger.id,
                DispatchDisposition.DROPPED_OVER_CAP,
                coalesced_count=claim.coalesced_count,
            )
        # 6. The door.
        return self._route(trigger, event, now=now, coalesced_count=claim.coalesced_count)

    def _route(
        self,
        trigger: EventTriggerRecord,
        event: Event,
        *,
        now: datetime,
        coalesced_count: int,
    ) -> DispatchOutcome:
        """Route a fired trigger to exactly one of the two doors (exhaustive over the union)."""
        action: TriggerAction = trigger.action
        human = self._human(trigger, event, coalesced_count)
        chain = (*event.causal_chain, trigger.id)
        if isinstance(action, FireTaskLeg):
            return self._fire_task_leg(
                trigger, event, action, now=now, human=human, chain=chain, coalesced=coalesced_count
            )
        if isinstance(action, EnqueueInitiativeCandidate):
            return self._enqueue_candidate(
                trigger, event, human=human, chain=chain, coalesced=coalesced_count
            )
        # Unreachable: the closed two-member union (criterion 2). The structural test proves no
        # third variant exists; a future un-handled door becomes a loud failure, not a silent
        # side-effect.
        msg = f"no dispatch door for action kind {action.kind!r}"
        raise EventDispatchError(msg)

    def _fire_task_leg(
        self,
        trigger: EventTriggerRecord,
        event: Event,
        action: FireTaskLeg,
        *,
        now: datetime,
        human: str,
        chain: tuple[str, ...],
        coalesced: int,
    ) -> DispatchOutcome:
        """Door (a): fire the confirmed task's next leg (the A1→A2 bridge with an ``EventFire``)."""
        owner = event.owner_id
        try:
            task = self._tasks.get(owner, action.task_id)
        except TaskNotFoundError:
            # The task vanished between match and fire (a CASCADE race — persona/task deletion also
            # CASCADE-removes the trigger, so this is a narrow window). Benign: no leg, no retry.
            self._audit(
                owner, "event_trigger.task_missing", action.task_id, {"trigger": trigger.id}
            )
            return DispatchOutcome(trigger.id, DispatchDisposition.TASK_MISSING)
        fire = EventFire(
            trigger_id=trigger.id,
            event_kind=str(event_kind_of(event)),
            event_id=event.event_id,
            fired_at=now,
            human=human,
            causal_chain=chain,
        )
        enqueue_task_leg(
            self._queue,
            owner_id=owner,
            task_id=action.task_id,
            predecessor_seq=task.head_checkpoint_seq,  # the A2-R-4 head anchor
            trigger=fire,
        )
        self._audit(
            owner,
            "event_trigger.fired",
            action.task_id,
            {
                "trigger_id": trigger.id,
                "event_kind": str(event_kind_of(event)),
                "event_id": event.event_id,
                "human": human,
                "door": "fire_task_leg",
            },
        )
        return DispatchOutcome(
            trigger.id,
            DispatchDisposition.FIRED,
            action_kind=action.kind,
            coalesced_count=coalesced,
        )

    def _enqueue_candidate(
        self,
        trigger: EventTriggerRecord,
        event: Event,
        *,
        human: str,
        chain: tuple[str, ...],
        coalesced: int,
    ) -> DispatchOutcome:
        """Door (b): enqueue an A5 candidate grounded on the event (the ``event_candidate`` job)."""
        owner = event.owner_id
        grounding = _grounding_for(event)
        if grounding is None:
            # A link event has no citable conversation/task — it cannot ground an A5 candidate
            # (A7-D-7 reuses existing citations). Audited, not silent; door-a is its usable form.
            self._audit(
                owner, "event_trigger.ungroundable", trigger.id, {"event_id": event.event_id}
            )
            return DispatchOutcome(trigger.id, DispatchDisposition.UNGROUNDABLE)
        grounding_kind, grounding_ref = grounding
        payload = EventCandidatePayload(
            persona_id=trigger.persona_id,
            event_kind=str(event_kind_of(event)),
            event_id=event.event_id,
            trigger_id=trigger.id,
            human=human,
            causal_chain=chain,
            grounding_kind=grounding_kind,
            grounding_ref=grounding_ref,
        )
        self._queue.enqueue(
            type=EVENT_CANDIDATE_JOB_TYPE,
            owner_id=owner,
            payload=payload.model_dump(mode="json"),
            idempotency_key=f"evtcand:{event.event_id}:{trigger.id}",
        )
        self._audit(
            owner,
            "event_trigger.fired",
            trigger.id,
            {
                "trigger_id": trigger.id,
                "event_kind": str(event_kind_of(event)),
                "event_id": event.event_id,
                "human": human,
                "door": "enqueue_candidate",
            },
        )
        return DispatchOutcome(
            trigger.id,
            DispatchDisposition.FIRED,
            action_kind=trigger.action.kind,
            coalesced_count=coalesced,
        )

    @staticmethod
    def _human(trigger: EventTriggerRecord, event: Event, coalesced_count: int) -> str:  # noqa: ARG004 — trigger reserved for richer wording later
        """The legible "ran because" string A6 renders (route (b) of A7-D-9)."""
        base = _describe(event)
        if coalesced_count > 0:
            return f"{base} (and {coalesced_count} more)"
        return base


def _describe(event: Event) -> str:
    """A one-line human description of what happened (the "ran because" body)."""
    if isinstance(event, ConnectorMessageReceived):
        return f"a message from {event.sender_id} arrived on {event.platform}"
    if isinstance(event, TaskLegCompleted):
        return "the task completed a step"
    if isinstance(event, TaskLegFailed):
        return f"the task failed ({event.failure_count}×)"
    if isinstance(event, TaskMilestone):
        return "the task reached a milestone"
    if isinstance(event, ConnectorLinked):
        return f"{event.platform} was linked"
    if isinstance(event, ConnectorUnlinked):
        return f"{event.platform} was unlinked"
    msg = "unreachable: unknown event kind"  # pragma: no cover
    raise EventDispatchError(msg)  # pragma: no cover


def _grounding_for(event: Event) -> tuple[str, str] | None:
    """The (citation-kind, ref) an event-sourced candidate grounds on, or ``None`` if ungroundable.

    A message is already a conversation turn (cite ``conversation``); a lifecycle event cites its
    ``task``; a link event has no citable anchor (A7-D-7 reuses existing citation kinds).
    """
    if isinstance(event, ConnectorMessageReceived):
        return ("conversation", event.conversation_id)
    if isinstance(event, TaskLegCompleted | TaskLegFailed | TaskMilestone):
        return ("task", event.task_id)
    return None
