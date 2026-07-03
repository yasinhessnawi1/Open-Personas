"""A8 (close-out) — the conversational reschedule flows through the REAL composition root.

The anti-inertness gate for T6's last seam (the one a manual operator pass would leave unproven):
a real chat turn drives cue → re-echo → confirm → ``RunEvent.task_rescheduled`` → the real
``TaskRescheduleService`` → the CAS door → and the next REAL scheduler tick fires at the NEW time.
The full chain is exercised through the actual ``RuntimeFactory.build_conversation_loop`` wiring
(which composes the reschedule interpreter + the tz/quiet-hours providers) — only the model backend
is scripted. No hand-invoked step; the fire is a real ``SchedulerTick.run_once``.
"""

# ruff: noqa: ARG001, SLF001
from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.schema.conversation import Conversation
from persona.stores.postgres import PostgresBackend
from persona_api.config import Edition
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.schedules import SchedulerLeader, SchedulerTick, ScheduleStore
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.services.task_origination_composition import compose_task_origination_services
from persona_api.services.task_reschedule_service import TaskRescheduleService
from persona_api.tasks.store import TaskStore
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends import StreamChunk
    from persona.schema.conversation import ConversationMessage
    from persona_runtime.agentic.events import RunEvent
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_AUDIT = Path("/tmp/persona-a8-resched-audit")
_LOCK = 0x5C4EDA
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


class _ScriptedBackend:
    """Answers the model steps by prompt-type: standing-create (recurring) + reschedule."""

    provider_name = "anthropic"
    model_name = "scripted"
    max_tokens = 4096

    @property
    def supports_native_tools(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **_: object) -> ChatResponse:
        content = self._answer(str(messages[0].content), str(messages[-1].content))
        return ChatResponse(
            content=content,
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )

    @staticmethod
    def _answer(system: str, user: str) -> str:
        if "RESCHEDULE" in system:  # the reschedule interpreter — target the listed task, 08→09
            match = re.search(r"id=(\S+?):", user)
            task_id = match.group(1) if match else ""
            return json.dumps(
                {
                    "match": "one",
                    "task_id": task_id,
                    "recurrence_rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
                }
            )
        if "STEER" in system:
            return '{"verb": null}'
        if "ADJUSTS the proposal" in system:
            return '{"amends": false}'
        if "STANDING task" in system:  # create a recurring (daily 08:00) schedule-backed task
            return json.dumps(
                {
                    "verdict": "standing",
                    "goal": "the morning brief",
                    "recurrence_rrule": "FREQ=DAILY;BYHOUR=8;BYMINUTE=0",
                }
            )
        return "{}"

    async def chat_stream(
        self,
        messages: list[ConversationMessage],  # noqa: ARG002 — ordinary generation isn't reached
        **_: object,
    ) -> AsyncIterator[StreamChunk]:
        from persona.backends import StreamChunk

        yield StreamChunk(
            delta="ok",
            is_final=True,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


class _ScriptedRegistry:
    def __init__(self) -> None:
        self._b = _ScriptedBackend()

    def get(self, _tier: str) -> _ScriptedBackend:
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


def _seed(su_url: str, embedder: object, owner: str, persona_id: str, conv_id: str) -> None:
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
            {"c": conv_id, "o": owner, "p": persona_id},
        )
    PostgresBackend(engine=su, embedder=embedder).upsert(  # type: ignore[arg-type]
        persona_id=persona_id,
        store_kind="self_facts",
        chunks=[_chunk(persona_id)],
    )
    su.dispose()


def _chunk(persona_id: str) -> object:
    from persona.schema.chunks import PersonaChunk

    return PersonaChunk(
        id=f"{persona_id}::self_facts::0000",
        text="self_fact: knows things",
        metadata={},
        created_at=datetime.now(UTC),
    )


def _cleanup(su_url: str, owner: str) -> None:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": owner})
    su.dispose()


async def _drive(loop: object, conv: Conversation, message: str) -> list[RunEvent]:
    events: list[RunEvent] = []

    async def _on_event(event: RunEvent) -> None:
        events.append(event)

    async for _chunk in loop.turn(conv, message, _on_event):  # type: ignore[attr-defined]
        pass
    return events


