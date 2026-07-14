"""Unit tests for persona_voice.model.VoicePromptAssembler (spec V5 T2).

Two things matter: (1) the assembled voice prompt carries the FULL persona
conditioning via the shared ``PromptBuilder`` — identity, constraints, and the
retrieved typed memory all present (criteria 1+2; the anti-bypass line); and
(2) the session-constant identity store-read is cached (D-V5-1) while the
variable stores are re-queried every turn.
"""

# ruff: noqa: ARG002 — store double with intentionally loose signatures.

from __future__ import annotations

from datetime import UTC, datetime

from persona.history import ConversationHistoryManager
from persona.schema.chunks import PersonaChunk
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry
from persona_voice.model import VoicePromptAssembler, VoiceTurnContext


def _chunk(text: str, meta: dict[str, str] | None = None) -> PersonaChunk:
    return PersonaChunk(
        id=f"id-{abs(hash(text)) % 10000}",
        text=text,
        metadata=meta or {},
        created_at=datetime.now(UTC),
    )


class _FakeStore:
    """In-memory store double: get_all serves identity, query serves the rest."""

    def __init__(
        self,
        *,
        all_chunks: list[PersonaChunk] | None = None,
        query_chunks: list[PersonaChunk] | None = None,
    ) -> None:
        self._all = all_chunks or []
        self._query = query_chunks or []
        self.get_all_calls = 0
        self.query_calls = 0

    def write(self, persona_id: str, chunks: list[PersonaChunk], **kwargs: object) -> None:
        self._all.extend(chunks)

    def query(
        self, persona_id: str, query: str, top_k: int, **filters: object
    ) -> list[PersonaChunk]:
        self.query_calls += 1
        return list(self._query[:top_k])

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:
        self.get_all_calls += 1
        return list(self._all)

    def recent(self, persona_id: str, limit: int) -> list[PersonaChunk]:
        return list(self._all[-limit:][::-1]) if limit > 0 else []

    def delete(self, persona_id: str) -> None:
        return None


def _persona() -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(
            name="Astrid",
            role="Norwegian tenancy law assistant",
            background="Knows husleieloven.",
            constraints=["Never give binding legal advice."],
        ),
    )


def _context() -> tuple[VoiceTurnContext, dict[str, _FakeStore]]:
    from persona.backends import BackendConfig

    stores = {
        "identity": _FakeStore(all_chunks=[_chunk("I am Astrid, a tenancy assistant.")]),
        "self_facts": _FakeStore(query_chunks=[_chunk("I specialise in tenancy law.")]),
        "worldview": _FakeStore(
            query_chunks=[_chunk("Tenants have strong protections.", {"epistemic": "fact"})]
        ),
        "episodic": _FakeStore(query_chunks=[_chunk("Last time we discussed mould.")]),
    }
    cfg = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]
    ctx = VoiceTurnContext(
        persona=_persona(),
        stores=stores,  # type: ignore[arg-type]
        conversation=Conversation(conversation_id="c1", persona_id="astrid", messages=[]),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=TierRegistry({"frontier": TierConfig(name="frontier", backend_config=cfg)}),
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
    )
    return ctx, stores


class TestFullConditioning:
    def test_assembled_prompt_carries_identity_constraints_and_memory(self) -> None:
        ctx, _ = _context()
        assembler = VoicePromptAssembler(ctx)

        msgs = assembler.build("What are my rights?", history=[], max_tokens=8000)
        system = msgs[0].content
        assert isinstance(system, str)

        # The anti-bypass line: full conditioning via the shared PromptBuilder.
        assert "You are Astrid" in system
        assert "You must NOT:" in system  # constraints enforced
        assert "Never give binding legal advice." in system
        assert "Relevant facts about yourself:" in system  # self_facts retrieved
        assert "Your views:" in system  # worldview retrieved
        assert "From earlier conversations:" in system  # episodic retrieved

    def test_message_shape_is_system_history_user(self) -> None:
        ctx, _ = _context()
        assembler = VoicePromptAssembler(ctx)
        msgs = assembler.build("hi", history=[], max_tokens=8000)
        assert msgs[0].role == "system"
        assert msgs[-1].role == "user"
        assert msgs[-1].content == "hi"

    def test_voice_assembler_builds_in_voice_mode(self) -> None:
        """The voice path renders the voice-mode register (Spec V11, V11-D-1).

        The shared ``PromptBuilder`` is the one source, but the voice assembler
        passes ``mode=VOICE`` so the spoken-delivery register is present — the
        whole point of V11 (voice ≠ chat, no longer a mirror).
        """
        ctx, _ = _context()
        assembler = VoicePromptAssembler(ctx)
        system = assembler.build("What are my rights?", history=[], max_tokens=8000)[0].content
        assert isinstance(system, str)
        assert "read aloud" in system  # the voice-register marker


