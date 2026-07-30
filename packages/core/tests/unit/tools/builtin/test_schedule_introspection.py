"""Unit tests for the schedule_introspect tool (R9-075) — fake reader, no DB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.schedules import ScheduleAgenda, ScheduledOccurrence
from persona.tools.builtin.schedule_introspection import make_schedule_introspection_tool

if TYPE_CHECKING:
    from persona.tools.protocol import AsyncTool

_NOW = datetime(2026, 8, 3, 6, 0, tzinfo=UTC)


def _occurrence(
    schedule_id: str,
    *,
    fire_at: datetime,
    timezone: str = "Europe/Oslo",
    human_terms: str = "every weekday at 08:00",
    subject: str | None = "Morning briefing",
    persona_id: str | None = "astrid",
) -> ScheduledOccurrence:
    return ScheduledOccurrence(
        schedule_id=schedule_id,
        persona_id=persona_id,
        task_id=None,
        fire_at=fire_at,
        timezone=timezone,
        human_terms=human_terms,
        subject=subject,
    )


class _FakeReader:
    """Records the window + persona filter it was asked for; returns a canned agenda."""

    def __init__(
        self,
        *,
        occurrences: list[ScheduledOccurrence] | None = None,
        truncated: bool = False,
    ) -> None:
        self._occurrences = occurrences or []
        self._truncated = truncated
        self.calls: list[tuple[datetime, datetime, str | None]] = []

    def read_agenda(
        self, *, start: datetime, end: datetime, persona_id: str | None
    ) -> ScheduleAgenda:
        self.calls.append((start, end, persona_id))
        return ScheduleAgenda(
            occurrences=tuple(self._occurrences),
            window_from=start,
            window_to=end,
            truncated=self._truncated,
        )


def _tool(reader: _FakeReader | None, *, persona_id: str | None = "astrid") -> AsyncTool:
    return make_schedule_introspection_tool(
        reader_provider=lambda: reader,
        persona_id=persona_id,
        clock=lambda: _NOW,
    )


@pytest.mark.asyncio
async def test_mine_scope_filters_to_this_persona() -> None:
    """``mine`` asks the reader for THIS persona's schedules only."""
    reader = _FakeReader(occurrences=[_occurrence("s1", fire_at=_NOW + timedelta(hours=2))])
    result = await _tool(reader).execute(scope="mine")

    assert result.is_error is False
    assert reader.calls[0][2] == "astrid"
    assert result.data is not None
    assert result.data["scope"] == "mine"
    assert "your own schedules" in result.content


@pytest.mark.asyncio
async def test_all_scope_covers_the_whole_user_calendar() -> None:
    """``all`` drops the persona filter — the scope a double-booking question needs."""
    reader = _FakeReader(
        occurrences=[
            _occurrence("s1", fire_at=_NOW + timedelta(hours=2)),
            _occurrence("s2", fire_at=_NOW + timedelta(days=1), persona_id="tars"),
        ]
    )
    result = await _tool(reader).execute(scope="all")

    assert result.is_error is False
    assert reader.calls[0][2] is None
    assert result.data is not None
    assert result.data["scope"] == "all"
    assert "across all of the user's personas" in result.content
    assert "s1" in result.content
    assert "s2" in result.content


@pytest.mark.asyncio
async def test_default_scope_is_the_personas_own_schedules() -> None:
    reader = _FakeReader()
    await _tool(reader).execute()
    assert reader.calls[0][2] == "astrid"


@pytest.mark.asyncio
async def test_window_runs_from_now_to_days_ahead() -> None:
    reader = _FakeReader()
    await _tool(reader).execute(scope="all", days_ahead=14)

    start, end, _ = reader.calls[0]
    assert start == _NOW
    assert end == _NOW + timedelta(days=14)


@pytest.mark.asyncio
async def test_empty_calendar_says_so_plainly() -> None:
    """Nothing scheduled reads as nothing scheduled — never as an unknown."""
    reader = _FakeReader()
    result = await _tool(reader).execute(scope="all", days_ahead=7)

    assert result.is_error is False
    assert result.content == (
        "Nothing is scheduled in the next 7 days "
        "(everything scheduled across all of the user's personas)."
    )


