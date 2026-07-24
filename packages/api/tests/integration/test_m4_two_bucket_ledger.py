"""Two-bucket ledger behaviour (Spec M4, T1a; owner Decision 1).

The allowance bucket (``credits.balance``) + PAYG lots (``payg_grants``) over M3's
ledger. Spend draws the allowance FIRST, then PAYG lots oldest-expiring first (FIFO);
``get_balance`` = allowance + Σ(unexpired lot remainders); expired lots are excluded;
``refund`` credits the allowance bucket. The M3 parity tests
(``test_credits_double_spend`` / ``test_credits_capture_up_to`` /
``test_credits_service_refund`` / ``test_credits_idempotent_billing`` /
``test_m2_proportional_credits``) stay green UNCHANGED — the allowance-only path is
byte-identical; this file pins the NEW two-bucket behaviour.

Runs against the superuser ``pg_engine`` (schema only) like the M3 credits tests. The
core service reads PAYG via its ``_payg_grants_t`` mirror while these tests write via
the canonical ``persona_api.db.models.payg_grants`` — so a mirror↔canonical drift
would fail here (the core-mirror drift guard).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

# ``capture_up_to`` / ``deduct_idempotent`` are core-service functions the api
# ``credits_service`` shim does not re-export (it exposes only deduct/refund/
# get_balance/ensure_balance/require_credits) — import them directly, as the M3
# parity tests do.
from persona.credits import capture_up_to, deduct_idempotent
from persona.errors import CreditsExhaustedError
from persona_api.db.models import credit_transactions as credit_tx_t
from persona_api.db.models import credits as credits_t
from persona_api.db.models import payg_grants as payg_t
from persona_api.services import credits_service
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_USER = "u_two_bucket"


@pytest.fixture
def seeded_engine(pg_engine: Engine) -> Engine:
    """Insert the FK target user row so credits / payg writes satisfy the constraint."""
    with pg_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": _USER, "e": f"{_USER}@x.test"},
        )
    return pg_engine


def _set_allowance(engine: Engine, user_id: str, balance: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            pg_insert(credits_t)
            .values(user_id=user_id, balance=balance)
            .on_conflict_do_update(index_elements=[credits_t.c.user_id], set_={"balance": balance})
        )


def _add_lot(
    engine: Engine,
    user_id: str,
    *,
    remaining: int,
    source_key: str,
    total: int | None = None,
    expires_in_days: float = 365.0,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            payg_t.insert().values(
                user_id=user_id,
                credits_total=total if total is not None else remaining,
                credits_remaining=remaining,
                expires_at=datetime.now(UTC) + timedelta(days=expires_in_days),
                source_billing_key=source_key,
            )
        )


def _allowance(engine: Engine, user_id: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                select(credits_t.c.balance).where(credits_t.c.user_id == user_id)
            ).scalar_one()
        )


def _lot_remaining(engine: Engine, source_key: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                select(payg_t.c.credits_remaining).where(payg_t.c.source_billing_key == source_key)
            ).scalar_one()
        )


def _ledger_count(engine: Engine, user_id: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                select(func.count())
                .select_from(credit_tx_t)
                .where(credit_tx_t.c.user_id == user_id)
            ).scalar_one()
        )


# --- get_balance = allowance + unexpired PAYG --------------------------------


def test_get_balance_is_allowance_plus_unexpired_payg(seeded_engine: Engine) -> None:
    _set_allowance(seeded_engine, _USER, 100)
    _add_lot(seeded_engine, _USER, remaining=50, source_key="lot1")
    assert credits_service.get_balance(rls_engine=seeded_engine, user_id=_USER) == 150


def test_expired_lot_excluded_from_balance(seeded_engine: Engine) -> None:
    _set_allowance(seeded_engine, _USER, 0)
    _add_lot(seeded_engine, _USER, remaining=100, source_key="expired", expires_in_days=-1.0)
    _add_lot(seeded_engine, _USER, remaining=10, source_key="live", expires_in_days=30.0)
    assert credits_service.get_balance(rls_engine=seeded_engine, user_id=_USER) == 10


# --- allowance-first, then FIFO PAYG draw ------------------------------------


def test_deduct_spills_allowance_then_payg(seeded_engine: Engine) -> None:
    """amount > allowance: draw the allowance to 0, then the remainder from PAYG."""
    _set_allowance(seeded_engine, _USER, 30)
    _add_lot(seeded_engine, _USER, remaining=100, source_key="lot")
    new_total = credits_service.deduct(
        rls_engine=seeded_engine, user_id=_USER, amount=50, reason="chat_turn"
    )
    assert new_total == 80  # 130 - 50
    assert _allowance(seeded_engine, _USER) == 0  # allowance drawn first, fully
    assert _lot_remaining(seeded_engine, "lot") == 80  # remainder (20) from PAYG


def test_deduct_from_payg_when_allowance_exhausted(seeded_engine: Engine) -> None:
    _set_allowance(seeded_engine, _USER, 0)
    _add_lot(seeded_engine, _USER, remaining=100, source_key="lot")
    new_total = credits_service.deduct(
        rls_engine=seeded_engine, user_id=_USER, amount=40, reason="chat_turn"
    )
    assert new_total == 60
    assert _allowance(seeded_engine, _USER) == 0
    assert _lot_remaining(seeded_engine, "lot") == 60


def test_fifo_draws_oldest_expiring_lot_first(seeded_engine: Engine) -> None:
    """Two live lots: the draw exhausts the sooner-expiring one before the later."""
    _set_allowance(seeded_engine, _USER, 0)
    _add_lot(seeded_engine, _USER, remaining=30, source_key="soon", expires_in_days=10.0)
    _add_lot(seeded_engine, _USER, remaining=30, source_key="later", expires_in_days=100.0)
    credits_service.deduct(rls_engine=seeded_engine, user_id=_USER, amount=40, reason="chat_turn")
    assert _lot_remaining(seeded_engine, "soon") == 0  # oldest-expiring drained first
    assert _lot_remaining(seeded_engine, "later") == 20  # then 10 from the later lot


def test_expired_lot_is_not_drawn(seeded_engine: Engine) -> None:
    """An expired lot is invisible to the draw — spendable excludes it, so an
    amount over the LIVE total hard-stops and the expired lot is untouched."""
    _set_allowance(seeded_engine, _USER, 0)
    _add_lot(seeded_engine, _USER, remaining=100, source_key="expired", expires_in_days=-1.0)
    _add_lot(seeded_engine, _USER, remaining=10, source_key="live", expires_in_days=30.0)
    credits_service.deduct(rls_engine=seeded_engine, user_id=_USER, amount=10, reason="chat_turn")
    assert _lot_remaining(seeded_engine, "live") == 0
    assert _lot_remaining(seeded_engine, "expired") == 100  # never touched
    with pytest.raises(CreditsExhaustedError):
        credits_service.deduct(
            rls_engine=seeded_engine, user_id=_USER, amount=5, reason="chat_turn"
        )
    assert _lot_remaining(seeded_engine, "expired") == 100  # still untouched after the raise


# --- strict exhaustion writes nothing ----------------------------------------


def test_deduct_over_total_spendable_raises_and_writes_nothing(seeded_engine: Engine) -> None:
    _set_allowance(seeded_engine, _USER, 10)
    _add_lot(seeded_engine, _USER, remaining=5, source_key="lot")
    before = _ledger_count(seeded_engine, _USER)
    with pytest.raises(CreditsExhaustedError):
        credits_service.deduct(
            rls_engine=seeded_engine, user_id=_USER, amount=20, reason="chat_turn"
        )
    assert _allowance(seeded_engine, _USER) == 10  # unchanged
    assert _lot_remaining(seeded_engine, "lot") == 5  # unchanged
    assert _ledger_count(seeded_engine, _USER) == before  # no ledger row


# --- capture spans buckets + shortfall ---------------------------------------


def test_capture_up_to_spans_buckets_and_marks_shortfall(seeded_engine: Engine) -> None:
    _set_allowance(seeded_engine, _USER, 10)
    _add_lot(seeded_engine, _USER, remaining=5, source_key="lot")
    captured, new_total = capture_up_to(
        rls_engine=seeded_engine, user_id=_USER, amount=20, reason="chat_turn:estimate"
    )
    assert captured == 15  # min(20, 10+5)
    assert new_total == 0
    assert _allowance(seeded_engine, _USER) == 0
    assert _lot_remaining(seeded_engine, "lot") == 0
    with seeded_engine.begin() as conn:
        reason = conn.execute(
            select(credit_tx_t.c.reason).where(credit_tx_t.c.user_id == _USER)
        ).scalar_one()
    assert reason == "chat_turn:estimate:shortfall"  # partial capture is marked


# --- refund credits the allowance bucket -------------------------------------


def test_refund_credits_allowance_even_when_charge_drew_payg(seeded_engine: Engine) -> None:
    """A charge that drew from PAYG, then refunded, lands in the ALLOWANCE bucket
    (never back into the lot) — the documented, approved edge."""
    _set_allowance(seeded_engine, _USER, 0)
    _add_lot(seeded_engine, _USER, remaining=100, source_key="lot")
    credits_service.deduct(rls_engine=seeded_engine, user_id=_USER, amount=40, reason="image_pre")
    assert _lot_remaining(seeded_engine, "lot") == 60  # drew from PAYG
    new_total = credits_service.refund(
        rls_engine=seeded_engine, user_id=_USER, amount=40, reason="image_refund"
    )
    assert new_total == 100  # 60 (lot) + 40 (allowance)
    assert _allowance(seeded_engine, _USER) == 40  # refund landed in the allowance bucket
    assert _lot_remaining(seeded_engine, "lot") == 60  # the lot is NOT restored


# --- idempotent re-delivery returns the two-bucket total ---------------------


def test_deduct_idempotent_redelivery_returns_two_bucket_total(seeded_engine: Engine) -> None:
    _set_allowance(seeded_engine, _USER, 50)
    _add_lot(seeded_engine, _USER, remaining=100, source_key="lot")
    first = deduct_idempotent(
        rls_engine=seeded_engine, user_id=_USER, amount=30, reason="bg", billing_key="k1"
    )
    assert first == 120  # 150 - 30
    # Re-delivery: same key → no further draw, returns the CURRENT two-bucket total.
    again = deduct_idempotent(
        rls_engine=seeded_engine, user_id=_USER, amount=30, reason="bg", billing_key="k1"
    )
    assert again == 120
    assert _allowance(seeded_engine, _USER) == 20  # 50 - 30, only drawn once
    assert _lot_remaining(seeded_engine, "lot") == 100  # untouched
    assert _ledger_count(seeded_engine, _USER) == 1  # one ledger row


# --- two-bucket double-spend race --------------------------------------------


def test_two_bucket_double_spend_race(seeded_engine: Engine) -> None:
    """8 threads, total spendable (allowance 3 + PAYG 2) covers exactly 5 → exactly
    5 succeed, 3 rejected, both buckets floor at 0, ledger == successes. The FOR
    UPDATE on the credits row serialises the concurrent draws (no overdraw)."""
    _set_allowance(seeded_engine, _USER, 3)
    _add_lot(seeded_engine, _USER, remaining=2, source_key="lot")

    def _attempt(_: int) -> bool:
        try:
            credits_service.deduct(rls_engine=seeded_engine, user_id=_USER, amount=1, reason="race")
        except CreditsExhaustedError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_attempt, range(8)))

    assert sum(results) == 5, "exactly the affordable count (3 allowance + 2 PAYG) succeed"
    assert credits_service.get_balance(rls_engine=seeded_engine, user_id=_USER) == 0
    assert _allowance(seeded_engine, _USER) == 0
    assert _lot_remaining(seeded_engine, "lot") == 0
    assert _ledger_count(seeded_engine, _USER) == 5
