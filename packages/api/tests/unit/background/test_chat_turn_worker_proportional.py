"""M2-T5 — proportional credits on the detached chat-turn worker (D-M2-5).

The owner-ruled charge: ``amount = max(credits_per_turn, ceil(cost_cents))``
at 1 credit = 1 cent, computed from the loop's recorded turn cost
(``last_turn_cost_cents`` / ``last_turn_cost_basis``); unpriced / legacy
turns and the kill-switch-OFF arm charge the flat floor. Each case drives the
REAL registry path (``start`` → detached task → ``_deduct``) with a scripted
loop and a recording credits double — the worker's own ``_turn_charge`` does
the arithmetic, nothing is hand-computed in the assertions.

Harness mirrors ``test_chat_turn_worker.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.backends.types import StreamChunk
from persona.schema.conversation import Conversation
from persona_api.background.chat_turn_worker import ChatTurnHandle, ChatTurnRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from persona_runtime.agentic.events import RunEvent

_CONV = "c_prop"
_OWNER = "u_prop"
_MSG = "m_prop"
_PERSONA = "p_prop"


def _conversation() -> Conversation:
    return Conversation(conversation_id=_CONV, persona_id=_PERSONA, messages=[])


class _CostLoop:
    """A fake ConversationLoop exposing the M2-T5 recorded-cost surface."""

    def __init__(
        self,
        *,
        cost_cents: float | None,
        cost_basis: str | None,
    ) -> None:
        self.last_turn_cost_cents = cost_cents
        self.last_turn_cost_basis = cost_basis

    async def turn(
        self,
        conversation: Conversation,  # noqa: ARG002
        user_message: str,  # noqa: ARG002
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,  # noqa: ARG002
        **_kwargs: object,
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta="ok", is_final=True)


class _LegacyLoop:
    """A pre-M2 loop shape: NO recorded-cost attributes at all."""

    async def turn(
        self,
        conversation: Conversation,  # noqa: ARG002
        user_message: str,  # noqa: ARG002
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,  # noqa: ARG002
        **_kwargs: object,
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta="ok", is_final=True)


class _RecordingSink:
    def checkpoint(self, **kwargs: object) -> None:  # noqa: ARG002
        return None

    def finalize(self, **kwargs: object) -> None:  # noqa: ARG002
        return None


class _RecordingCredits:
    """A CreditsPolicy double recording (amount, reason) per deduct."""

    def __init__(self) -> None:
        self.deducts: list[tuple[int, str]] = []

    def deduct(self, *, rls_engine: object, user_id: str, amount: int, reason: str) -> int:  # noqa: ARG002
        self.deducts.append((amount, reason))
        return 0


def _registry(
    billing: _RecordingCredits,
    *,
    credits_per_turn: int = 1,
    proportional: bool = True,
) -> ChatTurnRegistry:
    return ChatTurnRegistry(
        sink=_RecordingSink(),  # type: ignore[arg-type]
        rls_engine=object(),  # type: ignore[arg-type] — the double ignores it
        credits_policy=billing,  # type: ignore[arg-type]
        credits_per_turn=credits_per_turn,
        proportional_credits=proportional,
    )


async def _run_turn(reg: ChatTurnRegistry, loop: object) -> ChatTurnHandle:
    handle = reg.start(
        conversation_id=_CONV,
        owner_id=_OWNER,
        assistant_message_id=_MSG,
        loop=loop,  # type: ignore[arg-type]
        conversation=_conversation(),
        user_message="hello",
    )
    assert handle.task is not None
    await handle.task
    return handle


# ---------------------------------------------------------------------------
# The ceil boundary matrix (ruled): 1.0→1, 1.000001→2, 0.2→1, 0→floor.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cost_cents", "basis", "expected_amount", "expected_reason"),
    [
        (1.0, "estimate_static", 1, "chat_turn:estimate_static"),  # exact → no bump
        (1.000001, "estimate_static", 2, "chat_turn:estimate_static"),  # ceil semantics
        (0.2, "estimate_static", 1, "chat_turn:estimate_static"),  # floor
        (0.0, "actual_openrouter", 1, "chat_turn:actual_openrouter"),  # :free actual → floor
        (0.042, "actual_openrouter", 1, "chat_turn:actual_openrouter"),  # micro actual → floor
        (2.9999999999, "estimate_catalog", 3, "chat_turn:estimate_catalog"),  # printed-value ceil
        (5.3, "actual_openrouter", 6, "chat_turn:actual_openrouter"),
    ],
)
async def test_proportional_amount_matrix(
    cost_cents: float, basis: str, expected_amount: int, expected_reason: str
) -> None:
    billing = _RecordingCredits()
    reg = _registry(billing)
    await _run_turn(reg, _CostLoop(cost_cents=cost_cents, cost_basis=basis))
    assert billing.deducts == [(expected_amount, expected_reason)]


@pytest.mark.asyncio
async def test_unpriced_turn_charges_the_flat_floor() -> None:
    billing = _RecordingCredits()
    reg = _registry(billing)
    await _run_turn(reg, _CostLoop(cost_cents=0.0, cost_basis="unpriced"))
    assert billing.deducts == [(1, "chat_turn")]  # bare reason: it IS a flat charge


@pytest.mark.asyncio
async def test_legacy_loop_without_the_surface_charges_flat() -> None:
    billing = _RecordingCredits()
    reg = _registry(billing)
    await _run_turn(reg, _LegacyLoop())
    assert billing.deducts == [(1, "chat_turn")]


@pytest.mark.asyncio
async def test_no_recorded_turn_charges_flat() -> None:
    # The loop ran but wrote no TurnLog (e.g. the R1 bypass): attrs are None.
    billing = _RecordingCredits()
    reg = _registry(billing)
    await _run_turn(reg, _CostLoop(cost_cents=None, cost_basis=None))
    assert billing.deducts == [(1, "chat_turn")]


@pytest.mark.asyncio
async def test_kill_switch_off_reverts_to_flat() -> None:
    # PERSONA_API_PROPORTIONAL_CREDITS=false — the approved rollback hatch:
    # byte-identical pre-M2 billing even with a big recorded cost.
    billing = _RecordingCredits()
    reg = _registry(billing, proportional=False)
    await _run_turn(reg, _CostLoop(cost_cents=5.3, cost_basis="actual_openrouter"))
    assert billing.deducts == [(1, "chat_turn")]


@pytest.mark.asyncio
async def test_floor_knob_is_respected_both_ways() -> None:
    # credits_per_turn stays the floor knob (the spec's parenthetical).
    billing = _RecordingCredits()
    reg = _registry(billing, credits_per_turn=2)
    await _run_turn(reg, _CostLoop(cost_cents=5.3, cost_basis="estimate_static"))
    await _run_turn(reg, _CostLoop(cost_cents=0.5, cost_basis="estimate_static"))
    assert billing.deducts == [
        (6, "chat_turn:estimate_static"),  # ceil above the floor
        (2, "chat_turn:estimate_static"),  # floor above the ceil
    ]


@pytest.mark.asyncio
async def test_non_finite_recorded_cost_charges_flat() -> None:
    # Money-path defense: a poisoned float must never overcharge.
    billing = _RecordingCredits()
    reg = _registry(billing)
    await _run_turn(reg, _CostLoop(cost_cents=float("nan"), cost_basis="estimate_static"))
    await _run_turn(reg, _CostLoop(cost_cents=float("inf"), cost_basis="estimate_static"))
    assert billing.deducts == [(1, "chat_turn"), (1, "chat_turn")]


@pytest.mark.asyncio
async def test_no_policy_means_no_billing_still() -> None:
    # The community/unmetered shape is untouched by T5.
    reg = ChatTurnRegistry(sink=_RecordingSink())  # type: ignore[arg-type]
    handle = await _run_turn(reg, _CostLoop(cost_cents=5.0, cost_basis="estimate_static"))
    assert handle.task is not None  # completed cleanly with zero deducts
