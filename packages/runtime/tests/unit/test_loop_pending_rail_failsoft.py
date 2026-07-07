"""The pending-proposal rail's fail-soft (R4 operator find — the rail escape, loop half).

The live escape's second layer: with a proposal PENDING, a reply that is neither a clean
confirm nor an interpretable amendment ("once at 9 30" when the small-tier interpreter
misses, errors, or cannot express a one-time retiming) fell through to ORDINARY chat — a
free model turn that can confabulate (the R4-C1-20 .ics). The wrong fail-soft: on a pending
proposal the safe degradation is ask-once clarify (stay pending), never a free generation.

These tests drive the REAL ``ConversationLoop.turn`` (only the interpreter/backend
scripted) and pin the required behavior:

* amendment interpreter returns ``None`` → NO free generation; the proposal stays pending
  (the re-carried ``contract_proposal``) and the user is asked once;
* amendment interpreter RAISES (small-tier flake) → same ask-once clarify, never a crash
  and never a free turn;
* the clarify is ask-ONCE: a second uninterpretable reply releases the rail to ordinary
  chat (no infinite trap).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _fakes import FakeStore, ScriptedBackend, ScriptedRound  # type: ignore[import-not-found]
from persona.backends import BackendConfig
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation, ConversationMessage
from persona.schema.persona import Persona, PersonaIdentity
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona_runtime.logging import MemoryTurnLogWriter
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.task_origination import (
    ContractDraft,
    RecognitionKind,
    RecognitionOutcome,
)
from persona_runtime.tier import TierConfig, TierRegistry

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]


class _OrdinaryRecognizer:
    async def recognize(self, message: str, *, language: str = "en") -> RecognitionOutcome:  # noqa: ARG002
        return RecognitionOutcome(kind=RecognitionKind.ORDINARY)


class _NoneAmendment:
    """The small-tier miss: the interpreter cannot map the reply to a clause patch."""

    def __init__(self) -> None:
        self.calls = 0

    async def interpret(self, reply: str, draft: ContractDraft) -> ContractDraft | None:  # noqa: ARG002
        self.calls += 1
        return None


class _RaisingAmendment:
    """The small-tier flake: the backend call blows up mid-interpretation."""

    async def interpret(self, reply: str, draft: ContractDraft) -> ContractDraft | None:  # noqa: ARG002
        msg = "small-tier backend timeout"
        raise RuntimeError(msg)


def _make_loop(backend: ScriptedBackend, amendment: object) -> ConversationLoop:
    registry = TierRegistry(
        {
            "frontier": TierConfig(name="frontier", backend_config=_DUMMY_CFG),
            "mid": TierConfig(name="mid", backend_config=_DUMMY_CFG),
            "small": TierConfig(name="small", backend_config=_DUMMY_CFG),
        }
    )
    registry._cache = {"frontier": backend, "mid": backend, "small": backend}  # type: ignore[assignment]  # noqa: SLF001
    return ConversationLoop(
        persona=Persona(
            persona_id="astrid",
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
        standing_recognizer=_OrdinaryRecognizer(),  # type: ignore[arg-type]
        amendment_interpreter=amendment,  # type: ignore[arg-type]
    )


def _pending_conv() -> Conversation:
    """A conversation whose last assistant turn is the transcript's pending proposal."""
    draft = ContractDraft(goal="check email inbox")
    now = datetime.now(UTC)
    return Conversation(
        conversation_id="c1",
        persona_id="astrid",
        messages=[
            ConversationMessage(
                role="user",
                content="hello, can you schedule a task for me every 15 min, "
                "to check my email inbox",
                created_at=now,
            ),
            ConversationMessage(
                role="assistant",
                content="Goal: check email inbox\nWhen: once, on Tuesday 07 July at 09:19 "
                "your time · Europe/Oslo\n\nShall I go ahead?",
                created_at=now,
                metadata={"contract_proposal": draft.model_dump_json()},
            ),
        ],
    )


async def _run(loop: ConversationLoop, conv: Conversation, message: str) -> str:
    return "".join([c.delta or "" async for c in loop.turn(conv, message)])


@pytest.mark.asyncio
async def test_uninterpretable_reply_on_pending_proposal_never_runs_a_free_turn() -> None:
    """THE transcript escape: 'once at 9 30' + interpreter miss must NOT reach generation."""
    backend = ScriptedBackend(
        [ScriptedRound(text="I've created a one-time calendar reminder — import the .ics!")]
    )
    interp = _NoneAmendment()
    loop = _make_loop(backend, interp)
    conv = _pending_conv()

    await _run(loop, conv, "once at 9 30")

    assert interp.calls == 1  # the amendment path really ran and really missed
    assert backend.chat_stream_calls == 0, (
        "a pending proposal fail-softed into a FREE model turn — the confabulation door "
        "(R4-C1-20). The safe degradation is ask-once clarify, never ordinary generation."
    )
    last = conv.messages[-1]
    assert last.role == "assistant"
    assert "contract_proposal" in last.metadata, (
        "the proposal was dropped instead of staying pending through the clarify"
    )


@pytest.mark.asyncio
async def test_interpreter_error_on_pending_proposal_degrades_to_clarify() -> None:
    """A small-tier flake must degrade to the same clarify — never a crash or a free turn."""
    backend = ScriptedBackend([ScriptedRound(text="free turn — must not happen")])
    loop = _make_loop(backend, _RaisingAmendment())
    conv = _pending_conv()

    await _run(loop, conv, "once at 9 30")

    assert backend.chat_stream_calls == 0
    assert "contract_proposal" in conv.messages[-1].metadata


@pytest.mark.asyncio
async def test_clarify_is_ask_once_second_miss_releases_to_ordinary_chat() -> None:
    """No infinite trap: after one clarify, a second uninterpretable reply falls through."""
    backend = ScriptedBackend([ScriptedRound(text="Sure — switching topics, here's the weather.")])
    loop = _make_loop(backend, _NoneAmendment())
    conv = _pending_conv()

    await _run(loop, conv, "once at 9 30")  # → clarify, stays pending
    text = await _run(loop, conv, "actually what's the weather like")  # → released

    assert backend.chat_stream_calls == 1  # the second miss IS an ordinary turn
    assert "weather" in text
