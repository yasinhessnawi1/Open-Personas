"""K3-D-6 — the voice graph posture: a tighter profile, overlapped or skipped.

Voice has no TTFT slack — V5 measured first-token ~1607 ms, already over the
~600 ms gate (V5 close-out). So graph-awareness on a voice turn must add **zero
serial wall-clock** to the pre-model path. Two halves, one mechanism (Spec 18
profile philosophy — the standard chat profile from T2–T5 is untouched; voice
gets the tighter slice):

1. **The profile** (:func:`voice_graph_settings` + :data:`VOICE_NODE_BUDGET`):
   traversal OFF (the cheapest big latency win — no neighbour-expansion hops), a
   raised relevance floor (fewer, surer nodes under pressure), and a small node
   budget.
2. **Overlap-or-skip** (:func:`start_graph_retrieval` + :func:`take_graph_if_ready`):
   the query is kicked off in a worker thread at turn onset so it runs
   CONCURRENTLY with V5's existing pre-model work, and its result is taken **only
   if it already finished** by prompt-assembly time. A query that overruns the
   window is dropped for that turn (the turn proceeds **graph-off**) — never
   awaited on the critical path. So graph-awareness on voice is either free
   (overlapped) or absent (skipped), never a tax.

The query runs on a small pool of its own (R9-323), not the loop's default
executor: the turn's store read goes through ``asyncio.to_thread``, and a query
whose result may be dropped must not queue ahead of it when the shared pool is
full. A failed query logs its exception type once and is always read, so a
barge-in never leaves an unread error behind. A query dropped as late is
cancelled, so a queued one never takes a worker for a turn that has moved on,
and the drops are counted and reported (:class:`LateDropMeter`) so an operator
can see graph memory going off under a slow graph backend.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from persona.logging import get_logger

from persona_runtime.prompt import GraphContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.graph.config import GraphSettings

__all__ = [
    "DEFAULT_GRAPH_QUERY_WORKERS",
    "VOICE_NODE_BUDGET",
    "LateDropMeter",
    "configure_graph_executor",
    "shutdown_graph_executor",
    "start_graph_retrieval",
    "take_graph_if_ready",
    "voice_graph_settings",
]

#: The voice node budget (D-K3-6): fewer nodes than chat's ``result_budget`` — a
#: tighter slice for the latency-bound path. Passed to selection as ``max_items``.
VOICE_NODE_BUDGET = 4

#: How much the relevance floor is raised for voice vs chat (fewer, surer nodes
#: under pressure). Added to the base ``inject_similarity_floor``.
_VOICE_FLOOR_BUMP = 0.06

_log = get_logger("runtime.graph_voice")

#: Default workers in the voice graph pool (R9-323). A voice turn runs one query
#: and a worker process holds few calls, so two is enough; the bound keeps a slow
#: graph backend from piling up threads. The voice service sets the real count
#: from ``PERSONA_VOICE_GRAPH_QUERY_WORKERS`` (:func:`configure_graph_executor`).
DEFAULT_GRAPH_QUERY_WORKERS = 2

#: The shortest gap between two late-drop warnings (:class:`LateDropMeter`).
_LATE_DROP_REPORT_INTERVAL_S = 60.0

# Lazily created so importing this module starts no threads, and replaced after a
# shutdown so a later kick-off in the same process still has a pool.
_GRAPH_POOL: ThreadPoolExecutor | None = None
_GRAPH_POOL_WORKERS = DEFAULT_GRAPH_QUERY_WORKERS
_GRAPH_POOL_LOCK = threading.Lock()


def configure_graph_executor(max_workers: int) -> None:
    """Set the voice graph pool's worker count (R9-323).

    Takes effect when the pool is next built: at the first query, or after
    :func:`shutdown_graph_executor`. The voice launcher calls it once at
    construction, before any call can start a query.
    """
    if max_workers < 1:
        msg = f"the voice graph pool needs at least 1 worker, got {max_workers}"
        raise ValueError(msg)
    global _GRAPH_POOL_WORKERS  # noqa: PLW0603 (module singleton, see _graph_pool)
    with _GRAPH_POOL_LOCK:
        _GRAPH_POOL_WORKERS = max_workers


def _graph_pool() -> ThreadPoolExecutor:
    global _GRAPH_POOL  # noqa: PLW0603 (module singleton, lazy init under a lock)
    with _GRAPH_POOL_LOCK:
        if _GRAPH_POOL is None:
            _GRAPH_POOL = ThreadPoolExecutor(
                max_workers=_GRAPH_POOL_WORKERS, thread_name_prefix="voice-graph"
            )
        return _GRAPH_POOL


def shutdown_graph_executor() -> None:
    """Release the voice graph pool (idempotent). Call where the process closes up.

    Queued queries are cancelled and a running one is not waited on: its result
    is only ever taken if ready, so nothing needs it once the calls have ended.
    The next :func:`start_graph_retrieval` builds a fresh pool.
    """
    global _GRAPH_POOL  # noqa: PLW0603 (module singleton, see _graph_pool)
    with _GRAPH_POOL_LOCK:
        pool, _GRAPH_POOL = _GRAPH_POOL, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


class LateDropMeter:
    """Counts graph queries dropped as late, and reports them at a bounded rate.

    Every drop is noted at debug. A warning goes out at the first drop, then at
    most once per ``interval_s`` while drops keep coming, naming how many came
    since the last report and in the whole process. A run of them means graph
    memory is quietly off for the calls in this process (R9-323).
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        interval_s: float = _LATE_DROP_REPORT_INTERVAL_S,
    ) -> None:
        self._clock = clock
        self._interval_s = interval_s
        self._lock = threading.Lock()
        self._total = 0
        self._reported = 0
        self._last_report = -math.inf

    @property
    def total(self) -> int:
        """Late drops in this process so far."""
        return self._total

    def note(self) -> None:
        """Record one late drop, and warn when a report is due."""
        with self._lock:
            self._total += 1
            total = self._total
            now = self._clock()
            due = now - self._last_report >= self._interval_s
            since_last = total - self._reported
            if due:
                self._reported = total
                self._last_report = now
        _log.debug("voice graph query not ready by assembly; dropped ({total})", total=total)
        if due:
            _log.warning(
                "voice graph queries dropped as late: {since_last} since the last report, "
                "{total} in this process; those turns ran graph-off",
                since_last=since_last,
                total=total,
            )


