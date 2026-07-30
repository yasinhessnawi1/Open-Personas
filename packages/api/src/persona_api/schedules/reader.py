"""The api implementation of the schedule reader (R9-075).

:class:`APIScheduleReader` satisfies the core
:class:`~persona.schedules.reader.ScheduleReader` protocol over the SAME occurrences read model
the web calendar renders from (:func:`persona_api.services.occurrences_service.list_occurrences`)
— so the persona and the calendar can never disagree about what is coming, and there is no second
recurrence-expansion path to drift.

It **binds the owner** at construction (the dispatch-time provider builds a fresh reader per
request from the ``current_user_id`` contextvar), so every read is owner-scoped: the underlying
service reads through RLS (``app.current_user_id`` GUC), which makes a cross-tenant reach return
zero rows rather than another tenant's calendar. Read-only by construction — the protocol has no
mutator and this class adds none. Core ⊥ api: the protocol lives in core, this impl in api.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.schedules.reader import ScheduleAgenda, ScheduledOccurrence

from persona_api.services.occurrences_service import list_occurrences

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import Engine

    from persona_api.config import APIConfig

__all__ = ["APIScheduleReader"]


class APIScheduleReader:
    """Owner-scoped, read-only schedule occurrences over the RLS read model."""

    def __init__(self, engine: Engine, config: APIConfig, owner_id: str) -> None:
        """Bind the owner; every read scopes to it (the provider builds one per dispatch)."""
        self._engine = engine
        self._config = config
        self._owner_id = owner_id

    def read_agenda(
        self, *, start: datetime, end: datetime, persona_id: str | None
    ) -> ScheduleAgenda:
        """The owner's occurrences in ``[start, end]``, soonest first.

        ``persona_id`` restricts the result to the schedules that resolve to that persona
        (the ``scope="mine"`` half); ``None`` returns every schedule the owner has (the
        ``scope="all"`` half). The window is clamped server-side to the configured horizon
        and the result to the configured count — ``truncated`` carries that fact through
        honestly rather than silently returning a short list.
        """
        result = list_occurrences(
            self._engine,
            owner_id=self._owner_id,
            from_=start,
            to=end,
            config=self._config,
            persona_id=persona_id,
        )
        return ScheduleAgenda(
            occurrences=tuple(
                ScheduledOccurrence(
                    schedule_id=o.schedule_id,
                    persona_id=o.persona_id,
                    task_id=o.task_id,
                    fire_at=o.fire_at,
                    timezone=o.timezone,
                    human_terms=o.human_terms,
                    subject=o.subject,
                )
                for o in result.occurrences
            ),
            window_from=result.window_from,
            window_to=result.window_to,
            truncated=result.truncated,
        )
