"""The schedule read contract — scope vocabulary + agenda shapes (R9-075)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.errors import InvalidScheduleScopeError
from persona.schedules import (
    ScheduleAgenda,
    ScheduledOccurrence,
    ScheduleReader,
    ScheduleScope,
    parse_schedule_scope,
)
from pydantic import ValidationError

_NOW = datetime(2026, 8, 3, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("mine", ScheduleScope.MINE),
        ("all", ScheduleScope.ALL),
        ("  ALL  ", ScheduleScope.ALL),
        ("Mine", ScheduleScope.MINE),
        ("", ScheduleScope.MINE),  # empty ⇒ the persona's own commitments
    ],
)
def test_parse_schedule_scope_accepts_the_vocabulary(raw: str, expected: ScheduleScope) -> None:
    assert parse_schedule_scope(raw) is expected


@pytest.mark.parametrize("raw", ["everyone", "ours", "user", "mine;all"])
def test_parse_schedule_scope_rejects_unknown_names(raw: str) -> None:
    """A wrong scope is a wrong ANSWER, so it is rejected, never coerced to a default."""
    with pytest.raises(InvalidScheduleScopeError) as exc:
        parse_schedule_scope(raw)
    assert exc.value.context["scope"] == raw
    assert "mine" in exc.value.context["supported"]
    assert "all" in exc.value.context["supported"]


def test_occurrence_is_frozen_and_forbids_extras() -> None:
    occurrence = ScheduledOccurrence(
        schedule_id="sched-1",
        fire_at=_NOW,
        timezone="Europe/Oslo",
        human_terms="every day at 08:00",
    )
    with pytest.raises(ValidationError):
        occurrence.schedule_id = "sched-2"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ScheduledOccurrence(
            schedule_id="sched-1",
            fire_at=_NOW,
            timezone="Europe/Oslo",
            human_terms="daily",
            rrule="FREQ=DAILY",  # type: ignore[call-arg]
        )


def test_agenda_defaults_to_empty_and_untruncated() -> None:
    agenda = ScheduleAgenda(window_from=_NOW, window_to=_NOW)
    assert agenda.occurrences == ()
    assert agenda.truncated is False


def test_reader_protocol_is_structurally_satisfied() -> None:
    """Any owner-bound object with ``read_agenda`` IS a reader (no inheritance needed)."""

    class _Reader:
        def read_agenda(
            self, *, start: datetime, end: datetime, persona_id: str | None
        ) -> ScheduleAgenda:
            assert persona_id is None or isinstance(persona_id, str)
            return ScheduleAgenda(window_from=start, window_to=end)

    assert isinstance(_Reader(), ScheduleReader)


def test_reader_protocol_rejects_a_non_reader() -> None:
    class _NotAReader:
        def list_schedules(self) -> list[str]:
            return []

    assert not isinstance(_NotAReader(), ScheduleReader)
