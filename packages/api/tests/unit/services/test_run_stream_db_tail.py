"""The run event stream's DB tail (Spec W1, T2; D-W1-1).

A run the worker executes has no in-process registry handle, but its leg snapshots every
event into ``runs.steps`` through the one runs writer. ``stream_run_events`` therefore tails
the row when the registry has nothing. These drive the REAL generator against a real
database (the community SQLite engine) and the REAL ``run_record`` writers, and pin the
four conditions D-W1-1 attached to the fallback:

- the SSE frame shape is byte-identical to the live path's for the same event, so the
  web client needs no edit;
- events that land while the tail is open are emitted, each exactly once, in order;
- the tail ends with the ``end`` frame the moment the status leaves the live set;
- the poll stops when the client goes away: closing the generator stops every further
  read, and no task is left behind.

The registry path is untouched: with a handle present the queue is drained as before.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import personas as personas_t
from persona_api.errors import RunNotFoundError
from persona_api.services import run_record, run_service
from persona_runtime.agentic.events import RunEvent
from persona_runtime.agentic.run import Run, RunStatus
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
_OWNER = "user_tail"
_PERSONA = "persona_tail"
_RUN = "run_tail_1"
_POLL = 0.02


class _CountingEngine:
    """Delegates to a real engine and counts every transaction the tail opens."""

    def __init__(self, inner: Engine) -> None:
        self._inner = inner
        self.begins = 0

    def begin(self) -> object:
        self.begins += 1
        return self._inner.begin()

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _EmptyRegistry:
    def get(self, _run_id: str) -> None:
        return None


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "tail.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="t@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    run_record.insert_run(
        eng,
        run_id=_RUN,
        owner_id=_OWNER,
        persona_id=_PERSONA,
        task="tail me",
        started_at=_NOW,
    )
    yield eng
    eng.dispose()


def _frames(raw: list[bytes]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for chunk in raw:
        head, _, rest = chunk.decode().partition("\n")
        data = rest.removeprefix("data: ").rstrip("\n")
        out.append((head.removeprefix("event: "), data))
    return out


def _tail(engine: object) -> AsyncIterator[bytes]:
    return run_service.stream_run_events(
        registry=_EmptyRegistry(),  # type: ignore[arg-type]
        run_id=_RUN,
        rls_engine=engine,  # type: ignore[arg-type]  # a counting proxy in one test
        poll_interval_seconds=_POLL,
    )


async def _collect(gen: AsyncIterator[bytes], *, n: int) -> list[bytes]:
    got: list[bytes] = []
    async for chunk in gen:
        got.append(chunk)
        if len(got) == n:
            break
    return got


# --- shape parity with the live path ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_tailed_frame_is_byte_identical_to_the_live_frame(engine: Engine) -> None:
    started = RunEvent.started("tail me")
    thinking = RunEvent.thinking(0)
    log = [started.model_dump(mode="json"), thinking.model_dump(mode="json")]
    run_record.persist_progress(engine, run_id=_RUN, event_log=log)

    got = await _collect(_tail(engine), n=2)
    live = [
        f"event: {e.type}\ndata: {e.model_dump_json()}\n\n".encode() for e in (started, thinking)
    ]
    assert got == live


# --- events that land while the tail is open ----------------------------------------------


@pytest.mark.asyncio
async def test_events_that_land_later_are_emitted_once_in_order_then_end(
    engine: Engine,
) -> None:
    log = [RunEvent.started("tail me").model_dump(mode="json")]
    run_record.persist_progress(engine, run_id=_RUN, event_log=log)

    async def _leg_progresses() -> None:
        await asyncio.sleep(_POLL * 3)
        log.append(RunEvent.thinking(0).model_dump(mode="json"))
        run_record.persist_progress(engine, run_id=_RUN, event_log=list(log))
        await asyncio.sleep(_POLL * 3)
        log.append(RunEvent.thinking(1).model_dump(mode="json"))
        run_record.persist_progress(engine, run_id=_RUN, event_log=list(log))
        await asyncio.sleep(_POLL * 3)
        run_record.persist_final(
            engine,
            run_id=_RUN,
            run=Run(
                persona_id=_PERSONA,
                task="tail me",
                status=RunStatus.COMPLETED,
                steps=[],
                output="done",
                started_at=_NOW,
                finished_at=_NOW,
            ),
        )

    producer = asyncio.create_task(_leg_progresses())
    got = _frames([chunk async for chunk in _tail(engine)])
    await producer
    assert [name for name, _ in got] == ["started", "thinking", "thinking", "end"]
    assert [json.loads(d)["step"] for _, d in got[1:3]] == [0, 1]
    assert got[-1] == ("end", "{}")


@pytest.mark.asyncio
async def test_a_finished_run_yields_only_the_end_frame(engine: Engine) -> None:
    """After ``persist_final`` the row holds Steps, not events: the client reconciles
    from ``GET /runs/{id}`` (D-08-5), exactly as it does when a live stream ends."""
    run_record.persist_final(
        engine,
        run_id=_RUN,
        run=Run(
            persona_id=_PERSONA,
            task="tail me",
            status=RunStatus.COMPLETED,
            steps=[],
            output="done",
            started_at=_NOW,
            finished_at=_NOW,
        ),
    )
    assert _frames([c async for c in _tail(engine)]) == [("end", "{}")]


# --- the client goes away ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_disconnect_stops_the_poll_and_leaves_no_task(engine: Engine) -> None:
    """Starlette drives the generator from the response task and cancels that task when the
    client goes away. Reproduce exactly that: a consumer task iterates the live tail, is
    cancelled mid-poll, and afterwards the row is never read again and no task remains."""
    counting = _CountingEngine(engine)
    run_record.persist_progress(
        engine, run_id=_RUN, event_log=[RunEvent.started("tail me").model_dump(mode="json")]
    )
    before = {t for t in asyncio.all_tasks() if not t.done()}
    received: list[bytes] = []

    async def _consume() -> None:
        async for chunk in _tail(counting):
            received.append(chunk)

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(_POLL * 5)  # the run stays live, so the tail keeps polling
    assert received
    assert received[0].startswith(b"event: started")
    assert counting.begins >= 2
    consumer.cancel()  # the disconnect
    with pytest.raises(asyncio.CancelledError):
        await consumer
    settled = counting.begins
    await asyncio.sleep(_POLL * 6)
    assert counting.begins == settled, "the poll kept reading after the client left"
    leaked = {t for t in asyncio.all_tasks() if not t.done()} - before
    assert leaked == set(), f"tasks left behind: {leaked}"


# --- the pre-W1 contracts still hold ------------------------------------------------------


@pytest.mark.asyncio
async def test_no_handle_and_no_engine_is_still_not_active(engine: Engine) -> None:  # noqa: ARG001
    gen = run_service.stream_run_events(registry=_EmptyRegistry(), run_id=_RUN)  # type: ignore[arg-type]
    with pytest.raises(RunNotFoundError):
        await gen.__anext__()


@pytest.mark.asyncio
async def test_a_registry_handle_is_drained_as_before(engine: Engine) -> None:
    class _Handle:
        def __init__(self) -> None:
            self.events: asyncio.Queue[RunEvent | None] = asyncio.Queue()

    class _Registry:
        def __init__(self, handle: _Handle) -> None:
            self._h = handle

        def get(self, _run_id: str) -> _Handle:
            return self._h

    handle = _Handle()
    ev = RunEvent.started("live")
    handle.events.put_nowait(ev)
    handle.events.put_nowait(None)
    got = [
        c
        async for c in run_service.stream_run_events(
            registry=_Registry(handle),  # type: ignore[arg-type]
            run_id=_RUN,
            rls_engine=engine,
        )
    ]
    assert got == [
        f"event: started\ndata: {ev.model_dump_json()}\n\n".encode(),
        b"event: end\ndata: {}\n\n",
    ]
