"""Unit tests for persona_voice.model.VoiceTurnRecorder (spec V5 T8; D-V5-X).

The unified-memory write: on commit, the heard turn is written to the *same*
episodic store as text (criterion 3), correlated to the noted user transcript;
the assistant content is what was *heard* (truncated-as-heard on barge-in —
D-V4-4), never the planned reply. Compaction is scheduled off the critical path.
"""

# ruff: noqa: ANN401, ARG001, ARG002 — doubles with intentionally loose signatures.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from persona.history import ConversationHistoryManager
from persona.schema.chunks import PersonaChunk
from persona.schema.conversation import Conversation, ConversationMessage
from persona.schema.persona import Persona, PersonaIdentity
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry
from persona_voice.model import VoiceHistoryCompactor, VoiceTurnContext, VoiceTurnRecorder
from persona_voice.turn_taking.heard_words import BargedReply


class _RecordingStore:
    """In-memory episodic store double that records written chunks."""

    def __init__(self) -> None:
        self.chunks: list[PersonaChunk] = []

    def write(self, persona_id: str, chunks: list[PersonaChunk], **kwargs: Any) -> None:
        self.chunks.extend(chunks)

    def query(self, persona_id: str, query: str, top_k: int, **filters: Any) -> list[PersonaChunk]:
        return list(self.chunks[:top_k])

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:
        return list(self.chunks)

    def recent(self, persona_id: str, limit: int) -> list[PersonaChunk]:
        return list(self.chunks[-limit:][::-1]) if limit > 0 else []

    def delete(self, persona_id: str) -> None:
        return None


class _FailingEpisodicStore(_RecordingStore):
    """Episodic double whose persistence raises — e.g. an un-migrated DB where
    ``memory_chunks`` does not exist (the failure seen in the field)."""

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:
        raise RuntimeError('relation "memory_chunks" does not exist')


def _context(
    *, messages: list[ConversationMessage] | None = None
) -> tuple[VoiceTurnContext, _RecordingStore]:
    from persona.backends import BackendConfig

    episodic = _RecordingStore()
    stores = {
        "identity": _RecordingStore(),
        "self_facts": _RecordingStore(),
        "worldview": _RecordingStore(),
        "episodic": episodic,
    }
    cfg = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]
    ctx = VoiceTurnContext(
        persona=Persona(
            persona_id="astrid",
            identity=PersonaIdentity(
                name="Astrid", role="assistant", background="x", constraints=[]
            ),
        ),
        stores=stores,  # type: ignore[arg-type]
        conversation=Conversation(
            conversation_id="c1", persona_id="astrid", messages=messages or []
        ),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=TierRegistry({"frontier": TierConfig(name="frontier", backend_config=cfg)}),
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
    )
    return ctx, episodic


class TestUnifiedWrite:
    @pytest.mark.asyncio
    async def test_writes_combined_chunk_to_episodic_store(self) -> None:
        ctx, episodic = _context()
        recorder = VoiceTurnRecorder(ctx)
        recorder.note_user_message("What are my rights?")

        await recorder.on_reply_committed(
            BargedReply(heard_text="You have strong rights.", truncated=False, token_count=4)
        )

        assert len(episodic.chunks) == 1
        text = episodic.chunks[0].text
        assert "USER: What are my rights?" in text
        assert "ASSISTANT: You have strong rights." in text
        assert episodic.chunks[0].metadata.get("modality") == "voice"

    @pytest.mark.asyncio
    async def test_appends_user_and_heard_assistant_to_conversation(self) -> None:
        ctx, _ = _context()
        recorder = VoiceTurnRecorder(ctx)
        recorder.note_user_message("hi")

        await recorder.on_reply_committed(
            BargedReply(heard_text="hello there", truncated=False, token_count=2)
        )

        msgs = ctx.conversation.messages
        assert [m.role for m in msgs] == ["user", "assistant"]
        assert msgs[0].content == "hi"
        assert msgs[1].content == "hello there"
        assert msgs[1].metadata.get("modality") == "voice"

    @pytest.mark.asyncio
    async def test_no_user_noted_writes_nothing(self) -> None:
        ctx, episodic = _context()
        recorder = VoiceTurnRecorder(ctx)
        # No note_user_message → nothing to correlate.
        await recorder.on_reply_committed(
            BargedReply(heard_text="orphan reply", truncated=False, token_count=2)
        )
        assert episodic.chunks == []
        assert ctx.conversation.messages == []


class _RecordingTranscriptWriter:
    """Transcript-writer double — records the exact ``record_turn`` kwargs."""

    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    def record_turn(
        self, *, user_text: str | None, heard_text: str, truncated: bool, now: datetime
    ) -> None:
        self.turns.append(
            {"user_text": user_text, "heard_text": heard_text, "truncated": truncated}
        )


