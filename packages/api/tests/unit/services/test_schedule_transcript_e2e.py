"""The R4 live-transcript retelling, end-to-end across the REAL DB round-trip.

The operator-pass transcript this spec fixes, replayed through the production seams with
ONLY the model backends scripted (the real recognizer, judge, amendment interpreter, loop,
sink, and loader all run for real):

1. "hello, can you schedule a task for me every 15 min, to check my email inbox"
   → the echo now carries the REAL recurring cadence with the honest volume line
   ("every 15 minutes, around the clock — 96 times a day") — not the silent once-fallback.
2. The turn is persisted via the real ``MessagesTurnSink`` and the conversation RELOADED
   via the real ``_load_conversation`` — the pending proposal survives the turn boundary
   (the B-1 fix; before it, this hop erased the rail).
3. "once at 9 30" on the reloaded conversation → the amendment path re-echoes the one-time
   retiming (the B-3 fix) with ZERO free-generation calls (the rail never escapes to the
   confabulating model turn — the R4-C1-20 door stays shut).
4. A clean "yes" (after another reload hop) emits exactly one ``task_originated`` carrying
   the amended 09:30 one-time cadence.
"""

from __future__ import annotations

import sys
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends import BackendConfig
from persona.backends.types import ChatResponse, TokenUsage
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import personas as personas_t
from persona_api.middleware.rls_context import current_user_id
from persona_api.services.chat_service import _load_conversation
from persona_api.services.chat_turn_sink import MessagesTurnSink
from persona_runtime.logging import MemoryTurnLogWriter
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.task_origination import (
    ModelAmendmentInterpreter,
    ModelStandingIntentJudge,
    StandingIntentRecognizer,
)
from persona_runtime.tier import TierConfig, TierRegistry
from sqlalchemy import insert

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "runtime" / "tests"))
from _fakes import (  # type: ignore[import-not-found]  # noqa: E402
    FakeStore,
    ScriptedBackend,
    ScriptedRound,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]

_OWNER = "user_yasin"
_PERSONA = "astrid"
_CONV = "conv_transcript"

_TURN_1 = "hello, can you schedule a task for me every 15 min, to check my email inbox"
_TURN_2 = "once at 9 30"
_TURN_3 = "yes"

#: The scripted small-tier model replies (the ONLY test doubles — everything else is real).
_JUDGE_JSON = (
    '{"verdict": "standing", "goal": "check email inbox", '
    '"recurrence_rrule": "FREQ=MINUTELY;INTERVAL=15"}'
)
_AMEND_JSON = '{"amends": true, "one_time_at": "2027-07-07T09:30:00"}'


class _StubModelBackend:
    """A precision-layer chat backend returning one canned JSON reply."""

    def __init__(self, content: str) -> None:
        self._content = content

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return "claude-haiku-4-5-20251001"

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: object, **kwargs: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        return ChatResponse(
            content=self._content,
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


@pytest.fixture
def engine(tmp_path: object) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "t.db")  # type: ignore[operator]
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="y@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: Astrid"))
        conn.execute(insert(conversations_t).values(id=_CONV, owner_id=_OWNER, persona_id=_PERSONA))
    token = current_user_id.set(_OWNER)
    try:
        yield eng
    finally:
        current_user_id.reset(token)


def _make_loop(chat_backend: ScriptedBackend) -> ConversationLoop:
    registry = TierRegistry(
        {
            "frontier": TierConfig(name="frontier", backend_config=_DUMMY_CFG),
            "mid": TierConfig(name="mid", backend_config=_DUMMY_CFG),
            "small": TierConfig(name="small", backend_config=_DUMMY_CFG),
        }
    )
    registry._cache = {"frontier": chat_backend, "mid": chat_backend, "small": chat_backend}  # type: ignore[assignment]  # noqa: SLF001
    return ConversationLoop(
        persona=Persona(
            persona_id=_PERSONA,
            identity=PersonaIdentity(name="Astrid", role="assistant", background="bg"),
        ),
        stores={
            "identity": FakeStore(),
            "self_facts": FakeStore(),
            "worldview": FakeStore(),
            "episodic": FakeStore(),
        },  # type: ignore[arg-type]
        toolbox=Toolbox([], allow_list=None),
        skill_scanner=SkillScanner([]),
        skill_injector=SkillInjector(),
        scanned_skills=[],
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
        turn_log_writer=MemoryTurnLogWriter(),
        # The REAL recognizer + judge + amendment classes — only their backends scripted.
        standing_recognizer=StandingIntentRecognizer(
            ModelStandingIntentJudge(
                backend=_StubModelBackend(_JUDGE_JSON), default_timezone="Europe/Oslo"
            )
        ),
        amendment_interpreter=ModelAmendmentInterpreter(backend=_StubModelBackend(_AMEND_JSON)),
    )


