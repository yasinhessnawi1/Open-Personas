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
from loguru import logger as _loguru_logger
from persona.backends.types import StreamChunk
from persona.billing import BillingConfig
from persona.schema.conversation import Conversation
from persona_api.background.chat_turn_worker import ChatTurnHandle, ChatTurnRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

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
    """A CreditsPolicy double recording (amount, reason) + (cost_cents, cost_basis) per deduct."""

    def __init__(self) -> None:
        self.deducts: list[tuple[int, str]] = []
        #: Spec M3 (T1b): the recorded (cost_cents, cost_basis) per deduct — the
        #: true unclamped provider cost + provenance now on the ledger row.
        self.cost_records: list[tuple[float | None, str | None]] = []

    def deduct(
        self,
        *,
        rls_engine: object,  # noqa: ARG002
        user_id: str,  # noqa: ARG002
        amount: int,
        reason: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> int:
        self.deducts.append((amount, reason))
        self.cost_records.append((cost_cents, cost_basis))
        return 0


def _registry(
    billing: _RecordingCredits,
    *,
    credits_per_turn: int = 1,
    proportional: bool = True,
    max_turn_credits: int = 500,
    billing_config: BillingConfig | None = None,
) -> ChatTurnRegistry:
    return ChatTurnRegistry(
        sink=_RecordingSink(),  # type: ignore[arg-type]
        rls_engine=object(),  # type: ignore[arg-type] — the double ignores it
        credits_policy=billing,  # type: ignore[arg-type]
        credits_per_turn=credits_per_turn,
        proportional_credits=proportional,
        max_turn_credits=max_turn_credits,
        billing_config=billing_config,
    )


class _ShortfallCredits:
    """Spec M2 review (C1/I3) double: ``deduct`` always rejects (insufficient
    balance); ``capture_up_to`` records the call and reports a scripted
    partial capture — optionally raising ``DailySpendCapExceededError``
    itself (I3's defense-in-depth: the nested catch inside
    ``_capture_shortfall``)."""

    def __init__(self, *, captured: int = 1, day_cap_error: bool = False) -> None:
        self.deducts: list[tuple[int, str]] = []
        self.captures: list[tuple[int, str]] = []
        self._captured = captured
        self._day_cap_error = day_cap_error

    def deduct(
        self,
        *,
        rls_engine: object,  # noqa: ARG002
        user_id: str,  # noqa: ARG002
        amount: int,
        reason: str,
        **_kwargs: object,
    ) -> int:
        from persona.errors import CreditsExhaustedError

        self.deducts.append((amount, reason))
        raise CreditsExhaustedError("exhausted", context={"amount": str(amount)})

    def capture_up_to(
        self,
        *,
        rls_engine: object,  # noqa: ARG002
        user_id: str,  # noqa: ARG002
        amount: int,
        reason: str,
        **_kwargs: object,
    ) -> tuple[int, int]:
        self.captures.append((amount, reason))
        if self._day_cap_error:
            from persona.errors import DailySpendCapExceededError

            raise DailySpendCapExceededError("capped", context={"cap": "5"})
        return min(self._captured, amount), 0


@pytest.fixture
def loguru_capture() -> Iterator[list[str]]:
    """Loguru sink capturing every emitted message string (>= WARNING).

    The project's logging surface (``persona.logging.get_logger``) wraps
    loguru, so pytest's stdlib-only ``caplog`` does not see records — mirrors
    the pattern in ``test_tier_registry_multimodel.py``.
    """
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


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


# ---------------------------------------------------------------------------
# Spec M2 review — M2: bool excluded from the recorded-cost isinstance check.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bool_cost_excluded_charges_flat_no_exception() -> None:
    """``bool`` is an ``int`` subclass — an upstream bug that sets
    ``last_turn_cost_cents = True`` must not reach ``Decimal(str(True))``
    (an ``InvalidOperation`` crash); it charges the flat floor instead,
    symmetric with ``persona.backends.openai_compat._usage_cost_usd``'s own
    bool exclusion."""
    billing = _RecordingCredits()
    reg = _registry(billing)
    await _run_turn(reg, _CostLoop(cost_cents=True, cost_basis="estimate_static"))  # type: ignore[arg-type]
    assert billing.deducts == [(1, "chat_turn")]


# ---------------------------------------------------------------------------
# Spec M2 review — sanity ceiling: clamp the charged amount, never the
# persisted verbatim cost.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sanity_ceiling_clamps_an_absurd_charge_and_warns(
    loguru_capture: list[str],
) -> None:
    """An absurd computed charge (a unit-scale pricing bug shape, e.g. a
    resolver returning $/Mtok where cents/1k-tokens was expected — a 100x+
    blowup here) is clamped to ``max_turn_credits``; the loop's OWN recorded
    cost (the TurnLog's eventual source) is never mutated — only the charged
    amount is clamped — and a WARNING names both numbers plus the basis."""
    billing = _RecordingCredits()
    reg = _registry(billing)  # default max_turn_credits=500
    absurd_cost_cents = 50_000.0  # ceil() alone would charge 50,000 credits
    loop = _CostLoop(cost_cents=absurd_cost_cents, cost_basis="estimate_static")
    await _run_turn(reg, loop)
    assert billing.deducts == [(500, "chat_turn:estimate_static")]  # clamped to the ceiling
    assert loop.last_turn_cost_cents == absurd_cost_cents  # verbatim — never mutated
    warnings = [m for m in loguru_capture if "clamped to the sanity ceiling" in m]
    assert len(warnings) == 1
    assert "computed=50000" in warnings[0]
    assert "ceiling=500" in warnings[0]
    assert "basis=estimate_static" in warnings[0]


