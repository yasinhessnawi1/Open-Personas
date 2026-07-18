"""Unified voice→episodic memory write (spec V5 T8; D-V5-X-memory-write-on-commit).

A voice call is a conversation in the *same* model as a text chat (Spec 01/07):
its turns are written to the *same* episodic store, so something said by voice is
remembered when the user next types, and vice versa (criterion 3 — one continuous
persona, no separate voice store).

The write is **on commit only** (D-V5-X-memory-write-on-commit, the cancel↔write
race fix): V4's loop emits the per-turn ``HeardReply`` in its ``finally`` *after*
barge-in cancellation resolves, which the ``HeardWordsBridge`` adapts to a
:class:`~persona_voice.turn_taking.heard_words.BargedReply` and forwards to this
listener's :meth:`on_reply_committed`. So memory records what was *heard*
(truncated-as-heard on a barge-in), never what was *planned* (D-V4-4) — race-safe
by construction, because there is never a speculative mid-stream write.

The user side of the turn is correlated by turn key: the
:class:`~persona_voice.model.reply_producer.VoiceModelReplyProducer` calls
:meth:`note_user_message` with the transcribed user turn at generation start; the
matching :meth:`on_reply_committed` pairs it with the heard reply into one
combined episodic chunk (the same ``USER:…/ASSISTANT:…`` shape the text loop
writes — unified memory).

History compaction is scheduled here, in the inter-turn gap after the turn is
recorded — off the user-stops→persona-starts critical path (D-V5-3).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from persona.logging import get_logger
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id
from persona.schema.conversation import ConversationMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from persona_voice.model.history import VoiceHistoryCompactor
    from persona_voice.model.transcript import VoiceTranscriptWriter
    from persona_voice.model.turn_context import VoiceTurnContext
    from persona_voice.turn_taking.heard_words import BargedReply

__all__ = ["VoiceTurnRecorder"]

_LOG = get_logger("voice.memory")
_WRITTEN_BY = "voice.turn"


def _default_scheduler(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Schedule a background coroutine on the running loop (fire-and-forget)."""
    return asyncio.create_task(coro)


