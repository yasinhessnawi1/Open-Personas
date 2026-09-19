"""The ``schedule_introspect`` tool — the persona's window onto the calendar (R9-075).

A model-callable tool that answers "what's on my calendar this week?", "am I about to
double-book you?", and "when does that run again?" from computed schedule occurrences and
nothing else. It is the read half the platform was missing: a persona could create a
schedule (A1/A10) and the web calendar could render one (A8), but the toolbox had no way to
*look* at either — so the persona guessed, or said it couldn't know.

This tool itself only reads, but reading is no longer all the persona can do: the write door
is :mod:`persona.tools.builtin.schedule_write` (``schedule_book_once`` to put a one-off on
the calendar, ``schedule_remove`` to take an entry off it). That matters here because a
persona that believes its access is read-only says so, and until the write tools shipped it
was saying it truthfully. It must not keep saying it now. The ids this tool reports are also
what licenses a removal: every schedule it lists is recorded as SHOWN to the user, and
``schedule_remove`` refuses an id that no calendar read ever put in front of them.

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
``record_user_fact`` precedent). CQS: this tool answers a question about the calendar and
changes nothing on it: it cannot create, edit, pause, or cancel. The disclosure it records is
not calendar state; it is the note that the user has now seen these entries, which is what the
delete gate reads.
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

    from persona.schedules.disclosure import ScheduleDisclosures
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
    "may be more. Never invent an entry, a time, or a cadence. Your calendar access is not "
    "read-only: use `schedule_book_once` to put a one-off run on the calendar, and "
    "`schedule_remove` with a schedule id from THIS tool's answer to take an entry off it."
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
    disclosure_provider: Callable[[], ScheduleDisclosures | None] | None = None,
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
        disclosure_provider: Resolves the caller's disclosure record at DISPATCH time, so
            every schedule this read shows the user becomes an id ``schedule_remove`` will
            act on. ``None`` (the CLI / test path) ⇒ nothing is recorded, which only ever
            makes a later removal ask first.

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
        _record_disclosure(disclosure_provider, agenda)
        return ToolResult(
            tool_name=SCHEDULE_INTROSPECT_TOOL_NAME,
            content=_render_agenda(agenda, scope=parsed, days_ahead=days_ahead),
            data={"scope": parsed.value, **agenda.model_dump(mode="json")},
            truncated=agenda.truncated,
        )

    return schedule_introspect


def _record_disclosure(
    disclosure_provider: Callable[[], ScheduleDisclosures | None] | None,
    agenda: ScheduleAgenda,
) -> None:
    """Note every listed schedule as shown to this user: the delete gate's only input.

    Fail-soft on purpose: the read the user asked for is the point of the call, and a
    disclosure that does not land costs them one extra confirmation later, never a wrong
    answer and never a wrong delete.
    """
    if disclosure_provider is None:
        return
    disclosures = disclosure_provider()
    if disclosures is None:
        return
    disclosures.record({occurrence.schedule_id for occurrence in agenda.occurrences})


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
    lines.extend(_render_schedule(group) for group in _grouped_by_schedule(agenda.occurrences))
    if agenda.truncated:
        lines.append(
            "That list was capped, so there may be more — ask for a shorter window to see the rest."
        )
    return "\n".join(lines)


def _grouped_by_schedule(
    occurrences: Sequence[ScheduledOccurrence],
) -> list[list[ScheduledOccurrence]]:
    """Occurrences gathered per schedule, each group soonest-first, groups by next fire.

    A recurring schedule is ONE commitment. Listing it once per fire says the same sentence
    168 times for an hourly rule over a week, and the repetition is not free: this is a tool
    result a model reads back into its context on every ask.
    """
    groups: dict[str, list[ScheduledOccurrence]] = {}
    for occurrence in _sorted_occurrences(occurrences):
        groups.setdefault(occurrence.schedule_id, []).append(occurrence)
    return sorted(groups.values(), key=lambda g: g[0].fire_at)


def _render_schedule(group: list[ScheduledOccurrence]) -> str:
    """One line per SCHEDULE: what it does, how often, when it next fires, and its id.

    The count is stated only when a schedule fires more than once in the window, because
    "1 time" is noise and because the count is the thing the cadence alone cannot tell you:
    "every hour" over seven days is 168 real firings, and a person deciding whether to keep
    a routine wants that number in front of them.
    """
    first = group[0]
    parts: list[str] = []
    if first.subject:
        parts.append(first.subject)
    if first.human_terms:
        parts.append(first.human_terms)
    parts.append(f"next {_format_local(first.fire_at, first.timezone)}")
    if len(group) > 1:
        parts.append(f"{len(group)} times in this window")
    parts.append(f"schedule {first.schedule_id}")
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
