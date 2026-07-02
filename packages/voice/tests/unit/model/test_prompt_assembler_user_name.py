"""The voice path speaks the caller's name (Spec K6, K6-D-6, T4b).

A persona that knows the user's name in chat must know it in a call too (one
persona, one user). The name is resolved once at session setup and rides the
shared ``PromptBuilder`` in VOICE mode; a nameless caller is byte-identical.
"""

# ruff: noqa: ARG002 — store double with intentionally loose signatures.

from __future__ import annotations

from datetime import UTC, datetime

from persona.backends import BackendConfig
from persona.history import ConversationHistoryManager
from persona.schema.chunks import PersonaChunk
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry
from persona_voice.model import VoicePromptAssembler, VoiceTurnContext


class _FakeStore:
    def __init__(self, all_chunks: list[PersonaChunk] | None = None) -> None:
        self._all = all_chunks or []

    def query(
        self, persona_id: str, query: str, top_k: int, **filters: object
    ) -> list[PersonaChunk]:
        return []

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:
        return list(self._all)

    def recent(self, persona_id: str, limit: int) -> list[PersonaChunk]:
        return []


def _ctx(*, user_name: str | None) -> VoiceTurnContext:
    ident = PersonaChunk(id="i1", text="I am Astrid.", metadata={}, created_at=datetime.now(UTC))
    stores = {k: _FakeStore() for k in ("self_facts", "worldview", "episodic")}
    stores["identity"] = _FakeStore(all_chunks=[ident])
    cfg = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]
    return VoiceTurnContext(
        persona=Persona(
            persona_id="astrid",
            identity=PersonaIdentity(name="Astrid", role="assistant", background="bg"),
        ),
        stores=stores,  # type: ignore[arg-type]
        conversation=Conversation(conversation_id="c1", persona_id="astrid", messages=[]),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=TierRegistry({"frontier": TierConfig(name="frontier", backend_config=cfg)}),
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        user_name=user_name,
    )


def _system(user_name: str | None) -> str:
    msgs = VoicePromptAssembler(_ctx(user_name=user_name)).build("hi", history=[], max_tokens=8000)
    content = msgs[0].content
    assert isinstance(content, str)
    return content


def test_voice_speaks_the_caller_name() -> None:
    assert "You are speaking with Ada Lovelace." in _system("Ada Lovelace")


def test_nameless_voice_prompt_is_byte_identical() -> None:
    baseline = _system(None)
    assert "speaking with" not in baseline
    # Naming adds exactly the one line (parts join with a blank line); removing it
    # recovers the nameless prompt.
    named = _system("Ada Lovelace")
    assert named.replace("\n\nYou are speaking with Ada Lovelace.", "", 1) == baseline
