"""A4 tasks actually FIRE (and recur) through the real scheduler chain (Spec A4 schedule-attach).

The anti-inertness gate for recurrence — and the fix for the earlier miss where the composition
gate invoked the milestone hook manually and so never proved a task *fires*. Here NOTHING is forced:

    origination → task WAITING(until_time) → SchedulerTick.run_once → Worker.run_once (claims +
    executes the leg on the identical AgenticLoop) → continuation → milestone → digest

runs end-to-end through the REAL construction (real RuntimeFactory, real compose_task_origination_
services, real build_worker_registry, real SchedulerTick + Worker). The ONLY things injected are the
model backend (scripted — always the injected dependency) and the tick's observer clock ``now`` —
the schedule's ``next_fire_at`` is advanced solely by the real ``apply_fire`` re-arm, never by hand.

Two proofs:
* a **recurring** contract fires ≥2 times on cadence (re-arm → next occurrence → a second REAL
  fire), the task surviving each occurrence (WAITING, never terminal);
* a **one-off** contract fires exactly once then goes terminal, and a later tick fires nothing.
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
from persona.schema.conversation import Conversation
from persona.stores.postgres import PostgresBackend
from persona.tasks import TaskState, is_terminal
from persona_api.config import Edition
from persona_api.jobs import Worker
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.schedules import ScheduleStore
from persona_api.schedules.leadership import SchedulerLeader
from persona_api.schedules.tick import SchedulerTick
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.services.task_origination_composition import compose_task_origination_services
from persona_api.tasks.store import TaskStore
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Mapping

    from persona.schema.conversation import ConversationMessage
    from persona_runtime.agentic.events import RunEvent
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_AUDIT = Path("/tmp/persona-a4-recur-audit")
_LOCK_KEY = 987654
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


class _ScriptedA4Backend:
    """Judge → standing + a configurable cadence; leg AgenticLoop → a clean completion."""

    provider_name = "anthropic"
    model_name = "scripted"
    max_tokens = 4096

    def __init__(self, cadence_json: str) -> None:
        self._cadence = cadence_json  # e.g. '"recurrence_rrule": "FREQ=DAILY;BYHOUR=6;BYMINUTE=0"'

    @property
    def supports_native_tools(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **_: object) -> ChatResponse:
        system = str(messages[0].content)
        # The leg's AgenticLoop calls chat with the persona's agentic prompt (none of the A4
        # markers) → return a [FINAL] answer so the leg COMPLETES in one step (RunStatus.COMPLETED).
        content = "[FINAL] done for this run."
        if "STANDING task" in system:  # the standing-intent judge → standing + the cadence
            content = (
                '{"verdict": "standing", "goal": "track morning fares", ' + self._cadence + "}"
            )
        return ChatResponse(
            content=content, model=self.model_name, provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2), latency_ms=1.0,
        )

    async def chat_stream(
        self,
        messages: list[ConversationMessage],  # noqa: ARG002 — the leg completes on a plain reply
        **_: object,
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(
            delta="done", is_final=True,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


class _ScriptedRegistry:
    def __init__(self, cadence_json: str) -> None:
        self._b = _ScriptedA4Backend(cadence_json)

    def get(self, _tier_name: str) -> _ScriptedA4Backend:
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


def _seed(su_url: str, embedder: HashEmbedder384, owner: str, persona_id: str, conv: str) -> None:
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
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, title) "
                "VALUES (:c, :o, :p, '') ON CONFLICT DO NOTHING"
            ),
            {"c": conv, "o": owner, "p": persona_id},
        )
    backend = PostgresBackend(engine=su, embedder=embedder)
    from persona.schema.chunks import PersonaChunk

    backend.upsert(
        persona_id=persona_id, store_kind="self_facts",
        chunks=[PersonaChunk(id=f"{persona_id}::self_facts::0", text="x", metadata={},
                             created_at=datetime.now(UTC))],
    )
    su.dispose()


def _cleanup(su_url: str, owner: str) -> None:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": owner})
    su.dispose()


async def _drive(loop: object, conv: Conversation, message: str) -> list[RunEvent]:
    events: list[RunEvent] = []

    async def _on_event(event: RunEvent) -> None:
        events.append(event)

    async for _c in loop.turn(conv, message, _on_event):  # type: ignore[attr-defined]
        pass
    return events


def _digests(su_url: str, conv: str) -> list[str]:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT content FROM messages WHERE conversation_id = :c AND role = 'assistant' "
                "AND originated = true ORDER BY created_at"
            ),
            {"c": conv},
        ).all()
    su.dispose()
    return [r[0] for r in rows]


async def _originate_via_real_flow(
    factory: RuntimeFactory, services: object, owner: str, persona_id: str, conv_id: str
) -> str:
    """Drive the REAL recognize→echo→confirm→create flow; return the created task id."""
    loop = await factory.build_conversation_loop(persona_id)
    conv = Conversation(conversation_id=conv_id, persona_id=persona_id, messages=[])
    await _drive(loop, conv, "every morning, track the fares for me")  # → echo (schedule attached)
    confirm = await _drive(loop, conv, "yes")
    originated = [e for e in confirm if e.type == "task_originated"]
    assert len(originated) == 1
    data: Mapping[str, object] = {
        **originated[0].data, "owner_id": owner, "assistant_message_id": "amsg-recur",
    }
    outcome = await services.origination.originate(data)  # type: ignore[attr-defined]
    return outcome.task_id


async def _fire_and_run(tick: SchedulerTick, worker: Worker, *, at: datetime) -> None:
    """One REAL fire: the tick claims the due schedule (observer clock ``at``) + re-arms, then the
    worker drains the resulting jobs — the fire→leg bridge job AND the task leg it enqueues. No
    manual hook, no hand-advanced next_fire_at; the schedule re-arms itself via ``apply_fire``."""
    assert tick.run_once(now=at) == 1, "the scheduler did not fire the due schedule"
    ran = 0
    while (n := await worker.run_once()) > 0:  # drain: the bridge job, then the task leg
        ran += n
    assert ran >= 2, f"expected the fire-bridge job + the task leg to run, got {ran}"


def _build(cadence_json: str, app_engine: Engine, embedder: HashEmbedder384) -> RuntimeFactory:
    return RuntimeFactory(
        rls_engine=app_engine,
        embedder=embedder,
        tier_registry=_ScriptedRegistry(cadence_json),  # type: ignore[arg-type]
        turn_log_writer=_NullTurnLog(),  # type: ignore[arg-type]
        audit_root=_AUDIT,
    )


@pytest.mark.asyncio
async def test_recurring_task_fires_at_least_twice_on_cadence(
    app_engine: Engine, dispatch_engine: Engine, embedder: HashEmbedder384
) -> None:
    from persona_api.background.worker_root import build_worker_registry

    su_url = os.environ["DATABASE_URL"]
    owner, persona_id, conv_id = "user_recur", "persona_recur", "c_recur"
    _seed(su_url, embedder, owner, persona_id, conv_id)
    factory = _build('"recurrence_rrule": "FREQ=DAILY;BYHOUR=6;BYMINUTE=0"', app_engine, embedder)
    memory = PostgresBackend(engine=app_engine, embedder=embedder)
    token = current_user_id.set(owner)
    leader = SchedulerLeader(dispatch_engine, lock_key=_LOCK_KEY)
    try:
        services = compose_task_origination_services(
            rls_engine=app_engine, memory_backend=memory, edition=Edition.cloud, audit_root=_AUDIT
        )
        task_id = await _originate_via_real_flow(factory, services, owner, persona_id, conv_id)

        tasks = TaskStore(app_engine)
        created = tasks.get(owner, task_id)
        assert created.state is TaskState.WAITING  # born dormant, awaiting its first fire
        assert created.schedule_id is not None

        registry = build_worker_registry(
            rls_engine=app_engine, embedder=embedder, tier_registry=_ScriptedRegistry(""),  # type: ignore[arg-type]
            audit_root=_AUDIT, synthesis_tier="small",
            runtime_factory=factory, memory_backend=memory, edition=Edition.cloud,
        )
        tick = SchedulerTick(dispatch_engine=dispatch_engine, rls_engine=app_engine, leader=leader)
        worker = Worker(
            dispatch_engine=dispatch_engine, rls_engine=app_engine, registry=registry, worker_id="w"
        )
        schedules = ScheduleStore(app_engine)

        # Fire 1 — at the first real occurrence the store computed.
        f1 = schedules.get(owner, created.schedule_id).next_fire_at
        assert f1 is not None
        await _fire_and_run(tick, worker, at=f1)
        after1 = tasks.get(owner, task_id)
        assert not is_terminal(after1.state)  # a recurring occurrence must NOT terminate the task
        assert after1.state is TaskState.WAITING  # returned to waiting for the next fire

        # Fire 2 — at the NEXT occurrence the real apply_fire re-armed to (never hand-set).
        f2 = schedules.get(owner, created.schedule_id).next_fire_at
        assert f2 is not None
        assert f2 > f1  # the real re-arm advanced to a strictly later occurrence
        await _fire_and_run(tick, worker, at=f2)
        assert not is_terminal(tasks.get(owner, task_id).state)  # still alive after occurrence 2

        # Two REAL fires → two digest updates, and NEITHER lies "finished" (the task recurs).
        digests = _digests(su_url, conv_id)
        assert len(digests) >= 2
        assert not any("finished the task" in d for d in digests)
        assert any("run it again on schedule" in d for d in digests)
    finally:
        leader.resign()
        current_user_id.reset(token)
        _cleanup(su_url, owner)


@pytest.mark.asyncio
async def test_one_off_task_fires_exactly_once_then_terminal(
    app_engine: Engine, dispatch_engine: Engine, embedder: HashEmbedder384
) -> None:
    from persona_api.background.worker_root import build_worker_registry

    su_url = os.environ["DATABASE_URL"]
    owner, persona_id, conv_id = "user_once", "persona_once", "c_once"
    _seed(su_url, embedder, owner, persona_id, conv_id)
    at_iso = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0).isoformat()
    factory = _build(f'"one_time_at": "{at_iso}"', app_engine, embedder)
    memory = PostgresBackend(engine=app_engine, embedder=embedder)
    token = current_user_id.set(owner)
    leader = SchedulerLeader(dispatch_engine, lock_key=_LOCK_KEY)
    try:
        services = compose_task_origination_services(
            rls_engine=app_engine, memory_backend=memory, edition=Edition.cloud, audit_root=_AUDIT
        )
        task_id = await _originate_via_real_flow(factory, services, owner, persona_id, conv_id)
        tasks = TaskStore(app_engine)
        created = tasks.get(owner, task_id)
        assert created.state is TaskState.WAITING

        registry = build_worker_registry(
            rls_engine=app_engine, embedder=embedder, tier_registry=_ScriptedRegistry(""),  # type: ignore[arg-type]
            audit_root=_AUDIT, synthesis_tier="small",
            runtime_factory=factory, memory_backend=memory, edition=Edition.cloud,
        )
        tick = SchedulerTick(dispatch_engine=dispatch_engine, rls_engine=app_engine, leader=leader)
        worker = Worker(
            dispatch_engine=dispatch_engine, rls_engine=app_engine, registry=registry, worker_id="w"
        )
        schedules = ScheduleStore(app_engine)

        f1 = schedules.get(owner, created.schedule_id).next_fire_at  # type: ignore[arg-type]
        assert f1 is not None
        await _fire_and_run(tick, worker, at=f1)  # type: ignore[arg-type]
        # runs exactly once → terminal
        assert tasks.get(owner, task_id).state is TaskState.COMPLETED

        # A later tick finds nothing due (one-time re-armed to next_fire_at = NULL) → no re-fire.
        assert tick.run_once(now=f1 + timedelta(days=1)) == 0
        digests = _digests(su_url, conv_id)
        assert len(digests) == 1
        assert "finished the task" in digests[0]  # one-off honestly reports completion
    finally:
        leader.resign()
        current_user_id.reset(token)
        _cleanup(su_url, owner)