@pytest.mark.asyncio
async def test_occurrence_line_carries_time_subject_cadence_and_id() -> None:
    reader = _FakeReader(
        occurrences=[_occurrence("sched-abc", fire_at=datetime(2026, 8, 4, 6, 0, tzinfo=UTC))]
    )
    result = await _tool(reader).execute(scope="mine")

    line = result.content.splitlines()[1]
    assert line.startswith("- Tue 04 Aug 2026, 08:00 Europe/Oslo")  # rendered in ITS zone
    assert "Morning briefing" in line
    assert "every weekday at 08:00" in line
    assert "schedule sched-abc" in line


@pytest.mark.asyncio
async def test_occurrences_render_soonest_first() -> None:
    reader = _FakeReader(
        occurrences=[
            _occurrence("later", fire_at=_NOW + timedelta(days=2), subject="Later"),
            _occurrence("sooner", fire_at=_NOW + timedelta(hours=1), subject="Sooner"),
        ]
    )
    result = await _tool(reader).execute(scope="all")

    lines = result.content.splitlines()
    assert "Sooner" in lines[1]
    assert "Later" in lines[2]


@pytest.mark.asyncio
async def test_truncation_is_reported_honestly() -> None:
    reader = _FakeReader(
        occurrences=[_occurrence("s1", fire_at=_NOW + timedelta(hours=2))], truncated=True
    )
    result = await _tool(reader).execute(scope="all", days_ahead=90)

    assert result.truncated is True
    assert "capped" in result.content
    assert result.data is not None
    assert result.data["truncated"] is True


@pytest.mark.asyncio
async def test_unknown_timezone_degrades_to_utc_rather_than_failing() -> None:
    """A corrupt stored zone loses the frame, not the whole answer."""
    reader = _FakeReader(
        occurrences=[
            _occurrence("s1", fire_at=_NOW, timezone="Mars/Olympus", subject="Rover check")
        ]
    )
    result = await _tool(reader).execute(scope="all")

    assert result.is_error is False
    assert "UTC" in result.content
    assert "Rover check" in result.content


@pytest.mark.asyncio
async def test_no_reader_fails_closed() -> None:
    """Off-request (no owner) the tool refuses instead of answering blind."""
    result = await _tool(None).execute(scope="all")

    assert result.is_error is True
    assert result.content == "No calendar access right now."


@pytest.mark.asyncio
async def test_mine_without_a_persona_id_refuses_instead_of_widening() -> None:
    """Unresolvable ``mine`` must not silently become ``all`` — that would over-report."""
    reader = _FakeReader(occurrences=[_occurrence("s1", fire_at=_NOW)])
    result = await _tool(reader, persona_id=None).execute(scope="mine")

    assert result.is_error is True
    assert "scope='all'" in result.content
    assert reader.calls == []


@pytest.mark.asyncio
async def test_unknown_scope_is_rejected_with_the_vocabulary() -> None:
    reader = _FakeReader()
    result = await _tool(reader).execute(scope="everyone")

    assert result.is_error is True
    assert "'mine'" in result.content
    assert "'all'" in result.content
    assert reader.calls == []


@pytest.mark.parametrize("days_ahead", [0, -3, 91, 4000])
@pytest.mark.asyncio
async def test_days_ahead_out_of_range_is_rejected(days_ahead: int) -> None:
    reader = _FakeReader()
    result = await _tool(reader).execute(scope="all", days_ahead=days_ahead)

    assert result.is_error is True
    assert "between 1 and 90" in result.content
    assert reader.calls == []


@pytest.mark.asyncio
async def test_tool_advertises_both_scopes_to_the_model() -> None:
    """The scope distinction has to be visible in the schema, not just the docs."""
    schedule_tool = _tool(_FakeReader())

    assert schedule_tool.name == "schedule_introspect"
    props = schedule_tool.parameters_schema["properties"]
    assert set(props) == {"scope", "days_ahead"}
    assert "scope='mine'" in schedule_tool.description
    assert "scope='all'" in schedule_tool.description


@pytest.mark.asyncio
async def test_data_payload_carries_the_full_agenda() -> None:
    reader = _FakeReader(occurrences=[_occurrence("s1", fire_at=_NOW + timedelta(hours=3))])
    result = await _tool(reader).execute(scope="all", days_ahead=3)

    assert result.data is not None
    assert result.data["scope"] == "all"
    assert result.data["window_from"].startswith("2026-08-03")
    assert len(result.data["occurrences"]) == 1
    assert result.data["occurrences"][0]["schedule_id"] == "s1"
