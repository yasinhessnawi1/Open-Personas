"""Compose the production :class:`EventDispatcher` with its real R7 + audit + P6 wirings (A7).

:func:`build_event_dispatcher` is the one place the dispatcher's injected seams bind to the real
platform surfaces: the R7 day-cap (``book_day_spend``, A7-D-8), the ``audit_log`` forensics trail
(A7-D-9), and the durable P6 storm-drop bell. It is used by the T5 storm proof (the real path) and
composed into the worker + connector emission wiring at T6. The autonomy-pause seam stays injectable
— default no-op until A6 wires the real ``owner_autonomy_pause`` reader at merge-back (A6-D-8).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.credits.service import book_day_spend
from persona.events import EventTriggerSettings

from persona_api.db.engine import rls_connection
from persona_api.events.dispatcher import EventDispatcher, PauseCheck, _default_pause_check
from persona_api.events.store import EventTriggerStore
from persona_api.jobs.queue import JobQueue
from persona_api.services import audit_service
from persona_api.services.notifications_service import create_notification
from persona_api.tasks.store import TaskStore

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy import Engine

    from persona_api.config import APIConfig

__all__ = ["build_event_dispatcher"]

_STORM_DROP_KIND = "event_trigger_dropped"
_STORM_DROP_MESSAGE_KEY = "notifications.event_trigger.dropped"


def build_event_dispatcher(
    *,
    rls_engine: Engine,
    config: APIConfig,
    settings: EventTriggerSettings | None = None,
    pause_check: PauseCheck = _default_pause_check,
) -> EventDispatcher:
    """Build the dispatcher with the real R7 day-cap, audit_log, and P6 storm-drop bell.

    Args:
        rls_engine: The ``persona_app`` RLS engine every store/queue/audit op runs on (per-owner).
        config: The API config — ``credits_max_per_day`` is the R7 day-cap ceiling (0 = unlimited).
        settings: The A7 tunables (cooldown, chain depth, fire cost). Defaults from env.
        pause_check: The A6-D-8 autonomy-pause reader; default no-op until A6 injects the real one.
    """
    resolved = settings if settings is not None else EventTriggerSettings()

    def _audit(owner: str, action: str, target: str, metadata: Mapping[str, str] | None) -> None:
        audit_service.record(
            engine=rls_engine,
            user_id=owner,
            action=action,
            target=target,
            metadata=dict(metadata) if metadata is not None else None,
        )

    def _budget_check(owner: str) -> bool:
        # R7-D-3 conditional write: books one fire's cost within the day-cap, or refuses over-cap.
        # ``fire_cost_estimate``/``cap`` of 0 allow without booking (community/uncapped no-op).
        return book_day_spend(
            rls_engine=rls_engine,
            user_id=owner,
            cost=resolved.fire_cost_estimate,
            cap=config.credits_max_per_day,
        )

    def _surface_drop(owner: str, trigger_id: str, human: str) -> None:
        # The durable P6 bell (A7-D-8): over-cap drops are surfaced, never silent. Keyed on the
        # trigger so a storm's repeated drops coalesce to one bell (ON CONFLICT DO NOTHING).
        with rls_connection(rls_engine, owner) as conn:
            create_notification(
                conn=conn,
                owner_id=owner,
                kind=_STORM_DROP_KIND,
                ref_id=trigger_id,
                level="warning",
                message_key=_STORM_DROP_MESSAGE_KEY,
                params={"detail": human},
            )

    return EventDispatcher(
        store=EventTriggerStore(rls_engine),
        queue=JobQueue(rls_engine),
        task_store=TaskStore(rls_engine),
        settings_cooldown_seconds=resolved.cooldown_seconds,
        settings_max_chain_depth=resolved.max_chain_depth,
        audit=_audit,
        surface_drop=_surface_drop,
        pause_check=pause_check,
        budget_check=_budget_check,
    )
