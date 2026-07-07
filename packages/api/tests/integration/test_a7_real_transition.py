"""A7 T8 — the real-transition proof: a real inbound fires the leg ≥2× (criterion 3).

The load-bearing "is it wired live" gate — NOTHING is hand-forced. Two real inbound messages run
through the REAL :class:`SharedInboundFlow.handle_text` (the connector convergence point), which
emits ``connector.message_received`` through the REAL dispatcher (``build_event_dispatcher`` +
``make_message_received_emit``, exactly as ``__main__`` wires it); the dispatcher fires the
confirmed task's leg (door-a); the REAL worker runs it; the A7-D-X-event-standing continuation
returns the task to WAITING(on_event); and the SECOND inbound fires it AGAIN — the A4 recurrence
discipline for a standing "whenever X" watch. The only injections are the scripted model backend and
the resolver/transport fakes (the connector's own test seams); the emit, dispatch, leg,
continuation, and worker are all real.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.backends import StreamChunk, TokenUsage
from persona.backends.types import ChatResponse
from persona.events import EventKind, FireTaskLeg, MessageFilter
from persona.jobs import JobRegistry
from persona.schema.chunks import PersonaChunk
from persona.stores.postgres import PostgresBackend
from persona.tasks import Contract, TaskState, WaitKind
from persona_api.config import APIConfig
from persona_api.events import EventTriggerRecord, EventTriggerStore, build_event_dispatcher
from persona_api.events.connector_hooks import make_message_received_emit
from persona_api.jobs import Worker
from persona_api.jobs.queue import JobQueue
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.schedules import ScheduleStore
from persona_api.services.origination_adapters import (
    ScheduleCreatorAdapter,
    TaskCreatorAdapter,
)
from persona_api.services.origination_service import OriginationService
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.handler import register_task_leg_handler
from persona_api.tasks.leg_runner import RuntimeFactoryLegRunnerBuilder
from persona_api.tasks.scheduled_task_builders import build_backing_task
from persona_api.tasks.store import CheckpointStore, TaskStore
from persona_connectors.domain.flow import SharedInboundFlow
from persona_connectors.domain.normalise import NormalisedInbound
from persona_connectors.domain.resolution import ResolvedIdentity
from persona_runtime.task_origination import (
    ModelEventTriggerIntentJudge,
    StandingIntentRecognizer,
    StandingJudgment,
    StandingVerdict,
    build_task_originated_event,
    render_echo,
)
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from persona.schema.conversation import ConversationMessage
    from persona_connectors.domain.flow import TurnRequest
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_AUDIT = Path("/tmp/persona-a7-realtx-audit")
_OWNER = "user_a7_realtx"
_PERSONA = "persona_a7_realtx"
_TASK = "task_a7_realtx"
_SENDER = "landlord@example.com"
_T0 = datetime(2026, 7, 7, 9, 0, tzinfo=UTC)
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
    provider_name = "anthropic"
    model_name = "scripted"
    max_tokens = 4096

    @property
    def supports_native_tools(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, _messages: list[ConversationMessage], **_: object) -> ChatResponse:
        return ChatResponse(
            content="[FINAL] done.",
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )

    async def chat_stream(
        self, _messages: list[ConversationMessage], **_: object
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta="done", is_final=True, usage=TokenUsage(1, 1, 2))


class _Registry:
    def __init__(self) -> None:
        self._b = _LegBackend()

    def get(self, _tier: str) -> _LegBackend:
        return self._b

    @property
    def configured_tier_names(self) -> tuple[str, ...]:
        return ("frontier", "mid", "small")

    def supports_vision_for(self, _tier: str) -> bool:
        return False

    def metadata_for(self, _tier: str) -> None:
        return None

    def model_name_for(self, _tier: str) -> str:
        return "scripted"

    async def aclose(self) -> None:
        pass


class _NullTurnLog:
    def write(self, _log: object) -> None:
        pass


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send_system(self, *, conversation_key: str, text: str) -> None:  # noqa: A002, ARG002
        return

    async def send_persona(self, outbound: object) -> None:
        self.sent.append(outbound)

    @contextlib.asynccontextmanager
    async def typing(self, conversation_key: str) -> AsyncIterator[None]:  # noqa: ARG002
        yield


class _Resolver:
    def resolve(self, _inbound: object) -> ResolvedIdentity:
        return ResolvedIdentity(owner_id=_OWNER)


class _Store:
    def current_foreground(self, **_: object) -> None:
        return None

    def foreground(self, *, persona_id: str, **_: object) -> object:
        from persona_connectors.domain.conversation_model import ForegroundResult

        return ForegroundResult(conversation_id=f"conv_{persona_id}", resumed=False)

    def apply_new(self, **_: object) -> str:
        return "conv_new"


async def _run_turn(_request: TurnRequest) -> str:
    return "On it."


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 — migrations first
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set")
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


@pytest.fixture
def dispatch_engine(migrated_engine: Engine, database_url: str) -> Iterator[Engine]:  # noqa: ARG001
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed(su_url: str, embedder: HashEmbedder384) -> None:
    su = make_rls_engine(su_url.replace("+asyncpg", "+psycopg"))
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": _OWNER, "e": f"{_OWNER}@x"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, :y)"),
            {"p": _PERSONA, "u": _OWNER, "y": _YAML},
        )
    PostgresBackend(engine=su, embedder=embedder).upsert(
        persona_id=_PERSONA,
        store_kind="self_facts",
        chunks=[
            PersonaChunk(id=f"{_PERSONA}::self_facts::0", text="x", metadata={}, created_at=_T0)
        ],
    )
    su.dispose()


def _inbound(when: datetime, mid: str) -> NormalisedInbound:
    return NormalisedInbound(
        platform="email",
        sender_id=_SENDER,
        conversation_key="thread-1",
        message_id=mid,
        text="Please have a look at this.",
        received_at=when,
    )


def _fired_count(su: Engine) -> int:
    with su.begin() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM audit_log WHERE user_id = :u "
                    "AND action = 'event_trigger.fired'"
                ),
                {"u": _OWNER},
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_real_inbound_fires_the_standing_trigger_twice(
    app_engine: Engine,
    dispatch_engine: Engine,
    embedder: HashEmbedder384,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSONA_EVENT_TRIGGERS_ENABLED", "true")
    su_url = os.environ["DATABASE_URL"]
    _seed(su_url, embedder)
    token = current_user_id.set(_OWNER)
    try:
        # A confirmed event-trigger task (WAITING(on_event)) + its registry row watching the sender.
        task_store = TaskStore(app_engine)
        task_store.create(
            build_backing_task(
                task_id=_TASK,
                owner_id=_OWNER,
                persona_id=_PERSONA,
                contract=Contract(goal="summarise the landlord's email"),
                conversation_id=None,
                schedule_id=None,
                now=_T0,
                wait_on_event=True,
            )
        )
        EventTriggerStore(app_engine).create_if_absent(
            EventTriggerRecord(
                id="trg-realtx",
                owner_id=_OWNER,
                persona_id=_PERSONA,
                task_id=_TASK,
                event_kind=EventKind.CONNECTOR_MESSAGE_RECEIVED,
                platform="email",
                filter=MessageFilter(platform="email", sender=_SENDER),
                action=FireTaskLeg(task_id=_TASK),
                enabled=True,
                disabled_reason=None,
                last_fired_at=None,
                pending_coalesced_count=0,
                created_at=_T0,
                updated_at=_T0,
            ),
            now=_T0,
        )

        # The REAL worker (scripted model only) — its task_leg handler + continuation drive the
        # event-standing return to WAITING(on_event).
        factory = RuntimeFactory(
            rls_engine=app_engine,
            embedder=embedder,
            tier_registry=_Registry(),  # type: ignore[arg-type]
            turn_log_writer=_NullTurnLog(),  # type: ignore[arg-type]
            audit_root=_AUDIT,
        )
        registry = JobRegistry()
        register_task_leg_handler(
            registry,
            task_store=TaskStore(app_engine),
            checkpoint_store=CheckpointStore(app_engine),
            runner_builder=RuntimeFactoryLegRunnerBuilder(factory),
            continuation=TaskContinuation(
                task_store=TaskStore(app_engine),
                queue=JobQueue(app_engine),
                checkpoint_store=CheckpointStore(app_engine),
                schedule_store=ScheduleStore(app_engine),
            ),
        )
        worker = Worker(
            dispatch_engine=dispatch_engine,
            rls_engine=app_engine,
            registry=registry,
            worker_id="w-a7-realtx",
        )

        # The REAL flow wired EXACTLY as __main__ does: the emit closes over the real dispatcher.
        dispatcher = build_event_dispatcher(
            rls_engine=app_engine, config=APIConfig(credits_max_per_day=0)
        )
        flow = SharedInboundFlow(
            resolver=_Resolver(),  # type: ignore[arg-type]
            conversation_store=_Store(),  # type: ignore[arg-type]
            list_persona_names=lambda _owner: {_PERSONA: ["Astrid"]},
            run_turn=_run_turn,
            emit_message_received=make_message_received_emit(dispatcher),
        )
        transport = _FakeTransport()

        async def _drain() -> None:
            while (await worker.run_once()) > 0:
                pass

        # FIRE 1: a real inbound from the watched sender → emit → dispatch → leg → complete → wait.
        await flow.handle_text(_inbound(_T0, "m1"), transport=transport)  # type: ignore[arg-type]
        await _drain()
        after_one = task_store.get(_OWNER, _TASK)
        assert after_one.state is TaskState.WAITING
        assert after_one.wait_kind is WaitKind.ON_EVENT  # event-standing: ready for the next event
        assert _fired_count(create_engine(su_url.replace("+asyncpg", "+psycopg"))) == 1

        # FIRE 2: a SECOND real inbound, spaced beyond the cooldown → fires AGAIN (≥2×, the standing
        # discipline; not coalesced away).
        await flow.handle_text(_inbound(_T0 + timedelta(seconds=400), "m2"), transport=transport)  # type: ignore[arg-type]
        await _drain()

        su = create_engine(su_url.replace("+asyncpg", "+psycopg"))
        try:
            assert _fired_count(su) == 2  # the standing trigger fired on BOTH real inbounds
        finally:
            su.dispose()
        final = task_store.get(_OWNER, _TASK)
        assert final.state is TaskState.WAITING
        assert final.wait_kind is WaitKind.ON_EVENT
        assert final.head_checkpoint_seq is not None  # real legs advanced the head
    finally:
        current_user_id.reset(token)
        su = make_rls_engine(su_url.replace("+asyncpg", "+psycopg"))
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": _OWNER})
        su.dispose()


# --- create-via-chat → the same fire chain (sub-task 2) ----------------------------------------

_EVENT_JUDGE_JSON = (
    '{"verdict": "trigger", "goal": "summarise the landlord\'s email", "platform": "email", '
    '"sender": "landlord@example.com", '
    '"human_terms": "an email from landlord@example.com arrives"}'
)


class _EventJudgeBackend:
    """A scripted recognition backend that drafts the message watch from the chat turn."""

    async def chat(self, _messages: list[ConversationMessage], **_: object) -> ChatResponse:
        return ChatResponse(
            content=_EVENT_JUDGE_JSON,
            model="scripted",
            provider="local",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


class _NeverScheduleJudge:
    """The A4 schedule judge — never consulted here (the event branch owns the turn)."""

    async def judge(self, _message: str, *, language: str) -> StandingJudgment:  # noqa: ARG002
        return StandingJudgment(verdict=StandingVerdict.AMBIGUOUS)


class _FakeNotifier:
    async def notify(self, *_: object, **__: object) -> None:
        return


@pytest.mark.asyncio
async def test_create_via_chat_then_real_inbound_fires(
    app_engine: Engine,
    dispatch_engine: Engine,
    embedder: HashEmbedder384,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The create-via-chat door, end-to-end into the proven fire chain (no hand-forced steps).

    A real chat message → the REAL recogniser (event cue → scripted judge) → a trigger-bearing draft
    → the echo renders the concrete "When: whenever …" clause → the confirm builds the real
    ``task_originated`` event → the REAL :class:`OriginationService` mints the task + the registry
    row (the ONLY creation path) → a real inbound fires that freshly-minted row through the same
    dispatcher/worker chain proven above. Nothing is hand-seeded.
    """
    monkeypatch.setenv("PERSONA_EVENT_TRIGGERS_ENABLED", "true")
    su_url = os.environ["DATABASE_URL"]
    _seed(su_url, embedder)
    # The chat conversation the confirm turn lives in (the task FKs conversation_id).
    su_seed = make_rls_engine(su_url.replace("+asyncpg", "+psycopg"))
    with su_seed.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id) VALUES (:c, :u, :p) "
                "ON CONFLICT DO NOTHING"
            ),
            {"c": "conv_chat", "u": _OWNER, "p": _PERSONA},
        )
    su_seed.dispose()
    token = current_user_id.set(_OWNER)
    try:
        # 1) create-via-chat: a REAL message through the recogniser → a trigger draft.
        recognizer = StandingIntentRecognizer(
            _NeverScheduleJudge(),  # type: ignore[arg-type]
            event_judge=ModelEventTriggerIntentJudge(backend=_EventJudgeBackend()),  # type: ignore[arg-type]
        )
        outcome_rec = await recognizer.recognize(
            "when an email from landlord@example.com arrives, summarise it", language="en"
        )
        assert outcome_rec.draft is not None
        assert outcome_rec.draft.trigger is not None  # an event watch, not a schedule
        echo = render_echo(outcome_rec.draft)
        assert "When: whenever an email from landlord@example.com arrives" in echo  # legible clause

        # 2) the confirm → the REAL task_originated event → the REAL origination service mints
        #    the task (WAITING(on_event)) + the registry row. This is the only creation path.
        event = build_task_originated_event(
            draft=outcome_rec.draft,
            owner_id=_OWNER,
            persona_id=_PERSONA,
            persona_name="Astrid",
            conversation_id="conv_chat",
            assistant_message_id="assist-confirm-1",
        )
        origination = OriginationService(
            tasks=TaskCreatorAdapter(TaskStore(app_engine)),
            schedules=ScheduleCreatorAdapter(ScheduleStore(app_engine)),
            notifier=_FakeNotifier(),  # type: ignore[arg-type]
            triggers=EventTriggerStore(app_engine),
        )
        origin_outcome = await origination.originate(event.data)
        created_task_id = origin_outcome.task_id
        task_store = TaskStore(app_engine)
        born = task_store.get(_OWNER, created_task_id)
        assert born.state is TaskState.WAITING
        assert born.wait_kind is WaitKind.ON_EVENT  # event-driven task, born watching
        # the registry row exists and watches the named sender (created ONLY here, criterion 1).
        active = EventTriggerStore(app_engine).list_active(
            _OWNER, EventKind.CONNECTOR_MESSAGE_RECEIVED
        )
        minted = [t for t in active if t.task_id == created_task_id]
        assert len(minted) == 1
        assert isinstance(minted[0].filter, MessageFilter)
        assert minted[0].filter.sender == "landlord@example.com"

        # 3) a REAL inbound fires the freshly-minted row through the proven chain.
        factory = RuntimeFactory(
            rls_engine=app_engine,
            embedder=embedder,
            tier_registry=_Registry(),  # type: ignore[arg-type]
            turn_log_writer=_NullTurnLog(),  # type: ignore[arg-type]
            audit_root=_AUDIT,
        )
        registry = JobRegistry()
        register_task_leg_handler(
            registry,
            task_store=TaskStore(app_engine),
            checkpoint_store=CheckpointStore(app_engine),
            runner_builder=RuntimeFactoryLegRunnerBuilder(factory),
            continuation=TaskContinuation(
                task_store=TaskStore(app_engine),
                queue=JobQueue(app_engine),
                checkpoint_store=CheckpointStore(app_engine),
                schedule_store=ScheduleStore(app_engine),
            ),
        )
        worker = Worker(
            dispatch_engine=dispatch_engine,
            rls_engine=app_engine,
            registry=registry,
            worker_id="w-a7-createchat",
        )
        dispatcher = build_event_dispatcher(
            rls_engine=app_engine, config=APIConfig(credits_max_per_day=0)
        )
        flow = SharedInboundFlow(
            resolver=_Resolver(),  # type: ignore[arg-type]
            conversation_store=_Store(),  # type: ignore[arg-type]
            list_persona_names=lambda _owner: {_PERSONA: ["Astrid"]},
            run_turn=_run_turn,
            emit_message_received=make_message_received_emit(dispatcher),
        )

        async def _drain() -> None:
            while (await worker.run_once()) > 0:
                pass

        await flow.handle_text(_inbound(_T0, "cm1"), transport=_FakeTransport())  # type: ignore[arg-type]
        await _drain()

        after = task_store.get(_OWNER, created_task_id)
        assert after.state is TaskState.WAITING
        assert after.wait_kind is WaitKind.ON_EVENT  # fired, ran, returned to the standing watch
        su = create_engine(su_url.replace("+asyncpg", "+psycopg"))
        try:
            assert _fired_count(su) == 1  # the chat-created row fired on a real inbound
        finally:
            su.dispose()
    finally:
        current_user_id.reset(token)
        su = make_rls_engine(su_url.replace("+asyncpg", "+psycopg"))
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": _OWNER})
        su.dispose()
