"""Act-then-report delivery — the implicit A2 task (Spec A5, T8; A5-D-X-delivery-and-doors).

An ``ACT`` delivery creates a **lightweight implicit task** and lets the
EXISTING machinery do everything else — that is the whole design:

- the task + its run-once-NOW one-time schedule are the SAME rows A4's
  origination creates (Option B: every task is schedule-backed and actually
  executes — never an inert row); the real A1 tick fires it through the real
  A1→A2 bridge into a real leg;
- the leg runs under the ``PolicyGatedToolbox`` with the task's category
  policy — a step that would cross a gate mid-leg converts to an A3 approval,
  never a bypass;
- the user-facing REPORT is authored by the task machinery alone (the A4
  milestone hook → C0 ``Originator`` → A3's ``CadenceGate``): **this module
  writes no message, no notification, ever** — which is the structural form of
  the honesty bar: A5 cannot voice work as done because A5 never voices; the
  task machinery reports what actually happened (and a failure surfaces as
  A3's always-pass FAILURE class).

``PROPOSE`` deliveries (T9, Option C ruling) take one of two REAL doors:

- a candidate carrying a structured ``schedule_change`` routes through **A8's
  propose-first door verbatim** (``propose_reschedule`` with a deterministic
  proposal id): a PENDING proposal, ZERO schedule writes until a user-resolved
  ``apply_proposal`` runs — the pending state lives on A8's machinery
  (reload-durable), untouched by the A4 metadata gap (state.md);
- every other proposal is delivered as a persona-voiced C0 message through the
  injected sender (the SAME ``OriginatorUpdateSender`` composition A4's digests
  use — one origination door, nothing new) with a versioned template carrying
  the honest why. It creates NOTHING: the confirmation surface is T10's
  ledger-anchored verb; unconfirmed ⇒ zero tasks, zero schedules, zero fires.

Idempotent by construction: ids derive deterministically from the notice id
(one notice = one task = one schedule); a replayed delivery converges on the
existing rows and reports success without duplicating. Fail-soft: any creation
failure compensates a partial schedule and returns ``False`` — the notice
stays HELD for the next flush; the tick never crashes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from persona.errors import (
    InvalidRecurrenceRuleError,
    ScheduleNotFoundError,
    TaskNotFoundError,
)
from persona.initiative.envelope import EnvelopeAction
from persona.logging import get_logger
from persona.schedules import MissedFirePolicy, RecurrenceRule, Schedule
from persona.tasks import Contract, Task, TaskState, WaitKind

from persona_api.approvals.cadence import MessagePriority
from persona_api.schedules.reschedule import propose_reschedule
from persona_api.tasks.scheduled_fire import TASK_SCHEDULED_FIRE_JOB_TYPE

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.initiative import InitiativeCandidate
    from persona.schema.origination import PersonaIdentityTag
    from sqlalchemy import Engine

    from persona_api.initiative.store import InitiativeLedger, NoticeRecord
    from persona_api.schedules import ScheduleStore
    from persona_api.tasks.store import TaskStore

__all__ = [
    "INITIATIVE_PROPOSAL_TEMPLATE_VERSION",
    "InitiativeDeliveryExecutor",
    "ProposalSender",
    "implicit_task_id",
    "initiative_reschedule_proposal_id",
    "render_proposal",
]

_log = get_logger("api.initiative.delivery")


def implicit_task_id(notice_id: str) -> str:
    """The deterministic implicit-task id — one notice, one task (replay-safe)."""
    return f"itask_{notice_id}"


def _implicit_schedule_id(notice_id: str) -> str:
    return f"itasksched_{notice_id}"


def initiative_reschedule_proposal_id(notice_id: str) -> str:
    """The deterministic A8-proposal id — one notice, one proposal (replay-safe)."""
    return f"iresched_{notice_id}"


#: Bumped on any wording change (the Spec-10 discipline; A5-R-1 tunes the tone).
INITIATIVE_PROPOSAL_TEMPLATE_VERSION = "a5-propose-v1"


def render_proposal(candidate: InitiativeCandidate) -> str:
    """The persona-voiced proposal text — honest why, explicit ask (criterion 8).

    The attribution ("from your notes") is the human-readable why K3's rule
    demands: unprompted contact WITH the reason reads as attentiveness. The ask
    is explicit and decline-friendly — a proposal is a question, never a nudge.
    """
    ask = candidate.next_step.rstrip(".")
    ask = ask[0].lower() + ask[1:] if ask else ask
    return (
        f"From your notes: {candidate.observation} {candidate.why_now}\n"
        f"Want me to {ask}? Say yes and I'll get started, or tell me to leave it."
    )


class ProposalSender(Protocol):
    """The C0 send seam for generic proposals (api: ``OriginatorUpdateSender``).

    The SAME composition A4's digests ride (one origination door); ``None`` at
    composition (no memory backend/edition — the community no-messaging posture)
    keeps generic proposals HELD, exactly like the digest path's absence.
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
        """Originate one persona-voiced message (durable conversation write + delivery)."""
        ...


