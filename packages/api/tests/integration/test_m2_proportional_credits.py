"""M2-T5 — proportional credits against the REAL ledger (:5436; D-M2-5).

The money-leg integration proofs, on a real Postgres + the real
:class:`MeteredCreditsPolicy` (credits_policy.py UNTOUCHED by T5 — F7):

* the detached worker's deduct writes a ledger row whose ``delta`` carries the
  proportional amount and whose ``reason`` carries the basis
  (``chat_turn:<basis>`` — the R7 audit constraint),
* the R7 day-cap books the SAME proportional amount atomically, and an
  over-cap spend still fails loud + audited (existing mechanics, proportional
  amounts flowing through),
* the 402 pre-flight (``require_credits``) is UNCHANGED — it never considers
  the upcoming turn's proportional size,
* post-success insufficient balance is UNCHANGED — the conditional atomic
  decrement rejects (no ledger row, balance never negative), the worker
  catches and the turn still completes,
* the kill-switch OFF arm bills the pre-M2 flat charge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.backends.types import StreamChunk
from persona.credits import require_credits
from persona.errors import DailySpendCapExceededError
from persona.schema.conversation import Conversation
from persona_api.background.chat_turn_worker import ChatTurnRegistry
from persona_api.editions.credits_policy import MeteredCreditsPolicy
from persona_api.errors import CreditsExhaustedError
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from persona_runtime.agentic.events import RunEvent
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_OWNER = "u_m2_prop"
_PERSONA = "p_m2_prop"
_CONV = "c_m2_prop"
_MSG = "m_m2_prop"


class _CostLoop:
    def __init__(self, *, cost_cents: float | None, cost_basis: str | None) -> None:
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


class _NullSink:
    def checkpoint(self, **kwargs: object) -> None:  # noqa: ARG002
        return None

    def finalize(self, **kwargs: object) -> None:  # noqa: ARG002
        return None


def _seed(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": _OWNER, "e": f"{_OWNER}@x.test"},
        )


def _set_balance(engine: Engine, balance: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO credits (user_id, balance) VALUES (:u, :b) "
                "ON CONFLICT (user_id) DO UPDATE SET balance = :b"
            ),
            {"u": _OWNER, "b": balance},
        )


def _balance(engine: Engine) -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT balance FROM credits WHERE user_id = :u"), {"u": _OWNER}
        ).scalar_one()
    return int(row)


def _ledger(engine: Engine) -> list[tuple[int, str]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT delta, reason FROM credit_transactions "
                "WHERE user_id = :u ORDER BY created_at, id"
            ),
            {"u": _OWNER},
        ).all()
    return [(int(r[0]), str(r[1])) for r in rows]


def _day_spent(engine: Engine) -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT COALESCE(SUM(spent), 0) FROM day_spend WHERE user_id = :u"),
            {"u": _OWNER},
        ).scalar_one()
    return int(row)


async def _drive_turn(
    engine: Engine,
    policy: MeteredCreditsPolicy,
    loop: _CostLoop,
    *,
    proportional: bool = True,
) -> None:
    reg = ChatTurnRegistry(
        sink=_NullSink(),  # type: ignore[arg-type]
        rls_engine=engine,
        credits_policy=policy,
        credits_per_turn=1,
        proportional_credits=proportional,
    )
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


@pytest.fixture
def seeded(pg_engine: Engine) -> Engine:
    _seed(pg_engine)
    return pg_engine


@pytest.mark.asyncio
async def test_ledger_row_carries_proportional_amount_and_basis(seeded: Engine) -> None:
    _set_balance(seeded, 100)
    # cost 1.5c, estimate basis → amount ceil(1.5)=2, reason carries the basis.
    await _drive_turn(
        seeded, MeteredCreditsPolicy(), _CostLoop(cost_cents=1.5, cost_basis="estimate_static")
    )
    assert _ledger(seeded)[-1] == (-2, "chat_turn:estimate_static")
    assert _balance(seeded) == 98


@pytest.mark.asyncio
async def test_day_cap_books_the_proportional_amount(seeded: Engine) -> None:
    _set_balance(seeded, 100)
    policy = MeteredCreditsPolicy(daily_cap=10)
    # cost 5.5c → amount 6: the day counter must book 6, not the flat 1 (R7
    # operating on the SAME credits unit by construction).
    await _drive_turn(seeded, policy, _CostLoop(cost_cents=5.5, cost_basis="actual_openrouter"))
    assert _day_spent(seeded) == 6
    assert _ledger(seeded)[-1] == (-6, "chat_turn:actual_openrouter")
    # A second proportional spend of 6 would breach the cap of 10 → the
    # existing R7 fail-loud + audited refusal, with the proportional amount
    # in the refusal context. Direct policy call (the worker's over-cap
    # behaviour is R7's, unchanged by T5).
    with pytest.raises(DailySpendCapExceededError):
        policy.deduct(rls_engine=seeded, user_id=_OWNER, amount=6, reason="chat_turn:test")
    with seeded.begin() as conn:
        audit = conn.execute(
            text(
                "SELECT COUNT(*) FROM audit_log WHERE user_id = :u "
                "AND action = 'daily_spend_cap_exceeded'"
            ),
            {"u": _OWNER},
        ).scalar_one()
    assert int(audit) >= 1
    assert _day_spent(seeded) == 6  # the refused spend booked nothing


@pytest.mark.asyncio
async def test_402_preflight_is_unchanged(seeded: Engine) -> None:
    # The pre-flight gate never considers the upcoming proportional size:
    # balance 1 passes even though the next turn may cost more (the
    # post-success conditional floor is the point of truth) …
    _set_balance(seeded, 1)
    assert require_credits(rls_engine=seeded, user_id=_OWNER) == 1
    # … and an empty balance still refuses the turn outright (→ 402).
    _set_balance(seeded, 0)
    with pytest.raises(CreditsExhaustedError):
        require_credits(rls_engine=seeded, user_id=_OWNER)


@pytest.mark.asyncio
async def test_insufficient_balance_post_success_is_unchanged(seeded: Engine) -> None:
    # Balance 1, proportional amount 3: the conditional atomic decrement
    # REJECTS (no ledger row, balance untouched — never negative), the worker
    # catches, the turn still completes. Byte-identical R2 F-04 semantics.
    _set_balance(seeded, 1)
    before = _ledger(seeded)
    await _drive_turn(
        seeded, MeteredCreditsPolicy(), _CostLoop(cost_cents=2.5, cost_basis="estimate_static")
    )
    assert _balance(seeded) == 1
    assert _ledger(seeded) == before  # a failed decrement records nothing


@pytest.mark.asyncio
async def test_kill_switch_off_bills_flat(seeded: Engine) -> None:
    _set_balance(seeded, 100)
    await _drive_turn(
        seeded,
        MeteredCreditsPolicy(),
        _CostLoop(cost_cents=5.3, cost_basis="actual_openrouter"),
        proportional=False,
    )
    assert _ledger(seeded)[-1] == (-1, "chat_turn")
    assert _balance(seeded) == 99
