"""The connector reply runner PERSISTS both sides of a turn (R9-076).

Before this, ``build_reply_runner`` only ever READ the conversation and drove the
loop — nothing was written back. The conversation row existed (chats showed up in
the web UI) but stayed empty, and every inbound re-loaded that empty history, so a
persona had no memory across messages on ANY connector.

These run the REAL seam — ``start_chat_turn`` over the real ``MessagesTurnSink`` +
``ChatTurnRegistry`` against a real (community SQLite) database — with only the
model loop scripted. The load-bearing test is the round trip: two successive turns,
and the SECOND one must SEE the first in the history the loop is handed. Asserting
that a write function was called would prove nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends.types import StreamChunk
from persona_api.config import APIConfig, Edition
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import messages as messages_t
from persona_api.db.models import personas as personas_t
from persona_api.editions.factory import build_credits_policy
from persona_connectors.composition import build_reply_runner
from persona_connectors.domain.flow import TurnRequest
from persona_connectors.errors import TurnFailedError
from sqlalchemy import insert, select

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from persona.schema.conversation import Conversation
    from sqlalchemy import Engine

# The community (self-host) billing set: an explicit UnlimitedCreditsPolicy — "unbilled"
# is a stated decision here, never a forgotten keyword (R9-079).
_COMMUNITY_CONFIG = APIConfig(edition=Edition.community)
_UNBILLED = {
    "api_config": _COMMUNITY_CONFIG,
    "credits_policy": build_credits_policy(_COMMUNITY_CONFIG),
    "gateway": None,
    "job_queue": None,
}

_OWNER = "user_alice"
_PERSONA = "astrid"
_CONV = "conv_telegram_1"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """A real community (SQLite) database with one owner, persona + conversation."""
    eng = make_community_engine(tmp_path / "connectors.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="alice@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: Astrid"))
        conn.execute(insert(conversations_t).values(id=_CONV, owner_id=_OWNER, persona_id=_PERSONA))
    return eng


class _RecordingLoop:
    """A scripted ``ConversationLoop`` that records the history it was handed."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self._call = 0
        #: The (role, content) history the loop saw at the START of each turn.
        self.seen_history: list[list[tuple[str, str]]] = []

    async def turn(
        self,
        conversation: Conversation,
        user_message: str,  # noqa: ARG002 — the real loop signature
        on_event: Any = None,  # noqa: ARG002, ANN401 — ditto
        **_kwargs: object,
    ) -> AsyncIterator[StreamChunk]:
        self.seen_history.append(
            [
                (m.role, m.content if isinstance(m.content, str) else "")
                for m in conversation.messages
            ]
        )
        reply = self._replies[min(self._call, len(self._replies) - 1)]
        self._call += 1
        # The real loop appends the turn's pair to the conversation it was given;
        # the sink's ``finalize`` reads that tail for the compaction state.
        yield StreamChunk(delta=reply, is_final=True)


class _RaisingLoop:
    async def turn(
        self,
        conversation: Conversation,  # noqa: ARG002 — the real loop signature
        user_message: str,  # noqa: ARG002 — ditto
        on_event: Any = None,  # noqa: ARG002, ANN401 — ditto
        **_kwargs: object,
    ) -> AsyncIterator[StreamChunk]:
        raise RuntimeError("provider exploded")
        yield  # pragma: no cover — makes this an async generator


class _FakeRuntimeFactory:
    """Stands in for api's ``RuntimeFactory`` (the heavy deploy seam)."""

    def __init__(self, loop: object) -> None:
        self._loop = loop
        self.built_for: list[str] = []

    async def build_conversation_loop(self, persona_id: str) -> object:
        self.built_for.append(persona_id)
        return self._loop


@contextlib.contextmanager
def _owner_scope(owner_id: str) -> Iterator[None]:
    """The composition root's owner scope (community has no RLS to bind)."""
    from persona_api.middleware.rls_context import current_user_id

    token = current_user_id.set(owner_id)
    try:
        yield
    finally:
        current_user_id.reset(token)


def _rows(engine: Engine) -> list[tuple[str, str, str | None]]:
    with engine.begin() as conn:
        return [
            (str(r["role"]), str(r["content"]), r["streaming_status"])
            for r in conn.execute(
                select(messages_t)
                .where(messages_t.c.conversation_id == _CONV)
                .order_by(messages_t.c.created_at.asc())
            )
            .mappings()
            .all()
        ]


