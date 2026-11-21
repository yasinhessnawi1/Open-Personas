"""T6 — the voice graph posture (D-K3-6): tighter profile, overlapped or skipped.

The load-bearing lock proven, not asserted: graph-awareness on a voice turn adds
**zero serial wall-clock** to the pre-model path. The proof uses REAL wall-clock
timing (the same rigor as the T5 budget measurement) through the actual
:class:`FirstTokenLatencyTracker` — a graph query that overlaps V5's pre-model
window is free, one that overruns is dropped (graph-off), and a hypothetical
serial leg would be visibly larger in the tracker. The voice profile (traversal
off + raised floor + small node budget) is distinct; the chat profile is intact.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from typing import TYPE_CHECKING

import pytest
from persona.graph.config import GraphSettings
from persona_runtime.graph_voice import (
    VOICE_NODE_BUDGET,
    start_graph_retrieval,
    take_graph_if_ready,
    voice_graph_settings,
)
from persona_runtime.prompt import GraphContext, GraphKnowledgeItem, GraphRecency
from persona_runtime.routing.latency import FirstTokenLatencyTracker

if TYPE_CHECKING:
    from collections.abc import Callable


def _sleeper(ms: float) -> Callable[[str], GraphContext]:
    """A sync graph_retrieval that takes ``ms`` and returns a one-item bundle."""

    def f(query: str) -> GraphContext:  # noqa: ARG001 — query unused in the stub
        time.sleep(ms / 1000)
        return GraphContext(
            items=(GraphKnowledgeItem(concept_name="x", content="y", recency=GraphRecency.RECENT),)
        )

    return f


class TestVoiceProfile:
    def test_traversal_is_off(self) -> None:
        voice = voice_graph_settings(GraphSettings())
        assert voice.traversal_seed_count == 0
        assert voice.traversal_budget == 0

    def test_relevance_floor_is_raised(self) -> None:
        base = GraphSettings(inject_similarity_floor=0.66)
        assert voice_graph_settings(base).inject_similarity_floor > 0.66

    def test_floor_is_clamped_to_one(self) -> None:
        base = GraphSettings(inject_similarity_floor=0.99)
        assert voice_graph_settings(base).inject_similarity_floor <= 1.0

    def test_node_budget_is_a_tight_slice(self) -> None:
        assert 1 <= VOICE_NODE_BUDGET <= 4

    def test_standard_chat_profile_is_untouched(self) -> None:
        # One mechanism, profile-tuned: deriving the voice profile must not mutate
        # the base (chat) settings.
        base = GraphSettings()
        seed_before = base.traversal_seed_count
        voice_graph_settings(base)
        assert base.traversal_seed_count == seed_before
        assert base.traversal_seed_count > 0  # chat keeps traversal on


class TestOverlapOrSkip:
    @pytest.mark.asyncio
    async def test_take_if_ready_is_nonblocking_when_query_in_flight(self) -> None:
        task = start_graph_retrieval(_sleeper(200), "q")
        t0 = time.perf_counter()
        ctx = take_graph_if_ready(task)  # query still running
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert ctx == GraphContext()  # degrade to graph-off
        assert elapsed_ms < 20  # did NOT block on the 200 ms query
        await asyncio.gather(task, return_exceptions=True)  # drain the abandoned read

    @pytest.mark.asyncio
    async def test_overlapped_query_is_used_when_it_fits_the_window(self) -> None:
        task = start_graph_retrieval(_sleeper(80), "q")
        await asyncio.sleep(0.20)  # the concurrent pre-model window (> query)
        ctx = take_graph_if_ready(task)
        assert ctx.items  # the overlapped result was ready and used

    @pytest.mark.asyncio
    async def test_slow_query_degrades_to_graph_off(self) -> None:
        task = start_graph_retrieval(_sleeper(300), "q")
        await asyncio.sleep(0.05)  # window shorter than the query
        ctx = take_graph_if_ready(task)
        assert ctx == GraphContext()  # dropped for this turn, not awaited
        await asyncio.gather(task, return_exceptions=True)  # drain the abandoned read

    @pytest.mark.asyncio
    async def test_query_starts_at_call_time_not_at_the_callers_first_await(self) -> None:
        # R9-319: the voice producer runs its routing and selection work synchronously
        # after the kick-off, so the query must already be in its worker thread before
        # the caller yields. Otherwise that work is not overlapped at all. The loop is
        # held here (no await) while the worker is asked to start; the timeout only
        # bounds a failure, a passing run returns at once.
        started = threading.Event()
        release = threading.Event()

        def gated(query: str) -> GraphContext:
            started.set()
            release.wait(timeout=10)
            return _sleeper(0)(query)

        future = start_graph_retrieval(gated, "q")
        try:
            assert started.wait(timeout=5), "the query did not start until the caller yielded"
        finally:
            release.set()
        assert (await future).items

    @pytest.mark.asyncio
    async def test_query_sees_the_callers_context_at_kick_off(self) -> None:
        # The owner scope and the K4 recent window are ContextVars set before the
        # kick-off; the worker thread must see them, as asyncio.to_thread would.
        marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="unset")
        marker.set("turn-7")
        seen: list[str] = []

        def reads_marker(query: str) -> GraphContext:
            seen.append(marker.get())
            return _sleeper(0)(query)

        await start_graph_retrieval(reads_marker, "q")
        assert seen == ["turn-7"]

    @pytest.mark.asyncio
    async def test_errored_query_degrades_to_graph_off(self) -> None:
        def _boom(query: str) -> GraphContext:  # noqa: ARG001
            raise RuntimeError("retrieval failed")

        task = start_graph_retrieval(_boom, "q")
        await asyncio.sleep(0.02)
        assert take_graph_if_ready(task) == GraphContext()  # never a failure path


class TestZeroSerialAgainstLatencyTracker:
    """Prove zero serial wall-clock added to TTFT — measured through the real tracker."""

    async def _ttft_ms(
        self, mode: str, *, window_ms: float, graph_ms: float
    ) -> tuple[float, GraphContext]:
        """Simulate the pre-model critical path; return its wall-clock + graph used.

        ``window_ms`` is V5's existing concurrent pre-model work (store retrieve,
        routing, STT tail). ``graph_ms`` mimics K1's retrieval cost. ``first
        token`` is emitted right after assembly — so this wall-clock IS the
        pre-model contribution to TTFT.
        """
        slow = _sleeper(graph_ms)
        t0 = time.perf_counter()
        task = start_graph_retrieval(slow, "q") if mode == "overlap" else None
        await asyncio.sleep(window_ms / 1000)  # V5's concurrent pre-model work
        if mode == "overlap":
            assert task is not None
            ctx = take_graph_if_ready(task)
        elif mode == "serial":
            ctx = await asyncio.to_thread(slow, "q")  # a NEW serial leg (the forbidden design)
        else:  # "off"
            ctx = GraphContext()
        return (time.perf_counter() - t0) * 1000, ctx

    @pytest.mark.asyncio
    async def test_overlap_adds_zero_serial_serial_does_not(self) -> None:
        tracker = FirstTokenLatencyTracker(warmup_n=1)
        window, graph = 160.0, 90.0  # graph fits inside the pre-model window

        for _ in range(4):
            ms_off, _ = await self._ttft_ms("off", window_ms=window, graph_ms=graph)
            tracker.record("off", ms_off)
        for _ in range(4):
            ms_ov, ctx = await self._ttft_ms("overlap", window_ms=window, graph_ms=graph)
            tracker.record("overlap", ms_ov)
            assert ctx.items  # overlap actually used the graph (it fit the window)
        for _ in range(4):
            ms_ser, _ = await self._ttft_ms("serial", window_ms=window, graph_ms=graph)
            tracker.record("serial", ms_ser)

        off = tracker.get("off")
        overlap = tracker.get("overlap")
        serial = tracker.get("serial")
        assert off is not None
        assert overlap is not None
        assert serial is not None

        # Overlap adds ~zero serial vs the graph-off baseline (generous jitter
        # margin); a serial leg adds ~the whole graph time and is visibly larger.
        assert overlap - off < 40, f"overlap added serial time: off={off:.0f} overlap={overlap:.0f}"
        assert serial - off > 50, f"serial leg not visible: off={off:.0f} serial={serial:.0f}"
        assert serial - overlap > 40, "overlap must beat serial by ~the graph cost"
