"""The user's direct create door — the third verb on A8's mechanism (Spec A10, A10-D-1/2/6).

``POST /v1/me/schedule`` lands here: a deterministic, model-free schedule create through
**A8's existing ``ScheduleStore.create``** and the A2 task path — no new schedule-write
mechanism (the A10-D-9 invariant: one write path, one explicit user confirmation, now two
doors). The user is the **originator**; the named persona is the **executor** who delivers
each fire through the same A1→A2 fire bridge A4-created tasks use (A10-D-2 — the no-bypass
guarantee).

Owned invariants:

- **Fail-fast on never-fires** (A10-D-1): a cadence with no future occurrence
  (engine ``next_fire_after(now)`` is ``None``) raises :class:`ScheduleNeverFiresError`
  before any write — never a dead row the tick ignores forever.
- **Idempotency** (A10-D-6): the client mints one ``idempotency_key`` per create dialog;
  ids derive deterministically from ``user-create:{owner_id}:{key}``, so a double-click /
  retry converges on ONE task+schedule while two deliberate submits (two dialog-opens,
  two keys) stay distinct. Deliberately NO content-hash dedup — the argued inverse of
  A4-D-X ("remind me twice" via two explicit submits is the user's right).
- **Compensation**: a task-create failure after the schedule landed deletes the orphan
  schedule and re-raises — the synchronous HTTP error IS the failure visibility here
  (unlike A4's async seam, the user is present; no C0 account needed).
- **Audit** (A10-D-1): the store's ``schedule.create`` row carries
  ``actor=user_via_ui`` + ``originator=user`` + the subject.
"""

from __future__ import annotations

import contextlib
import unicodedata
from datetime import datetime  # noqa: TC003 — a runtime Pydantic field type
from typing import TYPE_CHECKING

from persona.errors import (
    PersonaNotFoundError,
    ScheduleNeverFiresError,
    ScheduleNotFoundError,
    TaskNotFoundError,
)
from persona.logging import get_logger
from persona.schedules import next_fire_after, pattern_to_rule
from persona.tasks import Contract, Task
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from persona_api.db.engine import rls_connection
from persona_api.db.models import personas as personas_t
from persona_api.services.calendar_reschedule_service import (
    ReschedulePreview,
    preview_schedule_cadence,
)
from persona_api.tasks.scheduled_task_builders import (
    build_backing_schedule,
    build_backing_task,
    derive_task_and_schedule_ids,
)

if TYPE_CHECKING:
    from persona.schedules import RecurrencePattern, RecurrenceRule, Schedule
    from sqlalchemy import Engine

    from persona_api.schedules.store import ScheduleStore
    from persona_api.tasks.store import TaskStore

__all__ = ["SUBJECT_MAX_LENGTH", "ScheduleCreateResult", "create_user_schedule"]

_log = get_logger("api.schedule_create")

#: Defensive cap on the reminder subject (a goal line, not a document).
SUBJECT_MAX_LENGTH = 500


