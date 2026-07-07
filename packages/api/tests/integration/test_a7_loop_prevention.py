"""A7 T4 — the adversarial self-triggering loop is stopped on the REAL chain (criterion 4).

The load-bearing anti-loop proof — NOTHING is forced (the A4 anti-inertness rule). A real message
fires a confirmed task's leg; the REAL worker runs it; on completion the REAL
:class:`~persona_api.events.LifecycleEmitter` (wired through the real ``on_leg_settled`` hook) emits
a ``task.leg_completed`` event that INHERITS the firing ``EventFire``'s causal chain and re-enters
the REAL dispatcher — the genuine connector→worker→lifecycle→dispatch cycle. A self-referential
trigger (fire this task's leg WHEN this task's leg completes) therefore re-enters carrying its own
id, and the loop guard REFUSES it (``event_trigger.loop_refused``), pre-claim, so it never consumes
the cooldown. The task is RECURRING (re-runnable), so absent the guard the loop is unbounded — the
guard is the only thing that ends it. The only injections are the scripted model backend and the
event clock; the leg, continuation, emitter, dispatcher, queue, and worker are all real.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.backends import StreamChunk, TokenUsage
from persona.backends.types import ChatResponse
from persona.events import (
    ConnectorMessageReceived,
    EventKind,
    FireTaskLeg,
    LifecycleFilter,
    MessageFilter,
)
from persona.jobs import JobRegistry
from persona.schedules import RecurrenceKind, RecurrencePattern
from persona.schema.chunks import PersonaChunk
from persona.stores.postgres import PostgresBackend
from persona_api.events import (
    DispatchDisposition,
    EventDispatcher,
    EventTriggerRecord,
    EventTriggerStore,
    LifecycleEmitter,
)
from persona_api.jobs import Worker
from persona_api.jobs.queue import JobQueue
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.schedules import ScheduleStore
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.services.schedule_create_service import create_user_schedule
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.handler import register_task_leg_handler
from persona_api.tasks.leg_runner import RuntimeFactoryLegRunnerBuilder
from persona_api.tasks.store import CheckpointStore, TaskStore
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from persona.schema.conversation import ConversationMessage
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_AUDIT = Path("/tmp/persona-a7-loop-audit")
_NOW = datetime(2026, 7, 6, 9, 0, tzinfo=UTC)
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
    """A clean one-step completion — the leg finishes each run (so it emits task.leg_completed)."""

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
        self, _messages: list[ConversationMessage], **_: object
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


class _Recorder:
    def __init__(self) -> None:
        self.audits: list[tuple[str, str, str, object]] = []

    def audit(self, owner: str, action: str, target: str, meta: object) -> None:
        self.audits.append((owner, action, target, meta))

    def surface(self, owner: str, trigger_id: str, human: str) -> None:  # noqa: ARG002
        return


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


def _seed(su_url: str, embedder: HashEmbedder384, owner: str, persona_id: str) -> None:
    su = make_rls_engine(su_url.replace("+asyncpg", "+psycopg"))
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
            PersonaChunk(id=f"{persona_id}::self_facts::0", text="x", metadata={}, created_at=_NOW)
        ],
    )
    su.dispose()


def _trigger(
    trigger_id: str,
    owner: str,
    persona: str,
    task_id: str,
    kind: EventKind,
    filt: MessageFilter | LifecycleFilter,
) -> EventTriggerRecord:
    return EventTriggerRecord(
        id=trigger_id,
        owner_id=owner,
        persona_id=persona,
        task_id=task_id,
        event_kind=kind,
        platform=None,
        filter=filt,
        action=FireTaskLeg(task_id=task_id),
        enabled=True,
        disabled_reason=None,
        last_fired_at=None,
        pending_coalesced_count=0,
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.mark.asyncio
async def test_self_triggering_task_does_not_loop_on_the_real_chain(
    app_engine: Engine, dispatch_engine: Engine, embedder: HashEmbedder384
) -> None:
    su_url = os.environ["DATABASE_URL"]
    owner, persona_id = "user_a7_loop", "persona_a7_loop"
    _seed(su_url, embedder, owner, persona_id)
    token = current_user_id.set(owner)
    try:
        # A RECURRING confirmed task — re-runnable, so absent the guard the loop is unbounded.
        created = create_user_schedule(
            app_engine,
            ScheduleStore(app_engine),
            TaskStore(app_engine),
            owner_id=owner,
            pattern=RecurrencePattern(kind=RecurrenceKind.DAILY, hour=6, minute=0),
            one_time_at=None,
            timezone="Europe/Oslo",
            persona_id=persona_id,
            subject="the standing watch",
            idempotency_key="a7-loop-01",
            now=_NOW,
        )
        task_id = created.task_id

        rec = _Recorder()
        dispatcher = EventDispatcher(
            store=EventTriggerStore(app_engine),
            queue=JobQueue(app_engine),
            task_store=TaskStore(app_engine),
            settings_cooldown_seconds=300,
            settings_max_chain_depth=3,
            audit=rec.audit,
            surface_drop=rec.surface,
        )
        emitter = LifecycleEmitter(dispatcher=dispatcher)

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
            on_leg_settled=emitter.on_leg_settled,  # the loop-critical chain inheritance
        )
        worker = Worker(
            dispatch_engine=dispatch_engine,
            rls_engine=app_engine,
            registry=registry,
            worker_id="w-a7-loop",
        )

        store = EventTriggerStore(app_engine)
        # T_msg: a real inbound fires the task's leg (the connector→ leg of the chain).
        store.create_if_absent(
            _trigger(
                "t_msg",
                owner,
                persona_id,
                task_id,
                EventKind.CONNECTOR_MESSAGE_RECEIVED,
                MessageFilter(platform="email"),
            ),
            now=_NOW,
        )
        # T_self: the adversarial self-loop — fire THIS task's leg WHEN its leg completes.
        store.create_if_absent(
            _trigger(
                "t_self",
                owner,
                persona_id,
                task_id,
                EventKind.TASK_LEG_COMPLETED,
                LifecycleFilter(task_id=task_id),
            ),
            now=_NOW,
        )

        # KICK: a real inbound message → the dispatcher fires the first leg (chain = (t_msg,)).
        inbound = ConnectorMessageReceived(
            event_id="evt-inbound",
            owner_id=owner,
            occurred_at=_NOW,
            platform="email",
            sender_id="boss@example.com",
            body="please watch this",
            conversation_id="conv-a7-loop",
            persona_id=persona_id,
            message_id="msg-1",
        )
        kick = dispatcher.dispatch(inbound, now=_NOW)
        assert [o.disposition for o in kick] == [DispatchDisposition.FIRED]

        # DRAIN the real worker: each leg completes → emits task.leg_completed (chain inherited) →
        # re-dispatches. The self-trigger fires ONCE legitimately (not yet in the chain), then is
        # REFUSED on recursion (now in the chain). A hard bound catches a broken guard (no hang).
        legs_ran = 0
        rounds = 0
        while (n := await worker.run_once()) > 0:
            legs_ran += n
            rounds += 1
            assert rounds < 25, "the loop did not terminate — the chain guard failed"

        # The guard — not the task ending — is what stopped it (the task is recurring/re-runnable).
        loop_refusals = [a for a in rec.audits if a[1] == "event_trigger.loop_refused"]
        assert loop_refusals, "the self-trigger was never loop-refused (the guard did not engage)"
        assert loop_refusals[0][2] == "t_self"
        # Finite: the self-trigger fired at most once (the legitimate reaction), then was refused —
        # a bounded number of real legs ran, never an unbounded storm.
        assert 1 <= legs_ran <= 5, f"expected a small finite number of legs, got {legs_ran}"

        # The refusal is pre-claim: it never advanced the cooldown beyond t_self's single real fire.
        refused_dispatch = dispatcher.dispatch(
            ConnectorMessageReceived(
                event_id="evt-probe",
                owner_id=owner,
                occurred_at=_NOW,
                platform="nope",  # matches nothing — just proves the machinery is quiescent
                sender_id="x",
                body="",
                conversation_id="c",
                persona_id=persona_id,
                message_id="m",
            ),
            now=_NOW,
        )
        assert refused_dispatch == []  # no residual triggers firing
    finally:
        current_user_id.reset(token)
        su = make_rls_engine(su_url.replace("+asyncpg", "+psycopg"))
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": owner})
        su.dispose()
