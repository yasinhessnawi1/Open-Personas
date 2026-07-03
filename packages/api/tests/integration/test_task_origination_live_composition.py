"""The A4 contract flow, through the REAL composition root (Spec A4, composition-root wiring).

This is the anti-inertness gate. It builds the **actual** production wiring — a real
:class:`RuntimeFactory` (whose ``build_conversation_loop`` composes the real standing recognizer +
model interpreters + task reader), the real ``compose_task_origination_services`` the API lifespan
calls, and the real ``build_worker_registry`` task-leg tenant — and proves the flow runs end-to-end
through it:

1. **recognize → echo → confirm → create** — a standing-intent turn through the real ``loop.turn``
   emits a ``task_originated`` event that the real ``OriginationService`` turns into a durable task
   (idempotent under replay).
2. **steering** — a "pause" turn through the real ``loop.turn`` resolves to a steer that the real
   ``TaskSteeringService`` applies to the live task.
3. **cancel-failure visibility** — the real notifier persists an un-suppressible cancel-failure
   account on the conversation (the trigger→classify logic is unit-proven in both directions).
4. **digest wiring** — the real ``build_worker_registry`` registers the task-leg + scheduled-fire
   tenants and wires the digest hook into the real handler (its DELIVERY on a real fire, and the
   honest one-off/recurring content, are proven in ``test_task_recurrence_live``).

Only the model backend is scripted (the injected dependency); everything the composition root builds
is real. A test that news-up the services itself would false-green the very inertness this kills.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.backends import StreamChunk, TokenUsage
from persona.backends.types import ChatResponse
from persona.schema.conversation import Conversation
from persona.stores.postgres import PostgresBackend
from persona_api.approvals.failure import account_for_cancel_failure
from persona_api.config import APIConfig, Edition
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services.origination_adapters import resolve_persona_tag
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.services.task_origination_composition import compose_task_origination_services
from persona_api.tasks.store import TaskStore
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.schema.conversation import ConversationMessage
    from persona_runtime.agentic.events import RunEvent
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_AUDIT = Path("/tmp/persona-a4-live-audit")
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
    """A chat backend that answers the A4 model steps by prompt-type + streams ordinary replies.

    The contract-flow gates use ``chat`` (non-streaming): the judge returns a standing verdict, the
    steering interpreter echoes the first active task's id back as a pause. ``chat_stream`` is the
    ordinary-generation path (not reached by the A4 turns, which end at the gate).
    """

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
        system = messages[0].content
        user = messages[-1].content
        content = self._answer(str(system), str(user))
        return ChatResponse(
            content=content,
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )

    @staticmethod
    def _answer(system: str, user: str) -> str:
        # Order matters: the steering prompt also mentions "STANDING tasks", so match the more
        # specific STEER / ADJUSTS markers before the judge's STANDING marker.
        if "STEER" in system:  # the steering interpreter — pause the first listed task
            match = re.search(r"id=(\S+):", user)
            task_id = match.group(1) if match else ""
            return json.dumps({"verb": "pause", "task_id": task_id})
        if "ADJUSTS the proposal" in system:  # the amendment interpreter (not exercised here)
            return '{"amends": false}'
        if "STANDING task" in system:  # the standing-intent judge
            return '{"verdict": "standing", "goal": "track morning fares"}'
        return "{}"

    async def chat_stream(
        self,
        messages: list[ConversationMessage],  # noqa: ARG002 — ordinary generation isn't reached
        **_: object,
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(
            delta="ok",
            is_final=True,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


class _ScriptedRegistry:
    """A TierRegistry stand-in returning the scripted A4 backend for any tier."""

    def __init__(self) -> None:
        self._b = _ScriptedA4Backend()

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


def _seed(
    su_url: str, embedder: HashEmbedder384, owner: str, persona_id: str, conv_id: str
) -> None:
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
        # The origination + digest + failure account all persist onto this conversation, so it must
        # exist + be owned (the recorder's ownership guard fires otherwise).
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, title) "
                "VALUES (:c, :o, :p, '') ON CONFLICT DO NOTHING"
            ),
            {"c": conv_id, "o": owner, "p": persona_id},
        )
    backend = PostgresBackend(engine=su, embedder=embedder)
    from persona.schema.chunks import PersonaChunk

    backend.upsert(
        persona_id=persona_id,
        store_kind="self_facts",
        chunks=[
            PersonaChunk(
                id=f"{persona_id}::self_facts::0000",
                text="self_fact: knows things",
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


async def _drive(loop: object, conv: Conversation, message: str) -> list[RunEvent]:
    events: list[RunEvent] = []

    async def _on_event(event: RunEvent) -> None:
        events.append(event)

    async for _chunk in loop.turn(conv, message, _on_event):  # type: ignore[attr-defined]
        pass
    return events


def _messages_on(su_url: str, conv_id: str) -> list[str]:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        rows = conn.execute(
            text("SELECT content FROM messages WHERE conversation_id = :c AND role = 'assistant'"),
            {"c": conv_id},
        ).all()
    su.dispose()
    return [r[0] for r in rows]


@pytest.mark.asyncio
async def test_a4_flows_through_the_real_composition_root(
    migrated_engine: Engine,  # noqa: ARG001 — ordering: migrations first
    embedder: HashEmbedder384,
) -> None:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set")
    su_url = os.environ["DATABASE_URL"]
    owner, persona_id, conv_id = "user_a4live", "persona_a4live", "c_a4live"
    _seed(su_url, embedder, owner, persona_id, conv_id)

    rls_engine = make_rls_engine(app_url)
    factory = RuntimeFactory(
        rls_engine=rls_engine,
        embedder=embedder,
        tier_registry=_ScriptedRegistry(),  # type: ignore[arg-type]
        turn_log_writer=_NullTurnLog(),  # type: ignore[arg-type]
        audit_root=_AUDIT,
    )
    memory_backend = PostgresBackend(engine=rls_engine, embedder=embedder)
    token = current_user_id.set(owner)
    try:
        # The REAL loop-side composition: build_conversation_loop wires the A4 interpreters/reader.
        loop = await factory.build_conversation_loop(persona_id)
        assert loop._standing_recognizer is not None  # noqa: SLF001 — the activation, proven wired
        assert loop._steering_interpreter is not None  # noqa: SLF001
        assert loop._task_reader_provider is not None  # noqa: SLF001

        # The REAL worker-side composition (the exact function app.py calls).
        services = compose_task_origination_services(
            rls_engine=rls_engine,
            memory_backend=memory_backend,
            edition=Edition.cloud,
            audit_root=_AUDIT,
        )

        # (1) recognize → echo → confirm → create, all through the real turn().
        conv = Conversation(conversation_id=conv_id, persona_id=persona_id, messages=[])
        echo_events = await _drive(loop, conv, "every morning, track the fares for me")
        assert all(e.type != "task_originated" for e in echo_events)  # the echo does not create
        assert conv.messages[-1].metadata.get("contract_proposal")  # a proposal is pending

        confirm_events = await _drive(loop, conv, "yes")
        originated = [e for e in confirm_events if e.type == "task_originated"]
        assert len(originated) == 1  # the one explicit confirmation emitted the create event

        # The worker injects the tenant + the confirm turn's message id (the idempotency anchor).
        event_data = {
            **originated[0].data,
            "owner_id": owner,
            "assistant_message_id": "amsg-a4-confirm",
        }
        outcome = await services.origination.originate(event_data)
        created = TaskStore(rls_engine).get(owner, outcome.task_id)
        assert created.contract.goal == "track morning fares"  # the confirmed contract landed

        # Idempotency: the same event replayed converges on exactly one task.
        again = await services.origination.originate(event_data)
        assert again.task_id == outcome.task_id
        assert len(TaskStore(rls_engine).list_for_owner(owner)) == 1

        # (2) steering: a "pause" turn resolves + the real steering service applies it.
        conv2 = Conversation(conversation_id=conv_id, persona_id=persona_id, messages=[])
        steer_events = await _drive(loop, conv2, "pause the fare tracker")
        steering = [e for e in steer_events if e.type == "task_steering"]
        assert len(steering) == 1
        assert steering[0].data["task_id"] == outcome.task_id  # resolved to the live task
        await services.steering.steer(
            {
                **steering[0].data,
                "owner_id": owner,
                "conversation_id": conv_id,
                "persona_id": persona_id,
            }
        )
        assert TaskStore(rls_engine).get(owner, outcome.task_id).paused is True

        # (3) cancel-failure visibility: the real notifier persists an un-suppressible account.
        tag = resolve_persona_tag(rls_engine, persona_id)
        assert tag is not None
        from persona_api.services.origination_adapters import OriginatorFailureNotifier

        notifier = OriginatorFailureNotifier(
            rls_engine=rls_engine,
            memory_backend=memory_backend,
            edition=Edition.cloud,
            audit_root=_AUDIT,
        )
        await notifier.notify(
            account_for_cancel_failure(outcome.task_id, cause="the store was unavailable"),
            persona=tag,
            owner_id=owner,
            conversation_id=conv_id,
        )
        assert any("still running" in m for m in _messages_on(su_url, conv_id))

        # (4) digest-on-milestone: the REAL worker registry wires the hook into the REAL handler.
        from persona_api.background.worker_root import build_worker_registry

        registry = build_worker_registry(
            rls_engine=rls_engine,
            embedder=embedder,
            tier_registry=_ScriptedRegistry(),  # type: ignore[arg-type]
            config=APIConfig(audit_root=str(_AUDIT)),
            synthesis_tier="small",
            runtime_factory=factory,
            memory_backend=memory_backend,
            edition=Edition.cloud,
        )
        # The A4 tenants are registered by the real construction, and the digest hook is wired into
        # the real task-leg handler. The digest's DELIVERY (and its honest one-off/recurring
        # content) is proven end-to-end through a REAL scheduler fire in test_task_recurrence_live —
        # invoking the hook by hand here would be the forced-verdict that test exists to kill.
        assert "task_leg" in registry.types()
        assert "task_scheduled_fire" in registry.types()  # the A1→A2 fire bridge is wired
        assert registry.get("task_leg").handler._on_milestone is not None  # noqa: SLF001
    finally:
        current_user_id.reset(token)
        rls_engine.dispose()
        _cleanup(su_url, owner)
