"""``reset_allowance_idempotent`` — the overwrite-reset primitive (Spec M4, T3b).

Core-level pins (the webhook end-to-end path is in ``test_stripe_webhook_handlers``):
the reset OVERWRITES the allowance bucket (no rollover), leaves PAYG lots untouched,
records the honest ``delta = new - old`` ledger row, stamps ``allowance_period``, and is
idempotent on the billing_key (a re-delivery does not overwrite).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.credits import reset_allowance_idempotent
from persona_api.db.models import credit_transactions as credit_tx_t
from persona_api.db.models import payg_grants as payg_t
from sqlalchemy import func, select, text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_USER = "u_reset"


@pytest.fixture
def seeded_engine(pg_engine: Engine) -> Engine:
    with pg_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": _USER, "e": f"{_USER}@x.test"},
        )
    return pg_engine


def _set_allowance(engine: Engine, balance: int, period: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO credits (user_id, balance, allowance_period) VALUES (:u, :b, :p) "
                "ON CONFLICT (user_id) DO UPDATE SET balance = :b, allowance_period = :p"
            ),
            {"u": _USER, "b": balance, "p": period},
        )


def _row(engine: Engine) -> tuple[int, str | None]:
    with engine.begin() as conn:
        r = conn.execute(
            text("SELECT balance, allowance_period FROM credits WHERE user_id = :u"), {"u": _USER}
        ).one()
    return int(r[0]), r[1]


def _add_lot(engine: Engine, remaining: int, source_key: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            payg_t.insert().values(
                user_id=_USER,
                credits_total=remaining,
                credits_remaining=remaining,
                expires_at=datetime.now(UTC) + timedelta(days=365),
                source_billing_key=source_key,
            )
        )


def test_reset_overwrites_allowance_and_records_the_delta(seeded_engine: Engine) -> None:
    _set_allowance(seeded_engine, 300)
    total = reset_allowance_idempotent(
        rls_engine=seeded_engine,
        user_id=_USER,
        allowance=2000,
        allowance_period="2026-07",
        reason="subscription_renewal",
        billing_key="in_1",
        cost_basis="grant_subscription",
    )
    assert total == 2000
    balance, period = _row(seeded_engine)
    assert balance == 2000  # overwrite, not increment
    assert period == "2026-07"  # period stamped
    with seeded_engine.begin() as conn:
        delta = conn.execute(
            select(credit_tx_t.c.delta).where(credit_tx_t.c.billing_key == "in_1")
        ).scalar_one()
    assert delta == 1700  # honest net change = 2000 - 300


def test_reset_leaves_payg_lots_untouched(seeded_engine: Engine) -> None:
    _set_allowance(seeded_engine, 300)
    _add_lot(seeded_engine, 100, "lot")
    total = reset_allowance_idempotent(
        rls_engine=seeded_engine,
        user_id=_USER,
        allowance=2000,
        allowance_period="2026-07",
        reason="subscription_renewal",
        billing_key="in_1",
    )
    assert total == 2100  # 2000 allowance + 100 PAYG
    with seeded_engine.begin() as conn:
        lot = conn.execute(
            select(payg_t.c.credits_remaining).where(payg_t.c.source_billing_key == "lot")
        ).scalar_one()
    assert lot == 100  # the PAYG lot is not reset


def test_reset_redelivered_does_not_overwrite(seeded_engine: Engine) -> None:
    _set_allowance(seeded_engine, 300)
    reset_allowance_idempotent(
        rls_engine=seeded_engine,
        user_id=_USER,
        allowance=2000,
        allowance_period="2026-07",
        reason="subscription_renewal",
        billing_key="in_1",
    )
    # The user spends 500.
    with seeded_engine.begin() as conn:
        conn.execute(text(f"UPDATE credits SET balance = balance - 500 WHERE user_id = '{_USER}'"))  # noqa: S608
    # Same invoice re-delivered → NO overwrite (the gate holds).
    total = reset_allowance_idempotent(
        rls_engine=seeded_engine,
        user_id=_USER,
        allowance=2000,
        allowance_period="2026-07",
        reason="subscription_renewal",
        billing_key="in_1",
    )
    assert total == 1500  # preserved, NOT re-inflated to 2000
    with seeded_engine.begin() as conn:
        rows = conn.execute(
            select(func.count()).select_from(credit_tx_t).where(credit_tx_t.c.billing_key == "in_1")
        ).scalar_one()
    assert rows == 1  # exactly one reset ledger row
