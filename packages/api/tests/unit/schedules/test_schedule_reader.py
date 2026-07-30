"""APIScheduleReader maps the calendar's read model onto the core agenda (R9-075).

No DB: the occurrences service is substituted so the assertions are about the MAPPING and
the owner/persona scoping the reader passes down, not about SQL (the service's own RLS
behaviour is covered by its integration tests).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from persona.schedules import ScheduleReader
from persona_api.config import APIConfig
from persona_api.schedules import reader as reader_module
from persona_api.schedules.reader import APIScheduleReader
from persona_api.services.occurrences_service import Occurrence, OccurrencesResult

_NOW = datetime(2026, 8, 3, 6, 0, tzinfo=UTC)


class _Recorder:
    """Stands in for ``list_occurrences``; records the kwargs it was called with."""

    def __init__(self, result: OccurrencesResult) -> None:
        self._result = result
        self.kwargs: dict[str, Any] = {}

    def __call__(self, engine: Any, **kwargs: Any) -> OccurrencesResult:  # noqa: ANN401
        self.kwargs = {"engine": engine, **kwargs}
        return self._result


def _result(*, truncated: bool = False) -> OccurrencesResult:
    return OccurrencesResult(
        occurrences=(
            Occurrence(
                schedule_id="sched-1",
                task_id="task-1",
                persona_id="astrid",
                fire_at=_NOW + timedelta(hours=2),
                timezone="Europe/Oslo",
                human_terms="every weekday at 08:00",
                subject="Morning briefing",
            ),
        ),
        history=(),
        window_from=_NOW,
        window_to=_NOW + timedelta(days=7),
        truncated=truncated,
    )


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder(_result())
    monkeypatch.setattr(reader_module, "list_occurrences", rec)
    return rec


def _reader() -> APIScheduleReader:
    return APIScheduleReader(None, APIConfig(), "user-a")  # type: ignore[arg-type]


def test_satisfies_the_core_reader_protocol() -> None:
    assert isinstance(_reader(), ScheduleReader)


def test_binds_the_owner_and_passes_the_window_down(recorder: _Recorder) -> None:
    end = _NOW + timedelta(days=7)
    _reader().read_agenda(start=_NOW, end=end, persona_id=None)

    assert recorder.kwargs["owner_id"] == "user-a"
    assert recorder.kwargs["from_"] == _NOW
    assert recorder.kwargs["to"] == end
    assert recorder.kwargs["persona_id"] is None


def test_mine_scope_passes_the_persona_filter_through(recorder: _Recorder) -> None:
    _reader().read_agenda(start=_NOW, end=_NOW + timedelta(days=1), persona_id="astrid")
    assert recorder.kwargs["persona_id"] == "astrid"


def test_maps_every_occurrence_field(recorder: _Recorder) -> None:
    agenda = _reader().read_agenda(start=_NOW, end=_NOW + timedelta(days=7), persona_id=None)

    assert recorder.kwargs  # the stand-in was the one that answered
    assert len(agenda.occurrences) == 1
    occurrence = agenda.occurrences[0]
    assert occurrence.schedule_id == "sched-1"
    assert occurrence.task_id == "task-1"
    assert occurrence.persona_id == "astrid"
    assert occurrence.fire_at == _NOW + timedelta(hours=2)
    assert occurrence.timezone == "Europe/Oslo"
    assert occurrence.human_terms == "every weekday at 08:00"
    assert occurrence.subject == "Morning briefing"


def test_carries_the_effective_window_and_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server's horizon clamp and cap reach the persona intact, not silently dropped."""
    monkeypatch.setattr(reader_module, "list_occurrences", _Recorder(_result(truncated=True)))

    agenda = _reader().read_agenda(start=_NOW, end=_NOW + timedelta(days=400), persona_id=None)

    assert agenda.truncated is True
    assert agenda.window_from == _NOW
    assert agenda.window_to == _NOW + timedelta(days=7)  # the EFFECTIVE end, not the asked-for one
