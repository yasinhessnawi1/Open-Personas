"""T2a (spec P1) — `MessagesTurnSink`: the `messages`-backed chat-turn persistence.

Persist-at-start + checkpoint + finalize-terminal (D-P1-checkpoint), replacing the
old persist-after-final discipline:

- ``open_turn`` — at turn START, persist the user message + an in-progress
  assistant row (``streaming_status='running'``); return the assistant id. So a
  reload mid-turn refetches the question + the partial answer (acceptance #2).
- ``checkpoint`` — UPDATE the in-progress assistant row's content + stream_events
  as it streams.
- ``finalize`` — terminal write: ``complete`` finalizes content + conversation
  compaction state (+ deduct, wired in T2b); ``cancelled`` / ``error`` keep the
  partial, marked terminal, no conversation-state write.

The DB-level one-active-turn guarantee (the partial unique index,
D-P1-one-active-turn) is exercised too. ``streaming_status`` / ``stream_events``
are DB columns ONLY — never ``ConversationMessage`` fields (the byte-for-byte
corpus is the canary).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.schema.conversation import Conversation
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import messages as messages_t
from persona_api.db.models import personas as personas_t
from persona_api.middleware.rls_context import current_user_id
from persona_api.schemas import ChannelContext, ImageRef
from persona_api.services.chat_turn_sink import MessagesTurnSink
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

_OWNER = "user_alice"
_PERSONA = "astrid"
_CONV = "conv_cafe"


@pytest.fixture
def engine(tmp_path: object) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "t.db")  # type: ignore[operator]
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="a@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: Astrid"))
        conn.execute(insert(conversations_t).values(id=_CONV, owner_id=_OWNER, persona_id=_PERSONA))
    token = current_user_id.set(_OWNER)  # the worker/route binds this in production
    try:
        yield eng
    finally:
        current_user_id.reset(token)


def _rows(engine: Engine) -> list[dict[str, object]]:
    with engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(
                select(messages_t)
                .where(messages_t.c.conversation_id == _CONV)
                .order_by(messages_t.c.created_at.asc())
            )
            .mappings()
            .all()
        ]


def test_open_turn_persists_user_and_in_progress_assistant(engine: Engine) -> None:
    sink = MessagesTurnSink(engine)
    msg_id = sink.open_turn(conversation_id=_CONV, user_message="hello", channel=None, images=None)
    rows = _rows(engine)
    assert [r["role"] for r in rows] == ["user", "assistant"]
    user, assistant = rows
    assert user["content"] == "hello"
    assert user["streaming_status"] is None  # the user row is never a streaming row
    assert assistant["id"] == msg_id
    assert assistant["content"] == ""
    assert assistant["streaming_status"] == "running"


def test_open_turn_text_only_leaves_channel_and_images_null(engine: Engine) -> None:
    MessagesTurnSink(engine).open_turn(
        conversation_id=_CONV, user_message="hi", channel=None, images=None
    )
    user = _rows(engine)[0]
    # The web-UI text-only case: no channel, no images on any row (the
    # byte-for-byte invariant the old persist path also held).
    assert user["channel"] is None
    assert user["images"] is None


def test_open_turn_tags_channel_and_images_on_user_row_only(engine: Engine) -> None:
    sink = MessagesTurnSink(engine)
    sink.open_turn(
        conversation_id=_CONV,
        user_message="describe",
        channel=ChannelContext(platform="slack"),
        images=[ImageRef(workspace_path="uploads/a.png", media_type="image/png")],
    )
    user, assistant = _rows(engine)
    assert user["channel"] == {
        "platform": "slack",
        "platform_user_id": None,
        "platform_chat_id": None,
        "metadata": {},
    }
    assert user["images"] == [{"workspace_path": "uploads/a.png", "media_type": "image/png"}]
    assert assistant["channel"] is None
    assert assistant["images"] is None


def test_checkpoint_updates_partial_content_and_events(engine: Engine) -> None:
    sink = MessagesTurnSink(engine)
    msg_id = sink.open_turn(conversation_id=_CONV, user_message="q", channel=None, images=None)
    sink.checkpoint(
        conversation_id=_CONV,
        assistant_message_id=msg_id,
        content="partial answer",
        events=[{"kind": "text", "delta": "partial answer"}],
    )
    assistant = _rows(engine)[1]
    assert assistant["content"] == "partial answer"
    assert assistant["streaming_status"] == "running"  # still in flight
    assert assistant["stream_events"] == [{"kind": "text", "delta": "partial answer"}]


def test_finalize_complete_writes_final_content_tier_and_conversation_state(
    engine: Engine,
) -> None:
    sink = MessagesTurnSink(engine)
    msg_id = sink.open_turn(conversation_id=_CONV, user_message="q", channel=None, images=None)
    conversation = Conversation(
        conversation_id=_CONV,
        persona_id=_PERSONA,
        messages=[],
        compacted_summary="a summary",
        compacted_up_to=3,
    )
    sink.finalize(
        conversation_id=_CONV,
        assistant_message_id=msg_id,
        conversation=conversation,
        status="complete",
        content="the full answer",
        events=[{"kind": "text", "delta": "the full answer"}],
        tier="mid",
    )
    assistant = _rows(engine)[1]
    assert assistant["content"] == "the full answer"
    assert assistant["streaming_status"] == "complete"
    assert assistant["tier_used"] == "mid"
    with engine.begin() as conn:
        conv = (
            conn.execute(select(conversations_t).where(conversations_t.c.id == _CONV))
            .mappings()
            .first()
        )
    assert conv is not None
    assert conv["compacted_summary"] == "a summary"
    assert conv["compacted_up_to"] == 3


def test_finalize_cancelled_keeps_partial_and_marks_terminal(engine: Engine) -> None:
    sink = MessagesTurnSink(engine)
    msg_id = sink.open_turn(conversation_id=_CONV, user_message="q", channel=None, images=None)
    sink.checkpoint(conversation_id=_CONV, assistant_message_id=msg_id, content="half", events=[])
    sink.finalize(
        conversation_id=_CONV,
        assistant_message_id=msg_id,
        conversation=Conversation(conversation_id=_CONV, persona_id=_PERSONA, messages=[]),
        status="cancelled",
        content="half",
        events=[],
    )
    assistant = _rows(engine)[1]
    assert assistant["streaming_status"] == "cancelled"
    assert assistant["content"] == "half"  # the partial is preserved (no partial billing, kept)


def test_open_turn_twice_violates_one_streaming_per_conversation(engine: Engine) -> None:
    sink = MessagesTurnSink(engine)
    sink.open_turn(conversation_id=_CONV, user_message="first", channel=None, images=None)
    # The partial unique index (D-P1-one-active-turn) forbids a second running
    # assistant row for the same conversation — the DB-level backstop.
    with pytest.raises(IntegrityError):
        sink.open_turn(conversation_id=_CONV, user_message="second", channel=None, images=None)


# -- R9-022: heal_orphaned_running (the lazy self-heal DB primitive) ---------


def test_heal_orphaned_running_marks_the_row_interrupted_and_returns_its_id(
    engine: Engine,
) -> None:
    sink = MessagesTurnSink(engine)
    msg_id = sink.open_turn(conversation_id=_CONV, user_message="q", channel=None, images=None)
    sink.checkpoint(
        conversation_id=_CONV,
        assistant_message_id=msg_id,
        content="partial before the crash",
        events=[{"kind": "text", "delta": "partial before the crash"}],
    )

    healed_id = sink.heal_orphaned_running(conversation_id=_CONV)

    assert healed_id == msg_id
    assistant = _rows(engine)[1]
    assert assistant["streaming_status"] == "interrupted"
    # The checkpointed partial is preserved byte-for-byte — the heal only flips
    # the lifecycle column, exactly like finalize's non-complete branch would.
    assert assistant["content"] == "partial before the crash"
    assert assistant["stream_events"] == [{"kind": "text", "delta": "partial before the crash"}]


def test_heal_orphaned_running_is_a_noop_when_nothing_is_running(engine: Engine) -> None:
    sink = MessagesTurnSink(engine)
    msg_id = sink.open_turn(conversation_id=_CONV, user_message="q", channel=None, images=None)
    sink.finalize(
        conversation_id=_CONV,
        assistant_message_id=msg_id,
        conversation=Conversation(conversation_id=_CONV, persona_id=_PERSONA, messages=[]),
        status="complete",
        content="done",
        events=[],
    )

    # The overwhelmingly common case: a healthy, already-finalized turn — no
    # 'running' row exists, so the heal touches nothing and reports it.
    assert sink.heal_orphaned_running(conversation_id=_CONV) is None
    assistant = _rows(engine)[1]
    assert assistant["streaming_status"] == "complete"  # untouched
    assert assistant["content"] == "done"


def test_heal_orphaned_running_on_a_fresh_conversation_is_a_noop(engine: Engine) -> None:
    # No messages at all yet (the very first turn on a brand-new conversation) —
    # must not raise, must report nothing healed.
    sink = MessagesTurnSink(engine)
    assert sink.heal_orphaned_running(conversation_id=_CONV) is None
    assert _rows(engine) == []
