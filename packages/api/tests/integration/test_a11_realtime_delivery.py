"""Spec A11 T4 — the live-delivery proof (criterion 1 + 3), the A4 anti-inertness bar.

A scheduled task fires through the REAL chain — the scheduler tick claims + re-arms,
the worker drains the fire-bridge job then the task leg on the identical AgenticLoop,
the leg's completion publishes a digest through the real C0 sender — and, with an SSE
stream **already open and untouched**, the digest **appears live** on that stream as a
``message.delivered`` event, with **NO reload and NO hand-called publish**. This is
exactly the R4-C1-23 scenario (a background delivery that used to require a refresh).

The fan-out is wired the production way: ``build_worker_registry(live_sessions=…)`` →
the digest sender's ``WebAppDeliverer`` consults the channel-backed
``ChannelLiveSessions``; the recorder persists the message BEFORE ``deliver`` runs, so
the live event is emitted AFTER the durable commit (the client's refetch always finds
the row). The real tick/worker/leg harness mirrors ``test_a10_real_fire`` (self-
contained here — pytest test modules aren't importable across files at runtime).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from persona.backends import StreamChunk, TokenUsage
from persona.backends.types import ChatResponse
from persona.schema.chunks import PersonaChunk
from persona.stores.postgres import PostgresBackend
from persona_api.background.run_worker import RunRegistry
from persona_api.background.worker_root import build_worker_registry
from persona_api.config import APIConfig, Edition
from persona_api.jobs import Worker
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.realtime.channel import UserEventChannel
from persona_api.realtime.live_sessions import ChannelLiveSessions
from persona_api.realtime.stream import stream_user_events
from persona_api.schedules import ScheduleStore
from persona_api.schedules.leadership import SchedulerLeader
from persona_api.schedules.tick import SchedulerTick
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.services.schedule_create_service import ScheduleCreateResult, create_user_schedule
from persona_api.tasks.store import TaskStore
from persona_runtime.agentic.run import RunStatus
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from persona.schema.conversation import ConversationMessage
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_AUDIT = Path("/tmp/persona-a11-fire-audit")
_A11_LOCK_KEY = 110811  # distinct from A10's, so a concurrent A10 run never contends
_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: |
    A helper.
  language_default: en
  constraints: []
self_facts:
  - fact: knows things
    confidence: 1.0
"""


class _LegBackend:
    """The leg's AgenticLoop backend — a clean one-step completion, nothing else scripted."""

    provider_name = "anthropic"
    model_name = "scripted"
    max_tokens = 4096

    @property
    def supports_native_tools(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **_: object) -> ChatResponse:  # noqa: ARG002
        return ChatResponse(
            content="[FINAL] done for this run.",
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )

    async def chat_stream(
        self,
        messages: list[ConversationMessage],  # noqa: ARG002 — scripted stub, unused
        **_: object,
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(
            delta="done",
            is_final=True,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


class _Registry:
    def __init__(self) -> None:
        self._b = _LegBackend()

    def get(self, _tier_name: str) -> _LegBackend:
        return self._b

    @property
    def configured_tier_names(self) -> tuple[str, ...]:
        return ("frontier", "mid", "small")

    def supports_vision_for(self, _tier_name: str) -> bool:
        return False

    def metadata_for(self, _tier_name: str) -> None:
        return None

    def model_name_for(self, _tier_name: str) -> str:
        return "scripted"

    async def aclose(self) -> None:
        pass


class _NullTurnLog:
    def write(self, _log: object) -> None:
        pass


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 — migrations first
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set")
    engine = make_rls_engine(app_url)
    yield engine
    engine.dispose()


@pytest.fixture
def dispatch_engine(migrated_engine: Engine, database_url: str) -> Iterator[Engine]:  # noqa: ARG001
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed(su_url: str, embedder: HashEmbedder384, owner: str, persona_id: str) -> None:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": owner, "e": f"{owner}@x"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:i, :o, :y)"),
            {"i": persona_id, "o": owner, "y": _YAML},
        )
    backend = PostgresBackend(engine=su, embedder=embedder)
    backend.upsert(
        persona_id=persona_id,
        store_kind="self_facts",
        chunks=[
            PersonaChunk(
                id=f"{persona_id}::self_facts::0",
                text="x",
                metadata={},
                created_at=datetime.now(UTC),
            )
        ],
    )
    su.dispose()


def _cleanup(su_url: str, owner: str) -> None:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": owner})
    su.dispose()


def _delete_persona(su_url: str, persona_id: str) -> None:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        conn.execute(text("DELETE FROM personas WHERE id = :i"), {"i": persona_id})
    su.dispose()


def _create_via_door(
    app_engine: Engine, *, owner: str, persona_id: str, one_time_at: datetime, key: str
) -> ScheduleCreateResult:
    return create_user_schedule(
        app_engine,
        ScheduleStore(app_engine),
        TaskStore(app_engine),
        owner_id=owner,
        pattern=None,
        one_time_at=one_time_at,
        timezone="Europe/Oslo",
        persona_id=persona_id,
        subject="the morning check",
        idempotency_key=key,
        now=datetime.now(UTC),
    )


