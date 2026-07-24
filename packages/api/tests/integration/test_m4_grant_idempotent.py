"""Adversarial: ``grant_idempotent`` grants the allowance bucket exactly once (Spec M4, T1b).

The exactly-once positive-delta grant primitive the Stripe webhook rides. Stripe
delivers at-least-once, so a re-delivered ``invoice.paid`` MUST grant exactly once —
the insert-first ``billing_key`` gate (``ON CONFLICT DO NOTHING`` on the unique index)
makes a re-delivery a clean no-op. Mirrors ``test_credits_idempotent_billing`` on the
deduct side. Grants credit the ALLOWANCE bucket (``credits.balance``); the return is
the two-bucket total.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.credits import grant_idempotent
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_USER = "u_grant_idem"


@pytest.fixture
def seeded_engine(pg_engine: Engine) -> Engine:
    with pg_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": _USER, "e": f"{_USER}@x.test"},
        )
    return pg_engine


def _set_allowance(engine: Engine, balance: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO credits (user_id, balance) VALUES (:u, :b) "
                "ON CONFLICT (user_id) DO UPDATE SET balance = :b"
            ),
            {"u": _USER, "b": balance},
        )


def _allowance(engine: Engine) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text("SELECT balance FROM credits WHERE user_id = :u"), {"u": _USER}
            ).scalar_one()
        )


def _grant_rows(engine: Engine) -> list[tuple[int, str, object]]:
    """(delta, reason, cost_basis) for the user's POSITIVE ledger rows (grants)."""
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT delta, reason, cost_basis FROM credit_transactions "
                "WHERE user_id = :u AND delta > 0 ORDER BY created_at, id"
            ),
            {"u": _USER},
        ).all()
    return [(int(r[0]), str(r[1]), r[2]) for r in rows]


def _add_lot(engine: Engine, *, remaining: int, source_key: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO payg_grants "
                "(user_id, credits_total, credits_remaining, expires_at, source_billing_key) "
                "VALUES (:u, :t, :r, :exp, :sk)"
            ),
            {
                "u": _USER,
                "t": remaining,
                "r": remaining,
                "exp": datetime.now(UTC) + timedelta(days=365),
                "sk": source_key,
            },
        )


def test_grant_once_credits_the_allowance_bucket(seeded_engine: Engine) -> None:
    _set_allowance(seeded_engine, 100)
    total = grant_idempotent(
        rls_engine=seeded_engine,
        user_id=_USER,
        amount=500,
        reason="subscription_renewal",
        billing_key="in_evt_1",
        cost_basis="grant_subscription",
    )
    assert total == 600  # 100 + 500
    assert _allowance(seeded_engine) == 600  # credited the allowance bucket
    rows = _grant_rows(seeded_engine)
    assert rows == [(500, "subscription_renewal", "grant_subscription")]


def test_grant_redelivered_five_times_grants_exactly_once(seeded_engine: Engine) -> None:
    """The load-bearing webhook safety: Stripe re-delivers ``invoice.paid`` → the
    same ``billing_key`` grants the allowance exactly once."""
    _set_allowance(seeded_engine, 0)
    results = [
        grant_idempotent(
            rls_engine=seeded_engine,
            user_id=_USER,
            amount=500,
            reason="subscription_renewal",
            billing_key="in_evt_redelivered",
            cost_basis="grant_subscription",
        )
        for _ in range(5)
    ]
    assert results == [500, 500, 500, 500, 500]  # every call returns the SAME total
    assert _allowance(seeded_engine) == 500  # granted once, not 5×
    assert len(_grant_rows(seeded_engine)) == 1  # exactly one ledger row


def test_concurrent_grant_double_fire_single_grant(seeded_engine: Engine) -> None:
    """Concurrent re-delivery of the same event → the insert-first gate + unique
    index serialise it to a single grant (no double-grant race)."""
    _set_allowance(seeded_engine, 0)

    def _fire(_: int) -> int:
        return grant_idempotent(
            rls_engine=seeded_engine,
            user_id=_USER,
            amount=500,
            reason="subscription_renewal",
            billing_key="in_evt_concurrent",
            cost_basis="grant_subscription",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_fire, range(8)))

    assert _allowance(seeded_engine) == 500  # exactly one grant landed
    assert len(_grant_rows(seeded_engine)) == 1


def test_grant_returns_two_bucket_total_and_leaves_payg_untouched(seeded_engine: Engine) -> None:
    _set_allowance(seeded_engine, 0)
    _add_lot(seeded_engine, remaining=100, source_key="lot")
    total = grant_idempotent(
        rls_engine=seeded_engine,
        user_id=_USER,
        amount=300,
        reason="free_refresh",
        billing_key="free_2026_07",
        cost_basis="grant_free_refresh",
    )
    assert total == 400  # allowance 300 (granted) + PAYG lot 100
    assert _allowance(seeded_engine) == 300  # grant went to the allowance bucket only
    with seeded_engine.begin() as conn:
        lot = int(
            conn.execute(
                text("SELECT credits_remaining FROM payg_grants WHERE source_billing_key = 'lot'")
            ).scalar_one()
        )
    assert lot == 100  # PAYG lot untouched by an allowance grant


def test_distinct_keys_grant_separately(seeded_engine: Engine) -> None:
    _set_allowance(seeded_engine, 0)
    grant_idempotent(
        rls_engine=seeded_engine,
        user_id=_USER,
        amount=300,
        reason="free_refresh",
        billing_key="free_2026_07",
        cost_basis="grant_free_refresh",
    )
    total = grant_idempotent(
        rls_engine=seeded_engine,
        user_id=_USER,
        amount=500,
        reason="subscription_renewal",
        billing_key="in_evt_2",
        cost_basis="grant_subscription",
    )
    assert total == 800  # two distinct grants both landed
    assert _allowance(seeded_engine) == 800
    assert len(_grant_rows(seeded_engine)) == 2
