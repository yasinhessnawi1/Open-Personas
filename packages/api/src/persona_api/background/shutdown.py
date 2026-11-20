"""Bounded shutdown: the api's teardown fits inside the container's stop grace (R9-313).

Docker gives the api 30 s from SIGINT to SIGKILL (deploy/server/docker-compose.yml). Uvicorn
spends up to ``--timeout-graceful-shutdown`` (5 s) on open requests, then the lifespan tears
down under one :class:`ShutdownDeadline` (``PERSONA_API_SHUTDOWN_BUDGET_SECONDS``, 20 s). The
worker drain, the connector transports and the in-flight runs and chat turns share that budget,
concurrently; whatever is still running at the deadline is given up on and logged.

Two rules every wait here keeps:

* ``asyncio.wait`` with a timeout, never ``wait_for``. On timeout ``wait_for`` cancels the
  awaited task again and then waits for that cancel to land, which is exactly what a task
  stuck in a shielded database write cannot do.
* Nothing given up on is lost for good: a durable job's lease lapses and the worker's
  maintenance sweep requeues it (``JobQueue.reclaim_expired``); a chat turn or run left
  ``running`` is reconciled by the restart sweep on the next boot.

What this cannot bound: a thread already inside a database call. ``asyncio.run`` joins the
default executor's threads on exit, so a thread stuck there holds the process until its
statement or TCP bound ends it (minutes) or SIGKILL at the grace does, whichever is first.
SIGKILL is the backstop. The kernel then closes the socket, and Postgres rolls back the
transaction the thread had open, so no write is left half done.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, TypeVar

from persona.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

__all__ = ["ShutdownDeadline", "cancel_and_wait"]

_log = get_logger("api.shutdown")

#: Any task or future: ``Mapping`` is invariant in its key, so a ``dict[Task[None], str]`` is
#: not a ``Mapping[Future[Any], str]`` without this.
_F = TypeVar("_F", bound="asyncio.Future[Any]")


async def cancel_and_wait(tasks: Mapping[_F, str], *, timeout: float, what: str) -> list[str]:
    """Cancel ``tasks`` and wait for them all together, for at most ``timeout`` seconds.

    ``tasks`` maps each task to a label for the log (ids only, no content). Returns the labels
    of the tasks still running at the bound, each logged as abandoned; a task that ended with
    an error is logged too, and never raised into the shutdown.
    """
    if not tasks:
        return []
    for task in tasks:
        task.cancel()
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    _log_errors(done, tasks, what=what)
    abandoned = [tasks[task] for task in pending]
    for label in abandoned:
        _log.warning(
            "{what} abandoned at shutdown: still running after {bound}s; {label}",
            what=what,
            bound=timeout,
            label=label,
        )
    return abandoned


def _log_errors(done: set[_F], labels: Mapping[_F, str], *, what: str) -> None:
    for task in done:
        if not task.cancelled() and task.exception() is not None:
            _log.warning(
                "{what} {label} ended with an error during shutdown: {err}",
                what=what,
                label=labels[task],
                err=str(task.exception()),
            )


class ShutdownDeadline:
    """One time budget for the whole teardown, spent by every step that waits."""

    def __init__(self, budget_s: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        if budget_s <= 0:
            msg = "the shutdown budget must be positive"
            raise ValueError(msg)
        self._clock = clock
        self._ends_at = clock() + budget_s

    def remaining(self) -> float:
        """Seconds left in the budget, never negative."""
        return max(0.0, self._ends_at - self._clock())

    async def run(self, steps: Mapping[str, Awaitable[object]]) -> list[str]:
        """Run the named steps together; give up on any still running when the budget ends.

        A step given up on is cancelled and not waited for. Returns their names. A step that
        fails is logged and does not stop the others.
        """
        if not steps:
            return []
        tasks = {asyncio.ensure_future(step): name for name, step in steps.items()}
        done, pending = await asyncio.wait(tasks, timeout=self.remaining())
        _log_errors(done, tasks, what="shutdown step")
        abandoned = [tasks[task] for task in pending]
        for task in pending:
            task.cancel()
            _log.warning("shutdown step abandoned at the deadline: {name}", name=tasks[task])
        return abandoned

    async def cancel(self, tasks: Mapping[str, asyncio.Future[Any] | None]) -> list[str]:
        """Cancel the named background tasks (``None`` skipped) within the remaining budget."""
        live = {task: name for name, task in tasks.items() if task is not None}
        return await cancel_and_wait(live, timeout=self.remaining(), what="background task")
