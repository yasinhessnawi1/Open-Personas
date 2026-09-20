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
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from persona.calls import calls as _calls
from persona_voice.agent.launcher import InProcessAgentLauncher
from persona_voice.agent.runner import AgentSession
from persona_voice.billing import VoiceExhaustionCutoff
from persona_voice.session.call_record import CallRecorder
from persona_voice.session.call_window import CallBillingWindow
from sqlalchemy import Engine, create_engine, select

pytestmark = [pytest.mark.asyncio]


class _FakeConfig:
    """Minimal launcher config stand-in — only ``is_cloud`` is read by ``_ensure_singletons``
    (Spec M4 T5c: the free-registry build is cloud-gated; ``False`` ⇒ community, no gating)."""

    is_cloud = False


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

    def close(
        self,
        *,
        end_reason: str,
        ended_at: object = None,  # noqa: ARG002 — mirrors CallRecorder.close
        conversation_ended_at: object = None,  # noqa: ARG002 — mirrors CallRecorder.close
    ) -> None:
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


class _SpyRecorderWithDuration:
    """A CallRecorder double whose close() returns a duration (Spec M3, T6b-1/2)."""

    def __init__(self, calls: list[str], duration_s: int) -> None:
        self._calls = calls
        self._duration_s = duration_s

    def open(self, started_at: object = None) -> None:  # noqa: ARG002 — mirror CallRecorder
        self._calls.append("recorder_open")

    def close(
        self,
        *,
        end_reason: str,  # noqa: ARG002 — mirrors CallRecorder.close
        ended_at: object = None,  # noqa: ARG002 — mirrors CallRecorder.close
        conversation_ended_at: object = None,  # noqa: ARG002 — mirrors CallRecorder.close
    ) -> int:
        self._calls.append("recorder_close")
        return self._duration_s


class _SpyBillingMeter:
    """A turn-billing-meter double recording the teardown LiveKit infra tick."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.infra_billed: list[int] = []

    async def bill_call_infra(self, duration_s: int) -> None:
        self.infra_billed.append(duration_s)
        self._calls.append("infra_billed")


async def test_teardown_finalizes_record_then_bills_livekit_infra_tick() -> None:
    """Spec M3 (T6b-1/2): teardown finalizes the durable call-record AND bills the
    LiveKit infra tick (per-min) using the duration close() returns — the same
    teardown a delete_room cutoff reaches via the room-disconnect handler."""
    calls: list[str] = []
    ended = asyncio.Event()
    recorder = _SpyRecorderWithDuration(calls, 180)
    meter = _SpyBillingMeter(calls)
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
        call_record_engine=_SpyEngine(calls),  # type: ignore[arg-type]
        turn_billing_meter=meter,  # type: ignore[arg-type]
    )

    task = asyncio.create_task(agent.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if "recorder_open" in calls:
            break
    ended.set()
    await task

    # The record finalizes FIRST (duration known), then the infra tick bills it.
    assert meter.infra_billed == [180]
    assert calls.index("recorder_close") < calls.index("infra_billed")


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
        config=_FakeConfig(),  # type: ignore[arg-type]
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

    launcher = InProcessAgentLauncher(config=_FakeConfig(), runner=_boom_runner)  # type: ignore[arg-type]
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

    launcher = InProcessAgentLauncher(config=_FakeConfig())  # type: ignore[arg-type]
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


# ---------- R9-202: the bill follows the conversation, not the session -------


class _ParticipantAwareRoom(_FakeRoom):
    """A room double that can deliver a ``participant_disconnected`` event.

    The runner registers its handler through the SAME setter the real
    :class:`~persona_voice.transport.room.VoiceRoom` exposes, so firing it here
    drives the production path rather than reaching past it.
    """

    def __init__(self, calls: list[str]) -> None:
        super().__init__(calls)
        self.participant_left_handler: Any = None

    def set_participant_left_handler(self, handler: Any) -> None:  # noqa: ANN401 — a callback
        self.participant_left_handler = handler


class _WindTheClock:
    """A hand-wound UTC clock shared by the record and the billable window."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _sqlite_calls_engine() -> Engine:
    """An in-memory engine carrying core's ``calls`` view (the real record SQL)."""
    engine = create_engine("sqlite://")
    _calls.metadata.create_all(engine)
    return engine


