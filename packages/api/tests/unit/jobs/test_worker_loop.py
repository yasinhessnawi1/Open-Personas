"""Unit tests for worker loop knobs (no DB) (Spec A0, T5) + signal ownership (R9-004)."""

# ruff: noqa: SLF001 — exercising private loop internals directly.
from __future__ import annotations

import asyncio
import signal
from unittest.mock import MagicMock

from persona.jobs import JobRegistry
from persona_api.background.worker_root import InProcessWorker
from persona_api.jobs import Worker


def _worker(**kw: object) -> Worker:
    return Worker(
        dispatch_engine=MagicMock(),
        rls_engine=MagicMock(),
        registry=JobRegistry(),
        worker_id="w-test",
        **kw,  # type: ignore[arg-type]
    )


def test_next_poll_delay_within_jitter_band() -> None:
    worker = _worker(poll_interval_seconds=1.0, poll_jitter_seconds=0.5)
    delays = [worker._next_poll_delay() for _ in range(200)]
    assert all(1.0 <= d <= 1.5 for d in delays), (
        "poll delay must stay in [interval, interval+jitter]"
    )
    assert len(set(delays)) > 1, "the poll delay must be jittered, not constant"


def test_zero_jitter_gives_constant_interval() -> None:
    worker = _worker(poll_interval_seconds=0.5, poll_jitter_seconds=0.0)
    assert worker._next_poll_delay() == 0.5


def test_request_drain_is_idempotent() -> None:
    worker = _worker()
    assert not worker._draining.is_set()
    worker.request_drain()
    worker.request_drain()  # second call absorbed
    assert worker._draining.is_set()


def test_worker_id_is_unique_per_process() -> None:
    from persona_api.jobs import make_worker_id

    assert make_worker_id() != make_worker_id(), "worker ids must be unique (PID reuse safety)"


# --- R9-004: signal ownership -------------------------------------------------
# asyncio's loop.add_signal_handler REPLACES the process-level handler uvicorn
# installed via signal.signal — so a worker hosted IN uvicorn's process must
# never register signal handlers (^C would drain the worker but never shut the
# server down). These tests pin the seam: opt-out never touches signal
# registration; the standalone default still traps both drain signals.


def _run_with_recorded_signal_installs(
    coro_factory: object, worker: Worker
) -> list[tuple[int, object]]:
    """Drive an async scenario with the running loop's add_signal_handler recorded."""
    calls: list[tuple[int, object]] = []

    async def drive() -> None:
        loop = asyncio.get_running_loop()
        # Shadow the bound method on this throwaway loop instance: any signal
        # registration lands in `calls` instead of the real signal machinery.
        loop.add_signal_handler = (  # type: ignore[method-assign]
            lambda sig, cb, *_args: calls.append((sig, cb))
        )
        await coro_factory(worker)  # type: ignore[operator]

    asyncio.run(drive())
    return calls


def test_run_in_process_mode_never_touches_signal_registration() -> None:
    worker = _worker()

    async def scenario(w: Worker) -> None:
        w.request_drain()  # exit the loop immediately (nothing in flight)
        await w.run(install_signal_handlers=False)

    calls = _run_with_recorded_signal_installs(scenario, worker)
    assert calls == [], "in-process hosting must never register process signal handlers"


def test_run_default_standalone_mode_installs_both_drain_signals() -> None:
    worker = _worker()

    async def scenario(w: Worker) -> None:
        w.request_drain()
        await w.run()  # default: the standalone worker process owns its signals

    calls = _run_with_recorded_signal_installs(scenario, worker)
    assert {sig for sig, _ in calls} == {signal.SIGTERM, signal.SIGINT}
    assert all(cb == worker.request_drain for _, cb in calls), (
        "the installed handler must be the graceful drain"
    )


def test_in_process_worker_hosting_installs_no_signals_and_still_drains() -> None:
    worker = _worker(poll_interval_seconds=0.01, poll_jitter_seconds=0.0)
    # Stub the queue so the loop genuinely spins (claims nothing) with no DB.
    queue = MagicMock()
    queue.claim.return_value = []
    queue.reclaim_expired.return_value = 0
    queue.archive_terminal.return_value = 0
    queue.purge_archive.return_value = 0
    worker._queue = queue

    async def scenario(w: Worker) -> None:
        host = InProcessWorker(w)
        host.start()
        await asyncio.sleep(0.05)  # the loop is live and polling
        assert not w._draining.is_set(), "sanity: the loop is running, not draining"
        await host.aclose()  # the lifespan drain path (request_drain + await)

    calls = _run_with_recorded_signal_installs(scenario, worker)
    assert calls == [], "InProcessWorker must leave signal ownership to uvicorn (R9-004)"
    assert worker._draining.is_set(), "aclose must still request + await the drain"
    assert queue.claim.called, "sanity: the loop actually polled before the drain"
