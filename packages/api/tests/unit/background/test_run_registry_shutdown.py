"""R9-313 round 2: closing the run registry is bounded, like the chat turns.

A run whose cancel cannot land (its undo waits out a database write) used to hold up the
api's shutdown for as long as the write hung. Now every run is cancelled and they are waited
for together, at most ``shutdown_timeout_s``; each one still running is logged and left for
the restart sweep.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from persona_api.background.run_worker import RunHandle, RunRegistry


async def _ignores_cancel(release: asyncio.Event) -> None:
    while not release.is_set():
        with contextlib.suppress(asyncio.CancelledError):
            await release.wait()


def _register(registry: RunRegistry, run_id: str, task: asyncio.Task[None]) -> RunHandle:
    handle = RunHandle(run_id, "user_alice")
    handle.task = task
    registry._handles[run_id] = handle  # noqa: SLF001 - a run started without a real loop
    return handle


@pytest.mark.asyncio
async def test_close_gives_up_on_a_run_that_will_not_end_and_logs_it() -> None:
    from loguru import logger

    registry = RunRegistry(object(), shutdown_timeout_s=0.05)  # type: ignore[arg-type]
    release = asyncio.Event()
    handle = _register(registry, "run_stuck", asyncio.create_task(_ignores_cancel(release)))
    await asyncio.sleep(0)
    captured: list[str] = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        await asyncio.wait_for(registry.aclose(), timeout=5)
    finally:
        logger.remove(sink_id)
        release.set()
        assert handle.task is not None
        with contextlib.suppress(asyncio.CancelledError):
            await handle.task

    assert handle.cancel_token.is_cancelled, "the loop is told to stop at its next step"
    line = next(w for w in captured if "abandoned" in w)
    assert "run=run_stuck" in line
    assert "owner=user_alice" in line


@pytest.mark.asyncio
async def test_close_returns_at_once_when_every_run_ends() -> None:
    registry = RunRegistry(object(), shutdown_timeout_s=30)  # type: ignore[arg-type]
    handle = _register(registry, "run_ok", asyncio.create_task(asyncio.sleep(60)))
    await asyncio.sleep(0)

    await asyncio.wait_for(registry.aclose(), timeout=5)

    assert handle.task is not None
    assert handle.task.cancelled()


@pytest.mark.parametrize("bound", [0, -1])
def test_a_shutdown_bound_must_be_positive(bound: float) -> None:
    with pytest.raises(ValueError, match="shutdown_timeout_s"):
        RunRegistry(object(), shutdown_timeout_s=bound)  # type: ignore[arg-type]
