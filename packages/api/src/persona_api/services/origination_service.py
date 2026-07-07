"""Confirm → create: the api side of the A4 origination seam (Spec A4, T6; A4-D-X).

The chat-turn worker hands this service the ``task_originated`` event the runtime emitted on
the confirm turn; the service creates the A2 task + A1 schedule (the A3 matrix rides the
contract). It owns the two live-mutation invariants the seam introduces:

- **Failure-visibility** — a confirmed contract that fails to create is **never silent**: any
  failure surfaces a persona-voiced :class:`FailureAccount` on the cadence-bypass
  ``MessagePriority.FAILURE`` class (so even a ``quiet`` preference can't suppress it), and any
  partially-created schedule is compensated away. "Confirmed but silently nothing" is
  structurally impossible.
- **Idempotency** — the origination key is the confirm turn's ``assistant_message_id`` (primary;
  one proposal turn = one task) with the content hash as the fallback; the task + schedule ids
  are derived deterministically from it, so a re-delivered event / double-confirm / worker retry
  converge on exactly one task — carrying the confirmed contract (the right survivor).

Runtime ⊥ api holds: the event arrives as a JSON payload (no runtime→api import); this service
is api and owns the stores. It depends on narrow Protocols so its invariants are unit-tested
with fakes and the real stores plug in at composition.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from persona.errors import PersonaError
from persona.logging import get_logger
from persona.schedules import RecurrenceRule, Schedule
from persona.tasks import Contract, Task

from persona_api.approvals.failure import FailureAccount, account_for_origination_failure
from persona_api.tasks.scheduled_task_builders import (
    build_backing_schedule,
    build_backing_task,
    derive_task_and_schedule_ids,
    derive_trigger_id,
)

if TYPE_CHECKING:
    from persona.schema.origination import PersonaIdentityTag

    from persona_api.events import EventTriggerRecord

__all__ = [
    "FailureNotifier",
    "OriginationKeyError",
    "OriginationOutcome",
    "OriginationService",
    "OriginationStatus",
    "ScheduleCreator",
    "TaskCreator",
    "TriggerCreator",
    "derive_origination_key",
]

_logger = get_logger("api.origination")


class OriginationKeyError(PersonaError):
    """No idempotency anchor could be derived — neither assistant_message_id nor a draft hash.

    Raised before any write, so there is never an *unkeyed* create path that a replay could
    duplicate (A4-D-X). In practice ``assistant_message_id`` is always present; this guards the
    impossible case rather than silently minting a random id.
    """


class TaskCreator(Protocol):
    """The owner-scoped task writer the service needs (the real adapter wraps ``TaskStore``)."""

    def get_optional(self, owner_id: str, task_id: str) -> Task | None:
        """The task, or ``None`` if it does not exist (the idempotency probe)."""
        ...

    def create_if_absent(self, task: Task) -> None:
        """Persist ``task``; a no-op if its id already exists (idempotent under replay)."""
        ...


class ScheduleCreator(Protocol):
    """The owner-scoped schedule writer (the real adapter wraps ``ScheduleStore``)."""

    def create_if_absent(self, schedule: Schedule, *, now: datetime) -> None:
        """Persist ``schedule``; a no-op if its id already exists (idempotent)."""
        ...

    def delete(self, owner_id: str, schedule_id: str) -> None:
        """Remove a schedule (the compensating action when the task create fails)."""
        ...


class TriggerCreator(Protocol):
    """The owner-scoped A7 trigger-registry writer (the real adapter wraps ``EventTriggerStore``)."""  # noqa: E501

    def create_if_absent(
        self, record: EventTriggerRecord, *, now: datetime
    ) -> EventTriggerRecord:
        """Persist the trigger row; reflect the existing row on a PK conflict (idempotent)."""
        ...

    def delete(self, owner_id: str, trigger_id: str) -> bool:
        """Remove a trigger (the compensating action when the task/trigger create fails)."""
        ...


class FailureNotifier(Protocol):
    """Sends a :class:`FailureAccount` to the user (the real impl rides the C0 Originator)."""

    async def notify(
        self,
        account: FailureAccount,
        *,
        persona: PersonaIdentityTag,
        owner_id: str,
        conversation_id: str,
    ) -> None:
        """Deliver the failure account on its cadence-bypass priority class."""
        ...


class OriginationStatus(StrEnum):
    """The outcome of an origination attempt."""

    CREATED = "created"
    IDEMPOTENT = "idempotent"  # the task already existed (replay / double-confirm)
    FAILED = "failed"


@dataclass(frozen=True)
class OriginationOutcome:
    """What an :meth:`OriginationService.originate` call did."""

    status: OriginationStatus
    task_id: str
    task: Task | None = None


def derive_origination_key(
    *, conversation_id: str, assistant_message_id: str, draft_hash: str
) -> str:
    """Derive the deterministic origination key (A4-D-X).

    ``assistant_message_id`` is the **primary** anchor (semantically precise: one proposal turn =
    one task; two genuinely-distinct confirmations are distinct turns → distinct keys). The
    content ``draft_hash`` is the **fallback**, used only when the message id is absent. If both
    are empty there is no safe anchor — raise rather than mint an unkeyed (replay-duplicating) id.
    """
    anchor = assistant_message_id.strip() or draft_hash.strip()
    if not anchor:
        raise OriginationKeyError(
            "no origination anchor (assistant_message_id and draft_hash both empty)",
            context={"conversation_id": conversation_id},
        )
    return f"originate:{conversation_id}:{anchor}"


class OriginationService:
    """Create the A2 task + A1 schedule from a confirmed contract, idempotently and visibly."""

    def __init__(
        self,
        *,
        tasks: TaskCreator,
        schedules: ScheduleCreator,
        notifier: FailureNotifier,
        triggers: TriggerCreator | None = None,
    ) -> None:
        """Inject the owner-scoped writers + the failure notifier (DI; composition wires real).

        ``triggers`` (Spec A7, T7) is the event-trigger registry writer; ``None`` means the deploy
        has no event-trigger surface wired, so a ``task_originated`` event that carries a trigger
        fails visibly (never a silently-dropped confirmed contract).
        """
        self._tasks = tasks
        self._schedules = schedules
        self._notifier = notifier
        self._triggers = triggers

    async def originate(self, data: Mapping[str, Any]) -> OriginationOutcome:
        """Create the task + schedule for one ``task_originated`` event (A4-D-X).

        Idempotent on the derived key; on any failure, surfaces an un-suppressible failure
        account and compensates a partial schedule. Returns the outcome (created / idempotent /
        failed) for the worker's telemetry.
        """
        owner_id = str(data["owner_id"])
        conversation_id = str(data["conversation_id"])
        key = derive_origination_key(
            conversation_id=conversation_id,
            assistant_message_id=str(data.get("assistant_message_id", "")),
            draft_hash=str(data.get("draft_hash", "")),
        )
        task_id, schedule_id = derive_task_and_schedule_ids(key)

        existing = self._tasks.get_optional(owner_id, task_id)
        if existing is not None:
            # Replay / double-confirm of the same proposal — exactly one task, already created.
            _logger.info("origination idempotent hit task_id={tid}", tid=task_id)
            return OriginationOutcome(OriginationStatus.IDEMPOTENT, task_id, existing)

        now = datetime.now(UTC)
        scheduled = bool(data.get("schedule"))
        triggered = bool(data.get("trigger"))
        trigger_id = derive_trigger_id(key) if triggered else None
        try:
            if scheduled:
                _contract = data.get("contract")
                _subject = (
                    str(_contract["goal"])
                    if isinstance(_contract, Mapping) and _contract.get("goal")
                    else None
                )
                schedule = _build_schedule(
                    schedule_id=schedule_id,
                    owner_id=owner_id,
                    payload=data["schedule"],
                    task_id=task_id,
                    now=now,
                    subject=_subject,
                )
                self._schedules.create_if_absent(schedule, now=now)
            task = _build_task(
                task_id=task_id,
                owner_id=owner_id,
                persona_id=str(data["persona_id"]),
                contract=Contract.model_validate(data["contract"]),
                conversation_id=conversation_id,
                schedule_id=schedule_id if scheduled else None,
                # A7: an event-triggered task is born WAITING(on_event) — the dispatcher's
                # door-a fires its leg; it carries no schedule.
                wait_on_event=triggered,
                now=now,
            )
            self._tasks.create_if_absent(task)
            if triggered:
                # A7 (T7): the trigger registry row is created HERE and ONLY here — on the confirmed
                # contract (criterion 1). No store wired ⇒ fail visibly, never drop.
                if self._triggers is None:
                    msg = "task_originated carried a trigger but no trigger registry is wired"
                    raise OriginationKeyError(msg, context={"task_id": task_id})
                assert trigger_id is not None  # noqa: S101 — set iff triggered
                self._triggers.create_if_absent(
                    _build_trigger_record(
                        trigger_id=trigger_id,
                        task_id=task_id,
                        owner_id=owner_id,
                        persona_id=str(data["persona_id"]),
                        payload=data["trigger"],
                        now=now,
                    ),
                    now=now,
                )
        except Exception as exc:  # noqa: BLE001 — any create failure MUST surface, never silently drop
            await self._on_failure(
                task_id=task_id,
                schedule_id=schedule_id if scheduled else None,
                trigger_id=trigger_id,
                owner_id=owner_id,
                persona=_persona_tag(data),
                conversation_id=conversation_id,
                cause=str(exc),
            )
            return OriginationOutcome(OriginationStatus.FAILED, task_id)
        return OriginationOutcome(OriginationStatus.CREATED, task_id, task)

    async def _on_failure(
        self,
        *,
        task_id: str,
        schedule_id: str | None,
        trigger_id: str | None,
        owner_id: str,
        persona: PersonaIdentityTag,
        conversation_id: str,
        cause: str,
    ) -> None:
        """Compensate a partial schedule/trigger, then surface an un-suppressible account."""
        if schedule_id is not None:
            try:
                self._schedules.delete(owner_id, schedule_id)  # compensate the orphan
            except Exception:  # noqa: BLE001 — best-effort cleanup; the user-visible account is what matters
                _logger.warning(
                    "origination compensation failed schedule_id={sid}", sid=schedule_id
                )
        if trigger_id is not None and self._triggers is not None:
            try:
                self._triggers.delete(owner_id, trigger_id)  # compensate a partial trigger row
            except Exception:  # noqa: BLE001 — best-effort cleanup; the account is what matters
                _logger.warning(
                    "origination compensation failed trigger_id={tid}", tid=trigger_id
                )
        account = account_for_origination_failure(task_id, cause=cause)
        try:
            await self._notifier.notify(
                account, persona=persona, owner_id=owner_id, conversation_id=conversation_id
            )
        except Exception:  # noqa: BLE001 — a notify failure is logged loudly; we cannot do more here
            _logger.error(
                "origination failure account could not be delivered task_id={tid}", tid=task_id
            )


def _persona_tag(data: Mapping[str, Any]) -> PersonaIdentityTag:
    from persona.schema.origination import PersonaIdentityTag

    return PersonaIdentityTag(
        persona_id=str(data["persona_id"]), display_name=str(data["persona_name"])
    )


def _build_task(
    *,
    task_id: str,
    owner_id: str,
    persona_id: str,
    contract: Contract,
    conversation_id: str,
    schedule_id: str | None,
    now: datetime,
    wait_on_event: bool = False,
) -> Task:
    """The A2 task for a confirmed contract — the shared builder (A10-D-8), A4 anchors."""
    return build_backing_task(
        task_id=task_id,
        owner_id=owner_id,
        persona_id=persona_id,
        contract=contract,
        conversation_id=conversation_id,
        schedule_id=schedule_id,
        now=now,
        wait_on_event=wait_on_event,
    )


def _build_schedule(
    *,
    schedule_id: str,
    owner_id: str,
    payload: Mapping[str, Any],
    task_id: str,
    now: datetime,
    subject: str | None = None,
) -> Schedule:
    """Parse the event's JSON cadence payload, then build via the shared builder (A10-D-8).

    The JSON-payload parsing is A4's (the runtime→api event crosses as data); the
    fire-bridge/one-mechanism construction is the shared module's.
    """
    recurrence_payload = payload.get("recurrence")
    one_time_raw = payload.get("one_time_at")
    return build_backing_schedule(
        schedule_id=schedule_id,
        owner_id=owner_id,
        timezone=str(payload["timezone"]),
        recurrence=RecurrenceRule.model_validate(recurrence_payload)
        if recurrence_payload
        else None,
        one_time_at=datetime.fromisoformat(one_time_raw) if isinstance(one_time_raw, str) else None,
        task_id=task_id,
        now=now,
        # A chat/voice-originated schedule reaches here ONLY from a user-CONFIRMED contract
        # (criterion 1) — the user explicitly asked to be reminded, exactly like the HTTP
        # reminder dialog (which defaults notify_on_fire=True). Key the bell on "did the user
        # ask", not "which door": a fire writes the coalesced schedule_fired bell (one
        # re-alerting entry per schedule — never 96 rows). Operator-pass find, 2026-07-07.
        notify_on_fire=True,
        # …and carry the contract goal as the reminder subject, so the bell reads
        # "{persona} ran your reminder: {subject}" — without it the {subject} interpolation
        # fails and the client falls back to the raw message key (operator-pass find).
        subject=subject,
    )


def _build_trigger_record(
    *,
    trigger_id: str,
    task_id: str,
    owner_id: str,
    persona_id: str,
    payload: Mapping[str, Any],
    now: datetime,
) -> EventTriggerRecord:
    """The A7 registry row for a confirmed event-trigger contract — door-a fires this task's leg.

    The ``payload`` is the runtime's ``TriggerSpec`` (``{event_kind, filter, human_terms}``, JSON).
    ``platform`` is left ``None`` — the store derives it from the filter (the single-write-path
    invariant). The row is born ENABLED (a confirmed contract is live); the action is fixed to fire
    the confirmed task's leg. Imports are local to keep the events package out of the service's
    import-time graph.
    """
    from persona.events import EventKind, FireTaskLeg, TriggerFilter
    from pydantic import TypeAdapter

    from persona_api.events import EventTriggerRecord

    trigger_filter: TriggerFilter = TypeAdapter(TriggerFilter).validate_python(payload["filter"])
    return EventTriggerRecord(
        id=trigger_id,
        owner_id=owner_id,
        persona_id=persona_id,
        task_id=task_id,
        event_kind=EventKind(str(payload["event_kind"])),
        platform=None,
        filter=trigger_filter,
        action=FireTaskLeg(task_id=task_id),
        enabled=True,
        disabled_reason=None,
        last_fired_at=None,
        pending_coalesced_count=0,
        created_at=now,
        updated_at=now,
    )
