"""The persona's write door onto the calendar: book a one-off, remove a routine.

``schedule_introspect`` gave a persona a window onto the calendar and nothing else, so a
user who asked for a Gantt redraw "an hour from now" got an honest, useless answer: "my
scheduling access is currently read-only". The entry they asked to have removed was still
there the next time they looked. This module is the other half.

Two tools, deliberately narrow:

* ``schedule_book_once`` takes a goal and a time and books ONE run. Not a cadence, not a
  routine: the recurring door is the calendar's own New routine dialog, and a tool that
  could quietly commit a user to something every morning is a different risk. The time is
  resolved by :func:`persona.schedules.whenphrase.resolve_when_phrase` against the same
  clock the ``datetime`` tool reads and the user's own timezone, so "in one hour" means the
  hour from now that the persona would also report if asked.
* ``schedule_remove`` removes ONE existing entry by the id ``schedule_introspect`` showed.
  It will not act on an id the user has not been shown (see the disclosure gate below).

Everything that is a scope or a policy question lives behind the injected ports, which the
composition root binds to this caller and this persona and resolves per dispatch. Neither
tool takes an owner, a persona, or a cadence: a tool that can be talked into a wider reach
by a well-chosen argument is not a narrow tool.

**Booking needs no second confirmation, and that is a ruling, not an oversight.** The
create door the New routine dialog uses (``POST /v1/me/schedule``) has an optional preview
route beside it; the preview is a dialog affordance, and the create service itself requires
nothing before it writes. In chat the user's ask IS the explicit confirmation the A10 door
was designed around, and the tool returns the same echo the dialog shows: when it runs,
what it will do, and how to change it.

**Removing does need the entry in front of the user first.** It is destructive, it is not
undoable, and a model can produce a plausible id it never read. So the id must have come
out of a real calendar read that the user saw (the disclosure ledger,
:mod:`persona.schedules.disclosure`). An id with no such reading is refused with the
obvious remedy: look at the calendar, then ask again. That refusal says nothing about
whether the id exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo  # noqa: TC003 (a runtime Pydantic field type)
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict

from persona.errors import ScheduleNotFoundError, ScheduleTimePhraseError
from persona.schedules.whenphrase import SUPPORTED_WHEN_FORMS, resolve_when_phrase
from persona.schema.tools import ToolResult
from persona.tools.protocol import tool

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.schedules.disclosure import ScheduleDisclosures
    from persona.tools.protocol import AsyncTool

__all__ = [
    "SCHEDULE_BOOK_ONCE_TOOL_NAME",
    "SCHEDULE_REMOVE_TOOL_NAME",
    "BookedSchedule",
    "RemovedSchedule",
    "ScheduleBookingPort",
    "ScheduleRemovalPort",
    "make_schedule_book_once_tool",
    "make_schedule_remove_tool",
]

#: The registered tool names. Exported so the composition root auto-allows them by
#: reference rather than by a duplicated string literal.
SCHEDULE_BOOK_ONCE_TOOL_NAME = "schedule_book_once"
SCHEDULE_REMOVE_TOOL_NAME = "schedule_remove"

#: Defensive cap on the goal line (matches the create door's own subject cap).
_GOAL_MAX_LENGTH = 500

_BOOK_GUIDANCE = (
    "Book ONE run at a specific time: the user asks you to do something later, or again at "
    "a set moment, and you put it on their real calendar. goal is what you will do, in a "
    "sentence they would recognise. when is the time in their own words: "
    f"{SUPPORTED_WHEN_FORMS}. The time is resolved against the real clock and their "
    "timezone, so you do not need to work the arithmetic out first. This books a ONE-OFF; "
    "for anything recurring, say that the New routine dialog on the Schedule page sets a "
    "repeating cadence. Report what this tool returns and nothing more: if it says the "
    "booking was already on the books, say that rather than claiming you made a new one."
)

_REMOVE_GUIDANCE = (
    "Remove one entry from the user's calendar, by the schedule id that `schedule_introspect` "
    "reported. Use it when they ask you to cancel, delete, stop or get rid of something that "
    "is scheduled. Removing is permanent, so give the id exactly as the calendar read gave "
    "it: if you have not read the calendar with them in this conversation, call "
    "`schedule_introspect` first and take the id from its answer. If the entry drives a "
    "standing task, that task is paused too, and the tool says so. Report the result as "
    "given; never say something was removed unless the tool said it was."
)


class BookedSchedule(BaseModel):
    """What the create door did, in the terms the persona reads back to the user.

    ``created`` is ``False`` on an idempotent replay: the same goal at the same minute was
    already booked, so the ids are the same and nothing was written twice. That is a real
    answer, not a failure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_id: str
    task_id: str
    created: bool
    subject: str
    fire_at: datetime
    timezone: str
    human_terms: str
    quiet_hours_offer: str | None = None


