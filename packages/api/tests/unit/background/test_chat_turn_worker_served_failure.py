"""A turn that fails after its answer was served is still charged, exactly once (R9-316).

When the conversation row is deleted while a text turn streams, the loop's write-back
fails (the episodic write is refused, or the TurnLog insert hits its foreign key) and
the turn ends as ``error``. The user has already seen the answer and the provider has
already been paid, so D-M3-1 asks for the charge. The loop says the answer was served
(``last_turn_served``) and what it cost (``last_turn_cost_cents``); the worker then
charges that figure through the idempotent deduct, keyed on the turn's assistant
message id, so the same turn can never be charged twice.

A turn that failed before its answer was served (a provider failure, an empty reply)
is still not charged. Harness mirrors ``test_chat_turn_worker_proportional.py``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from persona.backends.types import StreamChunk
from persona.errors import CreditsExhaustedError, MemoryOwnerMissingError
from persona.schema.conversation import Conversation
from persona_api.background.chat_turn_worker import (
    ChatTurnHandle,
    ChatTurnRegistry,
    served_turn_billing_key,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from persona_runtime.agentic.events import RunEvent

_CONV = "c_served"
_OWNER = "u_served"
_MSG = "msg_served"
_PERSONA = "p_served"


class _WriteBackError(Exception):
    """Stands in for the turn_logs foreign key violation."""


class _FailingLoop:
    """A ConversationLoop double: streams text, then fails at write-back (or before)."""

    def __init__(
        self,
        *,
        served: bool,
        cost_cents: float | None = 5.3,
        cost_basis: str | None = "estimate_static",
        error: Exception | None = None,
        deltas: tuple[str, ...] = ("the answer",),
    ) -> None:
        self._served = served
        self._cost = (cost_cents, cost_basis)
        self._error = error if error is not None else _WriteBackError("fk violation")
        self._deltas = deltas
        self.last_turn_served = False
        self.last_turn_cost_cents: float | None = None
        self.last_turn_cost_basis: str | None = None

    async def turn(
        self,
        conversation: Conversation,  # noqa: ARG002
        user_message: str,  # noqa: ARG002
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,  # noqa: ARG002
        **_kwargs: object,
    ) -> AsyncIterator[StreamChunk]:
        for delta in self._deltas:
            yield StreamChunk(delta=delta)
        if self._served:
            # The loop prices the served answer, then the write-back fails.
            self.last_turn_cost_cents, self.last_turn_cost_basis = self._cost
            self.last_turn_served = True
        raise self._error


class _CompleteLoop:
    last_turn_served = True
    last_turn_cost_cents = 5.3
    last_turn_cost_basis = "estimate_static"

    async def turn(
        self,
        conversation: Conversation,  # noqa: ARG002
        user_message: str,  # noqa: ARG002
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,  # noqa: ARG002
        **_kwargs: object,
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta="ok", is_final=True)


class _Sink:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    def checkpoint(self, **kwargs: object) -> None:  # noqa: ARG002
        return None

    def finalize(self, **kwargs: object) -> None:
        self.statuses.append(str(kwargs["status"]))


class _KeyedCredits:
    """A CreditsPolicy double whose keyed variants honour ``billing_key`` like the ledger."""

    def __init__(self, *, exhausted: bool = False, broken: bool = False) -> None:
        self.plain: list[tuple[int, str]] = []
        self.keyed: list[tuple[str, int, str, float | None, str | None]] = []
        self.captures: list[tuple[str, int, str]] = []
        self.keys: set[str] = set()
        self._exhausted = exhausted
        self._broken = broken

    def deduct(self, *, amount: int, reason: str, **_kwargs: object) -> int:
        self.plain.append((amount, reason))
        return 100

    def deduct_idempotent(
        self,
        *,
        rls_engine: object,  # noqa: ARG002
        user_id: str,  # noqa: ARG002
        amount: int,
        reason: str,
        billing_key: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> int:
        if self._broken:
            msg = "the owner's balance row is gone"
            raise RuntimeError(msg)
        if billing_key in self.keys:
            return 100  # a re-delivery: the ledger already holds this key, nothing drawn
        if self._exhausted:
            raise CreditsExhaustedError("exhausted", context={"amount": str(amount)})
        self.keyed.append((billing_key, amount, reason, cost_cents, cost_basis))
        self.keys.add(billing_key)
        return 100

    def capture_up_to_idempotent(
        self,
        *,
        rls_engine: object,  # noqa: ARG002
        user_id: str,  # noqa: ARG002
        amount: int,
        reason: str,
        billing_key: str,
        **_kwargs: object,
    ) -> tuple[int, int]:
        if billing_key in self.keys:
            return 0, 0
        self.keys.add(billing_key)
        self.captures.append((billing_key, amount, reason))
        return 2, 0


def _registry(billing: _KeyedCredits, sink: _Sink | None = None) -> ChatTurnRegistry:
    return ChatTurnRegistry(
        sink=sink or _Sink(),  # type: ignore[arg-type]
        rls_engine=object(),  # type: ignore[arg-type]  # the double ignores it
        credits_policy=billing,  # type: ignore[arg-type]
        credits_per_turn=1,
        proportional_credits=True,
    )


async def _run(
    reg: ChatTurnRegistry, loop: object
) -> tuple[ChatTurnHandle, list[tuple[str, object]]]:
    handle = reg.start(
        conversation_id=_CONV,
        owner_id=_OWNER,
        assistant_message_id=_MSG,
        loop=loop,  # type: ignore[arg-type]
        conversation=Conversation(conversation_id=_CONV, persona_id=_PERSONA, messages=[]),
        user_message="hello",
    )
    assert handle.task is not None
    await handle.task
    items: list[tuple[str, object]] = []
    while True:
        item = handle.events.get_nowait()
        if item is None:
            break
        items.append(item)
    return handle, items


def _terminal(items: list[tuple[str, object]]) -> str:
    return items[-1][0]


@pytest.mark.asyncio
async def test_a_turn_whose_write_back_fails_after_serving_is_charged_once() -> None:
    billing = _KeyedCredits()
    sink = _Sink()
    _handle, items = await _run(_registry(billing, sink), _FailingLoop(served=True))

    # Still an error to the user (the conversation is gone; nothing more to show)...
    assert sink.statuses == ["error"]
    assert _terminal(items) == "error"
    # ...but the served answer is charged: once, proportionally, keyed on the turn.
    assert billing.keyed == [
        (served_turn_billing_key(_MSG), 6, "chat_turn:estimate_static", 5.3, "estimate_static")
    ]
    assert billing.plain == []


@pytest.mark.asyncio
async def test_a_refused_episodic_write_is_charged_the_same_way() -> None:
    """PR #51 moved the failure to the episodic write; the outcome must not differ."""
    billing = _KeyedCredits()
    refused = MemoryOwnerMissingError("forgotten", context={"kind": "episodic"})
    _handle, items = await _run(_registry(billing), _FailingLoop(served=True, error=refused))

    assert _terminal(items) == "error"
    assert [entry[0] for entry in billing.keyed] == [served_turn_billing_key(_MSG)]
    assert billing.plain == []


