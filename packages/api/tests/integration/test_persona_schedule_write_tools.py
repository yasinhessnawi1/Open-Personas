"""The persona's calendar write door, through the real services (issue 13).

Real Postgres, real ``create_user_schedule`` and ``delete_schedule_with_intent``, real
composition: the ports come from ``RuntimeFactory`` exactly as a turn would build them, so
what is proved here is the production path and not a rehearsal of it.

What the owner reported: the persona said "my scheduling access is currently read-only. I
can inspect what's on the books, but I have no tool to book a one-off run at a specific
time", and the daily 07:00 Hacker News brief they asked it to drop was still on the calendar
afterwards. Both halves are asserted here against the durable rows.
"""

# ruff: noqa: ARG001 (``migrated_engine`` is a dependency-ordering fixture param).
from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from persona.schedules import RecurrenceKind, RecurrencePattern
from persona.tasks import TaskState
from persona.tools.builtin.schedule_introspection import make_schedule_introspection_tool
from persona.tools.builtin.schedule_write import (
    make_schedule_book_once_tool,
    make_schedule_remove_tool,
)
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import current_user_id
from persona_api.schedules import ScheduleStore
from persona_api.schedules.reader import APIScheduleReader
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.services.schedule_create_service import create_user_schedule
from persona_api.tasks.store import TaskStore
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OWNER = "user_write_tools"
_PERSONA = "persona_write_tools"
_OSLO = "Europe/Oslo"


@pytest.fixture
def engine(migrated_engine: Engine) -> Iterator[Engine]:
    """The ``persona_app`` RLS engine the services run on (seeding rides the superuser)."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    eng = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield eng
    eng.dispose()


@pytest.fixture
def seeded(migrated_engine: Engine) -> None:
    """One user in Oslo with one persona of their own."""
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email, timezone) VALUES (:u, :e, :tz)"),
            {"u": _OWNER, "e": f"{_OWNER}@example.com", "tz": _OSLO},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'name: x')"),
            {"p": _PERSONA, "o": _OWNER},
        )


@pytest.fixture
def owner_bound() -> Iterator[None]:
    """Bind the RLS owner the way a request would, for the whole test."""
    token = current_user_id.set(_OWNER)
    yield
    current_user_id.reset(token)


def factory_for(engine: Engine) -> RuntimeFactory:
    """The real composition root, on the real engine.

    One per test, which also gives each test its own disclosure ledger: the delete gate is
    being exercised here, not shared around behind the tests' backs.
    """
    return RuntimeFactory(
        rls_engine=engine,
        embedder=None,  # type: ignore[arg-type]
        tier_registry=None,  # type: ignore[arg-type]
        turn_log_writer=None,  # type: ignore[arg-type]
        audit_root=Path(tempfile.gettempdir()) / "persona-schedule-write-audit",
        sandbox_pool=None,
        workspace_root=None,
        image_backend=None,
    )


def _book_tool(factory: RuntimeFactory) -> object:
    return make_schedule_book_once_tool(
        port_provider=factory._build_schedule_booking_provider(_PERSONA),  # noqa: SLF001
        persona_id=_PERSONA,
    )


def _remove_tool(factory: RuntimeFactory) -> object:
    return make_schedule_remove_tool(
        port_provider=factory._build_schedule_removal_provider(),  # noqa: SLF001
        disclosure_provider=factory._build_schedule_disclosure_provider(),  # noqa: SLF001
    )


def _introspect_tool(factory: RuntimeFactory) -> object:
    return make_schedule_introspection_tool(
        reader_provider=factory._build_schedule_reader_provider(),  # noqa: SLF001
        persona_id=_PERSONA,
        disclosure_provider=factory._build_schedule_disclosure_provider(),  # noqa: SLF001
    )


def _agenda_ids(engine: Engine, *, days: int = 30) -> set[str]:
    """What the calendar read model would list for this owner right now."""
    now = datetime.now(UTC)
    agenda = APIScheduleReader(engine, APIConfig(), _OWNER).read_agenda(
        start=now, end=now + timedelta(days=days), persona_id=None
    )
    return {occurrence.schedule_id for occurrence in agenda.occurrences}


def _daily_brief(engine: Engine, subject: str = "Hacker News brief") -> str:
    """Seed the routine from the report: a daily 07:00 brief, through the user's own door."""
    result = create_user_schedule(
        engine,
        ScheduleStore(engine),
        TaskStore(engine),
        owner_id=_OWNER,
        pattern=RecurrencePattern(kind=RecurrenceKind.DAILY, hour=7, minute=0),
        one_time_at=None,
        timezone=_OSLO,
        persona_id=_PERSONA,
        subject=subject,
        idempotency_key=f"seed-{subject}",
        now=datetime.now(UTC),
    )
    return result.schedule_id