class RemovedSchedule(BaseModel):
    """What came off the calendar, so the confirmation can name it rather than its id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_id: str
    subject: str | None = None
    human_terms: str = ""
    paused_task_id: str | None = None


@runtime_checkable
class ScheduleBookingPort(Protocol):
    """The durable create seam, bound by the composition root to one caller and persona.

    The implementation owns every scope question: the caller's owner (resolved per dispatch),
    the executing persona, the audit actor, and the guards the calendar's own create door
    applies. It is a write surface only; nothing here reads the calendar back.
    """

    def timezone(self) -> str:
        """The caller's own IANA zone: the frame a spoken time is meant in."""
        ...

    def book_once(self, *, subject: str, fire_at: datetime, idempotency_key: str) -> BookedSchedule:
        """Create the one-off run, or converge on the existing one for the same key."""
        ...


@runtime_checkable
class ScheduleRemovalPort(Protocol):
    """The durable delete seam, bound by the composition root to one caller.

    Raises :class:`~persona.errors.ScheduleNotFoundError` for anything outside the caller's
    reach, so another tenant's entry and an id that never existed are one answer.
    """

    def remove(self, schedule_id: str) -> RemovedSchedule:
        """Delete the entry, record the user's intent, and report what came off."""
        ...


def make_schedule_book_once_tool(
    *,
    port_provider: Callable[[], ScheduleBookingPort | None],
    persona_id: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> AsyncTool:
    """Build the ``schedule_book_once`` tool over an owner-scoped booking port.

    Args:
        port_provider: Resolves the caller's port at DISPATCH time (the toolbox is built
            once; the owner is per request). ``None`` fails closed, as off-request it must.
        persona_id: The persona booking. Load bearing for the idempotency key, so a retry
            within the same minute converges instead of double-booking.
        clock: Returns "now" (tz-aware UTC): the anchor every relative time is resolved
            against, and the same source of truth the ``datetime`` tool reads. Injectable so
            the resolution is deterministic under test.

    Returns:
        The registered :class:`~persona.tools.protocol.AsyncTool`.
    """
    now_fn = clock if clock is not None else lambda: datetime.now(UTC)

    @tool(name=SCHEDULE_BOOK_ONCE_TOOL_NAME, description=_BOOK_GUIDANCE)
    async def schedule_book_once(goal: str, when: str) -> ToolResult:
        port = port_provider()
        if port is None:
            return _book_error("No calendar access right now, so I cannot book anything.")
        subject = " ".join(goal.split())[:_GOAL_MAX_LENGTH]
        if not subject:
            return _book_error("Tell me what to book: give the goal in a sentence.")

        try:
            fire_at = resolve_when_phrase(when, now=now_fn(), timezone=port.timezone())
        except ScheduleTimePhraseError as exc:
            return _book_error(f"{exc.message}. Give me {SUPPORTED_WHEN_FORMS}.")

        booked = port.book_once(
            subject=subject,
            fire_at=fire_at,
            idempotency_key=_booking_key(persona_id, subject, fire_at),
        )
        return ToolResult(
            tool_name=SCHEDULE_BOOK_ONCE_TOOL_NAME,
            content=_render_booking(booked),
            data=booked.model_dump(mode="json"),
        )

    return schedule_book_once


def make_schedule_remove_tool(
    *,
    port_provider: Callable[[], ScheduleRemovalPort | None],
    disclosure_provider: Callable[[], ScheduleDisclosures | None],
) -> AsyncTool:
    """Build the ``schedule_remove`` tool over an owner-scoped removal port.

    Args:
        port_provider: Resolves the caller's delete seam at DISPATCH time. ``None`` fails
            closed.
        disclosure_provider: Resolves the caller's disclosure record at DISPATCH time. The
            gate that keeps a never-read id from becoming a delete. ``None`` means the gate
            cannot be checked, which is treated as "not shown": it asks rather than acts.

    Returns:
        The registered :class:`~persona.tools.protocol.AsyncTool`.
    """

    @tool(name=SCHEDULE_REMOVE_TOOL_NAME, description=_REMOVE_GUIDANCE)
    async def schedule_remove(schedule_id: str) -> ToolResult:
        port = port_provider()
        if port is None:
            return _remove_error("No calendar access right now, so I cannot remove anything.")
        wanted = schedule_id.strip()
        if not wanted:
            return _remove_error(
                "Tell me which entry: give the schedule id from schedule_introspect."
            )

        disclosures = disclosure_provider()
        if disclosures is None or not disclosures.was_shown(wanted):
            # Not a refusal about that id's existence: it says nothing either way. It is the
            # rule that a delete follows something the user actually saw.
            return ToolResult(
                tool_name=SCHEDULE_REMOVE_TOOL_NAME,
                content=(
                    f"I have not read {wanted} off the calendar with the user in this "
                    "conversation, and I will not delete something they have not seen. "
                    "Call schedule_introspect, show them the entry, then ask again with the "
                    "id it reports."
                ),
                data={"removed": False, "reason": "not_shown", "schedule_id": wanted},
            )

        try:
            removed = port.remove(wanted)
        except ScheduleNotFoundError:
            return _remove_error(f"There is no calendar entry with id {wanted!r}.")
        return ToolResult(
            tool_name=SCHEDULE_REMOVE_TOOL_NAME,
            content=_render_removal(removed),
            data=removed.model_dump(mode="json"),
        )

    return schedule_remove


def _booking_key(persona_id: str | None, subject: str, fire_at: datetime) -> str:
    """A stable key for this booking: same persona, same goal, same minute is one booking.

    The calendar dialog deliberately does NOT dedup on content, because two submits are two
    deliberate acts by a person. A model is not a person clicking twice: a retried tool call
    inside the same minute is one intention, and the user cannot tell two identical entries
    apart afterwards. Different minutes stay different bookings, so "remind me again in an
    hour" still works exactly as asked.
    """
    from hashlib import sha256

    digest = sha256(f"{persona_id or '-'}|{subject}|{fire_at.isoformat()}".encode()).hexdigest()
    return f"persona-book-{digest[:32]}"


def _book_error(message: str) -> ToolResult:
    """An honest, model-readable failure (never a quietly different booking)."""
    return ToolResult(tool_name=SCHEDULE_BOOK_ONCE_TOOL_NAME, content=message, is_error=True)


def _remove_error(message: str) -> ToolResult:
    """An honest, model-readable failure (never a claim that something was removed)."""
    return ToolResult(tool_name=SCHEDULE_REMOVE_TOOL_NAME, content=message, is_error=True)


def _render_booking(booked: BookedSchedule) -> str:
    """The confirmation a person can read: when, what, and how to change it."""
    local = _format_local(booked.fire_at, booked.timezone)
    opening = "Booked" if booked.created else "That was already on the books"
    lines = [
        f"{opening}: {booked.subject}, {local}, once. "
        f"Change or cancel it on the Schedule page, or just say the word here. "
        f"Schedule id {booked.schedule_id}."
    ]
    if booked.quiet_hours_offer:
        lines.append(
            f"That lands inside their quiet hours; {booked.quiet_hours_offer} is the nearest "
            "edge if they would rather move it."
        )
    return " ".join(lines)


def _render_removal(removed: RemovedSchedule) -> str:
    """One sentence: what came off, and whether anything standing stopped with it."""
    named = removed.subject or f"the entry {removed.schedule_id}"
    cadence = f" ({removed.human_terms})" if removed.human_terms else ""
    sentence = f"Removed {named}{cadence} from the calendar; it will not run again."
    if removed.paused_task_id:
        sentence += " The standing task behind it is paused, so nothing re-arms it."
    return sentence


def _format_local(fire_at: datetime, timezone: str) -> str:
    """Format the fire in the schedule's own zone: the frame the user meant it in."""
    zone: tzinfo
    try:
        zone = ZoneInfo(timezone)
        label = timezone
    except (ZoneInfoNotFoundError, ValueError):
        zone, label = UTC, "UTC"
    local = fire_at.astimezone(zone)
    return f"{local:%a %d %b %Y, %H:%M} {label}"
