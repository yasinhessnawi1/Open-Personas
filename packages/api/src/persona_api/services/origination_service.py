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

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from persona.errors import PersonaError
from persona.logging import get_logger
from persona.schedules import RecurrenceRule, Schedule
from persona.tasks import Contract, Task, TaskState, WaitKind

from persona_api.approvals.failure import FailureAccount, account_for_origination_failure
from persona_api.tasks.scheduled_fire import TASK_SCHEDULED_FIRE_JOB_TYPE

if TYPE_CHECKING:
    from persona.schema.origination import PersonaIdentityTag

__all__ = [
    "FailureNotifier",
    "OriginationKeyError",
    "OriginationOutcome",
    "OriginationService",
    "OriginationStatus",
    "ScheduleCreator",
    "TaskCreator",
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


def _derive_id(prefix: str, key: str) -> str:
    """A deterministic id from the origination key (stable across replays)."""
    return f"{prefix}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}"


class OriginationService:
    """Create the A2 task + A1 schedule from a confirmed contract, idempotently and visibly."""

    def __init__(
        self,
        *,
        tasks: TaskCreator,
        schedules: ScheduleCreator,
        notifier: FailureNotifier,
    ) -> None:
        """Inject the owner-scoped writers + the failure notifier (DI; composition wires real)."""
        self._tasks = tasks
        self._schedules = schedules
        self._notifier = notifier

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
        task_id = _derive_id("task", key)
        schedule_id = _derive_id("sched", key)

        existing = self._tasks.get_optional(owner_id, task_id)
        if existing is not None:
            # Replay / double-confirm of the same proposal — exactly one task, already created.
            _logger.info("origination idempotent hit task_id={tid}", tid=task_id)
            return OriginationOutcome(OriginationStatus.IDEMPOTENT, task_id, existing)

        now = datetime.now(UTC)
        scheduled = bool(data.get("schedule"))
        try:
            if scheduled:
                schedule = _build_schedule(
                    schedule_id=schedule_id,
                    owner_id=owner_id,
                    payload=data["schedule"],
                    task_id=task_id,
                    now=now,
                )
                self._schedules.create_if_absent(schedule, now=now)
            task = _build_task(
                task_id=task_id,
                owner_id=owner_id,
                persona_id=str(data["persona_id"]),
                contract=Contract.model_validate(data["contract"]),
                conversation_id=conversation_id,
                schedule_id=schedule_id if scheduled else None,
                now=now,
            )
            self._tasks.create_if_absent(task)
        except Exception as exc:  # noqa: BLE001 — any create failure MUST surface, never silently drop
            await self._on_failure(
                task_id=task_id,
                schedule_id=schedule_id if scheduled else None,
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
        owner_id: str,
        persona: PersonaIdentityTag,
        conversation_id: str,
        cause: str,
    ) -> None:
        """Compensate a partial schedule, then surface an un-suppressible failure account."""
        if schedule_id is not None:
            try:
                self._schedules.delete(owner_id, schedule_id)  # compensate the orphan
            except Exception:  # noqa: BLE001 — best-effort cleanup; the user-visible account is what matters
                _logger.warning(
                    "origination compensation failed schedule_id={sid}", sid=schedule_id
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
) -> Task:
    """Construct the A2 task carrying the A4 contract (the matrix rides ``contract``).

    A schedule-backed task is born **WAITING(until_time)** — dormant at zero cost, awaiting its
    first scheduled fire, which the leg handler resumes (the existing WAITING→ACTIVE resume). This
    is what makes an A4 task actually execute (Spec A4 schedule-attach): without a runnable state
    the task would sit inert forever. A scheduleless task stays DEFINED (the pre-schedule shape).
    """
    scheduled = schedule_id is not None
    return Task(
        id=task_id,
        owner_id=owner_id,
        persona_id=persona_id,
        contract=contract,
        conversation_id=conversation_id,
        schedule_id=schedule_id,
        state=TaskState.WAITING if scheduled else TaskState.DEFINED,
        wait_kind=WaitKind.UNTIL_TIME if scheduled else None,
        created_at=now,
        updated_at=now,
    )


def _build_schedule(
    *,
    schedule_id: str,
    owner_id: str,
    payload: Mapping[str, Any],
    task_id: str,
    now: datetime,
) -> Schedule:
    """Construct the A1 schedule that fires the task's legs (target the A1→A2 fire bridge)."""
    recurrence_payload = payload.get("recurrence")
    one_time_raw = payload.get("one_time_at")
    return Schedule(
        id=schedule_id,
        owner_id=owner_id,
        timezone=str(payload["timezone"]),
        recurrence=RecurrenceRule.model_validate(recurrence_payload)
        if recurrence_payload
        else None,
        one_time_at=datetime.fromisoformat(one_time_raw) if isinstance(one_time_raw, str) else None,
        # Fire the schedule→leg bridge, NOT task_leg directly: the leg needs a ScheduledFire trigger
        # + the head-at-fire predecessor_seq the bridge computes (a static template can't carry it).
        target_job_type=TASK_SCHEDULED_FIRE_JOB_TYPE,
        payload_template={"task_id": task_id},
        created_at=now,
        updated_at=now,
    )
