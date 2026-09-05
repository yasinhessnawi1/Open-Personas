"""T3 — the origination gate wired into the voice reply producer (Spec A9, A9-D-1/D-5/D-7).

Placement proofs, at the reply-producer seam:

* **Byte-identical when unwired** — ``origination_gate=None`` ⇒ the gate block is skipped and the
  turn generates exactly as before (the None-inert guarantee).
* **Crisis precedence (criterion 3)** — an acute-W1 turn HARD-bypasses to the spoken safe completion
  *before* the gate; the gate is NEVER consulted (the R8 never-even-attempted form).
* **Gate owns the turn** — a recognized ask makes the gate speak the echo/confirm and the model is
  NEVER called (no generation on a delegated turn).
* **Delegation on confirm (A9-D-5/D-7)** — a clean confirm fires the delegation listener with the
  VERBATIM ask (never the mid-model draft) + conversation/persona from the session context, and
  voice speaks the "preparing in the background" line (never a "done").
* **Ordinary falls through** — the gate returning ``ordinary()`` leaves generation untouched.
"""

# ruff: noqa: ANN401, ARG002 — test doubles with intentionally loose signatures.

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from persona.backends import BackendConfig, StreamChunk, TokenUsage
from persona.history import ConversationHistoryManager
from persona.schema.chunks import PersonaChunk
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity, RoutingConfig
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry
from persona_voice.loop.streaming import Transcript
from persona_voice.model import VoiceModelReplyProducer, VoiceTurnContext
from persona_voice.model.origination_gate import DelegatedTurnIntent, VoiceOriginationDecision


def _chunk(text: str) -> PersonaChunk:
    return PersonaChunk(id=f"id-{abs(hash(text)) % 9999}", text=text, created_at=datetime.now(UTC))


class _FakeStore:
    def __init__(self, chunks: list[PersonaChunk] | None = None) -> None:
        self._chunks = chunks or []

    def write(self, persona_id: str, chunks: list[PersonaChunk], **kwargs: Any) -> None:
        self._chunks.extend(chunks)

    def query(self, persona_id: str, query: str, top_k: int, **filters: Any) -> list[PersonaChunk]:
        return list(self._chunks[:top_k])

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:
        return list(self._chunks)

    def recent(self, persona_id: str, limit: int) -> list[PersonaChunk]:
        return list(self._chunks[-limit:][::-1]) if limit > 0 else []

    def delete(self, persona_id: str) -> None:
        return None


class _ScriptedBackend:
    """A streaming backend that records whether it was called (the generation tripwire)."""

    provider_name = "anthropic"
    model_name = "test-model"
    supports_native_tools = False
    supports_vision = False

    def __init__(self, chunks: list[StreamChunk]) -> None:
        self._chunks = chunks
        self.called = False

    async def chat_stream(
        self,
        messages: list[Any],
        *,
        tools: Any = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: Any = None,
    ) -> AsyncIterator[StreamChunk]:
        self.called = True
        for chunk in self._chunks:
            yield chunk


class _FakeGate:
    """A scripted origination gate — records calls (the crisis-precedence tripwire)."""

    def __init__(self, decision: VoiceOriginationDecision) -> None:
        self._decision = decision
        self.calls: list[str] = []

    async def on_user_turn(self, transcript: str) -> VoiceOriginationDecision:
        self.calls.append(transcript)
        return self._decision

    def note_spoken_turn_committed(self, *, truncated: bool) -> None:
        return None


def _persona() -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(
            name="Astrid",
            role="assistant",
            background="Helps with everyday tasks.",
            constraints=["Be honest."],
        ),
        routing=RoutingConfig(tier_for_generation="frontier"),
    )


def _context(backend: object, *, gate: object | None = None) -> VoiceTurnContext:
    stores = {
        "identity": _FakeStore([_chunk("I am Astrid.")]),
        "self_facts": _FakeStore([_chunk("I help with tasks.")]),
        "worldview": _FakeStore([_chunk("People value reliability.")]),
        "episodic": _FakeStore([_chunk("We spoke yesterday.")]),
    }
    cfg = BackendConfig(provider="anthropic", model="test-model", api_key=None)  # type: ignore[arg-type]
    registry = TierRegistry({"frontier": TierConfig(name="frontier", backend_config=cfg)})
    registry._cache = {"frontier": backend}  # type: ignore[assignment,dict-item]  # noqa: SLF001
    return VoiceTurnContext(
        persona=_persona(),
        stores=stores,  # type: ignore[arg-type]
        conversation=Conversation(conversation_id="call-1", persona_id="astrid", messages=[]),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        origination_gate=gate,  # type: ignore[arg-type]
    )