_LATE_DROPS = LateDropMeter()


def voice_graph_settings(base: GraphSettings) -> GraphSettings:
    """The voice profile: traversal OFF + a raised relevance floor (D-K3-6).

    Returns a copy of ``base`` with traversal disabled (``seed_count`` and
    ``budget`` zeroed — the documented no-code-change off switch, config.py) and
    the injection floor bumped. ``base`` is unmodified, so the standard chat
    profile is untouched — voice gets the tighter slice from the same mechanism.
    The node count is applied separately as the ``VOICE_NODE_BUDGET`` selection
    cap.
    """
    return base.model_copy(
        update={
            "traversal_seed_count": 0,
            "traversal_budget": 0,
            "inject_similarity_floor": min(1.0, base.inject_similarity_floor + _VOICE_FLOOR_BUMP),
        }
    )


def start_graph_retrieval(
    graph_retrieval: Callable[[str], GraphContext], query: str
) -> asyncio.Future[GraphContext]:
    """Kick off the (sync) graph query on the voice graph pool, concurrently.

    Returns a future for ``graph_retrieval(query)`` running off the event-loop
    thread, so it overlaps the rest of the pre-model work. The caller takes the
    result with :func:`take_graph_if_ready`; it is never awaited on the critical
    path. Call at turn onset (as soon as the transcribed message is known).

    The query is handed to the executor HERE, before this returns (R9-319). The
    earlier ``create_task(to_thread(...))`` only reached the executor at the
    caller's first ``await``, so the synchronous routing and selection work right
    after the kick-off was never overlapped, and the query's window shrank to the
    store retrieval alone. The caller's context is copied now, exactly as
    :func:`asyncio.to_thread` copies it, so the owner scope and the K4 recent
    window still cross into the worker thread.

    The pool is the module's own (R9-323), so the query never queues ahead of
    the turn's store read in the default executor. A done callback reads every
    outcome: a failure logs one warning and its error is never left unread.

    If the process closes the pool between the lookup and the submit, the turn
    gets a finished future holding an empty bundle: it runs graph-off.
    """
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, graph_retrieval, query)
    try:
        future = loop.run_in_executor(_graph_pool(), call)
    except RuntimeError:
        # "cannot schedule new futures after shutdown": the process is closing.
        _log.debug("voice graph pool closed at kick-off; the turn runs graph-off")
        closed: asyncio.Future[GraphContext] = loop.create_future()
        closed.set_result(GraphContext())
        return closed
    future.add_done_callback(_log_failure)
    return future


def take_graph_if_ready(task: asyncio.Future[GraphContext]) -> GraphContext:
    """The overlapped graph result if it already finished, else graph-off.

    Non-blocking by construction: if the query has not completed by the time the
    prompt is assembled, the turn proceeds with an **empty** bundle (no graph
    block) rather than waiting — the zero-serial guarantee and the degrade-to-off
    fallback in one. A query that errored also degrades to off (graph is additive
    presence, never a failure path on voice).

    A query not ready is cancelled (R9-323): one still queued never runs, so it
    gives its place in the small pool back (the cancel reaches the pool on the
    loop's next pass, at the turn's next await); one already running finishes
    and is ignored. Each such drop is counted by the process's :class:`LateDropMeter`.
    """
    if task.done():
        if task.cancelled() or task.exception() is not None:
            return GraphContext()
        return task.result()
    # Not ready by assembly time: drop the graph for this turn. Cancelling marks
    # the future done (nothing is left unread) and stops a queued query; an
    # owner-scoped read has no side effects, so one already running may finish.
    task.cancel()
    _LATE_DROPS.note()
    return GraphContext()


def _log_failure(future: asyncio.Future[GraphContext]) -> None:
    """Read the query's outcome and log a failure by its type. Never raises.

    Reading the exception marks it retrieved, so a turn that never takes the
    graph (a barge-in) leaves no "Future exception was never retrieved". Only
    the type is logged: the message could carry the caller's words.
    """
    try:
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            _log.warning(
                "voice graph query failed; the turn runs graph-off ({error})",
                error=type(error).__name__,
            )
    except Exception:  # noqa: BLE001, S110 (a log line must never break the loop)
        pass
