"""Emit A2 task-lifecycle events from a settled leg — the loop-critical chain inheritance (A7, T4).

When a leg finishes, the worker calls :meth:`LifecycleEmitter.on_leg_settled` with the settled
outcome AND the leg's originating trigger. The emitter turns that into the matching
:class:`~persona.events.Event` (``task.leg_completed`` / ``task.milestone``) and dispatches it — and
here is the point of loop prevention: if the leg was fired by an :class:`~persona.tasks.EventFire`,
the emitted event **inherits that trigger's ``causal_chain``**. So a task that (directly or through
a chain of triggers) caused its own lifecycle event re-enters the dispatcher already carrying its
trigger, and the loop guard refuses it (A7-D-4, criterion 4). A clock/reply-fired leg carries an
empty chain — its lifecycle event is organic and may legitimately trigger work.

This is the ONE place the chain crosses the worker boundary back into the event stream; the
connector-inbound + link/unlink emitters (T6) never inherit a chain (their events are organic).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.events import TaskLegCompleted, TaskMilestone
from persona.logging import get_logger
from persona.tasks import EventFire
from persona_runtime.legs import LegDisposition

if TYPE_CHECKING:
    from datetime import datetime

    from persona.events import Event
    from persona.tasks import ResumeTrigger
    from persona_runtime.legs import LegOutcome

    from persona_api.events.dispatcher import EventDispatcher

__all__ = ["LifecycleEmitter"]

_log = get_logger("api.events.lifecycle")


class LifecycleEmitter:
    """Turn a settled leg into a dispatched lifecycle event, inheriting the trigger chain."""

    def __init__(self, *, dispatcher: EventDispatcher) -> None:
        self._dispatcher = dispatcher

    async def on_leg_settled(
        self, outcome: LegOutcome, trigger: ResumeTrigger, now: datetime
    ) -> None:
        """Emit + dispatch the lifecycle event this leg produced (chain inherited from EventFire).

        Best-effort by contract (the leg handler isolates + swallows failures): a dispatch hiccup
        must never fail the already-done leg. Only COMPLETED / timed-wait legs emit here;
        ``task.leg_failed`` is the dead-letter sweep's event (T6), not a settled-leg outcome.
        """
        event = self._event_for(outcome, trigger, now)
        if event is None:
            return
        self._dispatcher.dispatch(event, now=now)

    @staticmethod
    def _event_for(outcome: LegOutcome, trigger: ResumeTrigger, now: datetime) -> Event | None:
        # The inherited causal chain — the loop marker. Empty for a clock/reply-fired leg (organic).
        chain = trigger.causal_chain if isinstance(trigger, EventFire) else ()
        task = outcome.task
        seq = task.head_checkpoint_seq if task.head_checkpoint_seq is not None else 0
        if outcome.disposition is LegDisposition.COMPLETED:
            return TaskLegCompleted(
                event_id=f"task.leg_completed:{task.id}:{seq}",
                owner_id=task.owner_id,
                occurred_at=now,
                causal_chain=chain,
                task_id=task.id,
                persona_id=task.persona_id,
            )
        if outcome.disposition is LegDisposition.CONTINUE and outcome.resume_at is not None:
            # A timed-wait boundary is a milestone (the task paused until its next check).
            return TaskMilestone(
                event_id=f"task.milestone:{task.id}:{seq}",
                owner_id=task.owner_id,
                occurred_at=now,
                causal_chain=chain,
                task_id=task.id,
                persona_id=task.persona_id,
            )
        # CONTINUE (immediate) / WAITING_APPROVAL / FAILED are not lifecycle-trigger events here.
        return None
