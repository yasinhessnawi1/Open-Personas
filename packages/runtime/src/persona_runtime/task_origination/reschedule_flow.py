"""Assemble the reschedule re-echo — full clause + next-fire preview + quiet-hours warn (A8, T6).

Pure given the user's timezone + quiet window: turns a resolved :class:`RescheduleIntent` into the
persona's re-echo (the FULL new "When:" clause the user confirms, A8-D-2) and the event payload the
confirm emits. The new cadence parses through the SAME parser as create (:func:`parse_recurrence` /
:func:`parse_one_time`) — an unrepresentable cadence raises ``ScheduleParseError`` (the loop
declines honestly). The next-run preview is computed from a throwaway schedule anchored at ``now``,
and quiet-hours (A8-D-6) warn-plus-offer is appended when the next run lands inside the window.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from persona.schedules import (
    RecurrenceRule,
    Schedule,
    next_fire_after,
    quiet_hours_edge,
)

from persona_runtime.task_origination.reschedule import (
    RescheduleIntent,
    render_reschedule_echo,
)
from persona_runtime.task_origination.schedule import parse_one_time, parse_recurrence

if TYPE_CHECKING:
    from datetime import datetime

    from persona.schedules import QuietHours

__all__ = ["RescheduleEcho", "assemble_reschedule_echo"]


class RescheduleEcho:
    """The assembled re-echo: the text to show + the event payload a confirm emits."""

    __slots__ = ("echo_text", "event_data")

    def __init__(self, echo_text: str, event_data: dict[str, object]) -> None:
        self.echo_text = echo_text
        self.event_data = event_data


def assemble_reschedule_echo(
    intent: RescheduleIntent,
    *,
    task_goal: str,
    timezone: str,
    quiet_hours: QuietHours | None,
    now: datetime,
) -> RescheduleEcho:
    """Build the re-echo + the ``task_rescheduled`` event payload for a resolved reschedule.

    Raises :class:`~persona_runtime.errors.ScheduleParseError` when the new cadence is not
    representable (the loop declines honestly — the same parse-honesty boundary as create).
    """
    if intent.skip_next:
        echo = f'Skip the next run of "{task_goal}"? The one after is unaffected.'
        return RescheduleEcho(echo, _event(intent, timezone, skip_next=True))

    if intent.recurrence_rrule is not None:
        parsed = parse_recurrence(intent.recurrence_rrule, timezone, phrase=intent.recurrence_rrule)
        recurrence: RecurrenceRule | None = RecurrenceRule.from_rrule_string(
            intent.recurrence_rrule
        )
        one_time = None
    else:
        assert intent.one_time_at is not None  # RESOLVED guarantees one change kind
        parsed = parse_one_time(intent.one_time_at, timezone, phrase=str(intent.one_time_at))
        recurrence = None
        one_time = intent.one_time_at

    preview = Schedule(
        id="preview",
        owner_id="preview",
        timezone=timezone,
        recurrence=recurrence,
        one_time_at=one_time,
        target_job_type="preview",
        created_at=now,
        updated_at=now,
    )
    next_fire = next_fire_after(preview, after=now)
    next_phrase = (
        f"{next_fire.astimezone(ZoneInfo(timezone)):%a %-d %b}" if next_fire else "no upcoming run"
    )
    offer = _quiet_offer(next_fire, timezone, quiet_hours)
    echo = render_reschedule_echo(
        task_goal=task_goal,
        human_terms=parsed.human_terms,
        timezone=timezone,
        next_fire_phrase=next_phrase,
        quiet_hours_offer=offer,
    )
    return RescheduleEcho(echo, _event(intent, timezone))


def _quiet_offer(
    next_fire: datetime | None, timezone: str, quiet_hours: QuietHours | None
) -> str | None:
    """The nearest quiet-window edge (HH:MM) if the next run is inside quiet hours, else None."""
    if quiet_hours is None or next_fire is None:
        return None
    local = next_fire.astimezone(ZoneInfo(timezone))
    edge = quiet_hours_edge(local.hour * 60 + local.minute, quiet_hours)
    return None if edge is None else f"{edge // 60:02d}:{edge % 60:02d}"


def _event(
    intent: RescheduleIntent, timezone: str, *, skip_next: bool = False
) -> dict[str, object]:
    return {
        "task_id": intent.task_id,
        "timezone": timezone,
        "recurrence_rrule": intent.recurrence_rrule,
        "one_time_at": intent.one_time_at.isoformat() if intent.one_time_at else None,
        "skip_next": skip_next,
    }