@pytest.mark.asyncio
async def test_reschedule_flows_through_the_real_composition_root_and_fires(
    migrated_engine: Engine,
    embedder: object,
) -> None:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    su_url = os.environ["DATABASE_URL"]
    owner, persona_id, conv_id = "user_a8re", "persona_a8re", "c_a8re"
    _seed(su_url, embedder, owner, persona_id, conv_id)

    rls_engine = make_rls_engine(app_url)
    factory = RuntimeFactory(
        rls_engine=rls_engine,
        embedder=embedder,  # type: ignore[arg-type]
        tier_registry=_ScriptedRegistry(),  # type: ignore[arg-type]
        turn_log_writer=_NullTurnLog(),  # type: ignore[arg-type]
        audit_root=_AUDIT,
    )
    memory_backend = PostgresBackend(engine=rls_engine, embedder=embedder)  # type: ignore[arg-type]
    token = current_user_id.set(owner)
    try:
        loop = await factory.build_conversation_loop(persona_id)
        assert loop._reschedule_interpreter is not None  # the T6 activation, proven wired
        assert loop._timezone_provider is not None
        services = compose_task_origination_services(
            rls_engine=rls_engine,
            memory_backend=memory_backend,
            edition=Edition.cloud,
            audit_root=_AUDIT,
        )
        reschedule_service = TaskRescheduleService(
            task_reader=TaskStore(rls_engine),
            schedule_store=ScheduleStore(rls_engine),
            engine=rls_engine,
        )

        # (1) Create a recurring (daily 08:00) schedule-backed task via the real contract flow.
        conv = Conversation(conversation_id=conv_id, persona_id=persona_id, messages=[])
        await _drive(loop, conv, "every morning at 8, give me the morning brief")
        originated = [e for e in await _drive(loop, conv, "yes") if e.type == "task_originated"]
        assert len(originated) == 1
        outcome = await services.origination.originate(
            {**originated[0].data, "owner_id": owner, "assistant_message_id": "amsg-a8-create"}
        )
        schedule_id = TaskStore(rls_engine).get(owner, outcome.task_id).schedule_id
        assert schedule_id is not None
        assert ScheduleStore(rls_engine).get(owner, schedule_id).recurrence.byhour == (8,)  # type: ignore[union-attr]

        # (2) Reschedule it in chat: "move it to 9" → re-echo → confirm → the event.
        conv2 = Conversation(conversation_id=conv_id, persona_id=persona_id, messages=[])
        echo_events = await _drive(loop, conv2, "move the morning brief to 9")
        assert all(e.type != "task_rescheduled" for e in echo_events)  # the echo does NOT apply
        assert conv2.messages[-1].metadata.get("reschedule_proposal")  # a proposal is pending

        confirm = [e for e in await _drive(loop, conv2, "yes") if e.type == "task_rescheduled"]
        assert len(confirm) == 1  # the one explicit confirm emitted the reschedule event

        # (3) The real service applies it through the CAS door.
        reschedule_service.reschedule({**confirm[0].data, "owner_id": owner})
        rescheduled = ScheduleStore(rls_engine).get(owner, schedule_id)
        assert rescheduled.recurrence.byhour == (9,)  # type: ignore[union-attr] — applied at 09:00

        # (4) The next REAL fire happens at the NEW time through the real scheduler.
        new_fire = rescheduled.next_fire_at
        assert new_fire is not None
        dispatch = create_engine(su_url.replace("+asyncpg", "+psycopg"))
        try:
            leader = SchedulerLeader(dispatch, lock_key=_LOCK)
            tick = SchedulerTick(
                dispatch_engine=dispatch,
                rls_engine=rls_engine,
                leader=leader,
                default_grace_seconds=366 * 24 * 3600.0,
            )
            fired = tick.run_once(now=new_fire)
            leader.resign()
            assert fired == 1  # fired at the new time, through the real tick — no hand-advance
        finally:
            dispatch.dispose()
        after = ScheduleStore(rls_engine).get(owner, schedule_id)
        assert after.last_fire_at == new_fire  # the 09:00 occurrence fired
    finally:
        current_user_id.reset(token)
        rls_engine.dispose()
        _cleanup(su_url, owner)