class TestSyntheticTurnSuppression:
    """R9-001 — a synthetic turn (the greeting nudge / a narration prompt)
    persists its ASSISTANT half only, at the persistence boundary."""

    @pytest.mark.asyncio
    async def test_synthetic_turn_persists_assistant_only(self) -> None:
        ctx, episodic = _context()
        writer = _RecordingTranscriptWriter()
        recorder = VoiceTurnRecorder(ctx, transcript_writer=writer)  # type: ignore[arg-type]
        nudge = "(The voice call has just connected. Greet the person.)"
        recorder.note_user_message(nudge, synthetic=True)

        await recorder.on_reply_committed(
            BargedReply(heard_text="Hei! So glad you called.", truncated=False, token_count=5)
        )

        # Durable transcript: assistant row only — the nudge never becomes a user bubble.
        assert writer.turns == [
            {"user_text": None, "heard_text": "Hei! So glad you called.", "truncated": False}
        ]
        # Episodic memory: the greeting is remembered, the nudge is not minted as
        # something the user said.
        assert len(episodic.chunks) == 1
        assert episodic.chunks[0].text == "ASSISTANT: Hei! So glad you called."
        assert nudge not in episodic.chunks[0].text
        # In-memory live history keeps both halves (prompt context only, never
        # rendered) — the model still sees why it spoke unprompted.
        assert [m.role for m in ctx.conversation.messages] == ["user", "assistant"]

    @pytest.mark.asyncio
    async def test_synthetic_flag_resets_so_the_next_turn_persists_the_pair(self) -> None:
        ctx, episodic = _context()
        writer = _RecordingTranscriptWriter()
        recorder = VoiceTurnRecorder(ctx, transcript_writer=writer)  # type: ignore[arg-type]

        recorder.note_user_message("(greet them)", synthetic=True)
        await recorder.on_reply_committed(
            BargedReply(heard_text="Hello!", truncated=False, token_count=1)
        )
        # The next REAL turn is unaffected: user half persists again.
        recorder.note_user_message("what are my rights?")
        await recorder.on_reply_committed(
            BargedReply(heard_text="Strong ones.", truncated=False, token_count=2)
        )

        assert writer.turns[1]["user_text"] == "what are my rights?"
        assert "USER: what are my rights?" in episodic.chunks[1].text


class TestEpisodicWriteFailSoft:
    @pytest.mark.asyncio
    async def test_write_failure_does_not_raise_and_history_still_appends(self) -> None:
        ctx, _ = _context()
        # An un-migrated/misconfigured DB: the episodic persistence raises.
        ctx.stores["episodic"] = _FailingEpisodicStore()  # type: ignore[index]
        recorder = VoiceTurnRecorder(ctx)
        recorder.note_user_message("remember this")

        # MUST NOT raise (V4 calls this in finally) — the turn survives a memory
        # write failure rather than crashing as an unretrieved task exception.
        await recorder.on_reply_committed(
            BargedReply(heard_text="noted", truncated=False, token_count=1)
        )

        # In-session continuity is preserved even though persistence failed.
        assert [m.content for m in ctx.conversation.messages] == ["remember this", "noted"]


class TestBargeOverHonesty:
    @pytest.mark.asyncio
    async def test_records_heard_prefix_not_planned_on_truncation(self) -> None:
        ctx, episodic = _context()
        recorder = VoiceTurnRecorder(ctx)
        recorder.note_user_message("tell me everything")

        # Barge-in: only "The first part" was heard before interruption.
        await recorder.on_reply_committed(
            BargedReply(heard_text="The first part", truncated=True, token_count=3)
        )

        text = episodic.chunks[0].text
        assert "ASSISTANT: The first part" in text  # heard prefix only
        assert ctx.conversation.messages[1].content == "The first part"
        assert ctx.conversation.messages[1].metadata.get("truncated") == "true"


class TestOffCriticalPathCompaction:
    @pytest.mark.asyncio
    async def test_schedules_compaction_when_due(self) -> None:
        # 16 prior messages → compaction is due after this turn is appended.
        prior = [
            ConversationMessage(role="user", content=f"t{i}", created_at=datetime.now(UTC))
            for i in range(16)
        ]
        ctx, _ = _context(messages=prior)
        scheduled: list[Any] = []

        async def summariser(messages: list[ConversationMessage]) -> str:
            return "summary"

        recorder = VoiceTurnRecorder(
            ctx,
            compactor=VoiceHistoryCompactor(ctx.history_manager),
            summariser=summariser,
            scheduler=scheduled.append,  # capture the coro instead of running it
        )
        recorder.note_user_message("another turn")

        await recorder.on_reply_committed(
            BargedReply(heard_text="ok", truncated=False, token_count=1)
        )

        assert len(scheduled) == 1  # compaction scheduled off-path, not awaited inline
        scheduled[0].close()  # clean up the un-run coroutine

    @pytest.mark.asyncio
    async def test_no_compaction_scheduled_for_short_conversation(self) -> None:
        ctx, _ = _context()
        scheduled: list[Any] = []

        async def summariser(messages: list[ConversationMessage]) -> str:
            return "summary"

        recorder = VoiceTurnRecorder(
            ctx,
            compactor=VoiceHistoryCompactor(ctx.history_manager),
            summariser=summariser,
            scheduler=scheduled.append,
        )
        recorder.note_user_message("hi")
        await recorder.on_reply_committed(
            BargedReply(heard_text="yo", truncated=False, token_count=1)
        )

        assert scheduled == []