@pytest.mark.asyncio
async def test_sanity_ceiling_non_positive_disables_the_clamp() -> None:
    billing = _RecordingCredits()
    reg = _registry(billing, max_turn_credits=0)
    await _run_turn(reg, _CostLoop(cost_cents=50_000.0, cost_basis="estimate_static"))
    assert billing.deducts == [(50_000, "chat_turn:estimate_static")]  # unclamped


@pytest.mark.asyncio
async def test_sanity_ceiling_below_the_charge_is_configurable() -> None:
    # A tighter-than-default ceiling threaded through the constructor still clamps.
    billing = _RecordingCredits()
    reg = _registry(billing, max_turn_credits=10)
    await _run_turn(reg, _CostLoop(cost_cents=25.0, cost_basis="estimate_static"))
    assert billing.deducts == [(10, "chat_turn:estimate_static")]


@pytest.mark.asyncio
async def test_sanity_ceiling_does_not_clamp_under_the_limit() -> None:
    billing = _RecordingCredits()
    reg = _registry(billing)
    await _run_turn(reg, _CostLoop(cost_cents=5.3, cost_basis="estimate_static"))
    assert billing.deducts == [(6, "chat_turn:estimate_static")]  # unclamped, well under 500


# ---------------------------------------------------------------------------
# Spec M2 review — C1/I3: the partial-capture fallback wiring.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insufficient_balance_on_proportional_charge_falls_back_to_capture() -> None:
    """A genuinely-proportional charge (basis-qualified reason) that
    ``deduct`` rejects falls back to ``capture_up_to`` with the SAME
    amount/reason ``deduct`` was given."""
    billing = _ShortfallCredits(captured=1)
    reg = _registry(billing)  # type: ignore[arg-type]
    await _run_turn(reg, _CostLoop(cost_cents=5.3, cost_basis="estimate_static"))  # ceil=6
    assert billing.deducts == [(6, "chat_turn:estimate_static")]
    assert billing.captures == [(6, "chat_turn:estimate_static")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("proportional", "cost_cents", "basis"),
    [
        (False, 5.3, "actual_openrouter"),  # kill-switch OFF
        (True, 0.0, "unpriced"),  # unpriced, even with proportional billing ON
    ],
)
async def test_insufficient_balance_on_flat_floor_charge_never_captures(
    proportional: bool, cost_cents: float, basis: str
) -> None:
    """Pinned: the flat-floor path — kill-switch OFF, OR an unpriced/legacy
    turn even with proportional billing ON — classic all-or-nothing rejects.
    It NEVER falls back to ``capture_up_to``."""
    billing = _ShortfallCredits(captured=1)
    reg = _registry(billing, proportional=proportional)  # type: ignore[arg-type]
    await _run_turn(reg, _CostLoop(cost_cents=cost_cents, cost_basis=basis))
    assert billing.deducts == [(1, "chat_turn")]
    assert billing.captures == []  # never attempted


@pytest.mark.asyncio
async def test_capture_shortfall_day_cap_refusal_does_not_escape() -> None:
    """Spec M2 review (I3, defense-in-depth): even if ``capture_up_to``'s OWN
    day-cap booking is refused, the exception is caught here too — the turn
    still finishes cleanly (``await handle.task`` does not raise)."""
    billing = _ShortfallCredits(captured=1, day_cap_error=True)
    reg = _registry(billing)  # type: ignore[arg-type]
    handle = await _run_turn(reg, _CostLoop(cost_cents=5.3, cost_basis="estimate_static"))
    assert billing.captures == [(6, "chat_turn:estimate_static")]
    items: list[object] = []
    while not handle.events.empty():
        items.append(handle.events.get_nowait())
    kinds = [None if it is None else it[0] for it in items]  # type: ignore[index]
    assert "done" in kinds, "the completion branch must still be reached"
    assert kinds[-1] is None  # end-of-stream sentinel


