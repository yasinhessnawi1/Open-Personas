"""Direct core-service tests for ``persona.credits.capture_up_to`` (Spec M2 review, C1).

The opt-in partial-capture sibling of ``deduct``: charges ``min(amount,
balance)`` in ONE atomic statement, floored at 0, instead of ``deduct``'s
all-or-nothing rejection (the bug: a charge exceeding the remaining balance
used to deduct NOTHING and raise — every time, forever, once the balance sat
below a turn's true cost, so an already-completed expensive turn became
permanently unbillable while pre-flight kept passing it).

These tests exercise the primitive DIRECTLY (real Postgres, the real atomic
CTE-based UPDATE) — the worker-level wiring (WHEN it falls back to this vs.
classic ``deduct``) is covered separately in
``test_chat_turn_worker_proportional.py`` (unit, scripted doubles) and
``test_m2_review_fixes.py`` (integration, the full worker chain, including
the review's own "balance 2, charge 6" arc end to end).

``deduct``'s existing all-or-nothing semantics are UNTOUCHED by this
function's existence — see ``test_credits_capture_scope_guard.py`` (unit,
always-on) for the source-level pin that every OTHER metered caller
(authoring, imagegen, sandbox) still calls ``deduct``, never this.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from persona.credits import capture_up_to
from persona.errors import DailySpendCapExceededError
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_USER = "u_capture_up_to"


@pytest.fixture
def seeded_engine(pg_engine: Engine) -> Engine:
    with pg_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": _USER, "e": f"{_USER}@x.test"},
        )
    return pg_engine


def _set_balance(engine: Engine, balance: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO credits (user_id, balance) VALUES (:u, :b) "
                "ON CONFLICT (user_id) DO UPDATE SET balance = :b"
            ),
            {"u": _USER, "b": balance},
        )


def _balance(engine: Engine) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text("SELECT balance FROM credits WHERE user_id = :u"), {"u": _USER}
            ).scalar_one()
        )


def _ledger(engine: Engine) -> list[tuple[int, str]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT delta, reason FROM credit_transactions "
                "WHERE user_id = :u ORDER BY created_at, id"
            ),
            {"u": _USER},
        ).all()
    return [(int(r[0]), str(r[1])) for r in rows]


def _day_spent(engine: Engine) -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT COALESCE(SUM(spent), 0) FROM day_spend WHERE user_id = :u"),
            {"u": _USER},
        ).scalar_one()
    return int(row)


def test_full_capture_when_balance_covers_the_amount(seeded_engine: Engine) -> None:
    _set_balance(seeded_engine, 100)
    captured, new_balance = capture_up_to(
        rls_engine=seeded_engine, user_id=_USER, amount=30, reason="chat_turn:estimate_static"
    )
    assert (captured, new_balance) == (30, 70)
    assert _balance(seeded_engine) == 70
    # A FULL capture keeps the bare reason — byte-identical to what ``deduct`` writes.
    assert _ledger(seeded_engine)[-1] == (-30, "chat_turn:estimate_static")


def test_partial_capture_floors_at_zero_and_marks_shortfall(seeded_engine: Engine) -> None:
    """The review's own reproduction numbers: balance 2, charge 6."""
    _set_balance(seeded_engine, 2)
    captured, new_balance = capture_up_to(
        rls_engine=seeded_engine, user_id=_USER, amount=6, reason="chat_turn:estimate_static"
    )
    assert (captured, new_balance) == (2, 0)
    assert _balance(seeded_engine) == 0
    assert _ledger(seeded_engine)[-1] == (-2, "chat_turn:estimate_static:shortfall")


def test_zero_balance_captures_nothing_and_writes_no_ledger_row(seeded_engine: Engine) -> None:
    _set_balance(seeded_engine, 0)
    before = _ledger(seeded_engine)
    captured, new_balance = capture_up_to(
        rls_engine=seeded_engine, user_id=_USER, amount=5, reason="chat_turn:estimate_static"
    )
    assert (captured, new_balance) == (0, 0)
    assert _ledger(seeded_engine) == before  # no $0 ledger row


@pytest.mark.parametrize("amount", [0, -5])
def test_non_positive_amount_is_a_noop(seeded_engine: Engine, amount: int) -> None:
    _set_balance(seeded_engine, 10)
    before = _ledger(seeded_engine)
    captured, new_balance = capture_up_to(
        rls_engine=seeded_engine, user_id=_USER, amount=amount, reason="chat_turn:estimate_static"
    )
    assert (captured, new_balance) == (0, 10)
    assert _balance(seeded_engine) == 10
    assert _ledger(seeded_engine) == before


def test_day_cap_books_the_captured_amount_not_the_requested_one(seeded_engine: Engine) -> None:
    _set_balance(seeded_engine, 2)
    captured, _new_balance = capture_up_to(
        rls_engine=seeded_engine,
        user_id=_USER,
        amount=6,
        reason="chat_turn:estimate_static",
        daily_cap=1000,
    )
    assert captured == 2
    assert _day_spent(seeded_engine) == 2  # the CAPTURED amount, not the requested 6


def test_day_cap_refusal_rolls_back_the_whole_capture(seeded_engine: Engine) -> None:
    """A prior committed spend already sits close to the cap; even the smaller
    CAPTURED amount would breach it — ``capture_up_to`` raises and rolls back
    everything (balance untouched, no ledger row, day-spend untouched), the
    identical fail-closed discipline ``deduct`` already uses."""
    _set_balance(seeded_engine, 100)
    # Commit 9 of a 10-cap via a first, affordable (full) capture.
    capture_up_to(rls_engine=seeded_engine, user_id=_USER, amount=9, reason="warmup", daily_cap=10)
    assert _day_spent(seeded_engine) == 9

    _set_balance(seeded_engine, 2)  # now also a shortfall scenario
    ledger_before = _ledger(seeded_engine)

    with pytest.raises(DailySpendCapExceededError) as ei:
        capture_up_to(
            rls_engine=seeded_engine,
            user_id=_USER,
            amount=6,
            reason="chat_turn:estimate_static",
            daily_cap=10,
        )
    assert ei.value.context["requested_cost"] == "2"  # the CAPTURED amount, not 6
    assert _balance(seeded_engine) == 2, "the refused capture must not touch the balance"
    assert _ledger(seeded_engine) == ledger_before, "the refused capture writes no ledger row"
    assert _day_spent(seeded_engine) == 9, "the refused capture must not move the day counter"


def test_concurrent_partial_captures_cannot_overdraw(seeded_engine: Engine) -> None:
    """Adversarial proof (mirrors ``test_credits_double_spend.py``): N threads
    each attempt to capture 1 credit against a balance that only covers some
    of them. The atomic floor must prevent any overdraw even on the
    partial-capture path — sum(captured) never exceeds the initial balance,
    the balance floors at exactly 0 (never negative), and the ledger row
    count matches the number of non-zero captures."""
    engine = seeded_engine
    n_threads = 8
    affordable = 5
    _set_balance(engine, affordable)

    def _attempt(_: int) -> int:
        captured, _new_balance = capture_up_to(
            rls_engine=engine, user_id=_USER, amount=1, reason="probe"
        )
        return captured

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        results = list(pool.map(_attempt, range(n_threads)))

    assert sum(results) == affordable, f"expected exactly {affordable} credits captured total"
    assert results.count(1) == affordable
    assert results.count(0) == n_threads - affordable
    assert _balance(engine) == 0, "balance must floor at 0, never negative"
    assert len(_ledger(engine)) == affordable, "only the non-zero captures wrote a ledger row"
