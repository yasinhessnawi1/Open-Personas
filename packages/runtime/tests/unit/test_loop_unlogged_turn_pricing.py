"""A turn whose answer was served is priced even when it cannot be written back (R9-316).

When the conversation row is deleted while a text turn streams, the write-back fails:
the episodic write is refused by the forget guard (PR #51, migration 068) or the
TurnLog insert hits its foreign key to ``conversations``. The user has already seen the
streamed answer and the provider has already been paid, so the chat worker must still
charge it. It reads two things off the loop: ``last_turn_cost_cents`` (what the served
answer cost) and ``last_turn_served`` (the answer was served and priced, so a failure
after this point is a failure to record the turn, not a failure to produce it).

Driven through a REAL :class:`ConversationLoop` with the per-round pricing harness.
"""

# Test doubles keep the loose signatures of the protocols they stand in for.
# ruff: noqa: ANN401, ARG002
from __future__ import annotations

from typing import Any

import pytest
from _fakes import FakeStore  # type: ignore[import-not-found]
from persona.backends.errors import RateLimitError
from persona.backends.multi_model import MultiModelChatBackend
from persona.errors import MemoryOwnerMissingError
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona_runtime.logging import MemoryTurnLogWriter, TurnLog
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry
from test_loop_per_round_pricing import (  # type: ignore[import-not-found]
    _CFG,
    _echo_tool,
    _Member,
    _RateTable,
)

pytestmark = pytest.mark.asyncio


class _ConversationGoneError(Exception):
    """Stands in for the database's foreign key violation on ``turn_logs``."""


class _FailingTurnLogWriter:
    """A TurnLog sink whose insert fails, as it does once the conversation row is gone."""

    def __init__(self) -> None:
        self.attempts: list[TurnLog] = []

    def write(self, log: TurnLog) -> None:
        self.attempts.append(log)
        raise _ConversationGoneError("turn_logs.conversation_id violates its foreign key")


class _RefusingEpisodicStore(FakeStore):  # type: ignore[misc]
    """The episodic store once the conversation is forgotten: the owner guard refuses."""

    def write(self, persona_id: str, chunks: Any, **_kwargs: Any) -> None:
        raise MemoryOwnerMissingError("the conversation was forgotten", context={"kind": "x"})


def _loop(
    member: _Member,
    *,
    turn_log_writer: Any = None,
    episodic: Any = None,
) -> ConversationLoop:
    registry = TierRegistry(
        {t: TierConfig(name=t, backend_config=_CFG) for t in ("frontier", "mid", "small")}
    )
    chain = MultiModelChatBackend([member])
    registry._cache = {"frontier": chain, "mid": chain, "small": chain}  # type: ignore[assignment,attr-defined]  # noqa: SLF001
    stores: dict[str, Any] = {k: FakeStore() for k in ("identity", "self_facts", "worldview")}
    stores["episodic"] = episodic if episodic is not None else FakeStore()
    return ConversationLoop(
        persona=Persona(
            persona_id="astrid",
            identity=PersonaIdentity(name="Astrid", role="assistant", background="bg"),
        ),
        stores=stores,  # type: ignore[arg-type]
        toolbox=Toolbox([_echo_tool], allow_list=None),  # type: ignore[list-item]
        skill_scanner=SkillScanner([]),
        skill_injector=SkillInjector(),
        scanned_skills=[],
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
        turn_log_writer=turn_log_writer if turn_log_writer is not None else MemoryTurnLogWriter(),
        cost_source=_RateTable(),
    )


def _conversation() -> Conversation:
    return Conversation(conversation_id="c1", persona_id="astrid", messages=[])


async def _drain(loop: ConversationLoop) -> list[str]:
    return [chunk.delta async for chunk in loop.turn(_conversation(), "hello there")]


async def _drain_into(loop: ConversationLoop, seen: list[str]) -> None:
    """Drain a turn, keeping what streamed even when the turn then raises."""
    async for chunk in loop.turn(_conversation(), "hello there"):
        seen.append(chunk.delta)


async def test_a_logged_turn_is_served_and_priced() -> None:
    """The baseline the failure cases are compared with: 20.0 cents at ``vendor/dear``."""
    loop = _loop(_Member("dear", ["text"]))

    await _drain(loop)

    assert loop.last_turn_served is True
    assert loop.last_turn_cost_cents == pytest.approx(20.0)
    assert loop.last_turn_cost_basis == "estimate_static"


async def test_a_turn_whose_turn_log_insert_fails_is_still_priced() -> None:
    writer = _FailingTurnLogWriter()
    loop = _loop(_Member("dear", ["text"]), turn_log_writer=writer)
    seen: list[str] = []

    with pytest.raises(_ConversationGoneError):
        await _drain_into(loop, seen)

    # The user saw the answer before the write-back failed.
    assert "answer from dear" in "".join(seen)
    assert len(writer.attempts) == 1
    assert loop.last_turn_served is True
    # The same figure a logged turn records, and the same one the failed row carried.
    assert loop.last_turn_cost_cents == pytest.approx(20.0)
    assert loop.last_turn_cost_basis == "estimate_static"
    assert writer.attempts[0].cost_cents == pytest.approx(20.0)


async def test_a_turn_whose_episodic_write_is_refused_is_still_priced() -> None:
    """PR #51's guard refuses the episodic write first: the same outcome as the FK."""
    writer = MemoryTurnLogWriter()
    loop = _loop(
        _Member("dear", ["text"]), turn_log_writer=writer, episodic=_RefusingEpisodicStore()
    )

    with pytest.raises(MemoryOwnerMissingError):
        await _drain(loop)

    assert loop.last_turn_served is True
    assert loop.last_turn_cost_cents == pytest.approx(20.0)
    assert loop.last_turn_cost_basis == "estimate_static"
    # Nothing was logged: the turn failed at its first write-back step.
    assert writer.logs == []


async def test_a_turn_that_fails_before_any_answer_is_not_served() -> None:
    rate_limited = RateLimitError(
        "429", context={"status_code": "429", "reason": "insufficient_quota"}
    )
    loop = _loop(_Member("dear", [rate_limited]))

    with pytest.raises(Exception):  # noqa: B017, PT011  # the chain's exhaustion error
        await _drain(loop)

    assert loop.last_turn_served is False
    assert loop.last_turn_cost_cents is None


async def test_served_is_reset_at_each_turn_start() -> None:
    """A served turn followed by one that fails before answering never leaks the flag."""
    rate_limited = RateLimitError(
        "429", context={"status_code": "429", "reason": "insufficient_quota"}
    )
    loop = _loop(_Member("dear", ["text", rate_limited]))

    await _drain(loop)
    assert loop.last_turn_served is True

    with pytest.raises(Exception):  # noqa: B017, PT011  # the chain's exhaustion error
        await _drain(loop)

    assert loop.last_turn_served is False
    assert loop.last_turn_cost_cents is None
