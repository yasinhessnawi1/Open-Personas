"""Durable transcript writer — voice turns → the ``messages`` table (V9-D-1/D-2).

Before V9 a spoken turn reached only the persona-scoped episodic store
(``memory_chunks`` — no ``conversation_id``, so a per-call transcript can't be
grouped from it, V9-D-1) and the in-memory history (lost at teardown). It never
reached ``messages``, so a call left an EMPTY conversation under D-V7-7's
``/chat/{id}`` recap. :class:`VoiceTranscriptWriter` closes that gap: it persists
each committed turn (user STT + persona heard text) as real ``messages`` rows,
**byte-for-byte identical to a finalized chat turn** (V9-D-2) so the transcript
renders under the same thread/recap UI with no voice special-casing.

API-free: writes the api-owned ``messages`` table through core's own ``messages``
view (:data:`persona.transcript.messages`) on the session RLS engine — the
``memory_chunks`` / ``calls`` P2 precedent, no persona-api import. The write is
**best-effort** (mirrors the episodic write in :mod:`persona_voice.model.memory`,
which runs in V4's ``finally``): it MUST NOT raise — a transcript-write failure
degrades the saved transcript, never the live call.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.transcript import messages as _messages
from sqlalchemy import insert

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = ["VoiceTranscriptWriter"]

_LOG = get_logger("voice.transcript")

# Deterministic user-before-assistant ordering within one turn (mirrors the chat
# sink's ``now + timedelta(microseconds=1)`` for the assistant row).
_ONE_MICROSECOND = timedelta(microseconds=1)

#: The voice-origin marker carried in the existing ``channel`` JSONB (V9-D-4 —
#: reuse the column, no new ``messages.modality``). A render hint for the
#: transcript view; the transcript groups by the call-record time window, not this.
_VOICE_CHANNEL: dict[str, str] = {"modality": "voice"}


class VoiceTranscriptWriter:
    """Persists committed voice turns to ``messages`` (best-effort, byte-for-byte).

    Construct once per session over the session's RLS engine + ``conversation_id``
    (shared with chat, D-V5-6). Called from the per-turn commit hook while the
    call is live (the session engine is alive — turns commit before teardown), so
    no dedicated engine is needed (unlike the teardown-time call-record).

    Args:
        engine: The session's RLS-scoped sync engine (``app.current_user_id`` set
            per connection, so the ``messages`` RLS ``WITH CHECK`` — owner of the
            conversation — passes).
        conversation_id: The conversation these turns belong to.
    """

    def __init__(self, *, engine: Engine, conversation_id: str) -> None:
        self._engine = engine
        self._conversation_id = conversation_id

    def record_turn(
        self, *, user_text: str | None, heard_text: str, truncated: bool, now: datetime
    ) -> None:
        """Persist one committed turn as a user + an assistant ``messages`` row.

        Byte-for-byte with :class:`~persona_api.services.chat_turn_sink.MessagesTurnSink`
        (V9-D-2): ``msg_{uuid4}`` ids, the assistant row offset ``+1µs`` for a
        deterministic user-before-assistant order, the voice marker on ``channel``.
        Unlike the chat sink the assistant row is written ALREADY COMPLETE (voice
        turns are atomic, not streamed): ``streaming_status`` stays ``NULL`` (a
        finalized/legacy row — never ``'running'``, so the one-active-turn unique
        index is untouched) and ``originated`` defaults ``false`` (a solicited
        turn). Best-effort — MUST NOT raise.

        ``user_text=None`` (R9-001) persists the ASSISTANT row only — a synthetic
        turn (the turn-0 greeting nudge / a coalesced narration prompt) has no
        real user speech, so no user row is written and the internal instruction
        never renders as a user bubble. An assistant-only row is an established
        ``messages`` shape (originated/proactive chat turns), so no consumer
        assumes strict user/assistant pairing; the ``+1µs`` offset is kept
        unconditionally so ordering stays deterministic either way.
        """
        try:
            with self._engine.begin() as conn:
                if user_text is not None:
                    conn.execute(
                        insert(_messages).values(
                            id=f"msg_{uuid.uuid4().hex}",
                            conversation_id=self._conversation_id,
                            role="user",
                            content=user_text,
                            created_at=now,
                            channel=dict(_VOICE_CHANNEL),
                        )
                    )
                conn.execute(
                    insert(_messages).values(
                        id=f"msg_{uuid.uuid4().hex}",
                        conversation_id=self._conversation_id,
                        role="assistant",
                        content=heard_text,
                        created_at=now + _ONE_MICROSECOND,
                        channel={**_VOICE_CHANNEL, "truncated": str(truncated).lower()},
                    )
                )
        except Exception as exc:  # noqa: BLE001 — transcript persistence is best-effort
            _LOG.warning(
                "voice transcript write failed (conversation_id={cid}): {err}",
                cid=self._conversation_id,
                err=repr(exc)[:300],
            )