def _request(text: str) -> TurnRequest:
    return TurnRequest(owner_id=_OWNER, conversation_id=_CONV, persona_id=_PERSONA, text=text)


@pytest.mark.asyncio
async def test_turn_persists_both_the_user_message_and_the_reply(engine: Engine) -> None:
    """One turn writes BOTH sides — the conversation the web UI reads is no longer empty."""
    factory = _FakeRuntimeFactory(_RecordingLoop(["Hei, Yasin."]))
    run_turn = build_reply_runner(
        runtime_factory=factory,  # type: ignore[arg-type]
        rls_engine=engine,
        owner_scope=_owner_scope,
        **_UNBILLED,
    )

    reply = await run_turn(_request("hello there"))

    assert reply == "Hei, Yasin."
    assert _rows(engine) == [
        ("user", "hello there", None),
        ("assistant", "Hei, Yasin.", "complete"),
    ]


@pytest.mark.asyncio
async def test_second_turn_sees_the_first_turn_in_history(engine: Engine) -> None:
    """THE round trip (R9-076): message 2 reaches the model WITH message 1 in context.

    This is the memory guarantee — not "a write happened", but "the next turn reads
    what the previous turn wrote", through the real DB.
    """
    loop = _RecordingLoop(["First reply.", "Second reply."])
    run_turn = build_reply_runner(
        runtime_factory=_FakeRuntimeFactory(loop),  # type: ignore[arg-type]
        rls_engine=engine,
        owner_scope=_owner_scope,
        **_UNBILLED,
    )

    await run_turn(_request("my name is Yasin"))
    await run_turn(_request("what is my name?"))

    # Turn 1 saw an empty conversation; turn 2 saw turn 1's persisted pair.
    assert loop.seen_history[0] == []
    assert loop.seen_history[1] == [
        ("user", "my name is Yasin"),
        ("assistant", "First reply."),
    ]


@pytest.mark.asyncio
async def test_back_to_back_messages_are_ordered_not_refused(engine: Engine) -> None:
    """Two messages fired at once queue per conversation (the normal texting shape).

    Without the per-conversation lock the second turn would either trip the api's
    one-active-turn guard or load a history the first had not written yet.
    """
    loop = _RecordingLoop(["A.", "B."])
    run_turn = build_reply_runner(
        runtime_factory=_FakeRuntimeFactory(loop),  # type: ignore[arg-type]
        rls_engine=engine,
        owner_scope=_owner_scope,
        **_UNBILLED,
    )

    first, second = await asyncio.gather(run_turn(_request("one")), run_turn(_request("two")))

    assert (first, second) == ("A.", "B.")
    assert [role for role, _content, _status in _rows(engine)] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert loop.seen_history[1] == [("user", "one"), ("assistant", "A.")]


@pytest.mark.asyncio
async def test_a_failed_turn_raises_a_domain_error_and_persists_the_partial(
    engine: Engine,
) -> None:
    """A loop fault surfaces as ``TurnFailedError`` (the flow answers honestly).

    The worker never raises out of its background task — it finalizes ``error``; the
    collector converts that terminal frame into the domain error so the shared flow
    sends its turn-failed reply instead of an empty persona message.
    """
    run_turn = build_reply_runner(
        runtime_factory=_FakeRuntimeFactory(_RaisingLoop()),  # type: ignore[arg-type]
        rls_engine=engine,
        owner_scope=_owner_scope,
        **_UNBILLED,
    )

    with pytest.raises(TurnFailedError):
        await run_turn(_request("hello"))

    rows = _rows(engine)
    assert [role for role, _content, _status in rows] == ["user", "assistant"]
    assert rows[1][2] == "error"


@pytest.mark.asyncio
async def test_the_conversation_lock_map_does_not_grow() -> None:
    """A long-lived service must not accumulate one lock per conversation forever."""
    from persona_connectors.composition import _ConversationLocks

    locks = _ConversationLocks()
    async with locks.hold("conv_a"):
        assert set(locks._locks) == {"conv_a"}
    assert locks._locks == {}
