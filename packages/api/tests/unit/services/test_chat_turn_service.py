"""T2b (spec P1) — `start_chat_turn` + `stream_turn`: the detached chat-turn flow.

End-to-end over the real ``MessagesTurnSink`` + ``ChatTurnRegistry`` + a scripted
loop (community engine, no Postgres): persist-at-start, stream the live tail,
finalize + bill on clean completion, 409 on a second concurrent turn, and the
error path (partial persisted, error frame, no done, no bill).
"""

# ruff: noqa: ARG001, ARG002 — scripted loop_builder / loop / credits signatures mirror the real ones.

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends.types import StreamChunk
from persona.schema.conversation import Conversation
from persona.schema.tools import ToolCall, ToolResult
from persona_api.background.chat_turn_worker import ChatTurnRegistry
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import messages as messages_t
from persona_api.db.models import personas as personas_t
from persona_api.errors import TurnAlreadyActiveError
from persona_api.services import chat_service
from persona_api.services.chat_turn_sink import MessagesTurnSink
from persona_runtime.agentic.events import RunEvent
from sqlalchemy import insert, select

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

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
    return eng


class _ScriptedLoop:
    def __init__(self, deltas: list[str], *, tool: bool = False) -> None:
        self._deltas = deltas
        self._tool = tool

    async def turn(
        self,
        conversation: Conversation,
        user_message: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        **_kwargs: object,
    ) -> AsyncIterator[StreamChunk]:
        assert on_event is not None
        await on_event(RunEvent.tier("mid"))
        if self._tool:
            call = ToolCall(name="code_execution", args={"code": "x"}, call_id="c1")
            await on_event(RunEvent.tool_calling(-1, [call]))
            result = ToolResult(tool_name="code_execution", content="ok", call_id="c1")
            await on_event(RunEvent.tool_result(-1, "code_execution", result))
        for i, d in enumerate(self._deltas):
            yield StreamChunk(delta=d, is_final=i == len(self._deltas) - 1)


class _RaisingLoop:
    async def turn(
        self,
        conversation: Conversation,
        user_message: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        **_kwargs: object,
    ) -> AsyncIterator[StreamChunk]:
        assert on_event is not None
        await on_event(RunEvent.tool_calling(-1, [ToolCall(name="x", args={}, call_id="c1")]))
        raise RuntimeError("loop blew up")
        yield  # pragma: no cover


class _RecordingCredits:
    def __init__(self) -> None:
        self.deducts: list[tuple[str, int, str]] = []

    def deduct(self, *, rls_engine: object, user_id: str, amount: int, reason: str) -> int:
        self.deducts.append((user_id, amount, reason))
        return 0


def _loop_builder(loop: object) -> Callable[[str], Awaitable[Any]]:
    async def _build(_persona_id: str) -> object:
        return loop

    return _build


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