class VoiceTurnRecorder:
    """Writes the heard voice turn to the unified episodic store (D-V5-X).

    Implements V4's ``TurnTranscriptListener`` seam. Construct once per voice
    session over the :class:`VoiceTurnContext`.

    Args:
        context: The session-bound runtime collaborators (stores, conversation).
        compactor: Optional off-critical-path history compactor (D-V5-3). When
            given with ``summariser``, compaction is scheduled after each recorded
            turn if due.
        summariser: Optional async small-tier summariser for background compaction.
        scheduler: Schedules the background compaction coroutine; defaults to
            ``asyncio.create_task``. Injectable for deterministic tests.
        clock: UTC-now provider (injected for deterministic tests).
    """

    def __init__(
        self,
        context: VoiceTurnContext,
        *,
        compactor: VoiceHistoryCompactor | None = None,
        summariser: Callable[[list[ConversationMessage]], Awaitable[str]] | None = None,
        scheduler: Callable[[Coroutine[Any, Any, Any]], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        transcript_writer: VoiceTranscriptWriter | None = None,
    ) -> None:
        self._ctx = context
        self._compactor = compactor
        self._summariser = summariser
        self._schedule = scheduler or _default_scheduler
        self._clock = clock or (lambda: datetime.now(UTC))
        # V9 (V9-D-1/D-2): persists each committed turn to the durable ``messages``
        # transcript (byte-for-byte with a chat turn). Optional — None on any path
        # that doesn't persist a transcript (e.g. unit tests / community voice).
        self._transcript_writer = transcript_writer
        self._pending_user: str | None = None
        # R9-001: whether the pending "user" text is an internally-originated
        # prompt (the turn-0 greeting nudge / a coalesced narration), not the
        # caller's speech. Set alongside the correlation key; read at commit.
        self._pending_user_synthetic = False
        # Hold references to background compaction tasks so they are not GC'd
        # mid-flight (the standard asyncio fire-and-forget guard).
        self._bg_tasks: set[Any] = set()

    def note_user_message(self, text: str, *, synthetic: bool = False) -> None:
        """Record this turn's transcribed user message (correlation key).

        Args:
            text: the user-side prompt of this turn.
            synthetic: ``True`` when the "user" side is an internally-originated
                prompt riding the producer path (the turn-0 greeting nudge, a
                coalesced narration) rather than the caller's speech (R9-001).
                A synthetic turn persists its ASSISTANT half only — the internal
                instruction must never appear in the user-facing transcript or
                be minted into episodic memory as something the user said.
        """
        self._pending_user = text
        self._pending_user_synthetic = synthetic

    async def on_reply_committed(self, reply: BargedReply) -> None:
        """Write the heard turn to episodic memory (V4 calls this on commit).

        MUST NOT raise (V4 runs this in the invocation's ``finally``, including on
        barge-in). Writes the combined user/heard-assistant chunk to the unified
        episodic store, appends both messages to the live conversation, and
        schedules off-path compaction if due. A no-op if no user turn was noted
        (nothing to correlate).
        """
        user = self._pending_user
        synthetic = self._pending_user_synthetic
        self._pending_user = None
        self._pending_user_synthetic = False
        if user is None:
            return

        heard = reply.heard_text
        # Persist to the unified episodic store — BEST-EFFORT. This method MUST
        # NOT raise (V4 runs it in the invocation's ``finally``): a write failure
        # — e.g. a misconfigured/un-migrated database (no ``memory_chunks``) —
        # must not crash the turn or surface as an unretrieved task exception.
        # The live-history append below keeps in-session continuity regardless.
        try:
            self._write_episodic(user, heard, synthetic=synthetic)
        except Exception as exc:  # noqa: BLE001 — episodic persistence is best-effort
            _LOG.warning(
                "voice episodic write failed (persona_id={pid}): {err}",
                pid=self._ctx.persona_id,
                err=repr(exc)[:300],
            )

        now = self._clock()
        # The in-memory live history keeps BOTH halves even on a synthetic turn:
        # it is prompt context only (lost at teardown, never rendered), and the
        # model should keep seeing why it spoke unprompted. Only the DURABLE
        # surfaces below (episodic + messages transcript) suppress the synthetic
        # user half (R9-001).
        self._ctx.conversation.messages.append(
            ConversationMessage(role="user", content=user, created_at=now)
        )
        # The assistant message records what was HEARD (truncated-as-heard on a
        # barge-in), so the conversation history is honest too (D-V4-4).
        self._ctx.conversation.messages.append(
            ConversationMessage(
                role="assistant",
                content=heard,
                created_at=now,
                metadata={"modality": "voice", "truncated": str(reply.truncated).lower()},
            )
        )
        # V9 (V9-D-1/D-2): ALSO persist the turn to the durable ``messages``
        # transcript (the in-memory append above is lost at teardown; episodic is
        # persona-scoped and can't be grouped per call). Best-effort by construction
        # (the writer never raises) — same discipline as the episodic write above.
        if self._transcript_writer is not None:
            # R9-001: a synthetic turn persists the assistant row ONLY — the
            # internal instruction never becomes a user message bubble.
            self._transcript_writer.record_turn(
                user_text=None if synthetic else user,
                heard_text=heard,
                truncated=reply.truncated,
                now=now,
            )
        self._maybe_schedule_compaction()

    def _write_episodic(self, user_text: str, heard_text: str, *, synthetic: bool = False) -> None:
        """Write one combined episodic chunk per turn (mirrors the text loop).

        Minted uuidv7 id (K8-D-6), never a store-count index — the count read
        was O(N) per write and raced against the concurrent chat writer.

        A synthetic turn (R9-001) writes the ASSISTANT half only: the internal
        prompt must not be minted into memory as something the user said (it
        could otherwise resurface via retrieval or graph extraction).

        Stamps ``metadata["conversation_id"]`` from the session's live
        ``VoiceTurnContext.conversation`` (Spec K11, D-K11-9) — always in scope
        here (the context's ``conversation`` field is required, never optional),
        so a voice-born chunk cascades on ``DELETE .../conversations/{id}
        ?forget_memory=true`` exactly like a text-turn chunk does.
        """
        persona_id = self._ctx.persona_id
        store = self._ctx.stores["episodic"]
        chunk_id = mint_chunk_id(persona_id, "episodic")
        now = self._clock()
        text = (
            f"ASSISTANT: {heard_text}"
            if synthetic
            else f"USER: {user_text}\nASSISTANT: {heard_text}"
        )
        store.write(
            persona_id,
            [
                PersonaChunk(
                    id=chunk_id,
                    text=text,
                    metadata={
                        "importance": "0.5",
                        "modality": "voice",
                        "conversation_id": self._ctx.conversation.conversation_id,
                    },
                    created_at=now,
                    provenance=ChunkProvenance(
                        source=WriteSource.SYSTEM,
                        logical_id=chunk_id,
                        version=1,
                        written_at=now,
                        written_by=_WRITTEN_BY,
                    ),
                ),
            ],
            source=WriteSource.SYSTEM,
            written_by=_WRITTEN_BY,
        )

    def _maybe_schedule_compaction(self) -> None:
        """Schedule off-critical-path compaction if due (D-V5-3)."""
        if self._compactor is None or self._summariser is None:
            return
        if not self._compactor.is_compaction_due(self._ctx.conversation):
            return
        task = self._schedule(self._compactor.compact(self._ctx.conversation, self._summariser))
        # Keep a reference until done so the task is not garbage-collected.
        if isinstance(task, asyncio.Task):
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
