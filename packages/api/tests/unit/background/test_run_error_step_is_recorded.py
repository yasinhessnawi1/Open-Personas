"""A failed run keeps its failing step, and still reads safely (R9-180 + R9-097).

The loop used to let an unrecoverable failure leave as an exception, so the durable
record kept a run-level message and the event-log snapshot, and nothing on the record
said which step broke. It now ends the run the way every other terminal condition ends
it: ``RunStatus.ERROR``, the failure on ``Run.error``, and a closing ``StepType.ERROR``
step carrying the same text.

That moved the failure through two new doors on its way to a reader, and both had to keep
the R9-097 promise: a capacity exhaustion names our providers, model ids and routing
strategy, and none of that may reach the run page. The stored half is the row and its
closing step. The LIVE half is the ``error`` frame, which nothing used to stream at all
(the run raised instead) and which reaches both the person watching and the event-log
snapshot a reopen reads until the terminal write replaces it. All of it is pinned here.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.backends import AllModelsFailedError
from persona_api.background.run_worker import RunRegistry
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import personas as personas_t
from persona_api.db.models import runs as runs_t
from persona_api.services.user_facing_errors import CAPACITY_BUSY_FREE_MESSAGE
from persona_runtime.agentic.events import RunEvent
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from sqlalchemy import insert, select

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_OWNER = "user_broken"
_PERSONA = "astrid"
_RUN = "run_with_error_step"

#: What a tier exhaustion actually stringifies to, kept verbatim from production.
_RAW = (
    "every backend in MultiModelChatBackend exhausted [tier=frontier attempt_count=2 "
    + json.dumps([{"provider": "openrouter", "model": "openai/gpt-oss-20b:free"}])
    + "]"
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "runs.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="b@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: Astrid"))
        conn.execute(
            insert(runs_t).values(
                id=_RUN, owner_id=_OWNER, persona_id=_PERSONA, task="t", status="running"
            )
        )
    yield eng
    eng.dispose()


class _FailingLoop:
    """A loop that ends the way the real one now does: an ERROR run, not a raise.

    It also streams the ``error`` frame the real loop streams, and snapshots the run row
    at that moment, because the window between the frame and the terminal write is
    exactly when a watching reader, and anyone who reopens the run, sees the failure.
    """

    def __init__(self, engine: Engine, *, error: str, error_class: str) -> None:
        self._engine = engine
        self._error = error
        self._error_class = error_class
        #: ``runs.steps`` as it stood the instant the failure was streamed.
        self.snapshot_at_failure: list[dict[str, object]] = []

    async def run(self, task: str, **kw: object) -> Run:
        on_event = kw.get("on_event")
        if callable(on_event):
            await on_event(RunEvent.error(1, self._error, error_class=self._error_class))
            self.snapshot_at_failure = _steps(self._engine)
        return Run(
            persona_id=_PERSONA,
            task=task,
            status=RunStatus.ERROR,
            steps=[
                Step(type=StepType.TOOL_CALL),
                Step(type=StepType.ERROR, content=self._error),
            ],
            error=self._error,
            error_class=self._error_class,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )


def _row(engine: Engine) -> tuple[str, str, list[dict[str, object]]]:
    with engine.begin() as conn:
        row = conn.execute(
            select(runs_t.c.status, runs_t.c.error, runs_t.c.steps).where(runs_t.c.id == _RUN)
        ).one()
    steps = row.steps if isinstance(row.steps, list) else json.loads(row.steps)
    return str(row.status), str(row.error), steps


def _steps(engine: Engine) -> list[dict[str, object]]:
    return _row(engine)[2]


async def _drive(engine: Engine, loop: _FailingLoop) -> list[RunEvent]:
    """Run the worker to completion and return every frame the reader was sent."""
    registry = RunRegistry(engine)
    handle = registry.start(
        run_id=_RUN,
        owner_id=_OWNER,
        loop=loop,  # type: ignore[arg-type]
        task_text="t",
    )
    assert handle.task is not None
    await asyncio.wait_for(handle.task, timeout=5)
    streamed: list[RunEvent] = []
    while not handle.events.empty():
        event = handle.events.get_nowait()
        if event is not None:
            streamed.append(event)
    return streamed


def _streamed_error(events: list[RunEvent]) -> str:
    frames = [e for e in events if e.type == "error"]
    assert len(frames) == 1
    return str(frames[0].data["message"])


@pytest.mark.asyncio
async def test_the_failing_step_survives_onto_the_record(engine: Engine) -> None:
    """A reopened run has to be able to point at the step the work stopped on."""
    await _drive(
        engine, _FailingLoop(engine, error="the sandbox is gone", error_class="RuntimeError")
    )

    status, error, steps = _row(engine)
    assert status == "error"
    assert error == "the sandbox is gone"
    assert [s["type"] for s in steps] == ["tool_call", "error"]
    assert steps[-1]["content"] == "the sandbox is gone"


@pytest.mark.asyncio
async def test_a_capacity_exhaustion_still_never_reaches_the_reader(engine: Engine) -> None:
    """R9-097 through the new door: same promise, whichever way the failure arrives."""
    await _drive(
        engine,
        _FailingLoop(engine, error=_RAW, error_class=AllModelsFailedError.__name__),
    )

    _status, error, steps = _row(engine)
    # No subscription row for this owner IS the free plan (the D-M4-9 default).
    assert error == CAPACITY_BUSY_FREE_MESSAGE
    # The step and the row have to tell one story, not two.
    assert steps[-1]["content"] == CAPACITY_BUSY_FREE_MESSAGE
    for leaked in ("openrouter", "gpt-oss-20b", "MultiModelChatBackend"):
        assert leaked not in error
        assert leaked not in str(steps[-1]["content"])


@pytest.mark.asyncio
async def test_the_live_frame_carries_the_sentence_not_the_exhaustion(engine: Engine) -> None:
    """The watching reader is a reader too.

    The terminal write is not the first time a person sees the failure: the ``error``
    frame goes out the moment it happens, and it is what the run header renders.
    """
    loop = _FailingLoop(engine, error=_RAW, error_class=AllModelsFailedError.__name__)

    streamed = await _drive(engine, loop)

    assert _streamed_error(streamed) == CAPACITY_BUSY_FREE_MESSAGE


@pytest.mark.asyncio
async def test_the_mid_run_snapshot_carries_the_sentence_too(engine: Engine) -> None:
    """And so is anyone who reopens the run before the terminal write lands.

    Until ``persist_final`` replaces it, ``runs.steps`` holds the event log, so an
    unrewritten frame would be the failure a reopened run shows.
    """
    loop = _FailingLoop(engine, error=_RAW, error_class=AllModelsFailedError.__name__)

    await _drive(engine, loop)

    frames = [e for e in loop.snapshot_at_failure if e.get("type") == "error"]
    assert len(frames) == 1
    data = frames[0]["data"]
    assert isinstance(data, dict)
    assert data["message"] == CAPACITY_BUSY_FREE_MESSAGE
    for leaked in ("openrouter", "gpt-oss-20b", "MultiModelChatBackend"):
        assert leaked not in json.dumps(loop.snapshot_at_failure)


@pytest.mark.asyncio
async def test_an_unmapped_failure_streams_its_own_words(engine: Engine) -> None:
    """Scope guard, same as the stored half: only known leaks are rewritten.

    An unmapped failure often says something genuinely useful and already safe, and
    replacing it with a vague sentence is the same defect pointing the other way.
    """
    loop = _FailingLoop(engine, error="the workspace path does not exist", error_class="OSError")

    streamed = await _drive(engine, loop)

    assert _streamed_error(streamed) == "the workspace path does not exist"
