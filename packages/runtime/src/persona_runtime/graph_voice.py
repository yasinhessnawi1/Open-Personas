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
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from typing import TYPE_CHECKING

from persona_runtime.prompt import GraphContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.graph.config import GraphSettings

__all__ = [
    "VOICE_NODE_BUDGET",
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
    """Kick off the (sync) graph query in a worker thread, concurrently.

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
    """
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, graph_retrieval, query)
    return loop.run_in_executor(None, call)


def take_graph_if_ready(task: asyncio.Future[GraphContext]) -> GraphContext:
    """The overlapped graph result if it already finished, else graph-off.

    Non-blocking by construction: if the query has not completed by the time the
    prompt is assembled, the turn proceeds with an **empty** bundle (no graph
    block) rather than waiting — the zero-serial guarantee and the degrade-to-off
    fallback in one. A query that errored also degrades to off (graph is additive
    presence, never a failure path on voice).
    """
    if task.done():
        if task.cancelled() or task.exception() is not None:
            return GraphContext()
        return task.result()
    # Not ready by assembly time: drop the graph for this turn. Detach so the
    # abandoned task's result/exception is consumed (no "never retrieved"
    # warning); an owner-scoped read has no side effects, so letting it finish in
    # the pool is harmless.
    task.add_done_callback(_discard_result)
    return GraphContext()


def _discard_result(task: asyncio.Future[GraphContext]) -> None:
    """Consume an abandoned task's outcome so asyncio doesn't warn."""
    if not task.cancelled():
        task.exception()
