"""Build the conversational-verb worker services BOTH process roots need (R9-081).

A persona can confirm a task contract, reschedule a task, or answer an initiative
dial in any conversation. Whether those confirmations DO anything depends on which
process ran the turn: the api wired the worker-side services, the connector passed
``None`` for all four, so a user confirming over Telegram got "Done, I've set that
up" and nothing happened. The reply text is produced by the runtime BEFORE the
worker call and independently of it, which is why the failure is silent and reads
as a lie rather than an error.

Two of these services (reschedule, initiative) are pure functions of the RLS engine
and config, with no api-process dependency at all: they were left unwired by
association with origination, not because anything blocked them. Duplicating their
construction in the connector root is exactly the drift that produced R9-074 and
R9-079, where the connector's copy of a composition silently fell to constructor
defaults and every connector turn ran unbilled. So they are built here once and
called from both roots.

**Steering and origination are deliberately NOT here.** Both can take a
``FailureNotifier``, and the notifier narrates through the C0 delivery seam to a web
tab that a connector process does not have. Origination stays unwired pending that
decision. Steering is built per-root, because the api has a notifier to give it and
the connector does not, and pretending otherwise would hide a real difference in
what happens when a cancel fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from persona_api.config import APIConfig
    from persona_api.initiative.verb_service import InitiativeVerbService
    from persona_api.services.task_reschedule_service import TaskRescheduleService

__all__ = ["ConversationalVerbServices", "build_conversational_verb_services"]


@dataclass(frozen=True)
class ConversationalVerbServices:
    """The verb services that need no api-process-only collaborator.

    Attributes:
        reschedule: Worker side of the A8 reschedule verb, or ``None`` without an engine.
        initiative: Worker side of the A5 initiative verbs, or ``None`` when initiative
            is disabled (``PERSONA_INITIATIVE_ENABLED``, default OFF) or without an engine.
    """

    reschedule: TaskRescheduleService | None
    initiative: InitiativeVerbService | None


def build_conversational_verb_services(
    *, rls_engine: Engine | None, config: APIConfig
) -> ConversationalVerbServices:
    """Build the reschedule + initiative worker services from an engine and config.

    Args:
        rls_engine: The RLS-scoped engine; ``None`` yields an all-``None`` result so a
            deployment without a database keeps its current shape.
        config: The resolved api config (supplies the schedule tombstone window).

    Returns:
        The services, each ``None`` when its own precondition is unmet.
    """
    if rls_engine is None:
        return ConversationalVerbServices(reschedule=None, initiative=None)

    from persona_api.schedules.store import ScheduleStore  # noqa: PLC0415
    from persona_api.services.task_reschedule_service import TaskRescheduleService  # noqa: PLC0415
    from persona_api.tasks.store import TaskStore  # noqa: PLC0415

    # A8 (T6): applies a user-confirmed reschedule through the one CAS-guarded door.
    reschedule = TaskRescheduleService(
        task_reader=TaskStore(rls_engine),
        schedule_store=ScheduleStore(rls_engine),
        engine=rls_engine,
    )

    # A5 (T10): dial writes (+ the lazy schedule ensure) and the ledger-anchored
    # confirm/decline. Built only when initiative is enabled.
    from persona.initiative import InitiativeSettings  # noqa: PLC0415

    settings = InitiativeSettings()
    if not settings.enabled:
        return ConversationalVerbServices(reschedule=reschedule, initiative=None)

    from persona.config import PersonaCoreConfig  # noqa: PLC0415

    from persona_api.initiative.delivery import InitiativeDeliveryExecutor  # noqa: PLC0415
    from persona_api.initiative.store import DeclineStore, InitiativeLedger  # noqa: PLC0415
    from persona_api.initiative.verb_service import InitiativeVerbService  # noqa: PLC0415
    from persona_api.schedules.tombstones import ScheduleTombstoneStore  # noqa: PLC0415

    ledger = InitiativeLedger(rls_engine)
    initiative = InitiativeVerbService(
        rls_engine=rls_engine,
        schedules=ScheduleStore(rls_engine),
        ledger=ledger,
        declines=DeclineStore(rls_engine),
        executor=InitiativeDeliveryExecutor(
            ledger=ledger,
            tasks=TaskStore(rls_engine),
            schedules=ScheduleStore(rls_engine),
            timezone_for=PersonaCoreConfig().default_timezone,
            rls_engine=rls_engine,
        ),
        settings=settings,
        # R9-037: the dial verb's own lazy ensure refuses a recently user-deleted
        # scan schedule too (the same shared function the provisioner sweep gates).
        tombstones=ScheduleTombstoneStore(rls_engine),
        tombstone_window_days=config.schedule_tombstone_window_days,
    )
    return ConversationalVerbServices(reschedule=reschedule, initiative=initiative)