class TestConstantBlockCaching:
    def test_identity_store_read_once_across_turns(self) -> None:
        ctx, stores = _context()
        assembler = VoicePromptAssembler(ctx)

        assembler.build("turn one", history=[], max_tokens=8000)
        assembler.build("turn two", history=[], max_tokens=8000)
        assembler.build("turn three", history=[], max_tokens=8000)

        assert stores["identity"].get_all_calls == 1  # cached (D-V5-1)

    def test_variable_stores_queried_every_turn(self) -> None:
        ctx, stores = _context()
        assembler = VoicePromptAssembler(ctx)

        assembler.build("turn one", history=[], max_tokens=8000)
        assembler.build("turn two", history=[], max_tokens=8000)

        assert stores["self_facts"].query_calls == 2
        assert stores["worldview"].query_calls == 2
        assert stores["episodic"].query_calls == 2


class TestReinforceWiring:
    """Spec K8 (K8-D-5): the voice retrieval worker reinforces recalled episodic.

    ``retrieve`` runs inside the reply producer's ``asyncio.to_thread`` worker,
    so the synchronous reinforce here executes OFF the voice event loop by
    construction (the starvation rule). A store double without the command is
    a silent no-op (fail-soft) — proven by every other test in this file
    passing with the plain ``_FakeStore``.
    """

    def test_retrieve_reinforces_the_recalled_episodic_ids(self) -> None:
        ctx, stores = _context()
        recalls: list[tuple[str, list[str]]] = []
        stores["episodic"].reinforce = (  # type: ignore[attr-defined]
            lambda persona_id, chunk_ids: recalls.append((persona_id, chunk_ids))
        )
        assembler = VoicePromptAssembler(ctx)
        context = assembler.retrieve("mould again?")
        assert recalls == [("astrid", [c.id for c in context.episodic])]


def _norwegian_persona() -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(
            name="Astrid",
            role="Norwegian tenancy law assistant",
            background="Knows husleieloven.",
            language_default="nb",
        ),
    )


class TestReplyLanguageMirror:
    """Spec V14 (D-V14-12): the assembler selects the B5 mirror directive under an
    utterance-level TTS provider (``ctx.reply_language_mirror``)."""

    def test_mirror_flag_threads_into_the_mirror_directive(self) -> None:
        import dataclasses

        ctx, _ = _context()
        ctx = dataclasses.replace(ctx, persona=_norwegian_persona(), reply_language_mirror=True)
        system = VoicePromptAssembler(ctx).build("hei", history=[], max_tokens=8000)[0].content
        assert isinstance(system, str)
        lowered = system.lower()
        assert "same language the user writes or speaks in" in lowered  # mirror
        assert "always respond in" not in lowered  # NOT the pin

    def test_default_is_the_pin_directive(self) -> None:
        import dataclasses

        ctx, _ = _context()
        ctx = dataclasses.replace(ctx, persona=_norwegian_persona())  # mirror defaults False
        system = VoicePromptAssembler(ctx).build("hei", history=[], max_tokens=8000)[0].content
        lowered = system.lower()
        assert "always respond in norwegian" in lowered  # the pin
        assert "same language the user writes" not in lowered
