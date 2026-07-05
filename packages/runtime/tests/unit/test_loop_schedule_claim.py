"""A10 T4 — the honesty gate at the loop seam (criterion 5, the loop-level half).

Grounding is the PRIMARY gate (structural): the deterministic confirm branch early-returns
with the success voice + the ``task_originated`` emission, so the post-generation seam only
ever runs in the ¬created state. These tests drive the REAL ``ConversationLoop.turn`` (only
the backend scripted) and pin all four directions:

* a free-text schedule claim in ordinary generation gets the appended honest correction —
  streamed AND persisted (the write-back the synthesis tail reads);
* it fires with NO recognizer wired at all (the fail-soft keyless env — the worst
  confabulator);
* proposals / state descriptions / ordinary chat are byte-unchanged (precision);
* the REAL confirm path still voices success uncorrected + emits ``task_originated``
  (grounded positive, not over-corrected).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
from persona_runtime.schedule_claim import render_schedule_correction
from persona_runtime.task_origination import (
    ContractDraft,
    RecognitionKind,
    RecognitionOutcome,
)
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from persona.backends import StreamChunk
    from persona_runtime.agentic.events import RunEvent

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]
_CORRECTION = render_schedule_correction()


def _persona() -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(name="Astrid", role="assistant", background="bg"),
    )


class _OrdinaryRecognizer:
    """A wired recognizer whose judge always says ORDINARY — the weak-model miss."""

    def __init__(self) -> None:
        self.calls = 0

    async def recognize(self, message: str, *, language: str = "en") -> RecognitionOutcome:  # noqa: ARG002
        self.calls += 1
        return RecognitionOutcome(kind=RecognitionKind.ORDINARY)


def _make_loop(
    backend: ScriptedBackend, *, recognizer: _OrdinaryRecognizer | None = None
) -> ConversationLoop:
    registry = TierRegistry(
        {
            "frontier": TierConfig(name="frontier", backend_config=_DUMMY_CFG),
            "mid": TierConfig(name="mid", backend_config=_DUMMY_CFG),
            "small": TierConfig(name="small", backend_config=_DUMMY_CFG),
        }
    )
    registry._cache = {"frontier": backend, "mid": backend, "small": backend}  # type: ignore[assignment]  # noqa: SLF001
    return ConversationLoop(
        persona=_persona(),
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
        standing_recognizer=recognizer,  # type: ignore[arg-type]
    )


def _conv(messages: list[ConversationMessage] | None = None) -> Conversation:
    return Conversation(conversation_id="c1", persona_id="astrid", messages=messages or [])


async def _collect(
    loop: ConversationLoop, conv: Conversation, message: str
) -> tuple[list[StreamChunk], list[RunEvent]]:
    events: list[RunEvent] = []

    async def on_event(ev: RunEvent) -> None:
        events.append(ev)

    chunks = [c async for c in loop.turn(conv, message, on_event)]
    return chunks, events


def _streamed_text(chunks: list[StreamChunk]) -> str:
    return "".join(c.delta or "" for c in chunks)


@pytest.mark.asyncio
async def test_false_claim_on_recognizer_miss_gets_the_correction() -> None:
    """The observed confabulation: judge says ORDINARY, the chat model claims success."""
    backend = ScriptedBackend(
        [ScriptedRound(text="Done! I've scheduled it — you'll hear from me every morning.")]
    )
    recognizer = _OrdinaryRecognizer()
    loop = _make_loop(backend, recognizer=recognizer)
    conv = _conv()

    chunks, _events = await _collect(loop, conv, "set up a daily routine to update me")

    assert recognizer.calls == 1  # the recognizer really ran and really missed
    assert _CORRECTION in _streamed_text(chunks)  # the user SAW the correction
    assert conv.messages[-1].role == "assistant"
    assert _CORRECTION in conv.messages[-1].content  # write-back sees it (graph-safe)


@pytest.mark.asyncio
async def test_false_claim_with_no_recognizer_wired_gets_the_correction() -> None:
    """The fail-soft keyless env (recognizer=None) is the worst confabulator — covered."""
    backend = ScriptedBackend([ScriptedRound(text="I've set that up. Daily at 9 it is.")])
    loop = _make_loop(backend, recognizer=None)
    conv = _conv()

    chunks, _events = await _collect(loop, conv, "help me stay on top of my stretches")

    assert _CORRECTION in _streamed_text(chunks)
    assert _CORRECTION in conv.messages[-1].content


@pytest.mark.asyncio
async def test_ordinary_reply_is_byte_unchanged() -> None:
    backend = ScriptedBackend([ScriptedRound(text="Here's the summary you asked for.")])
    loop = _make_loop(backend)
    conv = _conv()

    chunks, _events = await _collect(loop, conv, "summarise this article")

    assert _CORRECTION not in _streamed_text(chunks)
    assert conv.messages[-1].content == "Here's the summary you asked for."


@pytest.mark.asyncio
async def test_proposal_and_state_descriptions_are_not_corrected() -> None:
    """Precision: offering to schedule, and truthfully describing an EXISTING schedule."""
    backend = ScriptedBackend(
        [
            ScriptedRound(
                text=(
                    "Your reminder is already set up — it runs daily at 9. "
                    "Want me to set up another one? If you confirm, I'll remind you "
                    "every evening too."
                )
            )
        ]
    )
    loop = _make_loop(backend)
    conv = _conv()

    chunks, _events = await _collect(loop, conv, "is my reminder still on?")

    assert _CORRECTION not in _streamed_text(chunks)


@pytest.mark.asyncio
async def test_real_confirm_path_still_voices_success_uncorrected() -> None:
    """Grounded positive, not over-corrected: confirm ⇒ success voice + task_originated,
    and NO correction (the deterministic confirm text early-returns before the seam)."""
    backend = ScriptedBackend([ScriptedRound(text="should never be generated")])
    loop = _make_loop(backend, recognizer=_OrdinaryRecognizer())
    draft = ContractDraft(goal="track morning fares")
    now = datetime.now(UTC)
    conv = _conv(
        [
            ConversationMessage(role="user", content="track fares every morning", created_at=now),
            ConversationMessage(
                role="assistant",
                content="When: every morning …",
                created_at=now,
                metadata={"contract_proposal": draft.model_dump_json()},
            ),
        ]
    )

    chunks, events = await _collect(loop, conv, "yes")

    originated = [e for e in events if e.type == "task_originated"]
    assert len(originated) == 1  # the success voice is emission-coupled (grounded)
    text = _streamed_text(chunks)
    assert "I've set that up" in text  # the honest success voice still speaks
    assert _CORRECTION not in text  # and is never second-guessed by the net
    assert backend.chat_stream_calls == 0  # deterministic branch — no generation at all