def _build_fire_machinery(
    app_engine: Engine,
    dispatch_engine: Engine,
    embedder: HashEmbedder384,
    channel: UserEventChannel,
) -> tuple[SchedulerTick, Worker, SchedulerLeader]:
    """The A10 machinery, wired with the A11 channel-backed live-session registry."""
    factory = RuntimeFactory(
        rls_engine=app_engine,
        embedder=embedder,
        tier_registry=_Registry(),  # type: ignore[arg-type]
        turn_log_writer=_NullTurnLog(),  # type: ignore[arg-type]
        audit_root=_AUDIT,
    )
    memory = PostgresBackend(engine=app_engine, embedder=embedder)
    registry = build_worker_registry(
        rls_engine=app_engine,
        embedder=embedder,
        tier_registry=_Registry(),  # type: ignore[arg-type]
        free_tier_registry=None,  # R9-096: no plans here — gating off, stated
        config=APIConfig(audit_root=str(_AUDIT)),
        synthesis_tier="small",
        runtime_factory=factory,
        memory_backend=memory,
        edition=Edition.cloud,
        live_sessions=ChannelLiveSessions(channel),  # message.delivered (T4)
        event_channel=channel,  # notification.created — the bell ping (T4b)
    )
    leader = SchedulerLeader(dispatch_engine, lock_key=_A11_LOCK_KEY)
    tick = SchedulerTick(dispatch_engine=dispatch_engine, rls_engine=app_engine, leader=leader)
    worker = Worker(
        dispatch_engine=dispatch_engine, rls_engine=app_engine, registry=registry, worker_id="w-a11"
    )
    return tick, worker, leader


async def _fire_and_run(tick: SchedulerTick, worker: Worker, *, at: datetime) -> None:
    """One REAL fire: the tick claims the due schedule + re-arms, the worker drains the jobs."""
    assert tick.run_once(now=at) == 1, "the scheduler did not fire the due schedule"
    ran = 0
    while (n := await worker.run_once()) > 0:  # drain: the bridge job, then the task leg
        ran += n
    assert ran >= 2, f"expected the fire-bridge job + the task leg to run, got {ran}"


async def _next_event(gen: AsyncIterator[bytes], *, name: str, timeout: float = 5.0) -> bytes:
    """Pull frames off an SSE stream until one carries ``event: {name}`` (or time out)."""
    marker = f"event: {name}\n".encode()

    async def _pump() -> bytes:
        async for frame in gen:
            if marker in frame:
                return frame
        msg = f"stream ended before an {name} event"
        raise AssertionError(msg)

    return await asyncio.wait_for(_pump(), timeout=timeout)


@pytest.mark.asyncio
async def test_scheduled_fire_delivers_message_live_over_the_channel_no_reload(
    app_engine: Engine, dispatch_engine: Engine, embedder: HashEmbedder384
) -> None:
    """Criterion 1 + 3: a REAL fire surfaces live on an open stream (no reload, no
    hand-called publish); a second tenant's stream never sees it (non-vacuous RLS)."""
    su_url = os.environ["DATABASE_URL"]
    owner_a, persona_a = "user_a11_live", "persona_a11_live"
    owner_b = "user_a11_other"
    _seed(su_url, embedder, owner_a, persona_a)
    _seed(su_url, embedder, owner_b, "persona_a11_other")

    channel = UserEventChannel(epoch="a11fire")
    token = current_user_id.set(owner_a)
    tick, worker, leader = _build_fire_machinery(app_engine, dispatch_engine, embedder, channel)

    # The pages are ALREADY OPEN and untouched — two tenants, two live streams on the
    # one channel, opened BEFORE the fire (each drains its opening `ready`).
    gen_a = stream_user_events(channel, owner_a, None, heartbeat_seconds=0.1)
    gen_b = stream_user_events(channel, owner_b, None, heartbeat_seconds=0.1)
    try:
        assert b"event: ready\n" in await gen_a.__anext__()
        assert b"event: ready\n" in await gen_b.__anext__()

        created = _create_via_door(
            app_engine,
            owner=owner_a,
            persona_id=persona_a,
            one_time_at=datetime.now(UTC) + timedelta(hours=1),
            key="a11-live-fire",
        )
        f1 = ScheduleStore(app_engine).get(owner_a, created.schedule_id).next_fire_at
        assert f1 is not None

        # THE REAL FIRE — tick claims + re-arms, the worker drains the bridge job then
        # the task leg; the leg's completion delivers the digest through the real C0
        # sender, which publishes message.delivered onto owner A's open stream. Nothing
        # in this test calls channel.publish().
        await _fire_and_run(tick, worker, at=f1)

        # Owner A's ALREADY-OPEN stream receives it live — no reload.
        frame = await _next_event(gen_a, name="message.delivered")
        assert b'"persona_id"' in frame

        # Non-vacuous RLS (criterion 3): owner B's stream sees only heartbeats — the
        # A-scoped delivery never crosses to B (no message.delivered within the window).
        with pytest.raises(asyncio.TimeoutError):
            await _next_event(gen_b, name="message.delivered", timeout=0.5)
    finally:
        leader.resign()
        current_user_id.reset(token)
        await gen_a.aclose()
        await gen_b.aclose()
        _cleanup(su_url, owner_a)
        _cleanup(su_url, owner_b)


