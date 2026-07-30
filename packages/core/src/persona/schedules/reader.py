"""Read-only schedule state for grounded calendar introspection (R9-075).

A persona could *create* schedules long before it could *read* them: A1 gave it the durable
clock, A8 gave the web calendar an occurrences read model, but the toolbox had no window onto
either — so "what's on my calendar this week?" and "am I double-booking you?" were unanswerable
except by guessing. This module is the missing contract.

:class:`ScheduleReader` is the read surface (CQS: reads only, no mutator anywhere on it). It
returns a :class:`ScheduleAgenda` — a window of computed future fires, each already resolved to
its human cadence terms and its captured timezone, with an honest ``truncated`` marker when the
server's cap bound the answer. There is **no rule text, no payload, and no mutator** on this
surface, so a persona holding a reader can narrate what is coming and nothing else.

Two scopes, deliberately explicit (:class:`ScheduleScope`): ``mine`` is the calling persona's own
schedules, ``all`` is everything the owner has scheduled across every persona. The distinction is
load-bearing — a persona that says "your calendar is clear" after looking at only its own slice is
wrong in the way that matters — so the vocabulary is parsed, not defaulted
(:func:`parse_schedule_scope`), and an unknown value raises
:class:`~persona.errors.InvalidScheduleScopeError`.

Owner scoping lives in the *implementation* (it binds the owner and reads through RLS); this
protocol takes no ``owner_id``, so a tool holding a reader can only ever see its own caller's
schedules — the :class:`~persona.tasks.reader.TaskStateReader` precedent.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic needs runtime access (model field type)
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from persona.errors import InvalidScheduleScopeError

__all__ = [
    "ScheduleAgenda",
    "ScheduleReader",
    "ScheduleScope",
    "ScheduledOccurrence",
    "parse_schedule_scope",
]


class ScheduleScope(StrEnum):
    """Whose schedules an introspection call covers (the owner's two named scopes).

    ``MINE`` restricts the answer to the calling persona's own schedules — what *it* has
    committed to. ``ALL`` covers every schedule the owner has, across every persona, which
    is the scope a double-booking question actually needs.
    """

    MINE = "mine"
    ALL = "all"


def parse_schedule_scope(value: str) -> ScheduleScope:
    """Parse a model-supplied scope name into a :class:`ScheduleScope`.

    Case- and whitespace-insensitive; an empty value means the default (``mine``), because
    "what's on my calendar" from a persona is most naturally about its own commitments.

    Args:
        value: The raw scope name from the tool call.

    Returns:
        The parsed scope.

    Raises:
        InvalidScheduleScopeError: If ``value`` is not a supported scope name. Never
            silently coerced — a wrong scope is a wrong answer, not a formatting nit.
    """
    normalised = value.strip().lower()
    if not normalised:
        return ScheduleScope.MINE
    try:
        return ScheduleScope(normalised)
    except ValueError as exc:
        raise InvalidScheduleScopeError(
            "unknown schedule scope",
            context={
                "scope": value,
                "supported": ", ".join(s.value for s in ScheduleScope),
            },
        ) from exc


class ScheduledOccurrence(BaseModel):
    """One computed future fire — when it happens, whose it is, and what it does.

    Attributes:
        schedule_id: The durable schedule this fire belongs to (the handle a reschedule
            or cancel needs, so the persona can act on what it just described).
        persona_id: The persona the schedule resolves to, or ``None`` when it resolves to
            none (an owner-level schedule with no backing task).
        task_id: The standing task the schedule drives, or ``None`` for a task-less schedule.
        fire_at: The absolute tz-aware UTC instant of this occurrence.
        timezone: The schedule's captured IANA zone — the frame the fire was *meant* in, and
            the one it should be spoken in.
        human_terms: The cadence in plain language (never a raw RRULE).
        subject: What the fire actually does, one line, when it is known.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_id: str
    persona_id: str | None = None
    task_id: str | None = None
    fire_at: datetime
    timezone: str
    human_terms: str
    subject: str | None = None


class ScheduleAgenda(BaseModel):
    """A window of upcoming fires + the honest truncation marker.

    Attributes:
        occurrences: The fires in ``[window_from, window_to]``, soonest first.
        window_from: Start of the window actually read (tz-aware UTC).
        window_to: End of the window actually read — the EFFECTIVE end after any
            server-side horizon clamp, not necessarily the end that was asked for.
        truncated: True when a cap bound the result, so the persona says "at least these"
            rather than "that's everything".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    occurrences: tuple[ScheduledOccurrence, ...] = ()
    window_from: datetime
    window_to: datetime
    truncated: bool = False


@runtime_checkable
class ScheduleReader(Protocol):
    """Owner-scoped, read-only access to upcoming schedule occurrences.

    Implementations bind the owner and read through RLS; this surface takes no ``owner_id``
    and offers no mutator (CQS), so a tool holding one can only read its own caller's
    calendar.
    """

    def read_agenda(
        self, *, start: datetime, end: datetime, persona_id: str | None
    ) -> ScheduleAgenda:
        """The owner's occurrences in ``[start, end]``, soonest first.

        Args:
            start: Window start (tz-aware UTC).
            end: Window end (tz-aware UTC); implementations may clamp it and report the
                effective end on the returned agenda.
            persona_id: Restrict to the schedules resolving to this persona, or ``None``
                for every schedule the owner has (the whole-calendar scope).
        """
        ...