def _billed_session(
    calls: list[str],
    *,
    clock: _WindTheClock,
    engine: Engine,
    max_billable_s: int,
) -> tuple[AgentSession, asyncio.Event, _ParticipantAwareRoom, _SpyBillingMeter]:
    """An AgentSession with the REAL recorder + REAL billable window and a spy meter."""
    ended = asyncio.Event()
    room = _ParticipantAwareRoom(calls)
    recorder = CallRecorder(
        engine=engine,
        call_id="call_1a5",
        conversation_id="c1",
        persona_id="p1",
        owner_id="u1",
        clock=clock,
    )

    meter = _SpyBillingMeter(calls)
    agent = AgentSession(
        voice_room=room,  # type: ignore[arg-type]
        loop=_FakeLoop(calls),  # type: ignore[arg-type]
        stt_seam=_FakeSttSeam(calls),  # type: ignore[arg-type]
        tts_seam=_FakeTtsSeam(calls),
        session=_FakeSessionMachine(calls),  # type: ignore[arg-type]
        mcp_clients=[],
        livekit_url="ws://localhost:7880",
        agent_token="tok",
        ended=ended,
        call_recorder=recorder,
        call_record_engine=_SpyEngine(calls),  # type: ignore[arg-type]
        turn_billing_meter=meter,  # type: ignore[arg-type]
        billing_window=CallBillingWindow(
            call_id="call_1a5", max_billable_s=max_billable_s, clock=clock
        ),
    )
    return agent, ended, room, meter