class ScheduleCreateResult(BaseModel):
    """The create confirmation — the ids + the same echo shape the preview showed.

    ``created`` is ``False`` on an idempotent replay (the task already existed for
    this ``idempotency_key``) — same ids, nothing written twice.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    schedule_id: str
    created: bool
    human_terms: str  # the full "When:" clause, human terms (no raw RRULE)
    timezone: str
    next_fire: datetime | None  # the engine's next fire for the created cadence
    quiet_hours_offer: str | None  # HH:MM nearest edge if inside quiet hours (A8's helper)


def normalize_subject(raw: str) -> str:
    """The stored subject form: control/format chars stripped, trimmed, capped (K6-D-8).

    The subject is DATA (it rides the contract goal and is rendered as a delimited
    value); stripping control/format characters keeps it from ever breaking the prompt
    structure it is later rendered into. An empty result is the caller's 422.
    """
    cleaned = "".join(ch for ch in raw if not unicodedata.category(ch).startswith("C"))
    return cleaned.strip()[:SUBJECT_MAX_LENGTH]


def create_user_schedule(
    engine: Engine,
    store: ScheduleStore,
    tasks: TaskStore,
    *,
    owner_id: str,
    pattern: RecurrencePattern | None,
    one_time_at: datetime | None,
    timezone: str,
    persona_id: str,
    subject: str,
    idempotency_key: str,
    now: datetime,
) -> ScheduleCreateResult:
    """Create the backing task + schedule for a user-initiated reminder (A10-D-1/2/6).

    Deterministic and model-free: picker-state in, the engine's next fire out. Raises
    :class:`ScheduleNeverFiresError` (→ 422) when the cadence has no future occurrence,
    :class:`PersonaNotFoundError` (→ 404) when the executor isn't the owner's, and
    ``ValueError`` (→ 422) when the subject is empty after normalisation.
    """
    cleaned_subject = normalize_subject(subject)
    if not cleaned_subject:
        msg = "subject is empty after normalisation"
        raise ValueError(msg)
    _require_owned_persona(engine, owner_id=owner_id, persona_id=persona_id)

    recurrence, one_time = _resolve_cadence(pattern, one_time_at)
    key = f"user-create:{owner_id}:{idempotency_key}"
    task_id, schedule_id = derive_task_and_schedule_ids(key)

    schedule = build_backing_schedule(
        schedule_id=schedule_id,
        owner_id=owner_id,
        timezone=timezone,
        recurrence=recurrence,
        one_time_at=one_time,
        task_id=task_id,
        now=now,
    )
    if next_fire_after(schedule, after=now) is None:
        raise ScheduleNeverFiresError(
            "this schedule would never fire",
            context={"cadence": "one_time" if one_time is not None else "recurring"},
        )

    preview = preview_schedule_cadence(
        engine,
        owner_id=owner_id,
        pattern=pattern,
        one_time_at=one_time_at,
        timezone=timezone,
        now=now,
    )

    if _get_task_optional(tasks, owner_id, task_id) is not None:
        # Replay / double-submit of the same dialog interaction — exactly one creation.
        _log.info("user schedule create idempotent hit task_id={tid}", tid=task_id)
        return _result(task_id, schedule_id, created=False, preview=preview)

    _create_schedule_if_absent(
        store,
        schedule,
        now=now,
        extra={"actor": "user_via_ui", "originator": "user", "subject": cleaned_subject},
    )
    task = build_backing_task(
        task_id=task_id,
        owner_id=owner_id,
        persona_id=persona_id,
        contract=_reminder_contract(cleaned_subject),
        conversation_id=None,  # no originating chat turn — the calendar is the door
        schedule_id=schedule_id,
        now=now,
    )
    try:
        tasks.create(task)
    except IntegrityError:
        # A racing replay landed the row first — converge, don't duplicate (A10-D-6).
        _log.info("user schedule task create raced; idempotent no-op task_id={tid}", tid=task_id)
    except Exception:
        # Compensate the orphan schedule, then let the HTTP error surface (the user is
        # present — the synchronous failure IS the visibility, unlike A4's async seam).
        _compensate_schedule(store, owner_id, schedule_id)
        raise
    return _result(task_id, schedule_id, created=True, preview=preview)


def _resolve_cadence(
    pattern: RecurrencePattern | None, one_time_at: datetime | None
) -> tuple[RecurrenceRule | None, datetime | None]:
    """Map picker-state → (recurrence, one_time) — the RRULE mapping is server-side."""
    if pattern is not None:
        return pattern_to_rule(pattern), None
    return None, one_time_at


def _require_owned_persona(engine: Engine, *, owner_id: str, persona_id: str) -> None:
    """Fail-fast unless the executor persona exists AND belongs to the owner.

    RLS-scoped: a cross-tenant persona id reads as absent — the same no-existence-oracle
    posture as the schedule store (both raise the not-found domain error).
    """
    with rls_connection(engine, owner_id) as conn:
        row = conn.execute(select(personas_t.c.id).where(personas_t.c.id == persona_id)).first()
    if row is None:
        raise PersonaNotFoundError("executor persona not found", context={"persona_id": persona_id})


def _reminder_contract(subject: str) -> Contract:
    """The reminder-shaped contract: the subject IS the goal; defaults bound the rest."""
    return Contract(
        goal=f"Remind and update the user about: {subject}",
        scope="Deliver a short, useful update on this subject at each scheduled fire.",
    )


def _get_task_optional(tasks: TaskStore, owner_id: str, task_id: str) -> Task | None:
    """The idempotency probe (the ``TaskCreatorAdapter.get_optional`` shape)."""
    try:
        return tasks.get(owner_id, task_id)
    except TaskNotFoundError:
        return None


def _create_schedule_if_absent(
    store: ScheduleStore, schedule: Schedule, *, now: datetime, extra: dict[str, str]
) -> None:
    """Persist the schedule; swallow a unique-violation (idempotent under replay)."""
    try:
        store.create(schedule, now=now, extra=extra)
    except IntegrityError:
        _log.info(
            "user schedule create raced to a duplicate; idempotent no-op schedule_id={sid}",
            sid=schedule.id,
        )


def _compensate_schedule(store: ScheduleStore, owner_id: str, schedule_id: str) -> None:
    """Best-effort orphan-schedule cleanup when the task create fails."""
    with contextlib.suppress(ScheduleNotFoundError):
        store.delete(owner_id, schedule_id)


def _result(
    task_id: str, schedule_id: str, *, created: bool, preview: ReschedulePreview
) -> ScheduleCreateResult:
    return ScheduleCreateResult(
        task_id=task_id,
        schedule_id=schedule_id,
        created=created,
        human_terms=preview.human_terms,
        timezone=preview.timezone,
        next_fire=preview.next_fire,
        quiet_hours_offer=preview.quiet_hours_offer,
    )
