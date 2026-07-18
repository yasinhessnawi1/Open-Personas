"""Idempotent billing primitives (Spec M3, T1a — D-M3-R5).

``deduct_idempotent`` / ``capture_up_to_idempotent`` are the at-least-once-safe
siblings of ``deduct`` / ``capture_up_to``: a ``billing_key`` + an insert-first
``ON CONFLICT (billing_key) DO NOTHING`` gate makes a re-delivered background /
task op a clean no-op (no double-charge). These exercise the real atomic
Postgres path — the whole deduct is idempotent (balance mutation AND ledger row
move together, or not at all), and the new ``cost_cents`` / ``cost_basis``
columns persist.

The existing non-keyed ``deduct`` / ``capture_up_to`` are UNCHANGED — see
``test_credits_capture_up_to.py`` (they never take a ``billing_key`` and their
behaviour is byte-identical to pre-M3; the M3 columns default NULL).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from persona.credits import capture_up_to_idempotent, deduct_idempotent
from persona.errors import CreditsExhaustedError
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_USER = "u_idempotent_billing"


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


def _ledger(engine: Engine) -> list[tuple[int, str, object, object, object]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT delta, reason, cost_cents, cost_basis, billing_key "
                "FROM credit_transactions WHERE user_id = :u ORDER BY created_at, id"
            ),
            {"u": _USER},
        ).all()
    return [(int(r[0]), str(r[1]), r[2], r[3], r[4]) for r in rows]


# --- deduct_idempotent -------------------------------------------------------


def test_deduct_idempotent_first_delivery_charges_and_records_cost(seeded_engine: Engine) -> None:
    _set_balance(seeded_engine, 100)
    new_balance = deduct_idempotent(
        rls_engine=seeded_engine,
        user_id=_USER,
        amount=6,
        reason="task_leg",
        billing_key="task:t1:leg:1",
        cost_cents=5.3,
        cost_basis="estimate_static",
    )
    assert new_balance == 94
    assert _balance(seeded_engine) == 94
    row = _ledger(seeded_engine)[-1]
    assert row[:2] == (-6, "task_leg")
    assert row[2] == pytest.approx(5.3)  # cost_cents persisted (Float, sub-cent-safe)
    assert row[3] == "estimate_static"
    assert row[4] == "task:t1:leg:1"


def test_deduct_idempotent_redelivery_is_a_noop(seeded_engine: Engine) -> None:
    _set_balance(seeded_engine, 100)
    kwargs = {
        "rls_engine": seeded_engine,
        "user_id": _USER,
        "amount": 6,
        "reason": "task_leg",
        "billing_key": "task:t1:leg:1",
        "cost_cents": 5.3,
        "cost_basis": "estimate_static",
    }
    first = deduct_idempotent(**kwargs)  # type: ignore[arg-type]
    second = deduct_idempotent(**kwargs)  # type: ignore[arg-type]  # the re-delivery
    assert first == 94
    assert second == 94, "the re-delivery must NOT charge again"
    assert _balance(seeded_engine) == 94
    assert len(_ledger(seeded_engine)) == 1, "the re-delivery writes no second ledger row"


def test_deduct_idempotent_unaffordable_first_delivery_raises_with_no_orphan_row(
    seeded_engine: Engine,
) -> None:
    """A strict idempotent deduct that fails the balance floor rolls the ledger
    insert back too — no orphan row, and the billing_key stays free to retry."""
    _set_balance(seeded_engine, 2)
    ledger_before = _ledger(seeded_engine)
    with pytest.raises(CreditsExhaustedError):
        deduct_idempotent(
            rls_engine=seeded_engine,
            user_id=_USER,
            amount=6,
            reason="task_leg",
            billing_key="task:t1:leg:1",
            cost_cents=5.3,
            cost_basis="estimate_static",
        )
    assert _balance(seeded_engine) == 2, "the refused deduct must not touch the balance"
    assert _ledger(seeded_engine) == ledger_before, "no orphan row from the rolled-back insert"


def test_deduct_idempotent_concurrent_same_key_charges_exactly_once(seeded_engine: Engine) -> None:
    """Two threads deliver the SAME billing_key at once; the ON CONFLICT gate
    lets exactly one charge land (the at-least-once double-charge guard)."""
    _set_balance(seeded_engine, 100)

    def _attempt(_: int) -> int:
        return deduct_idempotent(
            rls_engine=seeded_engine,
            user_id=_USER,
            amount=6,
            reason="task_leg",
            billing_key="task:t1:leg:race",
            cost_cents=5.3,
            cost_basis="estimate_static",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(_attempt, range(2)))

    assert _balance(seeded_engine) == 94, "exactly one of the two deliveries charged"
    assert len(_ledger(seeded_engine)) == 1


# --- capture_up_to_idempotent ------------------------------------------------


def test_capture_idempotent_first_delivery_captures_and_records(seeded_engine: Engine) -> None:
    _set_balance(seeded_engine, 100)
    captured, new_balance = capture_up_to_idempotent(
        rls_engine=seeded_engine,
        user_id=_USER,
        amount=6,
        reason="voice",
        billing_key="voice:s1:turn:1",
        cost_cents=5.3,
        cost_basis="provider_meter",
    )
    assert (captured, new_balance) == (6, 94)
    row = _ledger(seeded_engine)[-1]
    assert row[:2] == (-6, "voice")
    assert row[2] == pytest.approx(5.3)
    assert row[3] == "provider_meter"
    assert row[4] == "voice:s1:turn:1"


def test_capture_idempotent_partial_marks_shortfall(seeded_engine: Engine) -> None:
    _set_balance(seeded_engine, 2)
    captured, new_balance = capture_up_to_idempotent(
        rls_engine=seeded_engine,
        user_id=_USER,
        amount=6,
        reason="voice",
        billing_key="voice:s1:turn:1",
        cost_cents=5.3,
        cost_basis="provider_meter",
    )
    assert (captured, new_balance) == (2, 0)
    row = _ledger(seeded_engine)[-1]
    assert row[0] == -2
    assert row[1] == "voice:shortfall"  # partial capture is honest about underpayment


def test_capture_idempotent_redelivery_is_a_noop(seeded_engine: Engine) -> None:
    _set_balance(seeded_engine, 100)
    kwargs = {
        "rls_engine": seeded_engine,
        "user_id": _USER,
        "amount": 6,
        "reason": "voice",
        "billing_key": "voice:s1:turn:1",
        "cost_cents": 5.3,
        "cost_basis": "provider_meter",
    }
    first = capture_up_to_idempotent(**kwargs)  # type: ignore[arg-type]
    second = capture_up_to_idempotent(**kwargs)  # type: ignore[arg-type]  # the re-delivery
    assert first == (6, 94)
    assert second == (0, 94), "the re-delivery captures nothing and does not move the balance"
    assert len(_ledger(seeded_engine)) == 1