async def test_teardown_long_after_the_caller_left_bills_the_conversation() -> None:
    """R9-202, the central one. A session torn down eleven hours after the caller
    hung up must charge for the two minutes they were actually on the call.

    Production billed the whole gap: ``call_1a5`` ran 40,008 seconds of wall clock
    and took 1334 credits ($13.34) off a real person for minutes of conversation.
    Nothing here forces the end state: the room delivers a real
    ``participant_disconnected``, the clock then moves, and the ordinary teardown
    chain does the rest.
    """
    calls: list[str] = []
    clock = _WindTheClock(datetime(2026, 9, 2, 12, 42, 0, tzinfo=UTC))
    engine = _sqlite_calls_engine()
    agent, ended, room, meter = _billed_session(
        calls, clock=clock, engine=engine, max_billable_s=7200
    )

    task = asyncio.create_task(agent.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if "start_pipeline" in calls:
            break

    # Two minutes of conversation, then the caller leaves the room.
    clock.advance(120)
    room.participant_left_handler()
    # The room lingers: a worker drain closes the session eleven hours later.
    clock.advance(40_008 - 120)
    ended.set()
    await task

    # Billed: the conversation. NOT the 40,008 second gap.
    assert meter.infra_billed == [120]
    with engine.begin() as conn:
        row = conn.execute(select(_calls)).one()
    # Stored duration is the conversation too, and the room's real lifetime stays
    # readable as ended_at - started_at.
    assert row.duration_s == 120
    assert row.end_reason == "user_hangup"
    wall_clock_s = (row.ended_at - row.started_at).total_seconds()
    assert wall_clock_s == 40_008


async def test_a_conversation_past_the_ceiling_is_billed_at_the_ceiling() -> None:
    """The belt to the brace: whatever the measured conversation says, one call
    cannot bill past the configured per-call ceiling, and the record keeps the
    measured number."""
    calls: list[str] = []
    clock = _WindTheClock(datetime(2026, 9, 2, 12, 42, 0, tzinfo=UTC))
    engine = _sqlite_calls_engine()
    agent, ended, room, meter = _billed_session(
        calls, clock=clock, engine=engine, max_billable_s=600
    )

    task = asyncio.create_task(agent.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if "start_pipeline" in calls:
            break

    clock.advance(9_000)
    room.participant_left_handler()
    ended.set()
    await task

    assert meter.infra_billed == [600]
    with engine.begin() as conn:
        row = conn.execute(select(_calls)).one()
    assert row.duration_s == 9_000  # the record stays honest about what was measured


async def test_with_no_departure_the_last_committed_turn_ends_the_bill() -> None:
    """The worker-killed shape: no room event ever arrives, so the last committed
    turn is what the charge is measured to."""
    calls: list[str] = []
    clock = _WindTheClock(datetime(2026, 9, 2, 12, 42, 0, tzinfo=UTC))
    engine = _sqlite_calls_engine()
    agent, ended, _room, meter = _billed_session(
        calls, clock=clock, engine=engine, max_billable_s=7200
    )
    window = agent._billing_window  # noqa: SLF001 — the meter feeds this in production
    assert window is not None

    task = asyncio.create_task(agent.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if "start_pipeline" in calls:
            break

    clock.advance(200)
    window.note_turn_committed()  # what VoiceTurnBillingMeter.bill_turn fires
    clock.advance(5_000)
    ended.set()
    await task

    assert meter.infra_billed == [200]
    with engine.begin() as conn:
        row = conn.execute(select(_calls)).one()
    assert row.end_reason == "disconnect"  # nobody was seen to leave


async def test_an_unmeasurable_call_still_falls_back_to_wall_clock() -> None:
    """No departure and no turn: there is nothing better than wall clock, and the
    ceiling is what keeps that from costing a wallet."""
    calls: list[str] = []
    clock = _WindTheClock(datetime(2026, 9, 2, 12, 42, 0, tzinfo=UTC))
    engine = _sqlite_calls_engine()
    agent, ended, _room, meter = _billed_session(
        calls, clock=clock, engine=engine, max_billable_s=7200
    )

    task = asyncio.create_task(agent.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if "start_pipeline" in calls:
            break
    clock.advance(40_008)
    ended.set()
    await task

    assert meter.infra_billed == [7200]


# ---------- R9-203: the record says WHY the call ended -----------------------


async def test_a_drained_worker_records_shutdown() -> None:
    """A redeploy cancels the session task. That is neither a hangup nor a crash,
    and it is the shape that produced the production rows ending at one identical
    second."""
    calls: list[str] = []
    ended = asyncio.Event()
    recorder = _SpyCallRecorder(calls)
    agent = AgentSession(
        voice_room=_FakeRoom(calls),  # type: ignore[arg-type]
        loop=_FakeLoop(calls),  # type: ignore[arg-type]
        stt_seam=_FakeSttSeam(calls),  # type: ignore[arg-type]
        tts_seam=_FakeTtsSeam(calls),
        session=_FakeSessionMachine(calls),  # type: ignore[arg-type]
        mcp_clients=[],
        livekit_url="ws://localhost:7880",
        agent_token="tok",
        ended=ended,
        call_recorder=recorder,  # type: ignore[arg-type]
    )

    task = asyncio.create_task(agent.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if "recorder_open" in calls:
            break
    task.cancel()  # exactly what InProcessAgentLauncher.aclose() does on drain
    with pytest.raises(asyncio.CancelledError):
        await task

    assert recorder.end_reason == "shutdown"


async def test_the_credit_cutoff_records_exhausted() -> None:
    """The cutoff deletes the room, so the resulting disconnect is physically a
    hangup. The record must still say the house ended the call."""
    calls: list[str] = []
    ended = asyncio.Event()
    recorder = _SpyCallRecorder(calls)
    room = _ParticipantAwareRoom(calls)
    agent = AgentSession(
        voice_room=room,  # type: ignore[arg-type]
        loop=_FakeLoop(calls),  # type: ignore[arg-type]
        stt_seam=_FakeSttSeam(calls),  # type: ignore[arg-type]
        tts_seam=_FakeTtsSeam(calls),
        session=_FakeSessionMachine(calls),  # type: ignore[arg-type]
        mcp_clients=[],
        livekit_url="ws://localhost:7880",
        agent_token="tok",
        ended=ended,
        call_recorder=recorder,  # type: ignore[arg-type]
        billing_window=CallBillingWindow(call_id="call_x", max_billable_s=7200),
    )

    task = asyncio.create_task(agent.run())
    for _ in range(20):
        await asyncio.sleep(0)
        if "recorder_open" in calls:
            break
    # The cutoff fires, then deletes the room; both parties drop.
    await VoiceExhaustionCutoff(
        delete_room=_noop_delete,
        on_triggered=lambda: agent.mark_end_reason("exhausted"),
    ).trigger()
    room.participant_left_handler()
    ended.set()
    await task

    # The caller did leave, but the credit cutoff is the real cause and it spoke first.
    assert recorder.end_reason == "exhausted"


async def _noop_delete() -> None:
    """A delete_room double for the cutoff."""
