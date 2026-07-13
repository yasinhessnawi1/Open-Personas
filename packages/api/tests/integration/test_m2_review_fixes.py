"""Worker-level full-arc integration proofs for the Spec M2 final-review fixes.

Drives the REAL ``ChatTurnRegistry`` + REAL ``MeteredCreditsPolicy`` against a
real Postgres (:5436) — the same harness ``test_m2_proportional_credits.py``
uses — for the two review findings whose fix crosses the worker/policy/core
boundary:

* C1 (CRITICAL): a proportional charge that exceeds the remaining balance now
  captures the shortfall (floored at 0) instead of deducting nothing forever.
  The review's own reproduction: balance 2, charge 6 -> ledger -2 with a
  shortfall reason, day-cap books 2, balance 0, the NEXT turn's pre-flight
  402s. Kill-switch OFF is pinned unaffected (a flat charge classic-rejects).
* I3: a post-success ``DailySpendCapExceededError`` no longer escapes
  ``_deduct`` — the turn's completion branch (the ``done`` event) is still
  reached, no exception propagates through ``handle.task``, no bill is
  written, and the policy's durable audit row is present.

The core-service primitive (``persona.credits.capture_up_to``) is exercised
directly in ``test_credits_capture_up_to.py``; this file proves the WORKER
wires it correctly end to end.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.backends.types import StreamChunk
from persona.credits import require_credits
from persona.schema.conversation import Conversation
from persona_api.background.chat_turn_worker import ChatTurnHandle, ChatTurnRegistry
from persona_api.editions.credits_policy import MeteredCreditsPolicy
from persona_api.errors import CreditsExhaustedError
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from persona_runtime.agentic.events import RunEvent
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_OWNER = "u_m2_review"
_PERSONA = "p_m2_review"
_CONV = "c_m2_review"
_MSG = "m_m2_review"


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
        return int(
            conn.execute(
                text("SELECT balance FROM credits WHERE user_id = :u"), {"u": _OWNER}
            ).scalar_one()
        )


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


def _audit_count(engine: Engine) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log WHERE user_id = :u "
                    "AND action = 'daily_spend_cap_exceeded'"
                ),
                {"u": _OWNER},
            ).scalar_one()
        )


async def _drive_turn(
    engine: Engine,
    policy: MeteredCreditsPolicy,
    loop: _CostLoop,
    *,
    proportional: bool = True,
    credits_per_turn: int = 1,
) -> ChatTurnHandle:
    reg = ChatTurnRegistry(
        sink=_NullSink(),  # type: ignore[arg-type]
        rls_engine=engine,
        credits_policy=policy,
        credits_per_turn=credits_per_turn,
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
    await handle.task  # must never raise (I3)
    return handle


def _drain_kinds(handle: ChatTurnHandle) -> list[str | None]:
    items: list[object] = []
    while not handle.events.empty():
        items.append(handle.events.get_nowait())
    return [None if it is None else it[0] for it in items]  # type: ignore[index]


@pytest.fixture
def seeded(pg_engine: Engine) -> Engine:
    _seed(pg_engine)
    return pg_engine


# ----------------------------------------------------------------------------------- #
# C1 (CRITICAL) — the review's own reproduction, driven through the real worker chain.
# ----------------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_c1_partial_capture_full_arc(seeded: Engine) -> None:
    """balance 2, charge 6 -> ledger -2 with shortfall reason, day-cap +2,
    balance 0, and the NEXT turn's pre-flight 402s."""
    _set_balance(seeded, 2)
    policy = MeteredCreditsPolicy(daily_cap=1000)
    # cost 5.3c -> ceil(5.3) = 6, matching the review's own reproduction.
    handle = await _drive_turn(
        seeded, policy, _CostLoop(cost_cents=5.3, cost_basis="estimate_static")
    )

    assert _ledger(seeded)[-1] == (-2, "chat_turn:estimate_static:shortfall")
    assert _day_spent(seeded) == 2
    assert _balance(seeded) == 0
    assert "done" in _drain_kinds(handle), "the turn's completion branch must still be reached"

    # The next turn's pre-flight gate refuses outright — the C1 payoff.
    with pytest.raises(CreditsExhaustedError):
        require_credits(rls_engine=seeded, user_id=_OWNER)


@pytest.mark.asyncio
async def test_c1_kill_switch_off_never_partial_captures(seeded: Engine) -> None:
    """Pinned: with proportional billing OFF (the kill-switch), a shortfall
    classic all-or-nothing rejects — no ledger row, no partial capture —
    even though the SAME balance/cost combination would partially capture
    with proportional billing ON (see ``test_c1_partial_capture_full_arc``)."""
    _set_balance(seeded, 1)
    policy = MeteredCreditsPolicy()
    before = _ledger(seeded)
    handle = await _drive_turn(
        seeded,
        policy,
        _CostLoop(cost_cents=5.3, cost_basis="actual_openrouter"),
        proportional=False,
        credits_per_turn=2,  # the flat charge (2) still exceeds the balance (1)
    )
    assert _balance(seeded) == 1, "classic-reject leaves the balance untouched"
    assert _ledger(seeded) == before, "classic-reject writes no ledger row"
    assert "done" in _drain_kinds(handle)  # the turn still completes; only the bill is skipped


# ----------------------------------------------------------------------------------- #
# I3 — a post-success DailySpendCapExceededError must not escape _deduct.
# ----------------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_i3_day_cap_exceeded_post_success_does_not_escape(seeded: Engine) -> None:
    """A plentiful balance but a tiny day-cap: the FULL requested amount (6)
    breaches the cap (5) before the balance is even considered, so ``deduct``
    raises ``DailySpendCapExceededError`` directly. The turn's completion
    branch must still be reached, no exception escapes ``handle.task``, no
    bill is written, and the policy's durable audit row is present."""
    _set_balance(seeded, 1000)
    policy = MeteredCreditsPolicy(daily_cap=5)
    before = _ledger(seeded)
    handle = await _drive_turn(
        seeded,
        policy,
        _CostLoop(cost_cents=5.3, cost_basis="estimate_static"),  # ceil=6 > cap 5
    )

    assert _ledger(seeded) == before, "a day-cap refusal must bill nothing"
    assert _balance(seeded) == 1000, "the balance must be untouched"
    assert _day_spent(seeded) == 0, "the refused booking must not move the day counter"
    kinds = _drain_kinds(handle)
    assert "done" in kinds, "I3: the completion branch must still be reached"
    assert kinds[-1] is None  # end-of-stream sentinel
    assert _audit_count(seeded) >= 1, "the day-cap refusal must still be durably audited"