@pytest.mark.asyncio
async def test_a_turn_that_failed_before_serving_is_not_charged() -> None:
    billing = _KeyedCredits()
    _handle, items = await _run(_registry(billing), _FailingLoop(served=False, deltas=()))

    assert _terminal(items) == "error"
    assert billing.keyed == []
    assert billing.plain == []
    assert billing.captures == []


@pytest.mark.asyncio
async def test_a_loop_without_the_served_surface_is_not_charged_on_error() -> None:
    """A loop shape that does not expose ``last_turn_served`` keeps the old behaviour."""

    class _Legacy:
        last_turn_cost_cents = 5.3
        last_turn_cost_basis = "estimate_static"

        async def turn(self, *_args: object, **_kwargs: object) -> AsyncIterator[StreamChunk]:
            yield StreamChunk(delta="partial")
            raise _WriteBackError

    billing = _KeyedCredits()
    _handle, items = await _run(_registry(billing), _Legacy())

    assert _terminal(items) == "error"
    assert billing.keyed == []
    assert billing.plain == []


@pytest.mark.asyncio
async def test_the_same_turn_billed_again_is_not_charged_twice() -> None:
    """The key is the turn's own message id, so a second attempt to bill it is a no-op."""
    billing = _KeyedCredits()
    reg = _registry(billing)
    loop = _FailingLoop(served=True)
    handle, _items = await _run(reg, loop)

    # Bill the same turn again (a re-delivery): the key is derived from the turn's own
    # message id, so it is the same key and the ledger (here its double; the Postgres
    # test proves the real ``ON CONFLICT``) keeps one charge.
    reg._bill_served_failure(handle, loop)  # type: ignore[arg-type]  # noqa: SLF001

    assert [entry[0] for entry in billing.keyed] == [served_turn_billing_key(_MSG)]
    assert billing.plain == []


@pytest.mark.asyncio
async def test_an_exhausted_balance_captures_what_is_left_under_the_same_key() -> None:
    billing = _KeyedCredits(exhausted=True)
    _handle, items = await _run(_registry(billing), _FailingLoop(served=True))

    assert _terminal(items) == "error"
    assert billing.captures == [(served_turn_billing_key(_MSG), 6, "chat_turn:estimate_static")]


@pytest.mark.asyncio
async def test_a_billing_failure_never_swallows_the_error_event() -> None:
    """The account may be gone too (an erasure racing the turn): log, and end the stream."""
    billing = _KeyedCredits(broken=True)
    _handle, items = await _run(_registry(billing), _FailingLoop(served=True))

    assert _terminal(items) == "error"
    assert billing.keyed == []


@pytest.mark.asyncio
async def test_a_clean_turn_still_bills_through_the_plain_deduct_only() -> None:
    billing = _KeyedCredits()
    _handle, items = await _run(_registry(billing), _CompleteLoop())

    assert _terminal(items) == "done"
    assert billing.plain == [(6, "chat_turn:estimate_static")]
    assert billing.keyed == []


@pytest.mark.asyncio
async def test_a_user_cancel_after_serving_is_still_not_charged() -> None:
    """Unchanged contract: an explicit cancel finalizes the partial without billing."""
    billing = _KeyedCredits()
    loop = _FailingLoop(served=True, error=asyncio.CancelledError())
    reg = _registry(billing)
    handle = reg.start(
        conversation_id=_CONV,
        owner_id=_OWNER,
        assistant_message_id=_MSG,
        loop=loop,  # type: ignore[arg-type]
        conversation=Conversation(conversation_id=_CONV, persona_id=_PERSONA, messages=[]),
        user_message="hello",
    )
    handle.cancel_requested = True
    assert handle.task is not None
    await handle.task

    assert billing.keyed == []
    assert billing.plain == []


def test_the_key_is_rooted_on_the_turns_message_id() -> None:
    """R9-196: keys are unique table-wide, so the key embeds a globally unique id."""
    assert served_turn_billing_key("msg_abc") == "chat_turn:msg_abc"
