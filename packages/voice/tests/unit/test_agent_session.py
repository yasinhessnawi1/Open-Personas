"""Unit tests for the V6 A0 agent-worker lifecycle (spec V6).

These exercise :class:`AgentSession.run` (connect → active → pipeline → await
disconnect → teardown) and :class:`InProcessAgentLauncher` with fully-faked
collaborators — the STT/TTS/model/DB internals are already covered by V2–V5, so
the lifecycle ordering + teardown robustness are what's tested here. The real
heavy assembly (:func:`build_agent_session`) is exercised live by the V6
operator pass (D2), not unit tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from persona_voice.agent.launcher import InProcessAgentLauncher
from persona_voice.agent.runner import AgentSession

pytestmark = [pytest.mark.asyncio]


class _FakeSessionMachine:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    @property
    def session(self) -> object:
        return type("S", (), {"session_id": "s1"})()

    async def mark_active(self) -> None:
        self._calls.append("mark_active")

    async def end(self) -> None:
        self._calls.append("session_end")


class _FakeRoom:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.disconnect_handler: Any = None

    def set_disconnect_handler(self, handler: object) -> None:
        self.disconnect_handler = handler

    async def connect(self, url: str, token: str) -> None:  # noqa: ARG002
        self._calls.append("room_connect")

    async def disconnect(self) -> None:
        self._calls.append("room_disconnect")


class _FakeLoop:
    def __init__(self, calls: list[str], *, stop_raises: bool = False) -> None:
        self._calls = calls
        self._stop_raises = stop_raises

    async def start_pipeline(self) -> None:
        self._calls.append("start_pipeline")

    async def stop(self) -> None:
        self._calls.append("loop_stop")
        if self._stop_raises:
            msg = "boom"
            raise RuntimeError(msg)


class _FakeSttSeam:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def load(self) -> None:
        self._calls.append("stt_load")

    async def close(self) -> None:
        self._calls.append("stt_close")


class _FakeTtsSeam:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def cancel(self) -> None:
        self._calls.append("tts_cancel")


class _FakeMcpClient:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def disconnect(self) -> None:
        self._calls.append("mcp_disconnect")


def _make_session(calls: list[str], *, stop_raises: bool = False) -> tuple[AgentSession, Any]:
    ended = asyncio.Event()
    room = _FakeRoom(calls)
    agent = AgentSession(
        voice_room=room,  # type: ignore[arg-type]
        loop=_FakeLoop(calls, stop_raises=stop_raises),  # type: ignore[arg-type]
        stt_seam=_FakeSttSeam(calls),  # type: ignore[arg-type]
        tts_seam=_FakeTtsSeam(calls),
        session=_FakeSessionMachine(calls),  # type: ignore[arg-type]
        mcp_clients=[_FakeMcpClient(calls)],  # type: ignore[list-item]
        livekit_url="ws://localhost:7880",
        agent_token="tok",
        ended=ended,
    )
    return agent, ended


async def test_agent_session_runs_connect_active_pipeline_then_awaits_disconnect() -> None:
    calls: list[str] = []
    agent, ended = _make_session(calls)

    task = asyncio.create_task(agent.run())
    # Let run() reach the ended.wait() barrier.
    for _ in range(20):
        await asyncio.sleep(0)
        if "start_pipeline" in calls:
            break

    # Startup sequence ran; teardown has NOT (still awaiting disconnect).
    assert calls == ["stt_load", "room_connect", "mark_active", "start_pipeline"]

    ended.set()
    await task

    # Teardown ran after disconnect, in the documented order.
    assert calls[4:] == [
        "loop_stop",
        "stt_close",
        "tts_cancel",
        "mcp_disconnect",
        "room_disconnect",
        "session_end",
    ]


async def test_agent_session_launches_greet_after_pipeline_start() -> None:
    """Greet-first (Spec 32 A3): turn 0 is kicked off the run() path, after the
    pipeline starts and before run() blocks on disconnect."""
    calls: list[str] = []
    ended = asyncio.Event()

    async def _greet() -> None:
        calls.append("greet")

    agent = AgentSession(
        voice_room=_FakeRoom(calls),  # type: ignore[arg-type]
        loop=_FakeLoop(calls),  # type: ignore[arg-type]
        stt_seam=_FakeSttSeam(calls),  # type: ignore[arg-type]
        tts_seam=_FakeTtsSeam(calls),
        session=_FakeSessionMachine(calls),  # type: ignore[arg-type]
        mcp_clients=[_FakeMcpClient(calls)],  # type: ignore[list-item]
        livekit_url="ws://localhost:7880",
        agent_token="tok",
        ended=ended,
        greet=_greet,
    )
    task = asyncio.create_task(agent.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if "greet" in calls:
            break

    # Greet ran after the pipeline started (turn 0 once the loop is live).
    assert "greet" in calls
    assert calls.index("greet") > calls.index("start_pipeline")

    ended.set()
    await task


async def test_agent_session_teardown_is_best_effort_when_a_step_raises() -> None:
    calls: list[str] = []
    agent, ended = _make_session(calls, stop_raises=True)

    task = asyncio.create_task(agent.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if "start_pipeline" in calls:
            break
    ended.set()
    await task  # must not raise even though loop.stop() raised

    # Every subsequent teardown step still ran despite loop_stop raising.
    for step in ("loop_stop", "stt_close", "tts_cancel", "mcp_disconnect", "room_disconnect"):
        assert step in calls


async def test_build_agent_session_wires_room_disconnect_to_end() -> None:
    # The room's disconnect handler must end the session and release run()'s
    # awaiter. We assert this at the AgentSession level: the handler the runner
    # installs sets `ended` and ends the session.
    calls: list[str] = []
    agent, ended = _make_session(calls)
    # Simulate what build_agent_session installs: a handler that ends + sets.
    room = agent._voice_room  # noqa: SLF001 — white-box lifecycle assertion

    async def _on_disconnect() -> None:
        await agent._session.end()  # noqa: SLF001
        ended.set()

    room.set_disconnect_handler(_on_disconnect)  # type: ignore[attr-defined]

    task = asyncio.create_task(agent.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if "start_pipeline" in calls:
            break
    # Fire the room disconnect — the user hung up.
    await room.disconnect_handler()  # type: ignore[attr-defined]
    await task
    assert "session_end" in calls


# ---------- Spec V9: the durable call-record is opened + closed (V9-D-5) ------


class _SpyCallRecorder:
    """CallRecorder double recording its open()/close() into the call sequence."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.end_reason: str | None = None

    def open(self, started_at: object = None) -> None:  # noqa: ARG002 — mirror CallRecorder
        self._calls.append("recorder_open")

    def close(self, *, end_reason: str, ended_at: object = None) -> None:  # noqa: ARG002 — mirror
        self.end_reason = end_reason
        self._calls.append("recorder_close")


