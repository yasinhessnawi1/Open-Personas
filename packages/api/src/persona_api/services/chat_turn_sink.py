"""`MessagesTurnSink` — the ``messages``-backed chat-turn persistence (spec P1, T2).

The concrete :class:`~persona_api.background.chat_turn_worker.ChatTurnSink`: it
implements the persist-at-start + checkpoint + finalize-terminal model
(D-P1-checkpoint) that replaces the v1 persist-after-final discipline, so a chat
turn is **resumable** — the DB always reflects the latest progress.

- :meth:`open_turn` — at turn START (called by the route, before the detached
  task launches): persist the user message + an in-progress assistant row
  (``streaming_status='running'``). Returns the assistant message id (the
  checkpoint/finalize target). A reload mid-turn refetches both rows.
- :meth:`checkpoint` — UPDATE the in-progress assistant row's accumulating
  ``content`` + ``stream_events`` (called on a throttled cadence by the worker —
  D-P1-cadence).
- :meth:`finalize` — the terminal write. ``complete`` finalizes the assistant
  content + the conversation compaction state (the credits deduct rides this
  path — wired in T2b, D-P1-billing-contract); ``cancelled`` / ``error`` /
  ``interrupted`` mark the partial terminal without touching conversation state.

All writes are RLS-scoped via the ``current_user_id`` contextvar the caller
binds (the route for ``open_turn``; the worker for ``checkpoint`` / ``finalize``).
``streaming_status`` / ``stream_events`` are DB columns only — never
``ConversationMessage`` fields (the C0 lesson; the byte-for-byte corpus is the
canary).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import insert, update

from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import messages as messages_t

if TYPE_CHECKING:
    from persona.schema.conversation import Conversation
    from sqlalchemy import Engine

    from persona_api.schemas import ChannelContext, ImageRef

__all__ = ["MessagesTurnSink"]


class MessagesTurnSink:
    """Persist a detached chat turn into the ``messages`` table (one RLS-scoped engine)."""

    def __init__(self, rls_engine: Engine) -> None:
        self._engine = rls_engine

    def open_turn(
        self,
        *,
        conversation_id: str,
        user_message: str,
        channel: ChannelContext | None,
        images: list[ImageRef] | None,
    ) -> str:
        """Persist the user message + an in-progress assistant row at turn START.

        Returns the assistant message id (the checkpoint/finalize target). The
        user row carries the ``channel`` passthrough (D-08-3) + the inbound
        ``images`` refs (D-13-X-now option c); the assistant row starts empty and
        ``running``. ``created_at`` gets a per-row microsecond offset so the
        user→assistant order is deterministic (the LIST preview window orders by
        ``created_at`` desc; a transaction's ``now()`` is otherwise constant).
        Bumps ``conversations.updated_at`` so the conversation surfaces as active
        the moment the turn starts.

        Raises ``IntegrityError`` if a turn is already streaming for this
        conversation (the partial unique index — D-P1-one-active-turn).
        """
        assistant_id = f"msg_{uuid.uuid4().hex}"
        channel_json = channel.model_dump() if channel is not None else None
        images_json: list[dict[str, str]] | None = (
            [{"workspace_path": img.workspace_path, "media_type": img.media_type} for img in images]
            if images
            else None
        )
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            conn.execute(
                insert(messages_t).values(
                    id=f"msg_{uuid.uuid4().hex}",
                    conversation_id=conversation_id,
                    role="user",
                    content=user_message,
                    created_at=now,
                    channel=channel_json,
                    images=images_json,
                )
            )
            conn.execute(
                insert(messages_t).values(
                    id=assistant_id,
                    conversation_id=conversation_id,
                    role="assistant",
                    content="",
                    created_at=now + timedelta(microseconds=1),
                    streaming_status="running",
                    stream_events=[],
                )
            )
            conn.execute(
                update(conversations_t)
                .where(conversations_t.c.id == conversation_id)
                .values(updated_at=now)
            )
        return assistant_id

    def append_user_message(self, *, conversation_id: str, content: str) -> None:
        """Persist a single user message — no assistant row (Spec A6 chat-twin approval reply).

        The chat twin routes a decision reply to the resolver rather than a model turn, so there is
        no streaming assistant row to open — but the user's reply must still appear in the
        transcript. Bumps ``conversations.updated_at`` so the conversation surfaces as active.
        """
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            conn.execute(
                insert(messages_t).values(
                    id=f"msg_{uuid.uuid4().hex}",
                    conversation_id=conversation_id,
                    role="user",
                    content=content,
                    created_at=now,
                )
            )
            conn.execute(
                update(conversations_t)
                .where(conversations_t.c.id == conversation_id)
                .values(updated_at=now)
            )

    def checkpoint(
        self,
        *,
        conversation_id: str,  # noqa: ARG002 — part of the ChatTurnSink contract
        assistant_message_id: str,
        content: str,
        events: list[dict[str, object]],
    ) -> None:
        """Persist the in-progress partial (throttled cadence is the worker's call)."""
        with self._engine.begin() as conn:
            conn.execute(
                update(messages_t)
                .where(messages_t.c.id == assistant_message_id)
                .values(content=content, stream_events=events)
            )

    def finalize(
        self,
        *,
        conversation_id: str,
        assistant_message_id: str,
        conversation: Conversation,
        status: str,
        content: str,
        events: list[dict[str, object]],
        tier: str | None = None,
    ) -> None:
        """Terminal write. ``status`` ∈ {complete, cancelled, error, interrupted}.

        ``complete`` finalizes the assistant content + ``tier_used`` + the
        conversation compaction state (the loop mutated ``conversation`` in
        place). The non-complete states keep the partial ``content`` + mark the
        row terminal but do NOT touch conversation state (the turn did not finish
        cleanly). No billing here — the deduct rides this path in T2b
        (D-P1-billing-contract).
        """
        with self._engine.begin() as conn:
            conn.execute(
                update(messages_t)
                .where(messages_t.c.id == assistant_message_id)
                .values(
                    content=content,
                    streaming_status=status,
                    stream_events=events,
                    tier_used=tier,
                )
            )
            if status == "complete":
                conn.execute(
                    update(conversations_t)
                    .where(conversations_t.c.id == conversation_id)
                    .values(
                        compacted_summary=conversation.compacted_summary,
                        compacted_up_to=conversation.compacted_up_to,
                        updated_at=datetime.now(UTC),
                    )
                )
