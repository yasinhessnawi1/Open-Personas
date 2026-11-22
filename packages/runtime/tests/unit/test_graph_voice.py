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
import gc
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import pytest
from loguru import logger as _loguru_logger
from persona.graph.config import GraphSettings
from persona_runtime import graph_voice
from persona_runtime.graph_voice import (
    VOICE_NODE_BUDGET,
    configure_graph_executor,
    shutdown_graph_executor,
    start_graph_retrieval,
    take_graph_if_ready,
    voice_graph_settings,
)
from persona_runtime.prompt import GraphContext, GraphKnowledgeItem, GraphRecency
from persona_runtime.routing.latency import FirstTokenLatencyTracker

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

#: Bounds a FAILURE only: a passing run returns as soon as the awaited event is set.
_GATE_TIMEOUT_S = 5.0


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
        # kick-off; the worker thread must see them through the context copy
        # start_graph_retrieval makes at kick-off.
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


class TestOwnExecutor:
    """R9-323: the graph query runs on its own small pool, not the shared default one."""

    @pytest.mark.asyncio
    async def test_store_read_is_not_queued_behind_the_graph_query(self) -> None:
        # The turn's store read goes through asyncio.to_thread, the loop's default
        # executor. Shrink that pool to one worker: if the graph query shared it, the
        # store read would wait in the queue until the query finished. The query
        # waits for the store read instead, so a shared pool is a five-second miss
        # (the timeout below) and a separate pool returns at once. No wall clock.
        default_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="default-test")
        asyncio.get_running_loop().set_default_executor(default_pool)
        store_read_ran = threading.Event()
        marker: contextvars.ContextVar[str] = contextvars.ContextVar("owner", default="unset")
        marker.set("owner-7")
        seen: list[tuple[bool, str]] = []

        def graph_query(query: str) -> GraphContext:
            seen.append((store_read_ran.wait(_GATE_TIMEOUT_S), marker.get()))
            return _sleeper(0)(query)

        try:
            future = start_graph_retrieval(graph_query, "q")
            await asyncio.to_thread(store_read_ran.set)
            assert (await future).items
        finally:
            default_pool.shutdown(wait=True)
        # The store read ran while the query was still in flight, and the owner
        # scope still crossed through the context copy made at kick-off.
        assert seen == [(True, "owner-7")]

    @pytest.mark.asyncio
    async def test_a_shut_down_pool_is_replaced_on_the_next_query(self) -> None:
        # The process shuts the pool down with its other resources; a later kick-off
        # (a new app in the same process, or the next test) lazily builds a fresh one.
        assert (await start_graph_retrieval(_sleeper(0), "q")).items
        shutdown_graph_executor()
        shutdown_graph_executor()  # idempotent
        assert (await start_graph_retrieval(_sleeper(0), "q")).items


@pytest.fixture
def graph_log() -> Iterator[list[tuple[str, str]]]:
    """``(level, message)`` of every record the voice graph module emits."""
    captured: list[tuple[str, str]] = []

    def _sink(message: object) -> None:
        record = message.record  # type: ignore[attr-defined]
        if record["name"] == "persona_runtime.graph_voice":
            captured.append((record["level"].name, record["message"]))

    sink = _loguru_logger.add(_sink, level="DEBUG")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink)


@pytest.fixture
def fresh_pool() -> Iterator[None]:
    """A voice graph pool built from scratch at the default size, released after."""
    shutdown_graph_executor()
    configure_graph_executor(graph_voice.DEFAULT_GRAPH_QUERY_WORKERS)
    try:
        yield
    finally:
        configure_graph_executor(graph_voice.DEFAULT_GRAPH_QUERY_WORKERS)
        shutdown_graph_executor()