class _SpyEngine:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def dispose(self) -> None:
        self._calls.append("engine_dispose")


async def test_agent_session_opens_call_record_on_active_and_closes_on_teardown() -> None:
    """The recorder opens the moment the call is genuinely active (right after the
    pipeline starts) and is finalized — with end_reason + its dedicated engine
    disposed — at teardown, AFTER session.end() (V9-D-5)."""
    calls: list[str] = []
    ended = asyncio.Event()
    recorder = _SpyCallRecorder(calls)
    engine = _SpyEngine(calls)
    agent = AgentSession(
        voice_room=_FakeRoom(calls),  # type: ignore[arg-type]
        loop=_FakeLoop(calls),  # type: ignore[arg-type]
        stt_seam=_FakeSttSeam(calls),  # type: ignore[arg-type]
        tts_seam=_FakeTtsSeam(calls),
        session=_FakeSessionMachine(calls),  # type: ignore[arg-type]
        mcp_clients=[_FakeMcpClient(calls)],  # type: ignore[list-item]
        livekit_url="ws://localhost:7880",
        agent_token="tok",
        ended=ended,
        call_recorder=recorder,  # type: ignore[arg-type]
        call_record_engine=engine,  # type: ignore[arg-type]
    )

    task = asyncio.create_task(agent.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if "recorder_open" in calls:
            break

    # open() fires right after the pipeline starts (the call is genuinely active).
    assert calls[:5] == [
        "stt_load",
        "room_connect",
        "mark_active",
        "start_pipeline",
        "recorder_open",
    ]

    ended.set()
    await task

    # close() finalizes AFTER session.end() (the dedicated engine survives the
    # session-engine disposal), with the clean-path reason, then the dedicated
    # engine is disposed last.
    assert calls.index("session_end") < calls.index("recorder_close")
    assert recorder.end_reason == "disconnect"
    assert calls[-1] == "engine_dispose"


async def test_agent_session_records_error_reason_on_crash() -> None:
    """A real crash in run() records end_reason='error' (not the clean
    'disconnect') — the record reflects WHY the call ended (V9-D-5)."""
    calls: list[str] = []
    ended = asyncio.Event()
    recorder = _SpyCallRecorder(calls)
    agent = AgentSession(
        voice_room=_FakeRoom(calls),  # type: ignore[arg-type]
        loop=_FakeLoop(calls, stop_raises=False),  # type: ignore[arg-type]
        stt_seam=_FakeSttSeam(calls),  # type: ignore[arg-type]
        tts_seam=_FakeTtsSeam(calls),
        session=_FakeSessionMachine(calls),  # type: ignore[arg-type]
        mcp_clients=[_FakeMcpClient(calls)],  # type: ignore[list-item]
        livekit_url="ws://localhost:7880",
        agent_token="tok",
        ended=ended,
        call_recorder=recorder,  # type: ignore[arg-type]
    )
    # Make the post-active wait raise a real exception (not CancelledError).
    agent._ended = _RaisingEvent()  # noqa: SLF001 — inject a crash at the await barrier

    with pytest.raises(RuntimeError, match="boom"):
        await agent.run()

    assert recorder.end_reason == "error"


class _RaisingEvent:
    """An asyncio.Event-shaped double whose wait() raises a real exception."""

    async def wait(self) -> bool:
        raise RuntimeError("boom")


# ---------- Spec V8 #3: true-end closes the Deepgram stream (criterion #4) ----


class _SpyDeepgramBackend:
    """StreamingSTT double whose close() records that the billed socket finished."""

    def __init__(self) -> None:
        self.closed = False

    @property
    def provider_name(self) -> str:
        return "deepgram"

    @property
    def model_name(self) -> str:
        return "nova-3"

    async def push_audio(self, pcm: bytes, sample_rate: int) -> None: ...

    async def transcripts(self) -> AsyncIterator[object]:
        return
        yield  # pragma: no cover

    async def speech_activity_events(self) -> AsyncIterator[object]:
        return
        yield  # pragma: no cover

    async def close(self) -> None:
        self.closed = True


class _NullVAD:
    def __init__(self) -> None:
        self.closed = False

    async def load(self) -> None: ...

    async def push_audio(self, pcm: bytes, sample_rate: int) -> None: ...

    async def speech_activity_events(self) -> AsyncIterator[object]:
        return
        yield  # pragma: no cover

    async def close(self) -> None:
        self.closed = True


async def test_true_end_closes_the_deepgram_stream_no_lingering_billed_stream() -> None:
    """Spec V8 #3 / criterion #4 (D-V8-8): on a true call-end (room disconnect —
    the funnel for end / switch / reload-teardown), the runner's teardown closes
    the REAL seam adapter, which finishes the Deepgram socket. A lingering open
    stream would keep billing — this regression pins that it does not.
    """
    from persona_voice.stt.cost_gate import IdleAwareGate
    from persona_voice.stt.seam_adapter import V1STTStreamSeamAdapter

    calls: list[str] = []
    backend = _SpyDeepgramBackend()
    vad = _NullVAD()
    stt_seam = V1STTStreamSeamAdapter(
        backend=backend,  # type: ignore[arg-type]
        vad=vad,  # type: ignore[arg-type]
        gate=IdleAwareGate(),  # source-less ⇒ open; teardown path is what matters here
        reopen_preroll_ms=300.0,
    )
    ended = asyncio.Event()
    room = _FakeRoom(calls)
    agent = AgentSession(
        voice_room=room,  # type: ignore[arg-type]
        loop=_FakeLoop(calls),  # type: ignore[arg-type]
        stt_seam=stt_seam,
        tts_seam=_FakeTtsSeam(calls),
        session=_FakeSessionMachine(calls),  # type: ignore[arg-type]
        mcp_clients=[],
        livekit_url="ws://localhost:7880",
        agent_token="tok",
        ended=ended,
    )

    # The runner installs _on_room_disconnected → session.end() + ended.set().
    async def _on_disconnect() -> None:
        await agent._session.end()  # noqa: SLF001
        ended.set()

    room.set_disconnect_handler(_on_disconnect)

    task = asyncio.create_task(agent.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if "start_pipeline" in calls:
            break

    assert backend.closed is False  # still live mid-call
    # True end: the user hangs up / the call is switched / the page reloads.
    await room.disconnect_handler()  # type: ignore[attr-defined]
    await task

    # The Deepgram socket is finished + the VAD released — no lingering billed stream.
    assert backend.closed is True
    assert vad.closed is True


# ---------- launcher --------------------------------------------------------


async def test_launcher_spawns_runner_with_shared_singletons() -> None:
    received: dict[str, Any] = {}

    async def _fake_runner(**kwargs: object) -> None:
        received.update(kwargs)

    launcher = InProcessAgentLauncher(
        config=object(),  # type: ignore[arg-type]
        runner=_fake_runner,
    )
    # Pre-set the singletons so _ensure_singletons skips the heavy bge/tier build.
    sentinel_embedder = object()
    launcher._embedder = sentinel_embedder  # type: ignore[assignment]  # noqa: SLF001

    class _FakeTierRegistry:
        async def aclose(self) -> None:
            return None

    fake_tier = _FakeTierRegistry()
    launcher._tier_registry = fake_tier  # type: ignore[assignment]  # noqa: SLF001

    launcher.launch(session_id="s1", user_id="u1", persona_id="p1", conversation_id="c1")
    # Drain the spawned task.
    await asyncio.gather(*launcher._tasks)  # noqa: SLF001

    assert received["session_id"] == "s1"
    assert received["user_id"] == "u1"
    assert received["persona_id"] == "p1"
    assert received["conversation_id"] == "c1"
    assert received["embedder"] is sentinel_embedder
    assert received["tier_registry"] is fake_tier


async def test_launcher_isolates_a_failing_session() -> None:
    async def _boom_runner(**_kwargs: object) -> None:
        msg = "agent crashed"
        raise RuntimeError(msg)

    launcher = InProcessAgentLauncher(config=object(), runner=_boom_runner)  # type: ignore[arg-type]
    launcher._embedder = object()  # type: ignore[assignment]  # noqa: SLF001
    launcher._tier_registry = None  # type: ignore[assignment]  # noqa: SLF001

    # _ensure_singletons would try to build a real tier registry; prevent that.
    async def _noop_singletons() -> None:
        return None

    launcher._ensure_singletons = _noop_singletons  # type: ignore[method-assign]  # noqa: SLF001

    launcher.launch(session_id="s1", user_id="u1", persona_id="p1", conversation_id="c1")
    # The guarded task must complete WITHOUT raising into the caller.
    await asyncio.gather(*launcher._tasks)  # noqa: SLF001
    assert launcher._tasks == set()  # noqa: SLF001 — done-callback cleared it


@pytest.mark.asyncio
async def test_warm_starts_the_crisis_encoder_warmup_off_loop() -> None:
    """R6 T8: the REAL voice boot path (launcher.warm) invokes the crisis-encoder warm-up
    off the loop, so the first call never pays the ~40 s cold load (built-but-inert)."""

    class _CrisisStub:
        def __init__(self) -> None:
            self.warmed = 0

        def warmup(self) -> None:
            self.warmed += 1

    launcher = InProcessAgentLauncher(config=object())  # type: ignore[arg-type]
    # Pre-set the heavy singletons so warm() skips the real bge/tier build; the bad
    # sentinel embedder makes start_embedder_warmup's encode fail, which it swallows.
    launcher._embedder = object()  # type: ignore[assignment]  # noqa: SLF001
    launcher._tier_registry = object()  # type: ignore[assignment]  # noqa: SLF001
    stub = _CrisisStub()
    launcher._crisis_encoder = stub  # type: ignore[assignment]  # noqa: SLF001

    await launcher.warm()

    assert launcher._crisis_warmup is not None  # noqa: SLF001 — boot started the off-loop warm
    await launcher._crisis_warmup  # noqa: SLF001 — let the background warm complete
    assert stub.warmed == 1  # the real boot path invoked warmup()
