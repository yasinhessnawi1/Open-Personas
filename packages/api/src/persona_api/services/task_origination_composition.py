"""The worker-side composition of the A4 task-origination flow (Spec A4, composition-root wiring).

The single construction site for the origination + steering services the chat-turn worker consumes,
built from the real owner-scoped stores + the real C0 delivery composition. Both the API lifespan
(``app.py``) and the live composition test call **this** function, so the test exercises the exact
production wiring rather than a hand-assembled stand-in — the guard against false-greening the very
inertness this wiring exists to kill.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from persona_api.schedules.store import ScheduleStore
from persona_api.services.origination_adapters import (
    OriginatorFailureNotifier,
    ScheduleCreatorAdapter,
    TaskCreatorAdapter,
    make_persona_tag_resolver,
)
from persona_api.services.origination_service import OriginationService
from persona_api.services.task_steering_service import TaskSteeringService
from persona_api.tasks.store import TaskStore

if TYPE_CHECKING:
    from pathlib import Path

    from persona.stores.backend import Backend
    from sqlalchemy import Engine

    from persona_api.config import Edition
    from persona_api.services.web_deliverer import LiveSessionRegistry

__all__ = ["TaskOriginationServices", "compose_task_origination_services"]


@dataclass(frozen=True)
class TaskOriginationServices:
    """The pair the chat-turn worker needs — created + steered contract flow."""

    origination: OriginationService
    steering: TaskSteeringService


def compose_task_origination_services(
    *,
    rls_engine: Engine,
    memory_backend: Backend,
    edition: Edition,
    audit_root: Path,
    live_sessions: LiveSessionRegistry | None = None,
) -> TaskOriginationServices:
    """Build the A4 worker-side services over the real stores + C0 composition (A4-D-X).

    The failure notifier (shared by both services) originates an un-suppressible account on the
    conversation via the real :class:`Originator`; the origination service creates task + schedule
    idempotently through the store adapters; the steering service mutates through the owner-scoped
    ``TaskStore`` and surfaces a cancel-failure account when a cancel leaves the task active.

    ``live_sessions`` (Spec A11) is the channel-backed registry that lets an open tab receive an
    originated failure account live (``message.delivered``); ``None`` keeps the persist-only
    behaviour.
    """
    tasks = TaskStore(rls_engine)
    notifier = OriginatorFailureNotifier(
        rls_engine=rls_engine,
        memory_backend=memory_backend,
        edition=edition,
        audit_root=audit_root,
        sessions=live_sessions,
    )
    origination = OriginationService(
        tasks=TaskCreatorAdapter(tasks),
        schedules=ScheduleCreatorAdapter(ScheduleStore(rls_engine)),
        notifier=notifier,
    )
    steering = TaskSteeringService(
        tasks=tasks,
        notifier=notifier,
        persona_tag_resolver=make_persona_tag_resolver(rls_engine),
    )
    return TaskOriginationServices(origination=origination, steering=steering)
