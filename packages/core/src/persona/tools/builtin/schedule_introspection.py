"""The ``schedule_introspect`` tool — the persona's window onto the calendar (R9-075).

A model-callable, **read-only** tool that answers "what's on my calendar this week?", "am I
about to double-book you?", and "when does that run again?" from computed schedule occurrences
and nothing else. It is the read half the platform was missing: a persona could create a
schedule (A1/A10) and the web calendar could render one (A8), but the toolbox had no way to
*look* at either — so the persona guessed, or said it couldn't know.

Two scopes, both the owner asked for, and the difference is explicit in the call AND in the
answer:

* ``scope="mine"`` — the calling persona's own schedules. What *this* persona has committed to.
* ``scope="all"`` — every schedule the user has, across every persona. The scope a
  double-booking question actually needs.

The rendered output always names the scope it used, so a persona can never report a one-persona
slice as if it were the whole calendar. When the reader reports ``truncated`` the wording says
"at least these", never "that's everything".

Owner scoping + RLS live in the injected reader (resolved at dispatch via ``reader_provider``);
no reader (no request owner) fails closed. The tool is built once per conversation and stays
correctly scoped because the owner is resolved per call, not per build (the ``task_introspect`` /
``record_user_fact`` precedent). CQS: every path here reads — this tool cannot create, edit,
pause, or cancel anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from persona.errors import InvalidScheduleScopeError
from persona.schedules.reader import ScheduleScope, parse_schedule_scope
from persona.schema.tools import ToolResult
from persona.tools.protocol import tool

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from persona.schedules.reader import ScheduleAgenda, ScheduledOccurrence, ScheduleReader
    from persona.tools.protocol import AsyncTool

__all__ = ["SCHEDULE_INTROSPECT_TOOL_NAME", "make_schedule_introspection_tool"]

#: The registered tool name. Exported so the composition root can auto-allow it by
#: reference instead of by a duplicated string literal.
SCHEDULE_INTROSPECT_TOOL_NAME = "schedule_introspect"

_GUIDANCE = (
    "Look at what is actually scheduled before you answer anything about the calendar, a "
    "reminder, a recurring run, or whether a time is free. Use scope='mine' for your own "
    "schedules and scope='all' for everything the user has scheduled across all their "
    "personas — use 'all' whenever the question is about their day, their week, or a clash. "
    "days_ahead sets how far forward to look. Report ONLY what this tool returns: if it says "
    "nothing is scheduled, say nothing is scheduled; if it says the list was capped, say there "
    "may be more. Never invent an entry, a time, or a cadence."
)

#: The furthest ahead a single call may look. Matches the server-side occurrences horizon,
#: so asking for more would silently return less — better to say so than to mislead.
_MAX_DAYS_AHEAD = 90

_SCOPE_PHRASE: dict[ScheduleScope, str] = {
    ScheduleScope.MINE: "your own schedules",
    ScheduleScope.ALL: "everything scheduled across all of the user's personas",
}


def make_schedule_introspection_tool(
    *,
    reader_provider: Callable[[], ScheduleReader | None],
    persona_id: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> AsyncTool:
    """Build the ``schedule_introspect`` tool bound to an owner-scoped reader provider.

    Args:
        reader_provider: Resolves the current owner's :class:`ScheduleReader` at DISPATCH
            time (the toolbox is built once; the owner is per-request). ``None`` ⇒ fail
            closed — the tool reports it has no calendar access rather than answering blind.
        persona_id: The persona introspecting. Load-bearing here (unlike ``task_introspect``,
            where it is provenance only): it IS the ``scope="mine"`` filter. ``None`` ⇒ the
            own-schedules scope cannot be resolved and the tool says so instead of quietly
            widening to the whole calendar.
        clock: Returns "now" (tz-aware UTC) — the window anchor. Defaults to the system UTC
            clock; injectable so the window is deterministic under test.

    Returns:
        The registered :class:`~persona.tools.protocol.AsyncTool`.
    """
    now_fn = clock if clock is not None else lambda: datetime.now(UTC)

    @tool(name=SCHEDULE_INTROSPECT_TOOL_NAME, description=_GUIDANCE)
    async def schedule_introspect(scope: str = "mine", days_ahead: int = 7) -> ToolResult:
        try:
            parsed = parse_schedule_scope(scope)
        except InvalidScheduleScopeError:
            return _error(
                f"{scope!r} is not a scope I can read. Use 'mine' for my own schedules or "
                f"'all' for everything on the user's calendar."
            )
        if not 1 <= days_ahead <= _MAX_DAYS_AHEAD:
            return _error(f"days_ahead must be between 1 and {_MAX_DAYS_AHEAD}; got {days_ahead}.")
        if parsed is ScheduleScope.MINE and persona_id is None:
            return _error(
                "I can't tell which schedules are mine in this context. Ask again with "
                "scope='all' to see everything on the user's calendar."
            )

        reader = reader_provider()
        if reader is None:
            return _error("No calendar access right now.")

        start = now_fn()
        end = start + timedelta(days=days_ahead)
        agenda = reader.read_agenda(
            start=start,
            end=end,
            persona_id=persona_id if parsed is ScheduleScope.MINE else None,
        )
        return ToolResult(
            tool_name=SCHEDULE_INTROSPECT_TOOL_NAME,
            content=_render_agenda(agenda, scope=parsed, days_ahead=days_ahead),
            data={"scope": parsed.value, **agenda.model_dump(mode="json")},
            truncated=agenda.truncated,
        )

    return schedule_introspect


def _error(message: str) -> ToolResult:
    """An honest, model-readable failure (never a silently emptier answer)."""
    return ToolResult(
        tool_name=SCHEDULE_INTROSPECT_TOOL_NAME,
        content=message,
        is_error=True,
    )


def _render_agenda(agenda: ScheduleAgenda, *, scope: ScheduleScope, days_ahead: int) -> str:
    """Render the agenda for the model — scope-labelled, honest about caps."""
    window = "the next day" if days_ahead == 1 else f"the next {days_ahead} days"
    header_scope = _SCOPE_PHRASE[scope]
    if not agenda.occurrences:
        return f"Nothing is scheduled in {window} ({header_scope})."
    lines = [f"Scheduled in {window} ({header_scope}):"]
    lines.extend(_render_occurrence(o) for o in _sorted_occurrences(agenda.occurrences))
    if agenda.truncated:
        lines.append(
            "That list was capped, so there may be more — ask for a shorter window to see the rest."
        )
    return "\n".join(lines)


def _render_occurrence(occurrence: ScheduledOccurrence) -> str:
    """One line: when it fires (in its own zone), what it does, how often, and its id."""
    parts = [_format_local(occurrence.fire_at, occurrence.timezone)]
    if occurrence.subject:
        parts.append(occurrence.subject)
    if occurrence.human_terms:
        parts.append(occurrence.human_terms)
    parts.append(f"schedule {occurrence.schedule_id}")
    return "- " + " · ".join(parts)


def _format_local(fire_at: datetime, timezone: str) -> str:
    """Format a fire in the schedule's captured zone (the frame it was meant in).

    An unknown/corrupt stored zone degrades to UTC rather than failing the whole read — a
    persona that can list nine of ten commitments is more useful than one that lists none.
    """
    zone: tzinfo
    try:
        zone = ZoneInfo(timezone)
        label = timezone
    except (ZoneInfoNotFoundError, ValueError):
        zone, label = UTC, "UTC"
    local = fire_at.astimezone(zone)
    return f"{local:%a %d %b %Y, %H:%M} {label}"


def _sorted_occurrences(occurrences: Sequence[ScheduledOccurrence]) -> list[ScheduledOccurrence]:
    """Soonest first (the reader already orders; this is the belt-and-braces for fakes)."""
    return sorted(occurrences, key=lambda o: o.fire_at)
