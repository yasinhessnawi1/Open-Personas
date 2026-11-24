"""T4 (spec P1) — the chat reattach surface mechanics.

The service-level guts behind ``GET …/active-turn`` (the reattach seed),
``…/active-turn/events`` (the live-tail resubscribe), and ``…/active-turn/cancel``:

- :func:`chat_service.get_active_turn` returns the in-progress assistant row
  (content + stream_events + streaming_status) while a turn streams, else ``None``.
- ``stream_turn`` reattaches: events that fired while NO client was draining
  (the navigation gap) are **buffered** and delivered to the reattaching tail —
  "shows everything that happened while away" — then the live tail continues.
- ``ChatTurnRegistry.request_cancel`` returns ``False`` when no turn is live (the
  route's 404 path).
"""

# ruff: noqa: ARG002 — scripted-loop signatures mirror ConversationLoop.turn.

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from persona.backends.types import StreamChunk
from persona.schema.conversation import Conversation
from persona_api.background.chat_turn_worker import ChatTurnRegistry
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import personas as personas_t
from persona_api.middleware.rls_context import current_user_id
from persona_api.services import chat_service
from persona_api.services.chat_turn_sink import MessagesTurnSink
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

    from persona_runtime.agentic.events import RunEvent
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
    token = current_user_id.set(_OWNER)
    try:
        yield eng
    finally:
        current_user_id.reset(token)


def test_get_active_turn_returns_running_row_then_none(engine: Engine) -> None:
    sink = MessagesTurnSink(engine)
    msg_id = sink.open_turn(conversation_id=_CONV, user_message="q", channel=None, images=None)
    sink.checkpoint(
        conversation_id=_CONV,
        assistant_message_id=msg_id,
        content="partial",
        events=[{"kind": "text", "delta": "partial"}],
    )

    active = chat_service.get_active_turn(rls_engine=engine, conversation_id=_CONV)
    assert active is not None
    assert active["id"] == msg_id
    assert active["streaming_status"] == "running"
    assert active["content"] == "partial"

    # Once finalized, there is no active turn to reattach to.
    sink.finalize(
        conversation_id=_CONV,
        assistant_message_id=msg_id,
        conversation=Conversation(conversation_id=_CONV, persona_id=_PERSONA, messages=[]),
        status="complete",
        content="partial done",
        events=[],
    )
    assert chat_service.get_active_turn(rls_engine=engine, conversation_id=_CONV) is None


def _parse_kinds(frames: list[bytes]) -> list[str]:
    out: list[str] = []
    for frame in frames:
        for line in frame.decode().splitlines():
            if line.startswith("event: "):
                out.append(line.removeprefix("event: "))
    return out


def _deltas(frames: list[bytes]) -> str:
    text = ""
    for frame in frames:
        lines = frame.decode().splitlines()
        if any(line == "event: chunk" for line in lines):
            data = next(
                (line.removeprefix("data: ") for line in lines if line.startswith("data: ")), ""
            )
            if data:
                text += json.loads(data).get("delta", "")
    return text


@pytest.mark.asyncio
async def test_stream_turn_reattach_delivers_buffered_backlog_then_live_tail(
    engine: Engine,
) -> None:
    release = asyncio.Event()
    buffered = asyncio.Event()

    class _GapLoop:
        """Emits two chunks with NO consumer attached (the navigation gap),
        then blocks; a reattaching tail must still receive both, then the rest."""

        async def turn(
            self,
            conversation: Conversation,
            user_message: str,
            on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
            **_kwargs: object,
        ) -> AsyncIterator[StreamChunk]:
            yield StreamChunk(delta="while ", is_final=False)
            yield StreamChunk(delta="away ", is_final=False)
            buffered.set()  # two chunks are now buffered on the queue (no consumer)
            await release.wait()
            yield StreamChunk(delta="and back", is_final=True)

    async def _build(_pid: str) -> _GapLoop:
        return _GapLoop()

    registry = ChatTurnRegistry(sink=MessagesTurnSink(engine), rls_engine=engine)
    handle = await chat_service.start_chat_turn(
        long_op_instance="api",
        rls_engine=engine,
        sink=MessagesTurnSink(engine),
        registry=registry,
        loop_builder=_build,  # type: ignore[arg-type]
        owner_id=_OWNER,
        conversation_id=_CONV,
        user_message="hello",
        channel=None,
    )
    await buffered.wait()  # the gap happened: two chunks streamed with nobody attached

    # Reattach now and collect the whole tail.
    async def _drain() -> list[bytes]:
        return [f async for f in chat_service.stream_turn(handle)]

    drain_task = asyncio.create_task(_drain())
    await asyncio.sleep(0)  # let the tail drain the buffered backlog
    release.set()
    frames = await drain_task
    await handle.task

    # The reattaching tail saw EVERYTHING — the buffered gap chunks AND the live tail.
    assert _deltas(frames) == "while away and back"
    assert "done" in _parse_kinds(frames)


def test_request_cancel_returns_false_when_no_active_turn(engine: Engine) -> None:
    registry = ChatTurnRegistry(sink=MessagesTurnSink(engine), rls_engine=engine)
    # No turn started → nothing to cancel → the route maps this to 404.
    assert registry.request_cancel(_CONV) is False
