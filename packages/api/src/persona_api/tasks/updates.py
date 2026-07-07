"""Digest-granularity task updates (Spec A4, T10; A4-D-3, A4-D-6).

The conversational presence of work: task activity reaches the user as **digest-shaped** C0
updates at the contract's chosen granularity, on the contract's preferred channel — not as
leg-by-leg chatter (transcript hygiene). :func:`should_deliver_update` is the granularity filter;
:class:`TaskUpdatePublisher` composes it with the C0 originator.

The load-bearing invariant (A4-D-3): granularity governs the **progress** class only. A3's
**always-pass** classes — approval / failure / safety — bypass it (``bypasses_cap``), so even a
``quiet`` task still reaches the user when it needs them or something goes wrong. "Update silence"
can never hide a failure or a request for the user; it can only quiet routine progress.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from persona.tasks import UpdateGranularity

from persona_api.approvals.cadence import CadenceDecision, MessagePriority, bypasses_cap

if TYPE_CHECKING:
    from datetime import datetime

    from persona.schema.origination import PersonaIdentityTag
    from persona.tasks import Task

    from persona_api.approvals.cadence import CadenceGate, DigestSink

__all__ = ["TaskUpdatePublisher", "UpdateSender", "should_deliver_update"]


def should_deliver_update(
    *,
    priority: MessagePriority,
    granularity: UpdateGranularity,
    is_milestone: bool,
    is_completion: bool,
) -> bool:
    """Whether a task update reaches the user under its contract's granularity (A4-D-3).

    An always-pass class (approval / failure / safety) **always** delivers — the un-suppressible
    floor. A ``progress`` update is governed by the granularity: every leg, milestones only,
    completion only, or quiet (suppressed).
    """
    if bypasses_cap(priority):
        return True  # approval / failure / safety — the un-suppressible floor
    if granularity is UpdateGranularity.EVERY_LEG:
        return True
    if granularity is UpdateGranularity.MILESTONES:
        return is_milestone
    if granularity is UpdateGranularity.COMPLETION_ONLY:
        return is_completion
    return False  # QUIET — routine progress is held back (but the floor above still applies)


class UpdateSender(Protocol):
    """Sends one task update as a persona-voiced C0 message on a chosen channel.

    The real implementation composes the C0 :class:`~persona.originator.Originator` +
    C0/C1's ``DeliveryRouter`` (the ``channel`` feeds ``resolve_channel``; ``None`` → home).
    """

    async def send(
        self,
        *,
        persona: PersonaIdentityTag,
        owner_id: str,
        content: str,
        channel: str | None,
        conversation_id: str | None,
        priority: MessagePriority,
        now: datetime,
    ) -> None:
        """Deliver the update (persona-voiced, name-tagged) on ``channel`` (or home fallback)."""
        ...


class TaskUpdatePublisher:
    """Publishes a task update iff granularity admits it, on the contract's channel (A4-D-3/6)."""

    def __init__(
        self,
        *,
        sender: UpdateSender,
        cadence: CadenceGate | None = None,
        digest_sink: DigestSink | None = None,
    ) -> None:
        """Inject the C0-composed sender + (optionally) the A3-D-4 cadence cap and its digest sink.

        With ``cadence`` + ``digest_sink`` wired, a *progress* update that the granularity filter
        admits is still capped per persona/day: over the cap it batches to the sink (A6's morning
        review) instead of delivering now — the "chatter batches to the digest" contract. Without
        them (unit tests / a plain worker) the pre-cadence behaviour holds: admitted → delivered.
        """
        self._sender = sender
        self._cadence = cadence
        self._digest_sink = digest_sink

    async def publish(
        self,
        *,
        task: Task,
        persona: PersonaIdentityTag,
        priority: MessagePriority,
        is_milestone: bool,
        is_completion: bool,
        content: str,
        now: datetime,
    ) -> bool:
        """Decide + (if admitted) send. Returns whether the update was delivered.

        Granularity + channel come from the contract's :class:`UpdatePreference` (default:
        milestones, home channel). The always-pass floor overrides granularity (A4-D-3).
        """
        pref = task.contract.updates
        granularity = pref.granularity if pref is not None else UpdateGranularity.MILESTONES
        channel = pref.channel if pref is not None else None
        if not should_deliver_update(
            priority=priority,
            granularity=granularity,
            is_milestone=is_milestone,
            is_completion=is_completion,
        ):
            return False
        # A3-D-4 cadence: a progress message the granularity admits is still capped per persona/day.
        # Over the cap it batches to the digest sink (the morning review) rather than delivering now
        # — so a chatty task's routine progress never spams, but nothing is silently dropped. The
        # bypass classes (approval/failure/safety) always DELIVER (admit never counts them).
        if self._cadence is not None and self._digest_sink is not None:
            decision = self._cadence.admit(task.owner_id, task.persona_id, priority, now=now)
            if decision is CadenceDecision.DIGEST:
                self._digest_sink.defer(task.owner_id, task.persona_id, content, now=now)
                return False
        await self._sender.send(
            persona=persona,
            owner_id=task.owner_id,
            content=content,
            channel=channel,
            conversation_id=task.conversation_id,
            priority=priority,
            now=now,
        )
        return True
