"""A10 T4 — the confabulation close, proven through the REAL composed loop (criterion 5).

The anti-inertness proof of the NEGATIVE: a real chat turn through the actual
``RuntimeFactory.build_conversation_loop`` composition (real recognizer machinery over a
scripted small-tier judge; only the model backend is scripted) where the judge MISSES
("now" — the weak-model failure observed live) and the chat model freely claims
"I've scheduled it". The loop appends the deterministic honest correction and — the proven
negative — **``tasks = 0, schedules = 0``** in the real database: no false confirm survives
uncorrected, and nothing was created. The positive control drives the same composition with
a judge HIT to confirm the grounded confirm flow still voices success uncorrected and emits
``task_originated``.
"""

# ruff: noqa: ARG001 — dependency-ordering fixture params
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.schema.chunks import PersonaChunk
from persona.schema.conversation import Conversation
from persona.stores.postgres import PostgresBackend
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services.runtime_factory import RuntimeFactory
from persona_runtime.schedule_claim import render_schedule_correction
from sqlalchemy import text

if TYPE_CHECKING:
    from persona.backends import StreamChunk
    from persona.schema.conversation import ConversationMessage
    from persona_runtime.agentic.events import RunEvent
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_AUDIT = Path("/tmp/persona-a10-confab-audit")
_CORRECTION = render_schedule_correction()
_CLAIM_TEXT = "Done — I've scheduled it. You'll get an update every morning at 8."
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
    """The judge misses (or hits) by script; the chat stream freely claims success."""

    provider_name = "anthropic"
    model_name = "scripted"
    max_tokens = 4096

    def __init__(self, *, judge_verdict: str) -> None:
        self._verdict = judge_verdict

    @property
    def supports_native_tools(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **_: object) -> ChatResponse:
        system = str(messages[0].content)
        content = "{}"
        if "STANDING task" in system:
            if self._verdict == "standing":
                content = json.dumps(
                    {
                        "verdict": "standing",
                        "goal": "the morning brief",
                        "recurrence_rrule": "FREQ=DAILY;BYHOUR=8;BYMINUTE=0",
                    }
                )
            else:
                content = json.dumps({"verdict": "now_work"})  # the weak-model miss
        return ChatResponse(
            content=content,
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )

    async def chat_stream(
        self,
        messages: list[ConversationMessage],  # noqa: ARG002 — the claim is unconditional
        **_: object,
    ) -> AsyncIterator[StreamChunk]:
        from persona.backends import StreamChunk

        yield StreamChunk(
            delta=_CLAIM_TEXT,
            is_final=True,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


class _ScriptedRegistry:
    def __init__(self, *, judge_verdict: str) -> None:
        self._b = _ScriptedBackend(judge_verdict=judge_verdict)

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


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    engine = make_rls_engine(app_url)
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
    PostgresBackend(engine=su, embedder=embedder).upsert(
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


def _counts(su_url: str, owner: str) -> tuple[int, int]:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        tasks = conn.execute(
            text("SELECT count(*) FROM tasks WHERE owner_id = :o"), {"o": owner}
        ).scalar()
        schedules = conn.execute(
            text("SELECT count(*) FROM schedules WHERE owner_id = :o"), {"o": owner}
        ).scalar()
    su.dispose()
    return int(tasks or 0), int(schedules or 0)


async def _drive(
    factory: RuntimeFactory, persona_id: str, message: str, *, conv: Conversation | None = None
) -> tuple[str, list[RunEvent], Conversation]:
    loop = await factory.build_conversation_loop(persona_id)
    conversation = conv or Conversation(
        conversation_id="c_confab", persona_id=persona_id, messages=[]
    )
    events: list[RunEvent] = []

    async def on_event(ev: RunEvent) -> None:
        events.append(ev)

    streamed = ""
    async for chunk in loop.turn(conversation, message, on_event):
        streamed += chunk.delta or ""
    return streamed, events, conversation


@pytest.mark.asyncio
async def test_recognizer_miss_no_false_confirm_and_nothing_created(
    app_engine: Engine, embedder: HashEmbedder384
) -> None:
    """The proven negative: judge misses, the model claims — the correction lands and
    ``tasks=0, schedules=0``. Driven through the REAL composition, no hand-forced step."""
    su_url = os.environ["DATABASE_URL"]
    owner, persona_id = "user_confab", "persona_confab"
    _seed(su_url, embedder, owner, persona_id)
    token = current_user_id.set(owner)
    try:
        factory = RuntimeFactory(
            rls_engine=app_engine,
            embedder=embedder,
            tier_registry=_ScriptedRegistry(judge_verdict="now"),  # type: ignore[arg-type]
            turn_log_writer=_NullTurnLog(),  # type: ignore[arg-type]
            audit_root=_AUDIT,
        )
        streamed, events, conversation = await _drive(
            factory, persona_id, "every morning, track the fares for me"
        )

        assert _CLAIM_TEXT in streamed  # the model DID confabulate (the scripted claim)
        assert _CORRECTION in streamed  # …and the loop corrected it in the same turn
        assert _CORRECTION in conversation.messages[-1].content  # persisted corrected
        assert [e for e in events if e.type == "task_originated"] == []  # nothing emitted
        assert _counts(su_url, owner) == (0, 0)  # NOTHING was created — the negative
    finally:
        current_user_id.reset(token)
        _cleanup(su_url, owner)


@pytest.mark.asyncio
async def test_judge_hit_confirm_flow_voices_success_uncorrected(
    app_engine: Engine, embedder: HashEmbedder384
) -> None:
    """The grounded positive control: standing → echo → 'yes' → the emission-coupled
    success voice, task_originated emitted, and NO correction appended."""
    su_url = os.environ["DATABASE_URL"]
    owner, persona_id = "user_confab_pos", "persona_confab_pos"
    _seed(su_url, embedder, owner, persona_id)
    token = current_user_id.set(owner)
    try:
        factory = RuntimeFactory(
            rls_engine=app_engine,
            embedder=embedder,
            tier_registry=_ScriptedRegistry(judge_verdict="standing"),  # type: ignore[arg-type]
            turn_log_writer=_NullTurnLog(),  # type: ignore[arg-type]
            audit_root=_AUDIT,
        )
        echoed, _ev1, conversation = await _drive(
            factory, persona_id, "every morning, track the fares for me"
        )
        assert "When:" in echoed or "every" in echoed.lower()  # the compact echo turn

        confirmed, events, _conv = await _drive(factory, persona_id, "yes", conv=conversation)
        originated = [e for e in events if e.type == "task_originated"]
        assert len(originated) == 1  # the success voice is emission-coupled (grounded)
        assert "I've set that up" in confirmed  # the honest success voice
        assert _CORRECTION not in confirmed  # never second-guessed by the safety net
    finally:
        current_user_id.reset(token)
        _cleanup(su_url, owner)