def _final() -> StreamChunk:
    return StreamChunk(
        delta="",
        is_final=True,
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


async def _drain(producer: VoiceModelReplyProducer, text: str) -> list[str]:
    stream = await producer(Transcript(is_final=True, text=text, confidence=1.0))
    return [tok async for tok in stream]


pytestmark = pytest.mark.asyncio


async def test_gate_none_is_byte_identical_generation() -> None:
    # No gate ⇒ the block is skipped and the model generates as before.
    backend = _ScriptedBackend([StreamChunk(delta="Hi there"), _final()])
    producer = VoiceModelReplyProducer(_context(backend, gate=None))
    assert await _drain(producer, "hello") == ["Hi there"]
    assert backend.called


async def test_gate_ordinary_falls_through_to_generation() -> None:
    backend = _ScriptedBackend([StreamChunk(delta="Sure"), _final()])
    gate = _FakeGate(VoiceOriginationDecision.ordinary())
    producer = VoiceModelReplyProducer(_context(backend, gate=gate))
    assert await _drain(producer, "what's the weather") == ["Sure"]
    assert gate.calls == ["what's the weather"]
    assert backend.called  # ordinary ⇒ normal generation ran


async def test_gate_owns_turn_speaks_echo_without_calling_the_model() -> None:
    backend = _ScriptedBackend([StreamChunk(delta="SHOULD NOT SPEAK"), _final()])
    gate = _FakeGate(VoiceOriginationDecision(spoken="Here's what I'll do..."))
    producer = VoiceModelReplyProducer(_context(backend, gate=gate))
    out = await _drain(producer, "remind me every morning")
    assert out == ["Here's what I'll do..."]
    assert not backend.called  # a delegated/echo turn NEVER generates with the model


async def test_confirm_fires_delegation_with_verbatim_ask_and_speaks_preparing() -> None:
    from persona_runtime.task_origination import ContractDraft

    intents: list[DelegatedTurnIntent] = []
    backend = _ScriptedBackend([StreamChunk(delta="x"), _final()])
    decision = VoiceOriginationDecision(
        spoken="Got it — I'm setting that up in the background.",
        confirmed_draft=ContractDraft(goal="brief me on the news"),
        verbatim_ask="brief me on the news every morning",
    )
    gate = _FakeGate(decision)
    producer = VoiceModelReplyProducer(
        _context(backend, gate=gate), delegation_listener=intents.append
    )
    out = await _drain(producer, "yes")
    assert "background" in out[0].lower()  # preparing, never a "done"
    assert not backend.called
    # The VERBATIM ask is delegated (never the mid draft), with the call's conversation + persona.
    assert len(intents) == 1
    assert intents[0].verbatim_ask == "brief me on the news every morning"
    assert intents[0].conversation_id == "call-1"
    assert intents[0].persona_id == "astrid"
    assert intents[0].provenance == "voice"


async def test_confirm_without_listener_still_speaks_but_delegates_nothing() -> None:
    from persona_runtime.task_origination import ContractDraft

    backend = _ScriptedBackend([StreamChunk(delta="x"), _final()])
    decision = VoiceOriginationDecision(
        spoken="Setting that up in the background.",
        confirmed_draft=ContractDraft(goal="g"),
        verbatim_ask="remind me daily",
    )
    gate = _FakeGate(decision)
    producer = VoiceModelReplyProducer(_context(backend, gate=gate))  # no delegation_listener
    out = await _drain(producer, "yes")
    assert out
    assert not backend.called


async def test_crisis_bypass_precedes_the_gate_never_consulted() -> None:
    # An acute-W1 turn HARD-bypasses to the spoken safe completion BEFORE the gate (criterion 3).
    backend = _ScriptedBackend([StreamChunk(delta="x"), _final()])
    gate = _FakeGate(VoiceOriginationDecision(spoken="SHOULD NOT ECHO"))
    producer = VoiceModelReplyProducer(_context(backend, gate=gate))
    out = await _drain(producer, "i want to kill myself tonight")
    assert out  # the spoken safe-completion variant was produced
    assert "SHOULD NOT ECHO" not in out[0]
    assert gate.calls == []  # origination NEVER consulted on a crisis turn
    assert not backend.called


# --- T8: the confabulation guard on a NON-delegated voice turn (A9-D-6) ---------------------


async def test_non_delegated_reply_claiming_a_schedule_is_corrected() -> None:
    # An ORDINARY (gate-ordinary) turn whose reply falsely claims a create → the honest correction
    # is appended (spoken tail → heard → folded into episodic; the graph never learns a false fact).
    backend = _ScriptedBackend(
        [StreamChunk(delta="I've scheduled that every morning for you."), _final()]
    )
    gate = _FakeGate(VoiceOriginationDecision.ordinary())
    producer = VoiceModelReplyProducer(_context(backend, gate=gate))
    out = "".join(await _drain(producer, "can you check the news every morning"))
    assert "haven't actually created a schedule" in out  # the A10 correction, verbatim
    assert backend.called  # this WAS an ordinary generation turn


async def test_non_delegated_reply_without_a_claim_is_untouched() -> None:
    backend = _ScriptedBackend([StreamChunk(delta="Sure, the news looks quiet today."), _final()])
    producer = VoiceModelReplyProducer(_context(backend, gate=None))
    out = "".join(await _drain(producer, "what's the news"))
    assert "haven't actually created" not in out  # no claim ⇒ no correction
    assert out == "Sure, the news looks quiet today."


async def test_delegated_turn_never_reaches_the_confab_guard() -> None:
    # A gate-owned (delegated) turn early-returns before generation, so the confab guard — which
    # runs only on the ordinary path — never sees it (the delegated "preparing" line is no claim;
    # the grounded "done" comes from the real hand-back, not the mid model).
    backend = _ScriptedBackend([StreamChunk(delta="I've scheduled it."), _final()])
    gate = _FakeGate(VoiceOriginationDecision(spoken="Setting that up in the background."))
    producer = VoiceModelReplyProducer(_context(backend, gate=gate))
    out = "".join(await _drain(producer, "remind me every morning"))
    assert out == "Setting that up in the background."  # the gate owns it; no generation, no guard
    assert not backend.called


# --- T10: fail-soft — a gate error degrades to today's clean call ----------------------------


async def test_gate_error_falls_through_to_a_clean_ordinary_turn() -> None:
    class _BoomGate:
        async def on_user_turn(self, transcript: str) -> VoiceOriginationDecision:  # noqa: ARG002
            raise RuntimeError("keyless / over-budget judge blew up")

        def note_spoken_turn_committed(self, *, truncated: bool) -> None:  # noqa: ARG002
            return None

    backend = _ScriptedBackend([StreamChunk(delta="Sure, here's the news."), _final()])
    producer = VoiceModelReplyProducer(_context(backend, gate=_BoomGate()))
    out = "".join(await _drain(producer, "brief me every morning"))
    assert out == "Sure, here's the news."  # today's clean call — the gate failure never breaks it
    assert backend.called


# --------------------------------------------------------------------------- #
# R9-128: the gate's judge runs on the live path, so it gets a budget
# --------------------------------------------------------------------------- #
class _SlowGate:
    """A gate whose judge never comes back in time (a slow small-tier model)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def on_user_turn(self, transcript: str) -> VoiceOriginationDecision:
        self.calls.append(transcript)
        await asyncio.sleep(3600)
        return VoiceOriginationDecision(spoken="TOO LATE")

    def note_spoken_turn_committed(self, *, truncated: bool) -> None: ...


async def test_a_judge_past_its_budget_yields_an_ordinary_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller waited 14.7s for first audio while the judge thought. Past the budget
    the turn is answered as ordinary conversation, and the gate's late verdict is
    never spoken."""
    from persona_voice.model import reply_producer as producer_mod

    monkeypatch.setattr(producer_mod, "GATE_BUDGET_S", 0.05)
    backend = _ScriptedBackend([StreamChunk(delta="Sure"), _final()])
    gate = _SlowGate()
    producer = VoiceModelReplyProducer(_context(backend, gate=gate))

    out = await asyncio.wait_for(_drain(producer, "remind me every morning"), timeout=2.0)

    assert out == ["Sure"]
    assert backend.called
    assert gate.calls == ["remind me every morning"]