# ---------------------------------------------------------------------------
# Spec M3 (T1b) — the MeteredBilling retrofit: byte-identical parity at
# markup=1.0 / infra=0 (the WHOLE matrix above is that proof, run at the
# default config), plus the new machinery: markup folding + the cost_cents/
# cost_basis columns carrying the TRUE unclamped cost (D-M3-12).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_config_is_byte_identical_parity() -> None:
    """The retrofit at the DEFAULT BillingConfig (markup 1.0, chat infra 0) is
    byte-identical to the pre-M3 ``max(floor, ceil(cost))`` — the same numbers
    the matrix above pins. An explicit restatement of the parity gate."""
    billing = _RecordingCredits()
    reg = _registry(billing, billing_config=BillingConfig())
    await _run_turn(reg, _CostLoop(cost_cents=5.3, cost_basis="actual_openrouter"))  # ceil 6
    await _run_turn(reg, _CostLoop(cost_cents=1.0, cost_basis="estimate_static"))  # exact → 1
    await _run_turn(reg, _CostLoop(cost_cents=2.9999999999, cost_basis="estimate_catalog"))  # 3
    assert [a for a, _ in billing.deducts] == [6, 1, 3]


@pytest.mark.asyncio
async def test_markup_scales_the_proportional_charge() -> None:
    """``PERSONA_CREDIT_MARKUP`` folds into the charge: 1.4 × 5.0c = 7.0c → 7
    (the M4 margin knob; M3 ships 1.0 so this is opt-in and does not fire by
    default)."""
    billing = _RecordingCredits()
    reg = _registry(billing, billing_config=BillingConfig(credit_markup=1.4))
    await _run_turn(reg, _CostLoop(cost_cents=5.0, cost_basis="estimate_static"))
    assert billing.deducts == [(7, "chat_turn:estimate_static")]


@pytest.mark.asyncio
async def test_cost_columns_carry_the_true_cost_and_basis() -> None:
    """D-M3-12: the ledger row records the TRUE provider cost + provenance."""
    billing = _RecordingCredits()
    reg = _registry(billing)
    await _run_turn(reg, _CostLoop(cost_cents=5.3, cost_basis="actual_openrouter"))
    assert billing.deducts == [(6, "chat_turn:actual_openrouter")]
    assert billing.cost_records == [(5.3, "actual_openrouter")]


@pytest.mark.asyncio
async def test_clamped_charge_still_logs_the_true_unclamped_cost() -> None:
    """D-M3-12 (the logged-cost divergence fix): the sanity ceiling clamps the
    CHARGE to 500, but the recorded ``cost_cents`` is the TRUE 50000.0 — the
    clamp never distorts what the turn actually cost."""
    billing = _RecordingCredits()
    reg = _registry(billing)  # max_turn_credits=500
    await _run_turn(reg, _CostLoop(cost_cents=50_000.0, cost_basis="estimate_static"))
    assert billing.deducts == [(500, "chat_turn:estimate_static")]  # charge clamped
    assert billing.cost_records == [(50_000.0, "estimate_static")]  # cost UNclamped


@pytest.mark.asyncio
async def test_flat_floor_still_records_the_true_cost_when_known() -> None:
    """Kill-switch OFF charges the flat floor, but the row still records the
    true cost + basis (telemetry-truthful — the charge is flat, the cost is not)."""
    billing = _RecordingCredits()
    reg = _registry(billing, proportional=False)
    await _run_turn(reg, _CostLoop(cost_cents=5.3, cost_basis="actual_openrouter"))
    assert billing.deducts == [(1, "chat_turn")]  # flat charge unchanged
    assert billing.cost_records == [(5.3, "actual_openrouter")]  # true cost still logged


@pytest.mark.asyncio
async def test_legacy_loop_records_no_cost_columns() -> None:
    """A loop exposing no valid recorded cost writes NULL cost columns."""
    billing = _RecordingCredits()
    reg = _registry(billing)
    await _run_turn(reg, _LegacyLoop())
    assert billing.deducts == [(1, "chat_turn")]
    assert billing.cost_records == [(None, None)]
