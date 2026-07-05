"""A10 T3 — a USER-created schedule fires through the REAL chain (the A4 bar, load-bearing).

The anti-inertness gate for the create door: NOTHING is forced —

    create_user_schedule (the T1 door) → task WAITING(until_time) → SchedulerTick.run_once →
    Worker.run_once (the fire-bridge job, then the task leg on the identical AgenticLoop) →
    continuation → milestone digest, delivered in the EXECUTOR persona's voice

runs end-to-end through the real construction (real RuntimeFactory, real
build_worker_registry, real SchedulerTick + Worker). The ONLY injections are the scripted
model backend and the tick's observer clock; ``next_fire_at`` advances solely via the real
``apply_fire`` re-arm. Proves: recurring fires ≥2 on cadence; a one-time fires exactly once
and a later tick fires zero; the created next-fire equals an independent
``occurrences_between`` walk and appears in the occurrences READ; and the deleted-executor
degrade (A10-D-7) — pause + durable P6 notification, never a system-voiced fire, never a
tick/worker crash.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.backends import StreamChunk, TokenUsage
from persona.backends.types import ChatResponse
from persona.schedules import RecurrenceKind, RecurrencePattern, occurrences_between
from persona.schema.chunks import PersonaChunk
from persona.stores.postgres import PostgresBackend
from persona.tasks import TaskState, is_terminal
from persona_api.config import APIConfig, Edition
from persona_api.jobs import Worker
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.schedules import ScheduleStore
from persona_api.schedules.leadership import SchedulerLeader
from persona_api.schedules.tick import SchedulerTick
from persona_api.services import occurrences_service
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.services.schedule_create_service import (
    ScheduleCreateResult,
    create_user_schedule,
)
from persona_api.tasks.store import TaskStore
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from persona.schema.conversation import ConversationMessage
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_AUDIT = Path("/tmp/persona-a10-fire-audit")
_LOCK_KEY = 108108
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
        messages: list[ConversationMessage],  # noqa: ARG002
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


def _originated_messages(su_url: str, owner: str) -> list[str]:
    """Every persona-originated message the owner ever received (any conversation)."""
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT m.content FROM messages m "
                "JOIN conversations c ON m.conversation_id = c.id "
                "WHERE c.owner_id = :o AND m.role = 'assistant' AND m.originated = true "
                "ORDER BY m.created_at"
            ),
            {"o": owner},
        ).all()
    su.dispose()
    return [r[0] for r in rows]


def _notifications(su_url: str, owner: str) -> list[dict[str, object]]:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        rows = (
            conn.execute(
                text("SELECT kind, ref_id, level, params FROM notifications WHERE owner_id = :o"),
                {"o": owner},
            )
            .mappings()
            .all()
        )
    su.dispose()
    return [dict(r) for r in rows]


def _create_via_door(
    app_engine: Engine,
    *,
    owner: str,
    persona_id: str,
    pattern: RecurrencePattern | None = None,
    one_time_at: datetime | None = None,
    key: str = "dialog-fire-01",
) -> ScheduleCreateResult:
    return create_user_schedule(
        app_engine,
        ScheduleStore(app_engine),
        TaskStore(app_engine),
        owner_id=owner,
        pattern=pattern,
        one_time_at=one_time_at,
        timezone="Europe/Oslo",
        persona_id=persona_id,
        subject="the morning check",
        idempotency_key=key,
        now=datetime.now(UTC),
    )


def _build_machinery(
    app_engine: Engine, dispatch_engine: Engine, embedder: HashEmbedder384
) -> tuple[SchedulerTick, Worker, SchedulerLeader]:
    from persona_api.background.worker_root import build_worker_registry

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
        config=APIConfig(audit_root=str(_AUDIT)),
        synthesis_tier="small",
        runtime_factory=factory,
        memory_backend=memory,
        edition=Edition.cloud,
    )
    leader = SchedulerLeader(dispatch_engine, lock_key=_LOCK_KEY)
    tick = SchedulerTick(dispatch_engine=dispatch_engine, rls_engine=app_engine, leader=leader)
    worker = Worker(
        dispatch_engine=dispatch_engine, rls_engine=app_engine, registry=registry, worker_id="w"
    )
    return tick, worker, leader


async def _fire_and_run(tick: SchedulerTick, worker: Worker, *, at: datetime) -> None:
    """One REAL fire: the tick claims the due schedule + re-arms, the worker drains the jobs."""
    assert tick.run_once(now=at) == 1, "the scheduler did not fire the due schedule"
    ran = 0
    while (n := await worker.run_once()) > 0:  # drain: the bridge job, then the task leg
        ran += n
    assert ran >= 2, f"expected the fire-bridge job + the task leg to run, got {ran}"


@pytest.mark.asyncio
async def test_user_created_recurring_schedule_fires_twice_through_the_real_chain(
    app_engine: Engine, dispatch_engine: Engine, embedder: HashEmbedder384
) -> None:
    su_url = os.environ["DATABASE_URL"]
    owner, persona_id = "user_a10_recur", "persona_a10_recur"
    _seed(su_url, embedder, owner, persona_id)
    token = current_user_id.set(owner)
    tick, worker, leader = _build_machinery(app_engine, dispatch_engine, embedder)
    try:
        created = _create_via_door(
            app_engine,
            owner=owner,
            persona_id=persona_id,
            pattern=RecurrencePattern(kind=RecurrenceKind.DAILY, hour=6, minute=0),
        )
        tasks, schedules = TaskStore(app_engine), ScheduleStore(app_engine)
        assert tasks.get(owner, created.task_id).state is TaskState.WAITING

        # Bar 3a: the stored next-fire equals an independent forward-expansion walk.
        stored = schedules.get(owner, created.schedule_id)
        assert stored.next_fire_at is not None
        walk = occurrences_between(
            stored, stored.created_at, stored.created_at + timedelta(days=3), cap=10
        )
        assert stored.next_fire_at == walk[0]

        # Bar 3b: the new occurrence appears in the occurrences READ (the calendar's data).
        read = occurrences_service.list_occurrences(
            app_engine,
            owner_id=owner,
            from_=stored.created_at,
            to=stored.created_at + timedelta(days=3),
            config=APIConfig(audit_root=str(_AUDIT)),
        )
        mine = [o for o in read.occurrences if o.schedule_id == created.schedule_id]
        assert mine, "the created schedule must appear in the calendar read immediately"
        assert mine[0].fire_at == stored.next_fire_at
        assert mine[0].task_id == created.task_id

        # Bar 1: fire 1 at the first real occurrence; the task survives (recurs).
        f1 = stored.next_fire_at
        await _fire_and_run(tick, worker, at=f1)
        after1 = tasks.get(owner, created.task_id)
        assert not is_terminal(after1.state)
        assert after1.state is TaskState.WAITING

        # Fire 2 — at the occurrence the REAL apply_fire re-armed to (never hand-set).
        f2 = schedules.get(owner, created.schedule_id).next_fire_at
        assert f2 is not None
        assert f2 > f1
        await _fire_and_run(tick, worker, at=f2)
        assert not is_terminal(tasks.get(owner, created.task_id).state)

        # The EXECUTOR persona delivered both updates (originated, name-tagged), honestly.
        digests = _originated_messages(su_url, owner)
        assert len(digests) >= 2
        assert not any("finished the task" in d for d in digests)
        assert any("run it again on schedule" in d for d in digests)
    finally:
        leader.resign()
        current_user_id.reset(token)
        _cleanup(su_url, owner)


@pytest.mark.asyncio
async def test_user_created_one_time_fires_exactly_once(
    app_engine: Engine, dispatch_engine: Engine, embedder: HashEmbedder384
) -> None:
    su_url = os.environ["DATABASE_URL"]
    owner, persona_id = "user_a10_once", "persona_a10_once"
    _seed(su_url, embedder, owner, persona_id)
    token = current_user_id.set(owner)
    tick, worker, leader = _build_machinery(app_engine, dispatch_engine, embedder)
    try:
        created = _create_via_door(
            app_engine,
            owner=owner,
            persona_id=persona_id,
            one_time_at=datetime.now(UTC) + timedelta(hours=1),
            key="dialog-fire-02",
        )
        tasks, schedules = TaskStore(app_engine), ScheduleStore(app_engine)
        f1 = schedules.get(owner, created.schedule_id).next_fire_at
        assert f1 is not None

        await _fire_and_run(tick, worker, at=f1)
        assert tasks.get(owner, created.task_id).state is TaskState.COMPLETED

        # Bar 2: a later tick fires it ZERO more times (one-time → next_fire_at NULL).
        assert tick.run_once(now=f1 + timedelta(days=1)) == 0
        digests = _originated_messages(su_url, owner)
        assert len(digests) == 1
        assert "finished the task" in digests[0]
    finally:
        leader.resign()
        current_user_id.reset(token)
        _cleanup(su_url, owner)


@pytest.mark.asyncio
async def test_deleted_executor_degrades_to_pause_plus_notification_never_a_crash(
    app_engine: Engine, dispatch_engine: Engine, embedder: HashEmbedder384
) -> None:
    """A10-D-7: the persona is deleted AFTER create; the fire must degrade honestly.

    Pause (the existing audited door) + a durable P6 notification; NO originated message in
    anyone's voice (the persona-ness guarantee); the tick and worker complete normally; a
    later tick fires nothing (paused).
    """
    su_url = os.environ["DATABASE_URL"]
    owner, persona_id = "user_a10_gone", "persona_a10_gone"
    _seed(su_url, embedder, owner, persona_id)
    token = current_user_id.set(owner)
    tick, worker, leader = _build_machinery(app_engine, dispatch_engine, embedder)
    try:
        created = _create_via_door(
            app_engine,
            owner=owner,
            persona_id=persona_id,
            pattern=RecurrencePattern(kind=RecurrenceKind.DAILY, hour=6, minute=0),
            key="dialog-fire-03",
        )
        schedules = ScheduleStore(app_engine)
        f1 = schedules.get(owner, created.schedule_id).next_fire_at
        assert f1 is not None

        _delete_persona(su_url, persona_id)  # the executor vanishes between create and fire

        # The fire is claimed and the worker drains WITHOUT crashing.
        assert tick.run_once(now=f1) == 1
        while await worker.run_once() > 0:
            pass

        after = schedules.get(owner, created.schedule_id)
        assert after.paused is True  # the ratified degrade: paused, not firing into the void
        notes = _notifications(su_url, owner)
        kinds = {n["kind"] for n in notes}
        assert "schedule_executor_missing" in kinds  # durable bell entry (P6)
        assert _originated_messages(su_url, owner) == []  # NEVER a system-voiced fire

        # A later tick finds nothing due (paused schedules are skipped).
        assert tick.run_once(now=f1 + timedelta(days=1)) == 0
    finally:
        leader.resign()
        current_user_id.reset(token)
        _cleanup(su_url, owner)
