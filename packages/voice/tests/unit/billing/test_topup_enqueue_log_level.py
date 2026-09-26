"""The voice top-up enqueue logs at DEBUG, never INFO (R9-215, owner ruling 2026-09-26).

Voice reports every charged turn and the api applies the threshold (D-M5-16), so the
enqueue runs once per charged turn on every cloud call, and almost every one is a no-op
at the api. At INFO that printed one line per turn for the length of a call. The line
an operator reads is the api's INFO line for a charge it actually made.

Driven through the REAL :func:`enqueue_auto_topup` over an engine double that answers
the INSERT the way Postgres does, with a loguru sink scoped to this module's component.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from loguru import logger as _loguru_logger
from persona_voice.billing.topup_enqueue import enqueue_auto_topup

if TYPE_CHECKING:
    from collections.abc import Iterator

_COMPONENT = "voice.topup_enqueue"


class _Result:
    def __init__(self, row: tuple[str] | None) -> None:
        self._row = row

    def first(self) -> tuple[str] | None:
        return self._row


class _Connection:
    def __init__(self, row: tuple[str] | None) -> None:
        self._row = row
        self.executed = 0

    def execute(self, _statement: object, _params: object) -> _Result:
        self.executed += 1
        return _Result(self._row)


class _Engine:
    """Answers the INSERT with a new job id, or with no row for an ON CONFLICT no-op."""

    def __init__(self, row: tuple[str] | None) -> None:
        self.connection = _Connection(row)

    @contextmanager
    def begin(self) -> Iterator[_Connection]:
        yield self.connection


@pytest.fixture
def module_records() -> Iterator[list[tuple[str, str]]]:
    """Every record this module logs at any level, as ``(level name, message)``."""
    captured: list[tuple[str, str]] = []
    sink_id = _loguru_logger.add(
        lambda message: captured.append((message.record["level"].name, message.record["message"])),
        level=0,
        filter=lambda record: record["extra"].get("component") == _COMPONENT,
    )
    yield captured
    _loguru_logger.remove(sink_id)


@pytest.mark.parametrize(
    ("row", "expected_job"),
    [
        pytest.param(("job-1",), "job-1", id="new_job"),
        pytest.param(None, "<duplicate-noop>", id="duplicate_noop"),
    ],
)
def test_the_enqueue_line_is_debug_and_nothing_from_it_reaches_info(
    module_records: list[tuple[str, str]], row: tuple[str] | None, expected_job: str
) -> None:
    engine = _Engine(row)

    job_id = enqueue_auto_topup(
        engine,  # type: ignore[arg-type]
        owner_id="u1",
        old_balance=450,
        new_balance=430,
        call_id="call1",
        turn_seq=3,
    )

    assert engine.connection.executed == 1
    assert job_id == (row[0] if row is not None else None)
    assert module_records == [
        (
            "DEBUG",
            f"voice auto-top-up trigger enqueued (call=call1 turn=3 job={expected_job})",
        )
    ]
