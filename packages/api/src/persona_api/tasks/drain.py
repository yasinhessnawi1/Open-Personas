"""The deploy drain, reaching a leg that is already running (R9-129).

A2 gave :meth:`LegExecutor.run_leg` an ``external_cancel`` token so a leg could be stopped
cooperatively: the loop checks it at each step boundary, so the leg finishes its in-flight
step, writes its checkpoint, and stops. Two docstrings then claimed the worker's drain
signal was wired to it. Neither was: ``grep -rn 'external_cancel=' packages/*/src`` returned
nothing, so the executor always built a fresh token that nobody held, and a redeploy killed
whatever leg was running mid-step. The job's lease expired and it was reclaimed later, which
is correct but wasteful: the leg's model spend was already paid for and its work was thrown
away, because the salvage the box mechanism exists to perform never got asked for.

This is the holder of those tokens. One instance per worker process, composed at the worker
root and handed to the ``task_leg`` handler, which takes a token per leg and passes it as
``external_cancel``.

**Why in-memory is the right shape here, when the user's controls are durable.** W1's
``_ControlledRunner`` (D-W1-21) reads the durable task row at every step boundary, because a
cancel or a pause can be pressed in any process and has to reach a leg running in another
one. A drain is the opposite: it is this process, stopping. Nothing outside it needs to
know, the signal cannot outlive the process it belongs to, and a durable flag would be worse
than useless, since a crashed worker would leave a "draining" row that stops the next one.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.tasks import LegBoxLimit
from persona_runtime.agentic.run import CancelToken

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["LegDrainSignal"]

_log = get_logger("tasks.drain")


class LegDrainSignal:
    """Trips every running leg's cancel token when the process is asked to drain.

    Single-threaded by design: the worker's claim loop, the legs and the signal handler all
    run on one asyncio event loop, so the set is only ever touched between awaits.
    """

    def __init__(self) -> None:
        self._draining = False
        self._live: set[CancelToken] = set()

    @property
    def draining(self) -> bool:
        """True once :meth:`request_drain` has been called. Never goes back to false."""
        return self._draining

    @property
    def live_legs(self) -> int:
        """How many legs currently hold a token (what a drain would have to stop)."""
        return len(self._live)

    @contextmanager
    def leg_token(self) -> Iterator[CancelToken]:
        """Hand out this leg's token, and release it when the leg ends.

        A token handed out during a drain arrives already cancelled: the claim loop stops
        claiming on the drain signal, but a job claimed a moment earlier is still on its way
        into the handler, and that leg should stop at its first boundary rather than start a
        full box the process will not live long enough to finish.
        """
        token = CancelToken()
        if self._draining:
            token.cancel(LegBoxLimit.DRAIN.value)
        self._live.add(token)
        try:
            yield token
        finally:
            self._live.discard(token)

    def request_drain(self) -> None:
        """Ask every running leg to stop at its next step boundary. Idempotent.

        Wired to the worker's own drain signal, so a deploy, a Ctrl-C and the app lifespan's
        shutdown all reach a running leg through one path.
        """
        first = not self._draining
        self._draining = True
        for token in self._live:
            token.cancel(LegBoxLimit.DRAIN.value)
        if first:
            _log.info("drain requested: asking running legs to checkpoint", legs=len(self._live))