async def _turn(
    loop: ConversationLoop, conversation: Conversation, message: str
) -> tuple[str, list[Any]]:
    events: list[Any] = []

    async def on_event(ev: Any) -> None:  # noqa: ANN401
        events.append(ev)

    text = "".join(
        [c.delta or "" async for c in loop.turn(conversation, message, on_event=on_event)]
    )
    return text, events


def _persist_turn(engine: Engine, conversation: Conversation, user_message: str) -> None:
    """One production persistence hop: open_turn + finalize with the loop-mutated state."""
    sink = MessagesTurnSink(engine)
    assistant_id = sink.open_turn(
        conversation_id=_CONV, user_message=user_message, channel=None, images=None
    )
    final = conversation.messages[-1]
    sink.finalize(
        conversation_id=_CONV,
        assistant_message_id=assistant_id,
        conversation=conversation,
        status="complete",
        content=str(final.content),
        events=[],
        tier="small",
    )


def _reload(engine: Engine) -> Conversation:
    with engine.begin() as conn:
        return _load_conversation(conn, _CONV)


@pytest.mark.asyncio
async def test_the_live_transcript_now_runs_correctly_end_to_end(engine: Engine) -> None:
    chat_backend = ScriptedBackend(
        [ScriptedRound(text="I've created a one-time calendar reminder — import the .ics!")]
    )
    loop = _make_loop(chat_backend)

    # --- Turn 1: "every 15 min" → the honest recurring echo (BUG A fixed) --------------
    conversation = _reload(engine)
    echo, events_1 = await _turn(loop, conversation, _TURN_1)
    assert "every 15 minutes, around the clock — 96 times a day" in echo  # the volume line
    assert "Europe/Oslo" in echo
    assert "once," not in echo  # never the silent once-fallback the transcript showed
    assert all(getattr(ev, "type", "") != "task_originated" for ev in events_1)
    assert chat_backend.chat_stream_calls == 0  # the echo is deterministic, no free turn

    # --- The turn boundary: persist + reload (the hop that used to erase the rail) -----
    _persist_turn(engine, conversation, _TURN_1)
    conversation = _reload(engine)
    last = conversation.messages[-1]
    assert last.role == "assistant"
    assert "contract_proposal" in last.metadata  # B-1: the pending rail SURVIVES the DB

    # --- Turn 2: "once at 9 30" → the amendment re-echo, never a free turn (BUG B) -----
    reecho, events_2 = await _turn(loop, conversation, _TURN_2)
    assert chat_backend.chat_stream_calls == 0, (
        "the pending-proposal reply escaped to a free model turn — the confabulation door"
    )
    assert "09:30" in reecho  # the one-time retiming was understood and re-echoed
    assert all(getattr(ev, "type", "") != "task_originated" for ev in events_2)  # not yet
    assert "contract_proposal" in conversation.messages[-1].metadata  # still pending

    # --- The second boundary hop, then the clean confirm creates ------------------------
    _persist_turn(engine, conversation, _TURN_2)
    conversation = _reload(engine)
    _ack, events_3 = await _turn(loop, conversation, _TURN_3)
    originated = [ev for ev in events_3 if getattr(ev, "type", "") == "task_originated"]
    assert len(originated) == 1  # exactly one create, from the confirm turn
    schedule = originated[0].data["schedule"]
    assert schedule["recurrence"] is None  # the amended cadence: once …
    assert schedule["one_time_at"] is not None  # … at the retimed instant
    from datetime import datetime

    fired_at = datetime.fromisoformat(str(schedule["one_time_at"])).astimezone(UTC)
    assert fired_at == datetime(2027, 7, 7, 7, 30, tzinfo=UTC)  # 09:30 Oslo = 07:30Z (CEST)
    assert chat_backend.chat_stream_calls == 0  # the whole arc ran without one free turn


@pytest.mark.asyncio
async def test_interpreter_miss_on_the_reloaded_pending_clarifies_not_confabulates(
    engine: Engine,
) -> None:
    """The fail-soft half across the REAL DB hop: if the small tier misses the amendment,
    the reply gets the ask-once clarify — never the free turn the transcript produced."""
    chat_backend = ScriptedBackend([ScriptedRound(text="free turn — must never stream")])
    loop = _make_loop(chat_backend)
    conversation = _reload(engine)
    await _turn(loop, conversation, _TURN_1)
    _persist_turn(engine, conversation, _TURN_1)
    conversation = _reload(engine)

    # Swap the amendment stub for a MISS (the scripted small-tier "not an amendment").
    loop._amendment_interpreter = ModelAmendmentInterpreter(  # noqa: SLF001
        backend=_StubModelBackend('{"amends": false}')
    )
    clarify, _events = await _turn(loop, conversation, _TURN_2)

    assert chat_backend.chat_stream_calls == 0  # no free generation
    assert "here's what I have pending" in clarify  # the deterministic clarify spoke
    assert "contract_proposal" in conversation.messages[-1].metadata  # stays pending