# ------------------------------------------------------------------------------------------
# Booking a one-off
# ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded", "owner_bound")
async def test_booking_in_one_hour_lands_exactly_one_one_off_run(engine: Engine) -> None:
    """ "Book the Gantt redraw an hour from now": one task, one schedule, fires once."""
    before = datetime.now(UTC)
    result = await _book_tool(factory_for(engine)).execute(  # type: ignore[attr-defined]
        goal="Redraw the Gantt chart", when="in one hour"
    )
    assert result.is_error is False, result.content

    schedule_id = result.data["schedule_id"]
    schedule = ScheduleStore(engine).get(_OWNER, schedule_id)
    assert schedule.recurrence is None, "a one-off must not carry a cadence"
    assert schedule.one_time_at is not None
    delta = schedule.one_time_at - before
    assert timedelta(minutes=59) <= delta <= timedelta(minutes=61), delta
    assert schedule.timezone == _OSLO

    task = TaskStore(engine).get(_OWNER, result.data["task_id"])
    assert task.persona_id == _PERSONA
    assert task.state is TaskState.WAITING
    assert "Redraw the Gantt chart" in task.contract.goal

    assert schedule_id in _agenda_ids(engine), "the booking must show on the calendar"


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded", "owner_bound")
async def test_the_same_booking_twice_stays_one_entry(engine: Engine) -> None:
    """A retried tool call in the same minute is one intention, not two calendar rows."""
    tool = _book_tool(factory_for(engine))
    first = await tool.execute(goal="Redraw the Gantt chart", when="tomorrow 09:00")  # type: ignore[attr-defined]
    second = await tool.execute(goal="Redraw the Gantt chart", when="tomorrow 09:00")  # type: ignore[attr-defined]

    assert first.data["schedule_id"] == second.data["schedule_id"]
    assert first.data["created"] is True
    assert second.data["created"] is False
    assert len(_agenda_ids(engine)) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded", "owner_bound")
async def test_the_create_audit_names_the_chat_door(
    engine: Engine, migrated_engine: Engine
) -> None:
    """The durable record says the user asked for it, in conversation, not through the UI."""
    result = await _book_tool(factory_for(engine)).execute(  # type: ignore[attr-defined]
        goal="Redraw the Gantt chart", when="in one hour"
    )
    with migrated_engine.begin() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT metadata FROM audit_log WHERE target = :t "
                    "AND action = 'schedule.create'"
                ),
                {"t": result.data["schedule_id"]},
            )
            .mappings()
            .all()
        ]

    assert len(rows) == 1
    metadata = rows[0]["metadata"]
    assert metadata["actor"] == "user_via_chat"
    assert metadata["originator"] == "user"


# ------------------------------------------------------------------------------------------
# Removing an entry
# ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded", "owner_bound")
async def test_removing_a_shown_routine_takes_it_off_the_calendar(engine: Engine) -> None:
    """The second half of the report: the 07:00 brief actually goes, and stays gone."""
    factory = factory_for(engine)
    schedule_id = _daily_brief(engine)

    shown = await _introspect_tool(factory).execute(scope="all", days_ahead=7)  # type: ignore[attr-defined]
    assert schedule_id in shown.content, "the read has to name the entry it is disclosing"

    removed = await _remove_tool(factory).execute(schedule_id=schedule_id)  # type: ignore[attr-defined]

    assert removed.is_error is False, removed.content
    assert "Hacker News brief" in removed.content
    assert schedule_id not in _agenda_ids(engine), "the calendar still lists a removed entry"


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded", "owner_bound")
async def test_removing_a_routine_pauses_its_standing_task(engine: Engine) -> None:
    """Deleting the schedule stops the recurrence, so the task must not sit looking alive."""
    factory = factory_for(engine)
    schedule_id = _daily_brief(engine)
    task_id = TaskStore(engine).get_by_schedule_id(_OWNER, schedule_id).id  # type: ignore[union-attr]

    await _introspect_tool(factory).execute(scope="all", days_ahead=7)  # type: ignore[attr-defined]
    removed = await _remove_tool(factory).execute(schedule_id=schedule_id)  # type: ignore[attr-defined]

    assert removed.data["paused_task_id"] == task_id
    # Pause is an overlay on the task, not a state: it stays WAITING but cannot run.
    assert TaskStore(engine).get(_OWNER, task_id).paused is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded", "owner_bound")
async def test_removing_an_unshown_id_asks_first_and_deletes_nothing(engine: Engine) -> None:
    """The gate, against a REAL entry: without a calendar read, nothing is removed."""
    factory = factory_for(engine)
    schedule_id = _daily_brief(engine)

    result = await _remove_tool(factory).execute(schedule_id=schedule_id)  # type: ignore[attr-defined]

    assert result.data["removed"] is False
    assert result.data["reason"] == "not_shown"
    assert schedule_id in _agenda_ids(engine), "an unshown id must still be on the calendar"
    assert ScheduleStore(engine).get(_OWNER, schedule_id) is not None


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded", "owner_bound")
async def test_the_delete_tombstone_records_who_asked(
    engine: Engine, migrated_engine: Engine
) -> None:
    """R9-037's durable "the user deleted this" now also says how they said it."""
    factory = factory_for(engine)
    schedule_id = _daily_brief(engine)
    await _introspect_tool(factory).execute(scope="all", days_ahead=7)  # type: ignore[attr-defined]
    await _remove_tool(factory).execute(schedule_id=schedule_id)  # type: ignore[attr-defined]

    with migrated_engine.begin() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                text("SELECT reason FROM schedule_tombstones WHERE schedule_id = :s"),
                {"s": schedule_id},
            )
            .mappings()
            .all()
        ]

    assert len(rows) == 1
    assert rows[0]["reason"]["requested_by"] == "user_via_chat"