class _Gate:
    """Graph queries that hold their worker until released, and say when they start."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.ran: list[str] = []

    def query(self, label: str) -> tuple[Callable[[str], GraphContext], threading.Event]:
        started = threading.Event()

        def gated(query: str) -> GraphContext:
            self.ran.append(label)
            started.set()
            assert self.release.wait(_GATE_TIMEOUT_S), "the gate was never released"
            return _sleeper(0)(query)

        return gated, started


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestLateDrops:
    """R9-323: a query dropped as late gives its slot back, and the drops are visible."""

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fresh_pool")
    async def test_a_late_query_behind_busy_workers_is_cancelled_and_never_runs(self) -> None:
        # Both workers are held; the third query waits in the queue. Dropping it as
        # late must cancel it, so it never takes a worker for a turn that has moved
        # on. A fourth query submitted after it proves the queue was drained past it.
        gate = _Gate()
        query_a, started_a = gate.query("a")
        query_b, started_b = gate.query("b")
        query_c, _ = gate.query("c")
        query_d, _ = gate.query("d")
        a = start_graph_retrieval(query_a, "q")
        b = start_graph_retrieval(query_b, "q")
        try:
            assert started_a.wait(_GATE_TIMEOUT_S)
            assert started_b.wait(_GATE_TIMEOUT_S)
            c = start_graph_retrieval(query_c, "q")
            assert take_graph_if_ready(c) == GraphContext()
            assert c.cancelled()
            # The cancel reaches the pool on the next loop pass, which a real turn
            # gives at its next await; both workers are still held until then.
            await asyncio.sleep(0)
            d = start_graph_retrieval(query_d, "q")
        finally:
            gate.release.set()
        await asyncio.gather(a, b, d)
        assert sorted(gate.ran) == ["a", "b", "d"]  # the dropped query never ran

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fresh_pool")
    async def test_a_running_late_query_is_ignored_when_it_finishes(
        self, graph_log: list[tuple[str, str]]
    ) -> None:
        # A query already running cannot be stopped; it finishes, its result is
        # ignored, and the pool goes on serving the next turn.
        gate = _Gate()
        query_a, started_a = gate.query("a")
        a = start_graph_retrieval(query_a, "q")
        try:
            assert started_a.wait(_GATE_TIMEOUT_S)
            assert take_graph_if_ready(a) == GraphContext()
        finally:
            gate.release.set()
        assert (await start_graph_retrieval(_sleeper(0), "q")).items
        assert a.cancelled()
        assert not [message for _, message in graph_log if "query failed" in message]

    @pytest.mark.asyncio
    async def test_late_drops_are_counted_and_warned_at_most_once_a_minute(
        self, monkeypatch: pytest.MonkeyPatch, graph_log: list[tuple[str, str]]
    ) -> None:
        clock = _FakeClock()
        meter = graph_voice.LateDropMeter(clock=clock)
        monkeypatch.setattr(graph_voice, "_LATE_DROPS", meter)
        loop = asyncio.get_running_loop()

        def drop_one() -> None:
            pending: asyncio.Future[GraphContext] = loop.create_future()
            assert take_graph_if_ready(pending) == GraphContext()
            assert pending.cancelled()

        drop_one()  # the first drop in the process is reported at once
        for seconds in (10.0, 30.0, 59.0):
            clock.now = 1000.0 + seconds
            drop_one()  # within the minute: noted at debug only
        clock.now = 1000.0 + 61.0
        drop_one()  # a minute on: one report for the drops since the last one

        warnings = [message for level, message in graph_log if level == "WARNING"]
        debugs = [message for level, message in graph_log if level == "DEBUG"]
        assert len(debugs) == 5  # every drop is noted at debug
        assert len(warnings) == 2
        assert "1 since the last report, 1 in this process" in warnings[0]
        assert "4 since the last report, 5 in this process" in warnings[1]
        assert meter.total == 5

    @pytest.mark.asyncio
    async def test_a_ready_query_is_not_a_late_drop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        meter = graph_voice.LateDropMeter(clock=_FakeClock())
        monkeypatch.setattr(graph_voice, "_LATE_DROPS", meter)
        future = start_graph_retrieval(_sleeper(0), "q")
        await asyncio.wait({future})
        assert take_graph_if_ready(future).items
        assert meter.total == 0


class TestPoolSizeAndShutdown:
    """R9-323: the worker count is configurable, and shutdown is quiet and race-safe."""

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fresh_pool")
    async def test_the_configured_worker_count_runs_that_many_queries_at_once(self) -> None:
        configure_graph_executor(3)
        gate = _Gate()
        queries = [gate.query(label) for label in ("a", "b", "c")]
        futures = [start_graph_retrieval(query, "q") for query, _ in queries]
        try:
            assert all(started.wait(_GATE_TIMEOUT_S) for _, started in queries)
        finally:
            gate.release.set()
        await asyncio.gather(*futures)

    @pytest.mark.parametrize("workers", [0, -1])
    def test_a_worker_count_below_one_is_refused(self, workers: int) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            configure_graph_executor(workers)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fresh_pool")
    async def test_a_shutdown_between_pool_lookup_and_submit_runs_the_turn_graph_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The process closes the pool after start_graph_retrieval has looked it up
        # but before it submits. The turn must not see the RuntimeError.
        real_pool = graph_voice._graph_pool  # noqa: SLF001

        def racing_pool() -> ThreadPoolExecutor:
            pool = real_pool()
            shutdown_graph_executor()
            return pool

        monkeypatch.setattr(graph_voice, "_graph_pool", racing_pool)
        ran: list[str] = []

        def query(text: str) -> GraphContext:
            ran.append(text)
            return _sleeper(0)(text)

        future = start_graph_retrieval(query, "q")
        assert future.done()
        assert take_graph_if_ready(future) == GraphContext()
        assert ran == []

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("fresh_pool")
    async def test_a_shutdown_with_queued_queries_is_quiet(
        self, graph_log: list[tuple[str, str]]
    ) -> None:
        # Shutdown cancels the queued queries. A cancelled query is not a failure:
        # no warning, and nothing for the loop's exception handler.
        loop = asyncio.get_running_loop()
        reported: list[dict[str, Any]] = []
        loop.set_exception_handler(lambda _loop, context: reported.append(context))
        gate = _Gate()
        query_a, started_a = gate.query("a")
        query_b, started_b = gate.query("b")
        query_c, _ = gate.query("c")
        try:
            a = start_graph_retrieval(query_a, "q")
            b = start_graph_retrieval(query_b, "q")
            assert started_a.wait(_GATE_TIMEOUT_S)
            assert started_b.wait(_GATE_TIMEOUT_S)
            c = start_graph_retrieval(query_c, "q")
            shutdown_graph_executor()
            done, _ = await asyncio.wait({c}, timeout=_GATE_TIMEOUT_S)
            assert c in done
            assert c.cancelled()
            gate.release.set()
            await asyncio.gather(a, b)
            del c, done
            gc.collect()
        finally:
            gate.release.set()
            loop.set_exception_handler(None)
        assert sorted(gate.ran) == ["a", "b"]
        assert reported == []
        assert [message for level, message in graph_log if level == "WARNING"] == []


@pytest.fixture
def graph_warnings() -> Iterator[list[str]]:
    """The message of every WARNING the voice graph module emits."""
    captured: list[str] = []

    def _sink(message: object) -> None:
        record = message.record  # type: ignore[attr-defined]
        if record["level"].name == "WARNING" and record["name"] == "persona_runtime.graph_voice":
            captured.append(record["message"])

    sink = _loguru_logger.add(_sink, level="WARNING")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink)


_USER_TEXT = "sentinel spoken words from the caller"


def _boom(query: str) -> GraphContext:  # noqa: ARG001
    raise RuntimeError(_USER_TEXT)


class TestFailuresAreLogged:
    """R9-323: a failed graph query logs its exception type once, and is always read."""

    @pytest.mark.asyncio
    async def test_a_failed_query_logs_one_warning_with_the_type_only(
        self, graph_warnings: list[str]
    ) -> None:
        future = start_graph_retrieval(_boom, "q")
        await asyncio.wait({future})
        await asyncio.sleep(0)  # the done callbacks run on the next loop pass
        assert take_graph_if_ready(future) == GraphContext()
        assert len(graph_warnings) == 1
        assert "RuntimeError" in graph_warnings[0]
        assert _USER_TEXT not in graph_warnings[0]  # the message could hold user text

    @pytest.mark.asyncio
    async def test_a_successful_query_logs_nothing(self, graph_warnings: list[str]) -> None:
        future = start_graph_retrieval(_sleeper(0), "q")
        await asyncio.wait({future})
        await asyncio.sleep(0)
        assert graph_warnings == []

    @pytest.mark.asyncio
    async def test_a_barge_in_leaves_no_unread_error(self, graph_warnings: list[str]) -> None:
        # A barge-in cancels the turn before it takes the graph, so nobody ever reads
        # the failed future. asyncio reports such a future when it is collected
        # ("Future exception was never retrieved"); the done callback must read it.
        loop = asyncio.get_running_loop()
        reported: list[dict[str, Any]] = []
        loop.set_exception_handler(lambda _loop, context: reported.append(context))
        try:
            future = start_graph_retrieval(_boom, "q")
            await asyncio.wait({future})
            await asyncio.sleep(0)
            del future
            gc.collect()
        finally:
            loop.set_exception_handler(None)
        assert reported == []
        assert len(graph_warnings) == 1


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
