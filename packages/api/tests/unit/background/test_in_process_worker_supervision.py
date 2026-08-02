"""The in-process worker's loop must not die silently (R9-093).

`InProcessWorker.start()` launched `Worker.run()` with a bare
`asyncio.create_task` and never looked at it again: no done-callback, no
supervision, nothing awaiting it until shutdown. If `run()` raised, the task
died holding its exception, Python surfaced nothing until GC (if ever), the
health check stayed green, and every background surface stopped at once --
schedules, synthesis, consolidation. That is exactly the production symptom:
a 65-minute gap in which no job of any type was created or processed, spanning
a scheduled fire that never happened, with `/livez` returning 200 throughout.

The same failure class was already fixed for the connector runners in R9-073c
("restart a crashed platform runner with backoff instead of leaving it dead").
These tests hold the api worker to the same contract.
"""

from __future__ import annotations

import asyncio

import pytest
from persona_api.background import worker_root
from persona_api.background.worker_root import InProcessWorker


class _FakeWorker:
    """A Worker stand-in whose `run()` outcome is scripted per call."""

    def __init__(self, outcomes: list[str]) -> None:
        self._outcomes = list(outcomes)
        self.run_calls = 0
        self.drain_requested = False
        self.closed = False
        self.worker_id = "fake-worker"
        # The real Worker loops on `while not self._draining.is_set()` and
        # returns when drain is requested; the fake must honour that or
        # `aclose()` (which awaits the task) would hang forever.
        self._draining = asyncio.Event()

    async def run(self, *, install_signal_handlers: bool = True) -> None:  # noqa: ARG002 — signature parity with the real Worker.run
        self.run_calls += 1
        outcome = self._outcomes.pop(0) if self._outcomes else "block"
        if outcome == "raise":
            raise RuntimeError("loop blew up")
        if outcome == "return":
            return
        # "block" -- the healthy shape: run until drained, like the real loop.
        await self._draining.wait()

    def request_drain(self) -> None:
        self.drain_requested = True
        self._draining.set()

    def aclose(self) -> None:
        self.closed = True


async def _settle(fake: _FakeWorker, *, want: int) -> None:
    """Give the supervisor a bounded window to reach `want` run() calls."""
    for _ in range(200):
        if fake.run_calls >= want:
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_crashed_loop_is_restarted_not_left_dead() -> None:
    """THE regression: `run()` raising must not end the worker permanently.

    Before R9-093 the task simply died and every background surface stopped
    with no error, no log and no health-check change.
    """
    fake = _FakeWorker(["raise", "block"])
    runner = InProcessWorker(fake)  # type: ignore[arg-type]
    runner.start()
    try:
        await _settle(fake, want=2)
        assert fake.run_calls >= 2, (
            "the loop crashed and was never restarted -- this is the silent death "
            "that stopped every scheduled job in production"
        )
    finally:
        await runner.aclose()


@pytest.mark.asyncio
async def test_the_crash_is_logged_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A silent crash is the actual defect; the restart is only the remedy.

    The production incident was undiagnosable precisely because NOTHING was
    emitted for an hour, so the log line is part of the contract, not a nicety.

    Records the module logger directly rather than using ``caplog``/``capsys``/
    ``capfd``: this codebase logs through loguru, whose sink binds to the real
    stderr at import, so pytest's capture fixtures see nothing reliably. Asserting
    on the logger is both deterministic and closer to the actual contract.
    """
    recorded: list[tuple[str, str]] = []

    class _RecordingLog:
        def __getattr__(self, level: str) -> object:  # noqa: ANN401 — records any log level
            def _emit(message: str, **_fields: object) -> None:
                recorded.append((level, message))

            return _emit

    monkeypatch.setattr(worker_root, "_log", _RecordingLog())

    fake = _FakeWorker(["raise", "block"])
    runner = InProcessWorker(fake)  # type: ignore[arg-type]
    runner.start()
    try:
        await _settle(fake, want=2)
    finally:
        await runner.aclose()

    crash_lines = [(lvl, msg) for lvl, msg in recorded if "crashed" in msg]
    assert crash_lines, (
        "a crashed worker loop must say so; silence is exactly what hid this in production. "
        f"got: {recorded}"
    )
    level, message = crash_lines[0]
    assert level in {"exception", "error"}, f"a crash must log at error level, not {level!r}"
    assert "restarting" in message, "the log must state that recovery is being attempted"


@pytest.mark.asyncio
async def test_a_graceful_return_is_not_restarted() -> None:
    """A clean exit (drain) must END the worker, never respawn it.

    Restarting on a normal return would make shutdown impossible -- the
    supervisor would fight `aclose()` forever.
    """
    fake = _FakeWorker(["return"])
    runner = InProcessWorker(fake)  # type: ignore[arg-type]
    runner.start()
    try:
        await asyncio.sleep(0.15)
        assert fake.run_calls == 1, "a graceful return must not be restarted"
    finally:
        await runner.aclose()


@pytest.mark.asyncio
async def test_aclose_stops_a_healthy_loop_and_does_not_respawn() -> None:
    """Shutdown still works: drain requested, loop stopped, no resurrection."""
    fake = _FakeWorker(["block"])
    runner = InProcessWorker(fake)  # type: ignore[arg-type]
    runner.start()
    await _settle(fake, want=1)
    await runner.aclose()
    calls_at_close = fake.run_calls
    await asyncio.sleep(0.1)
    assert fake.run_calls == calls_at_close, "aclose must not be followed by a restart"
    assert fake.drain_requested is True
