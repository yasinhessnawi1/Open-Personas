"""R9-313: the api's shutdown fits inside the container's stop grace, whatever hangs.

``cancel_and_wait`` is what the chat turn and run registries close with; ``ShutdownDeadline``
is the one budget the lifespan teardown spends. Both give up on what will not end, log it,
and never wait for a cancel that cannot land.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import pytest
from persona_api.background.shutdown import ShutdownDeadline, cancel_and_wait

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    from loguru import logger

    captured: list[str] = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    yield captured
    logger.remove(sink_id)


async def _ignores_cancel(release: asyncio.Event) -> None:
    """A task whose cancel cannot land: it swallows each one until ``release`` is set."""
    while not release.is_set():
        with contextlib.suppress(asyncio.CancelledError):
            await release.wait()


async def _finish(release: asyncio.Event, *tasks: asyncio.Future[object]) -> None:
    release.set()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


@pytest.mark.asyncio
async def test_cancel_and_wait_returns_at_once_when_every_task_ends() -> None:
    task = asyncio.create_task(asyncio.sleep(60))
    await asyncio.sleep(0)

    abandoned = await asyncio.wait_for(
        cancel_and_wait({task: "run=r1"}, timeout=30, what="run"), timeout=5
    )

    assert abandoned == []
    assert task.cancelled()


@pytest.mark.asyncio
async def test_cancel_and_wait_gives_up_at_the_bound_and_logs_each(warnings: list[str]) -> None:
    release = asyncio.Event()
    stuck = asyncio.create_task(_ignores_cancel(release))
    await asyncio.sleep(0)
    try:
        abandoned = await asyncio.wait_for(
            cancel_and_wait({stuck: "run=r1 owner=u1"}, timeout=0.05, what="run"), timeout=5
        )
    finally:
        await _finish(release, stuck)

    assert abandoned == ["run=r1 owner=u1"]
    line = next(w for w in warnings if "abandoned" in w)
    assert "run=r1 owner=u1" in line


@pytest.mark.asyncio
async def test_cancel_and_wait_logs_a_task_that_ended_with_an_error(warnings: list[str]) -> None:
    async def _fails_on_cancel() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise RuntimeError("the undo failed") from None

    task = asyncio.create_task(_fails_on_cancel())
    await asyncio.sleep(0)

    assert await cancel_and_wait({task: "run=r2"}, timeout=5, what="run") == []
    assert any("run=r2" in w and "the undo failed" in w for w in warnings)


@pytest.mark.asyncio
async def test_cancel_and_wait_with_nothing_to_cancel_is_a_no_op() -> None:
    assert await cancel_and_wait({}, timeout=5, what="run") == []


@pytest.mark.asyncio
async def test_the_deadline_runs_steps_together_and_abandons_the_late(
    warnings: list[str],
) -> None:
    release = asyncio.Event()
    finished: list[str] = []

    async def _quick() -> None:
        await asyncio.sleep(0.01)
        finished.append("quick")

    deadline = ShutdownDeadline(0.3)
    loop = asyncio.get_running_loop()
    began = loop.time()
    try:
        abandoned = await asyncio.wait_for(
            deadline.run({"quick": _quick(), "stuck": _ignores_cancel(release)}), timeout=5
        )
    finally:
        release.set()
    elapsed = loop.time() - began

    assert abandoned == ["stuck"]
    assert finished == ["quick"]
    assert elapsed < 2
    assert any("stuck" in w and "abandoned" in w for w in warnings)


@pytest.mark.asyncio
async def test_the_deadline_is_one_budget_across_every_step() -> None:
    """A step run after the budget is spent gets no time at all."""
    clock = iter([0.0, 100.0, 100.0, 100.0])
    deadline = ShutdownDeadline(20, clock=lambda: next(clock))
    release = asyncio.Event()
    try:
        abandoned = await asyncio.wait_for(
            deadline.run({"late": _ignores_cancel(release)}), timeout=5
        )
    finally:
        release.set()

    assert abandoned == ["late"]
    assert deadline.remaining() == 0


@pytest.mark.asyncio
async def test_the_deadline_logs_a_step_that_failed_and_goes_on(warnings: list[str]) -> None:
    async def _boom() -> None:
        raise RuntimeError("telemetry flush failed")

    assert await ShutdownDeadline(5).run({"telemetry flush": _boom()}) == []
    assert any("telemetry flush" in w and "telemetry flush failed" in w for w in warnings)


@pytest.mark.asyncio
async def test_the_deadline_cancels_background_tasks_within_its_budget() -> None:
    task = asyncio.create_task(asyncio.sleep(60))
    await asyncio.sleep(0)

    await asyncio.wait_for(
        ShutdownDeadline(5).cancel({"catalog refresh": task, "absent": None}), timeout=5
    )

    assert task.cancelled()


def test_a_deadline_needs_a_positive_budget() -> None:
    with pytest.raises(ValueError, match="budget"):
        ShutdownDeadline(0)
