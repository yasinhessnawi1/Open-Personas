"""Unit tests for worker loop knobs (no DB) (Spec A0, T5) + signal ownership (R9-004)."""

# ruff: noqa: SLF001 — exercising private loop internals directly.
from __future__ import annotations

import asyncio
import signal
from unittest.mock import MagicMock

from persona.jobs import JobRegistry
from persona_api.background.worker_root import InProcessWorker
from persona_api.jobs import Worker
from sqlalchemy.exc import OperationalError


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


# --- R9-043: claim() DB failures must not crash the worker loop --------------
# Unlike the scheduler-tick/catalog-sync calls in the same loop (already
# `except Exception … must not crash the worker loop`), `claim()` at the top
# of `Worker.run`'s loop body was UNGUARDED — a transient DB drop (e.g. mid
# Ctrl-C drain) propagated an OperationalError straight out of `run()`, through
# `InProcessWorker.aclose`'s `await self._task`, into the lifespan shutdown
# ("Application shutdown failed. Exiting."). These pin BOTH halves of the fix:
# a transient failure during normal operation must retry (never crash), and a
# failure WHILE draining must end the drain cleanly (never retry-loop against
# a DB that just fell out from under the shutdown) — R9-004's clean-shutdown
# behavior is unaffected either way (in-flight jobs still drain normally).


def _mock_queue() -> MagicMock:
    queue = MagicMock()
    queue.reclaim_expired.return_value = 0
    queue.archive_terminal.return_value = 0
    queue.purge_archive.return_value = 0
    return queue


def test_claim_error_during_normal_run_logs_and_retries() -> None:
    worker = _worker(poll_interval_seconds=0.01, poll_jitter_seconds=0.0)
    queue = _mock_queue()
    calls = {"n": 0}

    def _claim_transient(**_kw: object) -> list[object]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("claim", {}, Exception("conn dropped"))
        # Proven the retry happened — stop the loop cleanly (not the thing
        # under test, just a deterministic way to end the scenario).
        worker.request_drain()
        return []

    queue.claim.side_effect = _claim_transient
    worker._queue = queue

    asyncio.run(worker.run(install_signal_handlers=False))  # must not raise

    assert calls["n"] >= 2, "a transient claim() failure must be retried, not crash the loop"
    assert worker._draining.is_set()


def test_claim_error_during_drain_ends_drain_cleanly() -> None:
    worker = _worker(poll_interval_seconds=0.01, poll_jitter_seconds=0.0)
    queue = _mock_queue()

    def _claim_then_die(**_kw: object) -> list[object]:
        # The exact R9-043 race: the drain signal (Ctrl-C) lands WHILE this
        # claim() call is in flight against a DB that just dropped.
        worker.request_drain()
        raise OperationalError("claim", {}, Exception("conn dropped"))

    queue.claim.side_effect = _claim_then_die
    worker._queue = queue

    asyncio.run(worker.run(install_signal_handlers=False))  # must not raise or hang

    assert worker._draining.is_set()
    assert queue.claim.call_count == 1, (
        "a claim() failure discovered mid-drain must end the drain on the spot, "
        "not retry against the dead DB"
    )


def test_drain_still_awaits_in_flight_jobs_after_a_claim_error() -> None:
    """R9-004 non-regression: a claim() failure that ends the drain must still
    let already-claimed in-flight work finish, not abandon it."""
    worker = _worker(poll_interval_seconds=0.01, poll_jitter_seconds=0.0, drain_seconds=5.0)
    queue = _mock_queue()

    def _claim_then_die(**_kw: object) -> list[object]:
        # Same race as test_claim_error_during_drain_ends_drain_cleanly: the
        # drain signal lands while this claim() call is in flight.
        worker.request_drain()
        raise OperationalError("claim", {}, Exception("conn dropped"))

    queue.claim.side_effect = _claim_then_die
    worker._queue = queue

    finished = asyncio.Event()

    async def _slow_job() -> None:
        await asyncio.sleep(0.05)
        finished.set()

    async def scenario() -> None:
        # Seed an in-flight task directly (the executor internals aren't the
        # concern here — only that _drain() still awaits whatever is tracked),
        # BEFORE the drain is requested — the claim() side effect above is what
        # requests it, on the loop's first iteration.
        task: asyncio.Task[object] = asyncio.create_task(_slow_job())
        worker._in_flight.add(task)
        task.add_done_callback(worker._settle_job_task)
        await worker.run(install_signal_handlers=False)

    asyncio.run(scenario())
    assert finished.is_set(), "in-flight work must still complete during a claim-error drain"


# --- R9-049: maintenance-sweep DB failures must not crash the worker loop ----
# Mirrors R9-043: unlike the scheduler-tick/catalog-sync calls in the same
# loop (already `except Exception … must not crash the worker loop`),
# `_maybe_run_maintenance` -> `run_maintenance` was UNGUARDED against DB
# errors — a DB drop during the sweep propagated straight out of `run()`, the
# same crash shape R9-043 fixed for `claim()`.


def test_maintenance_error_does_not_crash_the_loop() -> None:
    worker = _worker(poll_interval_seconds=0.01, poll_jitter_seconds=0.0)
    queue = _mock_queue()
    queue.reclaim_expired.side_effect = OperationalError("reclaim", {}, Exception("conn dropped"))

    def _claim_then_drain(**_kw: object) -> list[object]:
        worker.request_drain()
        return []

    queue.claim.side_effect = _claim_then_drain
    worker._queue = queue

    asyncio.run(worker.run(install_signal_handlers=False))  # must not raise

    assert worker._draining.is_set()
    assert queue.reclaim_expired.called, "sanity: maintenance actually ran and hit the DB error"


def test_maintenance_error_still_updates_cadence_clock() -> None:
    """A failed sweep must not spin hot retrying every loop iteration — the
    cadence clock advances even on failure, same as the other guarded
    periodic tasks (scheduler tick, catalog sync, ...)."""
    worker = _worker()
    queue = _mock_queue()
    queue.reclaim_expired.side_effect = OperationalError("reclaim", {}, Exception("conn dropped"))
    worker._queue = queue

    worker._maybe_run_maintenance()  # must not raise

    assert worker._last_maintenance > 0.0
