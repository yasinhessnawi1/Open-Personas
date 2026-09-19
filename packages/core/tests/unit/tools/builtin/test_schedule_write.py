"""The persona's calendar write tools: book a one-off, remove an entry (issue 13).

Fake ports, no DB. What is proved here is the tool's own contract: the time a spoken phrase
resolves to, the key that makes a retry one booking rather than two, the confirmation a user
can actually read, and the gate that stops a never-seen id becoming a deletion. Whether the
durable write lands is the integration test's job, through the real services.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.errors import ScheduleNotFoundError
from persona.schedules import ScheduleDisclosureLedger
from persona.tools.builtin.schedule_write import (
    BookedSchedule,
    RemovedSchedule,
    make_schedule_book_once_tool,
    make_schedule_remove_tool,
)

if TYPE_CHECKING:
    from persona.tools.protocol import AsyncTool

_NOW = datetime(2026, 9, 18, 11, 20, 37, tzinfo=UTC)
_OSLO = "Europe/Oslo"


class _FakeBookingPort:
    """Records every booking it was asked for; answers as the create door would."""

    def __init__(self, *, timezone: str = _OSLO, created: bool = True) -> None:
        self._timezone = timezone
        self._created = created
        self.bookings: list[tuple[str, datetime, str]] = []

    def timezone(self) -> str:
        return self._timezone

    def book_once(self, *, subject: str, fire_at: datetime, idempotency_key: str) -> BookedSchedule:
        self.bookings.append((subject, fire_at, idempotency_key))
        return BookedSchedule(
            schedule_id="sch_booked",
            task_id="task_booked",
            created=self._created,
            subject=subject,
            fire_at=fire_at,
            timezone=self._timezone,
            human_terms=f"once, at {fire_at.isoformat()}",
        )


class _FakeRemovalPort:
    """Removes what it is given, or reports the entry absent."""

    def __init__(self, *, known: set[str] | None = None) -> None:
        self._known = known if known is not None else {"sch_hn"}
        self.removed: list[str] = []

    def remove(self, schedule_id: str) -> RemovedSchedule:
        if schedule_id not in self._known:
            raise ScheduleNotFoundError("absent", context={"schedule_id": schedule_id})
        self.removed.append(schedule_id)
        return RemovedSchedule(
            schedule_id=schedule_id,
            subject="Hacker News brief",
            human_terms="every day at 07:00",
            paused_task_id="task_hn",
        )


def _book_tool(port: _FakeBookingPort | None, *, persona_id: str | None = "astrid") -> AsyncTool:
    return make_schedule_book_once_tool(
        port_provider=lambda: port,
        persona_id=persona_id,
        clock=lambda: _NOW,
    )


def _remove_tool(
    port: _FakeRemovalPort | None,
    *,
    shown: list[str] | None = None,
    disclosures_available: bool = True,
) -> AsyncTool:
    ledger = ScheduleDisclosureLedger()
    if shown:
        ledger.record("user_a", shown)
    bound = ledger.bind("user_a") if disclosures_available else None
    return make_schedule_remove_tool(
        port_provider=lambda: port,
        disclosure_provider=lambda: bound,
    )


# ----------------------------------------------------------------------------------------
# schedule_book_once
# ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_booking_an_hour_from_now_lands_one_run_at_the_right_time() -> None:
    """The ask from issue 13, end to end through the tool: one booking, the right instant."""
    port = _FakeBookingPort()
    result = await _book_tool(port).execute(goal="Redraw the Gantt chart", when="in one hour")

    assert result.is_error is False
    assert len(port.bookings) == 1
    subject, fire_at, _key = port.bookings[0]
    assert subject == "Redraw the Gantt chart"
    assert fire_at == datetime(2026, 9, 18, 12, 20, tzinfo=UTC)


@pytest.mark.asyncio
async def test_the_confirmation_says_when_what_and_how_to_change_it() -> None:
    """The user reads this sentence, not the JSON; it has to carry all three."""
    result = await _book_tool(_FakeBookingPort()).execute(
        goal="Redraw the Gantt chart", when="in one hour"
    )

    assert "Redraw the Gantt chart" in result.content
    assert "14:20 Europe/Oslo" in result.content  # the user's own wall clock, not UTC
    assert "Schedule page" in result.content
    assert "sch_booked" in result.content


@pytest.mark.asyncio
async def test_the_spoken_time_is_resolved_in_the_users_timezone() -> None:
    """ "tomorrow 09:00" is 09:00 where they are; the port supplies the zone."""
    port = _FakeBookingPort(timezone="America/New_York")
    await _book_tool(port).execute(goal="Call the bank", when="tomorrow 09:00")

    _subject, fire_at, _key = port.bookings[0]
    assert fire_at == datetime(2026, 9, 19, 13, 0, tzinfo=UTC)  # 09:00 EDT


@pytest.mark.asyncio
async def test_a_retry_in_the_same_minute_carries_the_same_key() -> None:
    """A model retrying its own call is one intention; the key is what makes it converge."""
    port = _FakeBookingPort()
    tool = _book_tool(port)
    await tool.execute(goal="Redraw the Gantt chart", when="in one hour")
    await tool.execute(goal="Redraw the Gantt chart", when="in one hour")

    assert port.bookings[0][2] == port.bookings[1][2]


@pytest.mark.asyncio
async def test_a_different_time_is_a_different_booking() -> None:
    """ "Remind me again in two hours" is a second commitment, not a duplicate."""
    port = _FakeBookingPort()
    tool = _book_tool(port)
    await tool.execute(goal="Redraw the Gantt chart", when="in one hour")
    await tool.execute(goal="Redraw the Gantt chart", when="in 2 hours")

    assert port.bookings[0][2] != port.bookings[1][2]


@pytest.mark.asyncio
async def test_an_idempotent_replay_is_reported_as_one_not_two() -> None:
    """The door says it already existed, so the persona must not claim it booked anew."""
    result = await _book_tool(_FakeBookingPort(created=False)).execute(
        goal="Redraw the Gantt chart", when="in one hour"
    )

    assert "already on the books" in result.content
    assert result.data["created"] is False


@pytest.mark.asyncio
async def test_an_unreadable_time_declines_and_offers_the_forms() -> None:
    """No approximation: a vague phrase gets the vocabulary back, and nothing is booked."""
    port = _FakeBookingPort()
    result = await _book_tool(port).execute(goal="Redraw the Gantt chart", when="soon")

    assert result.is_error is True
    assert "ISO" in result.content
    assert port.bookings == []


@pytest.mark.asyncio
async def test_a_time_in_the_past_books_nothing() -> None:
    """A one-off behind the clock would never fire; refuse before the write."""
    port = _FakeBookingPort()
    result = await _book_tool(port).execute(goal="Redraw the Gantt chart", when="2026-09-17T09:00")

    assert result.is_error is True
    assert port.bookings == []


@pytest.mark.asyncio
async def test_an_empty_goal_books_nothing() -> None:
    """A schedule with no subject is an entry nobody can read later."""
    port = _FakeBookingPort()
    result = await _book_tool(port).execute(goal="   ", when="in one hour")

    assert result.is_error is True
    assert port.bookings == []


@pytest.mark.asyncio
async def test_off_request_booking_fails_closed() -> None:
    """No caller bound means no calendar; it says so instead of writing somewhere."""
    result = await _book_tool(None).execute(goal="Redraw the Gantt chart", when="in one hour")

    assert result.is_error is True
    assert "calendar access" in result.content


# ----------------------------------------------------------------------------------------
# schedule_remove
# ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_removing_a_shown_entry_takes_it_off_and_confirms_in_one_sentence() -> None:
    """The other half of issue 13: the 07:00 brief the user asked to drop actually goes."""
    port = _FakeRemovalPort()
    result = await _remove_tool(port, shown=["sch_hn"]).execute(schedule_id="sch_hn")

    assert result.is_error is False
    assert port.removed == ["sch_hn"]
    assert "Hacker News brief" in result.content
    assert "every day at 07:00" in result.content
    assert "paused" in result.content


@pytest.mark.asyncio
async def test_an_unshown_id_asks_first_and_removes_nothing() -> None:
    """The gate. A plausible id nobody was shown is never a deletion."""
    port = _FakeRemovalPort()
    result = await _remove_tool(port, shown=[]).execute(schedule_id="sch_hn")

    assert port.removed == []
    assert result.data["removed"] is False
    assert result.data["reason"] == "not_shown"
    assert "schedule_introspect" in result.content


@pytest.mark.asyncio
async def test_the_unshown_refusal_says_nothing_about_whether_the_entry_exists() -> None:
    """A refusal that leaked existence would turn the gate into an oracle."""
    real = await _remove_tool(_FakeRemovalPort(known={"sch_hn"}), shown=[]).execute(
        schedule_id="sch_hn"
    )
    invented = await _remove_tool(_FakeRemovalPort(known={"sch_hn"}), shown=[]).execute(
        schedule_id="sch_nonsense"
    )

    assert real.content.replace("sch_hn", "X") == invented.content.replace("sch_nonsense", "X")


@pytest.mark.asyncio
async def test_a_shown_id_that_is_gone_reports_absence_honestly() -> None:
    """Shown earlier, deleted since: say it is not there, never claim a removal."""
    port = _FakeRemovalPort(known=set())
    result = await _remove_tool(port, shown=["sch_hn"]).execute(schedule_id="sch_hn")

    assert result.is_error is True
    assert port.removed == []


@pytest.mark.asyncio
async def test_no_disclosure_record_at_all_asks_rather_than_acts() -> None:
    """Off-request, or a process that never saw the reading: the safe direction is to ask."""
    port = _FakeRemovalPort()
    result = await _remove_tool(port, disclosures_available=False).execute(schedule_id="sch_hn")

    assert port.removed == []
    assert result.data["reason"] == "not_shown"


@pytest.mark.asyncio
async def test_an_empty_id_removes_nothing() -> None:
    """A blank id is a mis-call, not a wildcard."""
    port = _FakeRemovalPort()
    result = await _remove_tool(port, shown=["sch_hn"]).execute(schedule_id="  ")

    assert result.is_error is True
    assert port.removed == []


@pytest.mark.asyncio
async def test_off_request_removal_fails_closed() -> None:
    """No caller bound means no calendar to remove from."""
    result = await _remove_tool(None, shown=["sch_hn"]).execute(schedule_id="sch_hn")

    assert result.is_error is True
    assert "calendar access" in result.content