def _parse(frames: list[bytes]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for frame in frames:
        event = data = ""
        for line in frame.decode().splitlines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = line.removeprefix("data: ")
        if event:
            out.append((event, json.loads(data) if data else {}))
    return out


@pytest.mark.asyncio
async def test_start_chat_turn_binds_scope_during_build_and_resets_after(engine: Engine) -> None:
    # Spec P4-D-3 caution #1: the early sandbox-context bind around the loop build
    # is tightly scoped — present DURING the build (so the filesystem MCP child can
    # be scoped at spawn) and reset AFTER (no leak into the rest of the task / next
    # request). The loop_builder records the context it observed at build time.
    from persona_api.sandbox import get_sandbox_request_context

    observed: dict[str, object] = {}

    async def _recording_builder(_persona_id: str) -> object:
        ctx = get_sandbox_request_context()
        observed["owner_id"] = ctx.owner_id if ctx else None
        observed["conversation_id"] = ctx.conversation_id if ctx else None
        return _ScriptedLoop(["Hi"])

    # No context bound before the call.
    assert get_sandbox_request_context() is None

    sink = MessagesTurnSink(engine)
    registry = ChatTurnRegistry(sink=sink, rls_engine=engine)
    handle = await chat_service.start_chat_turn(
        rls_engine=engine,
        sink=sink,
        registry=registry,
        loop_builder=_recording_builder,  # type: ignore[arg-type]
        owner_id=_OWNER,
        conversation_id=_CONV,
        user_message="hello",
        channel=None,
    )
    # Bound to THIS request during the build…
    assert observed == {"owner_id": _OWNER, "conversation_id": _CONV}
    # …and reset immediately after (no leak).
    assert get_sandbox_request_context() is None
    await handle.task
    assert get_sandbox_request_context() is None


@pytest.mark.asyncio
async def test_start_persists_user_and_in_progress_assistant_immediately(engine: Engine) -> None:
    # The registry must share the sink that opened the turn, so checkpoint/finalize
    # target the same row. Build them together.
    sink = MessagesTurnSink(engine)
    registry = ChatTurnRegistry(sink=sink, rls_engine=engine)
    handle = await chat_service.start_chat_turn(
        rls_engine=engine,
        sink=sink,
        registry=registry,
        loop_builder=_loop_builder(_ScriptedLoop(["Hi"])),  # type: ignore[arg-type]
        owner_id=_OWNER,
        conversation_id=_CONV,
        user_message="hello",
        channel=None,
    )
    # Persisted at START — before the turn finished.
    rows = _rows(engine)
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "hello"
    await handle.task


@pytest.mark.asyncio
async def test_stream_turn_emits_chunks_then_done_and_finalizes_complete(engine: Engine) -> None:
    sink = MessagesTurnSink(engine)
    registry = ChatTurnRegistry(sink=sink, rls_engine=engine)
    handle = await chat_service.start_chat_turn(
        rls_engine=engine,
        sink=sink,
        registry=registry,
        loop_builder=_loop_builder(_ScriptedLoop(["Hello ", "there!"], tool=True)),  # type: ignore[arg-type]
        owner_id=_OWNER,
        conversation_id=_CONV,
        user_message="hello",
        channel=None,
    )
    frames = [f async for f in chat_service.stream_turn(handle)]
    await handle.task
    events = _parse(frames)
    kinds = [e for e, _ in events]
    assert kinds.index("tool_calling") < kinds.index("tool_result") < kinds.index("done")
    assert "tier" not in kinds  # tier rides done
    done = next(d for e, d in events if e == "done")
    assert done["tier"] == "mid"
    # Finalized: assistant row complete, full content persisted.
    assistant = _rows(engine)[1]
    assert assistant["streaming_status"] == "complete"
    assert assistant["content"] == "Hello there!"
    assert assistant["tier_used"] == "mid"


@pytest.mark.asyncio
async def test_second_concurrent_turn_is_blocked_409(engine: Engine) -> None:
    sink = MessagesTurnSink(engine)
    registry = ChatTurnRegistry(sink=sink, rls_engine=engine)

    async def _start_one(loop: object) -> object:
        return await chat_service.start_chat_turn(
            rls_engine=engine,
            sink=sink,
            registry=registry,
            loop_builder=_loop_builder(loop),  # type: ignore[arg-type]
            owner_id=_OWNER,
            conversation_id=_CONV,
            user_message="hi",
            channel=None,
        )

    handle = await _start_one(_ScriptedLoop(["x"]))
    # The turn is still registered until its task completes; a second start is 409.
    with pytest.raises(TurnAlreadyActiveError):
        await _start_one(_ScriptedLoop(["y"]))
    await handle.task


@pytest.mark.asyncio
async def test_error_turn_persists_partial_emits_error_no_done_no_bill(engine: Engine) -> None:
    creds = _RecordingCredits()
    sink = MessagesTurnSink(engine)
    registry = ChatTurnRegistry(
        sink=sink,
        rls_engine=engine,
        credits_policy=creds,
        credits_per_turn=7,  # type: ignore[arg-type]
    )
    handle = await chat_service.start_chat_turn(
        rls_engine=engine,
        sink=sink,
        registry=registry,
        loop_builder=_loop_builder(_RaisingLoop()),  # type: ignore[arg-type]
        owner_id=_OWNER,
        conversation_id=_CONV,
        user_message="hello",
        channel=None,
    )
    events = _parse([f async for f in chat_service.stream_turn(handle)])
    await handle.task
    kinds = [e for e, _ in events]
    # The tool_calling that fired before the raise reached the client…
    assert "tool_calling" in kinds
    # …then an error frame, never a done; and NO bill (D-08-6 unchanged for errors).
    assert "error" in kinds
    assert "done" not in kinds
    assert creds.deducts == []
    assistant = _rows(engine)[1]
    assert assistant["streaming_status"] == "error"


@pytest.mark.asyncio
async def test_clean_completion_bills_once(engine: Engine) -> None:
    creds = _RecordingCredits()
    sink = MessagesTurnSink(engine)
    registry = ChatTurnRegistry(
        sink=sink,
        rls_engine=engine,
        credits_policy=creds,
        credits_per_turn=7,  # type: ignore[arg-type]
    )
    handle = await chat_service.start_chat_turn(
        rls_engine=engine,
        sink=sink,
        registry=registry,
        loop_builder=_loop_builder(_ScriptedLoop(["done"])),  # type: ignore[arg-type]
        owner_id=_OWNER,
        conversation_id=_CONV,
        user_message="hi",
        channel=None,
    )
    [f async for f in chat_service.stream_turn(handle)]
    await handle.task
    assert creds.deducts == [(_OWNER, 7, "chat_turn")]


@pytest.mark.asyncio
async def test_first_turn_auto_titles_on_completion(engine: Engine) -> None:
    titled: list[str] = []

    async def _title_builder(first: str) -> str:
        titled.append(first)
        return "A Title"

    sink = MessagesTurnSink(engine)
    registry = ChatTurnRegistry(sink=sink, rls_engine=engine)
    handle = await chat_service.start_chat_turn(
        rls_engine=engine,
        sink=sink,
        registry=registry,
        loop_builder=_loop_builder(_ScriptedLoop(["hi"])),  # type: ignore[arg-type]
        owner_id=_OWNER,
        conversation_id=_CONV,
        user_message="my first question",
        channel=None,
        title_builder=_title_builder,
    )
    [f async for f in chat_service.stream_turn(handle)]
    await handle.task
    assert titled == ["my first question"]
    with engine.begin() as conn:
        title = conn.execute(
            select(conversations_t.c.title).where(conversations_t.c.id == _CONV)
        ).scalar_one()
    assert title == "A Title"