def _build_contract(candidate: InitiativeCandidate) -> Contract:
    """Author the implicit task's contract from the candidate (A5's authorship).

    The DEFAULT category policy rides along untouched: free categories run,
    gated categories gate mid-leg through A3's machinery — the envelope already
    proved the PLAN is all-safe, and the toolbox re-enforces it per dispatch
    (defense in depth; the plan is intent, the gate is the guarantee).
    """
    scope = f"{candidate.why_now} Grounded in: " + ", ".join(c.anchor for c in candidate.citations)
    return Contract(goal=candidate.next_step, scope=scope)


class InitiativeDeliveryExecutor:
    """The pipeline's delivery seam (T8: ACT; T9 adds PROPOSE).

    Owns NO user-facing surface: it creates the task + run-once schedule rows
    and lets the task/report machinery speak.
    """

    def __init__(
        self,
        *,
        ledger: InitiativeLedger,
        tasks: TaskStore,
        schedules: ScheduleStore,
        timezone_for: str = "UTC",
        proposal_sender: ProposalSender | None = None,
        persona_tag_resolver: Callable[[str], PersonaIdentityTag | None] | None = None,
        rls_engine: Engine | None = None,
    ) -> None:
        """Inject the T3 ledger + the A2/A1 stores + the C0 proposal seam (T9).

        ``proposal_sender``/``persona_tag_resolver`` absent ⇒ generic proposals
        stay HELD (the community no-messaging posture, mirroring the digest path).
        """
        self._ledger = ledger
        self._tasks = tasks
        self._schedules = schedules
        self._timezone = timezone_for
        self._proposal_sender = proposal_sender
        self._resolve_tag = persona_tag_resolver
        self._rls_engine = rls_engine  # apply_proposal's audit engine (the verb path)

    async def deliver(self, owner_id: str, notice_id: str, action: EnvelopeAction) -> bool:
        """Deliver one claimed notice; ``False`` keeps it HELD (nothing lost).

        Never raises — a delivery failure degrades to retained-held (bar 5).
        """
        try:
            notice = self._ledger.get_notice(owner_id, notice_id)
            if notice is None:
                _log.warning("delivery for an unknown notice; skipping id={nid}", nid=notice_id)
                return False
            if action is EnvelopeAction.ACT:
                return self._create_implicit_task(owner_id, notice)
            if notice.candidate.schedule_change is not None:
                return self._propose_via_a8_door(owner_id, notice)
            return await self._send_proposal(owner_id, notice)
        except Exception:  # noqa: BLE001 — a delivery failure retains the hold, never crashes
            _log.warning("initiative delivery failed; notice stays held (fail-soft)")
            return False

    def _create_implicit_task(self, owner_id: str, notice: NoticeRecord) -> bool:
        """Task + run-once-NOW schedule, deterministic ids, replay-convergent."""
        candidate = notice.candidate
        task_id = implicit_task_id(notice.id)
        schedule_id = _implicit_schedule_id(notice.id)
        try:
            existing = self._tasks.get(owner_id, task_id)
        except TaskNotFoundError:
            existing = None
        if existing is not None:
            return True  # a replayed delivery converges — exactly one task (bar 4).
        now = datetime.now(UTC)
        # Strictly after created_at or the one-time row is inert (next_fire_after
        # is strictly-greater-than) — one second forward = "run once, now-ish";
        # the next tick (~30s cadence) picks it up.
        fire_at = now + timedelta(seconds=1)
        schedule = Schedule(
            id=schedule_id,
            owner_id=owner_id,
            timezone=self._timezone,
            one_time_at=fire_at,  # run-once-NOW (Option B): the REAL tick fires it
            target_job_type=TASK_SCHEDULED_FIRE_JOB_TYPE,
            payload_template={"task_id": task_id},
            missed_fire_policy=MissedFirePolicy.FIRE_LATE_ONCE,
            created_at=now,
            updated_at=now,
        )
        task = Task(
            id=task_id,
            owner_id=owner_id,
            persona_id=notice.persona_id,  # the ARBITRATED voicer works it (A5-D-6)
            contract=_build_contract(candidate),
            schedule_id=schedule_id,
            state=TaskState.WAITING,  # dormant until the first (only) fire resumes it
            wait_kind=WaitKind.UNTIL_TIME,
            created_at=now,
            updated_at=now,
        )
        try:
            self._ensure_schedule(schedule, now=now)
            self._ensure_task(task)
        except Exception:  # noqa: BLE001 — compensate + retain (the origination-service posture)
            _log.warning("implicit-task creation failed; compensating (fail-soft)")
            self._compensate_schedule(owner_id, schedule_id)
            return False
        return True

    async def execute_confirmed(self, owner_id: str, notice_id: str) -> bool:
        """Execute a USER-CONFIRMED proposal (the T10 verb's apply step).

        The reload-durable confirmation route (Option C): the verb gate resolved
        the pending proposal from the LEDGER; this executes it through the same
        real doors delivery uses — a ``schedule_change`` proposal resolves +
        applies through A8's CAS door WITH the user's confirmation reference;
        a generic proposal becomes the implicit task (T8's machinery, the
        deterministic-id exactly-once). Never raises; ``False`` = nothing done.
        """
        try:
            notice = self._ledger.get_notice(owner_id, notice_id)
            if notice is None:
                return False
            change = notice.candidate.schedule_change
            if change is None:
                return self._create_implicit_task(owner_id, notice)
            return self._apply_confirmed_reschedule(owner_id, notice)
        except Exception:  # noqa: BLE001 — a failed apply is a no-op, never a crash
            _log.warning("confirmed-proposal execution failed (fail-soft)")
            return False

    def _apply_confirmed_reschedule(self, owner_id: str, notice: NoticeRecord) -> bool:
        """Rebuild the deterministic pending proposal, resolve WITH the user's
        reference, apply through A8's door — the only path to a schedule write."""
        from persona.schedules import resolve_proposal

        from persona_api.schedules.reschedule import apply_proposal

        change = notice.candidate.schedule_change
        assert change is not None  # noqa: S101 — routed here only when present
        task = self._tasks.get(owner_id, change.task_id)
        if task.schedule_id is None:
            return False
        recurrence: RecurrenceRule | None = None
        if change.recurrence_rrule is not None:
            recurrence = RecurrenceRule.from_rrule_string(change.recurrence_rrule)
        now = datetime.now(UTC)
        current = self._schedules.get(owner_id, task.schedule_id)
        pending = propose_reschedule(
            self._schedules,
            proposal_id=initiative_reschedule_proposal_id(notice.id),
            owner_id=owner_id,
            schedule_id=task.schedule_id,
            persona_id=notice.persona_id,
            new_recurrence=recurrence,
            new_one_time=change.one_time_at,
            new_timezone=current.timezone,
            reason=notice.candidate.why_now,
            now=now,
        )
        if self._rls_engine is None:
            _log.warning("no engine wired for apply_proposal; refusing (stays pending)")
            return False
        confirmed = resolve_proposal(pending, approve=True, resolved_by="user_via_chat", now=now)
        apply_proposal(self._schedules, self._rls_engine, confirmed, now=now)
        return True

    def _propose_via_a8_door(self, owner_id: str, notice: NoticeRecord) -> bool:
        """Route a schedule-change proposal through A8's door (T9 bar 1, verbatim).

        ``propose_reschedule`` touches NO schedule row — a PENDING proposal only;
        the change applies exclusively through a user-resolved ``apply_proposal``
        (A8-D-12, the structural propose-first gate). The proposal id derives from
        the notice (one notice = one proposal; a replay converges — the resolution
        machinery is first-decision-wins on top). Parse honesty: an unrepresentable
        proposed cadence is refused here (False, the hold ages out), never coerced.
        """
        change = notice.candidate.schedule_change
        assert change is not None  # noqa: S101 — routed here only when present
        try:
            task = self._tasks.get(owner_id, change.task_id)
        except TaskNotFoundError:
            _log.warning("schedule-change target task missing; refusing (stays held)")
            return False
        if task.schedule_id is None:
            _log.warning("schedule-change target has no schedule; refusing (stays held)")
            return False
        recurrence: RecurrenceRule | None = None
        if change.recurrence_rrule is not None:
            try:
                recurrence = RecurrenceRule.from_rrule_string(change.recurrence_rrule)
            except (InvalidRecurrenceRuleError, ValueError):
                _log.warning("unrepresentable proposed cadence; refusing (parse honesty)")
                return False
        current = self._schedules.get(owner_id, task.schedule_id)
        propose_reschedule(
            self._schedules,
            proposal_id=initiative_reschedule_proposal_id(notice.id),
            owner_id=owner_id,
            schedule_id=task.schedule_id,
            persona_id=notice.persona_id,
            new_recurrence=recurrence,
            new_one_time=change.one_time_at,
            new_timezone=current.timezone,
            reason=notice.candidate.why_now,
            now=datetime.now(UTC),
        )
        return True

    async def _send_proposal(self, owner_id: str, notice: NoticeRecord) -> bool:
        """Deliver a generic proposal as a persona-voiced C0 message (T9 Option C).

        Creates NOTHING (the unconfirmed-creates-nothing negative): the message is
        the proposal; T10's ledger-anchored verb is the confirmation surface. No
        sender wired (community posture) or no persona tag ⇒ stays held.
        """
        if self._proposal_sender is None or self._resolve_tag is None:
            return False
        tag = self._resolve_tag(notice.persona_id)
        if tag is None:
            return False  # persona deleted between hold and flush — nothing to voice
        await self._proposal_sender.send(
            persona=tag,
            owner_id=owner_id,
            content=render_proposal(notice.candidate),
            channel=None,
            conversation_id=None,
            priority=MessagePriority.PROGRESS,
            now=datetime.now(UTC),
        )
        return True

    def _ensure_schedule(self, schedule: Schedule, *, now: datetime) -> None:
        from sqlalchemy.exc import IntegrityError

        try:
            self._schedules.get(schedule.owner_id, schedule.id)
        except ScheduleNotFoundError:
            try:
                self._schedules.create(schedule, now=now)
            except IntegrityError:
                _log.info("implicit schedule ensure raced; existing row wins")

    def _ensure_task(self, task: Task) -> None:
        from sqlalchemy.exc import IntegrityError

        try:
            self._tasks.create(task)
        except IntegrityError:
            _log.info("implicit task ensure raced; existing row wins")

    def _compensate_schedule(self, owner_id: str, schedule_id: str) -> None:
        """Remove an orphan schedule after a failed task create (no inert fires)."""
        try:
            self._schedules.delete(owner_id, schedule_id)
        except Exception:  # noqa: BLE001 — best-effort; the fire on a taskless schedule no-ops
            _log.warning("compensation failed schedule_id={sid}", sid=schedule_id)