async def _fire_and_drain(tick: SchedulerTick, worker: Worker, *, at: datetime) -> None:
    """One REAL fire, drained without asserting a leg ran (the degrade path runs no leg)."""
    assert tick.run_once(now=at) == 1, "the scheduler did not fire the due schedule"
    while await worker.run_once() > 0:
        pass


@pytest.mark.asyncio
async def test_deleted_executor_fire_pings_the_bell_live_no_reload(
    app_engine: Engine, dispatch_engine: Engine, embedder: HashEmbedder384
) -> None:
    """Criterion 2: a REAL fire whose executor persona was deleted degrades to a durable
    ``schedule_executor_missing`` notification (A10-D-7) that ALSO surfaces live on the
    open stream as ``notification.created`` — no reload, no hand-called publish."""
    su_url = os.environ["DATABASE_URL"]
    owner, persona_id = "user_a11_degrade", "persona_a11_degrade"
    _seed(su_url, embedder, owner, persona_id)

    channel = UserEventChannel(epoch="a11degrade")
    token = current_user_id.set(owner)
    tick, worker, leader = _build_fire_machinery(app_engine, dispatch_engine, embedder, channel)
    gen = stream_user_events(channel, owner, None, heartbeat_seconds=0.1)
    try:
        assert b"event: ready\n" in await gen.__anext__()
        created = _create_via_door(
            app_engine,
            owner=owner,
            persona_id=persona_id,
            one_time_at=datetime.now(UTC) + timedelta(hours=1),
            key="a11-degrade-fire",
        )
        f1 = ScheduleStore(app_engine).get(owner, created.schedule_id).next_fire_at
        assert f1 is not None
        # Delete the executor persona → the task cascades away → the fire degrades.
        _delete_persona(su_url, persona_id)

        await _fire_and_drain(tick, worker, at=f1)

        # The durable executor-missing notification pings the open bell live.
        frame = await _next_event(gen, name="notification.created")
        assert b'"kind": "schedule_executor_missing"' in frame
        assert created.schedule_id.encode() in frame  # ref_id = the schedule
    finally:
        leader.resign()
        current_user_id.reset(token)
        await gen.aclose()
        _cleanup(su_url, owner)


@pytest.mark.asyncio
async def test_run_terminal_notification_surfaces_live_no_reload(
    migrated_engine: Engine,  # noqa: ARG001 — ensures migrations before the seed
) -> None:
    """Criterion 2: a completed run's ``run_terminal`` notification (P6) surfaces live on
    the open stream as ``notification.created`` — driven through the REAL persist
    chokepoint the worker calls (``_persist_final``), no hand-called publish."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    owner, persona_id, run_id = "user_a11_run", "persona_a11_run", "run_a11_1"
    su_url = os.environ["DATABASE_URL"]
    su = make_rls_engine(su_url)
    persona_yaml = "identity:\n  name: Ada"
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"), {"u": owner, "e": f"{owner}@x"}
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, :y)"),
            {"p": persona_id, "o": owner, "y": persona_yaml},
        )
        conn.execute(
            text(
                "INSERT INTO runs (id, owner_id, persona_id, task, status) "
                "VALUES (:r, :o, :p, 'do a thing', 'running')"
            ),
            {"r": run_id, "o": owner, "p": persona_id},
        )

    channel = UserEventChannel(epoch="a11run")
    engine = make_rls_engine(app_url)
    token = current_user_id.set(owner)
    reg = RunRegistry(engine, event_channel=channel)
    gen = stream_user_events(channel, owner, None, heartbeat_seconds=0.1)
    try:
        assert b"event: ready\n" in await gen.__anext__()
        run = SimpleNamespace(
            status=RunStatus.COMPLETED, steps=[], output="done", error=None, finished_at=None
        )
        # Drive the REAL terminal chokepoint (the exact call the run worker makes on
        # completion) — the write commits, then notification.created is published.
        reg._persist_final(run_id, run)  # type: ignore[arg-type]  # noqa: SLF001

        frame = await _next_event(gen, name="notification.created")
        assert b'"kind": "run_terminal"' in frame
        assert run_id.encode() in frame  # ref_id = the run
    finally:
        current_user_id.reset(token)
        await gen.aclose()
        engine.dispose()
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": owner})
        su.dispose()
